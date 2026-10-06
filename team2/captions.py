"""Auto-generated subtitles for incident clips.

The clip's audio is transcribed locally with faster-whisper (no audio leaves
the machine) and saved as a WebVTT file next to the clip, e.g.

    static/incident_clips/session_12/incident_5.webm
    static/incident_clips/session_12/incident_5.vtt

The admin video player loads that file as a <track>, so the browser's own CC
button shows the subtitles. Subtitle times are relative to the clip, which is
exactly what the player uses, so they line up with the video and with the
jump-to-event buttons.

Setup:   pip install faster-whisper
Options (environment variables, all optional):
    PROCTOR_WHISPER_MODEL   tiny | base | small   (default: base)
    PROCTOR_WHISPER_LANG    e.g. en                (default: auto-detect)

The first use downloads the model once (about 140 MB for "base").
Subtitles are machine-generated and can contain mistakes.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

WHISPER_MODEL = os.environ.get("PROCTOR_WHISPER_MODEL", "base")
WHISPER_LANGUAGE = os.environ.get(
    "PROCTOR_WHISPER_LANG") or None  # None = auto-detect

_model = None
_model_lock = threading.Lock()
_transcribe_lock = threading.Lock()  # one transcription at a time (CPU heavy)


class CaptionsUnavailable(RuntimeError):
    """Raised when the speech-to-text package is not installed."""


def _load_model():
    global _model
    with _model_lock:
        if _model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise CaptionsUnavailable(
                    "Subtitles need the faster-whisper package. Run: pip install faster-whisper"
                ) from exc
            _model = WhisperModel(
                WHISPER_MODEL, device="cpu", compute_type="int8")
        return _model


def _timestamp(seconds: float) -> str:
    ms_total = max(0, int(round(float(seconds) * 1000)))
    hours, rem = divmod(ms_total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def segments_to_vtt(segments) -> str:
    """Turn (start, end, text) segments into WebVTT text."""
    lines = ["WEBVTT", ""]
    n = 0
    for seg in segments:
        text = " ".join(str(seg.text).split())
        if not text:
            continue
        end = max(float(seg.end), float(seg.start) + 0.5)
        n += 1
        lines += [str(n),
                  f"{_timestamp(seg.start)} --> {_timestamp(end)}", text, ""]
    return "\n".join(lines)


def vtt_path_for(clip_file: Path) -> Path:
    return Path(clip_file).with_suffix(".vtt")


def get_or_create_vtt(clip_file: Path) -> str:
    """Return the WebVTT text for a clip, transcribing it the first time."""
    clip_file = Path(clip_file)
    vtt_file = vtt_path_for(clip_file)
    if vtt_file.exists():
        return vtt_file.read_text(encoding="utf-8")

    with _transcribe_lock:
        if vtt_file.exists():  # another request finished it while we waited
            return vtt_file.read_text(encoding="utf-8")
        model = _load_model()
        segments, _info = model.transcribe(
            str(clip_file), language=WHISPER_LANGUAGE, beam_size=1, vad_filter=True
        )
        text = segments_to_vtt(list(segments))
        tmp = vtt_file.with_suffix(".vtt.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(vtt_file)
        return text
