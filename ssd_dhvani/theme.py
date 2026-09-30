"""Theme loading — dark (default) and light stylesheets.

Kept in the package (NOT in main.py) so it can be imported both by the entry
script and by the window at runtime: in a PyInstaller exe the entry script is
``__main__`` and cannot be imported as ``main``.
"""
from __future__ import annotations

from pathlib import Path

QSS_DIR = Path(__file__).resolve().parent

#: colours the code paints by hand (log box, inline styles) per theme
PALETTE = {
    "dark": {
        "log_bg": "#0a0c0e", "log_fg": "#a8d8a0", "log_border": "#1c1f23",
    },
    "light": {
        "log_bg": "#ffffff", "log_fg": "#1f6b2c", "log_border": "#c9ced4",
    },
}


#: Whatever was last applied -- widgets that must style themselves (popup
#: menus) read this, since they cannot see the AppConfig.
CURRENT = "dark"


def stylesheet(theme: str) -> str:
    qss = QSS_DIR / ("styles_light.qss" if theme == "light" else "styles.qss")
    return qss.read_text(encoding="utf-8") if qss.exists() else ""


def apply_theme(app, theme: str) -> None:
    """Apply the dark/light stylesheet to a QApplication."""
    global CURRENT
    CURRENT = "light" if theme == "light" else "dark"
    app.setStyleSheet(stylesheet(theme))


def palette(theme: str) -> dict:
    return PALETTE.get(theme, PALETTE["dark"])


def menu_qss(theme: str | None = None) -> str:
    """Stylesheet for a popup menu.

    Needed because a QMenu whose parent widget carries an inline stylesheet
    (the track/review tiles do, to paint their dark frame) INHERITS it
    -- so in light mode the menu came up dark. Styling the menu explicitly
    overrides that inheritance.
    """
    if (theme or CURRENT) == "light":
        return ("QMenu{background:#ffffff;color:#1b1f24;"
                "border:1px solid #c9ced4;border-radius:6px;padding:4px;}"
                "QMenu::item{padding:6px 22px;border-radius:4px;}"
                "QMenu::item:selected{background:#e0952f;color:#ffffff;}"
                "QMenu::item:disabled{color:#9aa1a8;}"
                "QMenu::separator{height:1px;background:#e2e5e9;margin:4px 8px;}")
    return ("QMenu{background:#16181b;color:#e9edf0;"
            "border:1px solid #26292e;border-radius:6px;padding:4px;}"
            "QMenu::item{padding:6px 22px;border-radius:4px;}"
            "QMenu::item:selected{background:#e0952f;color:#0d0f11;}"
            "QMenu::item:disabled{color:#5f6870;}"
            "QMenu::separator{height:1px;background:#26292e;margin:4px 8px;}")
