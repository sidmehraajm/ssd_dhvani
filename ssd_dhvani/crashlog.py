"""Crash capture: never die silently.

PySide6 aborts the process when a Python exception escapes a Qt slot, and a
windowed (no-console) exe shows nothing at all -- the app just vanishes. This
installs a `sys.excepthook` (and a threading hook) that:

  * appends the full traceback to  <save_root>/.ssd_dhvani/crash.log
  * shows the user a dialog with the error and the log location
  * keeps the app running when the error came from a slot (Qt is usually
    still in a valid state; a wrong preview beats losing an unsaved take)

Only genuinely unrecoverable errors should end the session.
"""
from __future__ import annotations

import logging
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

log = logging.getLogger("ssd_dhvani.crash")

_LOG_PATH: Path | None = None
_seen: set[str] = set()      # don't spam the same repeating error


def log_path() -> Path | None:
    return _LOG_PATH


def _write(header: str, text: str) -> None:
    if _LOG_PATH is None:
        return
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as fp:
            fp.write(f"\n{'=' * 72}\n{datetime.now().isoformat(timespec='seconds')}"
                     f"  {header}\n{'=' * 72}\n{text}\n")
    except Exception:       # pragma: no cover - logging must never raise
        pass


def _notify(title: str, body: str) -> None:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is None:
            return
        box = QMessageBox()
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(title)
        box.setText(body)
        if _LOG_PATH is not None:
            box.setInformativeText(f"Details were appended to:\n{_LOG_PATH}")
        box.exec()
    except Exception:       # pragma: no cover
        pass


def install(save_root: Path) -> Path:
    """Point the crash log at `save_root` and install the hooks."""
    global _LOG_PATH
    _LOG_PATH = Path(save_root) / ".ssd_dhvani" / "crash.log"

    def _hook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        _write(f"UNHANDLED {exc_type.__name__}", text)
        log.error("unhandled exception:\n%s", text)
        key = f"{exc_type.__name__}:{exc}"
        if key not in _seen:
            _seen.add(key)
            _notify("ssd_dhvani hit an error",
                    f"{exc_type.__name__}: {exc}\n\n"
                    "The app is still running — finish and save the current "
                    "take, then restart when convenient.")

    def _thread_hook(args) -> None:
        _write(f"THREAD {args.thread.name if args.thread else '?'}",
               "".join(traceback.format_exception(
                   args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = _hook
    try:
        threading.excepthook = _thread_hook      # Python 3.8+
    except Exception:       # pragma: no cover
        pass
    # NATIVE crashes (access violations inside PortAudio / audio drivers) kill
    # the process without ever reaching a Python hook. faulthandler writes
    # every thread's stack to fault.log at the moment of death — that file is
    # the thing to look at when the app "just disappeared".
    try:
        import faulthandler
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = open(_LOG_PATH.with_name("fault.log"), "a", encoding="utf-8")
        fh.write(f"\n===== session start {datetime.now().isoformat(timespec='seconds')} =====\n")
        fh.flush()
        faulthandler.enable(fh, all_threads=True)
    except Exception:       # pragma: no cover
        pass
    return _LOG_PATH
