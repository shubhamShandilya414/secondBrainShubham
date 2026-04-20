"""Streamlit frontend for scheduling Meera jobs.

This UI is meant to be the light front end. A separate local worker consumes
the exported job JSON and actually launches the browser/mic automation.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from pathlib import Path

from env_utils import load_env_file

try:
    import streamlit as st
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "Streamlit is not installed in this Python environment. "
        "Run: python -m pip install -r requirements.txt"
    ) from exc

try:
    import requests
except ModuleNotFoundError:
    requests = None


ROOT = Path(__file__).resolve().parent
QUEUE_DIR = ROOT / "streamlit_jobs" / "inbox"

load_env_file(ROOT / ".env")


def _get_setting(name: str, default: str = "") -> str:
    try:
        secret_value = st.secrets.get(name, "")
        if secret_value:
            return str(secret_value).strip()
    except Exception:
        pass
    env_value = os.environ.get(name, "").strip()
    if env_value:
        return env_value
    return default


def _parse_join_time(value: str) -> dt.datetime:
    value = value.strip()
    if not value:
        raise ValueError("Join time is required")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError("Use a time like 2026-04-20T18:30")


def _combine_join_datetime(join_date: dt.date, join_time: dt.time) -> dt.datetime:
    return dt.datetime.combine(join_date, join_time.replace(second=0, microsecond=0))


def _quick_join_slots(count: int = 10, step_minutes: int = 2) -> list[tuple[str, dt.datetime]]:
    now = dt.datetime.now().replace(second=0, microsecond=0)
    remainder = now.minute % step_minutes
    if remainder == 0:
        start = now + dt.timedelta(minutes=step_minutes)
    else:
        start = now + dt.timedelta(minutes=step_minutes - remainder)
    slots: list[tuple[str, dt.datetime]] = []
    for index in range(count):
        slot_time = start + dt.timedelta(minutes=step_minutes * index)
        label = slot_time.strftime("%a %I:%M %p")
        slots.append((label, slot_time))
    return slots


def _build_job_payload(
    *,
    kind: str,
    meeting_link: str,
    site: str,
    join_time: str,
    display_name: str,
    passcode: str,
    voice_mode: bool,
    voice_device_index: str,
    context_mode: bool,
    context_lead_minutes: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": uuid.uuid4().hex[:10],
        "kind": kind,
        "meeting_link": meeting_link.strip(),
        "site": site.strip() or "teams",
        "join_time": _parse_join_time(join_time).isoformat(timespec="seconds"),
        "display_name": display_name.strip() or "Meera",
        "passcode": passcode.strip(),
        "voice_mode": voice_mode,
        "voice_device_index": int(voice_device_index) if voice_device_index.strip() else None,
        "context_mode": context_mode,
        "context_lead_minutes": max(1, int(context_lead_minutes or "15")),
        "context_brief_path": "",
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    if kind == "local_conversation":
        payload["meeting_link"] = ""
        payload["site"] = "teams"
        payload["join_time"] = ""
        payload["passcode"] = ""
    return payload


def _queue_job(payload: dict[str, object]) -> Path | None:
    try:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        path = QUEUE_DIR / f"{payload['id']}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path
    except Exception:
        return None


def _submit_to_remote_worker(payload: dict[str, object], worker_url: str) -> str:
    if requests is None:
        raise ModuleNotFoundError("requests is not installed in this Python environment.")
    worker_url = worker_url.strip().rstrip("/")
    if not worker_url:
        raise ValueError("Worker URL is empty")

    response = requests.post(f"{worker_url}/jobs", json=payload, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "Worker rejected the job"))
    return str(data.get("job_id", payload.get("id", "job")))


st.set_page_config(page_title="The Second Brain", page_icon="🧠", layout="wide")
st.title("The Second Brain")
st.caption("The UI stays light. The worker on your machine actually joins calls and uses the microphone.")

with st.sidebar:
    st.header("How it works")
    st.markdown(
        """
        1. Fill the form here.
        2. Download the job JSON or queue it locally.
        3. Run `worker.py` on the machine that has the browser and mic.
        4. The worker launches Meera at the right time.
        """
    )
    st.markdown(
        """
        **Important:** Streamlit Cloud cannot control your microphone or browser directly.
        Keep the worker on a local machine, VM, or server with GUI access.
        """
    )
    worker_url = st.text_input(
        "Worker URL (ngrok)",
        value=_get_setting("MEERA_WORKER_URL", ""),
        help="Paste the public ngrok URL for your local worker, for example https://abcd-1234.ngrok-free.app",
    )

tab_meeting, tab_local = st.tabs(["Schedule meeting", "Local conversation"])

with tab_meeting:
    st.subheader("Schedule a meeting join")
    with st.form("meeting_form"):
        meeting_link = st.text_input("Meeting link", placeholder="https://teams.live.com/meet/...")
        site = st.selectbox("Site", ["teams", "zoom"], index=0)
        join_time_mode = st.radio("Join time mode", ["Quick slots", "Custom"], horizontal=True)
        quick_slots = _quick_join_slots()
        quick_slot_labels = [f"{label}  ({slot.strftime('%H:%M')})" for label, slot in quick_slots]
        selected_quick_slot = None
        if join_time_mode == "Quick slots":
            slot_label = st.selectbox("Next 10 time slots, 2 minutes apart", quick_slot_labels, index=0)
            selected_quick_slot = quick_slots[quick_slot_labels.index(slot_label)][1]
            st.caption("These are local-time slots starting from the next available 2-minute boundary.")
        else:
            join_date = st.date_input("Join date", value=dt.date.today())
            join_clock = st.time_input(
                "Join time",
                value=(dt.datetime.now() + dt.timedelta(minutes=30)).time().replace(second=0, microsecond=0),
            )
            st.caption("This uses your local time zone.")
        display_name = st.text_input("Agent name", value="Meera")
        passcode = st.text_input("Passcode / token", placeholder="Optional for Teams, required for Zoom")
        voice_mode = st.checkbox("Enable conversation mode after join")
        voice_device_index = st.text_input("Microphone device index", placeholder="Leave blank for default")
        context_mode = st.checkbox("Fetch GitHub + Jira context before the call")
        context_lead_minutes = st.selectbox("Context lead time (minutes)", [1, 5, 10, 15, 30, 60], index=3)
        submitted = st.form_submit_button("Build job")

    if submitted:
        try:
            if join_time_mode == "Quick slots":
                if selected_quick_slot is None:
                    raise ValueError("Please choose a quick time slot.")
                join_time_value = selected_quick_slot
            else:
                join_time_value = _combine_join_datetime(join_date, join_clock)
            payload = _build_job_payload(
                kind="meeting",
                meeting_link=meeting_link,
                site=site,
                join_time=join_time_value.isoformat(timespec="seconds"),
                display_name=display_name,
                passcode=passcode,
                voice_mode=voice_mode,
                voice_device_index=voice_device_index,
                context_mode=context_mode,
                context_lead_minutes=str(context_lead_minutes),
            )
            queued = None
            remote_job_id = None
            if worker_url.strip():
                remote_job_id = _submit_to_remote_worker(payload, worker_url)
            else:
                queued = _queue_job(payload)
            st.success("Job built successfully.")
            if queued:
                st.info(f"Queued locally at {queued}.")
            if remote_job_id:
                st.info(f"Submitted to remote worker as job {remote_job_id}.")
            st.download_button(
                "Download job JSON",
                data=json.dumps(payload, indent=2),
                file_name=f"meera_job_{payload['id']}.json",
                mime="application/json",
            )
            st.code(json.dumps(payload, indent=2), language="json")
        except Exception as exc:
            st.error(str(exc))

with tab_local:
    st.subheader("Start local conversation")
    with st.form("local_form"):
        local_display_name = st.text_input("Agent name", value="Meera", key="local_display_name")
        local_voice_device_index = st.text_input("Microphone device index", placeholder="Leave blank for default", key="local_voice_device_index")
        local_context_brief_path = st.text_input("Context brief path (optional)", placeholder="/path/to/context_brief.md")
        local_voice_mode = st.checkbox("Enable voice mode after start", value=True)
        st.caption("Local conversation starts immediately, so no meeting time is needed.")
        local_submitted = st.form_submit_button("Build local job")

    if local_submitted:
        try:
            payload = _build_job_payload(
                kind="local_conversation",
                meeting_link="",
                site="teams",
                join_time=dt.datetime.now().isoformat(timespec="seconds"),
                display_name=local_display_name,
                passcode="",
                voice_mode=local_voice_mode,
                voice_device_index=local_voice_device_index,
                context_mode=False,
                context_lead_minutes="1",
            )
            payload["context_brief_path"] = local_context_brief_path.strip()
            queued = None
            remote_job_id = None
            if worker_url.strip():
                remote_job_id = _submit_to_remote_worker(payload, worker_url)
            else:
                queued = _queue_job(payload)
            st.success("Local conversation job built successfully.")
            if queued:
                st.info(f"Queued locally at {queued}.")
            if remote_job_id:
                st.info(f"Submitted to remote worker as job {remote_job_id}.")
            st.download_button(
                "Download job JSON",
                data=json.dumps(payload, indent=2),
                file_name=f"meera_local_job_{payload['id']}.json",
                mime="application/json",
            )
            st.code(json.dumps(payload, indent=2), language="json")
        except Exception as exc:
            st.error(str(exc))
