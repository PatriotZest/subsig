"""
reports/builder_databricks.py

Glue between the Databricks notebooks and builder.py.

    extract (PDF -> CSV)  ->  assessment notebook (CSV -> `result` dict)  ->  THIS FILE  ->  builder.py

What is in here
---------------
    AnalysisResultSource   feeds the assessment notebook's `result` dict to the
                           builder (it implements builder.DataSource) and saves the
                           finished reports as JSON files (e.g. in a Unity Catalog Volume)
    make_ai_query_llm      LLM function for code running INSIDE Databricks
                           (same ai_query call your extract notebook already uses)
    make_sdk_llm           LLM function for running from VS Code with a Databricks token
    run_builder            one call: result dict -> every report
    main                   command line: python builder_databricks.py result.json

Inside Databricks (in a notebook under notebooks/, with `result` from the assessment
notebook). The first two lines let a notebook in notebooks/ import from reports/; if
your environment's working directory is not the notebook's folder, append the full
path of the reports folder instead:

    import sys, os
    sys.path.append(os.path.abspath(os.path.join(os.getcwd(), "..", "reports")))
    from builder_databricks import run_builder, make_ai_query_llm
    llm = make_ai_query_llm(spark, "databricks-meta-llama-3-3-70b-instruct")
    source, summary = run_builder(result, llm,
                                  reports_dir="/Volumes/<catalog>/<schema>/<volume>/reports")

From VS Code (save `result` as result.json first):

    python builder_databricks.py result.json --reports-dir out                  # numbers only
    python builder_databricks.py result.json --reports-dir out --endpoint <name>  # with LLM text
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

try:
    from .builder import build_all_reports
except ImportError:  # running as a script / notebook next to builder.py
    from builder import build_all_reports

logger = logging.getLogger(__name__)

TOPIC_COLS = ["topic_id", "name"]
QT_COLS = ["question_id", "topic_id", "confidence", "confirmed_by_teacher"]


def student_id_for(name: str) -> str:
    """Stable anonymous id from the student's name (the CSVs only have names)."""
    return "s_" + hashlib.sha256(name.strip().encode("utf-8")).hexdigest()[:10]


def _safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(part))


# --------------------------------------------------------------------------- #
# DataSource over the assessment notebook's result
# --------------------------------------------------------------------------- #
class AnalysisResultSource:
    """builder.DataSource built from the assessment notebook's `result` dict.

    Uses result["responses"], ["questions"] and ["students"]. One result = one
    assessment, so the test_id arguments are accepted but not used.

    topics:          optional list of curriculum topic names
    question_topics: optional {"Q1": ["Linear equations"], ...}; nothing in the
                     pipeline maps questions to topics yet, so without this the
                     reports simply have no topic sections.
    reports_dir:     where finished reports are saved as JSON (a Volume path works);
                     None keeps them in memory only
    anonymize_names: show "Student 1", "Student 2", ... instead of real names
    """

    def __init__(self, result: dict, *, topics: Optional[list[str]] = None,
                 question_topics: Optional[dict[str, list[str]]] = None,
                 reports_dir: Optional[str | Path] = None,
                 anonymize_names: bool = False):
        for key in ("responses", "questions"):
            if not result.get(key):
                raise ValueError(f"The analysis result has no {key!r}.")
        rows = result["responses"]

        names = sorted({r["student"] for r in rows})
        ids = {n: student_id_for(n) for n in names}
        self._students = pd.DataFrame({
            "student_id": [ids[n] for n in names],
            "display_name": [f"Student {i}" if anonymize_names else n
                             for i, n in enumerate(names, 1)],
        })

        rubric_title: dict[str, str] = {}
        for r in rows:
            if r.get("rubric_title") and r["question_id"] not in rubric_title:
                rubric_title[r["question_id"]] = r["rubric_title"]
        self._questions = pd.DataFrame([{
            "question_id": q["question_id"],
            "label": q["question_id"],                  # e.g. "Q2.1"
            "text": q.get("question_text") or "",
            "points": q.get("possible"),
            "rubric_title": rubric_title.get(q["question_id"]),
        } for q in result["questions"]])

        self._responses = pd.DataFrame([{
            "student_id": ids[r["student"]],
            "question_id": r["question_id"],
            "points_earned": r.get("earned"),
            "points_possible": r.get("possible"),
            "rubric_items": r.get("label") or "",       # the notebook's "Result" text
            "source": "gradescope",
        } for r in rows])
        for col in ("points_earned", "points_possible"):
            self._responses[col] = pd.to_numeric(self._responses[col], errors="coerce")

        mapping = question_topics or {}
        topic_names = list(topics or [])
        for names_ in mapping.values():
            for n in names_:
                if n not in topic_names:
                    topic_names.append(n)
        tid = {n: f"t{i}" for i, n in enumerate(topic_names, 1)}
        self._topics = pd.DataFrame({"topic_id": list(tid.values()),
                                     "name": list(tid.keys())}, columns=TOPIC_COLS)
        self._question_topics = pd.DataFrame(
            [{"question_id": q, "topic_id": tid[n], "confidence": 1.0,
              "confirmed_by_teacher": True}
             for q, ns in mapping.items() for n in ns], columns=QT_COLS)

        self._dir = Path(reports_dir) if reports_dir else None
        if self._dir:
            self._dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, str] = {}

    @classmethod
    def from_json(cls, path_or_text: str | Path, **kwargs) -> "AnalysisResultSource":
        s = str(path_or_text)
        text = s if s.lstrip().startswith("{") else Path(s).read_text(encoding="utf-8")
        return cls(json.loads(text), **kwargs)

    # -- builder.DataSource ---------------------------------------------------
    def get_topics(self) -> pd.DataFrame:
        return self._topics

    def get_questions(self, test_id: str) -> pd.DataFrame:
        return self._questions

    def get_question_topics(self, test_id: str) -> pd.DataFrame:
        return self._question_topics

    def get_students(self, test_id: str) -> pd.DataFrame:
        return self._students

    def get_responses(self, test_id: str) -> pd.DataFrame:
        return self._responses

    def get_item_stats(self, test_id: str) -> pd.DataFrame:
        return pd.DataFrame()   # the builder computes difficulty/discrimination/flags

    def get_topic_mastery(self, test_id: str) -> pd.DataFrame:
        return pd.DataFrame()   # the builder computes it from question_topics

    def save_report(self, *, report_id: str, test_id: str, kind: str,
                    subject_id: str, payload_json: str) -> None:
        self._cache[report_id] = payload_json
        path = self._path(test_id, kind, subject_id)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload_json, encoding="utf-8")

    def get_report(self, *, test_id: str, kind: str, subject_id: str) -> Optional[str]:
        rid = f"{test_id}:{kind}:{subject_id}"
        if rid in self._cache:
            return self._cache[rid]
        path = self._path(test_id, kind, subject_id)
        return path.read_text(encoding="utf-8") if path and path.exists() else None

    # -- helpers ---------------------------------------------------------------
    def _path(self, test_id: str, kind: str, subject_id: str) -> Optional[Path]:
        if not self._dir:
            return None
        name = "teacher.json" if kind == "teacher" else f"student_{_safe(subject_id)}.json"
        return self._dir / _safe(test_id) / name

    def list_students(self) -> list[dict]:
        """[{student_id, display_name}] for the UI's student picker."""
        return self._students.to_dict("records")


# --------------------------------------------------------------------------- #
# LLM functions: any callable  prompt (str) -> reply text (str)
# --------------------------------------------------------------------------- #
def make_ai_query_llm(spark, endpoint: str, max_tokens: int = 4096):
    """For code running inside Databricks. Same ai_query call pattern as the
    extraction notebook, without structured output (the builder validates JSON itself)."""

    def llm(prompt: str) -> str:
        row = spark.sql(
            """SELECT ai_query(
                 :endpoint, :request,
                 modelParameters => named_struct('temperature', 0.0, 'max_tokens', :token_limit)
               ) AS answer""",
            args={"endpoint": endpoint, "request": prompt, "token_limit": max_tokens},
        ).first()
        answer = row["answer"]
        if not isinstance(answer, str):
            raise ValueError("ai_query did not return text; check the endpoint name and quota")
        return answer

    return llm


def make_sdk_llm(endpoint: str, max_tokens: int = 4096):
    """For running from VS Code. Needs `pip install databricks-sdk` and
    DATABRICKS_HOST / DATABRICKS_TOKEN set (for example in your local .env)."""
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

    client = WorkspaceClient()

    def llm(prompt: str) -> str:
        resp = client.serving_endpoints.query(
            name=endpoint,
            messages=[ChatMessage(role=ChatMessageRole.USER, content=prompt)],
            max_tokens=max_tokens,
            temperature=0.0,
        )
        return resp.choices[0].message.content

    return llm


# --------------------------------------------------------------------------- #
# One call: result -> all reports
# --------------------------------------------------------------------------- #
def run_builder(result: dict, llm=None, *, test_id: str = "assessment",
                reports_dir: Optional[str | Path] = None, force: bool = False,
                max_workers: int = 2, **source_kwargs):
    """Build and save the teacher report plus one report per student.

    Returns (source, summary). Lower max_workers to 1 if the endpoint throttles you.
    Extra keyword arguments go to AnalysisResultSource (topics, question_topics,
    anonymize_names).
    """
    source = AnalysisResultSource(result, reports_dir=reports_dir, **source_kwargs)
    summary = build_all_reports(test_id, source, llm, force=force,
                                max_workers=max_workers)
    if reports_dir:   # student list for the frontend's picker
        out = Path(reports_dir) / _safe(test_id) / "students.json"
        out.write_text(json.dumps(source.list_students(), indent=2), encoding="utf-8")
    return source, summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Build reports from the assessment notebook's result JSON.")
    p.add_argument("result_json", type=Path)
    p.add_argument("--reports-dir", default="reports_out")
    p.add_argument("--endpoint", help="Databricks chat endpoint; omit for numbers-only reports")
    p.add_argument("--test-id", default="assessment")
    p.add_argument("--force", action="store_true", help="rebuild reports that already exist")
    p.add_argument("--anonymize", action="store_true", help='show "Student 1", ... not names')
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = json.loads(args.result_json.read_text(encoding="utf-8"))
    llm = make_sdk_llm(args.endpoint) if args.endpoint else None
    _, summary = run_builder(result, llm, test_id=args.test_id,
                             reports_dir=args.reports_dir, force=args.force,
                             anonymize_names=args.anonymize)
    print(json.dumps(summary, indent=2))
    return 1 if summary["students_failed"] else 0


if __name__ == "__main__":
    sys.exit(main())