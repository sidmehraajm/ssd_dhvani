"""Main window -- the Control Room.

Top bar cascade is REAL AYON (no fake data):
    Project (get_project_names)  ->  Shot search (get_folders type=Shot)
    ->  Task (get_tasks for the selected shot folder)

The audio input is opened ONCE and kept live across task switches: the level
meters always move (that's how you set the gain), and switching tasks only
swaps take data -- it never re-opens the device.

Publishing runs live on a background thread with a progress bar and reports only
real successes (no fake "published").
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import time
import traceback
from functools import partial
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal, QThread, QUrl
from PySide6.QtGui import QDesktopServices, QColor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QComboBox, QPushButton, QTextEdit, QScrollArea, QFrame, QLineEdit,
    QProgressBar, QFileDialog, QCompleter, QStackedWidget, QCheckBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QSizePolicy, QMenu,
    QMessageBox, QDialog, QFormLayout, QDialogButtonBox, QApplication,
    QSpinBox,
)

from . import __version__, VERSION_TAG, VERSION_LABEL
from .config import AppConfig
from .ayon_client import AyonClient, Task as AyonTask
from .take_manager import TakeManager, _slug
from .manifest import TakeData, TRACK_ROLES
from . import devices as dev_mod
from . import recorder as rec_mod
from . import publish
from . import theme as theme_mod
from . import shotdb as shotdb_mod
from . import tools
from . import waveform as wave_mod
from . import offline_cache as offline_mod
from . import facial as facial_mod
from . import share as share_mod
from .widgets import (RecordButton, ClickableFrame, ReviewTile, TrackStrip,
                      DropZone, TRACK_COLORS, fmt_duration, fmt_db)

#: Task selected automatically when a shot is picked -- the FIRST of these
#: the shot has wins; otherwise the shot's first task is used.
DEFAULT_TASKS = ("primarycam", "primary_cam", "primary", "facialrecording",
                 "audio", "sound", "voice", "dialogue", "recording_package")

_STATUS_META = {
    "reject":  ("REJECT",   "#f2555a"),
    "view":    ("VIEW",     "#4b9fff"),
    "okay":    ("OKAY",     "#20c997"),
    "approve": ("APPROVED", "#3fb950"),
}


# --------------------------------------------------------------------------- #
# Background publish worker -- publishes view/approve takes one at a time and
# reports progress + REAL results (no fake success).
# --------------------------------------------------------------------------- #
class PublishWorker(QThread):
    progress = Signal(int)          # 0..100
    logged = Signal(str)
    done = Signal(int, int, str)    # (published_count, total, last_reason)

    def __init__(self, takes, task_dir, project, folder_id, task_id, author,
                 republish=False):
        super().__init__()
        self._republish = republish
        self._takes = takes
        self._task_dir = task_dir
        self._project = project
        self._folder_id = folder_id
        self._task_id = task_id
        self._author = author

    def run(self):
        total = len(self._takes)
        if total == 0:
            self.done.emit(0, 0, "nothing to publish")
            return
        published = 0
        last = ""
        for i, take in enumerate(self._takes):
            ok, msg = publish.publish_one(
                take, task_dir=self._task_dir, project=self._project,
                folder_id=self._folder_id, task_id=self._task_id, author=self._author,
                log_cb=self.logged.emit, republish=self._republish,
            )
            if ok:
                published += 1
            else:
                last = msg
            self.logged.emit(msg)
            self.progress.emit(int((i + 1) / total * 100))
        self.done.emit(published, total, last)


class CheckWorker(QThread):
    """Runs publish.check_take off the UI thread (it reads the network drive)."""
    done = Signal(str, object)      # (take name, publish.CheckReport)

    def __init__(self, take, task_dir, project, folder_id, task_id):
        super().__init__()
        self._take = take
        self._args = dict(task_dir=task_dir, project=project,
                          folder_id=folder_id, task_id=task_id)

    def run(self):
        rep = publish.check_take(self._take, **self._args)
        self.done.emit(self._take.take, rep)


class FacialImportWorker(QThread):
    """Copies / unzips a facial recording into a take, probes it and
    auto-syncs the mic -- a 4K phone video is gigabytes, never on the UI."""

    progress = Signal(str, float)            # message, 0..1
    done = Signal(str, object)               # take name, FacialEntry
    failed = Signal(str, str)                # take name, message

    def __init__(self, src, take_name, take_dir, task_dir, mic_wav, mic_role):
        super().__init__()
        self._src, self._take = src, take_name
        self._take_dir, self._task_dir = take_dir, task_dir
        self._mic, self._role = mic_wav, mic_role
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self):
        try:
            entry = facial_mod.import_facial(
                self._src, self._take_dir, self._task_dir,
                mic_wav=self._mic, mic_role=self._role,
                progress=self.progress.emit, stop=lambda: self._stop)
            self.done.emit(self._take, entry)
        except facial_mod.Cancelled:
            self.failed.emit(self._take, "import cancelled")
        except ValueError as exc:
            self.failed.emit(self._take, str(exc))
        except Exception:
            self.failed.emit(self._take, traceback.format_exc())


class TakeFinalizeWorker(QThread):
    """Drains the take's queue, closes the files and pre-computes their
    waveforms -- all off the UI thread, so Stop never freezes the window.
    Registration in the manifest happens back on the UI thread (`done`)."""

    done = Signal(object, object)    # take, TakeResult (or None)

    def __init__(self, take, writer):
        super().__init__()
        self._take, self._writer = take, writer

    def run(self):
        res = None
        try:
            res = self._writer.finish()
            for tr in res.tracks:
                if tr.path.exists():
                    wave_mod.peaks(tr.path)      # warm the review cache
        except Exception:
            logging.getLogger("ssd_dhvani").exception("finalize failed")
        self.done.emit(self._take, res)


class OfflinePlanWorker(QThread):
    """Works out what 'make <task> available offline' has to copy."""

    progress = Signal(str)
    done = Signal(str, str, list)        # project, task, plan
    failed = Signal(str)

    def __init__(self, ayon, project: str, task: str):
        super().__init__()
        self._ayon, self._project, self._task = ayon, project, task

    def run(self):
        try:
            plan = self._ayon.plan_offline_task(
                self._project, self._task, offline_mod.PLAYABLE_EXTS,
                progress=self.progress.emit)
            self.done.emit(self._project, self._task, plan)
        except Exception:
            self.failed.emit(traceback.format_exc())


class OfflineDownloadWorker(QThread):
    """Copies the planned versions into the offline cache.

    Files are copied in chunks to '<name>.part' and renamed only when
    complete, so a cancelled or interrupted sync never leaves a truncated
    file that looks valid. Versions already cached are skipped, so re-running
    it later only fetches what changed.
    """

    progress = Signal(str)
    step = Signal(int, int)              # items done, total
    done = Signal(str, str, dict)        # project, task, stats

    CHUNK = 8 * 1024 * 1024

    def __init__(self, ayon, cache, project: str, task: str, plan: list):
        super().__init__()
        self._ayon, self._cache = ayon, cache
        self._project, self._task, self._plan = project, task, plan
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _copy(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        part = dst.with_name(dst.name + ".part")
        try:
            with open(src, "rb") as fi, open(part, "wb") as fo:
                while True:
                    if self._stop:
                        raise InterruptedError("cancelled")
                    buf = fi.read(self.CHUNK)
                    if not buf:
                        break
                    fo.write(buf)
        except BaseException:
            # Cancelled, share dropped, disk full...: never leave a partial
            # file behind -- it would quietly eat space on every attempt.
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        os.replace(part, dst)
        shutil.copystat(src, dst)

    def run(self):
        stats = {"versions": 0, "skipped": 0, "failed": 0, "bytes": 0,
                 "shots": len({it["folder_id"] for it in self._plan})}
        total = len(self._plan)
        for i, it in enumerate(self._plan, 1):
            if self._stop:
                self.progress.emit("  offline copy cancelled — completed "
                                   "versions are kept")
                break
            label = (f"{it['folder_path'].rsplit('/', 1)[-1]} · "
                     f"{it['product']} v{it['version']:03d}")
            if self._cache.has(it["project"], it["folder_id"], it["task"],
                               it["product"], it["version_id"]):
                stats["skipped"] += 1
                self.step.emit(i, total)
                continue
            vdir = self._cache.version_dir(it["project"], it["folder_path"],
                                           it["task"], it["product"],
                                           it["version"])
            got: list[str] = []
            try:
                for f in it["files"]:
                    src, dst = Path(f["src"]), vdir / f["rel"]
                    if not (dst.exists() and dst.stat().st_size == src.stat().st_size):
                        self._copy(src, dst)
                        stats["bytes"] += dst.stat().st_size
                    got.append(f["rel"])
                if not got:
                    # Nothing reachable on disk from this PC: use the
                    # reviewable the publisher uploaded to the server.
                    p = self._ayon.download_reviewable_to(
                        it["project"], it["version_id"], vdir)
                    if p is not None:
                        got.append(p.name)
                        stats["bytes"] += p.stat().st_size
            except InterruptedError:
                # Files of this half-copied version that DID complete are
                # orphans (never indexed) -- remove them so a cancel is clean.
                if not self._cache.has(it["project"], it["folder_id"],
                                       it["task"], it["product"],
                                       it["version_id"]):
                    self._cache._rmtree(vdir)
                continue
            except Exception as exc:
                stats["failed"] += 1
                self.progress.emit(f"  ✕ {label}: {exc}")
                self.step.emit(i, total)
                continue
            if not got:
                stats["failed"] += 1
                self.progress.emit(f"  ✕ {label}: no playable media found "
                                   "on disk or on the server")
            else:
                entry = {k: it[k] for k in (
                    "project", "folder_id", "folder_path", "task", "task_id",
                    "product", "version", "version_id", "author", "created",
                    "comment")}
                entry.update({"dir": str(vdir), "files": got,
                              "cached_at": time.strftime("%Y-%m-%d %H:%M")})
                self._cache.put(entry)
                stats["versions"] += 1
                self.progress.emit(f"  ✓ {label} ({i}/{total})")
            self.step.emit(i, total)
        if not self._stop:
            self._cache.mark_task(self._project, self._task, stats)
        self.done.emit(self._project, self._task, stats)


class ShareWorker(QThread):
    """Check (and if allowed, mount) the studio share off the GUI thread:
    osascript / net use can take seconds, or hang on a dead server."""
    logged = Signal(str)
    done = Signal(object)          # share_mod.ShareStatus

    def __init__(self, config, project: str, mount: bool):
        super().__init__()
        self._config, self._project, self._mount = config, project, mount

    def run(self):
        try:
            st = share_mod.ensure(self._config, self._project,
                                  mount=self._mount, log=self.logged.emit)
        except Exception as exc:      # never let a thread raise
            st = share_mod.ShareStatus(False, "", f"share check failed: {exc}")
        self.done.emit(st)


class FolderLoadWorker(QThread):
    """Loads one project's folder list without blocking the UI."""

    done = Signal(str, int, dict)        # project, generation, {path: id}
    failed = Signal(str, int, str)       # project, generation, traceback

    def __init__(self, ayon, project: str, gen: int):
        super().__init__()
        self._ayon, self._project, self._gen = ayon, project, gen

    def run(self):
        try:
            folders = self._ayon.folders(self._project)
            self.done.emit(self._project, self._gen, folders)
        except Exception:
            self.failed.emit(self._project, self._gen, traceback.format_exc())


class ShotSyncWorker(QThread):
    """Mirrors EVERY project's shots + tasks into the local cache.

    Runs once per sign-in, in the background: two requests per project
    (folders, then all tasks in one go), so a studio-sized server is a few
    seconds of traffic and the whole shot list is available offline -- not
    just the project that happened to be opened.
    """

    progress = Signal(str)
    done = Signal(int, int)          # projects synced, shots cached

    def __init__(self, ayon, db):
        super().__init__()
        self._ayon = ayon
        self._db = db
        self._abort = False

    def stop(self) -> None:
        self._abort = True

    def run(self):
        projects, shots = 0, 0
        try:
            names = self._ayon.list_projects()
            self._db.sync_projects(names)
        except Exception:
            self.progress.emit("⚠ offline cache: could not list projects")
            self.done.emit(0, 0)
            return
        for name in names:
            if self._abort:
                break
            try:
                folders = self._ayon.folders(name)
                self._db.sync_folders(name, folders)
                shots += len(folders)
                projects += 1
                try:
                    self._db.sync_project_tasks(name, self._ayon.all_tasks(name))
                except Exception:
                    pass          # tasks are a bonus; shots are the point
                self.progress.emit(f"   cached {len(folders)} shot(s) for {name}")
            except Exception as exc:
                self.progress.emit(f"   ⚠ {name}: {exc}")
        self.done.emit(projects, shots)



class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig, ayon: AyonClient) -> None:
        super().__init__()
        self.config = config
        self.ayon = ayon
        # Offline mode: no AYON — typed shot/task, recordings under
        # {save_root}/offline/{shot}/{task}; publish & reference disabled.
        self.offline = not getattr(ayon, "is_connected", False)
        # The project actually SELECTED. The project box is searchable, so its
        # currentText() is whatever is being typed -- never use that as the
        # active project.
        self._current_project = ""
        # Local mirror of AYON project/shot/task NAMES, so offline sessions
        # still record into the real folder structure.
        self.shotdb = shotdb_mod.ShotDB(
            Path(config.save_root_path) / ".ssd_dhvani" / "shots.sqlite3")
        # Local copies of published versions, playable without the network.
        self.offline_cache = offline_mod.OfflineCache(
            Path(config.save_root_path) / ".ssd_dhvani" / "offline_cache")
        self.takes = TakeManager(config)
        self.selected_take: Optional[TakeData] = None

        # AYON selection state
        self._folders: dict[str, str] = {}   # {folder_path: folder_id} per project
        self._shot_id: str = ""
        self._shot_path: str = ""
        self._task_id: str = ""
        self._shot_tasks: list = []          # ayon_client.Task list for current shot
        self._ref_path: str = ""             # loaded reference file (absolute)
        self._pub_worker: Optional[PublishWorker] = None
        self._check_worker: Optional[CheckWorker] = None
        self._check_state: dict = {}     # take name -> True (all good) / False
        # studio drive check/mount (ShareWorker) state
        self._share_worker: Optional[ShareWorker] = None
        self._share_status = None          # last share_mod.ShareStatus
        self._share_thens: list = []       # callbacks of the running check
        self._share_queue: list = []       # (mount, callback) asked meanwhile
        self._share_mounting = False
        publish.set_share(config.share_url, config.share_windows_drive,
                          config.share_mount_point)

        # audio runtime: ONE input, opened once and kept live
        self._input: Optional[rec_mod.AudioInput] = None
        # Every QThread we start is kept here until it has FINISHED: a Python
        # QThread object garbage-collected while its thread still runs takes
        # the whole process down (native crash, nothing in the console).
        self._threads: list[QThread] = []
        self._strips: list[TrackStrip] = []
        self._recording = False
        self._rec_t0 = 0.0
        self._countdown_until = 0.0   # >0 while the count-in is running
        self._take_writer = None      # writer of the take being recorded
        self._fin_worker: Optional[TakeFinalizeWorker] = None
        self._silence_since = 0.0     # for the "no signal" hint

        self.setWindowTitle(f"ssd_dhvani {VERSION_LABEL}")
        self.resize(1440, 860)
        self.setMinimumSize(1120, 700)
        self._build()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)  # ~25 fps: meters + timecode

        if not dev_mod.available():
            self._log("✕ Audio input unavailable — " + dev_mod.why_unavailable())
        self._load_projects()
        # The input opens at startup (not on first task pick), so the meters
        # already move while you pick the shot -- time to check the gain.
        self._fill_host_apis()

    # ================================================================= #
    # Construction
    # ================================================================= #
    def _build(self) -> None:
        # Log box FIRST so _log() is safe from any point during construction.
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(96)
        self.log_box.setMaximumHeight(150)
        self._apply_log_palette()

        root = QWidget()
        self.setCentralWidget(root)
        col = QVBoxLayout(root)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        col.addWidget(self._build_titlebar())
        # The top bars hold a lot of controls; on a narrow window (or at 125%
        # Windows scaling) the last ones used to be cut off. Wrapping them in
        # a horizontal scroller keeps every control reachable.
        col.addWidget(self._hscroll(self._build_breadcrumb(), 62))

        col.addWidget(self._build_tabbar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_left_rail(), 0)
        body.addWidget(self._build_center(), 1)
        body.addWidget(self._build_right_dock(), 0)
        body_w = QWidget()
        body_w.setLayout(body)

        self.stack = QStackedWidget()
        self.stack.addWidget(body_w)                       # 0: RECORD
        self.stack.addWidget(self._build_review_page())    # 1: REVIEW
        self.stack.addWidget(self._build_published_page()) # 2: PUBLISHED
        col.addWidget(self.stack, 1)
        col.addWidget(self.log_box, 0)

    # ---- top bar -------------------------------------------------------- #
    def _build_titlebar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("TitleBar")
        bar.setMinimumHeight(48)
        h = QHBoxLayout(bar)
        h.setContentsMargins(18, 0, 18, 0)
        h.setSpacing(13)

        logo = QLabel("d")
        logo.setMinimumSize(24, 24)
        logo.setMaximumSize(28, 28)
        logo.setAlignment(Qt.AlignCenter)
        logo.setStyleSheet("background:#e0952f;color:#0d0f11;border-radius:6px;font-weight:800;")
        h.addWidget(logo)

        name = QLabel("ssd_dhvani")
        name.setObjectName("AppName")
        h.addWidget(name)

        ver = QLabel(VERSION_TAG)
        ver.setToolTip(f"ssd_dhvani {__version__}")
        ver.setStyleSheet(
            "color:#8b9298;font-family:'IBM Plex Mono',monospace;font-size:10px;")
        h.addWidget(ver)

        # Theme switch sits next to the logo where it is easy to find.
        self.theme_btn = QPushButton()
        self.theme_btn.setMinimumHeight(26)
        self.theme_btn.setMinimumWidth(86)
        self.theme_btn.setToolTip("Switch between dark and light theme")
        self.theme_btn.clicked.connect(self._toggle_theme)
        self._sync_theme_button()
        h.addWidget(self.theme_btn)

        self.crumb = QLabel("—")
        self.crumb.setObjectName("Crumb")
        h.addWidget(self.crumb)
        h.addStretch()

        if self.offline:
            self.ayon_dot = QLabel("● OFFLINE")
            self.ayon_dot.setToolTip(
                "Working offline — takes are saved locally under "
                "{save root}/offline/. Restart and sign in to publish.")
            self.ayon_dot.setStyleSheet(
                "color:#e0952f;font-weight:600;font-size:11px;")
        else:
            self.ayon_dot = QLabel("● AYON")
            connected = self.ayon.is_connected
            self.ayon_dot.setStyleSheet(
                f"color:{'#5fd47c' if connected else '#8b9298'};font-weight:600;font-size:11px;")
        h.addWidget(self.ayon_dot)

        # Studio share (smb://kumbh/production == P:\\): grey = unknown,
        # green = mounted, red = missing. Click = settings + connect.
        self.drive_btn = QPushButton("● Drive")
        self.drive_btn.setMinimumHeight(26)
        self.drive_btn.setToolTip("Studio drive — click to set user/password "
                                  "and connect")
        self.drive_btn.clicked.connect(self._open_share_dialog)
        self._paint_drive(None, "not checked yet")
        h.addWidget(self.drive_btn)

        user = getattr(self.ayon, "username", "") or "?"
        avatar = QLabel(user[:2].upper())
        avatar.setToolTip(user)
        avatar.setMinimumSize(28, 28)
        avatar.setMaximumSize(32, 32)
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setStyleSheet("background:#3a2f1c;color:#e0952f;border-radius:14px;font-weight:700;")
        h.addWidget(avatar)
        return bar

    # ---- top cascade: Project + shot search + Task (all real AYON) ----- #
    def _build_breadcrumb(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("Breadcrumb")
        bar.setMinimumHeight(52)
        h = QHBoxLayout(bar)
        h.setContentsMargins(18, 0, 18, 0)
        h.setSpacing(8)

        # Both are SEARCHABLE: type any part of the name, pick from the
        # filtered list (or press Enter for the first match). Nothing loads
        # until a real pick -- typing never triggers AYON queries.
        self.cb_project = QComboBox()
        self.cb_project.setMinimumWidth(170)
        h.addWidget(self.cb_project)

        self.cb_shot = QComboBox()
        self.cb_shot.setMinimumWidth(240)
        h.addWidget(self.cb_shot)

        task_lbl = QLabel("Task")
        task_lbl.setStyleSheet("color:#565d64;font-size:11px;")
        h.addWidget(task_lbl)
        self.cb_task = QComboBox()
        self.cb_task.setMinimumWidth(140)
        h.addWidget(self.cb_task)
        h.addStretch()

        save_lbl = QLabel("Save to")
        save_lbl.setStyleSheet("color:#565d64;font-size:11px;")
        h.addWidget(save_lbl)
        self.save_root_edit = QLineEdit(self.config.save_root)
        self.save_root_edit.setMinimumWidth(170)
        self.save_root_edit.setToolTip("Local folder where takes are recorded")
        self.save_root_edit.editingFinished.connect(self._on_save_root_changed)
        h.addWidget(self.save_root_edit)
        browse_btn = QPushButton("…")
        browse_btn.setMinimumWidth(34)
        browse_btn.setToolTip("Browse for the recording save folder")
        browse_btn.clicked.connect(self._browse_save_root)
        h.addWidget(browse_btn)

        ff_lbl = QLabel("ffmpeg")
        ff_lbl.setStyleSheet("color:#565d64;font-size:11px;")
        h.addWidget(ff_lbl)
        self.ffmpeg_edit = QLineEdit(self.config.ffmpeg_path)
        self.ffmpeg_edit.setMinimumWidth(190)
        self.ffmpeg_edit.setPlaceholderText(
            f"path to {tools.exe_name('ffmpeg')} (optional)…")
        self.ffmpeg_edit.setToolTip(
            "ffmpeg is only used when publishing: it renders the waveform "
            "video AYON's web viewer plays. Recording never needs it. "
            "Saved per workstation.")
        self.ffmpeg_edit.editingFinished.connect(self._on_ffmpeg_changed)
        h.addWidget(self.ffmpeg_edit)
        ff_browse = QPushButton("…")
        ff_browse.setMinimumWidth(34)
        ff_browse.setToolTip("Select the ffmpeg executable")
        ff_browse.clicked.connect(self._browse_ffmpeg)
        h.addWidget(ff_browse)

        self.sync_btn = QPushButton("⭳ Sync shots")
        self.sync_btn.setToolTip(
            "Cache every project's shots and tasks locally, so the full shot "
            "list is available when you work offline")
        self.sync_btn.clicked.connect(self._start_shot_sync)
        self.sync_btn.setVisible(not self.offline)
        h.addWidget(self.sync_btn)

        self.reload_btn = QPushButton("⟳ Reload")
        self.reload_btn.setToolTip(
            "Re-read projects, shots and tasks from AYON (new shots/tasks "
            "added on the server show up) — keeps your current selection")
        self.reload_btn.clicked.connect(self._reload_all)
        self.reload_btn.setVisible(not self.offline)
        h.addWidget(self.reload_btn)

        self._make_searchable(
            self.cb_project, "type to search project…",
            on_pick=lambda i: self._pick_project(self.cb_project.itemText(i)),
            committed=lambda: self._current_project)
        self._make_searchable(
            self.cb_shot, "type to search shot…",
            on_pick=self._on_shot_selected,
            committed=lambda: self._shot_path,
            # Offline, Enter on a name that isn't cached means a FREE-FORM
            # shot (handled below) -- so never auto-pick a partial match, and
            # never wipe what was typed.
            enter_partial=not self.offline, restore=not self.offline)
        self.cb_task.currentIndexChanged.connect(self._on_task_selected)

        if self.offline:
            # Offline still uses the REAL project/shot names from the local
            # cache; typing a shot that isn't cached falls back to a
            # free-form offline take.
            self.cb_shot.lineEdit().setPlaceholderText(
                "pick a cached shot, or type a shot number + Enter…")
            self.cb_shot.lineEdit().returnPressed.connect(
                self._apply_offline_context)
            self.cb_task.setEditable(True)
            self.cb_task.setInsertPolicy(QComboBox.NoInsert)
            self.cb_task.lineEdit().editingFinished.connect(
                self._offline_task_typed)
        return bar

    # ---- searchable dropdowns -------------------------------------------- #
    def _make_searchable(self, combo: QComboBox, placeholder: str, on_pick,
                         committed, enter_partial: bool = True,
                         restore: bool = True) -> None:
        """Type-to-filter dropdown: substring, case-insensitive.

        A pick can arrive three ways -- the dropdown list, the filtered
        search popup, or Enter -- and Qt 6 does not reliably emit the combo's
        own `activated` for a pick from the search popup (the text would
        change but nothing loaded). All three are routed to `on_pick(index)`
        and de-duplicated. Leaving the field with text that matches nothing
        puts back the committed value, so the box never shows a stale name.
        """
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        combo.setMaxVisibleItems(18)
        comp = combo.completer()
        comp.setCompletionMode(QCompleter.PopupCompletion)
        comp.setFilterMode(Qt.MatchContains)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setMaxVisibleItems(18)
        comp.popup().setMinimumWidth(max(260, combo.minimumWidth()))
        combo.lineEdit().setPlaceholderText(placeholder)

        combo.activated.connect(lambda i: self._combo_fire(combo, on_pick, i))
        try:
            comp.activated[str].connect(
                lambda text: self._combo_pick_text(combo, on_pick, text, False))
        except (KeyError, TypeError, IndexError):
            comp.activated.connect(
                lambda text: self._combo_pick_text(combo, on_pick, text, False))
        combo.lineEdit().returnPressed.connect(
            lambda: self._combo_pick_text(combo, on_pick,
                                          combo.lineEdit().text(), enter_partial))
        if restore:
            combo.lineEdit().editingFinished.connect(
                lambda: self._combo_restore(combo, committed()))

    def _combo_pick_text(self, combo, on_pick, text: str, partial: bool) -> None:
        text = (text or "").strip()
        if not text:
            return
        idx = combo.findText(text, Qt.MatchFixedString)   # exact, any case
        if idx < 0 and partial:
            low = text.lower()
            idx = next((i for i in range(combo.count())
                        if low in combo.itemText(i).lower()), -1)
        if idx >= 0:
            self._combo_fire(combo, on_pick, idx)

    def _combo_fire(self, combo, on_pick, idx: int) -> None:
        if idx < 0 or idx >= combo.count():
            return
        # The same pick often arrives twice (popup + Enter + activated).
        picks = self.__dict__.setdefault("_last_combo_pick", {})
        now = time.perf_counter()
        last = picks.get(id(combo))
        if last and last[0] == idx and now - last[1] < 0.5:
            return
        picks[id(combo)] = (idx, now)
        combo.blockSignals(True)
        combo.setCurrentIndex(idx)
        combo.blockSignals(False)
        combo.lineEdit().setText(combo.itemText(idx))
        on_pick(idx)

    @staticmethod
    def _combo_restore(combo, committed_text: str) -> None:
        typed = combo.lineEdit().text().strip()
        if typed and combo.findText(typed, Qt.MatchFixedString) >= 0:
            return
        if combo.lineEdit().text() != (committed_text or ""):
            combo.lineEdit().setText(committed_text or "")

    def _pick_project(self, name: str) -> None:
        if name and name != self._current_project:
            self._on_project(name)

    # ---- Record / Review tab switcher ------------------------------------ #
    def _build_tabbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("Breadcrumb")
        bar.setMinimumHeight(40)
        h = QHBoxLayout(bar)
        h.setContentsMargins(18, 0, 18, 0)
        h.setSpacing(6)
        self.tab_record = QPushButton("●  RECORD")
        self.tab_review = QPushButton("▶  REVIEW")
        self.tab_published = QPushButton("⬆  PUBLISHED")
        self._tabs = (self.tab_record, self.tab_review, self.tab_published)
        for i, b in enumerate(self._tabs):
            b.clicked.connect(lambda _c=False, idx=i: self._switch_tab(idx))
            h.addWidget(b)
        h.addStretch()
        self._style_tabs(0)
        return bar

    def _style_tabs(self, active: int) -> None:
        for i, b in enumerate(self._tabs):
            b.setObjectName("TabOn" if i == active else "Tab")
            b.style().unpolish(b)
            b.style().polish(b)

    def _switch_tab(self, idx: int) -> None:
        self.stack.setCurrentIndex(idx)
        self._style_tabs(idx)
        if idx == 1:
            self._refresh_review()
        elif idx == 2:
            self._refresh_published()

    # ---- Review page: big thumbnail grid + compact take controls --------- #
    def _build_review_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_export_bar())
        body = QWidget()
        h = QHBoxLayout(body)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        left = QWidget()
        left.setObjectName("Panel")
        left.setMinimumWidth(230)
        left.setMaximumWidth(300)
        v = QVBoxLayout(left)
        v.setContentsMargins(14, 16, 14, 16)
        v.setSpacing(10)

        v.addWidget(self._section("TAKE", muted=True))
        self.cb_rev_take = QComboBox()
        self.cb_rev_take.currentIndexChanged.connect(self._on_review_take_changed)
        v.addWidget(self.cb_rev_take)

        status_grid = QGridLayout()          # 2x2: four buttons, no clipping
        status_grid.setSpacing(4)
        self.rev_btn = {}
        for i, (key, text) in enumerate((("reject", "Reject"), ("view", "View"),
                                         ("okay", "Okay"), ("approve", "Approve"))):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, k=key: self._set_status_review(k))
            status_grid.addWidget(b, i // 2, i % 2)
            self.rev_btn[key] = b
        v.addLayout(status_grid)

        v.addWidget(self._section("COMMENT", muted=True))
        self.rev_comments = QTextEdit()
        self.rev_comments.setMinimumHeight(56)
        self.rev_comments.setPlaceholderText("Notes…")
        v.addWidget(self.rev_comments)
        save_c = QPushButton("Save Comment")
        save_c.clicked.connect(self._save_review_comment)
        v.addWidget(save_c)

        play_all = QPushButton("▶ Play All (video + tracks)")
        play_all.setObjectName("Primary")
        play_all.setToolTip("Open every track of this take in the default "
                            "player")
        play_all.clicked.connect(self._play_all_tracks)
        v.addWidget(play_all)
        v.addStretch(1)
        h.addWidget(left, 0)

        self.review_scroll = QScrollArea()
        self.review_scroll.setWidgetResizable(True)
        host = QWidget()
        host.setObjectName("CenterStage")
        self.review_grid = QGridLayout(host)
        self.review_grid.setContentsMargins(16, 16, 16, 16)
        self.review_grid.setSpacing(12)
        self.review_scroll.setWidget(host)
        h.addWidget(self.review_scroll, 1)
        outer.addWidget(body, 1)
        return page

    # ---- PUBLISHED tab ---------------------------------------------------- #
    def _build_published_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(18, 12, 18, 12)
        v.setSpacing(10)

        hdr = QHBoxLayout()
        title = QLabel("Published takes")
        title.setStyleSheet("font-size:14px;font-weight:700;")
        hdr.addWidget(title)
        hdr.addStretch()
        self.pub_refresh_btn = QPushButton("⟳ Refresh from AYON")
        self.pub_refresh_btn.clicked.connect(self._refresh_published)
        hdr.addWidget(self.pub_refresh_btn)
        v.addLayout(hdr)

        self.pub_summary = QLabel("")
        self.pub_summary.setWordWrap(True)
        self.pub_summary.setStyleSheet("color:#8b9298;font-size:11px;")
        v.addWidget(self.pub_summary)

        self.pub_table = QTableWidget(0, 6)
        self.pub_table.setHorizontalHeaderLabels(
            ["Take", "Status", "AYON version", "Tracks", "Comment", "Published"])
        self.pub_table.verticalHeader().setVisible(False)
        self.pub_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.pub_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.pub_table.setAlternatingRowColors(True)
        hh = self.pub_table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)     # comment grows
        self.pub_table.setWordWrap(False)
        self.pub_table.doubleClicked.connect(self._play_published_row)
        v.addWidget(self.pub_table, 1)

        note = QLabel("Double-click a row to play it. View, Okay and Approve "
                      "takes publish; each take publishes once and is then "
                      "locked.")
        note.setStyleSheet("color:#565d64;font-size:10px;")
        note.setWordWrap(True)
        v.addWidget(note)
        return page

    def _refresh_published(self) -> None:
        """Published takes for the CURRENT shot/task.

        Online, AYON is the source of truth: each published version carries an
        `ssd_take.json` sidecar with its review status, so this works on a
        review PC that never did the recording. Offline (or for versions
        published before the sidecar existed) the local manifest is used.
        """
        rows: list[dict] = []
        online = (not self.offline) and bool(self._shot_id and self._task_id)
        if online:
            try:
                for e in self.ayon.take_sidecars(
                        self._current_project, self._shot_id,
                        self._task_id):
                    rows.append({
                        "take": e.get("take") or "—",
                        "number": e.get("take_number", 0),
                        "status": e.get("review_status") or "",
                        "server_status": e.get("server_status") or "",
                        "version": e["version"],
                        "tracks": ", ".join(e.get("tracks", [])) or "—",
                        "comment": e.get("comments") or "",
                        "when": (e.get("published_at")
                                 or e.get("createdAt", ""))[:19].replace("T", " "),
                        "sidecar": e.get("sidecar", False),
                    })
            except Exception:
                self._log_exc("Could not read published takes from AYON")
                online = False
        if not online:      # offline / no shot selected -> local manifest
            for t in self.takes.manifest.takes:
                ver = int(getattr(t, "published_version", 0))
                if not ver:
                    continue
                rows.append({
                    "take": t.take, "number": t.number, "status": t.status,
                    "server_status": "", "version": ver,
                    "tracks": ", ".join((["facial video"] if t.facial else [])
                                        + list(t.tracks)) or "—",
                    "comment": t.comments or "", "when": "",
                    "sidecar": False})

        rows.sort(key=lambda r: r["version"], reverse=True)
        self.pub_table.setRowCount(len(rows))
        for row, r in enumerate(rows):
            label = (f"Take {r['number']:02d}" if r["number"]
                     else (r["take"] or "—"))
            status = r["status"]
            status_txt = _STATUS_META.get(status, (status or "—", ""))[0].title()
            if r["server_status"]:
                status_txt += f"   (AYON: {r['server_status']})"
            cells = [label, status_txt, f"v{r['version']:03d}",
                     r["tracks"], r["comment"], r["when"] or "—"]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text if col != 1 else
                                "Review status from the published sidecar"
                                if r["sidecar"] else
                                "From the local manifest (no sidecar on this "
                                "version — it predates the feature)")
                if col == 1:
                    item.setForeground(QColor(
                        _STATUS_META.get(status, ("", "#8b9298"))[1]))
                if col == 0:
                    item.setData(Qt.UserRole, r["take"])
                self.pub_table.setItem(row, col, item)

        approved = sum(1 for r in rows if r["status"] == "approve")
        okay = sum(1 for r in rows if r["status"] == "okay")
        pending = [t for t in self.takes.manifest.takes
                   if t.status in publish.PUBLISHABLE
                   and not getattr(t, "published_version", 0)]
        bits = [f"{len(rows)} published", f"{approved} approved",
                f"{okay} okay",
                f"{len(pending)} pending locally"]
        if online:
            bits.append("read from AYON — works on any machine")
        elif self.offline:
            bits.append("offline — from the local manifest")
        else:
            bits.append("pick a shot/task to read AYON")
        self.pub_summary.setText("   ·   ".join(bits))

    def _play_published_row(self, index) -> None:
        item = self.pub_table.item(index.row(), 0)
        if item is None:
            return
        take = self.takes.manifest.find(item.data(Qt.UserRole) or "")
        if take is not None:
            self._play_take(take)

    # ---- export target (top of the Review tab) ------------------------- #
    def _build_export_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("Breadcrumb")
        bar.setMinimumHeight(46)
        h = QHBoxLayout(bar)
        h.setContentsMargins(18, 6, 18, 6)
        h.setSpacing(8)

        lbl = QLabel("Export folder")
        lbl.setStyleSheet("color:#565d64;font-size:11px;")
        h.addWidget(lbl)

        self.export_dir_edit = QLineEdit(self.config.export_dir)
        self.export_dir_edit.setPlaceholderText(
            "pick a folder to copy takes to (editor, animator, lip-sync)…")
        self.export_dir_edit.setMinimumWidth(220)
        self.export_dir_edit.editingFinished.connect(self._on_export_dir_changed)
        h.addWidget(self.export_dir_edit, 1)

        browse = QPushButton("…")
        browse.setMinimumWidth(34)
        browse.setToolTip("Choose the export folder")
        browse.clicked.connect(self._browse_export_dir)
        h.addWidget(browse)

        open_btn = QPushButton("📂 Open")
        open_btn.setToolTip("Open the export folder")
        open_btn.clicked.connect(self._open_export_dir)
        h.addWidget(open_btn)

        hint = QLabel("→ right-click any waveform below → 'Export copy'")
        hint.setStyleSheet("color:#565d64;font-size:10px;")
        h.addWidget(hint)
        return bar

    def _on_export_dir_changed(self) -> None:
        self.config.export_dir = self.export_dir_edit.text().strip()
        self.config.save()
        self._log(f"Export folder → {self.config.export_dir}")

    def _browse_export_dir(self) -> None:
        start = self.config.export_dir or str(self.config.save_root_path)
        d = QFileDialog.getExistingDirectory(
            self, "Choose the export folder", start)
        if d:
            self.export_dir_edit.setText(d)
            self._on_export_dir_changed()

    def _open_export_dir(self) -> None:
        d = self.config.export_dir
        if not d or not Path(d).is_dir():
            self._log("Set a valid export folder first")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    def _export_copy(self, path: Path) -> None:
        """Copy one reviewed file to the export folder (never moves it)."""
        dest_dir = Path(self.config.export_dir or "")
        if not dest_dir.is_dir():
            self._log("✕ No export folder set — pick one at the top of the "
                      "Review tab")
            self._browse_export_dir()
            dest_dir = Path(self.config.export_dir or "")
            if not dest_dir.is_dir():
                return
        path = Path(path)
        if not path.exists():
            self._log(f"✕ Not found: {path}")
            return
        target = dest_dir / path.name
        try:
            if target.exists() and target.resolve() == path.resolve():
                self._log("✕ Source and destination are the same file")
                return
            shutil.copy2(path, target)
        except Exception:
            self._log_exc(f"Export failed for {path.name}")
            return
        self._log(f"⬆ Exported → {target}")

    # ---- left rail ------------------------------------------------------ #
    def _build_left_rail(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("Panel")
        panel.setMinimumWidth(230)
        panel.setMaximumWidth(300)
        v = QVBoxLayout(panel)
        v.setContentsMargins(14, 16, 14, 16)
        v.setSpacing(12)

        hdr = QHBoxLayout()
        hdr.addWidget(self._section("REFERENCE"))
        hdr.addStretch()
        ayon_refresh = QPushButton("⟳")
        ayon_refresh.setMinimumSize(26, 26)
        ayon_refresh.setToolTip("Re-query AYON for newly published versions")
        ayon_refresh.clicked.connect(self._refresh_ayon_versions_clicked)
        hdr.addWidget(ayon_refresh)

        self.offline_btn = QPushButton("⭳")
        self.offline_btn.setMinimumSize(26, 26)
        self.offline_btn.setToolTip(
            "Offline copies: copy the latest published versions of a task "
            "(on every shot) to this PC, to play when away from the network")
        menu = QMenu(self.offline_btn)
        menu.setStyleSheet(theme_mod.menu_qss())
        menu.aboutToShow.connect(lambda m=menu: self._build_offline_menu(m))
        self.offline_btn.setMenu(menu)
        hdr.addWidget(self.offline_btn)
        v.addLayout(hdr)

        self.cb_ref_task = QComboBox()   # populated with the shot's real tasks
        self.cb_ref_task.currentIndexChanged.connect(self._refresh_ref_versions)
        v.addWidget(self.cb_ref_task)
        self.cb_ref_ver = QComboBox()    # published versions of that task
        v.addWidget(self.cb_ref_ver)

        self.play_ref_btn = QPushButton("▶ Play Reference")
        self.play_ref_btn.setObjectName("Primary")
        self.play_ref_btn.clicked.connect(self._play_reference)
        v.addWidget(self.play_ref_btn)

        self.local_ref_btn = QPushButton("📂 Open local file…")
        self.local_ref_btn.setToolTip(
            "Play any audio or video file from this machine as the reference "
            "(works offline, no AYON needed)")
        self.local_ref_btn.clicked.connect(self._play_local_reference)
        v.addWidget(self.local_ref_btn)

        self.ref_status = QLabel("Pick task + version, then Play")
        self.ref_status.setWordWrap(True)
        self.ref_status.setStyleSheet("color:#565d64;font-size:10px;")
        v.addWidget(self.ref_status)
        v.addStretch(1)
        return panel

    def _build_center(self) -> QWidget:
        stage = QWidget()
        stage.setObjectName("CenterStage")
        v = QVBoxLayout(stage)
        v.setContentsMargins(16, 14, 16, 0)
        v.setSpacing(10)

        # Fold the whole mic section away -> the video drop area gets the room
        # (camera-first sessions). The input keeps running while folded.
        self.mic_toggle = QPushButton()
        self.mic_toggle.setFlat(True)
        self.mic_toggle.setStyleSheet(
            "QPushButton{text-align:left;color:#8b9298;font-weight:700;"
            "font-size:11px;letter-spacing:1px;border:none;padding:2px 0;}"
            "QPushButton:hover{color:#e0952f;}")
        self.mic_toggle.clicked.connect(self._toggle_mic)
        v.addWidget(self.mic_toggle)
        self.input_bar = self._build_input_bar()
        v.addWidget(self.input_bar)

        # Track strips live here; the count-in number is drawn over them.
        self.tracks_host = QWidget()
        tv = QVBoxLayout(self.tracks_host)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(8)
        self._tracks_layout = tv
        self.mic_panel = QWidget()
        mp = QVBoxLayout(self.mic_panel)
        mp.setContentsMargins(0, 0, 0, 0)
        mp.setSpacing(8)
        self.strips_box = QVBoxLayout()
        self.strips_box.setSpacing(8)
        mp.addLayout(self.strips_box)
        tv.addWidget(self.mic_panel)
        add_row = QHBoxLayout()
        self.add_track_btn = QPushButton("＋ Add track")
        self.add_track_btn.setToolTip(
            "Record another input channel of this device as its own file "
            "(e.g. a second transmitter on channel 2)")
        self.add_track_btn.clicked.connect(self._add_track_clicked)
        add_row.addWidget(self.add_track_btn)
        add_row.addStretch()
        mp.addLayout(add_row)

        # Facial recording: drop a video (or a zip with one) onto the take.
        self.drop_zone = DropZone()
        self.drop_zone.files_dropped.connect(self._on_facial_dropped)
        self.drop_zone.browse_requested.connect(self._browse_facial)
        self.drop_zone.new_take.toggled.connect(
            lambda _on: self._update_drop_target())
        tv.addWidget(self.drop_zone)
        hint = QLabel(
            "Aim for speech peaks between −12 and −6 dBFS (yellow). Set the "
            "gain on the mic / receiver / interface — never boost quiet "
            "audio later, it raises the noise too. The CLIP light latches "
            "until you click the meter.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#6b737a;font-size:11px;")
        self.gain_hint = hint
        tv.addWidget(hint)
        tv.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(self.tracks_host)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        v.addWidget(scroll, 1)
        self.grid_host = scroll          # the count-in overlay covers this

        # Big count-in number, drawn over the track area.
        self.countdown_label = QLabel("", self.grid_host)
        self.countdown_label.setAlignment(Qt.AlignCenter)
        self.countdown_label.setStyleSheet(
            "color:#f2555a;font-size:190px;font-weight:800;"
            "background:rgba(0,0,0,140);border-radius:18px;")
        self.countdown_label.hide()

        v.addWidget(self._hscroll(self._build_command_bar(), 104))
        self._apply_mic_collapsed(self.config.mic_collapsed)
        return stage

    def _toggle_mic(self) -> None:
        self.config.mic_collapsed = not self.config.mic_collapsed
        self.config.save()
        self._apply_mic_collapsed(self.config.mic_collapsed)

    def _apply_mic_collapsed(self, collapsed: bool) -> None:
        """Hide the mic controls (input bar, track strips, gain hint) and let
        the drop area take their space. Recording is unaffected."""
        for w in (self.input_bar, self.mic_panel, self.gain_hint):
            w.setVisible(not collapsed)
        tv = self._tracks_layout
        tv.setStretchFactor(self.drop_zone, 1 if collapsed else 0)
        tv.setStretch(tv.count() - 1, 0 if collapsed else 1)   # end spacer
        self.mic_toggle.setText(
            "▸  MICROPHONE  (hidden — still recording sound; click to show)"
            if collapsed else "▾  MICROPHONE")

    def _build_input_bar(self) -> QWidget:
        """Two rows: WHICH device (driver, input), then HOW it records
        (rate, depth, file, exclusive) + signal path + live status."""
        bar = QFrame()
        bar.setObjectName("InputCard")
        bar.setStyleSheet("QFrame#InputCard{background:#0f1113;"
                          "border:1px solid #1c1f23;border-radius:10px;}"
                          "QFrame#InputCard QLabel{border:none;}")
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(6)
        title = QLabel("INPUT")
        title.setObjectName("SectionLabel")
        outer.addWidget(title)
        row1, row2 = QHBoxLayout(), QHBoxLayout()
        row1.setSpacing(8)
        row2.setSpacing(8)
        outer.addLayout(row1)
        outer.addLayout(row2)
        h = row1

        def lbl(text: str) -> QLabel:
            w = QLabel(text)
            w.setStyleSheet("color:#565d64;font-size:11px;")
            return w

        h.addWidget(lbl("Driver"))
        self.cb_api = QComboBox()
        self.cb_api.setToolTip(
            "Audio driver API. Windows: WASAPI (recommended) or WDM-KS talk "
            "to the device at its real rate; MME / DirectSound resample "
            "silently. macOS: Core Audio.")
        self.cb_api.activated.connect(self._on_api_changed)
        h.addWidget(self.cb_api)

        h.addWidget(lbl("Input"))
        self.cb_device = QComboBox()
        self.cb_device.setMinimumWidth(340)
        self.cb_device.setToolTip("The microphone / receiver / interface. "
                                  "Remembered by name, not by number.")
        self.cb_device.activated.connect(self._on_device_changed)
        h.addWidget(self.cb_device)

        self.rescan_btn = QPushButton("⟳")
        self.rescan_btn.setFixedWidth(36)
        self.rescan_btn.setToolTip("Re-scan devices (after plugging a mic in)")
        self.rescan_btn.clicked.connect(self._rescan_devices)
        h.addWidget(self.rescan_btn)
        h.addStretch()
        h = row2

        h.addWidget(lbl("Rate"))
        self.cb_rate = QComboBox()
        self.cb_rate.setMinimumWidth(140)
        self.cb_rate.setToolTip(
            "Sample rate. 48 kHz is the film/video standard. Only rates the "
            "device accepts are listed; a higher rate than the hardware's own "
            "just means resampling, not quality.")
        self.cb_rate.activated.connect(self._on_quality_changed)
        h.addWidget(self.cb_rate)

        h.addWidget(lbl("Depth"))
        self.cb_depth = QComboBox()
        for key, text in dev_mod.SAMPLE_FORMATS.items():
            self.cb_depth.addItem(text, key)
        self.cb_depth.setToolTip(
            "24-bit: the master standard (what your mic delivers). "
            "32-bit float: extra headroom for heavy processing, bigger files. "
            "16-bit: CD quality, only if a delivery spec asks for it.")
        self.cb_depth.activated.connect(self._on_quality_changed)
        h.addWidget(self.cb_depth)

        h.addWidget(lbl("File"))
        self.cb_format = QComboBox()
        self.cb_format.addItem("WAV", "WAV")
        self.cb_format.addItem("FLAC (lossless, ~½ size)", "FLAC")
        self.cb_format.addItem("RF64 (WAV > 4 GB)", "RF64")
        self.cb_format.setToolTip(
            "WAV: plays everywhere (DaVinci, Maya, Blender). FLAC: lossless "
            "and about half the size. RF64: WAV without the 4 GB limit.")
        self.cb_format.activated.connect(self._on_quality_changed)
        h.addWidget(self.cb_format)

        self.chk_exclusive = QCheckBox("Exclusive")
        self.chk_exclusive.setToolTip(
            "WASAPI exclusive mode: bypass the Windows mixer entirely. "
            "Fails (and falls back to shared) if another app uses the mic.")
        self.chk_exclusive.toggled.connect(self._on_exclusive_toggled)
        h.addWidget(self.chk_exclusive)

        self.path_badge = QLabel("")
        self.path_badge.setMinimumWidth(150)
        self.path_badge.setToolTip(
            "Signal path. 'direct' = the driver's samples, untouched "
            "(bit-perfect). 'Windows mixer' = Windows processes the audio "
            "first (its Levels slider and any Enhancements apply) — fine for "
            "most work; for untouched audio use WDM-KS or Exclusive.")
        h.addWidget(self.path_badge)

        self.input_status = QLabel("○ no input")
        self.input_status.setStyleSheet("color:#8b9298;font-size:11px;")
        h.addWidget(self.input_status)
        h.addStretch()
        return bar
    @staticmethod
    def _hscroll(widget: QWidget, height: int) -> QScrollArea:
        """Wrap a toolbar so it scrolls sideways instead of clipping."""
        area = QScrollArea()
        area.setWidget(widget)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        area.setMinimumHeight(height)
        area.setMaximumHeight(height + 14)
        area.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        return area

    def _build_command_bar(self) -> QWidget:
        """Transport, laid out like a field recorder: record button, big
        timecode with a REC light, what the next take will be, the recording
        format, and how much recording time is left on the disk."""
        bar = QWidget()
        bar.setObjectName("CommandBar")
        bar.setMinimumHeight(92)
        h = QHBoxLayout(bar)
        h.setContentsMargins(22, 14, 22, 14)
        h.setSpacing(22)

        self.record_btn = RecordButton()
        self.record_btn.setToolTip("Record a new take / stop")
        self.record_btn.clicked.connect(self._toggle_record)
        h.addWidget(self.record_btn)

        lcd = QFrame()
        lcd.setObjectName("Lcd")
        lcd.setStyleSheet("QFrame#Lcd{background:#07090b;border:1px solid "
                          "#1c1f23;border-radius:8px;} QFrame#Lcd QLabel"
                          "{border:none;background:transparent;}")
        lv = QVBoxLayout(lcd)
        lv.setContentsMargins(14, 6, 14, 6)
        lv.setSpacing(0)
        top = QHBoxLayout()
        self.rec_light = QLabel("● REC")
        self.rec_light.setStyleSheet("color:#2a2e33;font-weight:800;"
                                     "font-size:11px;letter-spacing:1px;")
        top.addWidget(self.rec_light)
        top.addStretch()
        self.next_take_label = QLabel("NEXT  —")
        self.next_take_label.setStyleSheet("color:#6b737a;font-size:10px;"
                                           "letter-spacing:1px;")
        top.addWidget(self.next_take_label)
        lv.addLayout(top)
        self.tc_label = QLabel("00:00:00:00")
        self.tc_label.setObjectName("Timecode")
        self.tc_label.setStyleSheet(
            "font-family:'IBM Plex Mono',Consolas,monospace;font-size:34px;"
            "font-weight:700;color:#e9edf0;")
        lv.addWidget(self.tc_label)
        cap = QLabel(f"TIMECODE · {self.config.fps_display} FPS")
        cap.setStyleSheet("color:#5f6870;font-size:9px;letter-spacing:1px;")
        lv.addWidget(cap)
        h.addWidget(lcd)

        self.play_take_btn = QPushButton("▶ Play Take —")
        self.play_take_btn.clicked.connect(self._play_selected)
        h.addWidget(self.play_take_btn)

        h.addStretch()

        info = QVBoxLayout()
        info.setSpacing(4)
        self.armed_label = QLabel("● 0 tracks armed")
        self.armed_label.setObjectName("ArmedStatus")
        self.armed_label.setAlignment(Qt.AlignRight)
        info.addWidget(self.armed_label)
        self.disk_label = QLabel("")
        self.disk_label.setAlignment(Qt.AlignRight)
        self.disk_label.setStyleSheet("color:#6b737a;font-size:11px;")
        self.disk_label.setToolTip("Recording time left on the save drive "
                                   "at the current format")
        info.addWidget(self.disk_label)
        h.addLayout(info)
        return bar

    # ---- right dock ----------------------------------------------------- #
    def _build_right_dock(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("Panel")
        panel.setMinimumWidth(288)
        panel.setMaximumWidth(360)
        v = QVBoxLayout(panel)
        v.setContentsMargins(14, 16, 14, 16)
        v.setSpacing(12)

        v.addWidget(self._section("SELECT TAKE", muted=True))
        self.cb_take = QComboBox()
        self.cb_take.currentIndexChanged.connect(self._on_select_take_combo)
        v.addWidget(self.cb_take)

        v.addWidget(self._section("COMMENTS", muted=True))
        self.comments = QTextEdit()
        self.comments.setPlaceholderText("Notes…")
        self.comments.setMinimumHeight(64)
        v.addWidget(self.comments)

        save_btn = QPushButton("＋ Save Take")
        save_btn.setObjectName("Primary")
        save_btn.clicked.connect(self._on_save_take)
        v.addWidget(save_btn)

        takes_hdr = QHBoxLayout()
        takes_hdr.addWidget(self._section("TAKES", muted=True))
        takes_hdr.addStretch()
        nxt_lbl = QLabel("next #")
        nxt_lbl.setStyleSheet("color:#6b737a;font-size:10px;")
        takes_hdr.addWidget(nxt_lbl)
        # The number the NEXT take gets: set 3 to start this task at Take 3.
        # A number already used moves up to the next free one.
        self.next_take_spin = QSpinBox()
        self.next_take_spin.setRange(1, 999)
        self.next_take_spin.setFixedWidth(62)
        self.next_take_spin.setToolTip(
            "Number for the next take (recording or video drop). Set 3 to "
            "start at Take 3. Already-used numbers are skipped.")
        self.next_take_spin.valueChanged.connect(self._on_next_take_changed)
        takes_hdr.addWidget(self.next_take_spin)
        self.take_count = QLabel("0 recorded")
        self.take_count.setStyleSheet(
            "color:#e0952f;font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:11px;")
        takes_hdr.addWidget(self.take_count)
        v.addLayout(takes_hdr)

        self.takes_scroll = QScrollArea()
        self.takes_scroll.setWidgetResizable(True)
        self.takes_list = QWidget()
        self.takes_v = QVBoxLayout(self.takes_list)
        self.takes_v.setContentsMargins(0, 0, 0, 0)
        self.takes_v.setSpacing(7)
        self.takes_v.addStretch()
        self.takes_scroll.setWidget(self.takes_list)
        v.addWidget(self.takes_scroll, 1)

        open_btn = QPushButton("📂 Open Take Folder")
        open_btn.setToolTip("Open the saved take folder for this task")
        open_btn.clicked.connect(self._open_folder)
        v.addWidget(open_btn)

        self.status_hdr = self._section("TAKE STATUS · —", muted=True)
        v.addWidget(self.status_hdr)
        # 2x2 so four buttons still fit the side panel without clipping.
        status_grid = QGridLayout()
        status_grid.setSpacing(6)
        self.btn_reject = QPushButton("Reject")
        self.btn_view = QPushButton("View")
        self.btn_okay = QPushButton("Okay")
        self.btn_approve = QPushButton("Approve")
        for i, (b, key) in enumerate(((self.btn_reject, "reject"),
                                      (self.btn_view, "view"),
                                      (self.btn_okay, "okay"),
                                      (self.btn_approve, "approve"))):
            b.clicked.connect(lambda _c=False, k=key: self._set_status(k))
            status_grid.addWidget(b, i // 2, i % 2)
        v.addLayout(status_grid)

        self.publish_btn = QPushButton("PUBLISH")
        self.publish_btn.setObjectName("Publish")
        self.publish_btn.clicked.connect(self._on_publish)
        v.addWidget(self.publish_btn)

        self.publish_progress = QProgressBar()
        self.publish_progress.setRange(0, 100)
        self.publish_progress.setValue(0)
        self.publish_progress.setTextVisible(True)
        self.publish_progress.setFixedHeight(16)
        self.publish_progress.setVisible(False)
        self.publish_progress.setStyleSheet(
            "QProgressBar{border:1px solid #26292e;border-radius:5px;background:#131518;"
            "color:#c6ced6;font-size:10px;text-align:center;}"
            "QProgressBar::chunk{background:#2f9e44;border-radius:4px;}")
        v.addWidget(self.publish_progress)

        self.publish_note = QLabel("")
        self.publish_note.setAlignment(Qt.AlignCenter)
        self.publish_note.setWordWrap(True)
        self.publish_note.setStyleSheet("color:#5f6870;font-size:9px;")
        v.addWidget(self.publish_note)

        self._refresh_status_buttons()
        return panel

    # ---- theme ---------------------------------------------------------- #
    def _sync_theme_button(self) -> None:
        """Label says what a CLICK will do (plain ASCII: some Windows builds
        render moon/sun glyphs as blank boxes)."""
        light = self.config.theme == "light"
        self.theme_btn.setText("Dark mode" if light else "Light mode")

    def _toggle_theme(self) -> None:
        from PySide6.QtWidgets import QApplication
        self.config.theme = "dark" if self.config.theme == "light" else "light"
        self.config.save()
        theme_mod.apply_theme(QApplication.instance(), self.config.theme)
        self._apply_log_palette()
        self._sync_theme_button()
        self._style_tabs(self.stack.currentIndex())
        self._refresh_status_buttons()
        self._log(f"Theme: {self.config.theme}")

    def _apply_log_palette(self) -> None:
        c = theme_mod.palette(self.config.theme)
        self.log_box.setStyleSheet(
            "QTextEdit{font-family:Consolas,'IBM Plex Mono',monospace;"
            f"font-size:11px;background:{c['log_bg']};color:{c['log_fg']};"
            f"border:1px solid {c['log_border']};border-radius:0;padding:4px;}}")

    def _section(self, text: str, muted: bool = False) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("SectionLabelMuted" if muted else "SectionLabel")
        return lbl

    # ================================================================= #
    # AYON cascade -- Project -> Shot search -> Task (real data only)
    # ================================================================= #
    def _fill(self, combo: QComboBox, values: list[str]) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(values)
        combo.blockSignals(False)

    def _load_projects(self) -> None:
        if self.offline:
            ntasks = len(self.offline_cache.tasks())
            self.ref_status.setText(
                f"Offline — {ntasks} task(s) available offline (⭳ menu); "
                "or 'Open local file…'" if ntasks else
                "Offline — nothing copied for offline use yet (use ⭳ when "
                "online); 'Open local file…' still works")
            self.publish_btn.setEnabled(False)
            self.publish_note.setText(
                "Offline — takes are saved locally; publish needs AYON")
            # Cached AYON names: pick the REAL shot offline so the take lands
            # in the correct structure and can be published later.
            projects = self.shotdb.projects()
            if projects:
                nproj, nfold = self.shotdb.stats()
                self._fill(self.cb_project, projects)
                self.crumb.setText("  OFFLINE — pick a cached shot, or type one")
                self._log(f"Offline: {nfold} shot(s) across {nproj} project(s) "
                          "from the local cache — pick one, or type a shot "
                          "number for a free-form offline take")
                self._on_project(self._restore_last_project(projects))
            else:
                self.crumb.setText("  OFFLINE — type a shot number above, press Enter")
                self._log("Offline: no cached shots yet (sign in once on the "
                          "network to build the cache) — type a shot number; "
                          f"takes go to {self.config.save_root_path / 'offline'}")
            return   # the audio input opens from __init__ right after this
        try:
            projects = self.ayon.list_projects()
            self.shotdb.sync_projects(projects)
        except Exception:
            self._log_exc("Could not list projects")
            projects = []
        self._fill(self.cb_project, projects)
        self._start_shot_sync()
        if projects:
            self._log(f"Loaded {len(projects)} project(s)")
        else:
            self.crumb.setText("  no projects — check the log below")
        self._on_project(self._restore_last_project(projects))

    def _restore_last_project(self, projects: list) -> str:
        """Select the project used last time (if it still exists)."""
        last = self.config.last_project
        name = last if last in projects else (projects[0] if projects else "")
        if name:
            self.cb_project.blockSignals(True)
            self.cb_project.setCurrentIndex(self.cb_project.findText(name))
            self.cb_project.blockSignals(False)
            self.cb_project.lineEdit().setText(name)
            if name == last:
                self._log(f"Re-opened last project: {name}")
        return name

    # ---- offline shot cache --------------------------------------------- #
    def _start_shot_sync(self) -> None:
        """Cache EVERY project's shots for offline use (background)."""
        if self.offline or not self.shotdb.ok:
            return
        w = getattr(self, "_sync_worker", None)
        if w is not None and w.isRunning():
            return
        self._log("Syncing all projects' shots to the offline cache…")
        self._sync_worker = self._keep(ShotSyncWorker(self.ayon, self.shotdb))
        self._sync_worker.progress.connect(self._log)
        self._sync_worker.done.connect(self._on_shot_sync_done)
        self._sync_worker.start()

    def _on_shot_sync_done(self, projects: int, shots: int) -> None:
        self._log(f"✓ Offline cache updated: {shots} shot(s) across "
                  f"{projects} project(s) — available next time you work "
                  "without the network")

    def _on_project(self, project: str) -> None:
        self._current_project = project or ""
        if project and project != self.config.last_project:
            self.config.last_project = project
            self.config.save()
        for c in (self.cb_shot, self.cb_task, self.cb_ref_task, self.cb_ref_ver):
            c.blockSignals(True)
            c.clear()
            c.blockSignals(False)
        self._shot_id = ""
        self._shot_path = ""
        self._task_id = ""
        self._shot_tasks = []
        self._folders = {}
        if not project:
            return
        if self.offline:
            self._folders = self.shotdb.folders(project)
            if not self._folders:
                return
            self._log(f"{len(self._folders)} cached shot(s) for {project}")
            self._fill_shots()
            return
        # Online: the folder list of a big project can take seconds -- load
        # it in the background (this used to freeze the window). Show the
        # CACHED list instantly meanwhile so the dropdown is usable at once.
        cached = self.shotdb.folders(project)
        if cached:
            self._folders = cached
            self._fill_shots()
        self._start_folder_load(project)
        # Each project has its own AYON root -> check the drive for it.
        self._check_share(mount=self.config.share_auto_mount)

    def _start_folder_load(self, project: str) -> None:
        """Fetch the project's folders in the background; the answer keeps
        the currently picked shot (see _on_folders_loaded)."""
        self.cb_shot.lineEdit().setPlaceholderText(
            f"loading shots for {project}…")
        self._folder_gen = getattr(self, "_folder_gen", 0) + 1
        worker = FolderLoadWorker(self.ayon, project, self._folder_gen)
        worker.done.connect(self._on_folders_loaded)
        worker.failed.connect(self._on_folders_failed)
        # Keep a Python reference until each worker finishes (a QThread that
        # is garbage-collected while running crashes the process); finished
        # ones are pruned here.
        self._folder_workers = [w for w in getattr(self, "_folder_workers", [])
                                if w.isRunning()] + [worker]
        worker.start()

    def _on_folders_loaded(self, project: str, gen: int, folders: dict) -> None:
        # Ignore a stale answer if the user already switched project.
        if gen != getattr(self, "_folder_gen", 0) or \
                project != self._current_project:
            return
        self.shotdb.sync_folders(project, folders)
        keep = self.cb_shot.currentData()
        self._folders = folders
        self._fill_shots(keep=keep)
        self.cb_shot.lineEdit().setPlaceholderText("type to search shot…")
        self._log(f"Loaded {len(folders)} folders for {project} — type in the "
                  "shot dropdown to filter, pick one to load")
        if not folders:
            self.crumb.setText("  no folders — check the log below")

    def _on_folders_failed(self, project: str, gen: int, tb: str) -> None:
        if gen != getattr(self, "_folder_gen", 0):
            return
        self.cb_shot.lineEdit().setPlaceholderText("type to search shot…")
        self._log(f"✕ Could not list folders for {project}"
                  + (" — showing the cached list" if self._folders else "")
                  + f":\n{tb}")

    # ---- ⟳ Reload: re-read everything from AYON, keep the selection ----- #
    def _reload_all(self) -> None:
        if self.offline:
            return
        if self._busy_recording():
            self._log("Stop recording first, then reload")
            return
        project = self._current_project
        self._log("⟳ Reloading projects, shots and tasks from AYON…")
        try:
            projects = self.ayon.list_projects()
            self.shotdb.sync_projects(projects)
        except Exception:
            self._log_exc("Reload: could not list projects")
            return
        self._fill(self.cb_project, projects)
        if project not in projects:
            if project:
                self._log(f"⚠ {project} is no longer on the server")
            self._on_project(self._restore_last_project(projects))
            return
        self.cb_project.blockSignals(True)
        self.cb_project.setCurrentIndex(self.cb_project.findText(project))
        self.cb_project.blockSignals(False)
        self.cb_project.lineEdit().setText(project)
        self._start_folder_load(project)        # keeps the picked shot
        if self._shot_id:
            self._reload_tasks()
        self._check_share(mount=False)          # just refresh the ● Drive dot

    def _reload_tasks(self) -> None:
        """Re-fetch the current shot's tasks; keep the task if it still exists."""
        project = self._current_project
        keep_task = self.cb_task.currentText()
        keep_ref = self.cb_ref_task.currentText()
        try:
            tasks = self.ayon.tasks_for_folder(project, self._shot_id)
            self.shotdb.sync_tasks(project, self._shot_id, tasks)
        except Exception:
            self._log_exc(f"Reload: could not fetch tasks for {self._shot_path}")
            return
        self._shot_tasks = tasks
        for combo in (self.cb_task, self.cb_ref_task):
            combo.blockSignals(True)
            combo.clear()
            for t in tasks:
                combo.addItem(t.name, t.id)
            combo.blockSignals(False)
        ri = self.cb_ref_task.findText(keep_ref)
        if ri >= 0:
            self.cb_ref_task.blockSignals(True)
            self.cb_ref_task.setCurrentIndex(ri)
            self.cb_ref_task.blockSignals(False)
        self._refresh_ref_versions()
        self._log(f"✓ {len(tasks)} task(s) for {self._shot_path}")
        if not tasks:
            self._task_id = ""
            self.crumb.setText(f"  {self._shot_path} / (no tasks)")
            return
        ti = self.cb_task.findText(keep_task)
        if ti >= 0:
            # same task: only refresh what it shows (takes stay selected)
            self.cb_task.blockSignals(True)
            self.cb_task.setCurrentIndex(ti)
            self.cb_task.blockSignals(False)
            self._task_id = self.cb_task.itemData(ti) or ""
            self._refresh_takes()
            self._refresh_published()
        else:
            self._log(f"⚠ task '{keep_task}' is gone — switched to "
                      f"{self.cb_task.itemText(0)}")
            self._on_task_selected(0)

    def _fill_shots(self, keep=None) -> None:
        """Repopulate the shot dropdown; `keep` re-selects the user's pick
        (so a background refresh never throws away a chosen shot)."""
        self.cb_shot.blockSignals(True)
        typed = self.cb_shot.lineEdit().text()
        self.cb_shot.clear()
        for path in sorted(self._folders):
            self.cb_shot.addItem(path, (self._folders[path], path))
        idx = -1
        if keep:
            idx = self.cb_shot.findData(keep)
        self.cb_shot.setCurrentIndex(idx)     # nothing selected until the user picks
        if idx < 0:
            self.cb_shot.lineEdit().setText(typed if keep is None else "")
        self.cb_shot.blockSignals(False)

    def _on_shot_selected(self, idx: int) -> None:
        data = self.cb_shot.itemData(idx)
        if not data:
            return
        self._shot_id, self._shot_path = data
        project = self._current_project
        # ALL real tasks for this folder -> Task and Reference dropdowns.
        if self.offline:
            self._shot_tasks = [
                AyonTask(name=n, id=tid, task_type=tt)
                for n, tid, tt in self.shotdb.tasks(project, self._shot_id)]
            if not self._shot_tasks:      # never synced this shot's tasks
                self._shot_tasks = [AyonTask(name="recording", id="", task_type="")]
        else:
            try:
                self._shot_tasks = self.ayon.tasks_for_folder(project, self._shot_id)
                self.shotdb.sync_tasks(project, self._shot_id, self._shot_tasks)
            except Exception:
                self._log_exc(f"Could not fetch tasks for {self._shot_path}")
                self._shot_tasks = []
        for combo in (self.cb_task, self.cb_ref_task):
            combo.blockSignals(True)
            combo.clear()
            for t in self._shot_tasks:
                combo.addItem(t.name, t.id)
            combo.blockSignals(False)
        if self.offline:
            # Reference offline = only what has a local copy for THIS shot.
            cached = sorted({e["task"] for e in self.offline_cache.entries_for(
                project, self._shot_id)}, key=str.lower)
            self.cb_ref_task.blockSignals(True)
            self.cb_ref_task.clear()
            for name in cached:
                self.cb_ref_task.addItem(name, name)
            self.cb_ref_task.blockSignals(False)
            self.ref_status.setText(
                f"{len(cached)} task(s) available offline for this shot"
                if cached else "No offline copies for this shot")
        self._refresh_ref_versions()
        if self._shot_tasks:
            # Default to the task takes are actually recorded into; fall back
            # to the first task when this shot has no recording task.
            names = [t.name.lower() for t in self._shot_tasks]
            idx = next((names.index(d) for d in DEFAULT_TASKS if d in names), 0)
            if idx:
                self.cb_task.blockSignals(True)
                self.cb_task.setCurrentIndex(idx)
                self.cb_task.blockSignals(False)
            self._on_task_selected(idx)
        else:
            self._task_id = ""
            self.crumb.setText(f"  {self._shot_path} / (no tasks)")

    def _on_task_selected(self, idx: int) -> None:
        self._task_id = self.cb_task.itemData(idx) or ""
        project = self._current_project
        task = self.cb_task.currentText()
        if not (project and self._shot_path and task):
            return
        seq, scene, shot_name = self._derive_ctx(self._shot_path, self._shot_path)
        self.crumb.setText(f"  {shot_name} / {task}")
        self.takes.set_context(project, seq, scene, shot_name, task)
        self._log(f"Task context → {self.takes.task_dir}")
        # The input is NOT re-opened on task switch -- only take data changes.
        self.selected_take = None
        self.comments.clear()
        self._refresh_takes()
        # Published list always follows the selected shot/task.
        self._refresh_published()

    # ---- offline: typed shot/task -> {save_root}/offline/{shot}/{task} ---- #
    @staticmethod
    def _fs_safe(text: str) -> str:
        """Typed shot/task -> filesystem-safe name ('6010 chrn/1010' -> '6010_chrn_1010')."""
        return re.sub(r"[^\w.\-]+", "_", (text or "").strip()).strip("._")

    def _offline_task_typed(self) -> None:
        """Typed task name offline: re-apply whichever context is active."""
        if not self.offline:
            return
        if self._shot_path:              # a cached shot is selected
            self._on_task_selected(self.cb_task.currentIndex())
        else:
            self._apply_offline_context()

    def _apply_offline_context(self) -> None:
        """Free-form offline take: {save_root}/offline/{shot}/{task}."""
        if not self.offline:
            return
        typed = self.cb_shot.lineEdit().text().strip()
        # A cached shot typed exactly (any case) was already picked by the
        # searchable-dropdown handler, which runs first -- nothing to do.
        if self.cb_shot.findText(typed, Qt.MatchFixedString) >= 0:
            return
        shot = self._fs_safe(typed)
        if not shot:
            return
        self._shot_path = ""             # free-form: not a cached shot
        self._shot_id = ""
        task = self._fs_safe(self.cb_task.currentText()) or "recording"
        self.crumb.setText(f"  OFFLINE · {shot} / {task}")
        self.takes.set_context("offline", "", "", shot, task)
        self._log(f"Offline context → {self.takes.task_dir}")
        self.selected_take = None
        self.comments.clear()
        self._refresh_takes()
        # Published list always follows the selected shot/task.
        self._refresh_published()

    @staticmethod
    def _derive_ctx(shot_name: str, shot_path: str) -> tuple[str, str, str]:
        """Split the AYON folder path into (sequence, scene, shot) for the local
        save structure. Falls back to '_' for missing hierarchy levels."""
        parts = [p for p in shot_path.replace("\\", "/").split("/") if p]
        shot = parts[-1] if parts else shot_name
        scene = parts[-2] if len(parts) >= 2 else "_"
        seq = parts[-3] if len(parts) >= 3 else "_"
        return seq, scene, shot

    # ================================================================= #
    # Audio input -- opened ONCE, kept live (meters always move)
    # ================================================================= #
    def _busy_recording(self) -> bool:
        """True while a take records or is still being saved: the input
        must not be touched then (it would cut the take)."""
        fin = self._fin_worker
        return self._recording or (fin is not None and fin.isRunning())

    def _fill_host_apis(self) -> None:
        apis = dev_mod.host_apis()
        self.cb_api.blockSignals(True)
        self.cb_api.clear()
        for api in apis:
            legacy = api in dev_mod.LEGACY_APIS
            self.cb_api.addItem(api + ("  (resamples)" if legacy else ""), api)
        want = self.config.host_api or dev_mod.default_host_api()
        i = self.cb_api.findData(want)
        self.cb_api.setCurrentIndex(i if i >= 0 else 0)
        self.cb_api.blockSignals(False)
        if not apis:
            self.input_status.setText("✕ no audio inputs found")
            self._log("✕ No audio input devices found — plug the receiver "
                      "in and press ⟳")
            self._build_strips()
            self._update_armed()
            return
        self._fill_devices()

    def _current_api(self) -> str:
        return self.cb_api.currentData() or ""

    def _fill_devices(self) -> None:
        api = self._current_api()
        devs = dev_mod.input_devices(api)
        self.cb_device.blockSignals(True)
        self.cb_device.clear()
        for d in devs:
            self.cb_device.addItem(f"{d.name}  ({d.channels} ch)", d)
        pick = None
        if self.config.audio_device:
            pick = dev_mod.find_device(self.config.audio_device, api)
            if pick is None:
                self._log(f"⚠ '{self.config.audio_device}' is not connected "
                          f"({api}) — using the default input")
        pick = pick or dev_mod.default_device(api)
        idx = next((i for i, d in enumerate(devs)
                    if pick is not None and d.index == pick.index), 0)
        self.cb_device.setCurrentIndex(idx)
        self.cb_device.blockSignals(False)
        is_wasapi = dev_mod.supports_exclusive(api)
        self.chk_exclusive.blockSignals(True)
        self.chk_exclusive.setChecked(bool(self.config.wasapi_exclusive)
                                      and is_wasapi)
        self.chk_exclusive.blockSignals(False)
        self.chk_exclusive.setVisible(is_wasapi)
        self._fill_quality()
        self._open_input()

    def _current_device(self) -> Optional[dev_mod.InputDevice]:
        return self.cb_device.currentData()

    def _fill_quality(self) -> None:
        """Rates the device REALLY accepts; depth + file format from config."""
        dev = self._current_device()
        excl = self.chk_exclusive.isChecked()
        self.cb_rate.blockSignals(True)
        self.cb_rate.clear()
        if dev is not None:
            rates = dev_mod.supported_rates(dev, excl) or \
                [int(dev.default_rate or 48000)]
            native = int(dev.default_rate or 0)
            for r in rates:
                self.cb_rate.addItem(
                    f"{r / 1000:g} kHz" + ("  (native)" if r == native else ""), r)
            best = dev_mod.best_rate(dev, int(self.config.samplerate), excl)
            i = self.cb_rate.findData(best)
            self.cb_rate.setCurrentIndex(i if i >= 0 else 0)
        self.cb_rate.blockSignals(False)
        for combo, value in ((self.cb_depth, self.config.sample_format),
                             (self.cb_format, self.config.file_format)):
            combo.blockSignals(True)
            i = combo.findData(value)
            combo.setCurrentIndex(i if i >= 0 else 0)
            combo.blockSignals(False)

    def _open_input(self) -> None:
        """(Re)open the input with the current settings.

        Runs on the GUI thread ON PURPOSE: PortAudio isn't thread-safe and
        WASAPI objects belong to the thread that created them. v1.0.0 opened
        on a worker and closed here, which corrupted memory and crashed the
        app later (on Publish). Opening takes milliseconds.
        """
        if self._busy_recording():
            return
        self._close_input()
        dev = self._current_device()
        if dev is None:
            self.input_status.setText("✕ no input")
            self._build_strips()
            self._update_armed()
            return
        rate = int(self.cb_rate.currentData() or self.config.samplerate)
        subtype = self.cb_depth.currentData() or "PCM_24"
        inp = rec_mod.AudioInput(dev, rate, subtype,
                                 exclusive=self.chk_exclusive.isChecked(),
                                 latency=self.config.latency or "high")
        if not inp.open():
            self.input_status.setText("✕ input failed — see log")
            self._set_path_badge(None)
            self._log(f"✕ Could not open {dev.name}: {inp.error}\n   Another "
                      "app may hold it in exclusive mode, or the rate/format "
                      "isn't supported — try another Driver.")
            self._build_strips()
            self._update_armed()
            return
        self._input = inp
        if inp.error:                          # e.g. exclusive fell back
            self._log(f"⚠ {inp.error}")
        self._log(f"♪ Input: {dev.name} ({dev.hostapi}) · "
                  f"{inp.samplerate / 1000:g} kHz · "
                  f"{dev_mod.SAMPLE_FORMATS.get(inp.subtype, inp.subtype)} file · "
                  f"{inp.channels} ch · captured as {inp.dtype} · "
                  f"path: {inp.path_label}")
        self._set_path_badge(inp)
        self._build_strips()
        self._update_armed()

    def _close_input(self) -> None:
        inp, self._input = self._input, None
        if inp is not None:
            inp.close()

    def _refuse_while_recording(self) -> bool:
        if self._busy_recording():
            self._log("Input settings are locked while a take records or "
                      "saves — change them after it finishes")
            self._fill_quality()
            return True
        return False

    def _save_input_config(self) -> None:
        dev = self._current_device()
        self.config.host_api = self._current_api()
        self.config.audio_device = dev.name if dev else ""
        self.config.samplerate = int(self.cb_rate.currentData() or 48000)
        self.config.sample_format = self.cb_depth.currentData() or "PCM_24"
        self.config.file_format = self.cb_format.currentData() or "WAV"
        self.config.wasapi_exclusive = self.chk_exclusive.isChecked()
        self.config.save()

    def _on_api_changed(self, _idx: int) -> None:
        if self._refuse_while_recording():
            return
        self.config.host_api = self._current_api()
        self.config.save()
        self._fill_devices()

    def _on_device_changed(self, _idx: int) -> None:
        if self._refuse_while_recording():
            return
        self._fill_quality()
        self._save_input_config()
        self._open_input()

    def _on_quality_changed(self, _idx: int) -> None:
        if self._refuse_while_recording():
            return
        if (self.cb_format.currentData() == "FLAC"
                and self.cb_depth.currentData() == "FLOAT"):
            self._log("FLAC cannot store 32-bit float — switched the file "
                      "format to WAV")
            self.cb_format.setCurrentIndex(self.cb_format.findData("WAV"))
        reopen = (int(self.cb_rate.currentData() or 0) != getattr(
            self._input, "samplerate", -1) or
            (self.cb_depth.currentData() == "FLOAT") !=
            (getattr(self._input, "subtype", "") == "FLOAT"))
        self._save_input_config()
        if reopen:
            self._open_input()          # rate / sample type live on the stream
        else:
            if self._input is not None:
                self._input.subtype = self.cb_depth.currentData() or "PCM_24"
            self._update_armed()

    def _on_exclusive_toggled(self, _on: bool) -> None:
        if self._refuse_while_recording():
            return
        self._fill_quality()
        self._save_input_config()
        self._open_input()

    def _rescan_devices(self) -> None:
        """Re-enumerate devices (a receiver plugged in after launch). PortAudio
        can only do that with every stream closed."""
        if self._refuse_while_recording():
            return
        self._close_input()
        dev_mod.rescan()
        self._log("⟳ Re-scanned audio devices")
        self._fill_host_apis()

    # ---- tracks: role + channel ----------------------------------------- #
    def _n_channels(self) -> int:
        if self._input is not None:
            return self._input.channels
        dev = self._current_device()
        return max(1, min(dev.channels, 16)) if dev else 1

    def _build_strips(self) -> None:
        """(Re)build the track strips from config.audio_tracks."""
        for s in self._strips:
            self._dispose_strip(s)
        self._strips = []
        n = self._n_channels()
        seen: set = set()
        for spec in self.config.audio_tracks or [{"role": "mic_main",
                                                  "channel": 0}]:
            role = _slug(str(spec.get("role") or "mic_main"))
            if role in seen:
                continue
            seen.add(role)
            ch = int(spec.get("channel", 0))
            if ch >= n:
                self._log(f"⚠ track '{role}': input {ch + 1} doesn't exist on "
                          f"this device ({n} ch) — using input 1")
                ch = 0
            self._add_strip(role, ch)
        self._save_tracks_config()

    def _add_strip(self, role: str, channel: int) -> TrackStrip:
        color = TRACK_COLORS[len(self._strips) % len(TRACK_COLORS)]
        strip = TrackStrip(role, channel, self._n_channels(), color)
        strip.role_changed.connect(self._on_strip_role)
        strip.channel_changed.connect(self._on_strip_channel)
        strip.remove_requested.connect(self._on_strip_remove)
        self.strips_box.addWidget(strip)
        self._strips.append(strip)
        return strip

    def _add_track_clicked(self) -> None:
        if self._refuse_while_recording():
            return
        used = {s.role for s in self._strips}
        role = next((r for r in TRACK_ROLES if r not in used),
                    f"track_{len(self._strips) + 1}")
        used_ch = {s.channel for s in self._strips}
        ch = next((c for c in range(self._n_channels()) if c not in used_ch), 0)
        self._add_strip(role, ch)
        self._save_tracks_config()
        self._log(f"＋ Track '{role}' on input {ch + 1}")

    def _on_strip_role(self, strip: TrackStrip, role: str) -> None:
        """Roles are storage keys: they must be unique. A clash SWAPS the two
        roles (same rule as ssd_abhinaya's camera roles)."""
        new = _slug(role) if role else ""
        old = getattr(strip, "_committed_role", None) or new
        if not new:
            strip.set_role(old)
            return
        other = next((s for s in self._strips
                      if s is not strip and _slug(s.role) == new), None)
        if other is not None:
            other.set_role(old)
            other._committed_role = old
            self._log(f"Role '{new}' was taken — swapped with the other track "
                      f"(now '{old}')")
        strip.set_role(new)
        strip._committed_role = new
        self._save_tracks_config()

    def _on_strip_remove(self, strip: TrackStrip) -> None:
        if self._refuse_while_recording():
            return
        if len(self._strips) <= 1:
            self._log("A take needs at least one track")
            return
        self._strips.remove(strip)
        self._dispose_strip(strip)
        self._save_tracks_config()

    def _dispose_strip(self, strip: TrackStrip) -> None:
        """Remove a strip SAFELY: take it out of the layout, hide it and let
        Qt delete it on the next event-loop pass. No setParent(None) -- that
        hands ownership to Python, and Python + Qt then both delete it (a
        double free = native crash), especially from inside the strip's own
        button click."""
        self.strips_box.removeWidget(strip)
        strip.hide()
        strip.deleteLater()

    def _on_strip_channel(self, _strip: TrackStrip, _channel: int) -> None:
        self._save_tracks_config()

    def _save_tracks_config(self) -> None:
        for s in self._strips:
            s._committed_role = _slug(s.role)
        self.config.audio_tracks = [{"role": _slug(s.role), "channel": s.channel}
                                    for s in self._strips]
        self.config.save()
        self._update_armed()

    def _update_armed(self) -> None:
        n = len(self._strips) if self._input is not None else 0
        fmt = ""
        if self._input is not None:
            fmt = (f" · {self._input.samplerate / 1000:g} kHz · "
                   f"{dev_mod.SAMPLE_FORMATS.get(self.cb_depth.currentData(), '')}"
                   f" · {self.cb_format.currentData() or 'WAV'}")
        self.armed_label.setText(
            f"● {n} track{'s' if n != 1 else ''} armed{fmt}")
        self._update_disk_left()

    def _bytes_per_second(self) -> float:
        rate = getattr(self._input, "samplerate", 48000)
        bits = {"PCM_16": 16, "PCM_24": 24, "FLOAT": 32}.get(
            self.cb_depth.currentData() or "PCM_24", 24)
        ratio = 0.55 if self.cb_format.currentData() == "FLAC" else 1.0
        return rate * bits / 8 * max(1, len(self._strips)) * ratio

    def _update_disk_left(self) -> None:
        """'≈ 38 h left on C:' -- recording time left at this format."""
        try:
            root = self.config.save_root_path
            probe = root if root.exists() else Path(root.anchor or "/")
            free = shutil.disk_usage(probe).free
            secs = free / self._bytes_per_second()
            txt = (f"≈ {secs / 3600:,.0f} h" if secs >= 3600 * 2
                   else f"≈ {secs / 60:,.0f} min")
            self.disk_label.setText(f"{txt} of recording left on "
                                    f"{root.anchor or root}")
            self.disk_label.setStyleSheet(
                f"color:{'#f2555a' if secs < 1800 else '#6b737a'};"
                "font-size:11px;")
        except Exception:
            self.disk_label.setText("")

    def _set_path_badge(self, inp) -> None:
        if inp is None:
            self.path_badge.setText("")
            return
        direct = inp.path_label.startswith("direct")
        col = "#5fd47c" if direct else "#e0b12f"
        self.path_badge.setText(f"◆ {inp.path_label}")
        self.path_badge.setStyleSheet(
            f"color:{col};font-size:11px;font-weight:600;"
            f"border:1px solid {col};border-radius:9px;padding:1px 8px;")

    # ================================================================= #
    # Recording (always a NEW take -- never overwrites)
    # ================================================================= #
    def _toggle_record(self) -> None:
        if self._recording:
            self._stop_record()
            return
        fin = self._fin_worker
        if fin is not None and fin.isRunning():
            self._log("Still saving the previous take — record again when it "
                      "finishes")
            return
        if self.takes.task_dir is None:
            self._log("Type a shot number above and press Enter first"
                      if self.offline else "Pick a Project → Shot → Task first")
            return
        if self._input is None or not self._input.is_open:
            self._log("✕ No audio input is open — pick an Input above (or ⟳ "
                      "after plugging the mic in)")
            return
        if not self._strips:
            self._log("✕ No tracks — add one with ＋ Add track")
            return
        self._start_record()

    def _start_record(self) -> None:
        take = self.takes.new_take(comments=self.comments.toPlainText(),
                                   status="view")
        take.samplerate = self._input.samplerate
        take.sample_format = self._input.subtype
        take.file_format = self.cb_format.currentData() or "WAV"
        take.device = self._input.device.name
        take.host_api = self._input.device.hostapi
        self.selected_take = take
        ext = dev_mod.FILE_FORMATS.get(take.file_format, ".wav")
        specs = [rec_mod.TrackSpec(_slug(s.role), s.channel,
                                   self.takes.track_target_path(take, s.role, ext))
                 for s in self._strips]
        # The epoch (t=0) is placed `countdown_secs` in the FUTURE. The input
        # is already running, so samples before t=0 are simply discarded and
        # the take starts cleanly on "1" -- no start-up click or cough.
        count_in = max(0, int(self.config.countdown_secs))
        epoch = time.perf_counter() + count_in
        try:
            self._input.start_take(specs, epoch, file_format=take.file_format)
        except Exception as exc:
            self._log(f"✕ Could not start {take.take}: {exc}")
            try:
                self.takes.set_status(take, "reject")
                self.takes.set_comments(take, f"auto: failed to start ({exc})")
            except Exception:
                pass
            self._refresh_takes()
            return
        self.takes.save()
        self._recording = True
        self._rec_t0 = epoch
        self._countdown_until = epoch if count_in else 0.0
        for s in self._strips:
            s.set_recording(True)
        self.record_btn.set_recording(True)
        self._set_tc_recording(True)
        self._refresh_takes()          # the new take shows in the list at once
        for sp in specs:
            self._log(f"  ● {sp.role} (input {sp.channel + 1}) → {sp.path.name}")
        if count_in:
            self._show_countdown(count_in)
            self._log(f"● {take.take}: take starts in {count_in}s "
                      f"({take.format_label})")
        else:
            self._log(f"● Recording {take.take} ({take.format_label})")

    def _stop_record(self) -> None:
        """Stop instantly; the files are closed in the background."""
        take = self.selected_take
        aborted = bool(self._countdown_until) and \
            time.perf_counter() < self._countdown_until
        writer = self._input.detach_take() if self._input is not None else None
        self._recording = False
        self._countdown_until = 0.0
        self.countdown_label.hide()
        for s in self._strips:
            s.set_recording(False)
        self.record_btn.set_recording(False)
        self._set_tc_recording(False)
        if take is None or writer is None:
            self._refresh_takes()
            return
        if aborted:
            self._log(f"⏹ {take.take} stopped during the count-in — nothing "
                      "was recorded")
        self.record_btn.setEnabled(False)
        self.record_btn.setToolTip("Saving the previous take…")
        self._fin_worker = self._keep(TakeFinalizeWorker(take, writer))
        self._fin_worker.done.connect(
            lambda t, res, ab=aborted: self._on_take_finalized(t, res, ab))
        self._fin_worker.start()

    def _on_take_finalized(self, take: TakeData, res,
                           aborted: bool = False) -> None:
        self.record_btn.setEnabled(True)
        self.record_btn.setToolTip("")
        produced = 0
        if res is not None and res.error:
            self._log(f"✕ {take.take}: writing failed — {res.error} (disk "
                      "full or save folder gone?)")
        for tr in (res.tracks if res is not None else []):
            if tr.frames <= 0 or not tr.path.exists():
                if tr.path.exists():
                    try:
                        tr.path.unlink()     # 0-sample file: count-in only
                    except OSError:
                        pass
                if not aborted:
                    self._log(f"  ✕ {tr.role}: no audio was written")
                continue
            produced += 1
            dur = tr.frames / float(res.samplerate or 1)
            try:
                self.takes.register_track(
                    take, tr.role, tr.path, channel=tr.channel,
                    offset=res.offset, duration=dur, peak_db=tr.peak_db,
                    clipped=tr.clipped, save=False)
            except Exception:
                self._log_exc(f"Could not register {tr.role}")
                continue
            self._report_track(tr, dur, res)
        if produced == 0:
            # Nothing usable (e.g. stopped during the count-in): keep the
            # folder, but make sure the empty take can never be published.
            try:
                self.takes.set_status(take, "reject")
                if not take.comments:
                    self.takes.set_comments(
                        take, "auto: no audio (stopped before recording)")
            except Exception:
                pass
            self._refresh_takes()
            self._log(f"⏹ {take.take}: no audio saved — marked Reject")
            return
        try:
            self.takes.save()
        except Exception:
            self._log_exc("Could not save the manifest")
        if (sys.platform == "win32" and res.tracks and res.tracks[0].checked
                and not res.tracks[0].bit_perfect and res.tracks[0].peak > 0
                and not getattr(self, "_mixer_hint_shown", False)):
            self._mixer_hint_shown = True
            self._log("  ℹ Windows processed this audio before the app got it "
                      "(shared mode). For the cleanest signal: Sound settings → "
                      "Recording → your mic → Properties: turn Enhancements "
                      "off; its Levels slider scales the audio digitally. For "
                      "untouched audio pick Driver 'Windows WDM-KS' or tick "
                      "Exclusive (bit-perfect).")
        if res.xruns:
            self._log(f"  ⚠ {res.xruns} input overflow(s) during {take.take} — "
                      "the computer couldn't keep up and audio was lost "
                      "(clicks). Close heavy apps, or use a different Driver.")
        self._refresh_takes()
        self._log(f"✓ Saved {take.take}: {produced} track(s), "
                  f"{fmt_duration(take.duration)} · {take.format_label}")

    def _report_track(self, tr, dur: float, res) -> None:
        """One line per track + the warnings a non-audio-engineer needs."""
        size = tr.path.stat().st_size / 1e6
        path = ("" if tr.peak <= 0 else          # silence proves nothing
                "bit-perfect from the driver" if tr.bit_perfect else
                "processed by the OS audio engine")
        self._log(f"  ♪ {tr.role}: {tr.path.name} ({size:.1f} MB, "
                  f"{fmt_duration(dur)}, peak {fmt_db(tr.peak_db)}"
                  + (f", {path}" if path else "") + ")")
        if tr.clipped:
            self._log(f"  ⚠⚠ {tr.role}: CLIPPED — {tr.clipped} sample(s) hit "
                      "full scale. That distortion can't be removed; lower "
                      "the gain on the mic/receiver and record again.")
        elif tr.peak_db > -3:
            self._log(f"  ⚠ {tr.role}: peaks at {tr.peak_db:.1f} dBFS — very "
                      "close to clipping. Aim for −12 to −6.")
        if tr.peak <= 0:
            hint = ("macOS may be blocking the microphone: System Settings → "
                    "Privacy & Security → Microphone → allow this app."
                    if sys.platform == "darwin" else
                    "Wrong input channel, transmitter off/muted, or "
                    "another app has the device in exclusive mode.")
            self._log(f"  ⚠⚠ {tr.role}: DIGITAL SILENCE (every sample is 0). "
                      + hint)
        elif tr.peak_db < -40:
            self._log(f"  ⚠ {tr.role}: very quiet (peak {tr.peak_db:.1f} dBFS) "
                      "— raise the gain at the source; boosting later raises "
                      "the noise too.")

    # ================================================================= #
    # Facial recording: drop a video / zip onto a take
    # ================================================================= #
    def _facial_target(self, replace: bool = False
                       ) -> tuple[Optional[TakeData], str]:
        """(take the drop goes to -- None = a NEW take, reason text).

        A take that already HAS a video is not overwritten: the drop moves
        on to the next take without one (else a new take). `replace`
        (Shift held while dropping) targets the selected take anyway."""
        if self.drop_zone.new_take.isChecked():
            return None, "a NEW take (video only)"
        t = self.selected_take
        if t is None:
            return None, "a NEW take (no takes yet)"
        if getattr(t, "published_version", 0):
            return None, (f"a NEW take ({t.take} is published and locked)")
        if self._recording and t is self.selected_take:
            return None, "— wait: a take is recording"
        if t.facial is not None and not replace:
            nxt = self._next_free_take(t)
            name = t.take.replace("_", " ")
            if nxt is not None:
                return nxt, (f"{nxt.take.replace('_', ' ')} (next take — "
                             f"{name} already has a video)")
            return None, f"a NEW take ({name} already has a video)"
        return t, t.take.replace("_", " ")

    def _next_free_take(self, after: TakeData) -> Optional[TakeData]:
        """First take after `after` (by number) with no video that is not
        published and not rejected -- None if there is none."""
        for t in sorted(self.takes.manifest.takes, key=lambda x: x.number):
            if (t.number > after.number and t.facial is None
                    and not getattr(t, "published_version", 0)
                    and t.status != "reject"):
                return t
        return None

    def _update_drop_target(self) -> None:
        if not hasattr(self, "drop_zone"):
            return
        if self.takes.task_dir is None:
            self.drop_zone.set_target_text("Pick a Project → Shot → Task first")
            return
        take, why = self._facial_target()
        line = f"→ attaches to {why}"
        sel = self.selected_take
        if (sel is not None and sel.facial is not None
                and not self.drop_zone.new_take.isChecked()):
            f = sel.facial
            sync = (f"mic synced {f.sync_offset:+.3f} s ✓" if f.synced
                    else f.sync_note)
            line = (f"{sel.take.replace('_', ' ')} has: {Path(f.path).name} "
                    f"({f.label}, {fmt_duration(f.duration)}) · {sync}\n"
                    f"Next drop → {why} · Shift+drop replaces it")
        self.drop_zone.set_target_text(line)

    def _browse_facial(self) -> None:
        start = self.config.last_reference_dir or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the facial recording", start,
            "Video or zip (*.mp4 *.mov *.m4v *.mkv *.avi *.mxf *.webm *.zip);;"
            "All files (*)")
        if path:
            self.config.last_reference_dir = str(Path(path).parent)
            self.config.save()
            self._on_facial_dropped([path])

    def _on_facial_dropped(self, paths: list) -> None:
        if self.takes.task_dir is None:
            self._log("Pick a Project → Shot → Task first, then drop the video")
            return
        if self._recording:
            self._log("Stop recording first, then drop the facial video")
            return
        w = getattr(self, "_facial_worker", None)
        if w is not None and w.isRunning():
            self._log("Still importing the previous facial video — wait for it")
            return
        src = Path(paths[0])
        if len(paths) > 1:
            self._log(f"⚠ {len(paths)} files dropped — importing only "
                      f"{src.name} (one facial video per take)")
        # Shift held while dropping = REPLACE the selected take's video;
        # otherwise a take that already has one passes on to the next take.
        replace = bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)
        take, why = self._facial_target(replace=replace)
        if (self.selected_take is not None and take is not self.selected_take
                and self.selected_take.facial is not None and not replace):
            self._log(f"↪ {self.selected_take.take} already has a video — "
                      f"this one goes to {why}")
        if take is not None and take.facial is not None:
            if QMessageBox.question(
                    self, "Replace facial video",
                    f"{take.take} already has {Path(take.facial.path).name}.\n"
                    f"Replace it with {src.name}?") != QMessageBox.Yes:
                return
        if take is None:
            take = self.takes.new_take(comments=self.comments.toPlainText(),
                                       status="view")
            take.device, take.host_api = "", ""
            self.takes.save()
            self._log(f"＋ {take.take} created for the facial video")
        self.selected_take = take
        # the clean mic to sync with: mic_main, else the take's first track
        mic_role = ("mic_main" if "mic_main" in take.tracks
                    else next(iter(take.tracks), ""))
        mic = (self.takes.task_dir / take.tracks[mic_role].path
               if mic_role else None)
        self._log(f"🎥 Importing {src.name} → {take.take}"
                  + (f" (will auto-sync {mic_role})" if mic_role else ""))
        self.drop_zone.set_busy(True, f"importing {src.name}", 0.0)
        self.record_btn.setEnabled(False)
        self._facial_worker = self._keep(FacialImportWorker(
            src, take.take, self.takes.task_dir / take.take,
            self.takes.task_dir, mic, mic_role))
        self._facial_worker.progress.connect(
            lambda msg, frac: self.drop_zone.set_busy(True, msg, frac))
        self._facial_worker.done.connect(self._on_facial_done)
        self._facial_worker.failed.connect(self._on_facial_failed)
        self._facial_worker.start()
        self._refresh_takes()

    def _on_facial_done(self, take_name: str, entry) -> None:
        self.drop_zone.set_busy(False)
        self.record_btn.setEnabled(True)
        take = self.takes.manifest.find(take_name)
        if take is None:            # task switched meanwhile
            self._log(f"⚠ {take_name} is no longer loaded — the video is on "
                      "disk; switch back to that task to see it")
            return
        take.facial = entry
        if not take.tracks:         # video-only take: its length is the video's
            take.duration = entry.duration
        self.takes.save()
        self._log(f"✓ {take.take}: facial video {Path(entry.path).name} — "
                  f"{entry.label}, {fmt_duration(entry.duration)}, "
                  f"{len(entry.files)} file(s)"
                  + ("" if entry.has_audio else ", no sound in the video"))
        if entry.synced:
            self._log(f"  ♪ auto-sync: {entry.synced_to} starts "
                      f"{entry.sync_offset:+.3f} s after the video "
                      f"(confidence {entry.sync_confidence:g}) — the review "
                      "video will carry the clean mic in sync")
        elif entry.sync_note:
            self._log(f"  ⚠ {entry.sync_note}")
        if not tools.available():
            self._log("  ⚠ ffmpeg not found — the video is attached, but "
                      "thumbnail, sync and the review mp4 need ffmpeg")
        self._refresh_takes()
        if self.stack.currentIndex() == 1:
            self._refresh_review()

    def _on_facial_failed(self, take_name: str, msg: str) -> None:
        self.drop_zone.set_busy(False)
        self.record_btn.setEnabled(True)
        self._log(f"✕ Facial import into {take_name} failed: {msg}")
        take = self.takes.manifest.find(take_name)
        # a take created just for this video, now empty -> mark it Reject
        if (take is not None and not take.tracks and take.facial is None
                and not take.published_version):
            try:
                self.takes.set_status(take, "reject")
                self.takes.set_comments(take, "auto: facial import failed")
            except Exception:
                pass
        self._refresh_takes()

    # ================================================================= #
    # Take dock
    # ================================================================= #
    def _refresh_takes(self) -> None:
        if not self._recording:
            # Self-heal: pick up take files on disk that missed registration.
            try:
                for line in self.takes.reconcile_take_files():
                    self._log(f"♻ {line}")
            except Exception:
                self._log_exc("Take file reconcile failed")
        takes = self.takes.manifest.takes
        self.take_count.setText(f"{len(takes)} recorded")
        self.next_take_spin.blockSignals(True)
        self.next_take_spin.setValue(self.takes.manifest.next_take_number())
        self.next_take_spin.setEnabled(self.takes.task_dir is not None
                                       and not self._busy_recording())
        self.next_take_spin.blockSignals(False)
        nxt = (self.takes.manifest.next_take_name().replace("_", " ")
               if self.takes.task_dir is not None else "—")
        self.next_take_label.setText(
            f"NOW  {self.selected_take.take.replace('_', ' ')}"
            if self._recording and self.selected_take else f"NEXT  {nxt}")

        self.cb_take.blockSignals(True)
        self.cb_take.clear()
        for t in takes:
            lbl, _ = _STATUS_META[t.status]
            self.cb_take.addItem(f"Take {t.number:02d} — {lbl.title()}", t.take)
        if takes:
            # KEEP the operator's selection (a refresh used to jump back to
            # the last take -- so a status change or a dropped video could
            # land on the wrong take). New take / nothing chosen -> the last.
            cur = self.selected_take
            idx = next((i for i, t in enumerate(takes)
                        if cur is not None and t.take == cur.take),
                       len(takes) - 1)
            self.cb_take.setCurrentIndex(idx)
            self.selected_take = takes[idx]
        else:
            self.selected_take = None
        self.cb_take.blockSignals(False)

        for i in reversed(range(self.takes_v.count() - 1)):  # keep trailing stretch
            item = self.takes_v.itemAt(i)
            w = item.widget() if item else None
            if w is not None:
                # NOT setParent(None): this often runs inside that very row's
                # click handler; deleting it now = use-after-free (native
                # crash). Hide it and let Qt delete it after the click.
                self.takes_v.removeWidget(w)
                w.hide()
                w.deleteLater()
        for t in takes:
            self.takes_v.insertWidget(self.takes_v.count() - 1, self._take_row(t))

        self._refresh_status_buttons()
        self._refresh_publish_note()
        self._sync_comments()
        if self.stack.currentIndex() == 1:
            self._refresh_review()
        self._update_drop_target()
        num = f"{self.selected_take.number:02d}" if self.selected_take else "—"
        self.status_hdr.setText(f"TAKE STATUS · Take {num}")
        self.play_take_btn.setText(f"▶ Play Take {num}")

    def _take_row(self, take: TakeData) -> QWidget:
        lbl, color = _STATUS_META[take.status]
        row = ClickableFrame()
        row.setObjectName("TakeRow")
        on = self.selected_take is not None and take.take == self.selected_take.take
        # Selector-scoped: an unscoped stylesheet also paints the border on
        # every child label.
        row.setStyleSheet(
            f"QFrame#TakeRow{{background:{'#16181b' if on else '#0e1012'};"
            f"border-left:3px solid {color};border-radius:9px;}}")
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 9, 10, 9)
        col = QVBoxLayout()
        col.setSpacing(1)
        name = QLabel(f"Take {take.number:02d}")
        name.setStyleSheet("font-family:'IBM Plex Mono',monospace;"
                           "font-weight:600;color:#c6ced6;border:none;")
        col.addWidget(name)
        peak = max((t.peak_db for t in take.tracks.values()), default=-120.0)
        clipped = sum(t.clipped for t in take.tracks.values())
        live = (self._recording and self.selected_take is not None
                and take.take == self.selected_take.take)
        meta = ("● recording…" if live else
                f"{fmt_duration(take.duration)} · "
                f"{'CLIP' if clipped else fmt_db(peak).replace(' dBFS', ' dB')}"
                if take.tracks else
                f"{fmt_duration(take.duration)} · video only" if take.facial
                else "no audio")
        if take.facial is not None and take.tracks and not live:
            meta += " · + video"
        sub = QLabel(meta)
        sub.setStyleSheet(
            f"color:{'#f2555a' if clipped else '#6b737a'};font-size:10px;"
            "font-family:'IBM Plex Mono',monospace;border:none;")
        col.addWidget(sub)
        h.addLayout(col)
        h.addStretch()
        st = QLabel(lbl)
        st.setStyleSheet(f"color:{color};font-weight:700;font-size:9px;")
        h.addWidget(st)
        play = QPushButton("▶")
        play.setFixedSize(24, 24)
        play.clicked.connect(lambda _c=False, t=take: self._play_take(t))
        h.addWidget(play)
        if getattr(take, "published_version", 0):
            state = self._check_state.get(take.take)
            if state is not None:
                mark = QLabel("✓" if state else "⚠")
                mark.setToolTip("Last check: all good" if state else
                                "Last check found problems — see the log")
                mark.setStyleSheet(
                    f"color:{'#5fd47c' if state else '#e0952f'};"
                    "font-weight:700;font-size:13px;border:none;")
                h.addWidget(mark)
            chk = QPushButton("Check")
            chk.setFixedHeight(24)
            chk.setToolTip("Compare this published take with AYON and the "
                           "studio drive (read-only)")
            chk.clicked.connect(partial(self._check_take, take.take))
            h.addWidget(chk)
            rp = QPushButton("↻")
            rp.setFixedSize(24, 24)
            rp.setToolTip("Republish as the next version "
                          "(the old version stays untouched)")
            rp.clicked.connect(partial(self._republish_take, take.take))
            h.addWidget(rp)
        if not getattr(take, "published_version", 0):   # locked = no bin
            trash = QPushButton("🗑")
            trash.setFixedSize(24, 24)
            trash.setToolTip(f"Delete {take.take.replace('_', ' ')}")
            trash.clicked.connect(partial(self._delete_take, take.take))
            h.addWidget(trash)
        row.clicked.connect(lambda t=take: self._select_take(t))
        row.setContextMenuPolicy(Qt.CustomContextMenu)
        row.customContextMenuRequested.connect(
            partial(self._take_menu, row, take.take))
        return row

    # ---- take numbering + delete ----------------------------------------- #
    def _on_next_take_changed(self, value: int) -> None:
        if self.takes.task_dir is None or self._busy_recording():
            return
        self.takes.manifest.next_take = int(value)
        try:
            self.takes.save()
        except Exception:
            self._log_exc("Could not save the next take number")
        nxt = self.takes.manifest.next_take_name()
        self.next_take_label.setText(f"NEXT  {nxt.replace('_', ' ')}")
        if nxt != f"Take_{value:02d}":
            self._log(f"Take {value:02d} already exists — next take will be "
                      f"{nxt.replace('_', ' ')}")
        self._update_drop_target()

    def _take_menu(self, row, take_name: str, pos) -> None:
        take = self.takes.manifest.find(take_name)
        if take is None:
            return
        menu = QMenu(self)
        act_play = menu.addAction("▶ Play")
        act_del = menu.addAction(f"🗑 Delete {take_name.replace('_', ' ')}…")
        if getattr(take, "published_version", 0):
            act_del.setEnabled(False)
            act_del.setText(f"🔒 Published v{take.published_version:03d} — "
                            "can't delete")
        act_chk = act_rep = None
        if getattr(take, "published_version", 0):
            menu.addSeparator()
            act_chk = menu.addAction("✓ Check published version")
            act_rep = menu.addAction(
                f"↻ Republish as v{take.published_version + 1:03d}…")
        chosen = menu.exec(row.mapToGlobal(pos))
        if chosen is act_play:
            self._play_take(take)
        elif chosen is act_del:
            self._delete_take(take_name)
        elif act_chk is not None and chosen is act_chk:
            self._check_take(take_name)
        elif act_rep is not None and chosen is act_rep:
            self._republish_take(take_name)

    def _delete_take(self, take_name: str, *_args) -> None:
        take = self.takes.manifest.find(take_name)
        if take is None:
            return
        label = take_name.replace("_", " ")
        busy = (self._busy_recording()
                or (getattr(self, "_facial_worker", None) is not None
                    and self._facial_worker.isRunning())
                or (self._pub_worker is not None
                    and self._pub_worker.isRunning()))
        if busy:
            self._log(f"Can't delete {label} now — wait until recording / "
                      "saving / video import / publish has finished")
            return
        if QMessageBox.question(
                self, "Delete take",
                f"Delete {label}?\n\nIts folder is moved to _deleted inside "
                "the task folder (not erased), so it can be brought back by "
                "hand.") != QMessageBox.Yes:
            return
        try:
            moved = self.takes.delete_take(take)
        except Exception as exc:
            self._log(f"✕ Could not delete {label}: {exc}")
            return
        if self.selected_take is not None and self.selected_take.take == take_name:
            self.selected_take = None
        self._log(f"🗑 {label} deleted"
                  + (f" — files moved to {moved}" if moved else ""))
        self._refresh_takes()

    def _select_take(self, take: TakeData) -> None:
        self.selected_take = take
        self._refresh_takes()
        self._sync_comments()

    def _on_select_take_combo(self, idx: int) -> None:
        take_name = self.cb_take.itemData(idx)
        t = self.takes.manifest.find(take_name) if take_name else None
        if t:
            self.selected_take = t
            self._sync_comments()
            self._refresh_status_buttons()
            self._refresh_publish_note()
            self._refresh_takes()          # row highlight + drop target

    def _on_save_take(self) -> None:
        if self.takes.task_dir is None:
            self._log("Pick a Project → Shot → Task first")
            return
        if self.selected_take is None:
            self.selected_take = self.takes.new_take(
                comments=self.comments.toPlainText(), status="view")
        else:
            self.takes.set_comments(self.selected_take, self.comments.toPlainText())
        self._refresh_takes()
        self._log(f"✓ Saved {self.selected_take.take}")

    def _set_status(self, key: str) -> None:
        if self.selected_take is None:
            return
        self.takes.set_status(self.selected_take, key)
        self._refresh_takes()

    def _refresh_status_buttons(self) -> None:
        cur = self.selected_take.status if self.selected_take else None
        mapping = {
            "reject": (self.btn_reject, "StatusReject"),
            "view": (self.btn_view, "StatusView"),
            "okay": (self.btn_okay, "StatusOkay"),
            "approve": (self.btn_approve, "StatusApprove"),
        }
        for key, (btn, base) in mapping.items():
            btn.setObjectName(base + ("On" if cur == key else ""))
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _refresh_publish_note(self) -> None:
        if self.offline:
            self.publish_btn.setEnabled(False)
            self.publish_note.setText(
                "Offline — takes are saved locally; sign in to publish")
            return
        t = self.selected_take
        pending = [x for x in publish.publishable(self.takes.manifest.takes)
                   if not getattr(x, "published_version", 0)]
        self.publish_btn.setEnabled(bool(pending))
        if t is None:
            self.publish_note.setText("")
            return
        lbl = _STATUS_META[t.status][0].title()
        pv = getattr(t, "published_version", 0)
        if pv:
            self.publish_note.setText(
                f"Take {t.number:02d} ({lbl}) — published as v{pv:03d}, "
                f"locked · {len(pending)} new take(s) pending")
        elif t.status in publish.PUBLISHABLE:
            self.publish_note.setText(f"Take {t.number:02d} ({lbl}) → will publish to AYON")
        else:
            self.publish_note.setText(f"Take {t.number:02d} is Rejected → excluded, not published")

    # ================================================================= #
    # Actions
    # ================================================================= #

    def _refresh_ref_versions(self, *_args) -> None:
        """Fill the reference version picker for the selected task.

        Offline: from the local offline copies. Online: from AYON, with
        versions that also exist locally marked '· offline ✓' (they play from
        the local copy -- instant, no server round-trip)."""
        self.cb_ref_ver.blockSignals(True)
        self.cb_ref_ver.clear()
        project = self._current_project
        task_name = self.cb_ref_task.currentText()
        if self.offline:
            entries = [e for e in self.offline_cache.entries_for(
                           project, self._shot_id)
                       if e["task"].lower() == task_name.lower()]
            for e in entries:
                self.cb_ref_ver.addItem(
                    f"{e['product']} · v{e['version']:03d}",
                    {"version": e["version"], "id": e["version_id"],
                     "_cached": e})
            if not entries:
                self.cb_ref_ver.addItem("— not available offline —", None)
            self.cb_ref_ver.blockSignals(False)
            return
        task_id = self.cb_ref_task.currentData()
        if project and self._shot_id and task_id:
            try:
                versions = self.ayon.versions_for_task(
                    project, self._shot_id, task_id)
            except Exception:
                self._log_exc("Could not list published versions")
                versions = []
            for v in versions:
                local = self.offline_cache.by_version_id(v.get("id", ""))
                tag = "  · offline ✓" if local else ""
                self.cb_ref_ver.addItem(f"v{v['version']:03d}{tag}", v)
            if not versions:
                self.cb_ref_ver.addItem("— none —", None)
        self.cb_ref_ver.blockSignals(False)

    # ---- offline copies of published tasks ----------------------------- #
    def _build_offline_menu(self, menu: QMenu) -> None:
        menu.clear()
        menu.setStyleSheet(theme_mod.menu_qss())
        project = self._current_project
        task = self.cb_ref_task.currentText()
        busy = any(w is not None and w.isRunning() for w in (
            getattr(self, "_offline_plan", None),
            getattr(self, "_offline_worker", None)))
        if not self.offline:
            a = menu.addAction(
                f"⭳  Make '{task}' available offline (all shots in {project})…"
                if task else "⭳  Pick a shot + task first")
            a.setEnabled(bool(project and task) and not busy)
            a.triggered.connect(lambda: self._make_task_offline(project, task))
            tasks = self.offline_cache.tasks()
            u = menu.addAction(f"⟳  Update all offline tasks ({len(tasks)})")
            u.setEnabled(bool(tasks) and not busy)
            u.triggered.connect(self._update_all_offline)
        if busy:
            c = menu.addAction("✕  Cancel the running offline copy")
            c.triggered.connect(self._cancel_offline)
        menu.addSeparator()
        tasks = self.offline_cache.tasks()
        if tasks:
            head = menu.addAction("Available offline:")
            head.setEnabled(False)
            for t in tasks:
                sub = menu.addMenu(f"   {t['project']} · {t['task']}   "
                                   f"({t.get('versions', 0) + t.get('skipped', 0)} "
                                   f"versions, synced {t.get('synced_at', '?')})")
                sub.setStyleSheet(theme_mod.menu_qss())
                if not self.offline:
                    r = sub.addAction("⟳  Update now")
                    r.setEnabled(not busy)
                    r.triggered.connect(lambda _c=False, p=t["project"],
                                        k=t["task"]: self._make_task_offline(p, k))
                d = sub.addAction("🗑  Remove offline copy")
                d.setEnabled(not busy)
                d.triggered.connect(lambda _c=False, p=t["project"],
                                    k=t["task"]: self._remove_offline_task(p, k))
        else:
            none = menu.addAction("Nothing is available offline yet")
            none.setEnabled(False)
        menu.addSeparator()
        o = menu.addAction("📂  Open offline folder")
        o.triggered.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.offline_cache.root))))

    def _make_task_offline(self, project: str, task: str,
                           confirm: bool = True) -> None:
        if self.offline or not (project and task):
            return
        self._offline_confirm = confirm
        self._log(f"⭳ Planning offline copy of '{task}' across all shots in "
                  f"{project}…")
        self.offline_btn.setEnabled(False)
        self._offline_plan = self._keep(OfflinePlanWorker(self.ayon, project, task))
        self._offline_plan.progress.connect(lambda m: self._log(f"  {m}"))
        self._offline_plan.done.connect(self._on_offline_plan)
        self._offline_plan.failed.connect(self._on_offline_plan_failed)
        self._offline_plan.start()

    def _on_offline_plan_failed(self, tb: str) -> None:
        self.offline_btn.setEnabled(True)
        self._log(f"✕ Could not plan the offline copy:\n{tb}")

    def _on_offline_plan(self, project: str, task: str, plan: list) -> None:
        self.offline_btn.setEnabled(True)
        if not plan:
            self._log(f"Nothing published under '{task}' in {project}")
            QMessageBox.information(self, "Make available offline",
                                    f"Nothing is published under '{task}' "
                                    f"in {project}.")
            return
        todo = [it for it in plan if not self.offline_cache.has(
            it["project"], it["folder_id"], it["task"], it["product"],
            it["version_id"])]
        size = sum(f["size"] for it in todo for f in it["files"])
        server = sum(1 for it in todo if it["server_only"])
        shots = len({it["folder_id"] for it in plan})
        self.offline_cache.root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.offline_cache.root).free
        if size and size * 1.1 > free:
            QMessageBox.warning(
                self, "Not enough disk space",
                f"Copying '{task}' needs {size/1e9:.1f} GB but only "
                f"{free/1e9:.1f} GB is free on "
                f"{self.offline_cache.root.anchor}.")
            return
        if not todo:
            self.offline_cache.mark_task(project, task, {
                "versions": 0, "skipped": len(plan), "failed": 0,
                "bytes": 0, "shots": shots})
            self._log(f"✓ '{task}' is already fully available offline "
                      f"({len(plan)} versions, {shots} shots)")
            return
        msg = (f"Copy the latest '{task}' versions for {shots} shot(s) in "
               f"{project}?\n\n"
               f"  • {len(todo)} version(s) to copy"
               f" ({len(plan) - len(todo)} already up to date)\n"
               f"  • about {size/1e9:.2f} GB (free: {free/1e9:.0f} GB)\n")
        if server:
            msg += (f"  • {server} not reachable on disk from this PC — their "
                    "server reviewable will be downloaded instead\n")
        msg += "\nOnly playable media is copied. You can keep working meanwhile."
        if getattr(self, "_offline_confirm", True) and QMessageBox.question(
                self, "Make available offline", msg) != QMessageBox.Yes:
            self._offline_queue = []
            return
        self._log(f"⭳ Copying {len(todo)} version(s) of '{task}' "
                  f"(~{size/1e9:.2f} GB) in the background…")
        self._offline_worker = self._keep(OfflineDownloadWorker(
            self.ayon, self.offline_cache, project, task, todo))
        self._offline_worker.progress.connect(self._log)
        self._offline_worker.step.connect(
            lambda i, n: self.ref_status.setText(
                f"Offline copy of '{task}': {i}/{n}…"))
        self._offline_worker.done.connect(self._on_offline_done)
        self._offline_worker.start()

    def _on_offline_done(self, project: str, task: str, stats: dict) -> None:
        self.ref_status.setText(
            f"'{task}' offline: {stats['versions']} copied, "
            f"{stats['skipped']} up to date, {stats['failed']} failed")
        self._log(f"✓ Offline copy of '{task}' ({project}) finished — "
                  f"{stats['versions']} copied ({stats['bytes']/1e9:.2f} GB), "
                  f"{stats['skipped']} already up to date, "
                  f"{stats['failed']} failed")
        self._refresh_ref_versions()
        queue = getattr(self, "_offline_queue", [])
        if queue:
            p, t = queue.pop(0)
            self._make_task_offline(p, t, confirm=False)

    def _update_all_offline(self) -> None:
        tasks = [(t["project"], t["task"]) for t in self.offline_cache.tasks()]
        if not tasks:
            return
        self._offline_queue = tasks[1:]
        self._log(f"⟳ Updating {len(tasks)} offline task(s)…")
        self._make_task_offline(*tasks[0], confirm=False)

    def _cancel_offline(self) -> None:
        self._offline_queue = []
        w = getattr(self, "_offline_worker", None)
        if w is not None and w.isRunning():
            w.stop()
            self._log("Cancelling the offline copy after the current file…")

    def _remove_offline_task(self, project: str, task: str) -> None:
        if QMessageBox.question(
                self, "Remove offline copy",
                f"Delete the local copies of '{task}' ({project})?") \
                != QMessageBox.Yes:
            return
        n = self.offline_cache.remove_task(project, task)
        self._log(f"🗑 Removed {n} offline version(s) of '{task}' ({project})")
        self._refresh_ref_versions()

    def _refresh_ayon_versions_clicked(self) -> None:
        """⟳ in the REFERENCE header — pick up freshly published versions."""
        self._refresh_ref_versions()
        n = sum(1 for i in range(self.cb_ref_ver.count())
                if self.cb_ref_ver.itemData(i) is not None)
        self._log(f"⟳ AYON refreshed — {n} published version(s) for "
                  f"'{self.cb_ref_task.currentText()}'")

    def _open_external(self, title: str, items: list[tuple[str, Path]]) -> None:
        """Open every file in the OS default player (one window per file)."""
        opened = 0
        for label, path in items:
            if not path.exists():
                self._log(f"✕ {label}: file not found: {path}")
                continue
            size_kb = path.stat().st_size / 1024
            if size_kb < 4:
                self._log(f"⚠ {label}: {path.name} is only {size_kb:.1f} KB — "
                          "likely no audio was written")
            if QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                opened += 1
                self._log(f"▶ {label}: {path.name} ({size_kb:,.0f} KB)")
            else:
                self._log(f"✕ OS refused to open {path}")
        if not opened:
            self._log(f"{title}: nothing playable")

    def _play_local_reference(self) -> None:
        """Reference from a local file — the offline path, and a shortcut
        online too. Remembers the last folder used."""
        start = self.config.last_reference_dir or str(self.config.save_root_path)
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a reference file", start,
            "Media (*.wav *.flac *.mp3 *.aif *.aiff *.m4a *.mp4 *.mov *.mkv "
            "*.avi *.webm *.mxf);;Audio (*.wav *.flac *.mp3 *.aif *.aiff "
            "*.m4a);;Video (*.mp4 *.mov *.avi *.mkv *.webm *.mxf);;"
            "All files (*)")
        if not path:
            return
        p = Path(path)
        if not p.exists():
            self._log(f"✕ Reference not found: {p}")
            return
        self.config.last_reference_dir = str(p.parent)
        self.config.save()
        self._ref_path = str(p)
        self.ref_status.setText(f"Local: {p.name}")
        self._open_external("reference", [("reference", p)])
        self._log(f"▶ Reference (local): {p}")

    def _play_reference(self) -> None:
        """Resolve the selected published version (disk or AYON server) and
        open it in the default player."""
        project = self._current_project
        task_name = self.cb_ref_task.currentText()
        ver = self.cb_ref_ver.currentData()
        if not (project and self._shot_id):
            self._log("Pick a Project → Shot → Task first")
            return
        if ver is None:
            self._log(f"No published versions for '{task_name}'"
                      + (" available offline" if self.offline else ""))
            return
        # A local offline copy wins: instant, and the only option offline.
        entry = ver.get("_cached") or self.offline_cache.by_version_id(
            ver.get("id", ""))
        if entry:
            local = self.offline_cache.playable(entry)
            if local is not None:
                self.ref_status.setText(
                    f"v{entry['version']:03d} · {local.name} (offline copy)")
                self._open_external(
                    f"{entry['product']} v{entry['version']:03d}",
                    [("reference", local)])
                return
            if self.offline:
                self._log("✕ The offline copy's files are missing — update it "
                          "when back on the network")
                return
        self.ref_status.setText(f"Loading {task_name} v{ver['version']:03d}…")
        self._log(f"Loading {task_name} v{ver['version']:03d}…")
        try:
            info = self.ayon.resolve_version_file(project, ver)
        except Exception:
            self.ref_status.setText("✕ Load failed — see log below")
            self._log_exc("Reference load failed")
            return
        src_note = ("downloaded from AYON server"
                    if info.get("source") == "server" else "local file")
        self.ref_status.setText(
            f"{info['version']} · {Path(info['path']).name} ({src_note})")
        self._open_external(f"{task_name} {info['version']}",
                            [("reference", Path(info["path"]))])

    def _take_items(self, take: TakeData) -> list[tuple[str, Path]]:
        if self.takes.task_dir is None:
            return []
        return [(key, self.takes.task_dir / tr.path)
                for key, tr in take.tracks.items()]

    def _play_take(self, take: Optional[TakeData]) -> None:
        """Play a take's FIRST track (the main mic) in the default player --
        one player, not one per track. 'Play All Tracks' opens them all."""
        if take is None:
            self._log("No take selected")
            return
        if self.takes.task_dir is None:
            self._log("Pick a Project → Shot → Task first")
            return
        f = take.facial
        if f is not None:
            # the review mp4 (clean mic in sync) if it was built, else the
            # original facial video
            rev = self.takes.task_dir / f.review if f.review else None
            vid = rev if rev is not None and rev.exists() else \
                self.takes.task_dir / f.path
            self._open_external(take.take, [("facial", vid)])
            return
        items = self._take_items(take)
        if not items:
            self._log(f"{take.take}: no audio files on disk yet")
            return
        self._open_external(take.take, items[:1])

    def _play_all_tracks(self) -> None:
        """Review tab: open every track of the selected take."""
        take = self.selected_take
        if take is None or self.takes.task_dir is None:
            self._log("No take selected")
            return
        items = self._take_items(take)
        if take.facial is not None:
            items.insert(0, ("facial", self.takes.task_dir / take.facial.path))
        if not items:
            self._log(f"{take.take}: no audio files on disk yet")
            return
        self._open_external(take.take, items)

    # ================================================================= #
    # Review tab
    # ================================================================= #
    def _refresh_review(self) -> None:
        if not self._recording:
            try:
                for line in self.takes.reconcile_take_files():
                    self._log(f"♻ {line}")
            except Exception:
                self._log_exc("Take file reconcile failed")
        takes = self.takes.manifest.takes
        self.cb_rev_take.blockSignals(True)
        self.cb_rev_take.clear()
        sel_idx = -1
        for i, t in enumerate(takes):
            lbl, _c = _STATUS_META[t.status]
            self.cb_rev_take.addItem(f"Take {t.number:02d} — {lbl.title()}", t.take)
            if self.selected_take is not None and t.take == self.selected_take.take:
                sel_idx = i
        if sel_idx < 0 and takes:
            sel_idx = len(takes) - 1
            self.selected_take = takes[-1]
        if sel_idx >= 0:
            self.cb_rev_take.setCurrentIndex(sel_idx)
        self.cb_rev_take.blockSignals(False)
        self._sync_comments()

        take = self.selected_take
        self._refresh_review_status()

        # rebuild the grid
        while self.review_grid.count():
            item = self.review_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if take is None or self.takes.task_dir is None:
            return
        entries = list(take.tracks.items())
        n_tiles = len(entries) + (1 if take.facial else 0)
        cols = 1 if n_tiles <= 1 else 2
        start = 0
        if take.facial is not None:
            f = take.facial
            vid = self.takes.task_dir / f.path
            rev = self.takes.task_dir / f.review if f.review else None
            play = rev if rev is not None and rev.exists() else vid
            sync = (f"mic synced {f.sync_offset:+.3f} s" if f.synced
                    else "own sound" if f.has_audio else "no sound")
            thumb = self.takes.task_dir / f.thumb if f.thumb else None
            tile = ReviewTile(
                f"FACIAL · {vid.name}", str(play), detail=(
                    f"{f.label} · {fmt_duration(f.duration)} · {sync}"
                    + (f" · +{len(f.files) - 1} file(s)" if len(f.files) > 1
                       else "") + ("" if vid.exists() else " · file missing")),
                highlight=True,
                image=str(thumb) if thumb is not None and thumb.exists() else "")
            tile.open_requested.connect(
                lambda path: self._open_external("facial", [("facial", Path(path))]))
            tile.export_requested.connect(
                lambda _p, v=vid: self._export_copy(v))
            tile.reveal_requested.connect(
                lambda path: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(Path(path).parent))))
            self.review_grid.addWidget(tile, 0, 0)
            start = 1
        for i, (key, tr) in enumerate(entries, start):
            p = self.takes.task_dir / tr.path
            info = wave_mod.describe(p) if p.exists() else {}
            detail = (f"input {tr.channel + 1} · {fmt_duration(tr.duration)} · "
                      f"peak {fmt_db(tr.peak_db)}"
                      + (f" · {info['samplerate'] / 1000:g} kHz {info['subtype']}"
                         if info else " · file missing")
                      + (f" · ⚠ {tr.clipped} clipped" if tr.clipped else ""))
            tile = ReviewTile(f"{key} · {p.name}", str(p),
                              wave_mod.peaks(p) if p.exists() else None,
                              detail=detail, highlight=(i == 0))
            tile.open_requested.connect(
                lambda path, k=key: self._open_external(k, [(k, Path(path))]))
            tile.export_requested.connect(
                lambda path: self._export_copy(Path(path)))
            tile.reveal_requested.connect(
                lambda path: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(Path(path).parent))))
            self.review_grid.addWidget(tile, i // cols, i % cols)
        self.review_grid.setRowStretch(n_tiles // cols + 1, 1)

    def _sync_comments(self) -> None:
        """Keep the Record and Review comment boxes showing the SAME take.

        They are two separate widgets over one piece of data, so a comment
        typed on one tab used to stay invisible on the other until restart.
        A box the user is currently typing in is never overwritten.
        """
        text = self.selected_take.comments if self.selected_take else ""
        for box in (getattr(self, "comments", None),
                    getattr(self, "rev_comments", None)):
            if box is None or box.hasFocus():
                continue
            if box.toPlainText() != text:
                box.blockSignals(True)
                box.setPlainText(text)
                box.blockSignals(False)

    def _refresh_review_status(self) -> None:
        cur = self.selected_take.status if self.selected_take else None
        base = {"reject": "StatusReject", "view": "StatusView",
                "okay": "StatusOkay", "approve": "StatusApprove"}
        for key, btn in self.rev_btn.items():
            btn.setObjectName(base[key] + ("On" if cur == key else ""))
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _on_review_take_changed(self, idx: int) -> None:
        name = self.cb_rev_take.itemData(idx)
        t = self.takes.manifest.find(name) if name else None
        if t is not None:
            self.selected_take = t
            self._refresh_review()
            self._refresh_status_buttons()
            self._refresh_publish_note()
            self._sync_comments()
            self._update_drop_target()

    def _set_status_review(self, key: str) -> None:
        if self.selected_take is None:
            return
        self.takes.set_status(self.selected_take, key)
        self._refresh_takes()
        self._refresh_review()

    def _save_review_comment(self) -> None:
        if self.selected_take is None:
            return
        self.takes.set_comments(self.selected_take, self.rev_comments.toPlainText())
        self._sync_comments()
        self._log(f"✓ Comment saved on {self.selected_take.take}")

    def _play_selected(self) -> None:
        self._play_take(self.selected_take)

    # ---- adjustable recording save root --------------------------------- #
    # ---- ffmpeg location -------------------------------------------------- #
    def _on_ffmpeg_changed(self) -> None:
        self.config.ffmpeg_path = self.ffmpeg_edit.text().strip()
        self.config.save()
        tools.set_ffmpeg(self.config.ffmpeg_path)
        self._log(tools.describe())

    def _browse_ffmpeg(self) -> None:
        start = self.config.ffmpeg_path or r"C:\\"
        path, _ = QFileDialog.getOpenFileName(
            self, "Select ffmpeg.exe", start,
            "ffmpeg (ffmpeg.exe ffmpeg);;All files (*)")
        if path:
            self.ffmpeg_edit.setText(path)
            self._on_ffmpeg_changed()

    def _on_save_root_changed(self) -> None:
        text = self.save_root_edit.text().strip()
        if not text or text == self.config.save_root:
            return
        self.config.save_root = text
        self.config.save()
        self._log(f"Save root → {text}")
        # Re-apply the current context so new takes land under the new root.
        if self.cb_task.currentText():
            self._on_task_selected(self.cb_task.currentIndex())

    def _browse_save_root(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Select recording save folder", self.config.save_root)
        if d:
            self.save_root_edit.setText(d)
            self._on_save_root_changed()

    def _open_folder(self) -> None:
        """Open the current task's save folder (or the selected take's) in the OS."""
        if self.takes.task_dir is None:
            self._log("No save folder yet — pick a Task and record/save first")
            return
        target = self.takes.task_dir
        if self.selected_take is not None:
            take_dir = target / self.selected_take.take
            if take_dir.exists():
                target = take_dir
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        self._log(f"📂 Opened {target}")

    # ================================================================= #
    # Studio drive (smb://kumbh/production == P:\\ on Windows)
    # ================================================================= #
    def _paint_drive(self, ok, tip: str) -> None:
        color = "#8b9298" if ok is None else ("#5fd47c" if ok else "#f2555a")
        self.drive_btn.setStyleSheet(
            f"color:{color};font-weight:600;font-size:11px;")
        self.drive_btn.setToolTip(
            f"Studio drive {self.config.share_url}\n{tip}\n"
            "Click to set user / password and connect")

    def _check_share(self, mount: bool, then=None) -> None:
        """Check the drive in the background; `then(status)` runs after."""
        if self.offline:
            self._paint_drive(None, "offline — not checked")
            if then:
                then(share_mod.ShareStatus(False, "", "working offline"))
            return
        w = self._share_worker
        if w is not None and w.isRunning():
            self._share_queue.append((mount, then))
            return
        self._run_share(mount, [then] if then else [])

    def _run_share(self, mount: bool, thens: list) -> None:
        self._share_thens = thens
        self._share_mounting = mount
        self._paint_drive(None, "checking…")
        self._share_worker = self._keep(
            ShareWorker(self.config, self._current_project, mount))
        self._share_worker.logged.connect(self._log)
        self._share_worker.done.connect(self._on_share_done)
        self._share_worker.start()

    def _on_share_done(self, st) -> None:
        prev = self._share_status
        self._share_status = st
        self._paint_drive(st.ok, st.message)
        if st.mounted_now:
            self._log(f"✓ Studio drive mounted — {st.root}")
        elif prev is None or prev.ok != st.ok or prev.message != st.message:
            self._log(("✓ Studio drive: " if st.ok else "⚠ Studio drive: ")
                      + st.message)
        thens, self._share_thens = self._share_thens, []
        queue, self._share_queue = self._share_queue, []
        for fn in thens:
            fn(st)
        if not queue:
            return
        # Someone asked to MOUNT while a check-only run was going: run again.
        if not st.ok and not self._share_mounting and any(m for m, _ in queue):
            self._run_share(True, [fn for _m, fn in queue if fn])
        else:
            for _m, fn in queue:
                if fn:
                    fn(st)

    def _open_share_dialog(self) -> None:
        cfg = self.config
        dlg = QDialog(self)
        dlg.setWindowTitle("Studio drive")
        dlg.setMinimumWidth(460)
        form = QFormLayout(dlg)
        url = QLineEdit(cfg.share_url)
        url.setPlaceholderText("smb://kumbh/production")
        user = QLineEdit(cfg.share_user)
        user.setPlaceholderText("studio login (empty = guest / keychain)")
        pw = QLineEdit(cfg.share_password)
        pw.setEchoMode(QLineEdit.Password)
        root_now = getattr(self._share_status, "root", "") or "—"
        point = QLineEdit(cfg.share_mount_point)
        point.setPlaceholderText(publish.share_mount() or "/Volumes/production")
        point.setToolTip(f"Where the share is mounted on this Mac (empty = "
                         f"/Volumes/<share>). Publishing goes to {root_now}")
        remember = QCheckBox("Remember password on this workstation")
        remember.setChecked(cfg.share_remember)
        auto = QCheckBox("Connect automatically (at start and before publish)")
        auto.setChecked(cfg.share_auto_mount)
        st = self._share_status
        status = QLabel(st.message if st else "not checked yet")
        status.setWordWrap(True)
        form.addRow("Share", url)
        form.addRow("User", user)
        form.addRow("Password", pw)
        form.addRow("Mount point", point)
        form.addRow("", remember)
        form.addRow("", auto)
        form.addRow("Status", status)
        box = QDialogButtonBox(QDialogButtonBox.Cancel)
        box.addButton("Save && Connect", QDialogButtonBox.AcceptRole)
        box.accepted.connect(dlg.accept)
        box.rejected.connect(dlg.reject)
        form.addRow(box)
        if dlg.exec() != QDialog.Accepted:
            return
        cfg.share_url = url.text().strip() or "smb://kumbh/production"
        cfg.share_user = user.text().strip()
        cfg.set_share_password(pw.text())
        cfg.share_mount_point = point.text().strip()
        cfg.share_remember = remember.isChecked()
        cfg.share_auto_mount = auto.isChecked()
        cfg.save()      # the password is only written if 'Remember' is on
        publish.set_share(cfg.share_url, cfg.share_windows_drive,
                          cfg.share_mount_point)
        self._share_status = None          # log the next result again
        self._check_share(mount=True)

    def _on_publish(self) -> None:
        pubs = publish.publishable(self.takes.manifest.takes)
        locked = [t for t in pubs if getattr(t, "published_version", 0)]
        pubs = [t for t in pubs if not getattr(t, "published_version", 0)]
        for t in locked:
            self._log(f"  🔒 {t.take} already published as "
                      f"v{t.published_version:03d} — locked, not re-sent")
        if not pubs:
            self.publish_note.setText(
                "Nothing new to publish — published takes are locked; "
                "record a new take for a new version")
            return
        if not self.ayon.is_connected or not self._shot_id:
            self.publish_note.setText("Not connected to AYON / no shot selected — cannot publish")
            self._log("Publish blocked: connect to AYON and select a shot/task first")
            return
        if self._pub_worker is not None and self._pub_worker.isRunning():
            return
        # The files go onto the studio drive: make sure it is mounted (and
        # mount it if allowed) BEFORE anything is created in AYON.
        self.publish_btn.setEnabled(False)
        self.publish_note.setText("Checking the studio drive…")
        self._check_share(mount=self.config.share_auto_mount,
                          then=partial(self._publish_after_share, pubs))

    def _publish_after_share(self, pubs: list, st) -> None:
        if not st.ok:
            self.publish_btn.setEnabled(True)
            self.publish_note.setText(
                "Studio drive not mounted — click ● Drive to connect")
            self._log(f"✕ Publish blocked — studio drive: {st.message}")
            return
        self.publish_btn.setEnabled(False)
        self.publish_progress.setValue(0)
        self.publish_progress.setVisible(True)
        self.publish_note.setText(f"Publishing {len(pubs)} take(s)…")
        self._pub_worker = self._keep(PublishWorker(
            pubs, self.takes.task_dir, self.takes.manifest.project,
            self._shot_id, self._task_id,
            author=getattr(self.ayon, "username", ""),
        ))
        self._pub_worker.progress.connect(self.publish_progress.setValue)
        self._pub_worker.logged.connect(self._log)
        self._pub_worker.done.connect(self._on_publish_done)
        self._pub_worker.start()

    # ---- check + republish a published take ------------------------------ #
    def _publish_busy(self) -> bool:
        return ((self._pub_worker is not None and self._pub_worker.isRunning())
                or (self._check_worker is not None
                    and self._check_worker.isRunning()))

    def _check_take(self, take_name: str, *_args) -> None:
        take = self.takes.manifest.find(take_name)
        if take is None or not getattr(take, "published_version", 0):
            return
        if not self.ayon.is_connected or not self._shot_id:
            self._log("Check needs AYON: connect and select a shot/task first")
            return
        if self._publish_busy():
            self._log("Wait for the running publish / check to finish")
            return
        self._log(f"🔍 Checking {take_name} v{take.published_version:03d}…")
        # The drive must be mounted to look at the files (never mount here).
        self._check_share(mount=self.config.share_auto_mount,
                          then=partial(self._check_after_share, take))

    def _check_after_share(self, take, st) -> None:
        if not st.ok:
            self._log(f"✕ Check blocked — studio drive: {st.message}")
            return
        self._check_worker = self._keep(CheckWorker(
            take, self.takes.task_dir, self.takes.manifest.project,
            self._shot_id, self._task_id))
        self._check_worker.done.connect(self._on_check_done)
        self._check_worker.start()

    def _on_check_done(self, take_name: str, rep) -> None:
        for line in rep.lines:
            self._log("   " + line)
        self._check_state[take_name] = bool(rep.ok and rep.problems == 0)
        if self._check_state[take_name]:
            self._log(f"✓ {take_name} v{rep.version:03d}: everything is in place")
        else:
            self._log(f"⚠ {take_name} v{rep.version:03d}: {rep.problems} "
                      "problem(s) — use ↻ Republish to send it again as the "
                      "next version")
        self._refresh_takes()

    def _republish_take(self, take_name: str, *_args) -> None:
        take = self.takes.manifest.find(take_name)
        if take is None or not getattr(take, "published_version", 0):
            return
        if not self.ayon.is_connected or not self._shot_id:
            self._log("Republish needs AYON: connect and select a shot/task first")
            return
        if self._publish_busy():
            self._log("Wait for the running publish / check to finish")
            return
        old = take.published_version
        if QMessageBox.question(
                self, "Republish take",
                f"Publish {take_name.replace('_', ' ')} again as the next "
                f"version?\n\nv{old:03d} stays on AYON untouched; the new "
                "version becomes this take's published version.") != QMessageBox.Yes:
            return
        self.publish_btn.setEnabled(False)
        self.publish_note.setText(f"Republishing {take_name}…")
        self._check_share(mount=self.config.share_auto_mount,
                          then=partial(self._republish_after_share, take))

    def _republish_after_share(self, take, st) -> None:
        if not st.ok:
            self._refresh_publish_note()
            self._log(f"✕ Republish blocked — studio drive: {st.message}")
            return
        self.publish_progress.setValue(0)
        self.publish_progress.setVisible(True)
        self._pub_worker = self._keep(PublishWorker(
            [take], self.takes.task_dir, self.takes.manifest.project,
            self._shot_id, self._task_id,
            author=getattr(self.ayon, "username", ""), republish=True))
        self._pub_worker.progress.connect(self.publish_progress.setValue)
        self._pub_worker.logged.connect(self._log)
        self._pub_worker.done.connect(self._on_publish_done)
        self._pub_worker.start()

    def _on_publish_done(self, published: int, total: int, last_reason: str) -> None:
        self.publish_progress.setValue(100)
        self.publish_btn.setEnabled(True)
        try:
            self.takes.save()      # persist the published-version locks
        except Exception:
            self._log_exc("Could not save publish locks to manifest")
        if published == total:
            self.publish_note.setText(f"✓ Published {published}/{total} take(s) → AYON")
        elif published > 0:
            self.publish_note.setText(
                f"Published {published}/{total}. Last issue: {last_reason}")
        else:
            self.publish_note.setText(f"Published 0/{total} — {last_reason}")
        self._log(f"Publish complete: {published}/{total}")
        self._check_state.clear()        # versions changed: old checks are stale
        self._refresh_published()
        self._refresh_takes()

    # ================================================================= #
    # Loop: refresh previews + timecode
    # ================================================================= #
    def _tick(self) -> None:
        # ---- meters: read the peaks since the last tick, per channel ---- #
        inp = self._input
        if inp is not None and inp.is_open:
            peaks, clip = inp.read_levels()
            for s in self._strips:
                ch = s.channel
                if ch < len(peaks):
                    s.set_level(float(peaks[ch]), bool(clip[ch]))
            self._tick_n = getattr(self, "_tick_n", 0) + 1
            if self._tick_n % 12 == 0:                # ~2x per second
                self._update_input_status()
        # ---- timecode --------------------------------------------------- #
        if self._recording:
            now = time.perf_counter()
            if self._countdown_until:
                remain = self._countdown_until - now
                if remain > 0:
                    self._paint_countdown(remain)
                    self.tc_label.setText(f"-00:00:{int(remain) + 1:02d}:00")
                    return
                self._countdown_until = 0.0
                self.countdown_label.hide()
                self._log("● ACTION — take is now recording")
            elapsed = max(0.0, now - self._rec_t0)
            self._paint_rec_light(int(elapsed * 2) % 2 == 0)   # 1 Hz blink
            if int(elapsed) % 10 == 0 and int(elapsed * 25) % 25 == 0:
                self._update_disk_left()
            fps = max(1, int(self.config.fps_display))
            hh = int(elapsed // 3600)
            mm = int(elapsed % 3600 // 60)
            ss = int(elapsed % 60)
            ff = int((elapsed % 1) * fps)
            self.tc_label.setText(f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}")

    def _update_input_status(self) -> None:
        inp = self._input
        if inp is None:
            return
        if inp.callback_errors != getattr(self, "_cb_errs_seen", 0):
            self._cb_errs_seen = inp.callback_errors
            self._log(f"⚠ audio callback error ({inp.callback_errors}x): "
                      f"{inp.last_callback_error} — audio keeps flowing")
        if not inp.is_live:
            self.input_status.setText("⚠ no data from the device — unplugged?")
            self.input_status.setStyleSheet("color:#e0952f;font-size:11px;")
            return
        # A track whose meter sat at the floor for ~5 s: say so, gently.
        silent = [s.role for s in self._strips
                  if s.meter.peak_hold_db <= -59.9]
        now = time.perf_counter()
        if silent and not self._silence_since:
            self._silence_since = now
        elif not silent:
            self._silence_since = 0.0
        quiet = bool(silent) and now - self._silence_since > 5.0
        text = (f"● LIVE · {inp.samplerate / 1000:g} kHz · {inp.channels} ch"
                + (" · excl" if inp.exclusive_used else ""))
        if quiet:
            text += f" · no signal on {', '.join(silent)}"
        self.input_status.setText(text)
        self.input_status.setStyleSheet(
            f"color:{'#e0952f' if quiet else '#5fd47c'};font-size:11px;")
    # ---- count-in overlay ------------------------------------------------ #
    def _show_countdown(self, secs: int) -> None:
        self.countdown_label.setText(str(secs))
        self.countdown_label.resize(self.grid_host.size())
        self.countdown_label.raise_()
        self.countdown_label.show()

    def _paint_countdown(self, remain: float) -> None:
        n = int(remain) + 1
        if self.countdown_label.text() != str(n):
            self.countdown_label.setText(str(n))
        if self.countdown_label.size() != self.grid_host.size():
            self.countdown_label.resize(self.grid_host.size())

    def _set_recording_skin(self, on: bool) -> None:
        """Unmistakable ON-AIR state: red frame around the stage + red bars."""
        if on:
            self.centralWidget().setStyleSheet(
                "QWidget#CenterStage{border:3px solid #f2555a;}"
                "QWidget#CommandBar{border-top:3px solid #f2555a;}"
                "QWidget#TitleBar{border-bottom:3px solid #f2555a;}")
        else:
            self.centralWidget().setStyleSheet("")

    def _set_tc_recording(self, on: bool) -> None:
        self._set_recording_skin(on)
        self.tc_label.setStyleSheet(
            "font-family:'IBM Plex Mono',Consolas,monospace;font-size:34px;"
            f"font-weight:700;color:{'#f2555a' if on else '#e9edf0'};")
        self._paint_rec_light(on)

    def _paint_rec_light(self, lit: bool) -> None:
        self.rec_light.setStyleSheet(
            f"color:{'#f2555a' if lit else '#2a2e33'};font-weight:800;"
            "font-size:11px;letter-spacing:1px;")

    def _keep(self, thread: QThread) -> QThread:
        """Hold a reference to `thread` until it has finished (see __init__)."""
        self._threads = [t for t in self._threads if t.isRunning()]
        self._threads.append(thread)
        return thread

    def _log(self, msg: str) -> None:
        """On-screen log panel + stdout. Every action and failure lands here."""
        print(f"[ssd_dhvani] {msg}")
        box = getattr(self, "log_box", None)
        if box is not None:
            ts = time.strftime("%H:%M:%S")
            box.append(f"[{ts}] {msg}")
            box.verticalScrollBar().setValue(box.verticalScrollBar().maximum())

    def _log_exc(self, prefix: str) -> None:
        """Log a full traceback so the real error is visible in the app."""
        self._log(f"✕ {prefix}:\n{traceback.format_exc()}")

    def closeEvent(self, event):  # noqa: N802
        # Finalize an in-progress take FIRST: closing mid-record otherwise
        # leaves the take out of the manifest until the next reconcile.
        if self._recording:
            self._log("Closing while recording — finalizing the take first…")
            try:
                self._stop_record()
            except Exception:
                self._log_exc("Could not finalize the take on close")
        fin = self._fin_worker
        if fin is not None and fin.isRunning():
            self._log("Waiting for the last take to finish saving…")
            fin.wait(120000)
        if self._pub_worker is not None and self._pub_worker.isRunning():
            self._pub_worker.wait(3000)
        fw = getattr(self, "_facial_worker", None)
        if fw is not None and fw.isRunning():
            self._log("Cancelling the facial import (the take keeps its "
                      "previous video, if any)…")
            fw.stop()
        for t in list(self._threads):
            if t.isRunning():
                t.wait(10000)       # never destroy a running QThread
        self._close_input()
        sync = getattr(self, "_sync_worker", None)
        if sync is not None and sync.isRunning():
            sync.stop()               # finishes the current project, then exits
            sync.wait(8000)
        for w in getattr(self, "_folder_workers", []):
            if w.isRunning():
                w.wait(10000)
        off = getattr(self, "_offline_worker", None)
        if off is not None and off.isRunning():
            off.stop()                # finishes the current file, then exits
            off.wait(30000)
        plan = getattr(self, "_offline_plan", None)
        if plan is not None and plan.isRunning():
            plan.wait(10000)
        try:
            self.shotdb.close()
        except Exception:
            pass
        super().closeEvent(event)
