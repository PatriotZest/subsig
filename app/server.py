"""Subsigmoid backend: serves the static site and the /api endpoints in site/API.md.

Runs locally (`python3 app/server.py`) or as a Databricks App (see app/app.yaml).
Standard library only. Talks to Databricks over REST with DATABRICKS_HOST + DATABRICKS_TOKEN.

Real data path:
  upload CSV  ->  Files API puts it in a Unity Catalog Volume
              ->  Jobs API runs the `assessment` job on it (DATABRICKS_JOB_ID)
              ->  job JSON (per-student / per-question stats)
              ->  a Llama endpoint tags each question with skills
              ->  skill estimates + knew/guess/slip/gap labels  ->  Report (API.md)
The sample class in site/data stays available next to uploaded exams.
"""
import io
import json
import math
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent


def load_env():
    for p in [HERE / ".env", HERE.parent / ".env"]:
        if p.exists():
            for line in p.read_text().splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


load_env()
SITE = Path(os.environ.get("SITE_DIR") or next(p for p in [HERE / "site", HERE.parent / "site"] if p.exists()))
HOST = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
if HOST and not HOST.startswith("http"):
    HOST = "https://" + HOST
TOKEN = os.environ.get("DATABRICKS_TOKEN", "")
JOB_ID = os.environ.get("DATABRICKS_JOB_ID", "")
UPLOAD_DIR = os.environ.get("SUBSIGMOID_UPLOAD_DIR", "/Volumes/workspace/pdfdatasets/datasetforpdf/subsigmoid_uploads")
TAG_MODEL = os.environ.get("SUBSIGMOID_TAG_MODEL", "databricks-meta-llama-3-3-70b-instruct")
PORT = int(os.environ.get("DATABRICKS_APP_PORT") or os.environ.get("PORT") or 8000)
STATE_FILE = Path(os.environ.get("SUBSIGMOID_STATE", HERE / "state.json"))

lock = threading.Lock()


# ----------------------------------------------------------------------------- Databricks REST
def dbx(method, path, body=None, raw=None, timeout=60):
    if not (HOST and TOKEN):
        raise RuntimeError("DATABRICKS_HOST and DATABRICKS_TOKEN are not set")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    data = None
    if raw is not None:
        data, headers["Content-Type"] = raw, "application/octet-stream"
    elif body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(HOST + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode() or "{}"
            return json.loads(txt) if txt.strip().startswith(("{", "[")) else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Databricks {method} {path}: {e.code} {e.read().decode()[:300]}")


def upload_to_volume(path, content):
    dbx("PUT", "/api/2.0/fs/files" + path + "?overwrite=true", raw=content, timeout=120)


def run_job(csv_path):
    r = dbx("POST", "/api/2.1/jobs/run-now",
            {"job_id": int(JOB_ID), "job_parameters": {"csv_path": csv_path, "return_json": "true"}})
    return r["run_id"]


def job_result(run_id):
    """Returns ('running', None) | ('done', dict) | ('failed', message)."""
    run = dbx("GET", f"/api/2.1/jobs/runs/get?run_id={run_id}")
    st = run.get("state", {})
    life, result = st.get("life_cycle_state"), st.get("result_state")
    if life in ("PENDING", "QUEUED", "RUNNING", "TERMINATING", "BLOCKED", "WAITING_FOR_RETRY"):
        return "running", None
    if result != "SUCCESS":
        return "failed", st.get("state_message") or f"{life} {result}"
    task_id = run["tasks"][0]["run_id"]
    out = dbx("GET", f"/api/2.1/jobs/runs/get-output?run_id={task_id}")
    text = (out.get("notebook_output") or {}).get("result")
    if not text:
        return "failed", out.get("error") or "Job finished without a JSON result"
    return "done", json.loads(text)


def chat(prompt, max_tokens=1200):
    r = dbx("POST", f"/serving-endpoints/{TAG_MODEL}/invocations",
            {"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "temperature": 0}, timeout=120)
    return r["choices"][0]["message"]["content"]


# ----------------------------------------------------------------------------- state
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"exams": [], "reports": {}, "tags": {}, "analyzed": [], "last_sync": None}


STATE = load_state()


def save_state():
    try:
        STATE_FILE.write_text(json.dumps(STATE))
    except Exception as e:  # ephemeral disk on Apps; state also lives in memory
        print("state not saved:", e)


def sample(name):
    return json.loads((SITE / "data" / name).read_text())


# ----------------------------------------------------------------------------- analysis: job JSON -> Report
def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "student"


def tag_skills(questions):
    """Ask the LLM for a short skill list and which skills each question needs. Sees question text only."""
    listing = "\n".join(f"{q['id']}: {q['text'][:500]}" for q in questions)
    prompt = (
        "You label exam questions with the skills they test, for a teacher's skill report.\n"
        "Propose 2 to 5 short skill names (2-4 words each) that together cover this exam, "
        "then list which skills each question requires (1 or 2 skills per question).\n"
        'Reply with JSON only: {"skills": ["..."], "items": {"Q1": ["skill name"], ...}}\n\nQuestions:\n' + listing)
    txt = chat(prompt)
    m = re.search(r"\{.*\}", txt, re.S)
    data = json.loads(m.group(0))
    names = [s.strip() for s in data["skills"] if s.strip()][:5]
    skills = [{"id": slug(n), "name": n} for n in names]
    by_name = {n.lower(): slug(n) for n in names}
    items = {}
    for q in questions:
        tags = [by_name.get(t.lower().strip()) for t in data.get("items", {}).get(q["id"], [])]
        items[q["id"]] = [t for t in tags if t] or [skills[0]["id"]]
    return skills, items


def build_report(exam, job):
    qs = [{"id": q["question_id"], "text": q.get("question_text") or q["question_id"],
           "possible": q.get("possible") or 0, "mean_fraction": q.get("mean_fraction"),
           "difficulty": q.get("difficulty")} for q in job.get("questions", [])]
    warnings = [{"kind": "job", "text": w} for w in job.get("warnings", [])]
    try:
        skills, qmap = tag_skills(qs)
        tag_source = "ai"
    except Exception as e:
        skills, qmap, tag_source = [{"id": "all", "name": "Whole exam"}], {q["id"]: ["all"] for q in qs}, "fallback"
        warnings.append({"kind": "tagging", "text": f"Skill tagging failed, so the whole exam is treated as one skill ({e})."})

    # fraction of points per student per question
    frac, earned, possible = {}, {}, {q["id"]: q["possible"] for q in qs}
    for r in job.get("responses", []):
        p = r.get("possible") or possible.get(r["question_id"]) or 0
        if p:
            frac[(r["student"], r["question_id"])] = max(0.0, min(1.0, (r.get("earned") or 0) / p))
        earned[(r["student"], r["question_id"])] = r.get("earned") or 0
    names = [s["student"] for s in job.get("students", [])]
    # IRT layer from the job: Rasch ability and expected score over the modeled questions
    irt = {s["student"]: {"ability": round(s["proficiency"], 2), "score": round(s["estimated_true_score"], 2),
                          "max": s.get("modeled_max_points")}
           for s in job.get("students", []) if s.get("proficiency") is not None and s.get("estimated_true_score") is not None}

    # Per-skill DINA-style estimate: a student with the skill answers right unless they slip (s);
    # without it, right only by guessing (g). Partial credit counts as a fractional right answer.
    # The class base rate for each skill is fitted with a few EM steps.
    G, S = 0.2, 0.15

    def post(fs, prior):
        l1 = sum(f * math.log(1 - S) + (1 - f) * math.log(S) for f in fs)
        l0 = sum(f * math.log(G) + (1 - f) * math.log(1 - G) for f in fs)
        m = max(l1, l0)
        a1, a0 = prior * math.exp(l1 - m), (1 - prior) * math.exp(l0 - m)
        return a1 / (a1 + a0)

    skill_fs = {k["id"]: {n: [frac[(n, q["id"])] for q in qs if k["id"] in qmap[q["id"]] and (n, q["id"]) in frac] for n in names} for k in skills}
    base = {}
    for k in skills:
        prior = 0.6
        for _ in range(8):
            ps = [post(fs, prior) for fs in skill_fs[k["id"]].values() if fs]
            prior = min(.95, max(.05, sum(ps) / len(ps))) if ps else prior
        base[k["id"]] = prior

    students = []
    for n in names:
        mastery = {}
        for k in skills:
            fs = skill_fs[k["id"]][n]
            p = post(fs, base[k["id"]]) if fs else base[k["id"]]
            half = 1.1 * math.sqrt(max(p * (1 - p), .02) / (len(fs) + 1))
            mastery[k["id"]] = {"p": round(p, 2), "lo": round(max(0, p - half), 2), "hi": round(min(1, p + half), 2)}
        answers = []
        for q in qs:
            if (n, q["id"]) not in frac:
                continue
            f, need = frac[(n, q["id"])], [mastery[s]["p"] for s in qmap[q["id"]]]
            has = min(need) if need else .5
            right = f >= .7
            if right:
                label, p = ("knew", has) if has >= .5 else ("guess", 1 - has)
            else:
                label, p = ("slip", has) if has >= .5 else ("gap", 1 - has)
            if 0.4 <= has <= 0.6:
                label, p = "unclear", max(has, 1 - has)
            answers.append({"item": q["id"], "points": round(earned[(n, q["id"])], 2), "max_points": q["possible"],
                            "rubric": next((r.get("label") or "" for r in job.get("responses", []) if r["student"] == n and r["question_id"] == q["id"]), ""),
                            "label": label, "p": round(p, 2)})
        slips = sum(a["label"] == "slip" for a in answers); gaps = sum(a["label"] == "gap" for a in answers)
        guesses = sum(a["label"] == "guess" for a in answers)
        students.append({"id": slug(n), "name": n, "score": round(sum(a["points"] for a in answers), 2),
                         "max_score": round(sum(a["max_points"] for a in answers), 2), "mastery": mastery, "answers": answers,
                         "irt": irt.get(n),
                         "score_note": "understated" if slips >= 2 and gaps == 0 else "overstated" if guesses >= 2 else None})

    items = []
    for q in qs:
        col = [frac[(n, q["id"])] for n in names if (n, q["id"]) in frac]
        pv = sum(col) / len(col) if col else 0
        disc = 0.0
        if len(col) >= 3:
            rest = [sum(frac.get((n, o["id"]), 0) for o in qs if o["id"] != q["id"]) for n in names if (n, q["id"]) in frac]
            mr, mc = sum(rest) / len(rest), pv
            cov = sum((a - mr) * (b - mc) for a, b in zip(rest, col)) / len(col)
            sr = math.sqrt(sum((a - mr) ** 2 for a in rest) / len(rest)); sc = math.sqrt(sum((b - mc) ** 2 for b in col) / len(col))
            disc = cov / (sr * sc) if sr and sc else 0.0
        flags = ["Strong and weak students did about equally well, so it says little about who knows the material."] if len(col) >= 5 and disc < .12 else []
        diff = q["difficulty"] if q["difficulty"] is not None else round(-math.log((pv * len(col) + .5) / ((1 - pv) * len(col) + .5)), 2) if col else 0
        items.append({"id": q["id"], "prompt": re.sub(r"\s+", " ", q["text"]).strip()[:140], "skills": qmap[q["id"]],
                      "max_points": q["possible"], "percent_correct": round(pv, 2), "difficulty": diff,
                      "discrimination": round(disc, 2), "guess": G, "slip": S, "flags": flags})

    groups = [{"skill": k["id"], "students": [s["id"] for s in students if s["mastery"][k["id"]]["p"] < .3]} for k in skills]
    mistakes = [{"skill": k["id"],
                 "gaps": sum(a["label"] == "gap" for s in students for a in s["answers"] if k["id"] in qmap[a["item"]]),
                 "slips": sum(a["label"] == "slip" for s in students for a in s["answers"] if k["id"] in qmap[a["item"]])} for k in skills]
    if len(students) < 5:
        warnings.append({"kind": "small_class", "text": f"Only {len(students)} student(s) in this file, so skill estimates are rough."})
    return {"id": exam["id"], "test": {"name": exam["name"], "date": exam["date"], "course": "Uploaded exam",
                                       "source": exam["source"], "n_students": len(students), "n_items": len(items)},
            "skills": skills, "items": items, "students": students, "groups": groups, "mistakes": mistakes,
            "warnings": warnings, "tag_source": tag_source, "job_summary": job.get("summary")}


def watch(exam_id):
    """Background: poll the Databricks run, then build and store the report."""
    while True:
        with lock:
            exam = next((e for e in STATE["exams"] if e["id"] == exam_id), None)
        if not exam or exam["status"] != "analyzing":
            return
        try:
            status, payload = job_result(exam["run_id"])
        except Exception as e:
            status, payload = "running", None
            print("poll error", e)
        if status == "running":
            time.sleep(5)
            continue
        with lock:
            if status == "done":
                try:
                    STATE["reports"][exam_id] = build_report(exam, payload)
                    exam.update(status="analyzed", report_id=exam_id,
                                n_students=STATE["reports"][exam_id]["test"]["n_students"],
                                n_items=STATE["reports"][exam_id]["test"]["n_items"])
                except Exception as e:
                    exam.update(status="failed", error=f"Could not build the report: {e}")
            else:
                exam.update(status="failed", error=str(payload))
            save_state()
        return


for _e in STATE["exams"]:
    if _e.get("status") == "analyzing":
        threading.Thread(target=watch, args=(_e["id"],), daemon=True).start()


# ----------------------------------------------------------------------------- course, students
def course():
    c = sample("course.json")
    c["sample"] = False
    for e in c["exams"]:
        if e["id"] in STATE["analyzed"]:
            e.update(status="analyzed", report_id="quiz-3", report_note="sample report reused")
    with lock:
        c["exams"] = [dict(e) for e in STATE["exams"]] + c["exams"]
    if STATE.get("last_sync"):
        c["gradescope"]["last_sync"] = STATE["last_sync"]
    c["gradescope"]["connected"] = False  # no public Gradescope API; exams arrive by CSV upload
    c["databricks"] = {"connected": bool(HOST and TOKEN), "job_id": JOB_ID}
    return c


def report(rid):
    with lock:
        if rid in STATE["reports"]:
            return STATE["reports"][rid]
    f = SITE / "data" / "reports" / f"{Path(rid).name}.json"
    if f.exists():
        return json.loads(f.read_text())
    raise KeyError(rid)


def student(cid, sid):
    c = course()
    exams = []
    for e in sorted([x for x in c["exams"] if x.get("report_id") and x["status"] == "analyzed" and not x.get("report_note")], key=lambda x: x["date"]):
        r = report(e["report_id"])
        s = next((x for x in r["students"] if x["id"] == sid), None)
        if s:
            exams.append({"exam_id": e["id"], "name": e["name"], "date": e["date"], "score": s["score"], "max_score": s["max_score"],
                          "mastery": s["mastery"], "score_note": s["score_note"], "skills": r["skills"],
                          "answers": [{**a, "prompt": next((i["prompt"] for i in r["items"] if i["id"] == a["item"]), "")} for a in s["answers"]]})
    skills = report(exams[-1]["exam_id"])["skills"] if exams else c["skills"]
    name = next((s["name"] for s in c["students"] if s["id"] == sid), None)
    if not name and exams:
        name = next(x["name"] for x in report(exams[-1]["exam_id"])["students"] if x["id"] == sid)
    return {"student": {"id": sid, "name": name} if name else None, "skills": skills, "exams": exams}


# ----------------------------------------------------------------------------- quiz + live (in memory)
QUIZ = sample("quiz.json")
SESSIONS = {}


def quiz_public():
    return {**QUIZ, "items": [{**it, "options": [{"id": o["id"], "text": o["text"]} for o in it["options"]]} for it in QUIZ["items"]]}


def predict(sid, item_id):
    s = SESSIONS[sid]; counts = {}
    for it in QUIZ["items"]:
        pick = next((o for o in it["options"] if o["id"] == s["answers"].get(it["id"])), None)
        if pick and pick.get("mistake"):
            counts[pick["mistake"]] = counts.get(pick["mistake"], 0) + 1
        if pick and not pick.get("correct") and "neg" in it["skills"]:
            counts["neg-sub"] = counts.get("neg-sub", 0) + .5
    target = next(i for i in QUIZ["items"] if i["id"] == item_id)
    best, n = next(o for o in target["options"] if o.get("correct")), 0
    for o in target["options"]:
        if o.get("mistake") and counts.get(o["mistake"], 0) > n:
            best, n = o, counts[o["mistake"]]
    pred = {"item": item_id, "option": best["id"], "correct": bool(best.get("correct")), "mistake": best.get("mistake"),
            "mistake_text": QUIZ["mistakes"].get(best.get("mistake")) if best.get("mistake") else None,
            "p": round(.71 if best.get("correct") else min(.9, .48 + .14 * n), 2), "made_at": time.time()}
    s["prediction"] = pred
    return pred


def result(sid):
    s = SESSIONS[sid]; seen = {}; answers = []
    for it in QUIZ["items"]:
        o = next((x for x in it["options"] if x["id"] == s["answers"].get(it["id"])), {})
        if o.get("mistake"):
            seen[o["mistake"]] = seen.get(o["mistake"], 0) + 1
        answers.append({"item": it["id"], "option": o.get("id"), "right": bool(o.get("correct")), "mistake": o.get("mistake"), "skills": it["skills"]})
    for a in answers:
        a.update({"label": "knew", "p": .8} if a["right"] else {"label": "gap", "p": .82} if a["mistake"] and seen[a["mistake"]] >= 2 else {"label": "slip", "p": .64})
    mastery = []
    for k in QUIZ["skills"]:
        rel = [a for a in answers if k["id"] in a["skills"]]
        mastery.append({"skill": k["id"], "name": k["name"], "p": round((sum(a["right"] for a in rel) + .5) / (len(rel) + 1), 2)})
    return {"answers": answers, "mastery": mastery, "mistakes": QUIZ["mistakes"], "participants": len(SESSIONS)}


def live():
    made = [s for s in SESSIONS.values() if s.get("prediction") and QUIZ["predict_item"] in s["answers"]]
    hits = [s for s in made if s["answers"][QUIZ["predict_item"]] == s["prediction"]["option"]]
    recent = sorted(SESSIONS.items(), key=lambda kv: kv[1]["updated"], reverse=True)[:8]
    return {"simulated": False, "session_id": "room", "participants": len(SESSIONS),
            "finished": sum(len(s["answers"]) >= len(QUIZ["items"]) for s in SESSIONS.values()),
            "predictions": {"made": len(made), "hits": len(hits)},
            "recent": [{"participant": f"Participant {s['n']}", "item": f"Q{len(s['answers'])}", "prompt": "", "done": len(s["answers"]),
                        "predicted": QUIZ["predict_item"] in s["answers"] and bool(s.get("prediction")),
                        "hit": QUIZ["predict_item"] in s["answers"] and s.get("prediction", {}).get("option") == s["answers"].get(QUIZ["predict_item"])}
                       for _, s in recent],
            "evaluation": [{"id": "raw", "name": "Raw score", "uses": "percent right overall", "accuracy": None},
                           {"id": "count", "name": "Skill counting", "uses": "percent right per skill", "accuracy": None},
                           {"id": "irt", "name": "IRT alone", "uses": "question curves", "accuracy": None},
                           {"id": "subsigmoid", "name": "Subsigmoid", "uses": "curves, skills, tagged wrong options", "accuracy": None}]}


# ----------------------------------------------------------------------------- HTTP
MIDTERM = [["Q1", "3/8 + 1/4", ["frac"]], ["Q2", "(−6) × 4", ["neg"]], ["Q3", "9 − 6 ÷ 3", ["order"]], ["Q4", "2/5 × 15", ["frac"]],
           ["Q5", "−7 − (−10)", ["neg"]], ["Q6", "(3 + 5) × 2 − 4", ["order"]], ["Q7", "−3/4 + 1/2", ["frac", "neg"]],
           ["Q8", "12 ÷ (−3) + 2", ["neg", "order"]], ["Q9", "5/6 ÷ 2/3", ["frac"]], ["Q10", "(−2)³", ["neg"]],
           ["Q11", "4 + 6 × 2 − 8 ÷ 4", ["order"]], ["Q12", "1 2/3 − 3/4", ["frac"]], ["Q13", "−5 × (2 − 7)", ["neg", "order"]],
           ["Q14", "Order from least to greatest: −1/2, 1/3, −2/3", ["frac", "neg"]]]


class Handler(BaseHTTPRequestHandler):
    server_version = "Subsigmoid/1"

    def log_message(self, fmt, *args):
        print("%s %s" % (self.command, self.path))

    def send(self, code, obj=None, body=None, ctype="application/json"):
        data = body if body is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def route(self, method):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if p.startswith("/api/"):
                return self.api(method, p[4:].strip("/").split("/"), q)
            if method != "GET":
                return self.send(405, {"error": "method not allowed"})
            return self.static(p)
        except KeyError as e:
            self.send(404, {"error": f"not found: {e}"})
        except Exception as e:
            print("error", e)
            self.send(500, {"error": str(e)})

    def static(self, p):
        rel = "index.html" if p in ("", "/") else p.lstrip("/")
        f = (SITE / rel).resolve()
        if not str(f).startswith(str(SITE.resolve())) or not f.is_file():
            return self.send(404, body=b"Not found", ctype="text/plain")
        data = f.read_bytes()
        if f.suffix == ".html":  # switch the page to this backend
            data = data.replace(b'<!-- <meta name="subsigmoid-api" content="https://YOUR-APP.databricksapps.com"> -->',
                                b'<meta name="subsigmoid-api" content="self">')
        self.send(200, body=data, ctype=mimetypes.guess_type(str(f))[0] or "application/octet-stream")

    def api(self, method, parts, q):
        P = parts
        if P == ["health"]:
            return self.send(200, {"ok": True, "databricks": bool(HOST and TOKEN), "job_id": JOB_ID})
        if P == ["courses"]:
            c = course(); return self.send(200, [{"id": c["id"], "name": c["name"], "term": c["term"]}])
        if P[0] == "courses" and len(P) == 2:
            return self.send(200, course())
        if P[0] == "courses" and P[2:] == ["sync"] and method == "POST":
            STATE["last_sync"] = time.strftime("%Y-%m-%dT%H:%M:%S"); save_state()
            return self.send(200, {"last_sync": STATE["last_sync"], "new_exams": 0})
        if P[0] == "courses" and P[2:] == ["uploads"] and method == "POST":
            return self.upload()
        if P[0] == "courses" and len(P) == 4 and P[2] == "students":
            return self.send(200, student(P[1], P[3]))
        if P[0] == "reports" and len(P) == 2:
            return self.send(200, report(P[1]))
        if P[0] == "exams" and len(P) == 3 and P[2] == "tags":
            eid = P[1]
            if method == "PUT":
                STATE["tags"][eid] = self.body_json(); save_state(); return self.send(200, {"ok": True})
            if eid in STATE["tags"]:
                return self.send(200, STATE["tags"][eid])
            c = sample("course.json")
            if eid == "midterm":
                return self.send(200, {"exam_id": eid, "skills": c["skills"], "items": [{"id": i, "prompt": t, "skills": s, "source": "ai"} for i, t, s in MIDTERM]})
            r = report(eid)
            return self.send(200, {"exam_id": eid, "skills": r["skills"], "items": [{"id": i["id"], "prompt": i["prompt"], "skills": i["skills"], "source": "approved"} for i in r["items"]]})
        if P[0] == "exams" and len(P) == 3 and P[2] == "analyze" and method == "POST":
            if P[1] not in STATE["analyzed"]:
                STATE["analyzed"].append(P[1]); save_state()
            return self.send(200, {"report_id": "quiz-3" if P[1] == "midterm" else P[1]})
        if P == ["live"]:
            return self.send(200, live())
        if P == ["quiz"]:
            return self.send(200, quiz_public())
        if P[:2] == ["quiz", "sessions"]:
            if len(P) == 2 and method == "POST":
                sid = uuid.uuid4().hex[:10]
                SESSIONS[sid] = {"answers": {}, "n": len(SESSIONS) + 1, "updated": time.time()}
                return self.send(200, {"session_id": sid, "participants": len(SESSIONS)})
            sid = P[2]
            if sid not in SESSIONS:
                raise KeyError("session")
            if P[3:] == ["answers"] and method == "POST":
                b = self.body_json()
                SESSIONS[sid]["answers"][b["item"]] = b["option"]; SESSIONS[sid]["updated"] = time.time()
                return self.send(200, {"ok": True})
            if P[3:] == ["prediction"]:
                item = q.get("item", [QUIZ["predict_item"]])[0]
                if item in SESSIONS[sid]["answers"]:
                    return self.send(409, {"error": "already answered; predictions must come first"})
                return self.send(200, predict(sid, item))
            if P[3:] == ["result"]:
                return self.send(200, result(sid))
        return self.send(404, {"error": "unknown endpoint"})

    def upload(self):
        from email.parser import BytesParser
        from email.policy import default as email_policy
        n = int(self.headers.get("Content-Length") or 0)
        msg = BytesParser(policy=email_policy).parsebytes(
            b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + self.rfile.read(n))
        part = next((pt for pt in msg.iter_parts() if pt.get_param("name", header="content-disposition") == "file"), None)
        if part is None:
            return self.send(400, {"error": "No file field in the upload."})
        content = part.get_payload(decode=True) or b""
        name = Path(part.get_filename() or "exam.csv").stem
        text = content.decode("utf-8-sig", errors="replace")
        rows = list(__import__("csv").reader(io.StringIO(text)))
        header = [h.strip() for h in rows[0]] if rows else []
        if not {"Student", "Question"}.issubset(header) and not any(h.lower() == "studentname" for h in header):
            return self.send(400, {"error": "Expected a graded CSV with Student and Question columns (the format the PDF extraction notebook writes)."})
        si, qi = (header.index("Student"), header.index("Question")) if "Student" in header else (0, None)
        students = {r[si] for r in rows[1:] if len(r) > si and r[si].strip()}
        questions = {r[qi] for r in rows[1:] if qi is not None and len(r) > qi and r[qi].strip()}
        eid = "up-" + uuid.uuid4().hex[:8]
        path = f"{UPLOAD_DIR}/{eid}.csv"
        upload_to_volume(path, content)
        run_id = run_job(path)
        exam = {"id": eid, "name": name.replace("_", " ").replace("-", " ").strip() or "Uploaded exam", "date": time.strftime("%Y-%m-%d"),
                "status": "analyzing", "n_students": len(students), "n_items": len(questions), "report_id": None,
                "source": "Uploaded CSV · Databricks", "volume_path": path, "run_id": run_id,
                "run_url": f"{HOST}/#job/{JOB_ID}/run/{run_id}", "started": time.time()}
        with lock:
            STATE["exams"].insert(0, exam); save_state()
        threading.Thread(target=watch, args=(eid,), daemon=True).start()
        return self.send(200, {"exam": exam})


if __name__ == "__main__":
    print(f"Subsigmoid on http://localhost:{PORT}  (site: {SITE}, databricks: {'on' if HOST and TOKEN else 'off'})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
