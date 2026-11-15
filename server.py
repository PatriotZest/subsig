#!/usr/bin/env python3
"""Serve the site and a small, read-only Databricks assessment summary."""
import argparse
import json
import math
import os
from pathlib import Path
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"


class DashboardError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def load_env(path):
    """Environment variables override the optional, local, unserved .env file."""
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                key, sep, value = line.partition("=")
                if sep:
                    os.environ.setdefault(key.strip(), value.strip())


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the bearer token to another host.


def summarize(result, run):
    summary = result.get("summary", {})
    counts = [summary.get(k) for k in ("students", "questions", "responses")]
    if any(type(n) is not int or n < 0 for n in counts) or type(summary.get("model_fitted")) is not bool:
        raise DashboardError("Assessment output has an unexpected summary format.")
    questions = []
    for q in result.get("questions", []):
        fraction = q.get("mean_fraction")
        if type(fraction) not in (int, float) or not math.isfinite(fraction) or not 0 <= fraction <= 1:
            continue
        questions.append({"id": str(q.get("question_id", ""))[:80],
                          "text": str(q.get("question_text", ""))[:240],
                          "average": fraction})
    questions.sort(key=lambda q: q["average"])
    # Deliberately allowlist fields: no student names, responses, embeddings or token.
    return {"configured": True, "name": str(run.get("run_name") or "Assessment")[:120],
            "completed_at": datetime.fromtimestamp(run["end_time"] / 1000, timezone.utc).isoformat(),
            "summary": dict(zip(("students", "questions", "responses"), counts),
                            model_fitted=summary["model_fitted"], synthetic=summary.get("synthetic") is True),
            "model_note": next((w for w in result.get("warnings", [])
                                if isinstance(w, str) and w.startswith("Model skipped:")), None),
            "questions": questions[:3]}


class Databricks:
    def __init__(self, env):
        self.host = env.get("DATABRICKS_HOST", "").rstrip("/").removesuffix("/api/2.0")
        self.token = env.get("DATABRICKS_TOKEN", "")
        self.job_id = env.get("DATABRICKS_JOB_ID", "")
        self.task_key = env.get("DATABRICKS_TASK_KEY", "")
        self.configured = any((self.host, self.token, self.job_id))
        self.opener = build_opener(NoRedirects())
        self.lock = threading.Lock()
        self.cached = None
        self.cached_at = 0

    def get(self, path, **params):
        request = Request(self.host + "/api/2.1/jobs/" + path + "?" + urlencode(params),
                          headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise DashboardError("Databricks access was denied. Check the server credentials and job permissions.") from None
            raise DashboardError("Databricks could not return the assessment. Try again shortly.") from None
        except (URLError, TimeoutError, OSError):
            raise DashboardError("Databricks is unavailable. Try again shortly.") from None
        except (ValueError, TypeError):
            raise DashboardError("Databricks returned an invalid response.") from None

    def dashboard(self):
        if not self.configured:
            return {"configured": False}
        host = urlsplit(self.host)
        if not (host.scheme == "https" and host.netloc and not host.username and not host.password
                and host.path in ("", "/") and not host.query and not host.fragment
                and self.token and self.job_id.isdecimal()):
            raise DashboardError("Set DATABRICKS_HOST, DATABRICKS_TOKEN and DATABRICKS_JOB_ID on the server.", 503)
        with self.lock:
            if self.cached is not None and time.monotonic() - self.cached_at < 30:
                return self.cached
            runs = self.get("runs/list", job_id=self.job_id, completed_only="true", limit=25).get("runs", [])
            run = next((r for r in runs if r.get("state", {}).get("result_state") == "SUCCESS"), None)
            if run is None:
                raise DashboardError("No successful assessment run found. Run the assessment job with return_json=true.", 503)
            details = self.get("runs/get", run_id=run["run_id"])
            tasks = details.get("tasks", [])
            if tasks:
                candidates = [t for t in tasks if t.get("task_key") == self.task_key] if self.task_key else tasks
                if len(candidates) != 1:
                    raise DashboardError("Set DATABRICKS_TASK_KEY to the assessment notebook task on the server.", 503)
                task_id = candidates[0]["run_id"]
            else:
                task_id = run["run_id"]
            output = self.get("runs/get-output", run_id=task_id).get("notebook_output", {})
            if output.get("truncated") or not output.get("result"):
                raise DashboardError("Assessment output is missing or truncated. Run the notebook with return_json=true.")
            try:
                result = json.loads(output["result"])
                self.cached = summarize(result, run)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                raise DashboardError("Assessment output has an unexpected format.") from None
            self.cached_at = time.monotonic()
            return self.cached


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE), **kwargs)

    def do_GET(self):
        if urlsplit(self.path).path == "/api/dashboard":
            try:
                self.json_response(200, self.server.databricks.dashboard())
            except DashboardError as exc:
                self.json_response(exc.status, {"error": str(exc)})
            return
        super().do_GET()

    def json_response(self, status, payload):
        body = json.dumps(payload, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_head(self):
        # Only serve public site files, including for HEAD and encoded traversal attempts.
        target = Path(self.translate_path(self.path)).resolve()
        if not target.is_relative_to(SITE.resolve()) or any(p.startswith(".") for p in target.relative_to(SITE).parts):
            self.send_error(404)
            return None
        if target.is_dir() and not (target / "index.html").is_file():
            self.send_error(404)
            return None
        return super().send_head()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    load_env(args.env_file)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.databricks = Databricks(os.environ)
    print(f"Dashboard: http://{args.host}:{args.port}/app.html", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
