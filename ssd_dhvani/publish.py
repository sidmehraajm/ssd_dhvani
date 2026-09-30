"""AYON publish hook.

Publish policy: only `view`, `okay` and `approve` takes are ever sent
downstream. `reject` takes are excluded entirely.

What an audio take publishes as ONE AYON version:
  * one representation per track, named by ROLE (`mic_main`, `lav`, ...) --
    the untouched WAV/FLAC masters;
  * `ssd_take` -- the ssd_take.json sidecar (status, comments, format, ...);
  * a REVIEWABLE: AYON's web viewer is a video player, so the main track is
    rendered into a small H.264 video of its waveform with the sound on it.

This mirrors your vendored batch publisher (vendor/ayon_batch_folder_publisher.py,
a PyQt5 GUI). Its real AYON logic -- PublishWorker.run(): EntityHub -> product ->
version -> representations -> reviewable -- is ported HEADLESSLY into
`_live_publish_job()` here so it runs without a Qt event loop and never clashes with
this app's PySide6.

NO FAKE SUCCESS: if a take cannot actually be published (ayon-python-api missing,
not connected, no AYON folder selected, or no local files) `publish_one` returns
(False, reason). Nothing is ever reported as "published" unless the AYON calls ran.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

log = logging.getLogger("ssd_dhvani.publish")

if TYPE_CHECKING:
    from .take_manager import TakeData

from .ayon_lock import API_LOCK, LockedAPI

try:
    import ayon_api as _ayon_api_module  # type: ignore
    from ayon_api.entity_hub import EntityHub  # type: ignore
    # All ayon_api.* metadata calls are serialised on the shared connection;
    # media uploads are not (see ayon_lock.py). EntityHub calls, which use the
    # same connection internally, are wrapped in API_LOCK explicitly below.
    ayon_api = LockedAPI(_ayon_api_module)
    _HAVE_AYON = True
except Exception:  # pragma: no cover
    ayon_api = None  # type: ignore
    EntityHub = None  # type: ignore
    _HAVE_AYON = False

PUBLISHABLE = {"view", "okay", "approve"}
#: Audio masters a take folder may contain.
_AUDIO_MASTER_EXTS = {".wav", ".flac"}
#: Never published: half-written / temp files.
_SKIP_SUFFIXES = (".tmp", ".part")

#: Product type setting (config.publish_product_type):
#:   "auto" (default) -> chosen PER TAKE from what the take contains:
#:        audio only           -> 'audio'  (Maya/Blender/Resolve audio loaders)
#:        has a facial video   -> 'render' (Resolve's clip loaders; same as
#:                                          ssd_abhinaya)
#:   anything else ('audio', 'render', ...) -> forced for every take.
PRODUCT_TYPE = "auto"
AUDIO_TYPE = "audio"
VIDEO_TYPE = "render"
#: Product-name suffix per type. In AYON the type belongs to the PRODUCT, not
#: to a version -- one product can't be 'audio' for one take and 'render' for
#: the next. So each type gets its own product under the task.
_TYPE_SUFFIX = {AUDIO_TYPE: "", VIDEO_TYPE: "_facial"}


def set_product_type(product_type: str) -> None:
    """Called once at startup from the config."""
    global PRODUCT_TYPE
    pt = re.sub(r"[^a-zA-Z0-9_]", "", product_type or "")
    PRODUCT_TYPE = pt or "auto"


def product_for(take, base_name: str) -> tuple[str, str]:
    """(product type, product name) for one take.

    Audio-only takes keep the plain name ('6010_chrn_1010_sound'); takes
    with a facial video go to '<name>_facial' as 'render'. A forced type
    (config) uses the plain name for everything, like before."""
    if PRODUCT_TYPE != "auto":
        return PRODUCT_TYPE, base_name
    has_video = getattr(take, "facial", None) is not None
    ptype = VIDEO_TYPE if has_video else AUDIO_TYPE
    return ptype, base_name + _TYPE_SUFFIX[ptype]

#: Name of the per-take sidecar published alongside the media. It carries the
#: review state so ANY machine -- a review PC that never saw the recording --
#: can read status/version/comments straight from AYON.
SIDECAR_NAME = "ssd_take.json"
SIDECAR_REPRE = "ssd_take"
SIDECAR_VERSION = 1


def build_sidecar(take, *, project: str, folder_path: str, task: str,
                  version: int, author: str, take_dir) -> dict:
    """Everything a review machine needs about one take."""
    tracks = {}
    for key, tr in (getattr(take, "tracks", {}) or {}).items():
        entry = {"path": tr.path, "channel": tr.channel,
                 "source": getattr(tr, "source", ""),
                 "offset": tr.offset, "duration": tr.duration,
                 "peak_db": tr.peak_db, "clipped": tr.clipped}
        try:
            f = Path(take_dir) / Path(tr.path).name
            if f.exists():
                entry["size"] = f.stat().st_size
        except Exception:
            pass
        tracks[key] = entry
    from . import __version__ as _app_version
    return {
        "schema": f"ssd_dhvani.take/{SIDECAR_VERSION}",
        "app_version": _app_version,
        "project": project,
        "folder": folder_path,
        "task": task,
        "take": take.take,
        "take_number": getattr(take, "number", 0),
        "status": getattr(take, "status", "view"),
        "comments": getattr(take, "comments", "") or "",
        "version": int(version),
        "author": author,
        "published_at": datetime.now(timezone.utc)
                        .isoformat(timespec="seconds"),
        "samplerate": getattr(take, "samplerate", 0),
        "sample_format": getattr(take, "sample_format", ""),
        "file_format": getattr(take, "file_format", ""),
        "device": getattr(take, "device", ""),
        "host_api": getattr(take, "host_api", ""),
        "duration": getattr(take, "duration", 0.0),
        "fps_display": getattr(take, "fps_display", 0),
        "tracks": tracks,
        "facial": ({k: getattr(take.facial, k) for k in (
            "path", "files", "source", "width", "height", "fps", "frames",
            "duration", "has_audio", "synced_to", "sync_offset",
            "sync_confidence", "review")}
            if getattr(take, "facial", None) else None),
    }


class AlreadyPublished(Exception):
    """This exact take (matched by version source path) is already on AYON."""

    def __init__(self, version: int):
        super().__init__(f"already published as v{version:03d}")
        self.version = version


@dataclass
class PublishJob:
    """Matches vendor PublishWorker.__init__ arguments."""
    local_path: str
    project: str
    folder_id: Optional[str]
    task_id: Optional[str]
    author_name: str = ""
    variant: str = "main"
    comment: str = ""
    review_path: Optional[str] = None
    take_name: str = ""
    take: object = None          # TakeData, for the published sidecar


@dataclass
class PublishResult:
    published: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    ok: bool = True
    message: str = ""


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def publishable(take_list: "list[TakeData]") -> "list[TakeData]":
    return [t for t in take_list if getattr(t, "status", "view") in PUBLISHABLE]


def publish_one(
    take: "TakeData",
    *,
    task_dir: Optional[Path],
    project: str,
    folder_id: Optional[str],
    task_id: Optional[str],
    author: str = "",
    log_cb=None,
) -> tuple[bool, str]:
    """Publish a single take to AYON for real. Returns (ok, message).

    Returns (False, reason) -- never a fake success -- when it cannot publish.
    """
    if getattr(take, "status", "view") not in PUBLISHABLE:
        return False, f"{take.take}: status is {take.status} (only View/Okay/Approve publish)"
    if getattr(take, "published_version", 0):
        return False, (f"{take.take}: already published as "
                       f"v{take.published_version:03d} — locked, skipped")
    if not _HAVE_AYON:
        return False, f"{take.take}: ayon-python-api not installed"
    if not project or not folder_id:
        return False, f"{take.take}: no AYON project/shot selected"

    job = _build_job(take, task_dir, project, folder_id, task_id, author)
    if not os.path.isdir(job.local_path) and not os.path.isfile(job.local_path):
        return False, f"{take.take}: no recorded files on disk to publish"
    try:
        ver, msg = _live_publish_job(job, log_cb=log_cb)
        take.published_version = ver          # LOCK — never re-published
        return True, msg
    except AlreadyPublished as ap:
        take.published_version = ap.version   # backfill lock from the server
        return False, (f"{take.take}: found on AYON as v{ap.version:03d} "
                       "(matched by source path) — locked, skipped")
    except Exception:
        log.exception("Publish failed for %s", take.take)
        # Full traceback so the UI log shows the REAL error, like the
        # batch publisher's finished_signal does.
        return False, f"{take.take} failed:\n{traceback.format_exc()}"


def publish_takes(
    take_list: "list[TakeData]",
    *,
    task_dir: Optional[Path] = None,
    project: str = "",
    folder_id: Optional[str] = None,
    task_id: Optional[str] = None,
    author: str = "",
) -> PublishResult:
    """Publish all eligible takes (view/okay/approve). Honest -- no dry-run success."""
    result = PublishResult()
    for take in take_list:
        if getattr(take, "status", "view") not in PUBLISHABLE:
            result.skipped.append(f"{take.take} (status={take.status}) — excluded")
            continue
        ok, msg = publish_one(take, task_dir=task_dir, project=project,
                              folder_id=folder_id, task_id=task_id, author=author)
        if ok:
            result.published.append(take.take)
        else:
            result.ok = False
            result.skipped.append(msg)
    result.message = _summarize(result)
    log.info(result.message)
    return result


# --------------------------------------------------------------------------- #
# Job construction
# --------------------------------------------------------------------------- #
def _build_job(take: "TakeData", task_dir: Optional[Path], project: str,
               folder_id: Optional[str], task_id: Optional[str],
               author: str) -> PublishJob:
    local = str(Path(task_dir) / take.take) if task_dir else take.take
    # AYON version comment: the review status we picked, then the operator's
    # comment — e.g. "[Approve] great energy on this one".
    status = getattr(take, "status", "view").title()
    comment = f"[{status}] {(take.comments or '').strip()}".strip()
    return PublishJob(
        local_path=local, project=project, folder_id=folder_id, task_id=task_id,
        author_name=author, variant="main", comment=comment,
        review_path=_pick_review(take, task_dir), take_name=take.take,
        take=take,
    )


def make_review_proxy(src: str, log_cb=None) -> Optional[str]:
    """A video a WEB BROWSER can play, made from an audio file -- for AYON's
    viewer, which plays video only.

    The picture is the waveform (ffmpeg's `showwaves` filter draws it as the
    sound plays), H.264 / yuv420p / 25 fps; the sound is AAC 192k. Returns
    the temp path, or None if ffmpeg is unavailable or fails.
    """
    import subprocess
    import tempfile
    from . import tools
    _cb = log_cb or (lambda s: None)
    exe = tools.ffmpeg()
    if not exe or not src or not os.path.isfile(src):
        return None
    out = Path(tempfile.gettempdir()) / "ssd_dhvani_review" / (
        f"{uuid.uuid4().hex[:8]}_{Path(src).stem}.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    graph = ("[0:a]aformat=channel_layouts=mono,"
             "showwaves=s=1280x360:mode=cline:scale=sqrt:draw=full:rate=25:colors=#e0952f|#e0952f,"
             "format=yuv420p[v]")
    cmd = [exe, "-y", "-loglevel", "error", "-i", str(src),
           "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
           "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-ac", "2",
           "-movflags", "+faststart", str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=1800,
                           **tools.subprocess_flags())
        if r.returncode == 0 and out.exists() and out.stat().st_size > 1024:
            return str(out)
        _cb("⚠ could not render the waveform review video: "
            + (r.stderr or b"").decode(errors="ignore")[-300:])
    except Exception as exc:
        _cb(f"⚠ could not render the waveform review video: {exc}")
    return None


def _report_reviewables(project: str, vid: str, _cb) -> None:
    """Ask AYON what it did with the upload and SAY so in the app log."""
    try:
        res = ayon_api.get(f"projects/{project}/versions/{vid}/reviewables")
        items = (getattr(res, "data", None) or {}).get("reviewables") or []
    except Exception as exc:
        _cb(f"⚠ uploaded, but could not read back the reviewables: {exc}")
        return
    if not items:
        _cb("✕ AYON lists NO reviewable for this version after upload")
        return
    for it in items:
        avail = it.get("availability") or "unknown"
        name = it.get("filename") or it.get("fileId", "?")
        mark = "✓" if avail in ("ready", "unknown") else "⚠"
        _cb(f"{mark} AYON reviewable: {name} — availability: {avail}"
            + ("" if avail in ("ready", "unknown") else
               " (the server must convert it before the web viewer can play "
               "it — needs AYON's transcoding service)"))


def _pick_review(take: "TakeData", task_dir: Optional[Path]) -> Optional[str]:
    """The FIRST track (the main mic, top strip) is the AYON reviewable."""
    tracks = getattr(take, "tracks", {}) or {}
    if not tracks or task_dir is None:
        return None
    key = "mic_main" if "mic_main" in tracks else next(iter(tracks))
    p = Path(task_dir) / tracks[key].path
    return str(p) if p.suffix.lower() in _AUDIO_MASTER_EXTS else None


# --------------------------------------------------------------------------- #
# Representation building -- ported from the studio's batch publisher, which
# produces versions DaVinci can actually load. Three things matter and were
# all missing here: representation names are the file EXTENSION, paths are
# rootless ({root[publish]}/…) with a template, and each representation
# carries a `data.context` block. Without the context, ayon-core cannot build
# a load context, so "Load media" / "Load as clip" never appear in the menu.
# --------------------------------------------------------------------------- #
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".exr", ".tif", ".tiff",
              ".dpx", ".hdr", ".gif", ".tga", ".bmp"}
AUDIO_EXTS = {".wav", ".aif", ".aiff", ".mp3", ".flac", ".aac", ".m4a", ".ac3"}
SEQUENCE_RE = re.compile(r"^(?P<prefix>.*?)(?P<frame>\d+)$")


def source_file_hash(path: str) -> str:
    """ayon-core 'op3' style source hash (name|size|mtime)."""
    try:
        return "|".join([
            os.path.basename(path),
            str(os.path.getsize(path)),
            str(int(os.path.getmtime(path))),
        ]).replace(".", ",")
    except Exception:
        return ""


def audio_duration(path: str) -> float:
    """Seconds of audio in a WAV/FLAC (soundfile; no ffprobe needed)."""
    try:
        import soundfile as sf   # type: ignore
        return float(sf.info(str(path)).duration)
    except Exception:
        return 0.0


def _rootless(path: str, root_name: str, root_value: str) -> str:
    """Absolute path -> '{root[<name>]}/…' template path."""
    p = str(path).replace("\\", "/")
    rv = str(root_value).replace("\\", "/").rstrip("/")
    if rv and p.lower().startswith(rv.lower()):
        return "{root[%s]}%s" % (root_name, p[len(rv):])
    return p


def role_repre_name(rel: str, fname: str, ext: str, shot: str = "") -> str:
    """Stable, human-readable representation name: the track ROLE.

    '6010_chrn_1010_mic_main_take3.wav' -> 'mic_main'
    'ssd_take.json' -> 'ssd_take'
    The shot prefix and take number are stripped so the SAME name exists on
    every version -- which is what AYON's update-to-latest matches on. Falls
    back to the extension when nothing sensible is left.
    """
    stem = os.path.splitext(fname)[0]
    if shot and stem.startswith(f"{shot}_"):
        stem = stem[len(shot) + 1:]
    stem = re.sub(r"_take\d+", "", stem)
    name = re.sub(r"[^a-zA-Z0-9_]", "_", stem).strip("_")
    return name or (ext or "file")


def repre_name(rel: str, fname: str, ext: str, shot: str = "",
               facial_main: str = "") -> str:
    """Representation names that are the SAME on every version:
    track masters -> their role ('mic_main'), the facial video -> 'facial',
    the review mp4 -> 'review', other files from a facial zip ->
    'facial_<ext>' (e.g. 'facial_csv'), the sidecar -> 'ssd_take'."""
    parts = Path(rel.replace("\\", "/")).parts
    if parts and parts[0] == "facial":
        if fname == facial_main:
            return "facial"
        return f"facial_{ext or 'file'}"
    return role_repre_name(rel, fname, ext, shot=shot)


def build_facial_review(take, take_dir: Path, shot: str,
                        log_cb=None) -> Optional[Path]:
    """The take's review mp4: the facial video, browser-safe, with the
    CLEAN mic as its sound when possible.

    * mic auto-synced -> the mic, shifted by the measured offset
    * video has no sound but the take has a mic -> the mic from the start
      (best guess; said in the log)
    * otherwise -> the video's own sound
    Saved as Take_NN/{shot}_review_take{N}.mp4 and recorded on the take."""
    from . import media
    _cb = log_cb or (lambda s: None)
    fac = take.facial
    task_dir = Path(take_dir).parent
    video = task_dir / fac.path
    if not video.exists():
        _cb(f"⚠ facial video missing on disk: {video}")
        return None
    audio, delay = None, 0.0
    how = "the video's own sound" if fac.has_audio else "no sound"
    tracks = getattr(take, "tracks", {}) or {}
    if fac.synced and fac.synced_to in tracks:
        audio = task_dir / tracks[fac.synced_to].path
        delay, how = fac.sync_offset, (f"{fac.synced_to}, synced "
                                        f"{fac.sync_offset:+.3f} s")
    elif tracks and not fac.has_audio:
        role = "mic_main" if "mic_main" in tracks else next(iter(tracks))
        audio = task_dir / tracks[role].path
        how = f"{role}, lined up at the start (no sound in the video to sync)"
    if audio is not None and not audio.exists():
        audio, how = None, "the video's own sound (mic file missing)"
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", shot) or "shot"
    out = Path(take_dir) / f"{safe}_review_take{take.number}.mp4"
    _cb(f"{take.take}: rendering the review video ({how})…")
    made = media.review_video(video, out, audio=audio, audio_delay=delay,
                              log_cb=_cb)
    if made is not None:
        fac.review = str(made.relative_to(task_dir)).replace("\\", "/")
        _cb(f"{take.take}: review video ready ({made.stat().st_size / 1e6:.1f} MB)")
    return made


def collect_representations(files: list, name_fn=None) -> list:
    """Group (src, rel, fname) items into representation payloads.

    `name_fn(rel, fname, ext)` supplies the representation name; by default
    it is the EXTENSION, exactly like the studio's batch publisher. Loaders
    filter on the product type and the representation's `ext`/context, not on
    this name, so a friendlier name (the camera role) is safe and makes the
    loader menu readable. Consecutive numbered images of one extension become
    a single sequence representation.
    """
    groups: dict = {}
    for item in files:
        ext = os.path.splitext(item[2])[1].lstrip(".").lower()
        groups.setdefault(ext, []).append(item)

    repres: list = []
    used_names: set = set()

    def unique(base: str) -> str:
        base = base or "file"
        n, i = base, 1
        while n in used_names:
            n = f"{base}_{i}"
            i += 1
        used_names.add(n)
        return n

    for ext, items in sorted(groups.items()):
        is_image = ("." + ext) in IMAGE_EXTS
        seq_groups: dict = {}
        singles: list = []
        if is_image and len(items) > 1:
            for src, rel, fname in items:
                stem = os.path.splitext(fname)[0]
                m = SEQUENCE_RE.match(stem)
                if m and m.group("frame"):
                    key = (os.path.dirname(rel), m.group("prefix"),
                           len(m.group("frame")))
                    seq_groups.setdefault(key, []).append(
                        (int(m.group("frame")), src, rel, fname))
                else:
                    singles.append((src, rel, fname))
            for key in list(seq_groups):
                if len(seq_groups[key]) < 2:
                    _, src, rel, fname = seq_groups.pop(key)[0]
                    singles.append((src, rel, fname))
        else:
            singles = list(items)

        for (dirname, prefix, padding), seq in seq_groups.items():
            seq.sort()
            repres.append({
                "name": unique(ext), "ext": ext,
                "items": [(s, r, f) for _, s, r, f in seq],
                "frame_start": seq[0][0], "frame_end": seq[-1][0],
                "sequence": {"dirname": dirname, "prefix": prefix,
                             "padding": padding},
            })
        for src, rel, fname in sorted(singles, key=lambda it: it[1]):
            base = name_fn(rel, fname, ext) if name_fn else ext
            repres.append({
                "name": unique(base), "ext": ext,
                "items": [(src, rel, fname)],
                "frame_start": None, "frame_end": None, "sequence": None,
            })
    return repres


def _post_representation(project: str, vid: str, repre: dict, dest_base: str,
                         context_base: dict, root_name: str,
                         root_value: str) -> None:
    """Create one representation the way ayon-core's integrator does."""
    files_payload = []
    for _src, rel, fname in repre["items"]:
        dest = os.path.join(dest_base, rel)
        files_payload.append({
            "id": str(uuid.uuid4()),
            "name": fname,
            "path": _rootless(dest, root_name, root_value),
            "size": os.path.getsize(dest),
            "hash": source_file_hash(dest),
            "hash_type": "op3",
        })

    first_dest = os.path.join(
        dest_base, repre["items"][0][1]).replace("\\", "/")
    if repre["sequence"]:
        s = repre["sequence"]
        template = _rootless(dest_base, root_name, root_value)
        if s["dirname"]:
            template += "/" + s["dirname"].replace("\\", "/")
        template += "/%s{frame:0>%d}.%s" % (s["prefix"], s["padding"],
                                            repre["ext"])
    else:
        template = _rootless(os.path.join(dest_base, repre["items"][0][1]),
                             root_name, root_value)

    context = dict(context_base)
    context["representation"] = repre["name"]
    context["ext"] = repre["ext"]
    if repre["sequence"]:
        context["frame"] = str(repre["frame_start"]).zfill(
            repre["sequence"]["padding"])

    ayon_api.post(
        f"projects/{project}/representations",
        versionId=vid,
        name=repre["name"],
        attrib={"ext": repre["ext"], "path": first_dest, "template": template},
        data={"context": context},
        files=files_payload,
    )


# --------------------------------------------------------------------------- #
# Live publish -- Qt-free port of vendor PublishWorker.run()
# --------------------------------------------------------------------------- #
def _norm_src(p: str) -> str:
    """Path key for 'is this the same take folder?' checks."""
    return os.path.normcase(os.path.normpath(str(p))).replace("\\", "/").rstrip("/")


def _publish_root(project: str) -> tuple:
    """(root_name, root_value) this machine publishes into."""
    roots = ayon_api.get_project_roots_by_site_id(project) or {}
    if not roots:
        raise ValueError(f"No Roots defined for project '{project}'.")
    for key in ("publish", "work", "root"):
        if roots.get(key):
            return key, str(roots[key])
    name = list(roots)[0]
    return name, str(roots[name])


def _platform_key() -> str:
    """AYON's name for this OS in anatomy roots."""
    return {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")


def _copy_to_share(src: str, dest: str) -> None:
    """Copy data + timestamps. On SMB shares (especially from macOS) setting
    the file METADATA can fail with 'Operation not permitted' even though
    the data copied fine -- the data is what matters, so that is tolerated;
    a failure to copy the DATA still raises."""
    try:
        shutil.copy2(src, dest)
    except PermissionError:
        shutil.copyfile(src, dest)          # data only
        try:
            shutil.copystat(src, dest)
        except OSError:
            pass
    if os.path.getsize(dest) != os.path.getsize(src):
        raise OSError(f"copy to {dest} is incomplete")


def _server_publish_path(project: str, folder_id: str, product_type: str,
                         product_name: str, variant: str, version_str: str,
                         log_cb=None) -> str:
    _cb = log_cb or (lambda s: None)
    roots = ayon_api.get_project_roots_by_site_id(project)
    if not roots:
        raise ValueError(f"No Roots defined for project '{project}'.")
    _cb(f"AYON roots for this machine: {roots}")
    _rn, root_path = _publish_root(project)
    root_path = str(root_path)
    if not root_path.strip() or not os.path.isabs(root_path):
        raise ValueError(
            f"Publish root is empty or not absolute: {root_path!r} "
            f"(roots={roots}). Set this workstation's site roots in AYON "
            f"(project anatomy → roots → {_platform_key()}), or files would "
            "land in a wrong/relative location.")
    if not os.path.isdir(root_path):
        # The share isn't mounted / mapped. NEVER create it: on a Mac,
        # '/Volumes/<share>' would silently become a folder on the LOCAL disk
        # and the "published" files would exist nowhere anyone else can see.
        hint = ("Mount the studio share first (Finder → Go → Connect to "
                "Server) — it must appear exactly at this path."
                if sys.platform == "darwin" else
                "Map / connect the studio drive first.")
        raise ValueError(f"Publish root {root_path!r} is not reachable from "
                         f"this machine. {hint}")
    fe = ayon_api.get_folder_by_id(project, folder_id)
    _cb(f"AYON folder path: {fe['path']}")
    full = os.path.join(
        root_path.replace("\\", "/"), project, fe["path"].strip("/"),
        "publish", product_type, product_name, variant, version_str,
    )
    return os.path.normpath(full)


def _live_publish_job(job: PublishJob, log_cb=None) -> str:
    """Mirrors vendor/ayon_batch_folder_publisher PublishWorker.run()."""
    _cb = log_cb or (lambda s: None)
    project, folder_id = job.project, job.folder_id
    with API_LOCK:
        hub = EntityHub(project)
    fe = ayon_api.get_folder_by_id(project, folder_id)
    name = fe["name"]

    if job.task_id:
        te = ayon_api.get_task_by_id(project, job.task_id)
        raw = f"{name}_{te['name']}"
    else:
        raw = name

    # Type + product name follow what the take CONTAINS (see product_for):
    # audio-only -> 'audio', with a facial video -> 'render' in its own
    # product. ('generic' would be loadable by nothing -- abhinaya's lesson.)
    product_type, safe = product_for(
        job.take, re.sub(r"[^a-zA-Z0-9_.-]", "_", raw))
    _cb(f"{job.take_name}: product '{safe}' (type {product_type})")

    existing = list(ayon_api.get_products(
        project, folder_ids=[folder_id], product_names=[safe]))
    if existing:
        pid = existing[0]["id"]
        # Product type lives on the PRODUCT: if an older publish created it
        # with another type (e.g. 'generic'), loaders never list any of its
        # versions. Convert it so the whole history becomes loadable.
        old_type = existing[0].get("productType")
        if old_type and old_type != product_type and old_type != "generic":
            # Never flip a product between real types (audio <-> render):
            # every earlier version would change loaders under people's
            # feet. Keep what it is and say so.
            _cb(f"⚠ product '{safe}' already exists as '{old_type}' — "
                f"publishing into it as '{old_type}' (wanted {product_type})")
            product_type = old_type
        elif old_type and old_type != product_type:
            try:
                ayon_api.patch(f"projects/{project}/products/{pid}",
                               productType=product_type,
                               productBaseType=product_type)
                _cb(f"product '{safe}': type {old_type} → {product_type} "
                    "so the loaders accept it")
            except Exception as exc:
                _cb(f"⚠ could not convert product type ({exc}) — publishing "
                    f"as '{old_type}'; loaders may stay unavailable")
                product_type = old_type
        # Safety net: if ANY existing version was published from this exact
        # take folder (version attrib 'source'), the take is already on the
        # server — lock it instead of duplicating it as a new version.
        try:
            # Compare NORMALISED paths: the source attrib is stored with
            # forward slashes (version attribs patch below) while
            # job.local_path uses backslashes on Windows, so a raw == never
            # matched and this safety net silently did nothing.
            mine = _norm_src(job.local_path)
            for v in ayon_api.get_versions(project, product_ids=[pid]):
                src = (v.get("attrib") or {}).get("source") or ""
                if src and _norm_src(src) == mine:
                    raise AlreadyPublished(v["version"])
        except AlreadyPublished:
            raise
        except Exception:
            pass    # source check is best-effort only
        lv = ayon_api.get_last_version_by_product_id(project, pid)
        next_v = (lv["version"] + 1) if lv else 1
    else:
        with API_LOCK:
            np_ = hub.add_new_product(product_type=product_type, name=safe,
                                      folder_id=folder_id)
            hub.commit_changes()
        pid = np_["id"]
        next_v = 1

    ver_str = f"v{next_v:03d}"
    vkw = {"version": next_v, "product_id": pid}
    if job.task_id:
        vkw["task_id"] = job.task_id
    with API_LOCK:
        ve = hub.add_new_version(**vkw)
    if job.author_name:
        ve["author"] = job.author_name
    try:
        ve.attribs["source"] = job.local_path
    except Exception:
        pass
    if job.comment:
        try:
            ve.attribs["comment"] = job.comment
        except Exception:
            pass
    with API_LOCK:
        hub.commit_changes()
    vid = ve["id"]

    # Facial video -> the browser-safe review mp4 (clean mic in sync), saved
    # INTO the take so it is published as the 'review' representation too.
    facial_review = None
    if job.take is not None and getattr(job.take, "facial", None) is not None \
            and os.path.isdir(job.local_path):
        facial_review = build_facial_review(job.take, Path(job.local_path),
                                            name, log_cb=_cb)

    # Sidecar FIRST, so it is picked up by the file walk below and lands in
    # the published folder + as a representation. A review machine can then
    # read status/version/comments from AYON without ever seeing this PC.
    if job.take is not None and os.path.isdir(job.local_path):
        try:
            meta = build_sidecar(
                job.take, project=project, folder_path=fe.get("path", ""),
                task=(te["name"] if job.task_id else ""), version=next_v,
                author=job.author_name, take_dir=job.local_path)
            Path(job.local_path, SIDECAR_NAME).write_text(
                json.dumps(meta, indent=2), encoding="utf-8")
            _cb(f"{job.take_name}: wrote {SIDECAR_NAME} "
                f"(status={meta['status']}, v{next_v:03d})")
        except Exception as exc:
            _cb(f"⚠ could not write {SIDECAR_NAME}: {exc}")

    # Collect the take's files: every track master + the sidecar. Temp /
    # half-written files are skipped.
    files = []
    if os.path.isfile(job.local_path):
        fn = os.path.basename(job.local_path)
        files.append((job.local_path, fn, fn))
    elif os.path.isdir(job.local_path):
        for root, dirs, fnames in os.walk(job.local_path):
            # an interrupted facial import leaves 'facial.importing'
            dirs[:] = [d for d in dirs if not d.endswith(".importing")]
            for f in fnames:
                # temp files, and hidden ones (the Review-tab thumbnail)
                if f.lower().endswith(_SKIP_SUFFIXES) or f.startswith("."):
                    continue
                src = os.path.join(root, f)
                rel = os.path.relpath(src, job.local_path)
                files.append((src, rel, f))
    if not files:
        raise RuntimeError("no files to publish in take folder")
    files.sort(key=lambda it: it[1])       # deterministic representation order

    dest_base = _server_publish_path(project, folder_id, product_type, safe,
                                     job.variant, ver_str, log_cb=log_cb)
    _cb(f"{job.take_name}: {len(files)} file(s) → {dest_base}")
    for src, rel, _fname in files:
        dest = os.path.join(dest_base, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        _copy_to_share(src, dest)

    # ---- representations, integrator-style ------------------------------ #
    root_name, root_value = _publish_root(project)
    facial_main = ""
    if job.take is not None and getattr(job.take, "facial", None):
        facial_main = Path(job.take.facial.path).name
    repres = collect_representations(
        files, name_fn=lambda rel, fname, ext: repre_name(
            rel, fname, ext, shot=name, facial_main=facial_main))
    project_code = ""
    try:
        project_code = (ayon_api.get_project(project) or {}).get("code") or ""
    except Exception:
        pass
    context_base = {
        "project": {"name": project, "code": project_code},
        "folder": {"name": name, "path": fe.get("path", "")},
        "product": {"name": safe, "type": product_type},
        "version": next_v,
        "user": {"name": job.author_name or ""},
    }
    if job.task_id:
        context_base["task"] = {"name": te["name"]}

    # Version attributes loaders read directly. Audio has no frames, so the
    # frame range is the take's DURATION at the shot's fps -- that is what
    # makes the clip the right length on an editorial timeline.
    main_audio = next((r for r in repres
                       if ("." + r["ext"]) in _AUDIO_MASTER_EXTS), None)
    duration = 0.0
    if main_audio:
        duration = audio_duration(
            os.path.join(dest_base, main_audio["items"][0][1]))
    folder_attribs = fe.get("attrib") or {}
    frame_start = int(folder_attribs.get("frameStart") or 1001)
    fps = float(folder_attribs.get("fps") or 25.0)
    fac = getattr(job.take, "facial", None) if job.take is not None else None
    has_review = bool(job.review_path or facial_review)
    version_attribs = {
        "families": [product_type] + (["review"] if has_review else []),
        "handleStart": 0, "handleEnd": 0,
        "source": job.local_path.replace("\\", "/"),
    }
    if job.comment:
        version_attribs["comment"] = job.comment
    if fac is not None and fac.fps and fac.frames:
        # A video is in the version: its REAL frames and rate define the
        # range (what Resolve's clip loaders read), plus its resolution.
        fps, frames = float(fac.fps), int(fac.frames)
        if fac.width and fac.height:
            version_attribs["resolutionWidth"] = int(fac.width)
            version_attribs["resolutionHeight"] = int(fac.height)
    else:
        frames = int(round(duration * fps))
    version_attribs["fps"] = fps
    version_attribs["frameStart"] = frame_start
    version_attribs["frameEnd"] = frame_start + max(frames, 1) - 1
    try:
        ayon_api.patch(f"projects/{project}/versions/{vid}",
                       attrib=version_attribs)
    except Exception as exc:
        _cb(f"⚠ could not set version attribs: {exc}")

    for repre in repres:
        _post_representation(project, vid, repre, dest_base, context_base,
                             root_name, root_value)
    _cb(f"{job.take_name}: {len(repres)} representation(s): "
        + ", ".join(r["name"] for r in repres))

    # ---- reviewable (what AYON's web viewer plays) ---------------------- #
    # Failures here used to go only to the Python logger, so an unplayable
    # or rejected reviewable was invisible. Every step now reports to the
    # app log, and the server's verdict is read back.
    proxy = None
    if facial_review is not None:
        # already browser-safe, and it is ours (in the take) -- upload as is
        _cb(f"{job.take_name}: reviewable = the facial review video")
        upload = str(facial_review)
    elif not (job.review_path and os.path.isfile(job.review_path)):
        _cb(f"⚠ {job.take_name}: no audio track or video to use as the "
            "AYON reviewable")
        upload = None
    else:
        _cb(f"{job.take_name}: reviewable from {Path(job.review_path).name}")
        proxy = make_review_proxy(job.review_path, log_cb=_cb)
        if proxy is None:
            _cb("⚠ no reviewable uploaded: rendering the waveform video needs "
                "ffmpeg (set it in the top bar). The masters ARE published.")
            return next_v, (f"Published {job.take_name}: {len(files)} file(s) "
                            f"-> {ver_str} @ {dest_base} (no reviewable)")
        upload = proxy
    if upload is not None:
        try:
            resp = ayon_api.upload_reviewable(
                project, vid, upload,
                filename=f"{safe}_{ver_str}.mp4", content_type="video/mp4")
            code = getattr(resp, "status_code", 200)
            if isinstance(code, int) and code >= 400:
                raise RuntimeError(f"HTTP {code}: "
                                   f"{getattr(resp, 'text', '')[:200]}")
            _cb(f"⬆ reviewable uploaded "
                f"({os.path.getsize(upload) / 1e6:.1f} MB)")
            _report_reviewables(project, vid, _cb)
        except TypeError:
            # Older ayon-python-api without filename/content_type keywords.
            try:
                ayon_api.upload_reviewable(project, vid, upload)
                _cb("⬆ reviewable uploaded")
                _report_reviewables(project, vid, _cb)
            except Exception as exc:
                _cb(f"✕ reviewable upload FAILED: {exc}")
        except Exception as exc:
            _cb(f"✕ reviewable upload FAILED: {exc}")
        finally:
            if proxy:
                try:
                    os.remove(proxy)
                except OSError:
                    pass

    return next_v, (f"Published {job.take_name}: {len(files)} file(s) -> "
                    f"{ver_str} @ {dest_base}")


def _summarize(r: PublishResult) -> str:
    if r.published:
        return (f"{len(r.published)} take(s) published: {', '.join(r.published)}"
                + (f"  ·  {len(r.skipped)} not published" if r.skipped else ""))
    return f"Nothing published — {len(r.skipped)} take(s) not published"
