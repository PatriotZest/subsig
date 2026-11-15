# Subsigmoid API contract

## Connected assessment dashboard

Run `python3 server.py` from the repository root. Its same-origin
`GET /api/dashboard` reads the configured Databricks assessment job's latest
successful run, resolves the notebook task, retrieves its `return_json=true`
output, and returns only:

```ts
type Dashboard = {
  configured: true; name: string; completed_at: string;
  summary: { students: number; questions: number; responses: number;
             model_fitted: boolean; synthetic: boolean };
  model_note: string | null;
  questions: { id: string; text: string; average: number }[]; // up to 3, lowest average first
};
```

`average` is a fraction from 0 to 1. Questions without a valid normalized average
are omitted. Missing/truncated output and access failures return non-2xx
`{ error: string }` responses without upstream error bodies. No configured
credentials returns `{ configured: false }` so the static demo remains available.
The response is not stored in browser caches; the server caches it for 30 seconds.
Credentials belong in server environment variables or the ignored root `.env`,
never in HTML, JavaScript, query strings or requests from the browser.

## Full course backend (separate demo contract)

The front end is static HTML/JS:

- `index.html`: the front page
- `app.html`: the teacher dashboard (hash routes: `#/` course, `#/exams/{id}` exam or its tag review, `#/students/{id}`, `#/live`, `#/import`)
- `quiz.html`: the student quiz that judges take on their phones
- `method.html`: how the statistics work

Every call to the backend goes through `js/api.js`. With no backend configured, `api.js` answers
from `data/` (course, reports, quiz) plus a simulated live session. To point at the backend, set this
in each page's `<head>`, or open a page with `?api=<url>`:

```html
<meta name="subsigmoid-api" content="https://<your-app>.databricksapps.com">
```

JSON in and out unless noted. Errors are non-2xx.

## Courses and Gradescope

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/api/courses` | `{ id, name, term }[]` |
| GET | `/api/courses/{cid}` | `Course` |
| POST | `/api/courses/{cid}/sync` | `{ last_sync, new_exams }`: pull new exams from Gradescope |
| POST | `/api/courses/{cid}/uploads` | multipart `file` (Gradescope CSV) → `{ exam: Exam }` with `status: "needs_tags"` |

Gradescope has no public API that we know of, so `sync` is the hook for a future integration
(a browser extension or a partnership). For the hackathon, exams arrive by CSV upload.

```ts
type Course = { id: string; name: string; term: string;
  gradescope: { connected: boolean; course_name: string; last_sync: string };
  skills: Skill[]; students: { id: string; name: string }[]; exams: Exam[] };
type Skill = { id: string; name: string };
type Exam = { id: string; name: string; date: string; source: string; n_students: number; n_items: number;
  status: 'needs_tags' | 'analyzing' | 'analyzed'; report_id: string | null };
```

## Tagging and analysis

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| GET | `/api/exams/{eid}/tags` | | `Tags`: the AI-proposed Q-matrix |
| PUT | `/api/exams/{eid}/tags` | `Tags` | `{ ok: true }` |
| POST | `/api/exams/{eid}/analyze` | | `{ report_id }` once IRT + DINA have run |
| GET | `/api/reports/{rid}` | | `Report` |

```ts
type Tags = { exam_id: string; skills: Skill[];
  items: { id: string; prompt: string; skills: string[]; source: 'ai' | 'edited' | 'approved' }[] };

type Label = 'knew' | 'guess' | 'slip' | 'gap' | 'unclear';
type Report = { id: string;
  test: { name: string; date: string; course: string; source: string; n_students: number; n_items: number };
  skills: Skill[];
  items: { id: string; prompt: string; skills: string[]; max_points: number; percent_correct: number;
           difficulty: number; discrimination: number; guess: number; slip: number; flags: string[] }[];
  students: { id: string; name: string; score: number; max_score: number;
              mastery: Record<string, { p: number; lo: number; hi: number }>;
              answers: { item: string; points: number; max_points: number; rubric: string; label: Label; p: number }[];
              score_note: 'understated' | 'overstated' | null }[];
  groups: { skill: string; students: string[] }[];          // reteach groups
  mistakes: { skill: string; gaps: number; slips: number }[];
  warnings: { item?: string; skill?: string; kind: string; text: string }[] };
```

## Students

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/api/courses/{cid}/students/{sid}` | `{ student, skills, exams: { exam_id, name, date, score, max_score, mastery, score_note, answers }[] }` (oldest exam first; each answer also carries `prompt`) |

## Live session (judging demo)

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/api/live` | `Live`, polled every 2 s by the dashboard |

```ts
type Live = { simulated?: boolean; session_id: string; quiz_url: string; participants: number; finished: number;
  predictions: { made: number; hits: number };
  recent: { participant: string; item: string; prompt: string; done: number; predicted: boolean; hit: boolean }[];
  evaluation: { id: string; name: string; uses: string; accuracy: number | null }[] };
```

`evaluation` is the hide-one-answer test: raw score, skill counting, IRT alone, Subsigmoid.

## Quiz (student side)

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| GET | `/api/quiz` | | `Quiz` without answer keys |
| POST | `/api/quiz/sessions` | | `{ session_id, participants }` |
| POST | `/api/quiz/sessions/{id}/answers` | `{ item, option }` | `{ ok: true }` |
| GET | `/api/quiz/sessions/{id}/prediction?item=Q7` | | `{ item, option, correct, mistake, mistake_text, p }` |
| GET | `/api/quiz/sessions/{id}/result` | | `{ participants, mastery: {skill,name,p}[], answers: {item,option,right,mistake,skills,label,p}[], mistakes }` |

The prediction must be stored, with a timestamp, before the answer to that item arrives.

## Sample data

`../tools/make_sample_data.py` writes `data/course.json` and `data/reports/*.json`: one made-up class,
38 students, three analyzed quizzes and a midterm that still needs tags. `data/quiz.json` is the room
quiz with answer keys, used only by the mock.
