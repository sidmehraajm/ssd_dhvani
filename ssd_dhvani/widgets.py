"""Custom widgets: Record button, level meters, track strips, review tiles.

Kept separate from main_window so the layout code stays readable.
"""
from __future__ import annotations

import math
import time
from typing import Optional

from PySide6.QtCore import Qt, Signal, QRectF, QPointF
from PySide6.QtGui import QPainter, QColor, QBrush, QPen, QPolygonF, QPixmap
from PySide6.QtWidgets import (QPushButton, QFrame, QLabel, QVBoxLayout,
                               QHBoxLayout, QWidget, QMenu, QComboBox,
                               QCheckBox, QProgressBar)

from . import theme as _theme
from .manifest import TRACK_ROLES


def _themed_menu(parent) -> QMenu:
    """A popup that follows the app theme.

    Tiles set an inline stylesheet to paint their dark frame, and a QMenu
    parented to them INHERITS it -- which left menus dark in light mode.
    """
    menu = QMenu(parent)
    menu.setStyleSheet(_theme.menu_qss())
    return menu


class ClickableFrame(QFrame):
    """A QFrame that emits `clicked` -- used for selectable take rows.

    (Reassigning mousePressEvent as an instance attribute does NOT work: Qt
    dispatches the virtual method in C++ and never sees the Python attribute.)
    """
    clicked = Signal()

    def mousePressEvent(self, _e):  # noqa: N802
        self.clicked.emit()


# --------------------------------------------------------------------------- #
# Record button -- red circle (idle) / grey rounded square (recording)
# --------------------------------------------------------------------------- #
class RecordButton(QPushButton):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Record")
        self.setFixedSize(60, 60)
        self._recording = False
        self.setCursor(Qt.PointingHandCursor)

    def set_recording(self, on: bool) -> None:
        self._recording = on
        self.update()

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255, 30), 3))
        enabled = self.isEnabled()
        base = "#3b4148" if (self._recording or not enabled) else "#d1373d"
        p.setBrush(QBrush(QColor(base)))
        p.drawEllipse(2, 2, 56, 56)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor("#ffffff" if enabled else "#8b9298")))
        if self._recording:
            p.drawRoundedRect(QRectF(21, 21, 18, 18), 4, 4)   # stop square
        else:
            p.drawEllipse(19, 19, 22, 22)                     # record dot
        p.end()


# --------------------------------------------------------------------------- #
# Level meter
# --------------------------------------------------------------------------- #
#: The meter's range. -60 dBFS is effectively "nothing"; 0 is the ceiling.
METER_FLOOR_DB = -60.0
#: Where good speech peaks should land (the green→yellow→red boundaries).
METER_YELLOW_DB = -12.0
METER_RED_DB = -6.0
#: Scale labels drawn under the meter.
METER_SCALE = (-60, -48, -36, -24, -18, -12, -6, -3, 0)

#: One colour per track (the stripe on the left of its strip).
TRACK_COLORS = ("#e0952f", "#4b9fff", "#20c997", "#c77dff", "#f2555a",
                "#ffd166")


def level_to_db(level: float) -> float:
    return 20.0 * math.log10(level) if level > 0 else -200.0


def db_to_unit(db: float) -> float:
    """dBFS -> 0..1 position on the meter/waveform scale."""
    return max(0.0, min(1.0, (db - METER_FLOOR_DB) / -METER_FLOOR_DB))


class LevelMeter(QWidget):
    """Horizontal peak meter with a dB scale, a 1.5 s peak hold and a
    latching CLIP light.

    Green below -12 dBFS, yellow -12..-6, red above -6. The CLIP light stays
    lit after any full-scale sample until you click the meter -- a clip in
    the middle of a take is otherwise easy to miss.
    """

    HOLD_SECS = 1.5
    FALL_DB_PER_SEC = 24.0     # how fast the bar falls (smooth, readable)
    BAR_H = 14

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(self.BAR_H + 14)
        self.setMinimumWidth(220)
        self._db = METER_FLOOR_DB
        self._hold_db = METER_FLOOR_DB
        self._hold_t = 0.0
        self._last_t = time.perf_counter()
        self._clip = False
        self.setToolTip("Peak level. Aim for speech peaks between -12 and "
                        "-6 dBFS (yellow). Click to reset the CLIP light.")

    def set_level(self, level: float, clipped: bool = False) -> None:
        now = time.perf_counter()
        db = max(METER_FLOOR_DB, level_to_db(level))
        fallen = self._db - self.FALL_DB_PER_SEC * (now - self._last_t)
        self._db = max(db, fallen, METER_FLOOR_DB)
        self._last_t = now
        if db >= self._hold_db or now - self._hold_t > self.HOLD_SECS:
            self._hold_db, self._hold_t = db, now
        if clipped:
            self._clip = True
        self.update()

    @property
    def peak_hold_db(self) -> float:
        return self._hold_db

    @property
    def clipped(self) -> bool:
        return self._clip

    def reset_clip(self) -> None:
        self._clip = False
        self.update()

    def mousePressEvent(self, _e):  # noqa: N802
        self.reset_clip()

    def paintEvent(self, _ev):  # noqa: N802
        p = QPainter(self)
        clip_w = 38
        w = self.width() - clip_w - 6
        bh = self.BAR_H
        p.fillRect(0, 0, w, bh, QColor("#07090b"))
        # colour zones, lit up to the current level, dim beyond it
        x_lvl = w * db_to_unit(self._db)
        zones = ((METER_FLOOR_DB, METER_YELLOW_DB, "#2f9e44"),
                 (METER_YELLOW_DB, METER_RED_DB, "#e0b12f"),
                 (METER_RED_DB, 0.0, "#f2555a"))
        for lo, hi, col in zones:
            x0, x1 = w * db_to_unit(lo), w * db_to_unit(hi)
            dim = QColor(col)
            dim.setAlpha(38)
            p.fillRect(QRectF(x0, 2, x1 - x0, bh - 4), dim)
            x1 = min(x1, x_lvl)
            if x1 > x0:
                p.fillRect(QRectF(x0, 2, x1 - x0, bh - 4), QColor(col))
        # peak hold line
        if self._hold_db > METER_FLOOR_DB:
            xh = w * db_to_unit(self._hold_db)
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.drawLine(QPointF(xh, 1), QPointF(xh, bh - 1))
        # scale: ticks + dB labels under the bar
        f = p.font()
        f.setPixelSize(9)
        p.setFont(f)
        for db in METER_SCALE:
            x = w * db_to_unit(db)
            p.setPen(QPen(QColor(255, 255, 255, 60), 1))
            p.drawLine(QPointF(x, bh), QPointF(x, bh + 3))
            p.setPen(QColor("#6b737a"))
            txt = str(db)
            tw = p.fontMetrics().horizontalAdvance(txt)
            p.drawText(QPointF(min(max(0.0, x - tw / 2), w - tw), bh + 13), txt)
        # clip light
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#f2555a" if self._clip else "#1d2125"))
        p.drawRoundedRect(QRectF(w + 6, 0, clip_w, bh), 3, 3)
        p.setPen(QColor("#ffffff" if self._clip else "#5f6870"))
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(w + 6, 0, clip_w, bh), Qt.AlignCenter, "CLIP")
        p.end()


class ScrollingWave(QWidget):
    """The last few seconds of a track as a scrolling waveform envelope --
    like the input display on a field recorder. Newest audio on the right.
    Heights use the dB scale, so quiet speech is still visible."""

    SECONDS = 8.0
    TICKS_PER_SEC = 25       # matches the UI timer

    def __init__(self, color: str = "#e0952f", parent=None) -> None:
        super().__init__(parent)
        self._n = int(self.SECONDS * self.TICKS_PER_SEC)
        self._vals = [0.0] * self._n
        self._rec = [False] * self._n
        self._color = QColor(color)
        self.setMinimumHeight(46)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def push(self, level: float, recording: bool) -> None:
        self._vals.pop(0)
        self._rec.pop(0)
        self._vals.append(db_to_unit(level_to_db(level)))
        self._rec.append(recording)
        self.update()

    def paintEvent(self, _ev):  # noqa: N802
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#07090b"))
        mid = h / 2.0
        # -12 dBFS guide lines: where speech peaks should reach
        g = db_to_unit(METER_YELLOW_DB) * (mid - 2)
        p.setPen(QPen(QColor(224, 177, 47, 45), 1, Qt.DashLine))
        p.drawLine(QPointF(0, mid - g), QPointF(w, mid - g))
        p.drawLine(QPointF(0, mid + g), QPointF(w, mid + g))
        step = w / float(self._n)
        rec_col = QColor("#f2555a")
        for i, (v, r) in enumerate(zip(self._vals, self._rec)):
            if v <= 0:
                continue
            a = v * (mid - 2)
            c = rec_col if r else self._color
            p.fillRect(QRectF(i * step, mid - a, max(1.0, step - 0.5), 2 * a), c)
        p.setPen(QPen(QColor(255, 255, 255, 25), 1))
        p.drawLine(QPointF(0, mid), QPointF(w, mid))
        p.end()


# --------------------------------------------------------------------------- #
# Channel strip (Record tab): role + input channel + waveform + meter
# --------------------------------------------------------------------------- #
class TrackStrip(QFrame):
    """One recorded track: which device channel, under which role name.

    The role is the storage key (file name and AYON representation name),
    so it must be unique within a take -- the main window enforces that.

    Slots are bound METHODS, not lambdas capturing `self`: a lambda held by a
    child widget's signal keeps the strip's Python wrapper alive after Qt
    deleted it, and that dangling wrapper is how PySide apps crash later.
    """

    role_changed = Signal(object, str)       # (strip, new role)
    channel_changed = Signal(object, int)    # (strip, new channel 0-based)
    remove_requested = Signal(object)        # strip

    def __init__(self, role: str, channel: int, n_channels: int,
                 color: str = "#e0952f") -> None:
        super().__init__()
        self.setObjectName("TrackStrip")
        self._color = color
        self._recording = False
        self._style()
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 10, 12, 10)
        h.setSpacing(14)

        # ---- left: arm light + name + input ------------------------------ #
        left = QVBoxLayout()
        left.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.dot = QLabel("●")
        self.dot.setStyleSheet("color:#3b4148;font-size:15px;border:none;")
        self.dot.setToolTip("Red = this track is recording")
        top.addWidget(self.dot)
        self.cb_role = QComboBox()
        self.cb_role.setEditable(True)
        self.cb_role.setInsertPolicy(QComboBox.NoInsert)
        self.cb_role.addItems(TRACK_ROLES)
        self.cb_role.setMinimumWidth(130)
        self.cb_role.setToolTip("Track name — becomes the file name "
                                "({shot}_{name}_takeN.wav) and the AYON "
                                "representation name. Must be unique.")
        self.cb_role.setCurrentText(role)
        self.cb_role.lineEdit().editingFinished.connect(self._emit_role)
        self.cb_role.activated.connect(self._emit_role)
        top.addWidget(self.cb_role)
        left.addLayout(top)
        self.cb_channel = QComboBox()
        self.cb_channel.setToolTip("Which input channel of the device this "
                                   "track records")
        self.set_channel_count(n_channels, channel)
        self.cb_channel.currentIndexChanged.connect(self._emit_channel)
        left.addWidget(self.cb_channel)
        left.addStretch()
        h.addLayout(left)

        # ---- centre: scrolling waveform over the meter -------------------- #
        mid = QVBoxLayout()
        mid.setSpacing(4)
        self.wave = ScrollingWave(color)
        mid.addWidget(self.wave, 1)
        self.meter = LevelMeter()
        mid.addWidget(self.meter)
        h.addLayout(mid, 1)

        # ---- right: big peak readout + remove ----------------------------- #
        right = QVBoxLayout()
        right.setSpacing(0)
        self.db_label = QLabel("-inf")
        self.db_label.setMinimumWidth(84)
        self.db_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.db_label.setStyleSheet(
            "color:#e9edf0;font-family:'IBM Plex Mono',Consolas,monospace;"
            "font-size:22px;font-weight:700;border:none;")
        right.addWidget(self.db_label)
        cap = QLabel("PEAK dBFS")
        cap.setAlignment(Qt.AlignRight)
        cap.setStyleSheet("color:#5f6870;font-size:9px;letter-spacing:1px;"
                          "border:none;")
        right.addWidget(cap)
        right.addStretch()
        self.rm_btn = QPushButton("Remove")
        self.rm_btn.setToolTip("Remove this track")
        self.rm_btn.setStyleSheet("font-size:10px;padding:2px 8px;")
        self.rm_btn.clicked.connect(self._emit_remove)
        right.addWidget(self.rm_btn, 0, Qt.AlignRight)
        h.addLayout(right)
        self.setMinimumHeight(104)

    def _style(self) -> None:
        border = "#f2555a" if self._recording else "#1c1f23"
        self.setStyleSheet(
            "QFrame#TrackStrip{background:#0f1113;"
            f"border:1px solid {border};border-left:4px solid {self._color};"
            "border-radius:10px;}")

    # ---- signal relays (bound methods, see class docstring) ------------- #
    def _emit_role(self, *_a) -> None:
        self.role_changed.emit(self, self.role)

    def _emit_channel(self, index: int) -> None:
        self.channel_changed.emit(self, index)

    def _emit_remove(self) -> None:
        self.remove_requested.emit(self)

    @property
    def role(self) -> str:
        return self.cb_role.currentText().strip()

    def set_role(self, role: str) -> None:
        self.cb_role.blockSignals(True)
        self.cb_role.setCurrentText(role)
        self.cb_role.blockSignals(False)

    @property
    def channel(self) -> int:
        return max(0, self.cb_channel.currentIndex())

    def set_channel_count(self, n: int, keep: int | None = None) -> None:
        keep = self.channel if keep is None else keep
        self.cb_channel.blockSignals(True)
        self.cb_channel.clear()
        for i in range(max(1, n)):
            self.cb_channel.addItem(f"Input {i + 1}")
        self.cb_channel.setCurrentIndex(min(max(0, keep), max(1, n) - 1))
        self.cb_channel.blockSignals(False)

    def set_level(self, level: float, clipped: bool) -> None:
        self.meter.set_level(level, clipped)
        self.wave.push(level, self._recording)
        hold = self.meter.peak_hold_db
        if hold <= METER_FLOOR_DB:
            self.db_label.setText("-inf")
        else:
            self.db_label.setText(f"{hold:5.1f}")
        col = ("#f2555a" if self.meter.clipped or hold > METER_RED_DB else
               "#e0b12f" if hold > METER_YELLOW_DB else "#e9edf0")
        self.db_label.setStyleSheet(
            f"color:{col};font-family:'IBM Plex Mono',Consolas,monospace;"
            "font-size:22px;font-weight:700;border:none;")

    def set_recording(self, on: bool) -> None:
        self._recording = on
        self.dot.setStyleSheet(
            f"color:{'#f2555a' if on else '#3b4148'};font-size:15px;border:none;")
        self._style()
        for w in (self.cb_role, self.cb_channel, self.rm_btn):
            w.setEnabled(not on)     # never change routing mid-take


# --------------------------------------------------------------------------- #
# Review tile: waveform + caption
# --------------------------------------------------------------------------- #
class WaveformView(QWidget):
    """Paints a (mins, maxs) overview. Colour hints the level: red columns
    are at full scale (clipped)."""

    def __init__(self, peaks=None, parent=None) -> None:
        super().__init__(parent)
        self._peaks = peaks
        self.setMinimumHeight(120)

    def set_peaks(self, peaks) -> None:
        self._peaks = peaks
        self.update()

    def paintEvent(self, _ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#0b0d0f"))
        mid = h / 2.0
        p.setPen(QPen(QColor(255, 255, 255, 25), 1))
        p.drawLine(QPointF(0, mid), QPointF(w, mid))
        if self._peaks is None:
            p.setPen(QColor("#6b737a"))
            p.drawText(self.rect(), Qt.AlignCenter, "no waveform")
            p.end()
            return
        mins, maxs = self._peaks
        n = len(maxs)
        if n == 0:
            p.end()
            return
        top, bot = [], []
        for i in range(n):
            x = i * (w - 1) / max(1, n - 1)
            top.append(QPointF(x, mid - float(maxs[i]) * (mid - 2)))
            bot.append(QPointF(x, mid - float(mins[i]) * (mid - 2)))
        poly = QPolygonF(top + list(reversed(bot)))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#e0952f"))
        p.drawPolygon(poly)
        # mark clipped columns
        p.setPen(QPen(QColor("#f2555a"), 1))
        for i in range(n):
            if maxs[i] >= 0.999 or mins[i] <= -0.999:
                x = i * (w - 1) / max(1, n - 1)
                p.drawLine(QPointF(x, 0), QPointF(x, h))
        p.end()


class ReviewTile(QFrame):
    """Review-tab card: waveform + caption. Double-click opens the file in
    the OS default player."""

    open_requested = Signal(str)    # file path
    export_requested = Signal(str)  # file path -> copy to the export folder
    reveal_requested = Signal(str)  # file path -> show its folder

    def __init__(self, name: str, path: str, peaks=None,
                 detail: str = "", highlight: bool = False,
                 image: Optional[str] = None) -> None:
        super().__init__()
        self._path = path
        self.setMinimumSize(320, 190)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            "QFrame{background:#0f1113;border-radius:11px;"
            + ("border:1px solid #e0952f;" if highlight else
               "border:1px solid #1c1f23;") + "}")
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 4)
        v.setSpacing(4)
        if image is not None:                 # a video: show its thumbnail
            pic = QLabel()
            pic.setAlignment(Qt.AlignCenter)
            pic.setMinimumHeight(120)
            pm = QPixmap(image) if image else QPixmap()
            if not pm.isNull():
                pic.setPixmap(pm.scaled(420, 200, Qt.KeepAspectRatio,
                                        Qt.SmoothTransformation))
            else:
                pic.setText("▶ video")
                pic.setStyleSheet("color:#e0952f;font-size:26px;border:none;")
            pic.setStyleSheet(pic.styleSheet() + "background:#07090b;"
                              "border:none;border-radius:6px;")
            v.addWidget(pic, 1)
        else:
            self.wave = WaveformView(peaks)
            v.addWidget(self.wave, 1)
        for text, style in ((name, "color:#c6ced6;font-size:11px;"),
                            (detail, "color:#8b9298;font-size:10px;")):
            if not text:
                continue
            lbl = QLabel(text)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(style + "font-family:'IBM Plex Mono',monospace;"
                              "border:none;")
            v.addWidget(lbl)

    def mouseDoubleClickEvent(self, _e):  # noqa: N802
        self.open_requested.emit(self._path)

    def contextMenuEvent(self, e):  # noqa: N802
        menu = _themed_menu(self)
        a2 = menu.addAction("▶  Play")
        a2.triggered.connect(lambda: self.open_requested.emit(self._path))
        act = menu.addAction("⬆  Export copy")
        act.setToolTip("Copy this file to the export folder set at the top "
                       "of this tab")
        act.triggered.connect(lambda: self.export_requested.emit(self._path))
        menu.addSeparator()
        a3 = menu.addAction("📂  Show in folder")
        a3.triggered.connect(lambda: self.reveal_requested.emit(self._path))
        menu.exec(e.globalPos())


def fmt_duration(secs: float) -> str:
    secs = max(0.0, float(secs or 0))
    m, s = divmod(secs, 60)
    return f"{int(m)}:{s:05.2f}"


def fmt_db(db: Optional[float]) -> str:
    if db is None or db <= -119:
        return "-inf dBFS"
    return f"{db:.1f} dBFS"


# --------------------------------------------------------------------------- #
# Facial-recording drop zone (Record tab, under the tracks)
# --------------------------------------------------------------------------- #
class DropZone(QFrame):
    """Big drop target for the facial-recording video (or a .zip with it).

    Drop a file, or click to browse. The main window decides which take it
    attaches to and shows that here ('→ attaches to Take 03').
    """

    files_dropped = Signal(list)          # [path, ...]
    browse_requested = Signal()

    ACCEPT = (".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mxf", ".webm", ".zip")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(150)
        self._hover = False
        self._busy = False
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 12)
        v.setSpacing(6)
        head = QHBoxLayout()
        title = QLabel("FACIAL RECORDING")
        title.setStyleSheet("color:#e0952f;font-weight:800;font-size:11px;"
                            "letter-spacing:2px;border:none;background:none;")
        head.addWidget(title)
        head.addStretch()
        self.new_take = QCheckBox("as a new take")
        self.new_take.setToolTip(
            "Tick to put the video on a NEW take (video only). Unticked, it "
            "attaches to the selected take — record the audio, then drop "
            "that performance's video.")
        self.new_take.setStyleSheet("border:none;background:none;")
        head.addWidget(self.new_take)
        v.addLayout(head)
        self.big = QLabel("⤓  Drop the facial video or a .zip here  ·  or click to browse")
        self.big.setAlignment(Qt.AlignCenter)
        self.big.setStyleSheet("color:#c6ced6;font-size:15px;font-weight:600;"
                               "border:none;background:none;")
        v.addWidget(self.big, 1)
        self.target = QLabel("")
        self.target.setAlignment(Qt.AlignCenter)
        self.target.setWordWrap(True)
        self.target.setStyleSheet("color:#8b9298;font-size:11px;border:none;"
                                  "background:none;")
        v.addWidget(self.target)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(True)
        self.progress.setFixedHeight(14)
        self.progress.setVisible(False)
        v.addWidget(self.progress)
        self._restyle()

    def _restyle(self) -> None:
        border = "#e0952f" if (self._hover or self._busy) else "#3b4148"
        bg = "#16130d" if self._hover else "#0b0d0f"
        self.setStyleSheet(
            f"QFrame#DropZone{{background:{bg};border:2px dashed {border};"
            "border-radius:12px;}")

    def set_target_text(self, text: str) -> None:
        self.target.setText(text)

    def set_busy(self, busy: bool, text: str = "", frac: float = 0.0) -> None:
        self._busy = busy
        self.progress.setVisible(busy)
        if busy:
            self.progress.setValue(int(frac * 1000))
            self.progress.setFormat(text or "importing…")
        self._restyle()

    @classmethod
    def _accepted(cls, urls) -> list[str]:
        out = []
        for u in urls:
            p = u.toLocalFile()
            if p and p.lower().endswith(cls.ACCEPT):
                out.append(p)
        return out

    def dragEnterEvent(self, e):  # noqa: N802
        if not self._busy and e.mimeData().hasUrls() and \
                self._accepted(e.mimeData().urls()):
            self._hover = True
            self._restyle()
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragLeaveEvent(self, _e):  # noqa: N802
        self._hover = False
        self._restyle()

    def dropEvent(self, e):  # noqa: N802
        self._hover = False
        self._restyle()
        paths = self._accepted(e.mimeData().urls())
        if paths:
            e.acceptProposedAction()
            self.files_dropped.emit(paths)

    def mouseReleaseEvent(self, e):  # noqa: N802
        if not self._busy and e.button() == Qt.LeftButton:
            self.browse_requested.emit()
