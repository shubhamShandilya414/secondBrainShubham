"""Voice interaction entrypoint for Meera.

This script listens for a short spoken prompt, transcribes it with Sarvam AI,
and speaks a canned response when the prompt asks for an update.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import speech_recognition as sr

from sarvam_voice import SarvamVoiceError, speak_with_sarvam


UPDATE_PHRASES = [
    "what is your update",
    "what's your update",
    "whats your update",
    "give me your update",
    "tell me your update",
]


def record_one_utterance(timeout: int = 5, phrase_time_limit: int = 8) -> Path:
    recognizer = sr.Recognizer()
    try:
        with sr.Microphone() as source:
            print("Meera is listening...")
            recognizer.adjust_for_ambient_noise(source, duration=0.5)
            audio = recognizer.listen(
                source,
                timeout=timeout,
                phrase_time_limit=phrase_time_limit,
            )
    except Exception as exc:
        raise SarvamVoiceError(
            "Could not access the microphone. Make sure PyAudio is installed and a microphone is available."
        ) from exc

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    tmp_path.write_bytes(audio.get_wav_data())
    return tmp_path


def transcribe_with_sarvam(audio_path: Path) -> str:
    api_key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not api_key:
        raise SarvamVoiceError("SARVAM_API_KEY is not set")

    try:
        from sarvamai import SarvamAI
    except ModuleNotFoundError as exc:
        raise SarvamVoiceError(
            "sarvamai is not installed. Run: python -m pip install -r requirements.txt"
        ) from exc

    client = SarvamAI(api_subscription_key=api_key)
    with audio_path.open("rb") as file_obj:
        response = client.speech_to_text.transcribe(
            file=file_obj,
            model="saaras:v3",
            mode="transcribe",
            language_code="en-IN",
        )

    if hasattr(response, "transcript"):
        return str(response.transcript).strip()
    if isinstance(response, dict):
        return str(response.get("transcript", "")).strip()
    return str(response).strip()


def should_reply_with_update(text: str) -> bool:
    normalized = " ".join(text.lower().strip().split())
    return any(phrase in normalized for phrase in UPDATE_PHRASES)


def main() -> int:
    audio_path = record_one_utterance()
    try:
        transcript = transcribe_with_sarvam(audio_path)
        print(f"Heard: {transcript}")

        if should_reply_with_update(transcript):
            reply = "Let me fetch the context."
            print(f"Meera: {reply}")
            speak_with_sarvam(reply, speaker="shreya", target_language_code="en-IN")
        else:
            print("Meera did not detect the update question.")
    finally:
        try:
            audio_path.unlink(missing_ok=True)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
