# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for ssd_dhvani (AYON audio recording / review / publishing).

Build:   Windows:  build.bat            -> dist\\ssd_dhvani_v1.exe  (single file)
         macOS:    ./build.sh           -> dist/ssd_dhvani_v1.app
         (both run: pyinstaller ssd_dhvani.spec --noconfirm --clean)

PyInstaller cannot cross-compile: build the .exe ON Windows and the .app ON
a Mac. This one spec serves both.

Notes
-----
* config.json is NOT bundled (it can hold the AYON password). It is read from
  next to the .exe / next to the .app at runtime -- the build scripts stage
  a starter copy there.
* sounddevice and soundfile ship their C libraries (PortAudio, libsndfile)
  as package data in `_sounddevice_data` / `_soundfile_data`. Without
  collect_all the frozen app imports them and then can't find the binary --
  i.e. "works from source, no audio in the exe".
* macOS: an app that records audio MUST declare NSMicrophoneUsageDescription
  in its Info.plist. Without it macOS silently feeds it zeros (or kills it).
  The BUNDLE step below adds it; the first launch then asks for permission.
"""

import os as _os
import re as _re
import sys as _sys

from PyInstaller.utils.hooks import collect_all


def _app_version() -> str:
    try:
        src = open(_os.path.join("ssd_dhvani", "__init__.py"),
                   encoding="utf-8").read()
        return _re.search(r'__version__\s*=\s*"([^"]+)"', src).group(1)
    except Exception:
        return "0.0.0"


APP_VERSION = _app_version()
APP_NAME = f"ssd_dhvani_v{APP_VERSION.split('.')[0]}"
IS_MAC = _sys.platform == "darwin"
print(f"[spec] building {APP_NAME}  (ssd_dhvani {APP_VERSION}, {_sys.platform})")

# Windows: one portable .exe. macOS: a folder build wrapped in a .app
# (one-file .app bundles are deprecated by PyInstaller and slow to start).
ONEFILE = not IS_MAC
CONSOLE = _os.environ.get("SSD_BUILD_CONSOLE", "") == "1"

datas, binaries, hiddenimports = [], [], []
for pkg in ("ayon_api", "sounddevice", "_sounddevice_data",
            "soundfile", "_soundfile_data"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception as exc:
        print(f"[spec] note: {pkg} not collected ({exc})")
hiddenimports += ["cffi", "_cffi_backend"]

# Qt stylesheets (dark + light) are loaded at runtime from the bundle.
datas += [("ssd_dhvani/styles.qss", "ssd_dhvani"),
          ("ssd_dhvani/styles_light.qss", "ssd_dhvani")]

ICON = None
for cand in (("app.icns",) if IS_MAC else ("app.ico",)):
    if _os.path.exists(cand):
        ICON = cand

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PyQt5", "PyQt6", "PySide2",     # avoid clashing Qt bindings
        "tkinter", "matplotlib", "pytest", "cv2",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

if ONEFILE:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
        name=APP_NAME,
        debug=False, bootloader_ignore_signals=False, strip=False,
        upx=False, runtime_tmpdir=None, console=CONSOLE,
        disable_windowed_traceback=False, argv_emulation=False,
        target_arch=None, codesign_identity=None, entitlements_file=None,
        icon=ICON,
    )
else:
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name=APP_NAME,
        debug=False, bootloader_ignore_signals=False, strip=False,
        upx=False, console=CONSOLE, disable_windowed_traceback=False,
        argv_emulation=False, target_arch=None, codesign_identity=None,
        entitlements_file=None, icon=ICON,
    )
    coll = COLLECT(
        exe, a.binaries, a.zipfiles, a.datas,
        strip=False, upx=False, upx_exclude=[], name=APP_NAME,
    )
    if IS_MAC:
        app = BUNDLE(
            coll,
            name=f"{APP_NAME}.app",
            icon=ICON,
            bundle_identifier="com.desireseed.ssd_dhvani",
            info_plist={
                "CFBundleName": "ssd_dhvani",
                "CFBundleDisplayName": "ssd_dhvani",
                "CFBundleShortVersionString": APP_VERSION,
                "CFBundleVersion": APP_VERSION,
                "NSHighResolutionCapable": True,
                # REQUIRED to record: the text macOS shows in the prompt.
                "NSMicrophoneUsageDescription":
                    "ssd_dhvani records microphone takes for the pipeline.",
            },
        )
