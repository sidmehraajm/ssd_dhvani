"""ssd_dhvani entry point.

Flow:  Connect screen (AYON) ->  Main Control-Room window.

Run:   python main.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from ssd_dhvani.config import AppConfig
from ssd_dhvani.login_window import LoginWindow
from ssd_dhvani.main_window import MainWindow
from ssd_dhvani.theme import apply_theme
from ssd_dhvani import crashlog

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")


def main() -> int:
    # Windows display scaling (125% / 150%) otherwise rounds to whole
    # integers, which is what clipped labels and buttons at the edges.
    try:
        from PySide6.QtCore import Qt
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName("ssd_dhvani")

    config = AppConfig.load()
    # Crash hooks FIRST: a windowed exe otherwise vanishes with no trace when
    # an exception escapes a Qt slot.
    path = crashlog.install(config.save_root_path)
    logging.getLogger("ssd_dhvani").info("crash log: %s", path)
    # Mirror the app log to a rolling file for post-mortems.
    try:
        fh = logging.FileHandler(Path(path).with_name("session.log"),
                                 encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(fh)
    except Exception:
        pass
    # Resolve ffmpeg once, from config, before anything needs it.
    from ssd_dhvani import tools
    tools.set_ffmpeg(config.ffmpeg_path)
    logging.getLogger("ssd_dhvani").info(tools.describe())
    from ssd_dhvani import publish
    publish.set_product_type(config.publish_product_type)
    apply_theme(app, config.theme)
    holder: dict = {}

    login = LoginWindow(config)

    def _enter(client) -> None:
        win = MainWindow(config, client)
        holder["win"] = win          # keep a reference so it isn't GC'd
        win.show()
        login.close()

    login.connected.connect(_enter)
    login.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
