"""Sarvam AI text-to-speech helper for Meera's spoken announcements."""

from __future__ import annotations

import base64
import os
import platform
import subprocess
import tempfile
from pathlib import Path


class SarvamVoiceError(RuntimeError):
    pass


def speak_with_sarvam(
    text: str,
    *,
    speaker: str = "shreya",
    model: str = "bulbul:v3",
    target_language_code: str = "en-IN",
) -> None:
    """Generate speech with Sarvam AI and play it locally.

    Requires SARVAM_API_KEY in the environment.
    """

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
    response = client.text_to_speech.convert(
        text=text,
        model=model,
        target_language_code=target_language_code,
        speaker=speaker,
    )

    audio_blob = _extract_audio_blob(response)
    if not audio_blob:
        raise SarvamVoiceError("Sarvam AI returned no audio")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(audio_blob)

    try:
        _play_audio_file(tmp_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _extract_audio_blob(response) -> bytes:
    if hasattr(response, "audios"):
        audios = getattr(response, "audios")
    elif isinstance(response, dict):
        audios = response.get("audios")
    else:
        audios = None

    if not audios:
        return b""

    audio = audios[0]
    if isinstance(audio, bytes):
        return audio
    if isinstance(audio, str):
        return base64.b64decode(audio)
    return b""


def _play_audio_file(path: Path) -> None:
    system = platform.system().lower()
    if system == "darwin":
        subprocess.run(["afplay", str(path)], check=False)
        return

    if system == "linux":
        for cmd in (["paplay", str(path)], ["aplay", str(path)]):
            try:
                subprocess.run(cmd, check=False)
                return
            except FileNotFoundError:
                continue
        raise SarvamVoiceError("No audio player found (paplay/aplay)")

    if system == "windows":
        subprocess.run(
            [
                "powershell",
                "-c",
                f"(New-Object Media.SoundPlayer '{path}').PlaySync();",
            ],
            check=False,
        )
        return

    raise SarvamVoiceError(f"Unsupported platform: {platform.system()}")
