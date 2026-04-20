"""Local web UI for scheduling meeting joins in the background.

Run:
    python scheduler_web.py

Then open http://127.0.0.1:5000 in your browser.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from env_utils import load_env_file

try:
    from flask import Flask, render_template_string, request, url_for
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "Flask is not installed in the Python environment you are using. "
        "Run: python -m pip install -r requirements.txt"
    ) from exc


ROOT = Path(__file__).resolve().parent
AGENT_SCRIPT = ROOT / "meeting_agent.py"
CONTEXT_DIR = ROOT / "context_briefs"

load_env_file(ROOT / ".env")

app = Flask(__name__)


def parse_local_datetime(value: str) -> dt.datetime:
    value = value.strip()
    if not value:
        raise ValueError("Join time is required")

    formats = [
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError("Use a time like 2026-04-20T18:30")


@dataclass
class Job:
    id: str
    meeting_link: str
    site: Literal["teams", "zoom"]
    join_time: dt.datetime
    display_name: str
    passcode: str = ""
    voice_mode: bool = False
    voice_device_index: int | None = None
    context_mode: bool = False
    context_lead_minutes: int = 15
    context_brief_path: str = ""
    status: str = "scheduled"
    created_at: dt.datetime = field(default_factory=dt.datetime.now)
    launched_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    error: str | None = None


JOBS: list[Job] = []
LOCK = threading.Lock()


PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>The Second Brain</title>
  <style>
    :root {
      --bg: #0f172a;
      --panel: #111827;
      --panel-2: #1f2937;
      --text: #e5e7eb;
      --muted: #94a3b8;
      --accent: #6366f1;
      --accent-2: #4f46e5;
      --border: #334155;
      --danger: #ef4444;
      --success: #22c55e;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(99, 102, 241, 0.24), transparent 30%),
        radial-gradient(circle at top right, rgba(14, 165, 233, 0.16), transparent 25%),
        var(--bg);
      color: var(--text);
    }
    .wrap {
      max-width: 1100px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }
    .hero {
      display: grid;
      grid-template-columns: 1.3fr 0.9fr;
      gap: 20px;
      margin-bottom: 20px;
    }
    .card {
      background: rgba(17, 24, 39, 0.94);
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 20px;
      box-shadow: 0 30px 80px rgba(0, 0, 0, 0.28);
    }
    h1 {
      margin: 0 0 10px;
      font-size: 40px;
      line-height: 1.05;
    }
    .lead { color: var(--muted); margin: 0; font-size: 15px; line-height: 1.6; }
    form {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }
    .field { display: flex; flex-direction: column; gap: 6px; }
    .field.full { grid-column: 1 / -1; }
    label { font-size: 13px; color: #cbd5e1; }
    input, select {
      width: 100%;
      padding: 12px 14px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--panel-2);
      color: var(--text);
      font-size: 14px;
    }
    .actions {
      grid-column: 1 / -1;
      display: flex;
      gap: 12px;
      align-items: center;
      margin-top: 6px;
    }
    button {
      border: 0;
      border-radius: 12px;
      padding: 12px 16px;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: white;
      font-weight: 700;
      cursor: pointer;
    }
    .ghost {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--text);
      text-decoration: none;
      display: inline-flex;
      align-items: center;
    }
    .status {
      display: inline-block;
      padding: 5px 10px;
      border-radius: 999px;
      font-size: 12px;
      border: 1px solid var(--border);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .scheduled { color: #fde68a; }
    .running { color: #93c5fd; }
    .done { color: var(--success); }
    .error { color: #fca5a5; }
    .grid {
      display: grid;
      gap: 14px;
    }
    .jobs {
      display: grid;
      gap: 12px;
    }
    .job {
      background: rgba(31, 41, 55, 0.9);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 16px;
    }
    .job-top {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 10px;
    }
    .job-title { font-weight: 700; margin-bottom: 4px; }
    .job-meta { color: var(--muted); font-size: 13px; line-height: 1.5; }
    .alert {
      padding: 12px 14px;
      border-radius: 12px;
      margin-bottom: 18px;
      border: 1px solid var(--border);
      background: rgba(31, 41, 55, 0.85);
    }
    .alert.error { border-color: rgba(239, 68, 68, 0.35); }
    .alert.success { border-color: rgba(34, 197, 94, 0.35); }
    .small { color: var(--muted); font-size: 13px; }
    .section-title {
      font-size: 18px;
      font-weight: 700;
      margin: 0 0 12px;
    }
    @media (max-width: 900px) {
      .hero { grid-template-columns: 1fr; }
      form { grid-template-columns: 1fr; }
      .actions { flex-direction: column; align-items: stretch; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div class="card">
        <h1>The Second Brain</h1>
        <p class="lead">
          Paste a Teams or Zoom meeting link, choose a local join time, and the agent will launch in the background when it is due.
        </p>
      </div>
      <div class="card">
        <div class="small">Tool</div>
        <div class="job-title">http://127.0.0.1:5000</div>
        <div class="job-meta">
          Keep this tab open. The browser joiner runs separately when the scheduled time arrives.
        </div>
      </div>
    </div>

    {% if message %}
      <div class="alert {{ message_type }}">{{ message }}</div>
    {% endif %}
    {% if mic_warning %}
      <div class="alert error">
        No microphone devices are visible to Python. Grant microphone permission to
        your terminal or Python app in macOS System Settings, then refresh this page.
      </div>
    {% endif %}

    <div class="card" style="margin-bottom:20px;">
      <div class="section-title">Schedule a Meeting Join</div>
      <form method="post" action="{{ url_for('schedule') }}">
        <div class="field full">
          <label>Meeting link</label>
          <input name="meeting_link" placeholder="https://teams.live.com/meet/..." required>
        </div>
        <div class="field">
          <label>Site</label>
          <select name="site">
            <option value="teams">Teams</option>
            <option value="zoom">Zoom</option>
          </select>
        </div>
        <div class="field">
          <label>Join time</label>
          <input name="join_time" type="datetime-local" required>
        </div>
        <div class="field">
          <label>Agent name</label>
          <input name="display_name" placeholder="Meera" value="Meera">
        </div>
        <div class="field">
          <label>Passcode / token</label>
          <input name="passcode" placeholder="Optional for Teams, required for Zoom">
        </div>
        <div class="field full" style="display:flex; flex-direction:row; align-items:center; gap:10px; margin-top:6px;">
          <input name="voice_mode" type="checkbox" id="voice_mode" style="width:auto; transform: scale(1.1);">
          <label for="voice_mode">Enable Meera conversation mode after join</label>
        </div>
        <div class="small" style="grid-column: 1 / -1; margin-top:-2px;">
          Meera listens continuously and responds directly.
        </div>
        <div class="field">
          <label>Microphone device</label>
          <select name="voice_device_index">
            <option value="">Default microphone</option>
            {% for device in mic_devices %}
              <option value="{{ device.index }}">{{ device.label }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="field full" style="display:flex; flex-direction:row; align-items:center; gap:10px; margin-top:6px;">
          <input name="context_mode" type="checkbox" id="context_mode" style="width:auto; transform: scale(1.1);">
          <label for="context_mode">Fetch GitHub + Jira context before the call</label>
        </div>
        <div class="field">
          <label>Context lead time (minutes)</label>
          <input name="context_lead_minutes" type="number" min="1" value="15">
        </div>
        <div class="actions">
          <button type="submit">Schedule join</button>
          <a class="ghost" href="{{ url_for('index') }}">Refresh</a>
        </div>
      </form>
    </div>

    <div class="card" style="margin-bottom:20px;">
      <div class="section-title">Start Local Conversation</div>
      <div class="small" style="margin-bottom:14px;">
        Use this when you want to talk to Meera without joining a meeting. Pick a microphone and start a background chat session immediately.
      </div>
      <form method="post" action="{{ url_for('conversation') }}">
        <div class="field">
          <label>Agent name</label>
          <input name="display_name" placeholder="Meera" value="Meera">
        </div>
        <div class="field">
          <label>Microphone device</label>
          <select name="voice_device_index">
            <option value="">Default microphone</option>
            {% for device in mic_devices %}
              <option value="{{ device.index }}">{{ device.label }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="field full">
          <label>Context brief path (optional)</label>
          <input name="context_brief_path" placeholder="/Users/you/Desktop/secondBrain/context_briefs/meera_context_brief_20260421_091500.md">
        </div>
        <div class="actions">
          <button type="submit">Start local conversation</button>
          <a class="ghost" href="{{ url_for('index') }}">Refresh</a>
        </div>
      </form>
    </div>

    <div class="card">
      <div class="job-title" style="margin-bottom:14px;">Scheduled Jobs</div>
      <div class="jobs">
        {% for job in jobs %}
          <div class="job">
            <div class="job-top">
              <div>
                <div class="job-title">{{ job.display_name }}</div>
                <div class="job-meta">{{ job.meeting_link }}</div>
              </div>
              <span class="status {{ job.status }}">{{ job.status }}</span>
            </div>
            <div class="job-meta">
              Site: {{ job.site }}<br>
              Join time: {{ job.join_time.strftime("%Y-%m-%d %H:%M:%S") }}<br>
              Created: {{ job.created_at.strftime("%Y-%m-%d %H:%M:%S") }}
              {% if job.launched_at %}
                <br>Launched: {{ job.launched_at.strftime("%Y-%m-%d %H:%M:%S") }}
              {% endif %}
              {% if job.finished_at %}
                <br>Finished: {{ job.finished_at.strftime("%Y-%m-%d %H:%M:%S") }}
              {% endif %}
              {% if job.error %}
                <br>Error: {{ job.error }}
              {% endif %}
              {% if job.context_brief_path %}
                <br>Context brief: {{ job.context_brief_path }}
              {% endif %}
            </div>
          </div>
        {% else %}
          <div class="small">No jobs scheduled yet.</div>
        {% endfor %}
      </div>
    </div>
  </div>
</body>
</html>
"""


def _spawn_agent(job: Job) -> None:
    if not AGENT_SCRIPT.exists():
        job.status = "error"
        job.error = f"Missing agent script: {AGENT_SCRIPT}"
        job.finished_at = dt.datetime.now()
        return

    cmd = [
        sys.executable,
        "-u",
        str(AGENT_SCRIPT),
        "--site",
        job.site,
        "--meeting-id",
        job.meeting_link,
        "--display-name",
        job.display_name,
    ]
    if job.passcode:
        cmd.extend(["--passcode", job.passcode])
    if job.voice_mode:
        cmd.append("--voice-mode")
    if job.voice_device_index is not None:
        cmd.extend(["--voice-device-index", str(job.voice_device_index)])

    job.status = "running"
    job.launched_at = dt.datetime.now()
    env = os.environ.copy()
    if job.context_brief_path and not job.context_brief_path.startswith("failed:"):
        env["MEERA_CONTEXT_BRIEF_PATH"] = job.context_brief_path
    try:
        subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
        )
        job.status = "done"
        job.finished_at = dt.datetime.now()
    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
        job.finished_at = dt.datetime.now()


def _spawn_local_conversation(display_name: str, voice_device_index: int | None, context_brief_path: str) -> None:
    if not AGENT_SCRIPT.exists():
        raise FileNotFoundError(f"Missing agent script: {AGENT_SCRIPT}")

    cmd = [
        sys.executable,
        "-u",
        str(AGENT_SCRIPT),
        "--local-conversation",
        "--display-name",
        display_name,
    ]
    if voice_device_index is not None:
        cmd.extend(["--voice-device-index", str(voice_device_index)])
    if context_brief_path:
        cmd.extend(["--context-brief-path", context_brief_path])

    subprocess.Popen(cmd, cwd=str(ROOT), env=os.environ.copy())


def _schedule_job(job: Job) -> None:
    context_run_time = job.join_time - dt.timedelta(minutes=job.context_lead_minutes)
    if job.context_mode:
        if context_run_time > dt.datetime.now():
            while True:
                if (context_run_time - dt.datetime.now()).total_seconds() <= 0:
                    break
                time.sleep(1)
        _generate_context_brief(job)

    while True:
        if (job.join_time - dt.datetime.now()).total_seconds() <= 0:
            break
        time.sleep(1)
    _spawn_agent(job)


def _list_microphones() -> list[dict[str, object]]:
    try:
        import speech_recognition as sr

        names = sr.Microphone.list_microphone_names()
        return [{"index": idx, "label": f"{idx}: {name}"} for idx, name in enumerate(names)]
    except Exception:
        return []


def _generate_context_brief(job: Job) -> None:
    try:
        from work_context import WorkContextConfig, build_work_context_brief

        github_token = os.environ.get("GITHUB_TOKEN", "").strip()
        github_repos = [repo.strip() for repo in os.environ.get("GITHUB_REPOS", "").split(",") if repo.strip()]
        jira_email = os.environ.get("JIRA_EMAIL", "").strip()
        jira_api_token = os.environ.get("JIRA_API_TOKEN", "").strip()
        jira_base_url = os.environ.get("JIRA_BASE_URL", "").strip()
        jira_project_key = os.environ.get("JIRA_PROJECT_KEY", "").strip()
        jira_jql = os.environ.get("JIRA_JQL", "").strip()

        config = WorkContextConfig(
            github_repos=github_repos,
            jira_base_url=jira_base_url,
            jira_project_key=jira_project_key,
            jira_jql=jira_jql,
            github_token=github_token,
            jira_email=jira_email,
            jira_api_token=jira_api_token,
        )
        brief_path = build_work_context_brief(config, CONTEXT_DIR)
        job.context_brief_path = str(brief_path)
        print(f"[Context] Wrote brief to {brief_path}")
    except Exception as exc:
        job.context_brief_path = f"failed: {exc}"
        print(f"[Context] Failed to build brief: {exc}")


@app.get("/")
def index():
    with LOCK:
        jobs = list(reversed(JOBS))
    mic_devices = _list_microphones()
    return render_template_string(
        PAGE,
        jobs=jobs,
        message=None,
        message_type="success",
        mic_devices=mic_devices,
        mic_warning=not mic_devices,
        os=os,
    )


@app.post("/schedule")
def schedule():
    meeting_link = request.form.get("meeting_link", "").strip()
    site = request.form.get("site", "teams").strip() or "teams"
    join_time_raw = request.form.get("join_time", "").strip()
    display_name = request.form.get("display_name", "").strip() or "Meera"
    passcode = request.form.get("passcode", "").strip()
    voice_mode = request.form.get("voice_mode") == "on"
    voice_device_index_raw = request.form.get("voice_device_index", "").strip()
    voice_device_index = int(voice_device_index_raw) if voice_device_index_raw else None
    context_mode = request.form.get("context_mode") == "on"
    context_lead_minutes_raw = request.form.get("context_lead_minutes", "15").strip()
    try:
        context_lead_minutes = max(1, int(context_lead_minutes_raw))
    except ValueError:
        context_lead_minutes = 15
    if not meeting_link:
        with LOCK:
            jobs = list(reversed(JOBS))
        mic_devices = _list_microphones()
        return render_template_string(
            PAGE,
            jobs=jobs,
            message="Please enter a meeting link.",
            message_type="error",
            mic_devices=mic_devices,
            mic_warning=not mic_devices,
            os=os,
        )

    try:
        join_time = parse_local_datetime(join_time_raw)
    except ValueError as exc:
        with LOCK:
            jobs = list(reversed(JOBS))
        mic_devices = _list_microphones()
        return render_template_string(
            PAGE,
            jobs=jobs,
            message=str(exc),
            message_type="error",
            mic_devices=mic_devices,
            mic_warning=not mic_devices,
            os=os,
        )

    if join_time <= dt.datetime.now():
        with LOCK:
            jobs = list(reversed(JOBS))
        mic_devices = _list_microphones()
        return render_template_string(
            PAGE,
            jobs=jobs,
            message="Join time must be in the future.",
            message_type="error",
            mic_devices=mic_devices,
            mic_warning=not mic_devices,
            os=os,
        )

    job = Job(
        id=uuid.uuid4().hex[:10],
        meeting_link=meeting_link,
        site=site,  # type: ignore[arg-type]
        join_time=join_time,
        display_name=display_name,
        passcode=passcode,
        voice_mode=voice_mode,
        voice_device_index=voice_device_index,
        context_mode=context_mode,
        context_lead_minutes=context_lead_minutes,
    )

    with LOCK:
        JOBS.append(job)

    thread = threading.Thread(target=_schedule_job, args=(job,), daemon=True)
    thread.start()

    with LOCK:
        jobs = list(reversed(JOBS))
    mic_devices = _list_microphones()
    return render_template_string(
        PAGE,
        jobs=jobs,
        message=f"Scheduled {display_name} for {join_time.strftime('%Y-%m-%d %H:%M:%S')}.",
        message_type="success",
        mic_devices=mic_devices,
        mic_warning=not mic_devices,
        os=os,
    )


@app.post("/conversation")
def conversation():
    display_name = request.form.get("display_name", "").strip() or "Meera"
    voice_device_index_raw = request.form.get("voice_device_index", "").strip()
    voice_device_index = int(voice_device_index_raw) if voice_device_index_raw else None
    context_brief_path = request.form.get("context_brief_path", "").strip()

    try:
        _spawn_local_conversation(display_name, voice_device_index, context_brief_path)
        message = f"Started local conversation for {display_name}."
        message_type = "success"
    except Exception as exc:
        message = f"Could not start local conversation: {exc}"
        message_type = "error"

    with LOCK:
        jobs = list(reversed(JOBS))
    mic_devices = _list_microphones()
    return render_template_string(
        PAGE,
        jobs=jobs,
        message=message,
        message_type=message_type,
        mic_devices=mic_devices,
        mic_warning=not mic_devices,
        os=os,
    )


def main() -> int:
    print("The Second Brain running on http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
