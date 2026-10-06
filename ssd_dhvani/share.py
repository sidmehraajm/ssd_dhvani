"""Studio share: is the drive AYON publishes to mounted? If not, mount it.

smb://kumbh/production is the same share Windows maps as P:\\. AYON's
project anatomy gives each OS its own root for it (windows "P:/",
darwin e.g. "/Volumes/production"). Publishing copies files under that
root, so it has to be a REAL mount at exactly that path:

    macOS   -> `mount volume` via osascript (no sudo, lands in /Volumes/<share>)
    Windows -> `net use P: \\\\kumbh\\production ...`

Nothing here ever creates the mount folder: on a Mac a stray local
/Volumes/production folder would swallow "published" files (publish.py
refuses that too).
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

from . import tools

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"


@dataclass
class ShareStatus:
    ok: bool
    root: str          # the path that must exist (AYON publish root)
    message: str
    mounted_now: bool = False    # True = this call did the mounting


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def smb_parts(url: str) -> tuple[str, str]:
    """'smb://kumbh/production' -> ('kumbh', 'production')."""
    u = urlparse(url.strip())
    host = u.hostname or ""
    share = u.path.strip("/").split("/")[0] if u.path else ""
    return host, share


def unc_path(url: str) -> str:
    host, share = smb_parts(url)
    return rf"\\{host}\{share}"


def default_mount_point(url: str) -> str:
    """Where the OS puts the share when nothing else is configured."""
    _host, share = smb_parts(url)
    if IS_WIN:
        return "P:\\"
    return f"/Volumes/{share}" if share else ""


def ayon_root(project: str) -> str:
    """This machine's AYON publish root for `project` ('' if unknown)."""
    if not project:
        return ""
    try:
        from . import publish
        _name, root = publish._publish_root(project)
        return str(root or "")
    except Exception:
        return ""


def volume_of(root: str) -> str:
    """The mount point that has to exist for `root`.
    macOS '/Volumes/production/projects' -> '/Volumes/production'
    Windows 'P:/projects'                 -> 'P:\\'"""
    if IS_WIN:
        drive, _ = os.path.splitdrive(root)
        if drive and not drive.startswith("\\\\"):
            return drive.upper() + "\\"
        return drive          # UNC: \\host\share
    parts = os.path.normpath(root).split(os.sep)
    if len(parts) >= 3 and parts[1] == "Volumes":
        return os.sep.join(parts[:3])
    return root


def mac_mounts() -> dict[str, str]:
    """{mount point: '//user@host/share'} for every SMB mount (macOS)."""
    out: dict[str, str] = {}
    try:
        txt = subprocess.run(["mount"], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return out
    for line in txt.splitlines():
        m = re.match(r"^(//\S+) on (.+?) \((smbfs|cifs)", line)
        if m:
            out[m.group(2)] = m.group(1)
    return out


def is_mounted(root: str) -> bool:
    if not root:
        return False
    vol = volume_of(root)
    if IS_MAC and vol.startswith("/Volumes/"):
        # a plain local folder at /Volumes/x is NOT the share
        return os.path.ismount(vol) and os.path.isdir(root)
    return os.path.isdir(root)


def host_reachable(host: str, timeout: float = 3.0) -> bool:
    """Can we open the SMB port? Fails fast instead of a 60 s mount hang."""
    try:
        with socket.create_connection((host, 445), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Mounting
# --------------------------------------------------------------------------- #
def _mount_mac(url: str, user: str, password: str) -> tuple[bool, str]:
    # Credentials go in as argv, never pasted into the script text, so
    # quotes or backslashes in a password can't break (or inject into) it.
    script = ["-e", "on run argv"]
    if user:
        script += ["-e", "mount volume (item 1 of argv) as user name "
                         "(item 2 of argv) with password (item 3 of argv)"]
    else:
        script += ["-e", "mount volume (item 1 of argv)"]
    script += ["-e", "end run"]
    try:
        r = subprocess.run(["osascript", *script, url, user, password],
                           capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return False, "mount timed out after 45 s"
    except FileNotFoundError:
        return False, "osascript not found"
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if "-128" in err:
            err = "cancelled"
        else:
            err += " (check the user name / password in ● Drive)"
        return False, err or f"osascript exit {r.returncode}"
    return True, (r.stdout or "").strip()


def _mount_win(url: str, user: str, password: str,
               drive: str) -> tuple[bool, str]:
    cmd = ["net", "use"]
    if drive and not drive.startswith("\\\\"):
        cmd.append(drive.rstrip("\\"))
    cmd.append(unc_path(url))
    if user:
        cmd += [password, f"/user:{user}"]
    cmd.append("/persistent:yes")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45,
                           **tools.subprocess_flags())
    except subprocess.TimeoutExpired:
        return False, "net use timed out after 45 s"
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if "1219" in err:
            err += (" — Windows is already connected to this server with "
                    "another user. Disconnect it (net use * /delete) or use "
                    "the same user.")
        return False, err
    return True, (r.stdout or "").strip()


def ensure(cfg, project: str, mount: bool = True, log=None) -> ShareStatus:
    """Check the publish root; mount the share if it is missing and `mount`.

    The root comes from AYON (the project's root for this OS); config
    `share_mount_point` overrides it; else the OS default for the share."""
    _log = log or (lambda s: None)
    root = (ayon_root(project) or cfg.share_mount_point.strip()
            or default_mount_point(cfg.share_url))
    if not root:
        return ShareStatus(False, "", "No share URL / mount point configured")
    if is_mounted(root):
        return ShareStatus(True, root, f"mounted at {root}")
    if not mount:
        return ShareStatus(False, root, f"not mounted ({root})")

    host, share = smb_parts(cfg.share_url)
    if not (host and share):
        return ShareStatus(False, root, f"bad share URL {cfg.share_url!r} "
                                        "(expected smb://server/share)")
    vol = volume_of(root)

    if IS_MAC:
        # Already mounted somewhere ELSE (e.g. /Volumes/production-1)?
        for point, src in mac_mounts().items():
            if src.lower().endswith(f"{host}/{share}".lower()) and point != vol:
                return ShareStatus(False, root, (
                    f"{cfg.share_url} is mounted at {point}, but AYON "
                    f"publishes to {vol}. Eject {point} in Finder and press "
                    "Connect again."))
        if os.path.isdir(vol) and not os.path.ismount(vol):
            return ShareStatus(False, root, (
                f"{vol} is a LOCAL folder, so macOS can't mount the share "
                f"there (it would go to {vol}-1). Remove that folder first: "
                f"sudo rm -r '{vol}'  (check it holds nothing you need)."))

    if not host_reachable(host):
        return ShareStatus(False, root, (
            f"server '{host}' not reachable on port 445 — check the LAN "
            "cable / VPN and that the name resolves"))

    _log(f"Mounting {cfg.share_url} as "
         f"{cfg.share_user or '(guest / keychain)'} …")
    if IS_MAC:
        ok, msg = _mount_mac(cfg.share_url, cfg.share_user, cfg.share_password)
    elif IS_WIN:
        ok, msg = _mount_win(cfg.share_url, cfg.share_user, cfg.share_password,
                             vol)
    else:
        return ShareStatus(False, root, "auto-mount is only for macOS / Windows")
    if not ok:
        return ShareStatus(False, root, f"mount failed: {msg}")
    if is_mounted(root):
        return ShareStatus(True, root, f"mounted at {root}", mounted_now=True)
    # mounted, but not where AYON looks
    where = ""
    if IS_MAC:
        where = next((p for p, s in mac_mounts().items()
                      if s.lower().endswith(f"{host}/{share}".lower())), "")
    return ShareStatus(False, root, (
        f"the share mounted{' at ' + where if where else ''}, but {root} "
        "still doesn't exist — the AYON darwin/windows root must point "
        "inside this share"))
