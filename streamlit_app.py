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
from zoneinfo import ZoneInfo

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


def _app_timezone() -> ZoneInfo:
    tz_name = _get_setting("MEERA_TIMEZONE", "Asia/Kolkata")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def _now_local() -> dt.datetime:
    return dt.datetime.now(_app_timezone())


def _parse_join_time(value: str) -> dt.datetime:
    value = value.strip()
    if not value:
        raise ValueError("Join time is required")
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return parsed
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError("Use a time like 2026-04-20T18:30 or a full ISO time like 2026-04-20T18:30:00+05:30")


def _combine_join_datetime(join_date: dt.date, join_time: dt.time) -> dt.datetime:
    return dt.datetime.combine(join_date, join_time.replace(second=0, microsecond=0), tzinfo=_app_timezone())


def _quick_join_slots(count: int = 10, step_minutes: int = 2) -> list[tuple[str, dt.datetime]]:
    now = _now_local().replace(second=0, microsecond=0)
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
    persona: str,
    call_type: str,
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
        "persona": persona.strip(),
        "call_type": call_type.strip(),
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

with st.sidebar:
    st.caption(f"App timezone: {_app_timezone().key}")
    st.caption(f"Current app time: {_now_local().strftime('%Y-%m-%d %H:%M:%S %Z')}")

tab_meeting, tab_local = st.tabs(["Schedule meeting", "Local conversation"])
worker_url = _get_setting("MEERA_WORKER_URL", "")

PERSONA_OPTIONS = [
    "Software Developer",
    "Architect",
    "Business Analyst",
    "Sales",
    "Marketing",
]
CALL_TYPES_BY_PERSONA = {
    "Software Developer": ["BUG FIX CALL"],
    "Architect": ["ARCHITECTURE CALL"],
    "Business Analyst": ["REQUIREMENTS CALL"],
    "Sales": ["SALES CALL"],
    "Marketing": ["MARKETING CALL"],
}

if "persona" not in st.session_state:
    st.session_state.persona = PERSONA_OPTIONS[0]
if "call_type" not in st.session_state:
    st.session_state.call_type = CALL_TYPES_BY_PERSONA[st.session_state.persona][0]


def _sync_call_type() -> None:
    persona_value = st.session_state.get("persona", PERSONA_OPTIONS[0])
    allowed = CALL_TYPES_BY_PERSONA.get(persona_value, [CALL_TYPES_BY_PERSONA[PERSONA_OPTIONS[0]][0]])
    if st.session_state.get("call_type") not in allowed:
        st.session_state.call_type = allowed[0]

with tab_meeting:
    st.subheader("Schedule a meeting join")
    persona = st.selectbox("Persona", PERSONA_OPTIONS, key="persona", on_change=_sync_call_type)
    call_type = st.selectbox("Call type", CALL_TYPES_BY_PERSONA.get(persona, [CALL_TYPES_BY_PERSONA[PERSONA_OPTIONS[0]][0]]), key="call_type")
    bug_fix_context_enabled = persona == "Software Developer" and call_type == "BUG FIX CALL"
    build_job_enabled = bug_fix_context_enabled
    if not bug_fix_context_enabled:
        st.info("Currently only the Software Developer / BUG FIX CALL persona is configured for context loading. Other personas are visible, but their context is not wired yet.")
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
            join_date = st.date_input("Join date", value=_now_local().date())
            join_clock = st.time_input(
                "Join time",
                value=(_now_local() + dt.timedelta(minutes=30)).time().replace(second=0, microsecond=0),
            )
            st.caption(f"This uses {_app_timezone().key}.")
        display_name = st.text_input("Agent name", value="Meera")
        passcode = st.text_input("Passcode / token", placeholder="Optional for Teams, required for Zoom")
        voice_mode = st.checkbox("Enable conversation mode after join")
        voice_device_index = st.text_input("Microphone device index", placeholder="Leave blank for default")
        context_mode = st.checkbox(
            "Load persona context before the call",
            value=bug_fix_context_enabled,
            disabled=not bug_fix_context_enabled,
        )
        context_lead_minutes = st.selectbox("Context lead time (minutes)", [1, 5, 10, 15, 30, 60], index=3)
        if not build_job_enabled:
            st.caption("Build job is only enabled for Software Developer / BUG FIX CALL right now.")
        submitted = st.form_submit_button("Build job", disabled=not build_job_enabled)

    if submitted:
        try:
            persona = st.session_state.get("persona", PERSONA_OPTIONS[0])
            call_type = st.session_state.get("call_type", CALL_TYPES_BY_PERSONA[persona][0])
            if persona != "Software Developer" or call_type != "BUG FIX CALL":
                context_mode = False
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
                join_time=join_time_value.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
                display_name=display_name,
                passcode=passcode,
                voice_mode=voice_mode,
                voice_device_index=voice_device_index,
                context_mode=context_mode,
                context_lead_minutes=str(context_lead_minutes),
                persona=persona,
                call_type=call_type,
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
                join_time=_now_local().astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
                display_name=local_display_name,
                passcode="",
                voice_mode=local_voice_mode,
                voice_device_index=local_voice_device_index,
                context_mode=False,
                context_lead_minutes="1",
                persona=PERSONA_OPTIONS[0],
                call_type=CALL_TYPES_BY_PERSONA[PERSONA_OPTIONS[0]][0],
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
