"""Video helpers for facial recordings -- all through ffmpeg / ffprobe.

Facial recordings arrive as camera/phone files (often iPhone HEVC .mov,
sometimes 4K). Nothing here re-encodes the ORIGINAL: it is copied and
published untouched. ffmpeg only makes the extras:
  * `probe()`        -- resolution / fps / frames / duration / has audio
  * `thumbnail()`    -- one JPEG frame for the Review tab
  * `extract_audio()`-- the video's own (scratch) audio, for auto-sync
  * `review_video()` -- a browser-safe H.264 copy with the CLEAN mic audio,
                        shifted into sync -- the AYON reviewable
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Optional

import numpy as np

from . import tools

log = logging.getLogger("ssd_dhvani.media")

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mxf", ".webm"}
ZIP_EXTS = {".zip"}


def _run(cmd: list, timeout: float = 600, text: bool = False):
    return subprocess.run(cmd, capture_output=True, timeout=timeout, text=text,
                          **tools.subprocess_flags())


def probe(path) -> dict:
    """{'width','height','fps','frames','duration','has_audio','codec'} --
    best effort; {} when ffprobe is missing or the file isn't media."""
    exe = tools.ffprobe()
    if not exe:
        return {}
    try:
        r = _run([exe, "-v", "error", "-show_entries",
                  "stream=codec_type,codec_name,width,height,r_frame_rate,"
                  "avg_frame_rate,nb_frames,duration:format=duration",
                  "-of", "json", str(path)], timeout=60, text=True)
        data = json.loads(r.stdout or "{}")
    except Exception as exc:
        log.warning("ffprobe failed for %s: %s", path, exc)
        return {}
    out: dict = {"has_audio": False}
    for st in data.get("streams") or []:
        if st.get("codec_type") == "audio":
            out["has_audio"] = True
        elif st.get("codec_type") == "video" and "width" not in out:
            out["codec"] = st.get("codec_name", "")
            out["width"] = int(st.get("width") or 0)
            out["height"] = int(st.get("height") or 0)
            # avg_frame_rate is the real rate for phone VFR files;
            # r_frame_rate can be a "timebase" like 600/1.
            for key in ("avg_frame_rate", "r_frame_rate"):
                num, _, den = (st.get(key) or "0/0").partition("/")
                try:
                    fps = float(num) / float(den or 1)
                except (ValueError, ZeroDivisionError):
                    fps = 0.0
                if 0 < fps < 400:
                    out["fps"] = round(fps, 3)
                    break
            if st.get("nb_frames"):
                out["frames"] = int(st["nb_frames"])
            if st.get("duration"):
                out["duration"] = float(st["duration"])
    fmt_dur = (data.get("format") or {}).get("duration")
    if fmt_dur and not out.get("duration"):
        out["duration"] = float(fmt_dur)
    if not out.get("frames") and out.get("duration") and out.get("fps"):
        out["frames"] = int(round(out["duration"] * out["fps"]))
    return out


def thumbnail(video, out_jpg, at: float = 1.0) -> Optional[Path]:
    """One frame, 480 px wide, as JPEG. None on failure."""
    exe = tools.ffmpeg()
    if not exe:
        return None
    out_jpg = Path(out_jpg)
    for t in (at, 0.0):                  # very short clips: try the start
        try:
            _run([exe, "-y", "-loglevel", "error", "-ss", f"{t:.3f}",
                  "-i", str(video), "-frames:v", "1",
                  "-vf", "scale=480:-2", "-q:v", "4", str(out_jpg)], timeout=60)
            if out_jpg.exists() and out_jpg.stat().st_size > 0:
                return out_jpg
        except Exception:
            pass
    return None


def extract_audio(path, rate: int = 8000,
                  max_secs: float = 900.0) -> Optional[np.ndarray]:
    """Mono float32 samples of a file's audio at `rate` Hz (ffmpeg does the
    decoding + resampling, so it works for any container / codec)."""
    exe = tools.ffmpeg()
    if not exe:
        return None
    try:
        r = _run([exe, "-v", "error", "-i", str(path), "-t", f"{max_secs:.0f}",
                  "-vn", "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"],
                 timeout=600)
        if r.returncode != 0 or not r.stdout:
            return None
        return np.frombuffer(r.stdout, dtype=np.float32).copy()
    except Exception as exc:
        log.warning("audio extract failed for %s: %s", path, exc)
        return None


def review_video(video, out_mp4, *, audio=None, audio_delay: float = 0.0,
                 log_cb=None) -> Optional[Path]:
    """Browser-safe review copy of `video`: H.264 High / yuv420p, at most
    1920 wide, +faststart, AAC.

    `audio`: a WAV to use INSTEAD of the video's own sound (the clean mic),
    placed `audio_delay` seconds after the video's start (negative = the mic
    started earlier, so its head is trimmed). Without `audio`, the video's
    own sound is kept (if any). Returns the path, or None on failure.
    """
    _cb = log_cb or (lambda s: None)
    exe = tools.ffmpeg()
    if not exe:
        _cb("⚠ ffmpeg not found — no review video")
        return None
    out_mp4 = Path(out_mp4)
    tmp = out_mp4.with_name(out_mp4.stem + ".part.mp4")
    cmd = [exe, "-y", "-loglevel", "error", "-i", str(video)]
    if audio is not None:
        if audio_delay < -0.0005:
            cmd += ["-ss", f"{-audio_delay:.6f}"]   # trim the mic's head
        cmd += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0"]
        if audio_delay > 0.0005:                     # pad real silence
            cmd += ["-af", f"adelay={int(round(audio_delay * 1000))}:all=1"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    cmd += ["-vf", "scale='min(1920,iw)':-2:flags=lanczos,format=yuv420p",
            "-c:v", "libx264", "-profile:v", "high", "-preset", "veryfast",
            "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2",
            # the VIDEO defines the length: never -shortest (it cut video)
            "-movflags", "+faststart", str(tmp)]
    try:
        r = _run(cmd, timeout=3600)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1024:
            tmp.replace(out_mp4)
            return out_mp4
        _cb("⚠ review video failed: "
            + (r.stderr or b"").decode(errors="ignore")[-300:])
    except Exception as exc:
        _cb(f"⚠ review video failed: {exc}")
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return None
