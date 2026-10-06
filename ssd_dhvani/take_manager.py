"""Take lifecycle & business rules.

Rules enforced here:
  * A new Record pass ALWAYS creates a new take -- existing takes are never
    overwritten (`new_take`).
  * Every take has exactly one status: reject | view | okay | approve, plus
    comments.
  * Publish only sends view/okay/approve downstream (publish.publish_takes).

Folder layout:
    {root}/{Project}/{Sequence}/{Scene}/{Shot}/{Task}/{Take}/{shot}_{role}_take{N}.wav
    manifest.json  <- one per Task
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .config import AppConfig
from .manifest import Manifest, TakeData, TrackEntry, VALID_STATUS
from . import publish as publish_mod

AUDIO_EXTS = {".wav", ".flac"}


def _slug(name: str) -> str:
    """Role label -> filesystem-safe key: 'Mic Main' -> 'mic_main'."""
    s = re.sub(r"[^a-z0-9]+", "_", name.strip().lower())
    return s.strip("_") or "track"


class TakeManager:
    """Owns the manifest for one Task context and mediates all take mutations."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.manifest = Manifest()
        self.task_dir: Path | None = None

    # ------------------------------------------------------------------ #
    # Context
    # ------------------------------------------------------------------ #
    def set_context(self, project: str, sequence: str, scene: str,
                    shot: str, task: str) -> Manifest:
        """Point the manager at a Project/.../Task and load (or start) its manifest."""
        # Skip empty hierarchy levels -- offline mode records under
        # {root}/offline/{shot}/{task} with no sequence/scene level.
        parts = [p for p in (project, sequence, scene, shot, task) if p]
        self.task_dir = self.config.save_root_path.joinpath(*parts)
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = Manifest.load(self.task_dir)
        self.manifest.project = project
        self.manifest.sequence = sequence
        self.manifest.scene = scene
        self.manifest.shot = shot
        self.manifest.task = task
        return self.manifest

    def _require_ctx(self) -> Path:
        if self.task_dir is None:
            raise RuntimeError("No task context set -- call set_context() first.")
        return self.task_dir

    def save(self) -> None:
        self.manifest.save(self._require_ctx())

    # ------------------------------------------------------------------ #
    # Creating takes (never overwrites)
    # ------------------------------------------------------------------ #
    def new_take(self, comments: str = "", status: str = "view") -> TakeData:
        take_name = self.manifest.next_take_name()
        # A leftover Take_NN folder that is NOT in the manifest (copied in by
        # hand, old crash) is never reused -- its files would mix in.
        n = int(take_name.split("_")[-1])
        while (self._require_ctx() / take_name).exists():
            n += 1
            self.manifest.next_take = n
            take_name = self.manifest.next_take_name()
            n = int(take_name.split("_")[-1])
        self.manifest.next_take = 0      # chosen number used -> back to auto
        take = TakeData(
            take=take_name,
            status=status if status in VALID_STATUS else "view",
            comments=comments,
            samplerate=int(self.config.samplerate),
            sample_format=self.config.sample_format,
            file_format=self.config.file_format,
            device=self.config.audio_device,
            host_api=self.config.host_api,
            fps_display=int(self.config.fps_display),
        )
        self.manifest.takes.append(take)
        self.manifest.takes.sort(key=lambda t: t.number)   # Take 3 before 4
        (self._require_ctx() / take_name).mkdir(parents=True, exist_ok=True)
        self.save()
        return take

    def delete_take(self, take: TakeData) -> Path:
        """Remove a take from the task. Its folder is MOVED to
        {task}/_deleted/Take_NN_<time> -- not erased -- so a mis-click can be
        undone by moving it back (and re-adding it is a manual job).
        Published (locked) takes are refused: AYON already points at them."""
        if getattr(take, "published_version", 0):
            raise ValueError(f"{take.take} is published as "
                             f"v{take.published_version:03d} — it can't be deleted")
        root = self._require_ctx()
        src = root / take.take
        dest = None
        if src.exists():
            from datetime import datetime
            bin_dir = root / "_deleted"
            bin_dir.mkdir(exist_ok=True)
            dest = bin_dir / f"{take.take}_{datetime.now():%Y%m%d-%H%M%S}"
            shutil.move(str(src), str(dest))
        self.manifest.takes = [t for t in self.manifest.takes
                               if t.take != take.take]
        self.save()
        return dest

    def track_target_path(self, take: TakeData, role: str,
                          ext: str = ".wav") -> Path:
        """Naming convention: {shot}_{role}_take{N}.wav inside the take
        folder, e.g. Take_03/6010_chrn_1010_mic_main_take3.wav"""
        name = f"{_slug(self.manifest.shot) or 'shot'}_{_slug(role)}_take{take.number}"
        d = self._require_ctx() / take.take
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{name}{ext}"

    def register_track(self, take: TakeData, role: str, abs_path: Path, *,
                       channel: int = 0, source: str = "live",
                       offset: float = 0.0, duration: float = 0.0,
                       peak_db: float = -120.0, clipped: int = 0,
                       save: bool = True) -> None:
        rel = Path(abs_path).relative_to(self._require_ctx())
        take.tracks[_slug(role)] = TrackEntry(
            path=str(rel).replace("\\", "/"), channel=int(channel),
            source=source, offset=round(float(offset), 6),
            duration=round(float(duration), 4),
            peak_db=round(float(peak_db), 2) if peak_db > -1e9 else -120.0,
            clipped=int(clipped))
        take.duration = max([take.duration] +
                            [t.duration for t in take.tracks.values()])
        if save:
            self.save()

    def reconcile_take_files(self) -> list[str]:
        """Self-heal: register take audio files that exist ON DISK but are
        missing from the manifest (e.g. the app closed while finalizing).
        Returns human-readable lines describing what was recovered."""
        if self.task_dir is None:
            return []
        try:
            import soundfile as sf     # type: ignore
        except Exception:              # pragma: no cover
            return []
        healed: list[str] = []
        prefix = f"{_slug(self.manifest.shot) or 'shot'}_"
        for take in self.manifest.takes:
            tdir = self.task_dir / take.take
            if not tdir.is_dir():
                continue
            suffix = f"_take{take.number}"
            known = {Path(t.path).name for t in take.tracks.values()}
            for f in sorted(tdir.iterdir()):
                stem = f.stem
                if (f.suffix.lower() not in AUDIO_EXTS or f.name in known
                        or not (stem.startswith(prefix) and stem.endswith(suffix))):
                    continue
                role = stem[len(prefix):-len(suffix)].strip("_")
                if not role or _slug(role) in take.tracks:
                    continue
                try:
                    info = sf.info(str(f))
                except Exception:
                    continue       # unreadable / still being written
                if info.frames <= 0:
                    continue
                self.register_track(take, role, f, source="recovered",
                                    duration=info.duration, save=False)
                healed.append(f"{take.take}: recovered '{role}' ({f.name})")
        if healed:
            self.save()
        return healed

    # ------------------------------------------------------------------ #
    # Status & comments
    # ------------------------------------------------------------------ #
    def set_status(self, take: TakeData, status: str) -> None:
        if status not in VALID_STATUS:
            raise ValueError(f"status must be one of {VALID_STATUS}")
        take.status = status
        self.save()

    def set_comments(self, take: TakeData, comments: str) -> None:
        take.comments = comments
        self.save()

    # ------------------------------------------------------------------ #
    # Publish (view/okay/approve only -- reject excluded)
    # ------------------------------------------------------------------ #
    def publish(self, *, folder_id: str | None = None,
                task_id: str | None = None, author: str = ""):
        return publish_mod.publish_takes(
            self.manifest.takes,
            task_dir=self.task_dir,
            project=self.manifest.project,
            folder_id=folder_id,
            task_id=task_id,
            author=author,
        )

    def publishable_takes(self) -> list[TakeData]:
        return [t for t in self.manifest.takes if t.status in publish_mod.PUBLISHABLE]
