"""Connect screen -- per-user AYON login, same auth flow as the batch publisher.

On success it emits `connected(AyonClient)`; main.py then shows the main window.
Server/username are always remembered; the password only when 'Remember this
workstation' is ticked (base64, like the publisher's QSettings).
"""
from __future__ import annotations

import logging
import platform
import traceback
from datetime import datetime

from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QCheckBox, QFrame, QPlainTextEdit, QApplication,
)

from . import VERSION_LABEL
from .config import AppConfig
from .ayon_client import (AyonClient, OfflineClient, diagnose_server,
                          explain_login_error)

log = logging.getLogger("ssd_dhvani.login")


class _SignInWorker(QThread):
    """Signs in -- and on failure runs the server diagnostics -- OFF the UI
    thread. Both can wait on network timeouts (the diagnostics alone can take
    10+ s against an unreachable server), which used to freeze the window."""

    succeeded = Signal(object, str)          # client, username
    failed = Signal(str, str, str)           # error text, hint, full report

    def __init__(self, url: str, user: str, password: str) -> None:
        super().__init__()
        self._url, self._user, self._pw = url, user, password

    def run(self) -> None:
        client = AyonClient()
        try:
            username = client.login(self._url, self._user, self._pw)
        except Exception as exc:
            tb = traceback.format_exc().rstrip()      # capture BEFORE anything else
            hint = explain_login_error(exc)
            report = [
                f"ssd_dhvani {VERSION_LABEL}",
                f"time     : {datetime.now().isoformat(timespec='seconds')}",
                f"machine  : {platform.node()}  ({platform.platform()})",
                f"user     : {self._user}",
                f"error    : {type(exc).__name__}: {exc}",
                f"meaning  : {hint}",
                "",
            ]
            try:
                report += diagnose_server(self._url)
            except Exception as diag_exc:   # diagnostics must never break login
                report.append(f"diagnostics failed: {diag_exc}")
            report += ["", "traceback:", tb]
            self.failed.emit(f"{exc}", hint, "\n".join(report))
            return
        finally:
            self._pw = ""                            # don't keep it around
        self.succeeded.emit(client, username)


class LoginWindow(QWidget):
    connected = Signal(object)  # emits an AyonClient

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.setWindowTitle("ssd_dhvani — Connect")
        self.setMinimumWidth(480)   # grows when the details report is shown
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 24, 40, 34)
        outer.setSpacing(0)

        # gradient accent strip
        strip = QFrame()
        strip.setFixedHeight(6)
        strip.setStyleSheet(
            "background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #e0952f, stop:1 #3fb950); border-radius:3px;")
        outer.addWidget(strip)
        outer.addSpacing(26)

        # brand row
        brand = QHBoxLayout()
        logo = QLabel("a")
        logo.setFixedSize(38, 38)
        logo.setAlignment(Qt.AlignCenter)
        logo.setStyleSheet(
            "background:#e0952f;color:#0d0f11;border-radius:9px;"
            "font-weight:800;font-size:19px;")
        brand.addWidget(logo)
        brand.addSpacing(11)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        name = QLabel("ssd_dhvani")
        name.setStyleSheet("font-size:17px;font-weight:700;color:#e7ebee;")
        sub = QLabel(f"Recording & Publishing Tool · {VERSION_LABEL}")
        sub.setStyleSheet("font-family:'IBM Plex Mono',monospace;font-size:11px;color:#5f6870;")
        title_box.addWidget(name)
        title_box.addWidget(sub)
        brand.addLayout(title_box)
        brand.addStretch()
        outer.addLayout(brand)
        outer.addSpacing(22)

        heading = QLabel("Sign in to your AYON server")
        heading.setStyleSheet("font-size:13px;font-weight:600;color:#c6ced6;")
        outer.addWidget(heading)
        outer.addSpacing(16)

        outer.addWidget(self._field_label("SERVER URL"))
        self.url_edit = QLineEdit(self.config.ayon_server_url or "https://")
        outer.addWidget(self.url_edit)
        outer.addSpacing(12)

        outer.addWidget(self._field_label("USERNAME"))
        self.user_edit = QLineEdit(self.config.ayon_username)
        outer.addWidget(self.user_edit)
        outer.addSpacing(12)

        outer.addWidget(self._field_label("PASSWORD"))
        self.pass_edit = QLineEdit(self.config.ayon_password)
        self.pass_edit.setEchoMode(QLineEdit.Password)
        self.pass_edit.returnPressed.connect(self._on_connect)
        outer.addWidget(self.pass_edit)
        outer.addSpacing(14)

        self.remember = QCheckBox("Remember this workstation")
        self.remember.setChecked(self.config.remember_workstation)
        self.remember.setStyleSheet("color:#a9b2bb;font-size:12px;")
        outer.addWidget(self.remember)
        outer.addSpacing(10)

        self.connect_btn = QPushButton("Sign In")
        self.connect_btn.setObjectName("Publish")  # reuse the green filled style
        self.connect_btn.clicked.connect(self._on_connect)
        outer.addWidget(self.connect_btn)

        outer.addSpacing(8)
        self.offline_btn = QPushButton("Work Offline  (record locally)")
        self.offline_btn.setToolTip(
            "No network needed: type a shot number and record takes under "
            "{save root}/offline/. Publish && reference are disabled until "
            "you sign in again.")
        self.offline_btn.clicked.connect(self._on_offline)
        outer.addWidget(self.offline_btn)

        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#5f6870;font-size:11px;")
        outer.addSpacing(8)
        outer.addWidget(self.status)

        # Failure diagnostics: hidden until something goes wrong.
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.details_btn = QPushButton("Hide details")
        self.details_btn.clicked.connect(self._toggle_details)
        self.details_btn.setVisible(False)
        btn_row.addWidget(self.details_btn)
        self.copy_btn = QPushButton("Copy details")
        self.copy_btn.setToolTip("Copy the technical report for your admin")
        self.copy_btn.clicked.connect(self._copy_details)
        self.copy_btn.setVisible(False)
        btn_row.addWidget(self.copy_btn)
        outer.addSpacing(6)
        outer.addLayout(btn_row)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMinimumHeight(150)
        self.details.setVisible(False)
        self.details.setStyleSheet(
            "font-family:Consolas,'IBM Plex Mono',monospace;font-size:10px;")
        outer.addWidget(self.details)

    def _field_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "color:#8a939c;font-size:10px;font-weight:600;letter-spacing:1px;")
        return lbl

    def _on_connect(self) -> None:
        w = getattr(self, "_signin", None)
        if w is not None and w.isRunning():
            return                          # one attempt at a time
        url = self.url_edit.text().strip()
        user = self.user_edit.text().strip()
        pw = self.pass_edit.text()
        self.status.setText("Signing in…")
        self.status.setStyleSheet("color:#8b9298;font-size:11px;")
        self.connect_btn.setEnabled(False)
        self.offline_btn.setEnabled(False)
        log.info("sign-in attempt: url=%s user=%s", url, user)
        self._pending = (url, user, pw if self.remember.isChecked() else "")
        self._signin = _SignInWorker(url, user, pw)
        self._signin.succeeded.connect(self._on_signin_ok)
        self._signin.failed.connect(self._on_signin_failed)
        self._signin.start()

    def _on_signin_ok(self, client, username: str) -> None:
        url, user, pw = self._pending
        self._pending = ("", "", "")
        self.config.ayon_server_url = url
        self.config.ayon_username = user
        self.config.remember_workstation = self.remember.isChecked()
        self.config.set_password(pw)
        self.config.save()
        self.status.setText(f"● Signed in as {username}")
        self.status.setStyleSheet("color:#3fb950;font-size:11px;")
        self.connected.emit(client)

    def _on_signin_failed(self, err: str, hint: str, report: str) -> None:
        """Plain-language cause + a copyable technical report."""
        self._pending = ("", "", "")
        self.connect_btn.setEnabled(True)
        self.offline_btn.setEnabled(True)
        self.status.setText(f"✕ Sign-in failed: {err}\n{hint}")
        self.status.setStyleSheet("color:#f2555a;font-size:11px;")
        self.details_btn.setVisible(True)
        self.copy_btn.setVisible(True)
        self.details.setPlainText(report)
        self.details.setVisible(True)
        self.details_btn.setText("Hide details")
        for line in report.splitlines():
            log.info("login-diag | %s", line)

    def closeEvent(self, event):  # noqa: N802
        w = getattr(self, "_signin", None)
        if w is not None and w.isRunning():
            w.wait(15000)           # never destroy a running QThread
        super().closeEvent(event)

    def _toggle_details(self) -> None:
        show = not self.details.isVisible()
        self.details.setVisible(show)
        self.details_btn.setText("Hide details" if show else "Show details")

    def _copy_details(self) -> None:
        QApplication.clipboard().setText(self.details.toPlainText())
        self.status.setText(self.status.text().split("\n")[0]
                            + "\nDetails copied to the clipboard.")

    def _on_offline(self) -> None:
        """Skip AYON entirely -- record locally under {save_root}/offline/."""
        self.status.setText("● Offline mode — recording locally, no AYON")
        self.status.setStyleSheet("color:#e0952f;font-size:11px;")
        self.connected.emit(OfflineClient())
