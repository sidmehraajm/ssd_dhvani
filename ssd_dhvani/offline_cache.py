"""Offline copies of published versions ("make a task available offline").

Pick a project + task name while on the network, and the LATEST version of
every product published under that task -- on every shot -- is copied here.
Away from the network, the Reference panel plays straight from these copies.

Layout (under <save_root>/.ssd_dhvani/offline_cache/):

    <project>/<folder path>/<task>/<product>/v###/<files>
    index.json      what is cached, one entry per (project, shot, task, product)

Only the latest version of each product is kept: when a newer one is synced,
the older local copy is deleted. Only playable media (video / images / audio) is
copied -- see PLAYABLE_EXTS. All index writes are atomic (temp file + rename)
so an interrupted sync never corrupts the index.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

log = logging.getLogger("ssd_dhvani.offline")

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".mxf", ".m4v"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".exr", ".tif", ".tiff", ".dpx"}
AUDIO_EXTS = {".wav", ".flac", ".mp3", ".aif", ".aiff", ".m4a", ".ogg"}
PLAYABLE_EXTS = VIDEO_EXTS | IMAGE_EXTS | AUDIO_EXTS

_SAFE = re.compile(r"[^A-Za-z0-9_.\-]+")


def _safe(part: str) -> str:
    return _SAFE.sub("_", str(part)).strip("._") or "_"


def entry_key(project: str, folder_id: str, task: str, product: str) -> str:
    return f"{project}|{folder_id}|{task.lower()}|{product}"


class OfflineCache:
    """Index + file layout. Thread-safe: the download worker writes while the
    UI reads."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        self._index: dict = {"entries": {}, "tasks": {}}
        self._load()

    # ---- paths ---------------------------------------------------------- #
    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def version_dir(self, project: str, folder_path: str, task: str,
                    product: str, version: int) -> Path:
        parts = [_safe(p) for p in folder_path.strip("/").split("/") if p]
        return self.root.joinpath(_safe(project), *parts, _safe(task),
                                  _safe(product), f"v{int(version):03d}")

    # ---- persistence ------------------------------------------------------ #
    def _load(self) -> None:
        try:
            if self.index_path.exists():
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._index["entries"] = dict(data.get("entries") or {})
                    self._index["tasks"] = dict(data.get("tasks") or {})
        except Exception as exc:
            log.warning("offline index unreadable (%s) — starting empty", exc)

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._index, indent=2), encoding="utf-8")
        os.replace(tmp, self.index_path)      # atomic: never a half-written index

    # ---- writes ------------------------------------------------------------ #
    def put(self, entry: dict) -> None:
        """Record a completed version copy; drops the older local version."""
        key = entry_key(entry["project"], entry["folder_id"], entry["task"],
                        entry["product"])
        with self._lock:
            old = self._index["entries"].get(key)
            self._index["entries"][key] = entry
            self._save()
        if old and old.get("dir") and old.get("dir") != entry.get("dir"):
            self._rmtree(Path(old["dir"]))

    def mark_task(self, project: str, task: str, stats: dict) -> None:
        with self._lock:
            self._index["tasks"][f"{project}|{task.lower()}"] = {
                "project": project, "task": task,
                "synced_at": time.strftime("%Y-%m-%d %H:%M"), **stats}
            self._save()

    def remove_task(self, project: str, task: str) -> int:
        """Delete every local copy of `task` in `project`. Returns count."""
        n = 0
        with self._lock:
            for key, e in list(self._index["entries"].items()):
                if e["project"] == project and e["task"].lower() == task.lower():
                    self._rmtree(Path(e.get("dir", "")))
                    del self._index["entries"][key]
                    n += 1
            self._index["tasks"].pop(f"{project}|{task.lower()}", None)
            self._save()
        return n

    def _rmtree(self, d: Path) -> None:
        # Only ever delete inside the cache root.
        try:
            d = d.resolve()
            if self.root.resolve() in d.parents and d.exists():
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

    # ---- reads -------------------------------------------------------------- #
    def has(self, project: str, folder_id: str, task: str, product: str,
            version_id: str) -> bool:
        e = self._index["entries"].get(entry_key(project, folder_id, task, product))
        return bool(e and e.get("version_id") == version_id
                    and self.playable(e) is not None)

    def entries_for(self, project: str, folder_id: str) -> list[dict]:
        with self._lock:
            return sorted(
                (e for e in self._index["entries"].values()
                 if e["project"] == project and e["folder_id"] == folder_id),
                key=lambda e: (e["task"].lower(), e["product"]))

    def by_version_id(self, version_id: str) -> dict | None:
        with self._lock:
            for e in self._index["entries"].values():
                if e.get("version_id") == version_id:
                    return e
        return None

    def tasks(self) -> list[dict]:
        with self._lock:
            return sorted(self._index["tasks"].values(),
                          key=lambda t: (t["project"], t["task"].lower()))

    def playable(self, entry: dict) -> Path | None:
        """Best local file to open for an entry (video, then audio)."""
        d = Path(entry.get("dir", ""))
        files = [d / f for f in entry.get("files", [])]
        existing = [f for f in files if f.exists()]
        for exts in (VIDEO_EXTS, AUDIO_EXTS):
            for f in existing:
                if f.suffix.lower() in exts:
                    return f
        return existing[0] if existing else None

    def size_bytes(self) -> int:
        total = 0
        for p in self.root.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except Exception:
                pass
        return total
