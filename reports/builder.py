"""
reports/builder.py  (Issue #3: combine final data into reports)

Turns the Delta tables produced by ingestion (#4, #1) and analysis (#2) into two
kinds of cached JSON reports:

    StudentReport  one per student
    TeacherReport  one per test

Design rules
------------
1. Every number comes from the tables and is computed in code (pandas/numpy).
2. The LLM only writes narrative fields (explanations, study plan, rewrites).
   It receives the numbers as input and cannot change them: its JSON is
   validated against a narrative-only schema and merged into the fact-built
   report.
3. If the LLM fails twice, the report is still produced with
   narrative_available=False, so the demo never breaks.
4. Reports are cached in the `reports` table so the UI loads instantly.

Gradescope / extract.py data
----------------------------
    - Question ids are strings such as "Q2.1" (sub-questions exist), so questions
      are identified by a string `label`, never an int.
    - There are no student answers and no answer key. The rubric items the grader
      applied (`responses.rubric_items`) are the evidence for WHY points were lost.
    - Question text is best-effort OCR and may be empty or wrong. Set
      `questions.text_verified` = False for those; the builder will not suggest
      rewrites for questions whose text is empty or unverified.

The builder is decoupled from Databricks and from any LLM vendor:
    - DataSource: Protocol with the read/write methods listed below
    - LLMFn:      any callable str -> str (prompt in, text out)
Requires pydantic v2, pandas, numpy.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Literal, Optional, Protocol, Union

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
WEAK_TOPIC_THRESHOLD = 60.0       # topic score (%) below this counts as "weak"
SMALL_CLASS_WARNING = 20          # item statistics are noisy below this size
MAX_QUESTIONS_TO_EXPLAIN = 10     # cap missed questions sent to the LLM per student
MAX_FLAGGED_TO_REWRITE = 8        # cap flagged questions sent to the LLM per test
LOW_OCR_CONFIDENCE = 0.7
HIST_BINS = 10

# Thresholds for the built-in item analysis (used when the source supplies no item_stats)
TOO_HARD_BELOW = 0.30             # mean fraction of points earned
TOO_EASY_ABOVE = 0.95
POOR_DISCRIMINATION_BELOW = 0.20
MIN_STUDENTS_FOR_DISCRIMINATION = 10

ErrorType = Literal["conceptual", "procedural", "careless", "unknown"]
LLMFn = Callable[[str], str]


# --------------------------------------------------------------------------- #
# Data access contract (implement this over Databricks, or over mock DataFrames)
# --------------------------------------------------------------------------- #
class DataSource(Protocol):
    """Each get_* returns a pandas DataFrame with AT LEAST these columns."""

    def get_topics(self) -> pd.DataFrame:
        """topic_id, name"""

    def get_questions(self, test_id: str) -> pd.DataFrame:
        """question_id, label (e.g. "Q2.1"), text, points
        optional: rubric_title, text_verified (bool), correct_answer
        (legacy: `number` is used as the label when `label` is absent)"""

    def get_question_topics(self, test_id: str) -> pd.DataFrame:
        """question_id, topic_id, confidence, confirmed_by_teacher"""

    def get_students(self, test_id: str) -> pd.DataFrame:
        """student_id, display_name"""

    def get_responses(self, test_id: str) -> pd.DataFrame:
        """student_id, question_id, points_earned, points_possible
        optional: rubric_items (list, JSON list string, or the ' | '-joined
        'Result' string from extract.py), answer_text, selected_option,
        source, confidence"""

    def get_item_stats(self, test_id: str) -> pd.DataFrame:
        """question_id, difficulty, discrimination, flags
        optional: distractor_counts (dict or JSON string)
        flags may be a list, a JSON list string, or a comma-separated string
        May return an EMPTY DataFrame: the builder then computes these from
        `responses` (see compute_item_stats). `difficulty` means fraction of
        points earned (low = hard), not a Rasch logit."""

    def get_topic_mastery(self, test_id: str) -> pd.DataFrame:
        """student_id, topic_id, score_pct, n_questions  (may be empty)"""

    def save_report(self, *, report_id: str, test_id: str, kind: str,
                    subject_id: str, payload_json: str) -> None: ...

    def get_report(self, *, test_id: str, kind: str,
                   subject_id: str) -> Optional[str]:
        """Return cached payload_json or None."""


# --------------------------------------------------------------------------- #
# Report schemas (move to a shared models file if the backend/frontend need them)
# --------------------------------------------------------------------------- #
class TopicPerformance(BaseModel):
    topic_id: str
    topic: str
    pct: float
    class_avg: float
    n_questions: int
    is_weak: bool


class MissedQuestion(BaseModel):
    question_id: str
    question_label: str                      # e.g. "Q2.1"
    text: str = ""                           # may be empty if OCR failed
    text_verified: bool = True
    rubric_title: Optional[str] = None       # grader's label, NOT an answer key
    topics: list[str] = Field(default_factory=list)
    points_earned: float
    points_possible: float
    rubric_items: list[str] = Field(default_factory=list)  # what the grader applied
    student_answer: Optional[str] = None     # usually missing with Gradescope data
    correct_answer: Optional[str] = None     # only if a real answer key exists
    answer_source: Optional[str] = None
    ocr_confidence: Optional[float] = None
    # narrative (LLM) fields
    likely_error: ErrorType = "unknown"
    explanation: str = ""


class StudentReport(BaseModel):
    kind: Literal["student"] = "student"
    test_id: str
    student_id: str
    student_name: str
    overall_pct: float
    class_avg_pct: float
    topics: list[TopicPerformance]
    weak_topics: list[TopicPerformance]
    missed_questions: list[MissedQuestion]
    # narrative (LLM) fields
    summary: str = ""
    study_plan: list[str] = Field(default_factory=list)
    narrative_available: bool = False
    generated_at: str


class HistogramBin(BaseModel):
    range_label: str
    count: int


class ScoreDistribution(BaseModel):
    n_students: int
    mean: float
    median: float
    std: float
    min: float
    max: float
    histogram: list[HistogramBin]


class TopicClassPerformance(BaseModel):
    topic_id: str
    topic: str
    class_avg: float
    n_questions: int
    n_students_below_threshold: int
    pct_students_below_threshold: float


class FlaggedQuestion(BaseModel):
    question_id: str
    question_label: str                      # e.g. "Q2.1"
    text: str = ""
    text_verified: bool = True               # False -> no rewrite is suggested
    rubric_title: Optional[str] = None
    topics: list[str] = Field(default_factory=list)
    correct_answer: Optional[str] = None
    difficulty: Optional[float] = None
    discrimination: Optional[float] = None
    flags: list[str] = Field(default_factory=list)
    wrong_answer_breakdown: dict[str, int] = Field(default_factory=dict)  # MC options
    # most common rubric items applied to students who lost points: {item: n students}
    rubric_item_breakdown: dict[str, int] = Field(default_factory=dict)
    # narrative (LLM) fields
    issue_diagnosis: str = ""
    suggested_rewrite: str = ""
    rewrite_rationale: str = ""


class TeacherReport(BaseModel):
    kind: Literal["teacher"] = "teacher"
    test_id: str
    distribution: ScoreDistribution
    topic_performance: list[TopicClassPerformance]   # weakest first
    flagged_questions: list[FlaggedQuestion]         # most suspicious first
    small_class_warning: bool
    # narrative (LLM) fields
    reteach_summary: str = ""
    reteach_topics: list[str] = Field(default_factory=list)
    narrative_available: bool = False
    generated_at: str


# Narrative-only schemas: this is all the LLM is allowed to return.
class _StudentQuestionNarrative(BaseModel):
    question: Union[str, int]               # label, e.g. "Q2.1" (or "2.1")
    likely_error: ErrorType = "unknown"
    explanation: str = ""


class _StudentNarrative(BaseModel):
    summary: str = ""
    study_plan: list[str] = Field(default_factory=list)
    questions: list[_StudentQuestionNarrative] = Field(default_factory=list)


class _FlaggedNarrative(BaseModel):
    question: Union[str, int]               # label, e.g. "Q2.1" (or "2.1")
    issue_diagnosis: str = ""
    suggested_rewrite: str = ""
    rewrite_rationale: str = ""


class _TeacherNarrative(BaseModel):
    reteach_summary: str = ""
    reteach_topics: list[str] = Field(default_factory=list)
    flagged: list[_FlaggedNarrative] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(v):
    """NaN/NA -> None, leave everything else alone."""
    if v is None or isinstance(v, (list, dict, tuple, set)):
        return v
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def _as_list(v) -> list[str]:
    v = _clean(v)
    if v is None:
        return []
    if hasattr(v, "tolist"):
        v = v.tolist()
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v]
    s = str(v).strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            return [str(x) for x in json.loads(s)]
        except json.JSONDecodeError:
            pass
    return [p.strip() for p in s.split(",") if p.strip()]


def _as_dict(v) -> dict[str, int]:
    v = _clean(v)
    if v is None:
        return {}
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            return {}
    if isinstance(v, dict):
        return {str(k): int(c) for k, c in v.items()}
    return {}


_NO_RUBRIC_ITEM = "(no rubric item marked)"


def _str_or_none(v) -> Optional[str]:
    v = _clean(v)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _as_rubric_items(v) -> list[str]:
    """The grader's applied rubric items, e.g. ["forgot units (-1 pts)"].

    Accepts a list, a JSON list string, or extract.py's ' | '-joined string.
    Rubric text can contain commas, so we never split on commas here.
    """
    v = _clean(v)
    if v is None:
        return []
    if hasattr(v, "tolist"):
        v = v.tolist()
    if isinstance(v, (list, tuple, set)):
        items = [str(x) for x in v]
    else:
        s = str(v).strip()
        items = None
        if s.startswith("["):
            try:
                items = [str(x) for x in json.loads(s)]
            except json.JSONDecodeError:
                items = None
        if items is None:
            items = s.split(" | ")
    return [i.strip() for i in items if i.strip() and i.strip() != _NO_RUBRIC_ITEM]


def _countable_item(item: str) -> Optional[str]:
    """Key for counting a rubric item. None for plain "Correct" items."""
    text = re.sub(r"\s*\(\s*[+\-−–]?\s*\d+(?:\.\d+)?\s*pts?\s*\)\s*$", "",
                  item, flags=re.I).strip()
    if not text or text.lower().rstrip(".") == "correct":
        return None
    return item.strip()


def _norm_label(v) -> str:
    """'Q2.1', 'q 2.1', '2.1', 2.1 -> '2.1' so LLM replies match our labels."""
    s = str(v).strip().lower()
    s = re.sub(r"^(question|q)\s*\.?\s*", "", s)
    return s.strip()


def _question_label(q: dict, fallback) -> str:
    v = _clean(q.get("label"))
    if v is None:
        v = _clean(q.get("number"))
    if v is None:
        return str(fallback)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def _text_verified(q: dict) -> bool:
    """Empty text is never verified. Neither is text containing the markers the
    extraction notebook leaves for things it could not read ("[unreadable]",
    "[FIGURE: ...]"). Otherwise trust the text_verified column, and assume
    verified if the source doesn't provide one."""
    text = _str_or_none(q.get("text"))
    if text is None:
        return False
    low = text.lower()
    if "[unreadable" in low or "[figure" in low:
        return False
    flag = _clean(q.get("text_verified"))
    if flag is None:
        return True
    return str(flag).strip().lower() not in ("false", "0", "no", "")


def _pct(earned: float, possible: float) -> float:
    return round(100.0 * earned / possible, 1) if possible else 0.0


def _opt_float(v) -> Optional[float]:
    v = _clean(v)
    return None if v is None else round(float(v), 3)


def _extract_json(text: str) -> dict:
    """Parse a JSON object out of an LLM reply (tolerates ``` fences / chatter)."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in LLM reply")
    return json.loads(text[start:end + 1])


def _call_llm_json(llm: LLMFn, prompt: str, schema: type[BaseModel],
                   retries: int = 1):
    """Call the LLM and validate against `schema`. Returns None if it never works."""
    for attempt in range(retries + 1):
        p = prompt if attempt == 0 else (
            prompt + "\n\nYour previous reply was not valid. "
                     "Reply with ONLY the JSON object, nothing else.")
        try:
            return schema.model_validate(_extract_json(llm(p)))
        except Exception as exc:  # LLM/API errors and bad JSON both land here
            logger.warning("LLM narrative attempt %d failed: %s", attempt + 1, exc)
    logger.error("LLM narrative failed after %d attempts", retries + 1)
    return None


# --------------------------------------------------------------------------- #
# Item statistics (used when the source supplies none)
# --------------------------------------------------------------------------- #
def compute_item_stats(responses: pd.DataFrame) -> pd.DataFrame:
    """Difficulty, discrimination and flags per question, computed from scores.

    difficulty      mean fraction of points earned (0-1). LOW means hard.
                    This is NOT the Rasch logit in the analysis notebook's
                    questions[].difficulty, where HIGH means hard.
    discrimination  correlation between the fraction earned on this question and
                    the student's average fraction on all OTHER questions. Only
                    computed for classes of MIN_STUDENTS_FOR_DISCRIMINATION or more.
    flags           too_hard, too_easy, poor_discrimination, negative_discrimination
    """
    cols = ["question_id", "difficulty", "discrimination", "flags"]
    r = responses.copy()
    r["points_earned"] = pd.to_numeric(r["points_earned"], errors="coerce")
    r["points_possible"] = pd.to_numeric(r["points_possible"], errors="coerce")
    r = r[(r["points_possible"] > 0) & r["points_earned"].notna()]
    if r.empty:
        return pd.DataFrame(columns=cols)
    r["frac"] = (r["points_earned"] / r["points_possible"]).clip(0, 1)
    m = r.pivot_table(index="student_id", columns="question_id",
                      values="frac", aggfunc="first")
    out = []
    for qid in m.columns:
        difficulty = float(m[qid].mean())
        disc = None
        if len(m) >= MIN_STUDENTS_FOR_DISCRIMINATION and m.shape[1] >= 2:
            c = m[qid].corr(m.drop(columns=qid).mean(axis=1))
            disc = None if pd.isna(c) else float(c)
        flags = []
        if difficulty < TOO_HARD_BELOW:
            flags.append("too_hard")
        if difficulty > TOO_EASY_ABOVE:
            flags.append("too_easy")
        if disc is not None:
            if disc < 0:
                flags.append("negative_discrimination")
            elif disc < POOR_DISCRIMINATION_BELOW:
                flags.append("poor_discrimination")
        out.append({"question_id": str(qid), "difficulty": round(difficulty, 3),
                    "discrimination": None if disc is None else round(disc, 3),
                    "flags": flags})
    return pd.DataFrame(out, columns=cols)


# --------------------------------------------------------------------------- #
# Per-test context: load every table ONCE, precompute class-level aggregates
# --------------------------------------------------------------------------- #
class _TestContext:
    def __init__(self, test_id: str, source: DataSource):
        self.test_id = test_id
        self.questions = source.get_questions(test_id)
        self.responses = source.get_responses(test_id).copy()
        self.item_stats = source.get_item_stats(test_id)
        self.question_topics = source.get_question_topics(test_id)
        self.students = source.get_students(test_id)
        topics = source.get_topics()

        if self.responses.empty:
            raise ValueError(f"No responses found for test_id={test_id!r}")

        # No item_stats from upstream (the analysis notebook doesn't produce
        # discrimination or flags) -> compute them here, before NaN scores become 0.
        if self.item_stats is None or self.item_stats.empty:
            self.item_stats = compute_item_stats(self.responses)

        self.responses["points_earned"] = self.responses["points_earned"].fillna(0)

        self._q_by_id = {str(r["question_id"]): r
                         for r in self.questions.to_dict("records")}
        self._topic_name = {str(r["topic_id"]): str(r["name"])
                            for r in topics.to_dict("records")}
        self._student_name = {str(r["student_id"]): str(r["display_name"])
                              for r in self.students.to_dict("records")}

        # question_id -> [topic names], confirmed + high-confidence first
        qt = self.question_topics.copy()
        if not qt.empty:
            qt["confirmed_by_teacher"] = qt.get(
                "confirmed_by_teacher", False)
            qt["confidence"] = qt.get("confidence", 1.0)
            qt = qt.sort_values(["confirmed_by_teacher", "confidence"],
                                ascending=False)
        self._q_topics: dict[str, list[str]] = {}
        self._q_topic_ids: dict[str, list[str]] = {}
        for r in qt.to_dict("records"):
            qid, tid = str(r["question_id"]), str(r["topic_id"])
            self._q_topics.setdefault(qid, []).append(self.topic_name(tid))
            self._q_topic_ids.setdefault(qid, []).append(tid)

        mastery = source.get_topic_mastery(test_id)
        if mastery is None or mastery.empty:
            mastery = self._compute_mastery()
        self.mastery = mastery
        self.class_topic_avg = mastery.groupby("topic_id")["score_pct"].mean()

        totals = self.responses.groupby("student_id")[
            ["points_earned", "points_possible"]].sum()
        self.student_pct = (100.0 * totals["points_earned"]
                            / totals["points_possible"].replace(0, np.nan)
                            ).fillna(0.0)

    # -- lookups -------------------------------------------------------------
    def topic_name(self, topic_id: str) -> str:
        return self._topic_name.get(str(topic_id), str(topic_id))

    def student_name(self, student_id: str) -> str:
        return self._student_name.get(str(student_id), str(student_id))

    def question(self, question_id: str) -> dict:
        return self._q_by_id.get(str(question_id), {})

    def question_topic_names(self, question_id: str) -> list[str]:
        return self._q_topics.get(str(question_id), [])

    def n_questions_in_topic(self, topic_id: str) -> int:
        return sum(1 for ids in self._q_topic_ids.values() if str(topic_id) in ids)

    # -- fallback if student_topic_mastery wasn't populated by #2 ----------------
    def _compute_mastery(self) -> pd.DataFrame:
        if self.question_topics.empty:
            return pd.DataFrame(columns=["student_id", "topic_id",
                                         "score_pct", "n_questions"])
        j = self.responses.merge(
            self.question_topics[["question_id", "topic_id"]], on="question_id")
        g = j.groupby(["student_id", "topic_id"]).agg(
            earned=("points_earned", "sum"),
            possible=("points_possible", "sum"),
            n_questions=("question_id", "nunique")).reset_index()
        g["score_pct"] = 100.0 * g["earned"] / g["possible"].replace(0, np.nan)
        g["score_pct"] = g["score_pct"].fillna(0.0)
        return g[["student_id", "topic_id", "score_pct", "n_questions"]]


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
STUDENT_PROMPT = """You are a supportive tutor writing feedback for one student on a paper test.

RULES
- Use ONLY the facts in the JSON below. Never invent scores, questions or answers.
- Do not change or recompute any numbers.
- Refer to each question by its question_label exactly as given (for example "Q2.1").
- rubric_items are what the grader actually marked on this student's work (for example
  "forgot units (-1 pts)"). They are your main evidence for why points were lost:
  base likely_error and the explanation on them.
- rubric_title is the grader's short label for the question. It is NOT an answer key.
  Never present it as the correct answer.
- If "text" is empty or text_verified is false, the question wording is unknown or may be
  misread. Do not guess what the question asked; explain from rubric_items and rubric_title only.
- student_answer is usually missing. Never invent what the student wrote.
- likely_error must be one of: conceptual, procedural, careless, unknown.
  Use "unknown" if rubric_items is empty or unclear.
- If ocr_confidence is below {low_conf}, the transcription may be wrong. Say so instead of guessing.
- Keep each explanation to 1-3 sentences, plain and encouraging, and say how to fix the mistake.
- study_plan: 3-5 concrete steps in priority order, each tied to a weak topic or missed question.
- summary: 2-3 sentences on overall performance and the main thing to focus on.

Return ONLY a JSON object of this shape:
{{"summary": "...", "study_plan": ["..."], "questions": [{{"question": "Q1", "likely_error": "conceptual", "explanation": "..."}}]}}

FACTS
{facts}
"""

TEACHER_PROMPT = """You are an assessment-design consultant helping a teacher improve a test.

RULES
- Use ONLY the facts in the JSON below. Never invent statistics, questions or answers.
- Do not change or recompute any numbers.
- Refer to each question by its question_label exactly as given (for example "Q2.1").
- rubric_item_breakdown counts the rubric items graders most often applied to students who
  lost points on that question. Use it as evidence of what went wrong.
  rubric_title is the grader's short label for the question. It is NOT an answer key.
- If a question's text is empty or text_verified is false, its wording is unknown or may be
  misread. For that question set suggested_rewrite to "" and rewrite_rationale to "", base
  issue_diagnosis only on the statistics and rubric items, and say the question text needs checking.
- For each flagged question: issue_diagnosis = why the statistics suggest a problem
  (ambiguous wording, miskeyed answer, off-curriculum, too hard, too easy, attractive distractor).
  Be tentative where the data is thin ("may", "suggests").
- suggested_rewrite = ONE clearer version of the question that tests the same concept at the
  same difficulty. Do not change what the correct answer is, unless you diagnose a likely miskey,
  in which case say so in rewrite_rationale.
- rewrite_rationale = 1-2 sentences tying the rewrite to the evidence.
- reteach_summary: 2-4 sentences on what to reteach, based on topic_performance.
- reteach_topics: the topic names most worth reteaching (max 5).
- If small_class_warning is true, note that the statistics are based on a small class.

Return ONLY a JSON object of this shape:
{{"reteach_summary": "...", "reteach_topics": ["..."], "flagged": [{{"question": "Q1", "issue_diagnosis": "...", "suggested_rewrite": "...", "rewrite_rationale": "..."}}]}}

FACTS
{facts}
"""


# --------------------------------------------------------------------------- #
# Student report
# --------------------------------------------------------------------------- #
def _student_facts(ctx: _TestContext, student_id: str) -> StudentReport:
    resp = ctx.responses[ctx.responses["student_id"] == student_id]
    if resp.empty:
        raise ValueError(f"No responses for student_id={student_id!r}")

    overall = _pct(resp["points_earned"].sum(), resp["points_possible"].sum())
    class_avg = round(float(ctx.student_pct.mean()), 1)

    # topics
    mine = ctx.mastery[ctx.mastery["student_id"] == student_id]
    topics: list[TopicPerformance] = []
    for r in mine.to_dict("records"):
        tid = str(r["topic_id"])
        pct = round(float(r["score_pct"]), 1)
        topics.append(TopicPerformance(
            topic_id=tid,
            topic=ctx.topic_name(tid),
            pct=pct,
            class_avg=round(float(ctx.class_topic_avg.get(r["topic_id"], pct)), 1),
            n_questions=int(r.get("n_questions") or ctx.n_questions_in_topic(tid)),
            is_weak=pct < WEAK_TOPIC_THRESHOLD,
        ))
    topics.sort(key=lambda t: t.pct)  # weakest first
    weak = [t for t in topics if t.is_weak]

    # missed questions (any lost points), biggest loss first
    missed: list[MissedQuestion] = []
    lost = resp[resp["points_earned"] < resp["points_possible"]].copy()
    lost["_lost"] = lost["points_possible"] - lost["points_earned"]
    lost = lost.sort_values("_lost", ascending=False)
    for r in lost.to_dict("records"):
        q = ctx.question(r["question_id"])
        answer = _clean(r.get("answer_text")) or _clean(r.get("selected_option"))
        missed.append(MissedQuestion(
            question_id=str(r["question_id"]),
            question_label=_question_label(q, r["question_id"]),
            text=_str_or_none(q.get("text")) or "",
            text_verified=_text_verified(q),
            rubric_title=_str_or_none(q.get("rubric_title")),
            topics=ctx.question_topic_names(r["question_id"]),
            points_earned=float(r["points_earned"]),
            points_possible=float(r["points_possible"]),
            rubric_items=_as_rubric_items(r.get("rubric_items")),
            student_answer=None if answer is None else str(answer),
            correct_answer=_str_or_none(q.get("correct_answer")),
            answer_source=_clean(r.get("source")),
            ocr_confidence=_opt_float(r.get("confidence")),
        ))

    return StudentReport(
        test_id=ctx.test_id, student_id=str(student_id),
        student_name=ctx.student_name(student_id),
        overall_pct=overall, class_avg_pct=class_avg,
        topics=topics, weak_topics=weak, missed_questions=missed,
        generated_at=_now(),
    )


def _student_prompt(report: StudentReport) -> str:
    facts = {
        "overall_pct": report.overall_pct,
        "class_avg_pct": report.class_avg_pct,
        "weak_topics": [t.model_dump(include={"topic", "pct", "class_avg"})
                        for t in report.weak_topics],
        "missed_questions": [
            q.model_dump(include={"question_label", "text", "text_verified",
                                  "rubric_title", "topics",
                                  "points_earned", "points_possible",
                                  "rubric_items", "student_answer",
                                  "correct_answer", "ocr_confidence"})
            for q in report.missed_questions[:MAX_QUESTIONS_TO_EXPLAIN]],
    }
    return STUDENT_PROMPT.format(low_conf=LOW_OCR_CONFIDENCE,
                                 facts=json.dumps(facts, indent=2))


def build_student_report(test_id: str, student_id: str, source: DataSource,
                         llm: Optional[LLMFn] = None, *,
                         _ctx: Optional[_TestContext] = None) -> StudentReport:
    ctx = _ctx or _TestContext(test_id, source)
    report = _student_facts(ctx, student_id)

    if llm is not None and (report.missed_questions or report.weak_topics):
        narrative = _call_llm_json(llm, _student_prompt(report), _StudentNarrative)
        if narrative is not None:
            by_label = {_norm_label(n.question): n for n in narrative.questions}
            for q in report.missed_questions:
                n = by_label.get(_norm_label(q.question_label))
                if n:
                    q.likely_error, q.explanation = n.likely_error, n.explanation
            report.summary = narrative.summary
            report.study_plan = narrative.study_plan
            report.narrative_available = True
    return report


# --------------------------------------------------------------------------- #
# Teacher report
# --------------------------------------------------------------------------- #
def _distribution(ctx: _TestContext) -> ScoreDistribution:
    v = ctx.student_pct.to_numpy(dtype=float)
    counts, edges = np.histogram(v, bins=HIST_BINS, range=(0, 100))
    hist = [HistogramBin(range_label=f"{int(edges[i])}-{int(edges[i + 1])}",
                         count=int(counts[i])) for i in range(HIST_BINS)]
    return ScoreDistribution(
        n_students=int(len(v)),
        mean=round(float(v.mean()), 1), median=round(float(np.median(v)), 1),
        std=round(float(v.std(ddof=1)) if len(v) > 1 else 0.0, 1),
        min=round(float(v.min()), 1), max=round(float(v.max()), 1),
        histogram=hist)


def _topic_performance(ctx: _TestContext) -> list[TopicClassPerformance]:
    out = []
    for tid, grp in ctx.mastery.groupby("topic_id"):
        below = int((grp["score_pct"] < WEAK_TOPIC_THRESHOLD).sum())
        out.append(TopicClassPerformance(
            topic_id=str(tid), topic=ctx.topic_name(tid),
            class_avg=round(float(grp["score_pct"].mean()), 1),
            n_questions=ctx.n_questions_in_topic(tid),
            n_students_below_threshold=below,
            pct_students_below_threshold=round(100.0 * below / len(grp), 1)))
    out.sort(key=lambda t: t.class_avg)  # weakest first
    return out


def _wrong_answer_breakdown(ctx: _TestContext, question_id: str,
                            from_stats) -> dict[str, int]:
    precomputed = _as_dict(from_stats)
    if precomputed:
        return precomputed
    r = ctx.responses[(ctx.responses["question_id"] == question_id)
                      & (ctx.responses["points_earned"]
                         < ctx.responses["points_possible"])]
    col = None
    for c in ("selected_option", "answer_text"):
        if c in r.columns and r[c].notna().any():
            col = c
            break
    if col is None:
        return {}
    counts = r[col].dropna().astype(str).str.strip().value_counts().head(5)
    return {k: int(c) for k, c in counts.items()}


def _rubric_item_breakdown(ctx: _TestContext, question_id: str) -> dict[str, int]:
    """Most common rubric items applied to students who lost points on this
    question: {item text: number of students}. Plain "Correct" items are skipped."""
    if "rubric_items" not in ctx.responses.columns:
        return {}
    r = ctx.responses[(ctx.responses["question_id"] == question_id)
                      & (ctx.responses["points_earned"]
                         < ctx.responses["points_possible"])]
    counts: dict[str, int] = {}
    for raw in r["rubric_items"]:
        keys = {k for k in map(_countable_item, _as_rubric_items(raw)) if k}
        for k in keys:                       # once per student
            counts[k] = counts.get(k, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1])[:5])


def _flagged_questions(ctx: _TestContext) -> list[FlaggedQuestion]:
    out = []
    for r in ctx.item_stats.to_dict("records"):
        flags = _as_list(r.get("flags"))
        if not flags:
            continue
        qid = str(r["question_id"])
        q = ctx.question(qid)
        out.append(FlaggedQuestion(
            question_id=qid,
            question_label=_question_label(q, qid),
            text=_str_or_none(q.get("text")) or "",
            text_verified=_text_verified(q),
            rubric_title=_str_or_none(q.get("rubric_title")),
            topics=ctx.question_topic_names(qid),
            correct_answer=_str_or_none(q.get("correct_answer")),
            difficulty=_opt_float(r.get("difficulty")),
            discrimination=_opt_float(r.get("discrimination")),
            flags=flags,
            wrong_answer_breakdown=_wrong_answer_breakdown(
                ctx, qid, r.get("distractor_counts")),
            rubric_item_breakdown=_rubric_item_breakdown(ctx, qid),
        ))
    # most suspicious first: lowest (or negative) discrimination
    out.sort(key=lambda f: (f.discrimination is None, f.discrimination or 0.0))
    return out


def _teacher_prompt(report: TeacherReport) -> str:
    facts = {
        "n_students": report.distribution.n_students,
        "small_class_warning": report.small_class_warning,
        "score_mean": report.distribution.mean,
        "score_median": report.distribution.median,
        "topic_performance": [t.model_dump() for t in report.topic_performance],
        "flagged_questions": [
            f.model_dump(include={"question_label", "text", "text_verified",
                                  "rubric_title", "topics",
                                  "correct_answer", "difficulty",
                                  "discrimination", "flags",
                                  "wrong_answer_breakdown",
                                  "rubric_item_breakdown"})
            for f in report.flagged_questions[:MAX_FLAGGED_TO_REWRITE]],
    }
    return TEACHER_PROMPT.format(facts=json.dumps(facts, indent=2))


def build_teacher_report(test_id: str, source: DataSource,
                         llm: Optional[LLMFn] = None, *,
                         _ctx: Optional[_TestContext] = None) -> TeacherReport:
    ctx = _ctx or _TestContext(test_id, source)
    dist = _distribution(ctx)
    report = TeacherReport(
        test_id=test_id, distribution=dist,
        topic_performance=_topic_performance(ctx),
        flagged_questions=_flagged_questions(ctx),
        small_class_warning=dist.n_students < SMALL_CLASS_WARNING,
        generated_at=_now())

    if llm is not None:
        narrative = _call_llm_json(llm, _teacher_prompt(report), _TeacherNarrative)
        if narrative is not None:
            by_label = {_norm_label(n.question): n for n in narrative.flagged}
            for f in report.flagged_questions:
                n = by_label.get(_norm_label(f.question_label))
                if n:
                    f.issue_diagnosis = n.issue_diagnosis
                    # enforced in code, not just in the prompt: never offer a
                    # rewrite of question text we could not read reliably
                    f.suggested_rewrite = n.suggested_rewrite if f.text_verified else ""
                    f.rewrite_rationale = n.rewrite_rationale if f.text_verified else ""
            report.reteach_summary = narrative.reteach_summary
            report.reteach_topics = narrative.reteach_topics
            report.narrative_available = True
    return report


# --------------------------------------------------------------------------- #
# Orchestration + caching (call this at the end of the analysis job)
# --------------------------------------------------------------------------- #
def _report_id(test_id: str, kind: str, subject_id: str) -> str:
    return f"{test_id}:{kind}:{subject_id}"


def _save(source: DataSource, test_id: str, kind: str, subject_id: str,
          report: BaseModel) -> None:
    source.save_report(report_id=_report_id(test_id, kind, subject_id),
                       test_id=test_id, kind=kind, subject_id=subject_id,
                       payload_json=report.model_dump_json())


def build_all_reports(test_id: str, source: DataSource,
                      llm: Optional[LLMFn] = None, *, force: bool = False,
                      max_workers: int = 4) -> dict:
    """Build + cache the teacher report and one report per student.

    Skips reports that are already cached unless force=True. A failure for one
    student is logged and does not stop the rest.
    """
    ctx = _TestContext(test_id, source)
    summary = {"teacher_built": False, "students_built": 0,
               "students_skipped": 0, "students_failed": []}

    # teacher report
    if force or source.get_report(test_id=test_id, kind="teacher",
                                  subject_id=test_id) is None:
        _save(source, test_id, "teacher", test_id,
              build_teacher_report(test_id, source, llm, _ctx=ctx))
        summary["teacher_built"] = True

    # student reports (LLM calls run in threads; saves stay on this thread)
    student_ids = [str(s) for s in ctx.responses["student_id"].unique()]
    todo = []
    for sid in student_ids:
        if not force and source.get_report(test_id=test_id, kind="student",
                                           subject_id=sid) is not None:
            summary["students_skipped"] += 1
        else:
            todo.append(sid)

    def _safe(sid: str):
        try:
            return sid, build_student_report(test_id, sid, source, llm, _ctx=ctx)
        except Exception:
            logger.exception("Student report failed for %s", sid)
            return sid, None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for sid, rep in pool.map(_safe, todo):
            if rep is None:
                summary["students_failed"].append(sid)
            else:
                _save(source, test_id, "student", sid, rep)
                summary["students_built"] += 1
    return summary


def get_cached_report(test_id: str, kind: Literal["student", "teacher"],
                      subject_id: str, source: DataSource) -> Optional[dict]:
    """For the FastAPI GET routes: return the cached report as a dict, or None."""
    raw = source.get_report(test_id=test_id, kind=kind, subject_id=subject_id)
    return json.loads(raw) if raw else None