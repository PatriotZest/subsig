"""Deploy Subsigmoid as a Databricks App.

    python3 app/deploy.py

Reads DATABRICKS_HOST / DATABRICKS_TOKEN from .env. Steps:
  1. stores the token in a secret scope (the app reads it through app.yaml, never from the code)
  2. uploads app/server.py, app/app.yaml and site/ to the workspace
  3. creates the app if needed (with the secret as a resource) and deploys it
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for line in (ROOT / ".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
HOST, TOKEN = os.environ["DATABRICKS_HOST"].rstrip("/"), os.environ["DATABRICKS_TOKEN"]
APP = os.environ.get("SUBSIGMOID_APP_NAME", "subsigmoid")
SCOPE, KEY = "subsigmoid", "token"


def api(method, path, body=None, ok404=False):
    req = urllib.request.Request(HOST + path, method=method, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            t = r.read().decode()
            return json.loads(t) if t.strip() else {}
    except urllib.error.HTTPError as e:
        msg = e.read().decode()
        if ok404 and (e.code in (404, 400) or "RESOURCE_ALREADY_EXISTS" in msg or "ALREADY_EXISTS" in msg):
            return {"_error": msg}
        raise SystemExit(f"{method} {path} -> {e.code} {msg[:400]}")


me = api("GET", "/api/2.0/preview/scim/v2/Me")["userName"]
DEST = f"/Workspace/Users/{me}/subsigmoid-app"
print("workspace user:", me)

# 1. secret
api("POST", "/api/2.0/secrets/scopes/create", {"scope": SCOPE}, ok404=True)
api("POST", "/api/2.0/secrets/put", {"scope": SCOPE, "key": KEY, "string_value": TOKEN})
print("secret stored in scope", SCOPE)

# 2. upload source
files = [(ROOT / "app" / "server.py", "server.py"), (ROOT / "app" / "app.yaml", "app.yaml")]
files += [(p, "site/" + str(p.relative_to(ROOT / "site"))) for p in sorted((ROOT / "site").rglob("*")) if p.is_file() and p.name != ".DS_Store"]
dirs = sorted({str(Path(DEST, rel).parent) for _, rel in files})
for d in dirs:
    api("POST", "/api/2.0/workspace/mkdirs", {"path": d})
for src, rel in files:
    api("POST", "/api/2.0/workspace/import", {"path": f"{DEST}/{rel}", "format": "AUTO", "overwrite": True,
                                               "content": base64.b64encode(src.read_bytes()).decode()})
print(f"uploaded {len(files)} files to {DEST}")

# 3. app
app = api("GET", f"/api/2.0/apps/{APP}", ok404=True)
if "_error" in app:
    print("creating app", APP)
    api("POST", "/api/2.0/apps", {"name": APP, "description": "Subsigmoid: skill reports from graded exams",
                                   "resources": [{"name": "databricks-token", "secret": {"scope": SCOPE, "key": KEY, "permission": "READ"}}]})
for _ in range(60):
    app = api("GET", f"/api/2.0/apps/{APP}")
    state = app.get("compute_status", {}).get("state")
    if state == "ACTIVE":
        break
    if state in ("STOPPED",):
        api("POST", f"/api/2.0/apps/{APP}/start", {})
    print("  compute:", state); time.sleep(10)
dep = api("POST", f"/api/2.0/apps/{APP}/deployments", {"source_code_path": DEST, "mode": "SNAPSHOT"})
print("deployment", dep.get("deployment_id"), dep.get("status", {}).get("state"))
for _ in range(60):
    d = api("GET", f"/api/2.0/apps/{APP}/deployments/{dep['deployment_id']}")
    st = d.get("status", {}).get("state")
    if st in ("SUCCEEDED", "FAILED", "CANCELLED"):
        print("deployment", st, d.get("status", {}).get("message", ""))
        break
    time.sleep(5)
print("app url:", api("GET", f"/api/2.0/apps/{APP}").get("url"))
