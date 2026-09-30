"""Where ffmpeg/ffprobe live.

ssd_dhvani records audio WITHOUT ffmpeg (sounddevice + soundfile do that).
ffmpeg is only used at publish time, to render the AYON reviewable: a small
H.264 video of the take's waveform with the sound on it, because AYON's web
viewer is a video player.

Resolution order:
    1. the configured path (config.ffmpeg_path)
    2. ffmpeg on PATH
    3. well-known install locations for THIS operating system
ffprobe is looked for next to the resolved ffmpeg first.

macOS note: apps started from Finder do NOT get your shell's PATH, so a
Homebrew ffmpeg (/opt/homebrew/bin) is invisible to `which` there -- that's
why the Homebrew folders are in the fallback list.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger("ssd_dhvani.tools")

#: Set once at startup from AppConfig (and whenever the user changes it).
_CONFIGURED: str = ""

_FALLBACKS = {
    "win32": (
        r"C:\blender\ffmpeg-8.1.1-essentials_build\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    ),
    "darwin": (
        "/opt/homebrew/bin/ffmpeg",      # Homebrew, Apple Silicon
        "/usr/local/bin/ffmpeg",         # Homebrew, Intel
        "/opt/local/bin/ffmpeg",         # MacPorts
    ),
}


def exe_name(tool: str) -> str:
    return f"{tool}.exe" if sys.platform == "win32" else tool


def set_ffmpeg(path: str | None) -> str:
    """Remember the operator's ffmpeg. Returns what will actually be used."""
    global _CONFIGURED
    _CONFIGURED = str(path or "").strip()
    resolved = ffmpeg()
    log.info("ffmpeg: configured=%r -> using %r", _CONFIGURED, resolved)
    return resolved


def ffmpeg() -> str:
    """Absolute path to ffmpeg, or "" when it cannot be found."""
    if _CONFIGURED:
        p = Path(_CONFIGURED).expanduser()
        if p.is_dir():                      # a folder was picked
            for name in ("ffmpeg.exe", "ffmpeg"):
                if (p / name).exists():
                    return str(p / name)
        elif p.exists():
            return str(p)
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in _FALLBACKS.get(sys.platform, ()):
        if os.path.exists(cand):
            return cand
    return ""


def ffprobe() -> str:
    """ffprobe next to the resolved ffmpeg, else PATH, else ""."""
    exe = ffmpeg()
    if exe:
        d = Path(exe).parent
        for name in ("ffprobe.exe", "ffprobe"):
            if (d / name).exists():
                return str(d / name)
    return shutil.which("ffprobe") or ""


def available() -> bool:
    return bool(ffmpeg())


def subprocess_flags() -> dict:
    """Keep ffmpeg from flashing a console window in the Windows exe."""
    if sys.platform == "win32":
        import subprocess
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def describe() -> str:
    """One line for the log / settings UI."""
    exe = ffmpeg()
    if not exe:
        return ("ffmpeg NOT found — recording works, but publishes get no "
                "AYON web-viewer preview. Set the path in the top bar.")
    src = "configured" if _CONFIGURED else "found automatically"
    probe = ffprobe()
    return f"ffmpeg: {exe} ({src})" + ("" if probe else "  [ffprobe missing]")
