"""Browser-based meeting join agent.

This agent supports Zoom and Microsoft Teams style meetings.

Usage:
    python meeting_agent.py --site teams --meeting-id 123456 --passcode token
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import subprocess
import tempfile
import sys
import re
import os
import time
import threading
import queue
from typing import Optional
from pathlib import Path

from env_utils import load_env_file
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from sarvam_reasoning import SarvamReasoningError, reason_with_sarvam
from sarvam_voice import SarvamVoiceError, speak_with_sarvam

try:
    import speech_recognition as sr
except ModuleNotFoundError:
    sr = None

try:
    import requests
except ModuleNotFoundError:
    requests = None

load_env_file()

#https://teams.live.com/meet/937131588618?p=vf6q274cE0FhjdS1E0
@dataclasses.dataclass(frozen=True)
class MeetingConfig:
    meeting_id: str
    passcode: str
    display_name: str = "Meera"
    headless: bool = False
    browser: str = "chromium"
    site: str = "teams"
    voice_mode: bool = False
    announce_join: bool = False
    voice_device_index: int | None = None
    context_brief_path: str = ""
    local_conversation_only: bool = False


@dataclasses.dataclass
class ConversationState:
    last_issue_key: str = ""
    last_issue_context: str = ""
    last_summary: str = ""
    is_speaking: bool = False
    suppress_until: float = 0.0
    end_requested: bool = False


class MeetingJoinAgent:
    """Join a meeting in a browser using a meeting ID and passcode."""

    def __init__(self, config: MeetingConfig) -> None:
        self.config = config
        self._context_brief_text = self._load_context_brief()
        self._conversation_state = ConversationState()

    def _log(self, message: str) -> None:
        print(f"[Meera] {message}", flush=True)

    def join(self) -> None:
        if self.config.local_conversation_only:
            self._handle_conversation_session()
            return

        if not self.config.meeting_id.strip():
            raise ValueError("meeting_id is required unless local conversation mode is enabled")

        join_url = self._build_join_url(
            self.config.site, self.config.meeting_id, self.config.passcode
        )

        with sync_playwright() as p:
            browser_type = getattr(p, self.config.browser, None)
            if browser_type is None:
                raise ValueError(f"Unsupported browser: {self.config.browser}")

            launch_args = []
            if self.config.site == "teams" and self.config.browser == "chromium":
                launch_args.append("--use-fake-ui-for-media-stream")

            browser = browser_type.launch(
                headless=self.config.headless,
                args=launch_args,
            )
            context_options = {"viewport": {"width": 1280, "height": 900}}
            if self.config.site == "teams":
                context_options["permissions"] = ["camera", "microphone"]
            context = browser.new_context(**context_options)
            if self.config.site == "teams":
                context.grant_permissions(
                    ["camera", "microphone"], origin="https://teams.live.com"
                )
            page = context.new_page()
            page.goto(join_url, wait_until="domcontentloaded")
            if not self.config.headless:
                try:
                    page.bring_to_front()
                except Exception:
                    pass

            if self.config.site == "zoom":
                self._handle_zoom_join(page)
            else:
                self._handle_teams_join(page)

            self._wait_for_meeting_entry(page)
            if self.config.announce_join:
                self._announce_join()
            if self.config.voice_mode:
                self._start_conversation_session()

            # Keep the browser open so the user can interact with the meeting.
            if not self.config.headless:
                try:
                    page.bring_to_front()
                except Exception:
                    pass
            print("Meeting join flow started. Leave the browser open to continue.")
            print(f"Opened: {join_url}")

            try:
                input("Press Enter to close the browser when you're done...")
            finally:
                context.close()
                browser.close()

    def _build_join_url(self, site: str, meeting_id: str, passcode: str) -> str:
        if site == "zoom":
            return self._build_zoom_join_url(meeting_id, passcode)
        if site == "teams":
            return self._build_teams_join_url(meeting_id, passcode)
        raise ValueError(
            f"Unsupported site: {site}. Use 'zoom' or 'teams'."
        )

    def _build_zoom_join_url(self, meeting_id: str, passcode: str) -> str:
        meeting_id = re.sub(r"\D", "", meeting_id)
        if not meeting_id:
            raise ValueError("meeting_id must contain at least one digit")

        if not passcode:
            raise ValueError("passcode is required")

        encoded = base64.urlsafe_b64encode(passcode.encode("utf-8")).decode("utf-8")
        return f"https://zoom.us/wc/join/{meeting_id}?pwd={encoded}"

    def _build_teams_join_url(self, meeting_id: str, passcode: str) -> str:
        meeting_id = meeting_id.strip()
        if not meeting_id:
            raise ValueError("meeting_id is required for Teams")

        if meeting_id.startswith("http://") or meeting_id.startswith("https://"):
            return meeting_id

        token = passcode.strip()
        if token:
            return f"https://teams.live.com/meet/{meeting_id}?p={token}"
        return f"https://teams.live.com/meet/{meeting_id}"

    def _handle_zoom_join(self, page) -> None:
        self._try_click(page, [
            "text=Join from Your Browser",
            "text=Join from your browser",
            "text=Join from Browser",
        ])

        self._fill_if_present(page, [
            'input[name="displayName"]',
            'input[placeholder*="Name" i]',
            'input[type="text"]',
        ], self.config.display_name)

        self._fill_if_present(page, [
            'input[name="passcode"]',
            'input[placeholder*="passcode" i]',
            'input[type="password"]',
        ], self.config.passcode)

        self._try_click(page, [
            "button:has-text('Join')",
            "text=Join",
            "button:has-text('Continue')",
        ])

    def _handle_teams_join(self, page) -> None:
        # Teams often starts on a launcher page with just one primary action.
        self._click_text_anywhere(page, [
            "Continue on this browser",
            "Join on the Teams app",
            "Join on the web instead",
            "Use the web app instead",
            "Join on the browser",
        ])

        # Some Teams flows show the same launcher again after a redirect.
        self._sleep_and_retry_join(page, [
            "Continue on this browser",
            "Join on the web instead",
        ])

        self._wait_for_teams_prejoin(page)
        self._fill_teams_name(page)
        self._type_into_focused_name_field(page)
        self._commit_teams_name(page)
        self._click_teams_join_now(page)

    def _wait_for_teams_prejoin(self, page) -> bool:
        selectors = [
            'input[placeholder*="Type your name" i]',
            'input[placeholder*="name" i]',
            'input[name="name"]',
            'button:has-text("Join now")',
            'button:has-text("Join Now")',
        ]
        for selector in selectors:
            try:
                page.locator(selector).first.wait_for(state="visible", timeout=15000)
                return True
            except Exception:
                continue
        return False

    def _fill_teams_name(self, page) -> bool:
        selectors = [
            'input[placeholder="Type your name"]',
            'input[placeholder*="Type your name" i]',
            'input[aria-label*="name" i]',
            'input[placeholder*="name" i]',
            'input[name="name"]',
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=5000)
                locator.click(timeout=1500, force=True)
                locator.fill(self.config.display_name, timeout=5000)
                return True
            except Exception:
                continue

        if self._fill_if_present(page, selectors, self.config.display_name):
            return True

        try:
            field = page.get_by_placeholder("Type your name", exact=False).first
            if field.count() and field.is_visible():
                field.fill(self.config.display_name, timeout=2500)
                return True
        except Exception:
            pass

        try:
            field = page.get_by_role("textbox").first
            if field.count() and field.is_visible():
                field.fill(self.config.display_name, timeout=2500)
                return True
        except Exception:
            pass

        return False

    def _type_into_focused_name_field(self, page) -> bool:
        try:
            focused = page.locator(":focus").first
            if focused.count() and focused.is_visible():
                focused.click(timeout=1500, force=True)
                focused.fill("", timeout=1500)
                page.keyboard.insert_text(self.config.display_name)
                page.keyboard.press("Tab")
                return True
        except Exception:
            pass

        try:
            page.keyboard.type(self.config.display_name, delay=40)
            page.keyboard.press("Tab")
            return True
        except Exception:
            return False

    def _commit_teams_name(self, page) -> bool:
        try:
            page.keyboard.press("Enter")
            return True
        except Exception:
            pass

        try:
            page.keyboard.press("Tab")
            page.keyboard.press("Enter")
            return True
        except Exception:
            return False

    def _click_teams_join_now(self, page) -> bool:
        selectors = [
            "button:has-text('Join now')",
            "button:has-text('Join Now')",
            "button:has-text('Join')",
            "text=Join now",
            "text=Join",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=5000)
                locator.click(timeout=5000, force=True)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeoutError:
                    pass
                try:
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
                return True
            except Exception:
                continue

        if self._click_text_anywhere(page, ["Join now", "Join"]):
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                pass
            try:
                page.wait_for_timeout(1000)
            except Exception:
                pass
            return True

        return False

    def _click_text_anywhere(self, page, labels: list[str]) -> bool:
        for label in labels:
            if self._click_by_role_or_text(page, label):
                return True
            if self._click_by_dom_scan(page, label):
                return True
            if self._click_in_frames(page, label):
                return True
        return False

    def _sleep_and_retry_join(self, page, labels: list[str]) -> bool:
        for _ in range(5):
            try:
                page.wait_for_timeout(800)
            except Exception:
                pass
            if self._click_text_anywhere(page, labels):
                return True
        return False

    def _click_by_role_or_text(self, page, label: str) -> bool:
        try:
            button = page.get_by_role(
                "button", name=re.compile(re.escape(label), re.I)
            ).first
            if button.count() and button.is_visible():
                button.click(timeout=2500, force=True)
                return True
        except Exception:
            pass

        try:
            link = page.get_by_role(
                "link", name=re.compile(re.escape(label), re.I)
            ).first
            if link.count() and link.is_visible():
                link.click(timeout=2500, force=True)
                return True
        except Exception:
            pass

        try:
            text = page.get_by_text(label, exact=False).first
            if text.count() and text.is_visible():
                text.click(timeout=2500, force=True)
                return True
        except Exception:
            pass

        return False

    def _click_by_dom_scan(self, page, label: str) -> bool:
        script = """
        (needle) => {
          const lower = needle.toLowerCase();
          const candidates = Array.from(
            document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]')
          );
          const match = candidates.find((el) => {
            const text = (el.innerText || el.value || el.textContent || '').trim().toLowerCase();
            return text.includes(lower);
          });
          if (!match) return false;
          match.click();
          return true;
        }
        """
        try:
            return bool(page.evaluate(script, label))
        except Exception:
            return False

    def _click_in_frames(self, page, label: str) -> bool:
        for frame in page.frames:
            try:
                if frame == page.main_frame:
                    continue
                if self._click_in_frame(frame, label):
                    return True
            except Exception:
                continue
        return False

    def _click_in_frame(self, frame, label: str) -> bool:
        script = """
        (needle) => {
          const lower = needle.toLowerCase();
          const candidates = Array.from(
            document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]')
          );
          const match = candidates.find((el) => {
            const text = (el.innerText || el.value || el.textContent || '').trim().toLowerCase();
            return text.includes(lower);
          });
          if (!match) return false;
          match.click();
          return true;
        }
        """
        try:
            return bool(frame.evaluate(script, label))
        except Exception:
            return False

    def _try_click(self, page, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    locator.click(timeout=2500)
                    return True
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
        return False

    def _fill_if_present(self, page, selectors: list[str], value: str) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    locator.fill(value, timeout=2500)
                    return True
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
        return False

    def _wait_for_meeting_entry(self, page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeoutError:
            pass

    def _announce_join(self) -> None:
        message = "I joined on Shubham's behalf."
        self._log(message)
        try:
            speak_with_sarvam(message, speaker="shreya", target_language_code="en-IN")
            return
        except SarvamVoiceError as exc:
            self._log(f"Sarvam voice unavailable: {exc}")
        except Exception as exc:
            self._log(f"Sarvam voice failed: {exc}")

        if sys.platform == "darwin":
            try:
                subprocess.run(["say", message], check=False)
            except Exception:
                pass

    def _start_conversation_session(self) -> None:
        thread = threading.Thread(target=self._handle_conversation_session, daemon=True)
        thread.start()

    def _handle_conversation_session(self, duration_seconds: int | None = None) -> None:
        if sr is None:
            print("SpeechRecognition is not installed, skipping conversation mode.")
            return

        if not self._has_input_device():
            self._log(
                "No microphone input device is visible to Python. "
                "Check macOS microphone permission for Terminal/Python and select an input device."
            )
            return

        api_key = os.environ.get("SARVAM_API_KEY", "").strip()
        if not api_key:
            self._log("SARVAM_API_KEY is not set, skipping conversation mode.")
            return

        try:
            from sarvamai import SarvamAI
        except ModuleNotFoundError:
            self._log("sarvamai is not installed, skipping conversation mode.")
            return

        client = SarvamAI(api_subscription_key=api_key)
        deadline = None if duration_seconds is None else time.time() + duration_seconds
        transcript_queue: queue.Queue[str] = queue.Queue()
        stop_event = threading.Event()
        recognizer = sr.Recognizer()
        mic_kwargs = {}
        if self.config.voice_device_index is not None:
            mic_kwargs["device_index"] = self.config.voice_device_index

        self._log("conversation mode enabled. Meera is now listening continuously.")

        def callback(_: object, audio: object) -> None:
            if stop_event.is_set():
                return
            if self._conversation_state.is_speaking or time.time() < self._conversation_state.suppress_until:
                return
            audio_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp.write(audio.get_wav_data())
                    audio_path = tmp.name

                with open(audio_path, "rb") as file_obj:
                    response = client.speech_to_text.transcribe(
                        file=file_obj,
                        model="saaras:v3",
                        mode="transcribe",
                        language_code="en-IN",
                    )

                transcript = getattr(response, "transcript", None)
                if transcript is None and isinstance(response, dict):
                    transcript = response.get("transcript")
                transcript = str(transcript or "").strip()
                if transcript:
                    transcript_queue.put(transcript)
            except Exception as exc:
                self._log(f"Voice prompt failed: {exc}")
            finally:
                if audio_path:
                    try:
                        os.unlink(audio_path)
                    except Exception:
                        pass

        stop_listening = None
        try:
            with sr.Microphone(**mic_kwargs) as calibration_source:
                recognizer.adjust_for_ambient_noise(calibration_source, duration=0.4)

            source = sr.Microphone(**mic_kwargs)
            self._log("microphone ready, listening...")
            stop_listening = recognizer.listen_in_background(
                source,
                callback,
                phrase_time_limit=8,
            )
            try:
                while deadline is None or time.time() < deadline:
                    try:
                        transcript = transcript_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if not transcript:
                        continue
                    self._process_conversation_transcript(client, transcript)
            finally:
                stop_event.set()
                try:
                    if stop_listening is not None:
                        stop_listening(wait_for_stop=False)
                except Exception:
                    pass
        except Exception as exc:
            self._log(f"Could not access microphone: {exc}")

    def _process_conversation_transcript(self, client, transcript: str) -> None:
        self._log(f"Heard transcript: {transcript}")
        self._remember_conversation_context(transcript)

        self._conversation_state.is_speaking = True
        try:
            reasoned_reply = self._generate_reasoned_reply(transcript)
            if reasoned_reply and reasoned_reply.strip().lower() != "none":
                self._log(f"responding: {reasoned_reply}")
                speak_with_sarvam(reasoned_reply, speaker="shreya", target_language_code="en-IN")
                self._conversation_state.suppress_until = time.time() + self._post_speech_cooldown_seconds(reasoned_reply)
                return

            reply = self._build_conversation_reply(transcript)
            if not reply:
                self._log("No response needed.")
                return

            self._log(f"responding: {reply}")
            speak_with_sarvam(reply, speaker="shreya", target_language_code="en-IN")
            self._conversation_state.suppress_until = time.time() + self._post_speech_cooldown_seconds(reply)
        except Exception as exc:
            self._log(f"Voice prompt failed: {exc}")
        finally:
            self._conversation_state.is_speaking = False

    def _post_speech_cooldown_seconds(self, text: str) -> float:
        raw = os.environ.get("MEERA_POST_SPEECH_COOLDOWN", "").strip()
        if raw:
            try:
                return max(0.15, min(1.0, float(raw)))
            except ValueError:
                pass
        # Short debounce so Meera does not hear her own TTS, but can catch your next reply quickly.
        return max(0.2, min(0.6, len(text) / 80.0))

    def _build_conversation_reply(self, text: str) -> str:
        normalized = re.sub(r"[^a-z0-9\s]", " ", text.lower())
        normalized = " ".join(normalized.split())
        if not normalized:
            return ""

        if self._looks_like_end_call(normalized):
            self._conversation_state.end_requested = True
            return self._end_call_reply(normalized)

        if self._conversation_state.end_requested:
            return self._hangup_reminder_reply(normalized)

        if self._looks_like_partial_jira_key(normalized):
            return self._partial_jira_key_reply(normalized)

        if self._looks_like_acknowledgement_action(normalized):
            if self._conversation_state.last_issue_key:
                self._update_conversation_state_from_reply(
                    self._conversation_state.last_issue_key,
                    self._acknowledgement_reply(normalized),
                )
            return self._acknowledgement_reply(normalized)

        jira_key = self._extract_jira_issue_key(normalized)
        if jira_key:
            live_jira_reply = self._summarize_jira_issue_live(jira_key, normalized)
            if live_jira_reply:
                return live_jira_reply
            jira_reply = self._summarize_jira_issue(jira_key, normalized)
            if jira_reply:
                return jira_reply

        if self._conversation_state.last_issue_key and self._looks_like_jira_intent(normalized):
            followup_reply = (
                self._summarize_jira_issue_live(self._conversation_state.last_issue_key, normalized)
                or self._summarize_jira_issue(self._conversation_state.last_issue_key, normalized)
            )
            if followup_reply:
                return followup_reply

        if self._looks_like_capability_question(normalized):
            return self._capability_reply()

        if self._looks_like_context_request(normalized):
            context_reply = self._context_brief_reply(normalized)
            if self._conversation_state.last_issue_key:
                issue_followup = self._summarize_jira_issue_live(self._conversation_state.last_issue_key, normalized) or self._summarize_jira_issue(self._conversation_state.last_issue_key, normalized)
                if issue_followup:
                    return issue_followup
            return context_reply or self._default_bug_finder_reply()

        if self._context_brief_text:
            matched = self._match_brief_line(normalized)
            if matched:
                return matched

        if any(word in normalized for word in {"hello", "hi", "hey"}):
            return "Hi, I’m Meera."

        if any(word in normalized for word in {"thanks", "thank you"}):
            return "You’re welcome."

        bug_reply = self._default_bug_finder_reply(normalized)
        if bug_reply:
            return bug_reply
        return "Share a Jira ticket key like CUDI-18 and I’ll summarize it."

    def _generate_reasoned_reply(self, transcript: str) -> str:
        if os.environ.get("MEERA_USE_REASONING", "1").strip().lower() in {"0", "false", "no"}:
            return ""

        api_key = os.environ.get("SARVAM_API_KEY", "").strip()
        if not api_key:
            return ""

        normalized = re.sub(r"[^a-z0-9\s]", " ", transcript.lower())
        normalized = " ".join(normalized.split())
        if self._looks_like_end_call(normalized):
            self._conversation_state.end_requested = True
            return self._end_call_reply(normalized)
        if self._conversation_state.end_requested:
            return self._hangup_reminder_reply(normalized)
        if self._looks_like_partial_jira_key(normalized):
            return self._partial_jira_key_reply(normalized)
        if self._looks_like_acknowledgement_action(normalized):
            reply = self._acknowledgement_reply(normalized)
            self._remember_conversation_context(transcript)
            return reply

        issue_key = self._extract_jira_issue_key(transcript)
        followup_issue = False
        if not issue_key and self._conversation_state.last_issue_key and (
            self._is_followup_about_issue(transcript) or self._looks_like_jira_intent(transcript.lower())
        ):
            issue_key = self._conversation_state.last_issue_key
            followup_issue = True

        structured_reply = ""
        if issue_key:
            issue_context = self._live_jira_context(issue_key) or self._jira_context_from_brief(issue_key)
            if not issue_context and followup_issue:
                issue_context = self._conversation_state.last_issue_context
            structured_reply = self._summarize_jira_issue_live(issue_key, transcript) or self._summarize_jira_issue(issue_key, transcript)
            if not structured_reply:
                structured_reply = self._conversation_state.last_summary or ""
        else:
            issue_context = self._context_brief_excerpt(transcript)
            if self._looks_like_capability_question(transcript):
                structured_reply = self._capability_reply()
            elif self._looks_like_context_request(transcript):
                structured_reply = self._default_bug_finder_reply(transcript) or self._context_brief_reply(transcript)
                if not structured_reply and self._conversation_state.last_summary:
                    structured_reply = self._conversation_state.last_summary
            else:
                structured_reply = self._default_bug_finder_reply(transcript)

        if issue_key or followup_issue or self._looks_like_context_request(transcript) or self._is_followup_about_issue(transcript):
            if structured_reply:
                self._update_conversation_state_from_reply(issue_key, structured_reply)
                if issue_context and not self._conversation_state.last_issue_context:
                    self._conversation_state.last_issue_context = issue_context
                return structured_reply
            return self._fallback_jira_summary(transcript, issue_key)

        messages = [
            {
                "role": "system",
                "content": (
                    "You are Meera, a helpful meeting assistant for Jira-heavy calls. "
                    "Answer like a real teammate: natural, short, and specific. "
                    "Understand the user's intent first. "
                    "If they ask about a Jira issue, summarize the issue, status, comments, blockers, or next steps in plain language. "
                    "Do not quote Jira comments verbatim unless the user explicitly asks for a quote. "
                    "Summarize what the comment means. "
                    "If they ask what you can do, explain your capabilities briefly. "
                    "If the intent is unclear, ask one short clarifying question instead of guessing. "
                    "Keep responses to 1-3 sentences and avoid robotic phrasing."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"User transcript: {transcript}\n\n"
                    f"Relevant context:\n{issue_context or 'No additional context available.'}\n\n"
                    f"Reference answer:\n{structured_reply or 'No reference answer available.'}\n\n"
                    "Reply as Meera."
                ),
            },
        ]

        try:
            result = reason_with_sarvam(
                messages,
                model=os.environ.get("SARVAM_REASONING_MODEL", "sarvam-105b"),
                reasoning_effort=os.environ.get("SARVAM_REASONING_EFFORT", "high"),
                temperature=0.2,
                max_tokens=220,
            )
            reply = result.content.strip()
            if reply and reply.lower() not in {"none", "null"} and "i don't see any transcript" not in reply.lower():
                self._update_conversation_state_from_reply(issue_key, reply)
                if issue_context and not self._conversation_state.last_issue_context:
                    self._conversation_state.last_issue_context = issue_context
                return reply
        except SarvamReasoningError as exc:
            self._log(f"Sarvam reasoning unavailable: {exc}")
        except Exception as exc:
            self._log(f"Sarvam reasoning failed: {exc}")
        if structured_reply:
            self._update_conversation_state_from_reply(issue_key, structured_reply)
            if issue_context and not self._conversation_state.last_issue_context:
                self._conversation_state.last_issue_context = issue_context
        return structured_reply

    def _looks_like_acknowledgement_action(self, text: str) -> bool:
        phrases = [
            "i will close the issue",
            "i'll close the issue",
            "i will close it",
            "i'll close it",
            "close the issue",
            "mark it closed",
            "i will mark it closed",
            "i'll mark it closed",
            "that makes sense",
            "got it",
            "understood",
        ]
        return any(phrase in text for phrase in phrases)

    def _acknowledgement_reply(self, text: str) -> str:
        if any(phrase in text for phrase in {"i will close", "i'll close", "close the issue", "mark it closed"}):
            return "Got it. Closing it sounds right."
        if any(phrase in text for phrase in {"that makes sense", "got it", "understood"}):
            return "Perfect. Let me know if you want me to pull anything else."
        return "Understood."

    def _looks_like_end_call(self, text: str) -> bool:
        phrases = [
            "end the call",
            "end call",
            "close the call",
            "finish the call",
            "wrap up",
            "we are done",
            "we're done",
            "that is all",
            "that's all",
            "bye",
            "goodbye",
            "hang up",
            "let's end",
            "lets end",
            "let's wrap up",
            "lets wrap up",
        ]
        return any(phrase in text for phrase in phrases)

    def _looks_like_partial_jira_key(self, text: str) -> bool:
        compact = re.sub(r"[^a-z0-9]", "", text.lower())
        project_key = os.environ.get("JIRA_PROJECT_KEY", "").strip().lower()
        if not compact:
            return False

        if project_key and compact == project_key:
            return True

        if project_key and compact.startswith(project_key) and not re.search(r"\d", compact[len(project_key):]):
            return True

        if len(compact) <= 5 and compact.isalpha():
            return True

        if len(compact) <= 8 and compact.isalnum() and not re.search(r"-\d+$", compact):
            return True

        return False

    def _partial_jira_key_reply(self, text: str) -> str:
        project_key = os.environ.get("JIRA_PROJECT_KEY", "").strip() or "the project key"
        return f"I need the full ticket key, like {project_key}-18, to look it up."

    def _end_call_reply(self, text: str) -> str:
        if any(word in text for word in {"bye", "goodbye"}):
            return "Thanks, bye."
        if any(word in text for word in {"wrap up", "close the call", "end the call", "finish the call"}):
            return "Thanks. We can wrap here."
        return "Thanks. We’re done here."

    def _hangup_reminder_reply(self, text: str) -> str:
        if any(word in text for word in {"still here", "not yet", "wait", "one sec", "one second"}):
            return "No problem. Please hang up when you’re ready."
        return "We can stop here. Please hang up when you’re ready."

    def _looks_like_context_request(self, text: str) -> bool:
        phrases = [
            "what is your update",
            "what's your update",
            "give me your update",
            "tell me the update",
            "status update",
            "any update",
            "what is the status",
            "summarize",
            "summary",
            "plan for the call",
            "what should we discuss",
            "what is happening",
        ]
        if any(phrase in text for phrase in phrases):
            return True
        return any(word in text for word in {"update", "updates", "status", "plan", "summary"})

    def _is_followup_about_issue(self, text: str) -> bool:
        followup_markers = {
            "next action",
            "next step",
            "next steps",
            "what next",
            "what is the next action",
            "what is next",
            "comments",
            "latest comment",
            "latest comments",
            "status",
            "blocker",
            "issue",
            "bug",
            "ticket",
            "that one",
            "this one",
            "current issue",
        }
        if any(marker in text for marker in followup_markers):
            return True
        return any(word in text for word in {"next", "action", "comment", "comments", "blocker", "blocked"})

    def _remember_conversation_context(self, transcript: str) -> None:
        issue_key = self._extract_jira_issue_key(transcript)
        if issue_key:
            self._conversation_state.last_issue_key = issue_key
            issue_context = self._live_jira_context(issue_key) or self._jira_context_from_brief(issue_key)
            if issue_context:
                self._conversation_state.last_issue_context = issue_context
            summary = self._summarize_jira_issue_live(issue_key, transcript) or self._summarize_jira_issue(issue_key, transcript)
            if summary:
                self._conversation_state.last_summary = summary
            return

        if self._is_followup_about_issue(transcript) and self._conversation_state.last_issue_key:
            return

    def _update_conversation_state_from_reply(self, issue_key: str, reply: str) -> None:
        if issue_key:
            self._conversation_state.last_issue_key = issue_key
        if reply:
            self._conversation_state.last_summary = reply

    def _live_jira_context(self, issue_key: str) -> str:
        if requests is None:
            return ""

        base_url = os.environ.get("JIRA_BASE_URL", "").strip().rstrip("/")
        if not base_url:
            return ""

        jira_email = os.environ.get("JIRA_EMAIL", "").strip()
        jira_api_token = os.environ.get("JIRA_API_TOKEN", "").strip()
        headers = {"Accept": "application/json"}
        auth = (jira_email, jira_api_token) if jira_email and jira_api_token else None
        url = f"{base_url}/rest/api/3/issue/{issue_key}"
        try:
            response = requests.get(
                url,
                params={"fields": "summary,status,assignee,comment,updated,issuetype"},
                headers=headers,
                auth=auth,
                timeout=30,
            )
            if response.status_code == 404:
                return ""
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            self._log(f"Live Jira lookup failed for {issue_key}: {exc}")
            return ""

        fields = payload.get("fields", {})
        summary = fields.get("summary", "")
        status = (fields.get("status") or {}).get("name", "")
        assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
        issue_type = (fields.get("issuetype") or {}).get("name", "")
        updated = fields.get("updated", "")
        comments = fields.get("comment", {}).get("comments", []) or []
        lines = [f"{issue_key}: {summary}".strip(": ")]
        if issue_type:
            lines.append(f"type: {issue_type}")
        if status:
            lines.append(f"status: {status}")
        if assignee:
            lines.append(f"assignee: {assignee}")
        if updated:
            lines.append(f"updated: {updated}")
        for idx, comment in enumerate(comments[:3], start=1):
            body = self._jira_body_to_text(comment.get("body", ""))
            if body:
                lines.append(f"comment {idx}: {body}")
        return "\n".join(lines).strip()

    def _jira_context_from_brief(self, issue_key: str) -> str:
        block = self._find_jira_issue_block(issue_key)
        if not block:
            return ""

        lines = [f"{issue_key}: {block.get('summary', '')}".strip(": ")]
        for key in ("status", "assignee"):
            value = block.get(key, "")
            if value:
                lines.append(f"{key}: {value}")
        comments = block.get("comments", [])
        for idx, comment in enumerate(comments[:3], start=1):
            lines.append(f"comment {idx}: {comment}")
        return "\n".join(lines).strip()

    def _context_brief_excerpt(self, transcript: str) -> str:
        if not self._context_brief_text:
            return ""

        terms = {term for term in re.findall(r"[a-z0-9]+", transcript.lower()) if len(term) >= 3}
        lines = self._context_brief_text.splitlines()
        hits: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- ") and any(term in stripped.lower() for term in terms):
                hits.append(stripped[2:].strip())
        if not hits:
            hits = self._brief_highlights()[:3]
        return "\n".join(hits[:5])

    def _looks_like_capability_question(self, text: str) -> bool:
        phrases = [
            "what can you do",
            "what do you do",
            "what are you",
            "how can you help",
            "help me",
            "who are you",
            "tell me about yourself",
        ]
        if any(phrase in text for phrase in phrases):
            if self._looks_like_jira_intent(text):
                return False
            return True
        return text.endswith("?") and any(word in text for word in {"help", "capable", "can"})

    def _looks_like_jira_intent(self, text: str) -> bool:
        if self._extract_jira_issue_key(text):
            return True

        project_key = os.environ.get("JIRA_PROJECT_KEY", "").strip().lower()
        compact = re.sub(r"[^a-z0-9]", "", text.lower())
        if project_key and project_key in compact:
            return True

        jira_words = {
            "jira",
            "ticket",
            "issue",
            "bug",
            "problem",
            "solve",
            "update",
            "status",
            "comment",
            "comments",
            "blocker",
            "blocked",
            "non bug",
            "not a bug",
            "valid bug",
            "close the issue",
            "close it",
            "next action",
            "next step",
        }
        return any(phrase in text for phrase in jira_words)

    def _load_context_brief(self) -> str:
        path = (
            self.config.context_brief_path
            or os.environ.get("MEERA_CONTEXT_BRIEF_PATH", "").strip()
        )
        if not path:
            return ""

        brief_path = Path(path)
        if not brief_path.exists():
            self._log(f"Context brief not found: {brief_path}")
            return ""

        try:
            return brief_path.read_text(encoding="utf-8")
        except Exception as exc:
            self._log(f"Could not read context brief: {exc}")
            return ""

    def _context_brief_reply(self, query_text: str = "") -> str:
        highlights = self._brief_highlights()
        if highlights:
            jira_line = self._best_jira_highlight(query_text) if query_text else ""
            if jira_line:
                return jira_line
            return "Here’s the Jira context: " + "; ".join(highlights[:2])
        return ""

    def _brief_highlights(self) -> list[str]:
        if not self._context_brief_text:
            return []

        lines = self._context_brief_text.splitlines()
        highlights: list[str] = []
        in_section = False
        for line in lines:
            stripped = line.strip()
            if stripped == "### What Meera Should Say":
                in_section = True
                continue
            if in_section and stripped.startswith("### "):
                break
            if in_section and stripped.startswith("- "):
                highlights.append(stripped[2:].strip())
        return highlights

    def _match_brief_line(self, text: str) -> str:
        brief_lines = [
            line.strip("- ").strip()
            for line in self._context_brief_text.splitlines()
            if line.strip().startswith("- ")
        ]
        tokens = {token for token in text.split() if len(token) >= 3}
        best_line = ""
        best_score = 0
        for line in brief_lines:
            lower = line.lower()
            score = sum(1 for token in tokens if token in lower)
            if score > best_score:
                best_score = score
                best_line = line
        if best_score:
            return f"Here’s what I found: {best_line}"
        return ""

    def _extract_jira_issue_key(self, text: str) -> str:
        upper_text = text.upper()

        explicit = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", upper_text)
        if explicit:
            return explicit.group(1)

        compact = re.sub(r"[^A-Z0-9]", "", upper_text)
        project_key = os.environ.get("JIRA_PROJECT_KEY", "").strip().upper()
        if not project_key:
            return ""

        if compact.startswith(project_key):
            issue_number = compact[len(project_key):]
            if issue_number.isdigit():
                return f"{project_key}-{int(issue_number)}"

        compact_project = re.sub(r"[^A-Z0-9]", "", project_key)
        if compact.startswith(compact_project):
            issue_number = compact[len(compact_project):]
            if issue_number.isdigit():
                return f"{project_key}-{int(issue_number)}"

        spaced_project = "".join(ch for ch in project_key if ch.isalnum())
        if spaced_project and spaced_project in compact:
            suffix = compact.split(spaced_project, 1)[1]
            if suffix.isdigit():
                return f"{project_key}-{int(suffix)}"

        return ""

    def _default_bug_finder_reply(self, text: str = "") -> str:
        if not self._context_brief_text:
            return ""
        if text and not any(
            word in text
            for word in {"jira", "bug", "ticket", "issue", "comment", "comments", "status", "blocker", "blocked", "summary"}
        ):
            return ""
        jira_line = self._best_jira_highlight(text)
        if jira_line:
            return jira_line
        highlights = self._brief_highlights()
        if highlights:
            return "Jira bug summary: " + "; ".join(highlights[:2])
        return ""

    def _capability_reply(self) -> str:
        return (
            "Yeah, I can look at Jira and tell you the gist. "
            "Give me the ticket key and I’ll keep it short."
        )

    def _summarize_jira_issue(self, issue_key: str, query_text: str = "") -> str:
        issue = self._find_jira_issue_block(issue_key)
        if not issue:
            return ""

        summary = issue.get("summary", "")
        status = issue.get("status", "")
        assignee = issue.get("assignee", "")
        comments = issue.get("comments", [])

        intent = self._infer_issue_intent(query_text)
        return self._format_human_jira_reply(
            issue_key=issue_key,
            summary=summary,
            status=status,
            assignee=assignee,
            issue_type="",
            comments=comments,
            intent=intent,
        )

    def _summarize_jira_issue_live(self, issue_key: str, query_text: str = "") -> str:
        if requests is None:
            self._log("requests is not installed, skipping live Jira lookup.")
            return ""

        base_url = os.environ.get("JIRA_BASE_URL", "").strip().rstrip("/")
        if not base_url:
            return ""

        jira_email = os.environ.get("JIRA_EMAIL", "").strip()
        jira_api_token = os.environ.get("JIRA_API_TOKEN", "").strip()

        headers = {"Accept": "application/json"}
        auth = None
        if jira_email and jira_api_token:
            auth = (jira_email, jira_api_token)

        issue_url = f"{base_url}/rest/api/3/issue/{issue_key}"
        params = {
            "fields": "summary,status,assignee,comment,updated,issuetype",
        }
        try:
            response = requests.get(issue_url, params=params, headers=headers, auth=auth, timeout=30)
            if response.status_code == 404:
                return ""
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            self._log(f"Live Jira lookup failed for {issue_key}: {exc}")
            return ""

        fields = payload.get("fields", {})
        summary = fields.get("summary", "")
        status = (fields.get("status") or {}).get("name", "")
        assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
        issue_type = (fields.get("issuetype") or {}).get("name", "")
        updated = fields.get("updated", "")
        comments = fields.get("comment", {}).get("comments", []) or []
        comment_texts = [
            self._jira_body_to_text(comment.get("body", ""))
            for comment in comments[:3]
        ]
        intent = self._infer_issue_intent(query_text)
        reply = self._format_human_jira_reply(
            issue_key=issue_key,
            summary=summary,
            status=status,
            assignee=assignee,
            issue_type=issue_type,
            comments=comment_texts,
            intent=intent,
            updated=updated,
        )
        return reply

    def _find_jira_issue_block(self, issue_key: str) -> dict[str, object]:
        if not self._context_brief_text:
            return {}

        lines = self._context_brief_text.splitlines()
        pattern = re.compile(rf"^- {re.escape(issue_key)}:\s*(.*)$")
        for index, line in enumerate(lines):
            match = pattern.match(line.strip())
            if not match:
                continue

            summary = match.group(1).strip()
            status = ""
            assignee = ""
            comments: list[str] = []
            cursor = index + 1
            while cursor < len(lines):
                raw = lines[cursor].rstrip()
                stripped = raw.strip()
                if not stripped:
                    cursor += 1
                    continue
                if stripped.startswith("- ") and not raw.startswith("  - "):
                    break
                if stripped.startswith("### ") or stripped.startswith("## ") or stripped == "---":
                    break
                if stripped.startswith("- Type:"):
                    pass
                elif stripped.startswith("- Status:"):
                    status = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("- Assignee:"):
                    assignee = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("- Comments:"):
                    cursor += 1
                    while cursor < len(lines):
                        comment_line = lines[cursor].rstrip()
                        comment_stripped = comment_line.strip()
                        if not comment_stripped:
                            cursor += 1
                            continue
                        if comment_stripped.startswith("- ") and not comment_line.startswith("    - "):
                            cursor -= 1
                            break
                        if comment_line.startswith("    - "):
                            comments.append(comment_stripped[2:].strip())
                        elif comment_stripped.startswith("### ") or comment_stripped.startswith("## ") or comment_stripped.startswith("- "):
                            cursor -= 1
                            break
                        cursor += 1
                cursor += 1

            return {
                "summary": summary,
                "status": status,
                "assignee": assignee,
                "comments": comments,
            }
        return {}

    def _infer_issue_intent(self, query_text: str) -> str:
        if any(word in query_text for word in {"why", "non bug", "not a bug", "valid bug", "should we close", "close it"}):
            return "why"
        if any(word in query_text for word in {"comment", "comments", "said", "say", "update"}):
            return "comments"
        if any(word in query_text for word in {"status", "state", "open", "closed"}):
            return "status"
        if any(word in query_text for word in {"block", "blocked", "blocker", "risk"}):
            return "blocker"
        if any(word in query_text for word in {"next", "next step", "plan", "what should", "what to do"}):
            return "next"
        return "overview"

    def _format_human_jira_reply(
        self,
        issue_key: str,
        summary: str,
        status: str,
        assignee: str,
        issue_type: str,
        comments: list[str],
        intent: str,
        updated: str = "",
    ) -> str:
        summary = str(summary).strip()
        status = str(status).strip()
        assignee = str(assignee).strip()
        issue_type = str(issue_type).strip()
        updated = str(updated).strip()
        comments = [c.strip() for c in comments if str(c).strip()]

        if not comments and not summary:
            return ""

        meaning = ""
        if comments:
            if intent == "comments":
                meaning = self._summarize_comment_meaning(comments[:2])
            elif intent == "why":
                meaning = self._summarize_comment_meaning(comments[:2])
            elif intent == "blocker":
                meaning = self._summarize_single_comment(comments[0])
            elif intent == "next":
                meaning = self._summarize_single_comment(comments[0])
            else:
                meaning = self._summarize_single_comment(comments[0])

        if intent == "why":
            if meaning:
                return f"Yeah, because {meaning}."
            if summary:
                return f"Yeah, this looks more like expected behavior than a bug."
            return "Yeah, this looks more like expected behavior than a bug."

        if intent == "next":
            if meaning:
                return f"I’d close it, honestly. {meaning}."
            return "I’d close it if that matches the team’s call."

        if intent == "blocker":
            if meaning:
                return f"It looks blocked because {meaning}."
            return "It looks blocked right now."

        if intent == "comments":
            if meaning:
                return f"The comments basically say {meaning}."
            return "There isn’t much extra in the comments."

        if intent == "status":
            base = summary or issue_key
            if status and assignee and assignee.lower() != "unassigned":
                return f"{base} is still {status.lower()}, and it’s with {assignee}."
            if status:
                return f"{base} is still {status.lower()}."
            return f"{base} is the ticket we’re looking at."

        base = summary or issue_key
        if comments and meaning:
            if summary:
                return f"{summary}. The comments point to {meaning}."
            return f"It looks like {meaning}."

        if status and assignee and assignee.lower() != "unassigned":
            return f"{base} is still {status.lower()}, and it’s with {assignee}."
        if status:
            return f"{base} is still {status.lower()}."
        if assignee and assignee.lower() != "unassigned":
            return f"{base} is with {assignee}."
        if updated:
            return f"{base} was last updated on {updated}."
        return base

    def _summarize_comment_meaning(self, comments: list[str]) -> str:
        text = " ".join(comment.strip() for comment in comments if comment.strip()).lower()
        if not text:
            return "the comments do not add much more context"
        if "shipped orders cannot be canceled" in text or "cannot be canceled" in text or "can't be canceled" in text or "cannot cancel" in text:
            if "not a valid bug" in text or "invalid bug" in text or "should be closed" in text:
                return "shipped orders cannot be canceled, so this looks like a non-bug that should be closed"
            return "shipped orders cannot be canceled"
        if "not a valid bug" in text or "invalid bug" in text or "should be closed" in text:
            return "this looks like a non-bug and should be closed"
        if "blocked" in text or "blocker" in text:
            return "there is still a blocker in the way"
        return self._simplify_comment_text(text)

    def _summarize_single_comment(self, comment: str) -> str:
        text = comment.strip().lower()
        if not text:
            return "there’s no extra detail in the comment"
        if "shipped orders cannot be canceled" in text or "cannot be canceled" in text or "can't be canceled" in text or "cannot cancel" in text:
            return "shipped orders cannot be canceled"
        if "not a valid bug" in text or "invalid bug" in text or "should be closed" in text:
            return "this looks like a non-bug and should probably be closed"
        if "blocked" in text or "blocker" in text:
            return "the work is blocked"
        return self._simplify_comment_text(text)

    def _fallback_jira_summary(self, transcript: str, issue_key: str = "") -> str:
        key = issue_key or self._extract_jira_issue_key(transcript)
        if not key:
            key = self._conversation_state.last_issue_key

        if not key:
            return "I need a full Jira ticket key like CUDI-18 to summarize the issue."

        issue = self._summarize_jira_issue_live(key, transcript) or self._summarize_jira_issue(key, transcript)
        if issue:
            return issue

        if self._conversation_state.last_summary:
            return self._conversation_state.last_summary

        return f"I checked {key}, but I do not have enough context to summarize it."

    def _simplify_comment_text(self, text: str) -> str:
        cleaned = re.sub(r"\s+", " ", text).strip()
        cleaned = cleaned.replace("NEXT ACTION-", "next action:")
        cleaned = cleaned.replace("NEXT ACTION", "next action")
        cleaned = cleaned.replace("NEXT ACTION:", "next action:")
        cleaned = cleaned.replace("  ", " ")
        return cleaned[:1].upper() + cleaned[1:] if cleaned else ""

    def _join_nicely(self, items: list[str]) -> str:
        clean = [item.strip() for item in items if item.strip()]
        if not clean:
            return ""
        if len(clean) == 1:
            return clean[0]
        if len(clean) == 2:
            return f"{clean[0]} and {clean[1]}"
        return ", ".join(clean[:-1]) + f", and {clean[-1]}"

    def _jira_body_to_text(self, value: object, max_len: int = 220) -> str:
        text = self._adf_to_text(value).strip()
        if not text:
            return ""
        text = re.sub(r"\s+", " ", text)
        if len(text) <= max_len:
            return text
        return text[: max_len - 1].rstrip() + "…"

    def _adf_to_text(self, value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(part for part in (self._adf_to_text(item) for item in value) if part)
        if isinstance(value, dict):
            if "text" in value and isinstance(value["text"], str):
                return value["text"]
            if "content" in value:
                return self._adf_to_text(value["content"])
            if "value" in value and isinstance(value["value"], str):
                return value["value"]
            return " ".join(
                part for part in (self._adf_to_text(item) for item in value.values()) if part
            )
        return str(value)

    def _best_jira_highlight(self, text: str) -> str:
        if not self._context_brief_text:
            return ""

        lines = self._context_brief_text.splitlines()
        issue_lines = [line.strip() for line in lines if re.match(r"^- [A-Z][A-Z0-9]+-\d+:", line.strip())]
        if not issue_lines:
            return ""

        if not text:
            first = issue_lines[0]
            return f"Here’s the Jira bug I found: {first.lstrip('- ').strip()}"

        tokens = {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) >= 3}
        best_line = ""
        best_score = 0
        for line in issue_lines:
            lowered = line.lower()
            score = sum(1 for token in tokens if token in lowered)
            if score > best_score:
                best_score = score
                best_line = line

        if best_line:
            return f"Here’s the Jira bug I found: {best_line.lstrip('- ').strip()}"
        return f"Here’s the Jira bug I found: {issue_lines[0].lstrip('- ').strip()}"

    def _has_input_device(self) -> bool:
        try:
            import pyaudio

            pa = pyaudio.PyAudio()
            try:
                return pa.get_device_count() > 0
            finally:
                pa.terminate()
        except Exception:
            return False


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Join a Zoom or Teams meeting using browser automation."
    )
    parser.add_argument(
        "--meeting-id",
        default="",
        help="Meeting ID (required unless --local-conversation is used)",
    )
    parser.add_argument(
        "--passcode",
        default="",
        help="Meeting passcode or token (required for Zoom, optional for Teams URLs)",
    )
    parser.add_argument(
        "--display-name",
        default="Meera",
        help="Display name used in the meeting",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the browser headlessly",
    )
    parser.add_argument(
        "--site",
        default="teams",
        choices=["zoom", "teams"],
        help="Meeting provider to use",
    )
    parser.add_argument(
        "--browser",
        default="chromium",
        choices=["chromium", "firefox", "webkit"],
        help="Browser engine to use",
    )
    parser.add_argument(
        "--voice-mode",
        action="store_true",
        help="Enable continuous conversation mode after joining and answer with Sarvam voice",
    )
    parser.add_argument(
        "--voice-device-index",
        type=int,
        default=None,
        help="Optional microphone device index for conversation mode",
    )
    parser.add_argument(
        "--announce-join",
        action="store_true",
        help="Speak 'I joined on Shubham's behalf.' after joining",
    )
    parser.add_argument(
        "--context-brief-path",
        default="",
        help="Optional Markdown brief to use for conversation responses",
    )
    parser.add_argument(
        "--local-conversation",
        action="store_true",
        help="Run Meera locally without joining a meeting",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    config = MeetingConfig(
        meeting_id=args.meeting_id,
        passcode=args.passcode,
        display_name=args.display_name,
        headless=args.headless,
        browser=args.browser,
        site=args.site,
        voice_mode=args.voice_mode,
        voice_device_index=args.voice_device_index,
        announce_join=args.announce_join,
        context_brief_path=args.context_brief_path,
        local_conversation_only=args.local_conversation,
    )
    agent = MeetingJoinAgent(config)
    agent.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
