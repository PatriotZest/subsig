# Subsigmoid

A grade tells you how many points a student lost, not why. Subsigmoid takes a class's graded exam and works out, for every wrong answer, whether the student was missing a skill or just made a careless mistake. Each student also gets a true score: what they'd have scored with no slips and no lucky guesses.

Built at MadHacks 2026.

## How it works

1. **Extract.** `notebooks/extract_databricks.ipynb` turns graded Gradescope PDFs into CSVs (one row per student per question).
2. **Upload.** Upload one CSV, several, or a whole folder on the dashboard. The files are merged into one exam and saved to a Unity Catalog volume.
3. **IRT.** `notebooks/assessment.ipynb` runs as a Databricks job. It fits a Rasch model: how hard each question is and how strong each student is.
4. **Skills.** Llama 3.3 on Databricks model serving reads each question and tags the skills it needs. A DINA-style model then estimates which skills each student has, from *which* questions they missed.
5. **Report.** Every answer is labeled (knew it, slip, guess, or missing skill), and every student gets a true score, reteach groups and per-question stats.

## Layout

| Path | What |
| --- | --- |
| `site/` | Front page, dashboard, phone quiz, method page (static HTML/JS) |
| `app/` | Backend (`server.py`) and Databricks App deploy script. See `app/README.md` |
| `notebooks/` | PDF extraction and the assessment job |
| `reports/` | Report builder |
| `server.py` | Read-only summary of the latest job run |

## Run it

```bash
python3 app/server.py      # needs .env with DATABRICKS_HOST, DATABRICKS_TOKEN, DATABRICKS_JOB_ID
python3 app/deploy.py      # deploy as a Databricks App
```

Then open http://localhost:8000. `site/data/sample-graded.csv` is an example file to upload (38 made-up students). The `sample-demo` branch has a version of the dashboard filled with a made-up class.
