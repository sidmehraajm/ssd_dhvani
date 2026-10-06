"""manifest.json read/write — one authoritative file per Task.

Shape:
{
  "project": ..., "sequence": ..., "scene": ..., "shot": ..., "task": ...,
  "takes": [ { "take": "Take_01", "status": "approve", "comments": "...",
              "created_at": ISO8601, "samplerate": 48000,
              "sample_format": "PCM_24", "file_format": "WAV",
              "device": "Microphone (Wireless Mic Rx)",
              "host_api": "Windows WASAPI", "duration": 12.4,
              "tracks": { "mic_main": {"path": "...", "channel": 0, ...} } } ]
}
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

MANIFEST_NAME = "manifest.json"
# Ordered worst -> best. Everything except "reject" is publishable.
VALID_STATUS = ("reject", "view", "okay", "approve")
#: Suggested track roles -- storage keys and file naming
#: ({shot}_{role}_take{N}.wav). Free text is allowed; roles must be unique.
TRACK_ROLES = ("mic_main", "mic_2", "lav", "boom", "room", "line")


@dataclass
class TrackEntry:
    """One recorded mono file of a take."""
    path: str                   # relative to the task folder
    channel: int = 0            # device channel it came from (0-based)
    source: str = "live"        # "live" | "recovered"
    offset: float = 0.0         # seconds from take t=0 to the first sample
    duration: float = 0.0
    peak_db: float = -120.0
    clipped: int = 0            # samples at full scale


@dataclass
class FacialEntry:
    """The facial-recording video (and anything that came with it in a zip)
    attached to a take. Paths are relative to the task folder."""
    path: str                        # the main video
    files: list = field(default_factory=list)   # every imported file
    source: str = ""                 # what was dropped (file / zip name)
    width: int = 0
    height: int = 0
    fps: float = 0.0
    frames: int = 0
    duration: float = 0.0
    codec: str = ""
    has_audio: bool = False
    thumb: str = ""                  # hidden JPEG for the Review tab
    # auto-sync of the clean mic to the video's scratch audio
    synced_to: str = ""              # track role it was synced with
    sync_offset: float = 0.0         # mic start, seconds after video start
    sync_confidence: float = 0.0
    sync_note: str = ""              # why it isn't synced, if it isn't
    review: str = ""                 # browser-safe review mp4 (built at publish)

    @property
    def synced(self) -> bool:
        return bool(self.synced_to)

    @property
    def label(self) -> str:
        bits = []
        if self.width:
            bits.append(f"{self.width}x{self.height}")
        if self.fps:
            bits.append(f"{self.fps:g} fps")
        return " · ".join(bits)


@dataclass
class TakeData:
    take: str                              # "Take_01"
    status: str = "view"                   # reject | view | okay | approve
    comments: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    samplerate: int = 48000
    sample_format: str = "PCM_24"          # PCM_16 | PCM_24 | FLOAT
    file_format: str = "WAV"               # WAV | FLAC | RF64
    device: str = ""
    host_api: str = ""
    duration: float = 0.0
    fps_display: int = 24                  # timecode rate (for editorial)
    tracks: dict[str, TrackEntry] = field(default_factory=dict)
    #: Facial-recording video dropped onto this take (optional).
    facial: "FacialEntry | None" = None
    #: AYON version this take was published as (0 = not yet published).
    #: Once set, the take is LOCKED: it is never re-published — only new
    #: takes create new versions.
    published_version: int = 0

    @property
    def number(self) -> int:
        try:
            return int(self.take.split("_")[-1])
        except (ValueError, IndexError):
            return 0

    @property
    def format_label(self) -> str:
        """'48 kHz · 24-bit · WAV' for logs and tiles."""
        bits = {"PCM_16": "16-bit", "PCM_24": "24-bit",
                "FLOAT": "32-bit float"}.get(self.sample_format,
                                             self.sample_format)
        return f"{self.samplerate / 1000:g} kHz · {bits} · {self.file_format}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tracks"] = {k: asdict(v) for k, v in self.tracks.items()}
        d["facial"] = asdict(self.facial) if self.facial else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TakeData":
        tfields = TrackEntry.__dataclass_fields__
        tracks = {k: TrackEntry(**{a: b for a, b in v.items() if a in tfields})
                  for k, v in (d.get("tracks") or {}).items()}
        raw = d.get("facial")
        ffields = FacialEntry.__dataclass_fields__
        facial = (FacialEntry(**{a: b for a, b in raw.items() if a in ffields})
                  if isinstance(raw, dict) and raw.get("path") else None)
        known = {f for f in cls.__dataclass_fields__
                 if f not in ("tracks", "facial")}
        return cls(tracks=tracks, facial=facial,
                   **{k: v for k, v in d.items() if k in known})


@dataclass
class Manifest:
    project: str = ""
    sequence: str = ""
    scene: str = ""
    shot: str = ""
    task: str = ""
    takes: list[TakeData] = field(default_factory=list)
    #: Number the operator wants for the NEXT take (0 = automatic: last + 1).
    #: Lets a task start at Take 3, or fill a gap left by a deleted take.
    next_take: int = 0

    # ---- persistence ---------------------------------------------------- #
    @classmethod
    def load(cls, task_dir: Path) -> "Manifest":
        path = Path(task_dir) / MANIFEST_NAME
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        takes = [TakeData.from_dict(t) for t in data.get("takes", [])]
        meta = {k: data.get(k, "") for k in ("project", "sequence", "scene", "shot", "task")}
        try:
            nxt = int(data.get("next_take") or 0)
        except (TypeError, ValueError):
            nxt = 0
        return cls(takes=takes, next_take=nxt, **meta)

    def save(self, task_dir: Path) -> Path:
        path = Path(task_dir)
        path.mkdir(parents=True, exist_ok=True)
        out = path / MANIFEST_NAME
        payload = {
            "project": self.project, "sequence": self.sequence, "scene": self.scene,
            "shot": self.shot, "task": self.task,
            "next_take": int(self.next_take or 0),
            "takes": [t.to_dict() for t in self.takes],
        }
        # Write to a temp file, then rename: a crash mid-write can never
        # leave a half-written (unreadable) manifest behind.
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(out)
        return out

    # ---- helpers -------------------------------------------------------- #
    def next_take_number(self) -> int:
        """The chosen number if set (moved up past numbers already used),
        else last take + 1."""
        used = {t.number for t in self.takes}
        n = (self.next_take if self.next_take > 0
             else max(used, default=0) + 1)
        while n in used:
            n += 1
        return n

    def next_take_name(self) -> str:
        return f"Take_{self.next_take_number():02d}"

    def find(self, take_name: str) -> TakeData | None:
        return next((t for t in self.takes if t.take == take_name), None)
