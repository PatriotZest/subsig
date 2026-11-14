#!/usr/bin/env python3
"""Extract Gradescope scores and best-effort scanned question prompts.

Requirements: Python 3.10+, PyMuPDF, and the Tesseract executable for OCR.
Usage: python extract.py green.pdf -o graded_summary.csv
       python extract.py *.pdf --no-ocr -o grades.csv
       python extract.py green.pdf --question-overrides corrections.json

Optional corrections JSON maps question IDs to verified prompt text, e.g.
{"1": "Verified question one", "2.1": "Verified subquestion"}.
Overrides apply to one input PDF only. A <PDF stem>.questions.json file
beside the PDF is loaded automatically if its source_sha256 matches.
A complete correction set bypasses OCR; partial sets correct only those IDs. Rubric titles are copied verbatim;
they are not a verified answer key. OCR excerpts omit diagrams and tables.
"""
import argparse
import hashlib
import csv
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pymupdf

CHECK = "\uf00c"
PRIVATE_USE = re.compile(r"[\ue000-\uf8ff]")
SCORE_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*pts?$")
ITEM_RE = re.compile(r"^([\uf00c✓✔])?\s*([+−–-])\s*(\d+(?:\.\d+)?)\s*pts?\b\s*(.*)$")
QHEAD_RE = re.compile(r"^Question\s+(\d+)$")
SUB_RE = re.compile(r"^(\d+(?:\.\d+)+)$")

# --------------------------------------------------------------------------- #
# 2. Parse the Gradescope rubric text
# --------------------------------------------------------------------------- #
def parse_header(text):
    name = re.search(r"Student\s*\n\s*(.+)", text)
    total = re.search(r"Total Points\s*\n\s*([\d.]+)\s*/\s*([\d.]+)", text)
    return (
        name.group(1).strip() if name else "(unknown)",
        float(total.group(1)) if total else None,
    )


def parse_rubric(text):
    """Return a list of entries: {id, title, earned, possible, items:[{pts,text,applied}]}."""
    # "...partial credit.Question 5" can arrive glued together; split them apart
    text = re.sub(r"(?<=\S)(Question\s+\d+)", r"\n\1", text)

    entries, cur, last_item = [], None, None
    pending, title_lines = None, []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        m = QHEAD_RE.match(line)
        if m:
            pending, title_lines, last_item = m.group(1), [], None
            continue
        m = SUB_RE.match(line)
        if m:
            pending, title_lines, last_item = m.group(1), [], None
            continue

        if pending is not None:                      # collecting title until the score line
            m = SCORE_RE.match(line)
            if m:
                cur = {
                    "id": pending,
                    "title": " ".join(title_lines).strip(),
                    "earned": float(m.group(1)),
                    "possible": float(m.group(2)),
                    "items": [],
                }
                entries.append(cur)
                pending = None
            else:
                title_lines.append(PRIVATE_USE.sub("", line).strip())
            continue

        if cur is None:
            continue
        m = ITEM_RE.match(line)
        if m:
            last_item = {
                "applied": bool(m.group(1)),
                "pts": float(m.group(3)) * (-1 if m.group(2) in "−–-" else 1),
                "text": PRIVATE_USE.sub("", m.group(4)).strip(),
            }
            cur["items"].append(last_item)
        elif last_item is not None:                  # wrapped description line
            last_item["text"] += " " + PRIVATE_USE.sub("", line).strip()

    return entries


def leaf_entries(entries):
    """Entries that are graded directly (skip parents that only hold sub-questions)."""
    ids = [e["id"] for e in entries]
    return [e for e in entries if not any(i.startswith(e["id"] + ".") for i in ids)]


def result_text(entry):
    applied = [it for it in entry["items"] if it["applied"]]
    if not applied:
        return "(no rubric item marked)"
    parts = []
    for it in applied:
        if it["text"].strip().lower().rstrip(".") == "correct":
            parts.append("Correct")
        else:
            parts.append(f"{it['text']} ({it['pts']:+g} pts)")
    return " | ".join(parts)


# OCR retains page boundaries and word confidence. Never shift later questions
# to fill a missing number: that was the cause of the Q10/Q11 mismatch.
NUMBER_RE = re.compile(r"^\s*([\\|IlO\d]{1,2})\s*[.,)]\s*(.*)$")
DIGITS = str.maketrans({"\\": "1", "|": "1", "I": "1", "l": "1", "O": "0"})
STOP_RE = re.compile(r"This question is just a reminder|THIS EXAM IS DONE|USE THIS PAGE FOR SCRATCH", re.I)
NOISE_RE = re.compile(r"MY EXAM CODE|Page \d+ of \d+|Print your|Show your work|^\W*CS\W?\d{3}", re.I)
PART_RE = re.compile(r"^([a-z])[.,)]\s+", re.I)
PROMPT_RE = re.compile(r"\b(?:What|Given|Enter|Complete|For the|An instruction|and copied)\b", re.I)


def ocr_lines(page, dpi):
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    proc = subprocess.run(
        ["tesseract", "stdin", "stdout", "--psm", "3", "tsv"],
        input=pix.tobytes("png"), capture_output=True, timeout=120,
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode(errors="replace").strip())
    groups = {}
    for word in csv.DictReader(io.StringIO(proc.stdout.decode()), delimiter="\t", quoting=csv.QUOTE_NONE):
        if word["level"] != "5" or not word["text"].strip():
            continue
        key = tuple(word[k] for k in ("block_num", "par_num", "line_num"))
        groups.setdefault(key, []).append(word)
    result = []
    for words in groups.values():
        result.append({
            "text": " ".join(w["text"] for w in words),
            "confidence": sum(float(w["conf"]) for w in words) / len(words),
            "x": min(int(w["left"]) for w in words) / pix.width,
            "y": min(int(w["top"]) for w in words) / pix.height,
        })
    return sorted(result, key=lambda line: (line["y"], line["x"]))


def read_pdf(pdf_path, do_ocr=True, dpi=300):
    typed, pages = [], []
    with pymupdf.open(pdf_path) as doc:
        for number, page in enumerate(doc, 1):
            text = page.get_text().strip()
            # Classify each page independently, including digital question pages.
            if re.search(r"(?m)^Question\s+\d+\s*$", text) and re.search(r"\d\s*/\s*\d.*pts?", text):
                typed.append(text)
            elif "Total Points" in text and "Student" in text:
                typed.append(text)
            elif len(text) >= 50:
                pages.append((number, [{"text": t, "confidence": 100, "x": 0, "y": 0.5}
                                       for t in text.splitlines() if t.strip()]))
            elif do_ocr:
                print(f"  OCR page {number}/{len(doc)}", file=sys.stderr)
                pages.append((number, ocr_lines(page, dpi)))
    return "\n".join(typed), pages


def clean_prompt(lines):
    """Keep a short prose excerpt, never a dump of handwriting/table contents."""
    kept = []
    active = False
    for line in lines:
        t = line["text"].strip()
        if STOP_RE.search(t):
            break
        if NOISE_RE.search(t) or line["y"] < 0.055 or line["y"] > 0.91:
            active = False
            continue
        t = re.sub(r"\s*\(\s*\d+(?:\.\d+)?\s*\)\s*", " ", t).strip()
        start = bool(PROMPT_RE.search(t) or PART_RE.match(t) or t.startswith(("Recall ", "Note:", "A-")))
        # Low-confidence numeric/code fragments are not recoverable prose.
        prose = len(re.findall(r"[A-Za-z]{2,}", t)) >= 3
        if start or (active and prose and line["confidence"] >= 55):
            if "?" in t:
                t = t[:t.index("?") + 1]
            kept.append(t)
            active = not t.endswith(("?", "."))
        else:
            active = False
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


def split_questions(pages, entries):
    parents = {int(e["id"]): e for e in entries if "." not in e["id"]}
    candidates = {}
    last_number = 0
    started = False
    for page_number, lines in pages:
        starts = []
        for idx, line in enumerate(lines):
            t = line["text"]
            if STOP_RE.search(t):
                starts.append((idx, None, "stop"))
                break
            if "Part I" in t:
                started = True
            m = NUMBER_RE.match(t)
            number = None
            method = "number"
            body = t
            if m and line["x"] < 0.2:
                token = m.group(1).translate(DIGITS)
                n = int(token)
                if n in parents and n > last_number and PROMPT_RE.search(m.group(2)):
                    number, body = n, m.group(2)
                    if n == 1:
                        started = True
            # A unique multiword rubric title can recover an obscured number,
            # but never an arbitrary next sentence or the exam reminder.
            if number is None and started and line["x"] < 0.25 and PROMPT_RE.search(t):
                matches = []
                for n, entry in parents.items():
                    title = re.sub(r"\([^)]*\)", "", entry["title"]).strip().lower()
                    if len(title.split()) >= 2 and title in t.lower() and n > last_number:
                        matches.append(n)
                if len(matches) == 1:
                    number, method = matches[0], "rubric title; verify number"
                    prompt = PROMPT_RE.search(t)
                    body = t[prompt.start():]
            if started and number is not None and number > last_number:
                starts.append((idx, number, method))
                lines[idx] = dict(line, text=body)
                last_number = number
        # A question ends at the next heading OR this page's end. Missing
        # headings cannot absorb the following page's diagrams or questions.
        for j, (idx, number, method) in enumerate(starts):
            if number is None:
                continue
            end = starts[j + 1][0] if j + 1 < len(starts) else len(lines)
            chunk = lines[idx:end]
            candidates[number] = {"text": clean_prompt(chunk), "page": page_number,
                                  "method": method, "lines": chunk}
    return candidates


def subquestion_text(question, entry_id):
    if "." not in entry_id:
        return question["text"]
    part = int(entry_id.split(".")[1])
    lines = question["lines"]
    markers = [(i, ord(m.group(1).lower()) - ord("a") + 1)
               for i, line in enumerate(lines) if (m := PART_RE.match(line["text"]))]
    selected = [i for i, n in markers if n == part]
    if len(selected) != 1:
        return question["text"]
    start = selected[0]
    end = next((i for i, _ in markers if i > start), len(lines))
    context = clean_prompt(lines[:markers[0][0]])
    excerpt = clean_prompt(lines[start:end])
    return " ".join(s for s in (context, excerpt) if s)


FIELDS = ["Student", "Question", "Question text", "Rubric answer/title (verbatim)",
          "Result", "Points earned", "Points possible", "Points lost", "Score (%)"]


def process_pdf(pdf_path, do_ocr=True, dpi=300, overrides=None):
    overrides = overrides or {}
    typed, pages = read_pdf(pdf_path, False, dpi)
    student, total = parse_header(typed)
    entries = parse_rubric(typed)
    leaves = leaf_entries(entries)
    if not leaves:
        raise ValueError("No Gradescope rubric entries found; refusing to write an empty report")
    if len({e["id"] for e in leaves}) != len(leaves):
        raise ValueError("Duplicate rubric question IDs; inspect the source PDF")
    earned = sum(e["earned"] for e in leaves)
    if total is not None and abs(earned - total) > 1e-6:
        raise ValueError(f"Parsed points {earned:g} do not match PDF total {total:g}")
    missing = [e for e in leaves if e["id"] not in overrides and e["id"].split(".")[0] not in overrides]
    if missing and do_ocr:
        if not shutil.which("tesseract"):
            raise ValueError("Install Tesseract or use --no-ocr; no complete question corrections found")
        _, pages = read_pdf(pdf_path, True, dpi)
    questions = split_questions(pages, entries)
    if missing:
        print(f"  {len(missing)} prompts use unverified extraction; inspect binary values and diagrams.", file=sys.stderr)
    rows = []
    for e in leaves:
        qnum = int(e["id"].split(".")[0])
        question = questions.get(qnum)
        text = subquestion_text(question, e["id"]) if question else ""
        if e["id"] in overrides or str(qnum) in overrides:
            text = overrides.get(e["id"], overrides.get(str(qnum)))
        rows.append([student, f"Q{e['id']}", text, e["title"], result_text(e),
                     e["earned"], e["possible"], e["possible"] - e["earned"],
                     round(100 * e["earned"] / e["possible"], 2) if e["possible"] else ""])
    return rows


def load_corrections(path, pdf):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Corrections must be a JSON object")
    if "questions" in data:
        expected = data.get("source_sha256")
        if not expected or hashlib.sha256(Path(pdf).read_bytes()).hexdigest() != expected:
            raise ValueError("Corrections belong to a different PDF; refusing to apply them")
        data = data["questions"]
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and re.fullmatch(r"\d+(?:\.\d+)*", k)
        and isinstance(v, str) and v.strip() for k, v in data.items()
    ):
        raise ValueError("Corrections must map question IDs to nonempty strings")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdfs", nargs="*", type=Path, help="PDF paths; defaults to Desktop/*.pdf")
    parser.add_argument("-o", "--output", type=Path, help="Output CSV (defaults to graded_summary.csv beside first PDF)")
    parser.add_argument("--no-ocr", action="store_true", help="Extract grades without scanning images")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--question-overrides", type=Path, help="JSON of verified question transcriptions")
    args = parser.parse_args(argv)
    pdfs = [p.expanduser() for p in args.pdfs] or sorted((Path.home() / "Desktop").glob("*.pdf"))
    if not pdfs:
        parser.error("No PDFs found. Supply a PDF path.")
    if args.dpi < 72 or args.dpi > 600:
        parser.error("--dpi must be between 72 and 600")
    if args.question_overrides and len(pdfs) != 1:
        parser.error("Question overrides require exactly one input PDF")
    rows = []
    # Fail without replacing the existing CSV if any input cannot be parsed.
    try:
        for pdf in pdfs:
            corrections = args.question_overrides or pdf.with_suffix(".questions.json")
            overrides = load_corrections(corrections, pdf) if corrections.exists() else {}
            if args.question_overrides and not corrections.exists():
                raise ValueError(f"Corrections file not found: {corrections}")
            if overrides:
                print(f"  Loaded {len(overrides)} question corrections from {corrections.name}", file=sys.stderr)
            part = process_pdf(pdf, not args.no_ocr, args.dpi, overrides)
            rows.extend(part)
            print(f"{pdf.name}: {len(part)} graded rows", file=sys.stderr)
    except Exception as exc:
        print(f"Error: {exc}. Output not replaced.", file=sys.stderr)
        return 1
    out = (args.output or pdfs[0].parent / "graded_summary.csv").expanduser()
    if out.resolve() in {p.resolve() for p in pdfs}:
        parser.error("Output must not overwrite an input PDF")
    out.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", newline="", encoding="utf-8-sig",
                                         dir=out.parent, delete=False) as f:
            tmp = Path(f.name)
            writer = csv.writer(f)
            writer.writerow(FIELDS)
            writer.writerows(rows)
        tmp.replace(out)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
