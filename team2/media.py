from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from flask import current_app

from models import db
from .store import get_incident, list_segments_for_incident, attach_event_offsets
from sqlalchemy import text


def _binary(name: str) -> str | None:
    configured = os.environ.get(name.upper() + "_BIN")
    return configured or shutil.which(name)


def _probe(path: str) -> dict | None:
    ffprobe = _binary("ffprobe")
    if not ffprobe or not os.path.exists(path):
        return None
    p = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def _probe_duration(path: str) -> float | None:
    data = _probe(path)
    if not data:
        return None
    try:
        return float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        return None


def validate_media(path: str) -> bool:
    data = _probe(path)
    if not data or not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    try:
        duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        return False
    streams = data.get("streams", [])
    has_video = any(s.get("codec_type") == "video" for s in streams)
    return duration > 0 and has_video


def _has_audio(path: str) -> bool:
    data = _probe(path)
    return bool(data and any(s.get("codec_type") == "audio" for s in data.get("streams", [])))


def _transcode_contiguous(rows: list[dict], output_path: Path) -> dict[int, float]:
    """Decode each independently playable WebM segment and re-encode one continuous WebM.

    The old concat-demuxer/stream-copy approach can leave WebM timestamp/cluster boundaries
    that make a browser stop early or freeze at a later segment. The concat filter resets each
    segment's timestamps before joining them into one timeline.
    """
    ffmpeg = _binary("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg was not found on PATH")

    durations: dict[int, float] = {}
    usable = []
    for row in rows:
        path = row["file_path"]
        duration = _probe_duration(path)
        if duration is None or duration <= 0:
            continue
        usable.append(row)
        durations[int(row["sequence_no"])] = duration

    if not usable:
        raise RuntimeError("No valid recording segments are available")

    # All normal Team 2 segments contain audio. If one segment is missing audio, we still
    # preserve the video by producing a video-only clip rather than generating a broken file.
    with_audio = all(_has_audio(row["file_path"]) for row in usable)

    cmd = [ffmpeg, "-y"]
    filters = []
    if with_audio:
        for i, row in enumerate(usable):
            cmd += ["-i", row["file_path"]]
            filters.append(f"[{i}:v:0]setpts=PTS-STARTPTS[v{i}]")
            filters.append(f"[{i}:a:0]asetpts=PTS-STARTPTS[a{i}]")
        concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(len(usable)))
        filters.append(f"{concat_inputs}concat=n={len(usable)}:v=1:a=1[v][a]")
        cmd += [
            "-filter_complex", ";".join(filters),
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libvpx-vp9", "-b:v", "900k",
            "-c:a", "libopus", "-b:a", "64k",
            "-deadline", "good", "-f", "webm", str(output_path),
        ]
    else:
        for i, row in enumerate(usable):
            cmd += ["-i", row["file_path"]]
            filters.append(f"[{i}:v:0]setpts=PTS-STARTPTS[v{i}]")
        concat_inputs = "".join(f"[v{i}]" for i in range(len(usable)))
        filters.append(f"{concat_inputs}concat=n={len(usable)}:v=1:a=0[v]")
        cmd += [
            "-filter_complex", ";".join(filters),
            "-map", "[v]",
            "-c:v", "libvpx-vp9", "-b:v", "900k",
            "-deadline", "good", "-f", "webm", str(output_path),
        ]

    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if p.returncode != 0 or not validate_media(str(output_path)):
        output_path.unlink(missing_ok=True)
        detail = (p.stderr or p.stdout or "FFmpeg failed").strip()[-2000:]
        raise RuntimeError(f"FFmpeg could not produce a valid incident video: {detail}")
    return durations


def finalize_incident(incident_id: int, force_end: bool = False, exam_end_ms: int | None = None) -> dict:
    incident = get_incident(incident_id)
    if not incident:
        raise ValueError("Incident not found")
    if incident["status"] == "finalized" and incident.get("clip_path"):
        return incident

    session_id = int(incident["session_id"])
    first_event = int(incident["first_event_exam_ms"])
    last_event = int(incident["last_event_exam_ms"])
    required_end = int(exam_end_ms) if force_end and exam_end_ms is not None else last_event + 10_000

    segments = list_segments_for_incident(session_id, first_event, last_event)
    segments = [s for s in segments if os.path.exists(s["file_path"]) and os.path.getsize(s["file_path"]) > 0]
    if not segments:
        raise RuntimeError("No recording segments are available for this incident yet")

    # For normal 10-second quiet closure, do not finalize until the recording has actually
    # captured the whole requested post-event window. This prevents clips from being cut off
    # while the final 5-second MediaRecorder segment is still uploading.
    if not force_end:
        latest_end = max(int(s["end_exam_ms"]) for s in segments)
        if latest_end < required_end:
            raise RuntimeError(
                f"Recording coverage is not complete yet ({latest_end}ms < {required_end}ms); waiting for the final segment"
            )

    # Only use a contiguous run from the first relevant segment. A missing sequence means
    # there is a real recording gap, so the clip must not silently jump over it.
    contiguous = [segments[0]]
    for row in segments[1:]:
        previous = contiguous[-1]
        if int(row["sequence_no"]) != int(previous["sequence_no"]) + 1:
            break
        contiguous.append(row)

    if not force_end and int(contiguous[-1]["end_exam_ms"]) < required_end:
        raise RuntimeError("Recording segments contain a gap before the incident closing point; waiting for upload/recovery")

    clip_start_exam_ms = int(contiguous[0]["start_exam_ms"])
    clip_end_exam_ms = int(contiguous[-1]["end_exam_ms"])

    root = Path(current_app.root_path)
    out_dir = root / "static" / "incident_clips" / f"session_{session_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"incident_{incident_id}_{uuid.uuid4().hex[:10]}"
    output_path = out_dir / f"{stem}.webm"

    durations = _transcode_contiguous(contiguous, output_path)
    rel_path = str(output_path.relative_to(root)).replace("\\", "/")

    db.session.execute(
        text(
            "UPDATE team2_incident SET status='finalized', clip_path=:clip, "
            "clip_start_exam_ms=:start_ms, clip_end_exam_ms=:end_ms, finalized_at=CURRENT_TIMESTAMP WHERE id=:id"
        ),
        {"clip": rel_path, "start_ms": clip_start_exam_ms, "end_ms": clip_end_exam_ms, "id": incident_id},
    )
    db.session.commit()
    attach_event_offsets(incident_id, contiguous, clip_start_exam_ms, duration_overrides=durations)
    return get_incident(incident_id)
