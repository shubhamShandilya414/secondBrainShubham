"""Local worker that runs Meera jobs created by the Streamlit frontend.

The Streamlit app can save JSON job specs into a queue directory. This worker
watches that directory, claims jobs, and launches `meeting_agent.py` on the
local machine where browser automation and microphone access are available.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import argparse
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from env_utils import load_env_file

try:
    from flask import Flask, jsonify, request
except ModuleNotFoundError:
    Flask = None
    jsonify = None
    request = None


ROOT = Path(__file__).resolve().parent
AGENT_SCRIPT = ROOT / "meeting_agent.py"
QUEUE_DIR = ROOT / "streamlit_jobs" / "inbox"
PROCESSING_DIR = ROOT / "streamlit_jobs" / "processing"
COMPLETED_DIR = ROOT / "streamlit_jobs" / "completed"
FAILED_DIR = ROOT / "streamlit_jobs" / "failed"

load_env_file(ROOT / ".env")

app = Flask(__name__) if Flask is not None else None


def _ensure_dirs() -> None:
    for path in (QUEUE_DIR, PROCESSING_DIR, COMPLETED_DIR, FAILED_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _parse_iso_datetime(value: str) -> dt.datetime:
    value = value.strip()
    if not value:
        raise ValueError("Missing join_time")
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo(os.environ.get("MEERA_TIMEZONE", "Asia/Kolkata")))
    return parsed


def _load_job(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Job file must contain a JSON object")
    return payload


def _move(path: Path, target_dir: Path) -> Path:
    target = target_dir / path.name
    return Path(shutil.move(str(path), str(target)))


def _job_path(job_id: str, directory: Path) -> Path:
    return directory / f"{job_id}.json"


def _build_context_config_from_env():
    from work_context import WorkContextConfig

    github_repos = [repo.strip() for repo in os.environ.get("GITHUB_REPOS", "").split(",") if repo.strip()]
    return WorkContextConfig(
        github_repos=github_repos,
        jira_base_url=os.environ.get("JIRA_BASE_URL", "").strip(),
        jira_project_key=os.environ.get("JIRA_PROJECT_KEY", "").strip(),
        jira_jql=os.environ.get("JIRA_JQL", "").strip(),
        github_token=os.environ.get("GITHUB_TOKEN", "").strip(),
        jira_email=os.environ.get("JIRA_EMAIL", "").strip(),
        jira_api_token=os.environ.get("JIRA_API_TOKEN", "").strip(),
    )


def _write_context_brief(job_id: str, persona: str = "", call_type: str = "") -> str:
    from work_context import build_work_context_brief

    context_dir = ROOT / "context_briefs"
    config = _build_context_config_from_env()
    config = config.__class__(
        github_repos=config.github_repos,
        jira_base_url=config.jira_base_url,
        jira_project_key=config.jira_project_key,
        jira_jql=config.jira_jql,
        github_token=config.github_token,
        jira_email=config.jira_email,
        jira_api_token=config.jira_api_token,
        persona=persona,
        call_type=call_type,
        max_prs_per_repo=config.max_prs_per_repo,
        max_comments_per_item=config.max_comments_per_item,
        max_jira_issues=config.max_jira_issues,
    )
    brief_path = build_work_context_brief(config, context_dir)
    print(f"[Worker] Job {job_id}: wrote context brief to {brief_path}")
    return str(brief_path)


def _is_bug_fix_context(job: dict[str, Any]) -> bool:
    persona = str(job.get("persona", "")).strip().lower()
    call_type = str(job.get("call_type", "")).strip().upper()
    return persona == "software developer" and call_type == "BUG FIX CALL"


def _submit_job(payload: dict[str, Any]) -> Path:
    _ensure_dirs()
    job_id = str(payload.get("id") or "").strip() or f"job_{int(time.time())}"
    payload["id"] = job_id
    path = _job_path(job_id, QUEUE_DIR)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[Worker] Job {job_id}: queued at {path}")
    return path


def _launch_meera(job: dict[str, Any], brief_path: str = "") -> None:
    if not AGENT_SCRIPT.exists():
        raise FileNotFoundError(f"Missing agent script: {AGENT_SCRIPT}")

    kind = str(job.get("kind", "meeting")).strip()
    display_name = str(job.get("display_name", "Meera")).strip() or "Meera"
    persona = str(job.get("persona", "Software Developer")).strip() or "Software Developer"
    call_type = str(job.get("call_type", "BUG FIX CALL")).strip() or "BUG FIX CALL"
    voice_mode = bool(job.get("voice_mode", False))
    voice_device_index = job.get("voice_device_index")
    context_brief_path = str(job.get("context_brief_path", "")).strip() or brief_path

    cmd = [
        sys.executable,
        "-u",
        str(AGENT_SCRIPT),
        "--display-name",
        display_name,
        "--persona",
        persona,
        "--call-type",
        call_type,
    ]
    if kind == "local_conversation":
        cmd.append("--local-conversation")
    else:
        meeting_link = str(job.get("meeting_link", "")).strip()
        site = str(job.get("site", "teams")).strip() or "teams"
        if not meeting_link:
            raise ValueError("Missing meeting_link")
        cmd.extend(["--site", site, "--meeting-id", meeting_link])
        passcode = str(job.get("passcode", "")).strip()
        if passcode:
            cmd.extend(["--passcode", passcode])

    if voice_mode:
        cmd.append("--voice-mode")
    if voice_device_index not in {None, ""}:
        cmd.extend(["--voice-device-index", str(voice_device_index)])
    if context_brief_path:
        cmd.extend(["--context-brief-path", context_brief_path])

    print(f"[Worker] Launching Meera: {' '.join(cmd)}")
    subprocess.Popen(cmd, cwd=str(ROOT), env=os.environ.copy())


def _run_job_file(path: Path) -> None:
    try:
        claimed = _move(path, PROCESSING_DIR)
    except Exception as exc:
        print(f"[Worker] Could not claim {path.name}: {exc}")
        return

    try:
        job = _load_job(claimed)
        job_id = str(job.get("id") or claimed.stem)
        kind = str(job.get("kind", "meeting")).strip()
        print(f"[Worker] Processing job {job_id} ({kind})")

        if bool(job.get("context_mode", False)) and _is_bug_fix_context(job):
            lead_minutes = int(job.get("context_lead_minutes", 15) or 15)
            join_time = _parse_iso_datetime(str(job.get("join_time", "")))
            context_time = join_time - dt.timedelta(minutes=max(1, lead_minutes))
            now = dt.datetime.now(join_time.tzinfo or ZoneInfo(os.environ.get("MEERA_TIMEZONE", "Asia/Kolkata")))
            if context_time > now:
                wait_seconds = (context_time - now).total_seconds()
                print(f"[Worker] Job {job_id}: waiting {wait_seconds:.0f}s for context prep")
                time.sleep(wait_seconds)
            brief_path = _write_context_brief(
                job_id,
                str(job.get("persona", "")).strip(),
                str(job.get("call_type", "")).strip(),
            )
            job["context_brief_path"] = brief_path
            claimed.write_text(json.dumps(job, indent=2), encoding="utf-8")
        elif bool(job.get("context_mode", False)):
            print(f"[Worker] Job {job_id}: skipping context load because persona is not configured for it.")

        join_time = job.get("join_time")
        if join_time:
            scheduled_time = _parse_iso_datetime(str(join_time))
            now = dt.datetime.now(scheduled_time.tzinfo or ZoneInfo(os.environ.get("MEERA_TIMEZONE", "Asia/Kolkata")))
            wait_seconds = (scheduled_time - now).total_seconds()
            if wait_seconds > 0:
                print(f"[Worker] Job {job_id}: waiting {wait_seconds:.0f}s for join time")
                time.sleep(wait_seconds)

        _launch_meera(job, str(job.get("context_brief_path", "")).strip())
        shutil.move(str(claimed), str((COMPLETED_DIR / claimed.name)))
        print(f"[Worker] Job {job_id}: launched successfully")
    except Exception as exc:
        try:
            shutil.move(str(claimed), str((FAILED_DIR / claimed.name)))
        except Exception:
            pass
        print(f"[Worker] Job failed: {exc}")


def _job_status(job_id: str) -> dict[str, str]:
    for directory, status in (
        (QUEUE_DIR, "queued"),
        (PROCESSING_DIR, "processing"),
        (COMPLETED_DIR, "completed"),
        (FAILED_DIR, "failed"),
    ):
        candidate = _job_path(job_id, directory)
        if candidate.exists():
            return {"id": job_id, "status": status, "path": str(candidate)}
    return {"id": job_id, "status": "missing"}


def _serve_api(host: str, port: int) -> None:
    if app is None:
        raise ModuleNotFoundError(
            "Flask is not installed in this Python environment. "
            "Run: python -m pip install -r requirements.txt"
        )

    @app.get("/health")
    def health():  # type: ignore[no-redef]
        return jsonify({"ok": True, "service": "meera-worker"})

    @app.post("/jobs")
    def jobs():  # type: ignore[no-redef]
        payload = request.get_json(force=True, silent=False)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "Expected a JSON object"}), 400
        try:
            path = _submit_job(payload)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "job_id": payload["id"], "path": str(path)}), 202

    @app.get("/jobs/<job_id>")
    def job_status(job_id: str):  # type: ignore[no-redef]
        return jsonify(_job_status(job_id))

    print(f"[Worker] HTTP API listening on http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local Meera worker.")
    parser.add_argument("--serve", action="store_true", help="Expose an HTTP API for remote job submission")
    parser.add_argument("--host", default=os.environ.get("MEERA_WORKER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MEERA_WORKER_PORT", "8765")))
    args = parser.parse_args()

    _ensure_dirs()
    print(f"[Worker] Watching {QUEUE_DIR}")
    watcher = threading.Thread(target=_watch_queue, daemon=True)
    watcher.start()
    if args.serve:
        _serve_api(args.host, args.port)
        return 0
    while True:
        time.sleep(1)


def _watch_queue() -> None:
    while True:
        for path in sorted(QUEUE_DIR.glob("*.json")):
            thread = threading.Thread(target=_run_job_file, args=(path,), daemon=True)
            thread.start()
            time.sleep(0.2)
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
