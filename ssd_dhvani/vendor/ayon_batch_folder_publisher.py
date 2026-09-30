import sys
import os
import uuid
import traceback
import datetime
import re
import shutil
import base64
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QComboBox, QPushButton, QFileDialog,
                             QTextEdit, QMessageBox, QProgressBar, QLineEdit,
                             QGroupBox, QFormLayout, QCompleter, QCheckBox,
                             QTableWidget, QTableWidgetItem, QHeaderView,
                             QAbstractItemView, QRadioButton, QMenu, QAction)
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QSettings, QTimer
from PyQt5.QtGui import QIcon

try:
    import ayon_api
    from ayon_api.entity_hub import EntityHub
except ImportError:
    print("Warning: ayon_api or EntityHub not found.")


# ══════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════
VARIANT_PRESETS = [
    "main",
    "Reference_",
    "AI_",
    "Render_",
    "Texture_",
    "Concept_",
    "Background_",
    "Comfy_",
    "Playblast_",
    "cctx_",
    "pc2_"
    
]


# ══════════════════════════════════════════════════════════════════════════
# NoScrollComboBox
# Default QComboBox changes its selected value when the mouse wheel scrolls
# over it — this version ignores wheel events and lets them bubble up to
# the table so the table scrolls instead.
# ══════════════════════════════════════════════════════════════════════════
class NoScrollComboBox(QComboBox):
    def wheelEvent(self, e):
        e.ignore()


# ══════════════════════════════════════════════════════════════════════════
# DnDLineEdit — drag-and-drop aware line edit
# ══════════════════════════════════════════════════════════════════════════
class DnDLineEdit(QLineEdit):
    path_dropped = pyqtSignal(str)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            self.setText(path)
            self.path_dropped.emit(path)


# ══════════════════════════════════════════════════════════════════════════
# DnDReviewEdit — drag-and-drop aware line edit for review media
# ══════════════════════════════════════════════════════════════════════════
class DnDReviewEdit(QLineEdit):
    """Drag-and-drop enabled line edit specifically for review media paths."""
    path_dropped = pyqtSignal(str)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            self.setText(path)
            self.path_dropped.emit(path)


# ══════════════════════════════════════════════════════════════════════════
# PathTypeWidget — Folder / File radio pair with a change signal
# ══════════════════════════════════════════════════════════════════════════
class PathTypeWidget(QWidget):
    type_changed = pyqtSignal(bool)   # True = File, False = Folder

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 4, 0)
        lay.setSpacing(8)
        self.rb_folder = QRadioButton("Folder")
        self.rb_file   = QRadioButton("File")
        self.rb_folder.setChecked(True)
        lay.addWidget(self.rb_folder)
        lay.addWidget(self.rb_file)
        lay.addStretch()
        self.rb_file.toggled.connect(self.type_changed.emit)

    def is_file(self) -> bool:
        return self.rb_file.isChecked()

    def set_is_file(self, value: bool):
        (self.rb_file if value else self.rb_folder).setChecked(True)


# ══════════════════════════════════════════════════════════════════════════
# VariantComboBox — editable combo with preset variants
# ══════════════════════════════════════════════════════════════════════════
class VariantComboBox(NoScrollComboBox):
    """
    Editable combo that ships with preset variant names.
    Users can still type a custom value.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.addItems(VARIANT_PRESETS)
        self.setCurrentText("main")
        self.setPlaceholderText("main")
        # Allow partial matching in the popup
        completer = self.completer()
        if completer:
            completer.setCompletionMode(QCompleter.PopupCompletion)
            completer.setFilterMode(Qt.MatchContains)


# ══════════════════════════════════════════════════════════════════════════
# PublishWorker — sequential background publisher
# ══════════════════════════════════════════════════════════════════════════
class PublishWorker(QThread):
    log_msg         = pyqtSignal(str)
    progress_update = pyqtSignal(int, int)        # job_index, pct
    finished_signal = pyqtSignal(int, bool, str)  # job_index, ok, msg

    def __init__(self, job_index, local_path, project, folder_id, task_id,
                 author_name, variant, comment, review_path=None):
        super().__init__()
        self.job_index   = job_index
        self.local_path  = local_path
        self.project     = project
        self.folder_id   = folder_id
        self.task_id     = task_id
        self.author_name = author_name
        self.variant     = variant or "main"
        self.comment     = comment
        self.review_path = review_path

    def get_publish_path(self, product_type, product_name, version_str):
        try:
            roots = ayon_api.get_project_roots_by_site_id(self.project)
            if not roots:
                raise ValueError(f"No Roots defined for project '{self.project}'.")
            root_path = (roots.get("publish") or roots.get("work")
                         or roots.get("root") or list(roots.values())[0])
            fe = ayon_api.get_folder_by_id(self.project, self.folder_id)
            full = os.path.join(
                root_path.replace("\\", "/"), self.project,
                fe["path"].strip("/"),
                "publish", product_type, product_name, self.variant, version_str
            )
            return os.path.normpath(full)
        except Exception as e:
            self.log_msg.emit(f"Path Error: {e}")
            return None

    def run(self):
        try:
            hub  = EntityHub(self.project)
            fe   = ayon_api.get_folder_by_id(self.project, self.folder_id)
            name = fe["name"]

            if self.task_id:
                te   = ayon_api.get_task_by_id(self.project, self.task_id)
                raw  = f"{name}_{te['name']}"
            else:
                raw  = name

            safe         = re.sub(r"[^a-zA-Z0-9_.-]", "_", raw)
            product_type = "generic"
            self.log_msg.emit(f"[Job {self.job_index+1}] {safe}  |  variant: {self.variant}")

            existing = list(ayon_api.get_products(
                self.project, folder_ids=[self.folder_id], product_names=[safe]
            ))
            if existing:
                pid   = existing[0]["id"]
                lv    = ayon_api.get_last_version_by_product_id(self.project, pid)
                next_v = (lv["version"] + 1) if lv else 1
            else:
                np    = hub.add_new_product(product_type=product_type, name=safe,
                                            folder_id=self.folder_id)
                hub.commit_changes()
                pid   = np["id"]
                next_v = 1

            ver_str = f"v{next_v:03d}"
            vkw     = {"version": next_v, "product_id": pid}
            if self.task_id:
                vkw["task_id"] = self.task_id

            ve = hub.add_new_version(**vkw)
            if self.author_name:
                ve["author"] = self.author_name
            try:    ve.attribs["source"]  = self.local_path
            except Exception: pass
            if self.comment:
                try: ve.attribs["comment"] = self.comment
                except Exception: pass
            hub.commit_changes()
            vid = ve["id"]

            files = []
            if os.path.isfile(self.local_path):
                fn = os.path.basename(self.local_path)
                files.append((self.local_path, fn, fn))
            elif os.path.isdir(self.local_path):
                for root, _, fnames in os.walk(self.local_path):
                    for f in fnames:
                        src = os.path.join(root, f)
                        rel = os.path.relpath(src, self.local_path)
                        files.append((src, rel, f))
            else:
                self.finished_signal.emit(self.job_index, False, "Source path does not exist.")
                return

            if not files:
                self.finished_signal.emit(self.job_index, False, "Empty folder or invalid file.")
                return

            dest_base = self.get_publish_path(product_type, safe, ver_str)
            if not dest_base:
                raise Exception("Could not determine server publish path.")
            self.log_msg.emit(f"[Job {self.job_index+1}] → {dest_base}")

            for i, (src, rel, fname) in enumerate(files):
                dest = os.path.join(dest_base, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                repre = re.sub(r"[^a-zA-Z0-9_]", "_", os.path.splitext(rel)[0])
                ayon_api.post(
                    f"projects/{self.project}/representations",
                    versionId=vid, name=repre,
                    attrib={"ext": os.path.splitext(fname)[1].lstrip("."),
                            "path": dest, "template": dest},
                    data={"path": dest},
                    files=[{"id": str(uuid.uuid4()), "name": fname,
                            "path": dest, "size": os.path.getsize(dest)}],
                )
                self.progress_update.emit(self.job_index,
                                          int((i + 1) / len(files) * 100))

            if self.review_path and os.path.isfile(self.review_path):
                self.log_msg.emit(f"[Job {self.job_index+1}] Uploading review…")
                try:
                    ayon_api.upload_reviewable(self.project, vid, self.review_path)
                    self.log_msg.emit(f"[Job {self.job_index+1}] Review uploaded.")
                except Exception as e:
                    self.log_msg.emit(f"Warning: review upload failed: {e}")

            self.finished_signal.emit(
                self.job_index, True, f"Published {len(files)} file(s) → {ver_str}"
            )
        except Exception:
            self.finished_signal.emit(self.job_index, False, traceback.format_exc())


# ══════════════════════════════════════════════════════════════════════════
# Main Window
# ══════════════════════════════════════════════════════════════════════════
class AyonBatchPublisherUI(QWidget):

    # ── Table column indices ───────────────────────────────────────────
    COL_CHECK   = 0
    COL_TYPE    = 1
    COL_PATH    = 2
    COL_BROWSE  = 3
    COL_FOLDER  = 4
    COL_TASK    = 5
    COL_REVIEW  = 6
    COL_RBROWSE = 7
    COL_VARIANT = 8
    COL_COMMENT = 9
    COL_STATUS  = 10
    NUM_COLS    = 11

    def __init__(self):
        super().__init__()
        self.setWindowTitle("AYON Batch Folder Publisher")
        self.resize(1440, 820)

        self.cached_folders: dict = {}
        # Each entry is a dict of {widget_name: widget, 'cached_tasks': dict}
        self.row_data_list: list  = []
        self._bulk_propagating    = False

        self.current_user   = None
        self.publish_queue  = []
        self.current_worker = None
        self.settings       = QSettings("Ynput", "BatchFolderPublisher")

        self.apply_modern_theme()
        self.init_ui()
        self.try_auto_login()

    # ──────────────────────────────────────────────────────────────────
    # Styling
    # ──────────────────────────────────────────────────────────────────
    def apply_modern_theme(self):
        self.setStyleSheet("""
        QWidget {
            background-color: #252525; color: #dcdcdc;
            font-family: 'Segoe UI', 'Inter', sans-serif; font-size: 13px;
        }
        QGroupBox {
            border: 1px solid #3a3a3a; border-radius: 6px;
            margin-top: 18px; font-weight: 600; padding-top: 8px;
        }
        QGroupBox::title {
            subcontrol-origin: margin; subcontrol-position: top left;
            padding: 0 6px; color: #00c49a;
            font-size: 10px; letter-spacing: 1.2px;
        }
        QLineEdit, QComboBox, QTextEdit {
            background-color: #303030; border: 1px solid #444;
            border-radius: 3px; padding: 3px 6px; color: #e8e8e8;
            selection-background-color: #00b894;
        }
        QLineEdit:focus, QComboBox:focus, QTextEdit:focus { border-color: #00b894; }
        QLineEdit:disabled, QComboBox:disabled { color: #555; background: #282828; }

        /* ── Combobox dropdown button — clearly visible for artists ── */
        QComboBox {
            padding-right: 28px;   /* room for the arrow button */
        }
        QComboBox::drop-down {
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 26px;
            background-color: #3f3f3f;
            border-left: 1px solid #555;
            border-top-right-radius: 3px;
            border-bottom-right-radius: 3px;
        }
        QComboBox::drop-down:hover {
            background-color: #00b894;
            border-left-color: #00b894;
        }
        QComboBox::drop-down:pressed {
            background-color: #009e7e;
        }
        QComboBox::down-arrow {
            image: none;
            width: 0px; height: 0px;
            border-left:   5px solid transparent;
            border-right:  5px solid transparent;
            border-top:    6px solid #c0c0c0;
        }
        QComboBox::down-arrow:hover  { border-top-color: #ffffff; }
        QComboBox::down-arrow:on     { border-top-color: #ffffff; }
        QComboBox:disabled::drop-down { background-color: #2c2c2c; border-left-color: #3a3a3a; }
        QComboBox:disabled::down-arrow { border-top-color: #4a4a4a; }
        QComboBox QAbstractItemView {
            background: #333; border: 1px solid #555;
            selection-background-color: #00b894; color: #e8e8e8;
            padding: 2px;
        }

        /* ── Table ── */
        QTableWidget {
            background-color: #252525; gridline-color: #333;
            border: 1px solid #3a3a3a; border-radius: 4px;
            outline: none;
        }
        QTableWidget::item { padding: 0px; border: none; }
        QTableWidget::item:selected { background: transparent; }
        QHeaderView {
            background-color: #1c1c1c; border: none;
        }
        QHeaderView::section {
            background-color: #1c1c1c; color: #00c49a;
            font-size: 10px; font-weight: 700; letter-spacing: 0.8px;
            padding: 5px 6px; border: none;
            border-right: 1px solid #333; border-bottom: 1px solid #333;
        }
        QHeaderView::section:hover { background-color: #222; color: #00e6b0; }
        QHeaderView::section:first { border-radius: 0; }
        QHeaderView::section[clickable="true"] { cursor: pointer; }

        QScrollBar:vertical {
            background: #1e1e1e; width: 8px; border-radius: 4px;
        }
        QScrollBar::handle:vertical {
            background: #444; border-radius: 4px; min-height: 24px;
        }
        QScrollBar::handle:vertical:hover { background: #00b894; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar:horizontal {
            background: #1e1e1e; height: 8px; border-radius: 4px;
        }
        QScrollBar::handle:horizontal {
            background: #444; border-radius: 4px; min-width: 24px;
        }
        QScrollBar::handle:horizontal:hover { background: #00b894; }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

        /* ── Buttons ── */
        QPushButton {
            background-color: #3a3a3a; border: none; border-radius: 4px;
            padding: 6px 14px; color: #e0e0e0; font-weight: 600;
        }
        QPushButton:hover  { background-color: #484848; }
        QPushButton:pressed { background-color: #303030; }
        QPushButton:disabled { color: #4a4a4a; background: #2c2c2c; }
        QPushButton#MainAction {
            background-color: #00b894; color: #fff;
            font-size: 14px; padding: 11px; border-radius: 5px; letter-spacing: 0.5px;
        }
        QPushButton#MainAction:hover   { background-color: #00cfa7; }
        QPushButton#MainAction:pressed { background-color: #009e7e; }
        QPushButton#AddRowBtn  { background-color: #1f6fa8; color: #fff; }
        QPushButton#AddRowBtn:hover  { background-color: #2980b9; }
        QPushButton#BulkBtn    { background-color: #5a2d82; color: #fff; }
        QPushButton#BulkBtn:hover    { background-color: #7d3c98; }
        QPushButton#BulkBtn::menu-indicator { width: 0; }
        QPushButton#RemoveBtn  { background-color: #6e1f1f; color: #fff; }
        QPushButton#RemoveBtn:hover  { background-color: #922b21; }
        QPushButton#LogoutBtn  { background-color: #4a3000; color: #e0b060; }
        QPushButton#LogoutBtn:hover  { background-color: #6b4500; }
        QPushButton#ClearLogBtn {
            background: transparent; color: #555;
            padding: 2px 8px; font-size: 11px; font-weight: 400;
        }
        QPushButton#ClearLogBtn:hover { color: #999; }
        /* Small icon-only buttons inside table cells */
        QPushButton#IconBtn {
            background-color: #383838; border-radius: 3px;
            padding: 2px; font-size: 14px;
        }
        QPushButton#IconBtn:hover { background-color: #484848; }

        /* ── Progress bar ── */
        QProgressBar {
            border: 1px solid #383838; border-radius: 3px;
            text-align: center; background: #2a2a2a;
            color: #aaa; font-size: 11px;
        }
        QProgressBar::chunk { background-color: #00b894; border-radius: 2px; }

        /* ── Checkboxes ── */
        QCheckBox { color: #bbb; spacing: 4px; }
        QCheckBox::indicator {
            width: 14px; height: 14px;
            border-radius: 3px; border: 1px solid #4a4a4a; background: #303030;
        }
        QCheckBox::indicator:checked  { background: #00b894; border-color: #00b894; }
        QCheckBox::indicator:hover    { border-color: #00b894; }

        /* ── Radios ── */
        QRadioButton { color: #bbb; }
        QRadioButton::indicator {
            width: 13px; height: 13px;
            border-radius: 7px; border: 1px solid #555; background: #303030;
        }
        QRadioButton::indicator:checked {
            background: #00b894; border: 2px solid #252525;
            outline: 1px solid #00b894;
        }

        /* ── Menus ── */
        QMenu {
            background: #2e2e2e; border: 1px solid #4a4a4a;
            border-radius: 4px; padding: 4px; color: #e0e0e0;
        }
        QMenu::item { padding: 7px 16px 7px 10px; border-radius: 3px; }
        QMenu::item:selected { background: #00b894; color: #fff; }
        QMenu::separator { height: 1px; background: #3a3a3a; margin: 3px 6px; }
        """)

    # ──────────────────────────────────────────────────────────────────
    # UI construction
    # ──────────────────────────────────────────────────────────────────
    def init_ui(self):
        main = QVBoxLayout(self)
        main.setSpacing(8)
        main.setContentsMargins(16, 14, 16, 14)

        # ── App header ────────────────────────────────────────────────
        hrow = QHBoxLayout()
        title = QLabel("AYON  Batch Folder Publisher")
        title.setStyleSheet(
            "font-size: 17px; font-weight: 700; color: #f0f0f0; letter-spacing: 0.5px;"
        )
        hrow.addWidget(title)
        hrow.addStretch()
        ver = QLabel("v1.1 ")
        ver.setStyleSheet(
            "font-size: 10px; color: #00b894; font-weight: 700;"
            "background: #1a3028; border-radius: 3px; padding: 2px 7px;"
        )
        hrow.addWidget(ver)
        main.addLayout(hrow)

        # ── Auth ──────────────────────────────────────────────────────
        self.group_login = QGroupBox("AUTHENTICATION")
        fl = QFormLayout()
        fl.setSpacing(7)
        self.input_url  = QLineEdit()
        self.input_url.setPlaceholderText("https://your-server.ayon.cloud")
        self.input_user = QLineEdit()
        self.input_user.setPlaceholderText("username")
        self.input_pass = QLineEdit()
        self.input_pass.setEchoMode(QLineEdit.Password)
        self.cb_remember = QCheckBox("Remember Me")
        self.btn_login   = QPushButton("Sign In")
        self.btn_login.clicked.connect(self.handle_login)
        fl.addRow("Server:", self.input_url)
        fl.addRow("User:",   self.input_user)
        fl.addRow("Pass:",   self.input_pass)
        fl.addRow("",        self.cb_remember)
        fl.addRow("",        self.btn_login)
        self.group_login.setLayout(fl)
        main.addWidget(self.group_login)

        # ── Batch publish ─────────────────────────────────────────────
        self.group_publish = QGroupBox("BATCH PUBLISH")
        self.group_publish.setEnabled(False)
        pub = QVBoxLayout()
        pub.setSpacing(7)

        # Toolbar
        tbar = QHBoxLayout()
        tbar.setSpacing(8)
        tbar.addWidget(QLabel("Project:"))
        self.cb_project = QComboBox()
        self.cb_project.setMinimumWidth(200)
        self.cb_project.setMaximumWidth(320)
        self.cb_project.currentIndexChanged.connect(self.cache_project_data)
        tbar.addWidget(self.cb_project)

        # ── Logout button (shown after login) ─────────────────────────
        self.btn_logout = QPushButton("⇠  Logout")
        self.btn_logout.setObjectName("LogoutBtn")
        self.btn_logout.setToolTip("Sign out and return to login screen")
        self.btn_logout.clicked.connect(self.handle_logout)
        tbar.addWidget(self.btn_logout)

        self.lbl_status = QLabel("—")
        self.lbl_status.setStyleSheet("color: #555; font-size: 11px; padding-left: 6px;")
        tbar.addWidget(self.lbl_status)
        tbar.addStretch()

        # Bulk Import button with dropdown menu
        self.btn_bulk = QPushButton("⬇  Bulk Import")
        self.btn_bulk.setObjectName("BulkBtn")
        self.btn_bulk.setToolTip("Auto-create rows from files or a scanned directory")
        bmenu = QMenu(self)
        bmenu.addAction(QAction("📄  Select Multiple Files",              self,
                                triggered=self._bulk_import_files))
        bmenu.addSeparator()
        bmenu.addAction(QAction("📂  Scan Directory  (one row per item)", self,
                                triggered=self._bulk_import_directory))
        self.btn_bulk.setMenu(bmenu)

        self.btn_remove = QPushButton("✕  Remove Selected")
        self.btn_remove.setObjectName("RemoveBtn")
        self.btn_remove.clicked.connect(self.remove_selected_rows)

        self.btn_add_row = QPushButton("＋  Add Job")
        self.btn_add_row.setObjectName("AddRowBtn")
        self.btn_add_row.clicked.connect(self.add_table_row)

        tbar.addWidget(self.btn_bulk)
        tbar.addWidget(self.btn_remove)
        tbar.addWidget(self.btn_add_row)
        pub.addLayout(tbar)

        # ── Main table ────────────────────────────────────────────────
        self.table = QTableWidget(0, self.NUM_COLS)
        self.table.setHorizontalHeaderLabels([
            "☑", "SOURCE TYPE", "LOCAL PATH", "",
            "ASSET FOLDER", "TASK", "REVIEW MEDIA", "",
            "VARIANT", "COMMENT", "STATUS",
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setShowGrid(True)
        self.table.setWordWrap(False)

        hdr = self.table.horizontalHeader()
        # Column resize modes
        hdr.setSectionResizeMode(self.COL_CHECK,   QHeaderView.Fixed)
        hdr.setSectionResizeMode(self.COL_TYPE,    QHeaderView.Fixed)
        hdr.setSectionResizeMode(self.COL_PATH,    QHeaderView.Stretch)
        hdr.setSectionResizeMode(self.COL_BROWSE,  QHeaderView.Fixed)
        hdr.setSectionResizeMode(self.COL_FOLDER,  QHeaderView.Interactive)
        hdr.setSectionResizeMode(self.COL_TASK,    QHeaderView.Interactive)
        hdr.setSectionResizeMode(self.COL_REVIEW,  QHeaderView.Interactive)
        hdr.setSectionResizeMode(self.COL_RBROWSE, QHeaderView.Fixed)
        hdr.setSectionResizeMode(self.COL_VARIANT, QHeaderView.Fixed)
        hdr.setSectionResizeMode(self.COL_COMMENT, QHeaderView.Stretch)
        hdr.setSectionResizeMode(self.COL_STATUS,  QHeaderView.Fixed)
        hdr.setMinimumSectionSize(28)

        # Fixed column widths
        self.table.setColumnWidth(self.COL_CHECK,   30)
        self.table.setColumnWidth(self.COL_TYPE,    128)
        self.table.setColumnWidth(self.COL_BROWSE,  32)
        self.table.setColumnWidth(self.COL_FOLDER,  195)
        self.table.setColumnWidth(self.COL_TASK,    145)
        self.table.setColumnWidth(self.COL_REVIEW,  185)
        self.table.setColumnWidth(self.COL_RBROWSE, 32)
        self.table.setColumnWidth(self.COL_VARIANT,  105)   # wider for dropdown
        self.table.setColumnWidth(self.COL_STATUS,  130)

        # Clicking the ☑ column header toggles all checkboxes
        hdr.sectionClicked.connect(self._on_header_section_clicked)

        pub.addWidget(self.table)
        self.group_publish.setLayout(pub)
        main.addWidget(self.group_publish)

        # ── Log ───────────────────────────────────────────────────────
        lhdr = QHBoxLayout()
        lhdr.addWidget(_tiny_label("OUTPUT LOG"))
        lhdr.addStretch()
        btn_clr = QPushButton("clear")
        btn_clr.setObjectName("ClearLogBtn")
        lhdr.addWidget(btn_clr)
        main.addLayout(lhdr)

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setFixedHeight(110)
        self.txt_log.setStyleSheet(
            "QTextEdit { font-family: Consolas, 'Fira Code', monospace; font-size: 12px;"
            "background: #191919; color: #a8d8a0; border: 1px solid #2e2e2e; border-radius: 4px; padding: 4px; }"
        )
        btn_clr.clicked.connect(self.txt_log.clear)
        main.addWidget(self.txt_log)

        # ── Publish All ───────────────────────────────────────────────
        self.btn_publish_all = QPushButton("▶   PUBLISH ALL JOBS")
        self.btn_publish_all.setObjectName("MainAction")
        self.btn_publish_all.setEnabled(False)
        self.btn_publish_all.clicked.connect(self.run_publish_all)
        main.addWidget(self.btn_publish_all)

    # ──────────────────────────────────────────────────────────────────
    # Logging
    # ──────────────────────────────────────────────────────────────────
    def log(self, text: str):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.txt_log.append(f"<span style='color:#3a3a3a;'>[{ts}]</span> {text}")

    # ──────────────────────────────────────────────────────────────────
    # Status label
    # ──────────────────────────────────────────────────────────────────
    def _update_status(self):
        total = len(self.row_data_list)
        sel   = sum(1 for rd in self.row_data_list if rd["checkbox"].isChecked())
        if total == 0:
            self.lbl_status.setText("No jobs")
            return
        sel_str = (f"  ·  <b style='color:#00b894;'>{sel} selected</b>") if sel else ""
        self.lbl_status.setText(
            f"<span style='color:#666;'>{total} job{'s' if total != 1 else ''}{sel_str}</span>"
        )

    # ──────────────────────────────────────────────────────────────────
    # Header column-0 click → toggle all checkboxes
    # ──────────────────────────────────────────────────────────────────
    def _on_header_section_clicked(self, section: int):
        if section == self.COL_CHECK:
            all_checked = all(rd["checkbox"].isChecked() for rd in self.row_data_list)
            for rd in self.row_data_list:
                rd["checkbox"].setChecked(not all_checked)

    # ──────────────────────────────────────────────────────────────────
    # Authentication
    # ──────────────────────────────────────────────────────────────────
    def handle_login(self):
        url  = self.input_url.text().strip()
        user = self.input_user.text().strip()
        pw   = self.input_pass.text().strip()
        os.environ["AYON_SERVER_URL"] = url
        try:
            ayon_api.close_connection()
            con = ayon_api.GlobalServerAPI()
            con.login(user, pw)
            self.current_user = con.get_user()["name"]
            self.settings.setValue("ayon_url",  url)
            self.settings.setValue("ayon_user", user)
            if self.cb_remember.isChecked():
                self.settings.setValue("ayon_pass",
                                       base64.b64encode(pw.encode()).decode())
            else:
                self.settings.remove("ayon_pass")
            self.log(f"Login successful!  <span style='color:#00c49a;'>Welcome, <b>{self.current_user}</b></span>")
            self.group_login.setEnabled(False)
            self.group_publish.setEnabled(True)
            self.btn_publish_all.setEnabled(True)
            self.load_projects()
        except Exception as e:
            QMessageBox.critical(self, "Login Error", str(e))

    def try_auto_login(self):
        url  = self.settings.value("ayon_url",  "")
        user = self.settings.value("ayon_user", "")
        b64  = self.settings.value("ayon_pass", "")
        if url:  self.input_url.setText(url)
        if user: self.input_user.setText(user)
        if url and user and b64:
            try:
                pw = base64.b64decode(b64.encode()).decode()
                self.input_pass.setText(pw)
                self.cb_remember.setChecked(True)
                os.environ["AYON_SERVER_URL"] = url
                ayon_api.close_connection()
                con = ayon_api.GlobalServerAPI()
                con.login(user, pw)
                self.current_user = con.get_user()["name"]
                self.log(f"Auto-login successful!  <span style='color:#00c49a;'>Welcome, <b>{self.current_user}</b></span>")
                self.group_login.setEnabled(False)
                self.group_publish.setEnabled(True)
                self.btn_publish_all.setEnabled(True)
                self.load_projects()
            except Exception:
                self.settings.remove("ayon_pass")
                self.input_pass.clear()

    def handle_logout(self):
        """Sign out, clear session, and return to the login panel."""
        reply = QMessageBox.question(
            self, "Logout",
            "Sign out and return to the login screen?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            ayon_api.close_connection()
        except Exception:
            pass

        # Don't clear remembered credentials — just de-authenticate
        self.current_user = None
        self._clear_all_rows()
        self.cached_folders.clear()
        self.cb_project.blockSignals(True)
        self.cb_project.clear()
        self.cb_project.blockSignals(False)

        self.group_publish.setEnabled(False)
        self.btn_publish_all.setEnabled(False)
        self.group_login.setEnabled(True)
        self.input_pass.clear()   # always clear password on logout for safety

        self.log("Logged out.")

    # ──────────────────────────────────────────────────────────────────
    # Project caching
    # ──────────────────────────────────────────────────────────────────
    def load_projects(self):
        self.cb_project.blockSignals(True)
        self.cb_project.clear()
        for p in ayon_api.get_projects():
            self.cb_project.addItem(p["name"])
        self.cb_project.blockSignals(False)
        self.cache_project_data()

    def cache_project_data(self):
        project = self.cb_project.currentText()
        if not project:
            return
        self.log(f"Caching folders for <b>{project}</b>…")
        self.cached_folders.clear()
        try:
            for f in ayon_api.get_folders(project):
                self.cached_folders[f["path"]] = f["id"]
            self.log(f"Cached <b>{len(self.cached_folders)}</b> folders.")
        except Exception as e:
            self.log(f"Error caching folders: {e}")
            return
        self._clear_all_rows()
        self.add_table_row()

    # ──────────────────────────────────────────────────────────────────
    # Row management
    # ──────────────────────────────────────────────────────────────────
    def add_table_row(self) -> dict:
        """
        Add one row to the table and return its row-data dict.
        """
        row_idx = self.table.rowCount()
        self.table.insertRow(row_idx)

        # ── Create widgets ────────────────────────────────────────────
        chk          = QCheckBox()
        type_w       = PathTypeWidget()
        path_edit    = DnDLineEdit()
        path_edit.setPlaceholderText("Source path  (or drag & drop)")

        # Asset Folder — searchable dropdown (change 2)
        cb_folder    = NoScrollComboBox()
        cb_folder.setEditable(True)
        cb_folder.setInsertPolicy(QComboBox.NoInsert)
        cb_folder.completer().setCompletionMode(QCompleter.PopupCompletion)
        cb_folder.completer().setFilterMode(Qt.MatchContains)
        cb_folder.addItems(sorted(self.cached_folders.keys()))

        cb_task      = NoScrollComboBox()

        # Review media — now drag-and-drop enabled (change 3)
        review_edit  = DnDReviewEdit()
        review_edit.setPlaceholderText("Review media — drag & drop or browse…")

        # Variant — editable combo with presets (change 4)
        variant_cb   = VariantComboBox()

        comment_edit = QLineEdit()
        comment_edit.setPlaceholderText("Comment…")
        progress     = QProgressBar()
        progress.setValue(0)
        progress.setFormat("Pending")
        progress.setTextVisible(True)

        # Row-data dict — single source of truth for this row's widgets
        rd: dict = {
            "checkbox":     chk,
            "type_widget":  type_w,
            "path_edit":    path_edit,
            "cb_folder":    cb_folder,
            "cb_task":      cb_task,
            "review_edit":  review_edit,
            "variant_cb":   variant_cb,
            "comment_edit": comment_edit,
            "progress":     progress,
            "cached_tasks": {},     # {folder_id: {task_name: task_id}}
        }

        # ── Connect signals ───────────────────────────────────────────
        cb_folder.currentTextChanged.connect(
            lambda text, r=rd: self._on_folder_changed(r, text)
        )
        cb_task.currentIndexChanged.connect(
            lambda _idx, r=rd: self._on_task_user_changed(r)
        )
        type_w.type_changed.connect(
            lambda is_file, r=rd: self._on_type_user_changed(r, is_file)
        )
        path_edit.textChanged.connect(
            lambda text, r=rd: self._on_path_text_changed(r, text)
        )
        path_edit.path_dropped.connect(
            lambda path, r=rd: self._on_path_dropped(r, path)
        )
        # Review field DnD (change 3)
        review_edit.path_dropped.connect(
            lambda path, r=rd: self._on_review_dropped(r, path)
        )
        chk.stateChanged.connect(self._update_status)

        # ── Browse buttons ────────────────────────────────────────────
        btn_browse = QPushButton("📁")
        btn_browse.setObjectName("IconBtn")
        btn_browse.setFixedSize(30, 30)
        btn_browse.clicked.connect(lambda _=False, r=rd: self._browse_source(r))

        btn_rbrowse = QPushButton("🎬")
        btn_rbrowse.setObjectName("IconBtn")
        btn_rbrowse.setFixedSize(30, 30)
        btn_rbrowse.clicked.connect(lambda _=False, r=rd: self._browse_review(r))

        # ── Helper to centre a widget in a table cell ─────────────────
        def _centred(w):
            c = QWidget()
            cl = QHBoxLayout(c)
            cl.setContentsMargins(1, 1, 1, 1)
            cl.setAlignment(Qt.AlignCenter)
            cl.addWidget(w)
            return c

        def _filled(w):
            c = QWidget()
            cl = QHBoxLayout(c)
            cl.setContentsMargins(2, 2, 2, 2)
            cl.addWidget(w)
            return c

        # ── Place widgets in table cells ──────────────────────────────
        self.table.setCellWidget(row_idx, self.COL_CHECK,   _centred(chk))
        self.table.setCellWidget(row_idx, self.COL_TYPE,    type_w)
        self.table.setCellWidget(row_idx, self.COL_PATH,    _filled(path_edit))
        self.table.setCellWidget(row_idx, self.COL_BROWSE,  _centred(btn_browse))
        self.table.setCellWidget(row_idx, self.COL_FOLDER,  _filled(cb_folder))
        self.table.setCellWidget(row_idx, self.COL_TASK,    _filled(cb_task))
        self.table.setCellWidget(row_idx, self.COL_REVIEW,  _filled(review_edit))
        self.table.setCellWidget(row_idx, self.COL_RBROWSE, _centred(btn_rbrowse))
        self.table.setCellWidget(row_idx, self.COL_VARIANT, _filled(variant_cb))
        self.table.setCellWidget(row_idx, self.COL_COMMENT, _filled(comment_edit))
        self.table.setCellWidget(row_idx, self.COL_STATUS,  _filled(progress))

        self.row_data_list.append(rd)

        if cb_folder.count() > 0:
            self._on_folder_changed(rd, cb_folder.currentText())

        self._update_status()
        return rd

    def _clear_all_rows(self):
        self.table.setRowCount(0)
        self.row_data_list.clear()
        self._update_status()

    def remove_selected_rows(self):
        indices = [i for i, rd in enumerate(self.row_data_list)
                   if rd["checkbox"].isChecked()]
        if not indices:
            self.log("No rows selected — nothing removed.")
            return
        for i in reversed(indices):
            self.table.removeRow(i)
            self.row_data_list.pop(i)
        self.log(f"Removed <b>{len(indices)}</b> row(s).")
        self._update_status()

    # ──────────────────────────────────────────────────────────────────
    # Signal handlers
    # ──────────────────────────────────────────────────────────────────
    def _on_folder_changed(self, rd: dict, text: str):
        """Populate the Task combobox for this row."""
        cb_task   = rd["cb_task"]
        folder_id = self.cached_folders.get(text)

        if not folder_id:
            cb_task.blockSignals(True)
            cb_task.clear()
            cb_task.addItem("— pick folder first —")
            cb_task.blockSignals(False)
            return

        if folder_id not in rd["cached_tasks"]:
            rd["cached_tasks"][folder_id] = {}
            project = self.cb_project.currentText()
            try:
                for t in ayon_api.get_tasks(project, folder_ids=[folder_id]):
                    rd["cached_tasks"][folder_id][t["name"]] = t["id"]
            except Exception as e:
                self.log(f"Warning: could not fetch tasks for '{text}': {e}")

        cb_task.blockSignals(True)
        cb_task.clear()
        cb_task.addItem("None")
        cb_task.addItems(sorted(rd["cached_tasks"][folder_id].keys()))
        cb_task.blockSignals(False)

    def _on_task_user_changed(self, rd: dict):
        """Propagate task selection to all other checked rows."""
        if self._bulk_propagating:
            return
        if not rd["checkbox"].isChecked():
            return
        task_name = rd["cb_task"].currentText()
        self._bulk_propagating = True
        for other in self.row_data_list:
            if other is rd or not other["checkbox"].isChecked():
                continue
            idx = other["cb_task"].findText(task_name)
            if idx >= 0:
                other["cb_task"].setCurrentIndex(idx)
        self._bulk_propagating = False

    def _on_type_user_changed(self, rd: dict, is_file: bool):
        """Propagate File/Folder radio to all other checked rows."""
        if self._bulk_propagating:
            return
        if not rd["checkbox"].isChecked():
            return
        self._bulk_propagating = True
        for other in self.row_data_list:
            if other is rd or not other["checkbox"].isChecked():
                continue
            other["type_widget"].set_is_file(is_file)
        self._bulk_propagating = False

    # Image/video extensions that should auto-populate the Review field
    _REVIEW_EXTS = {
        ".mp4", ".mov", ".avi", ".mkv", ".webm", ".mxf",   # video
        ".png", ".jpg", ".jpeg", ".exr", ".tif", ".tiff",  # image
        ".dpx", ".hdr", ".gif",
    }

    def _on_path_text_changed(self, rd: dict, text: str):
        text = text.strip()
        if text and os.path.exists(text):
            self._try_autofill_folder(rd, text)
            self._try_autofill_review(rd, text)

    def _on_path_dropped(self, rd: dict, path: str):
        """
        DnD drop on the source path field.
        Change 5: auto-set File/Folder radio based on what was dropped.
        """
        # Auto-detect and set type radio (change 5)
        is_file = os.path.isfile(path)
        rd["type_widget"].set_is_file(is_file)
        self._try_autofill_folder(rd, path)
        self._try_autofill_review(rd, path)

    def _on_review_dropped(self, rd: dict, path: str):
        """DnD drop on the review media field — just accept any file."""
        if os.path.isfile(path):
            rd["review_edit"].setText(path)
            self.log(
                f"<span style='color:#00b894;'>🎬 Review set:</span> "
                f"<b>{os.path.basename(path)}</b>"
            )
        else:
            self.log("Review media must be a file, not a folder.")

    def _try_autofill_review(self, rd: dict, path: str):
        """
        If the source path is a reviewable image/video AND the Review
        Media field is currently empty, copy the source path into it.
        """
        if not os.path.isfile(path):
            return
        ext = os.path.splitext(path)[1].lower()
        if ext not in self._REVIEW_EXTS:
            return
        review_field = rd["review_edit"]
        if review_field.text().strip():
            return   # don't overwrite manually set review path
        review_field.setText(path)

    # ──────────────────────────────────────────────────────────────────
    # Auto-fill folder from filename stem
    # ──────────────────────────────────────────────────────────────────
    def _try_autofill_folder(self, rd: dict, path: str):
        """
        Extract the filename stem (or folder name) and search cached_folders
        for an exact case-insensitive match on the last path component.
        """
        stem = (os.path.splitext(os.path.basename(path))[0]
                if os.path.isfile(path)
                else os.path.basename(path.rstrip("/\\")))
        if not stem:
            return

        sl = stem.lower()
        for folder_path in self.cached_folders:
            if folder_path.rstrip("/").rsplit("/", 1)[-1].lower() == sl:
                cb = rd["cb_folder"]
                if cb.currentText() != folder_path:
                    cb.setCurrentText(folder_path)
                    # Visual pulse for 2.5 s
                    cb.setStyleSheet(
                        "QComboBox { border: 1.5px solid #00b894; background: #1c3028; }"
                    )
                    QTimer.singleShot(2500, lambda c=cb: c.setStyleSheet(""))
                    self.log(
                        f"<span style='color:#00b894;'>✓ Auto-matched</span>"
                        f"  <b>{stem}</b>  →  {folder_path}"
                    )
                return

    # ──────────────────────────────────────────────────────────────────
    # Browse helpers
    # ──────────────────────────────────────────────────────────────────
    def _browse_source(self, rd: dict):
        if rd["type_widget"].is_file():
            f, _ = QFileDialog.getOpenFileName(self, "Select File", "", "All Files (*)")
            if f:
                rd["path_edit"].setText(f)
                # Auto-set type on browse too (change 5)
                rd["type_widget"].set_is_file(os.path.isfile(f))
        else:
            d = QFileDialog.getExistingDirectory(self, "Select Folder")
            if d:
                rd["path_edit"].setText(d)
                rd["type_widget"].set_is_file(False)

    def _browse_review(self, rd: dict):
        f, _ = QFileDialog.getOpenFileName(
            self, "Select Review Media", "",
            "Media Files (*.mp4 *.mov *.avi *.mkv *.webm *.mxf "
            "*.png *.jpg *.jpeg *.exr *.tif *.tiff *.dpx *.hdr *.gif)"
        )
        if f:
            rd["review_edit"].setText(f)

    # ──────────────────────────────────────────────────────────────────
    # Bulk Import
    # ──────────────────────────────────────────────────────────────────
    def _bulk_import_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select Files to Import", "", "All Files (*)"
        )
        if files:
            self._create_rows_for_paths(files)

    def _bulk_import_directory(self):
        d = QFileDialog.getExistingDirectory(self, "Select Directory to Scan")
        if not d:
            return
        try:
            paths = sorted(
                os.path.join(d, n) for n in os.listdir(d)
                if not n.startswith(".")
            )
        except Exception as e:
            self.log(f"Error scanning directory: {e}")
            return
        if not paths:
            self.log("Directory is empty.")
            return
        self._create_rows_for_paths(paths)

    def _create_rows_for_paths(self, paths: list):
        """
        Core bulk-import helper. For each path:
          1. Adds a row
          2. Sets File/Folder radio based on what's on disk  (change 5)
          3. Sets path (triggers textChanged → autofill)
        """
        for path in paths:
            rd = self.add_table_row()
            is_file = os.path.isfile(path)
            rd["type_widget"].set_is_file(is_file)   # change 5
            rd["path_edit"].setText(path)             # triggers autofill

        self.log(f"⬇ Bulk import: <b>{len(paths)}</b> row(s) created.")
        self.table.scrollToBottom()

    # ──────────────────────────────────────────────────────────────────
    # Publish queue
    # ──────────────────────────────────────────────────────────────────
    def run_publish_all(self):
        if not self.row_data_list:
            QMessageBox.warning(self, "Warning", "No jobs in the table.")
            return

        for btn in (self.btn_publish_all, self.btn_add_row,
                    self.btn_remove, self.btn_bulk, self.btn_logout):
            btn.setEnabled(False)
        self.cb_project.setEnabled(False)
        self.publish_queue = []

        project = self.cb_project.currentText()

        for i, rd in enumerate(self.row_data_list):
            local_path = rd["path_edit"].text().strip()
            if not local_path or not os.path.exists(local_path):
                _set_progress(rd["progress"], "Skipped", 0, "#555")
                continue

            folder_path = rd["cb_folder"].currentText()
            folder_id   = self.cached_folders.get(folder_path)
            if not folder_id:
                _set_progress(rd["progress"], "No folder", 0, "#7b3a00")
                continue

            task_name = rd["cb_task"].currentText()
            task_id   = (
                rd["cached_tasks"].get(folder_id, {}).get(task_name)
                if task_name not in ("None", "— pick folder first —")
                else None
            )

            # Read variant from the editable combo (change 4)
            variant_text = rd["variant_cb"].currentText().strip() or "main"

            data = {
                "local_path":  local_path,
                "project":     project,
                "folder_id":   folder_id,
                "task_id":     task_id,
                "author_name": self.current_user,
                "variant":     variant_text,
                "comment":     rd["comment_edit"].text().strip(),
                "review_path": rd["review_edit"].text().strip(),
            }
            _set_progress(rd["progress"], "Queued", 0)
            self.publish_queue.append((i, data))

        self.log(f"Queued <b>{len(self.publish_queue)}</b> valid job(s).")
        self._process_next_job()

    def _process_next_job(self):
        if not self.publish_queue:
            self.log("✅  <b>Batch Publish Complete!</b>")
            QMessageBox.information(self, "Done", "All queued jobs have finished.")
            for btn in (self.btn_publish_all, self.btn_add_row,
                        self.btn_remove, self.btn_bulk, self.btn_logout):
                btn.setEnabled(True)
            self.cb_project.setEnabled(True)
            return

        job_index, data = self.publish_queue.pop(0)
        _set_progress(self.row_data_list[job_index]["progress"], "Publishing…", 0)

        self.current_worker = PublishWorker(job_index, **data)
        self.current_worker.log_msg.connect(self.log)
        self.current_worker.progress_update.connect(
            lambda idx, pct: self.row_data_list[idx]["progress"].setValue(pct)
        )
        self.current_worker.finished_signal.connect(self._on_worker_finished)
        self.current_worker.start()

    def _on_worker_finished(self, job_index: int, success: bool, message: str):
        pb = self.row_data_list[job_index]["progress"]
        if success:
            _set_progress(pb, "Done ✓", 100, "#27ae60")
        else:
            _set_progress(pb, "Failed ✗", 0, "#922b21")
            self.log(f"[Job {job_index+1}] ERROR: {message}")

        if self.current_worker:
            self.current_worker.quit()
            self.current_worker.wait()
            self.current_worker = None

        self._process_next_job()


# ── Tiny helpers ──────────────────────────────────────────────────────────
def _set_progress(pb: QProgressBar, text: str, value: int, color: str = None):
    pb.setFormat(text)
    pb.setValue(value)
    pb.setStyleSheet(
        f"QProgressBar::chunk {{ background-color: {color}; }}" if color else ""
    )


def _tiny_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        "color: #444; font-size: 10px; font-weight: 700; letter-spacing: 1px;"
    )
    return lbl


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = AyonBatchPublisherUI()
    window.show()
    sys.exit(app.exec_())