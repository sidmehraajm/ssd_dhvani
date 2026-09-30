"""Import a facial recording (a video, or a .zip containing one) into a take.

Layout inside the take folder:
    Take_03/
        6010_chrn_1010_mic_main_take3.wav
        facial/
            MySlate_3_iPhone.mov        <- the MAIN video (largest in a zip)
            MySlate_3_iPhone.csv        <- everything else from the zip, kept
            video_metadata.json
            .thumb.jpg                  <- hidden: Review-tab thumbnail only

Rules
-----
* The original is COPIED (never moved, never re-encoded) -- the published
  facial video is exactly what the camera wrote.
* Copies go to '<name>.part' first and are renamed when complete: a cancel
  or a full disk never leaves a half file that looks valid.
* Zips: junk (__MACOSX, .DS_Store, Thumbs.db) is skipped, and any entry that
  would land OUTSIDE the facial folder ("../x", absolute paths -- a
  'zip-slip') is refused.
* If the take has a mic track and the video has sound, the mic is auto-synced
  to the video (sync.py) and the offset stored -- the review mp4 then plays
  the clean mic in sync.
"""
from __future__ import annotations

import logging
import os
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from . import media, sync
from .manifest import FacialEntry

log = logging.getLogger("ssd_dhvani.facial")

FACIAL_DIR = "facial"
_JUNK = {".ds_store", "thumbs.db", "desktop.ini"}
_CHUNK = 8 * 1024 * 1024


class Cancelled(Exception):
    pass


def is_importable(path) -> bool:
    ext = Path(path).suffix.lower()
    return ext in media.VIDEO_EXTS or ext in media.ZIP_EXTS


def _copy(src: Path, dst: Path, progress, stop) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    total = max(1, src.stat().st_size)
    done = 0
    try:
        with open(src, "rb") as fi, open(part, "wb") as fo:
            while True:
                if stop():
                    raise Cancelled()
                buf = fi.read(_CHUNK)
                if not buf:
                    break
                fo.write(buf)
                done += len(buf)
                progress(f"copying {src.name}", done / total)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dst)
    shutil.copystat(src, dst)


def _safe_member(name: str) -> Optional[PurePosixPath]:
    """Zip entry -> relative path, or None if it is junk or unsafe."""
    p = PurePosixPath(name.replace("\\", "/"))
    if not p.parts or name.endswith("/"):
        return None
    if p.is_absolute() or ".." in p.parts or ":" in p.parts[0]:
        return None                                 # zip-slip
    if p.parts[0] == "__MACOSX" or p.name.lower() in _JUNK \
            or p.name.startswith("._"):
        return None
    return p


def _extract(zpath: Path, dest: Path, progress, stop) -> list[Path]:
    out: list[Path] = []
    with zipfile.ZipFile(zpath) as z:
        members = [(m, _safe_member(m.filename)) for m in z.infolist()]
        skipped = [m.filename for m, rel in members
                   if rel is None and not m.is_dir()]
        members = [(m, rel) for m, rel in members if rel is not None]
        # a zip whose files all sit in ONE top folder: drop that folder level
        tops = {rel.parts[0] for _m, rel in members}
        strip = len(tops) == 1 and all(len(rel.parts) > 1 for _m, rel in members)
        total = max(1, sum(m.file_size for m, _r in members))
        done = 0
        for m, rel in members:
            if stop():
                raise Cancelled()
            parts = rel.parts[1:] if strip else rel.parts
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.with_name(target.name + ".part")
            try:
                with z.open(m) as fi, open(part, "wb") as fo:
                    while True:
                        if stop():
                            raise Cancelled()
                        buf = fi.read(_CHUNK)
                        if not buf:
                            break
                        fo.write(buf)
                        done += len(buf)
                        progress(f"unzipping {rel.name}", done / total)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            os.replace(part, target)
            out.append(target)
        if skipped:
            log.info("zip: skipped %s", skipped)
    return out


def import_facial(src, take_dir: Path, task_dir: Path, *,
                  mic_wav: Optional[Path] = None, mic_role: str = "",
                  progress: Callable = lambda msg, frac: None,
                  stop: Callable = lambda: False) -> FacialEntry:
    """Copy/unzip `src` into `take_dir/facial`, probe it, thumbnail it and
    auto-sync the mic. Raises ValueError with a plain message on bad input."""
    src = Path(src)
    if not src.exists():
        raise ValueError(f"not found: {src}")
    if not is_importable(src):
        raise ValueError(f"{src.name}: not a video or a .zip")
    final = Path(take_dir) / FACIAL_DIR
    # Import into a TEMP folder and swap only when it succeeded: replacing a
    # video must never lose the old one if the new import fails / is cancelled.
    dest = Path(take_dir) / (FACIAL_DIR + ".importing")
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    try:
        if src.suffix.lower() in media.ZIP_EXTS:
            try:
                files = _extract(src, dest, progress, stop)
            except zipfile.BadZipFile:
                raise ValueError(f"{src.name} is not a valid zip file")
        else:
            target = dest / src.name
            _copy(src, target, progress, stop)
            files = [target]
        if not any(f.suffix.lower() in media.VIDEO_EXTS for f in files):
            raise ValueError(f"{src.name}: no video file inside (looked for "
                             f"{', '.join(sorted(media.VIDEO_EXTS))})")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    # success: swap the new folder in (the old one is only ever OUR copy)
    if final.exists():
        shutil.rmtree(final)
    os.replace(dest, final)
    files = [final / f.relative_to(dest) for f in files]
    videos = [f for f in files if f.suffix.lower() in media.VIDEO_EXTS]
    # The main video: the largest (a calibration / preview clip is smaller).
    main = max(videos, key=lambda f: f.stat().st_size)

    progress("reading the video", 1.0)
    info = media.probe(main)
    rel = lambda p: str(Path(p).relative_to(task_dir)).replace("\\", "/")  # noqa: E731
    entry = FacialEntry(
        path=rel(main), files=[rel(f) for f in files], source=src.name,
        width=info.get("width", 0), height=info.get("height", 0),
        fps=info.get("fps", 0.0), frames=info.get("frames", 0),
        duration=round(info.get("duration", 0.0), 4),
        codec=info.get("codec", ""), has_audio=bool(info.get("has_audio")))
    thumb = media.thumbnail(main, final / ".thumb.jpg",
                            at=min(1.0, entry.duration / 2 or 0.0))
    if thumb:
        entry.thumb = rel(thumb)
    resync(entry, task_dir, mic_wav, mic_role, progress)
    return entry


def resync(entry: FacialEntry, task_dir: Path, mic_wav: Optional[Path],
           mic_role: str, progress: Callable = lambda m, f: None) -> None:
    """(Re)compute the mic <-> video sync and store it on `entry`."""
    entry.synced_to, entry.sync_offset, entry.sync_confidence = "", 0.0, 0.0
    if mic_wav is None or not Path(mic_wav).exists():
        entry.sync_note = ("no mic track on this take — the video's own sound "
                           "is used" if entry.has_audio else
                           "no mic track and no sound in the video — the "
                           "review video is silent")
        return
    if not entry.has_audio:
        entry.sync_note = ("the video has no sound to sync with — the mic "
                           "is lined up at the start")
        return
    progress("syncing the mic to the video", 1.0)
    res = sync.sync_video_to_mic(Path(task_dir) / entry.path, mic_wav)
    entry.sync_confidence = res.confidence
    if res.ok:
        entry.synced_to = mic_role
        entry.sync_offset = round(res.offset, 5)
        entry.sync_note = ""
    else:
        entry.sync_note = (f"auto-sync failed ({res.reason}) — the review "
                           "keeps the video's own sound")
