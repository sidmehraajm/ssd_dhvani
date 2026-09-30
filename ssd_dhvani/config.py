"""Externalized configuration for ssd_dhvani.

Precedence (highest first):
    1. Environment variables (AYON_SERVER_URL, SSD_SAVE_ROOT)
    2. config.json next to the project root (or path given by SSD_CONFIG)
    3. Built-in defaults below

Auth is per-user (username + password), same as the batch publisher.
The password is stored (base64, like the publisher's QSettings) ONLY when
'Remember this workstation' is ticked.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #
# When frozen by PyInstaller, __file__ lives in a temp unpack dir, so config.json
# must be resolved next to the executable instead. Otherwise use the project root.
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
    # macOS .app: the executable is ssd_dhvani_v1.app/Contents/MacOS/...;
    # keep config.json next to the .app bundle, where people can find it.
    if sys.platform == "darwin" and PROJECT_ROOT.parent.name == "Contents":
        PROJECT_ROOT = PROJECT_ROOT.parents[2]
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = Path(os.environ.get("SSD_CONFIG", PROJECT_ROOT / "config.json"))

# Default local save root -> the user's Music folder (exists on Windows and
# macOS alike, and keeps recordings out of the code repo).
DEFAULT_SAVE_ROOT = Path.home() / "Music" / "ssd_dhvani"


def _default_tracks() -> list:
    return [{"role": "mic_main", "channel": 0}]


@dataclass
class AppConfig:
    ayon_server_url: str = ""
    ayon_username: str = ""
    ayon_password_b64: str = ""      # only persisted when remember_workstation
    save_root: str = str(DEFAULT_SAVE_ROOT)
    remember_workstation: bool = False
    theme: str = "dark"              # "dark" | "light"
    # Count-in before a take: the input is already running, samples captured
    # during the count-in are discarded -- the take starts clean on "1".
    countdown_secs: int = 3
    last_reference_dir: str = ""     # folder of the last local reference file
    export_dir: str = ""             # 'Export copy' destination (Review tab)
    # ffmpeg is only needed for the AYON reviewable (waveform video). Empty =
    # look on PATH, then the usual install folders for this OS.
    ffmpeg_path: str = ""
    fps_display: int = 24            # timecode rate shown while recording

    # ---- audio input ----------------------------------------------------- #
    # Stored by NAME + host API, never by device index (indices change).
    host_api: str = ""               # "" = best for this OS (WASAPI / Core Audio)
    audio_device: str = ""           # "" = system default input
    samplerate: int = 48000          # Hz
    sample_format: str = "PCM_24"    # PCM_16 | PCM_24 | FLOAT
    file_format: str = "WAV"         # WAV | FLAC | RF64
    wasapi_exclusive: bool = False   # Windows only: bypass the Windows mixer
    latency: str = "high"            # bigger buffers = fewer dropouts
    # What to record: one mono file per track = role + device channel.
    audio_tracks: list = field(default_factory=_default_tracks)

    # ---- publish ----------------------------------------------------------- #
    # AYON product type. "auto" = per take: audio-only takes -> 'audio'
    # (Maya/Blender/Resolve audio loaders), takes with a facial video ->
    # 'render' in a '<name>_facial' product (Resolve's clip loaders). Any
    # other value (e.g. "audio") forces that type for every take.
    publish_product_type: str = "auto"

    # ---- password helpers ------------------------------------------------ #
    @property
    def ayon_password(self) -> str:
        if not self.ayon_password_b64:
            return ""
        try:
            return base64.b64decode(self.ayon_password_b64.encode()).decode()
        except Exception:
            return ""

    def set_password(self, password: str) -> None:
        self.ayon_password_b64 = (
            base64.b64encode(password.encode()).decode() if password else "")

    # ---- persistence ------------------------------------------------------ #
    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

        # Environment overrides win.
        cfg.ayon_server_url = os.environ.get("AYON_SERVER_URL", cfg.ayon_server_url)
        cfg.save_root = os.environ.get("SSD_SAVE_ROOT", cfg.save_root)
        if not cfg.audio_tracks:
            cfg.audio_tracks = _default_tracks()
        return cfg

    def save(self, path: Path | None = None) -> None:
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        payload = asdict(self)
        # Never persist the password unless the operator opted in.
        if not self.remember_workstation:
            payload["ayon_password_b64"] = ""
        try:
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass    # read-only install folder: keep running with in-memory settings

    @property
    def save_root_path(self) -> Path:
        return Path(self.save_root).expanduser()
