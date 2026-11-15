# Subsigmoid backend

`server.py` serves the site and the `/api/*` routes in `../site/API.md`. It uses only the Python standard library.

When you upload graded CSVs (one file, several, or a whole folder: they are merged into one exam; the format `notebooks/extract_databricks.ipynb` writes; `site/data/sample-graded.csv` is an example):

1. The file is written to the Unity Catalog volume `/Volumes/workspace/pdfdatasets/datasetforpdf/subsigmoid_uploads/`.
2. The assessment job (`notebooks/assessment.ipynb`, job id in `DATABRICKS_JOB_ID`) runs on it and returns its JSON.
3. Llama 3.3 70B on Databricks model serving reads the question text and tags 2–5 skills, plus the skills each question needs.
4. `server.py` turns the job output and the tags into a report. Each skill gets a mastery estimate, and each answer gets a label: knew, guess, slip, gap, or unclear.

## Run locally

Create `subsig/.env`. It is gitignored; never commit it.

```
DATABRICKS_HOST=https://<workspace>.cloud.databricks.com
DATABRICKS_TOKEN=<personal access token>
DATABRICKS_JOB_ID=339930322654689
```

```bash
python3 app/server.py
```

Open http://localhost:8000/app.html. Without a token, the server still runs and serves the sample data; uploads return an error.

## Deploy as a Databricks App

```bash
python3 app/deploy.py
```

The script does four things:

- stores the token in the secret scope `subsigmoid`;
- uploads `server.py`, `app.yaml` and `site/` to your workspace folder;
- creates the app `subsigmoid` if it doesn't exist;
- deploys it and prints the URL.

Before you use it, know these limits:

- Opening the app URL requires a login to the Databricks workspace.
- On Free Edition, apps stop after 24 hours. Run `deploy.py` again to start the app back up.
- State (uploads, reports) is saved to `subsigmoid_uploads/state.json` in the volume, so it survives app restarts.
- The made-up sample class is on the `sample-demo` branch. `main` shows only real uploads.
