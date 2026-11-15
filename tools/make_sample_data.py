"""Generate the front end's sample data: one course with four Gradescope exams.

Writes site/data/course.json and site/data/reports/<exam>.json in the shapes documented in
site/API.md. Numbers come from a small DINA simulation with exact posteriors, so they hang
together, but they are made up. The real analysis belongs to the backend.
"""
import itertools, json, math, os, random

random.seed(14)
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "site", "data")
SKILLS = [("frac", "Fractions"), ("neg", "Negative numbers"), ("order", "Order of operations")]
K = [s for s, _ in SKILLS]
PROFILES = list(itertools.product([0, 1], repeat=len(K)))
PRIOR = {"frac": .75, "neg": .6, "order": .7}
NAMES = ["Ana", "Ben", "Dev", "Mai", "Sam", "Lia", "Omar", "Kai", "Zoe", "Raj", "Eli", "Noa", "Ivy", "Leo",
         "Ada", "Max", "Tess", "Jon", "Uma", "Theo", "Rosa", "Finn", "Nia", "Cal", "Wes", "Ines", "Hugo", "Mila",
         "Yuki", "Ezra", "June", "Pax", "Remy", "Sol", "Vera", "Ike", "Lou", "Bea"]

# item: id, prompt, skills, rubric deduction, guess, slip
EXAMS = [
    ("quiz-1", "Quiz 1", "2026-09-19", [
        ("Q1", "2/3 + 1/6", ["frac"], "Did not find a common denominator", .15, .15),
        ("Q2", "(−5) + 3", ["neg"], "Sign error", .25, .12),
        ("Q3", "6 + 2 × 5", ["order"], "Order of operations", .15, .12),
        ("Q4", "3/5 × 10", ["frac"], "Multiplied the denominator", .2, .15),
        ("Q5", "−4 − 6", ["neg"], "Sign error", .2, .14),
        ("Q6", "18 ÷ 3 × 2", ["order"], "Divided last", .2, .15),
        ("Q7", "1/4 + 2/4", ["frac"], "Added denominators", .3, .1),
        ("Q8", "(−2) × 3 + 1", ["neg", "order"], "Sign error", .12, .18),
    ]),
    ("quiz-2", "Quiz 2", "2026-10-10", [
        ("Q1", "5/6 − 1/3", ["frac"], "Did not find a common denominator", .15, .15),
        ("Q2", "(−8) ÷ (−2)", ["neg"], "Sign error", .2, .12),
        ("Q3", "(4 + 2) × 3", ["order"], "Ignored parentheses", .2, .12),
        ("Q4", "−1/2 + 1/4", ["frac", "neg"], "Sign error", .12, .18),
        ("Q5", "7 − (−3)", ["neg"], "Subtracted instead of added", .18, .14),
        ("Q6", "20 − 3 × 4", ["order"], "Order of operations", .15, .14),
        ("Q7", "2/3 ÷ 1/3", ["frac"], "Divided across", .2, .16),
        ("Q8", "−3 + 4 × (−2)", ["neg", "order"], "Order of operations", .1, .18),
        ("Q9", "1 1/2 + 2 3/4", ["frac"], "Mixed number error", .15, .2),
    ]),
    ("quiz-3", "Quiz 3", "2026-10-31", [
        ("Q1", "1/2 + 1/3", ["frac"], "Added numerators and denominators", .15, .15),
        ("Q2", "(−3) × (−4)", ["neg"], "Sign error", .20, .12),
        ("Q3", "2 + 3 × 4", ["order"], "Order of operations", .15, .15),
        ("Q4", "1/2 − 3/4", ["frac", "neg"], "Sign error", .12, .18),
        ("Q5", "5 − (−2)", ["neg"], "Subtracted instead of added", .18, .14),
        ("Q6", "10 − 4 ÷ 2", ["order"], "Order of operations", .15, .15),
        ("Q7", "Which is larger, 3/8 or 2/5?", ["frac"], "Compared denominators", .50, .35),
        ("Q8", "4 − 2 × (−3)", ["neg", "order"], "Sign error", .10, .18),
        ("Q9", "3/4 × 2/3", ["frac"], "Cross-multiplied", .15, .15),
    ]),
]
FIXED = {("quiz-1", "Ana"): [1, 0, 1, 1, 0, 1, 1, 0], ("quiz-2", "Ana"): [1, 0, 1, 0, 0, 1, 1, 0, 1],
         ("quiz-3", "Ana"): [1, 0, 1, 1, 0, 0, 1, 0, 1],
         ("quiz-1", "Ben"): [1, 1, 1, 1, 1, 1, 0, 1], ("quiz-2", "Ben"): [1, 1, 0, 1, 1, 1, 1, 1, 1],
         ("quiz-3", "Ben"): [0, 1, 0, 1, 0, 1, 0, 1, 1]}
THRESH = {"frac": -.9, "neg": -.35, "order": -.7}

# each student's lasting tendencies; skills are correlated through one overall ability and improve over time
traits = {}
for name in NAMES:
    th = random.gauss(0, 1)
    traits[name] = {s: th + random.gauss(0, .7) for s in K}

def eta(profile, skills): return all(profile[K.index(s)] for s in skills)
def p_right(profile, it): return 1 - it[5] if eta(profile, it[2]) else it[4]

def make_report(t, exam_id, exam_name, date, ITEMS):
    students = []
    for name in NAMES:
        y = FIXED.get((exam_id, name))
        if y is None:
            prof = tuple(int(traits[name][s] + .3 * (t - 2) > THRESH[s]) for s in K)
            y = [int(random.random() < p_right(prof, it)) for it in ITEMS]
        prior = [math.prod(PRIOR[s] if b else 1 - PRIOR[s] for s, b in zip(K, pr)) for pr in PROFILES]
        like = [math.prod(p_right(pr, it) if yy else 1 - p_right(pr, it) for it, yy in zip(ITEMS, y)) for pr in PROFILES]
        post = [a * b for a, b in zip(prior, like)]; z = sum(post); post = [p / z for p in post]
        mastery = {}
        for k, s in enumerate(K):
            p = sum(pp for pr, pp in zip(PROFILES, post) if pr[k])
            n = sum(1 for it in ITEMS if s in it[2])
            half = 1.1 * math.sqrt(max(p * (1 - p), .02) / (n + 1))
            mastery[s] = {"p": round(p, 2), "lo": round(max(0, p - half), 2), "hi": round(min(1, p + half), 2)}
        answers = []
        for it, yy in zip(ITEMS, y):
            has = sum(pp for pr, pp in zip(PROFILES, post) if eta(pr, it[2]))
            if yy: label, p = ("knew", has) if has >= .6 else ("guess", 1 - has) if has <= .4 else ("unclear", max(has, 1 - has))
            else:  label, p = ("slip", has) if has >= .6 else ("gap", 1 - has) if has <= .4 else ("unclear", max(has, 1 - has))
            answers.append({"item": it[0], "points": 2 * yy, "max_points": 2, "rubric": "Correct" if yy else it[3],
                            "label": label, "p": round(p, 2)})
        slips = sum(a["label"] == "slip" for a in answers); gaps = sum(a["label"] == "gap" for a in answers)
        guesses = sum(a["label"] == "guess" for a in answers)
        students.append({"id": name.lower(), "name": name, "score": sum(a["points"] for a in answers),
                         "max_score": 2 * len(ITEMS), "mastery": mastery, "answers": answers,
                         "score_note": "understated" if slips >= 2 and gaps == 0 else "overstated" if guesses >= 2 else None})
    items, tot = [], [s["score"] for s in students]
    for j, it in enumerate(ITEMS):
        col = [s["answers"][j]["points"] / 2 for s in students]; pv = sum(col) / len(col)
        rest = [a - 2 * c for a, c in zip(tot, col)]; mr = sum(rest) / len(rest)
        cov = sum((r - mr) * (c - pv) for r, c in zip(rest, col)) / len(col)
        sr = math.sqrt(sum((r - mr) ** 2 for r in rest) / len(rest)); sc = math.sqrt(pv * (1 - pv)) or 1
        disc = cov / (sr * sc) if sr else 0
        flags = ["Strong and weak students did about equally well, so it says little about who knows the material."] if disc < .12 else []
        items.append({"id": it[0], "prompt": it[1], "skills": it[2], "max_points": 2, "percent_correct": round(pv, 2),
                      "difficulty": round(-math.log((pv * len(col) + .5) / ((1 - pv) * len(col) + .5)), 2),
                      "discrimination": round(disc, 2), "guess": it[4], "slip": it[5], "flags": flags})
    warnings = [{"item": i["id"], "kind": "low_discrimination", "text": f} for i in items for f in i["flags"]]
    for s, nm in SKILLS:
        solo = [i for i in items if i["skills"] == [s]]
        if len(solo) < 2:
            warnings.append({"skill": s, "kind": "few_items", "text": f"Only {len(solo)} question tests {nm.lower()} on its own. Add one more to separate it from other skills."})
    groups = [{"skill": s, "students": [st["id"] for st in students if st["mastery"][s]["p"] < .3]} for s in K]
    mistakes = [{"skill": s, "gaps": sum(st["answers"][j]["label"] == "gap" for st in students for j, it in enumerate(ITEMS) if s in it[2]),
                 "slips": sum(st["answers"][j]["label"] == "slip" for st in students for j, it in enumerate(ITEMS) if s in it[2])} for s in K]
    return {"id": exam_id, "sample": True,
            "test": {"name": exam_name, "date": date, "course": "Pre-algebra · Section 3", "source": "Gradescope export",
                     "n_students": len(students), "n_items": len(items)},
            "skills": [{"id": s, "name": n} for s, n in SKILLS],
            "items": items, "students": students, "groups": groups, "mistakes": mistakes, "warnings": warnings}

os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
exams = []
for t, (eid, name, date, ITEMS) in enumerate(EXAMS):
    r = make_report(t, eid, name, date, ITEMS)
    json.dump(r, open(os.path.join(ROOT, "reports", eid + ".json"), "w"), ensure_ascii=False, indent=1)
    exams.append({"id": eid, "name": name, "date": date, "status": "analyzed", "n_students": len(NAMES),
                  "n_items": len(ITEMS), "report_id": eid, "source": "Gradescope export"})
    print(name, [(g["skill"], len(g["students"])) for g in r["groups"]], "flags", sum(bool(i["flags"]) for i in r["items"]))
exams.append({"id": "midterm", "name": "Midterm", "date": "2026-11-12", "status": "needs_tags", "n_students": len(NAMES),
              "n_items": 14, "report_id": None, "source": "Gradescope export"})
course = {"id": "prealg-3", "name": "Pre-algebra · Section 3", "term": "Fall 2026", "sample": True,
          "gradescope": {"connected": True, "course_name": "MATH 095 Sec 3 (sample)", "last_sync": "2026-11-13T16:20:00"},
          "skills": [{"id": s, "name": n} for s, n in SKILLS],
          "students": [{"id": n.lower(), "name": n} for n in NAMES],
          "exams": sorted(exams, key=lambda e: e["date"], reverse=True)}
json.dump(course, open(os.path.join(ROOT, "course.json"), "w"), ensure_ascii=False, indent=1)
print("wrote", ROOT)
