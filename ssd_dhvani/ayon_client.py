"""AYON access -- ONE global per-user connection shared by queries and publish.

Auth is identical to the proven batch publisher
(vendor/ayon_batch_folder_publisher.py, handle_login):

    os.environ["AYON_SERVER_URL"] = url
    con = ayon_api.GlobalServerAPI()
    con.login(user, password)

That sets ayon-python-api's GLOBAL default connection, so every module-level
``ayon_api.*`` call is authenticated -- the queries here AND the publish calls
in publish.py.

No fake data and no swallowed errors: failures raise ``AyonError`` with a
clear message; the UI logs the full traceback in its on-screen log panel.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ssd_dhvani.ayon")

from .ayon_lock import API_LOCK, LockedAPI

try:
    import ayon_api as _ayon_api_module  # type: ignore
    # Every ayon_api.* call in this module goes through the shared lock (see
    # ayon_lock.py): the UI, the shot sync and the offline cache all use the
    # ONE global connection concurrently.
    ayon_api = LockedAPI(_ayon_api_module)
    _HAVE_AYON = True
except Exception:  # pragma: no cover - import guard
    ayon_api = None  # type: ignore
    _HAVE_AYON = False

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".mxf"}
AUDIO_EXTS = {".wav", ".flac", ".mp3", ".aif", ".aiff", ".m4a", ".ogg"}


class AyonError(RuntimeError):
    """Raised for any AYON failure -- message is meant to be shown to the user."""


_ROOT_TMPL = re.compile(r"^\{root\[(\w+)\]\}(.*)$")


def _local_roots(project: str) -> dict:
    """This machine's roots, with the Mac share translation (publish.py)."""
    from .publish import local_roots
    return local_roots(project)


def _resolve_root_template(path: str, roots: dict) -> str | None:
    """'{root[publish]}/Proj/...' -> '<this machine's publish root>/Proj/...'"""
    m = _ROOT_TMPL.match(path)
    if not m:
        return None
    root = roots.get(m.group(1))
    if not root:
        return None
    return os.path.normpath(str(root) + m.group(2))


def _expand_candidates(raw_paths: list[str], roots: dict,
                       project: str) -> list[str]:
    """All plausible local paths for representation file entries.

    Publishes store the PUBLISHING machine's literal path plus {root[x]}
    templates; this machine's roots may differ (e.g. stored P:\\PR_PRE\\...
    while here publish=P:\\Training, work=C:/Training). So for every entry we
    also rebuild the tail (from /{project}/ onward) against EVERY local root.
    """
    out: list[str] = []
    marker = f"/{project}/"
    for p in raw_paths:
        out.append(p)
        m = _ROOT_TMPL.match(p)
        tail = None
        if m:
            tail = m.group(2)
        tails: list[str] = []
        if tail:
            tails.append(tail)
        else:
            # EVERY '/{project}/' in the path is a candidate cut point: with
            # a root folder named like the project (P:\\Training\\Training\\...)
            # the FIRST match is the root, not the project -- on a Mac that
            # built '/Volumes/x/Training/Training/...', which doesn't exist.
            norm = p.replace("\\", "/")
            i = norm.find(marker)
            while i >= 0:
                tails.append(norm[i:])
                i = norm.find(marker, i + 1)
        for t in tails:
            for root in roots.values():
                if root:
                    out.append(os.path.normpath(str(root).rstrip("/\\") + t))
    seen: set[str] = set()
    return [c for c in out if not (c in seen or seen.add(c))]


@dataclass
class Task:
    name: str
    id: str
    task_type: str = ""


def diagnose_server(url: str, timeout: float = 6.0) -> list[str]:
    """Why can't we sign in? Checks DNS, TCP, and the AYON API itself.

    Every line is safe to show the operator and to paste into a bug report;
    no credentials are included.
    """
    import socket
    import urllib.error
    import urllib.request
    from urllib.parse import urlparse

    lines: list[str] = []
    url = (url or "").strip().rstrip("/")
    if not url:
        return ["no server URL given"]
    p = urlparse(url)
    host = p.hostname or ""
    port = p.port or (443 if p.scheme == "https" else 80)
    lines.append(f"URL      : {url}  (host={host}, port={port})")

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        lines.append(f"DNS      : {host} -> {', '.join(ips)}")
    except Exception as exc:
        lines.append(f"DNS      : ✕ cannot resolve '{host}' ({exc})")
        return lines

    try:
        with socket.create_connection((host, port), timeout=timeout):
            lines.append(f"TCP      : ✓ connected to {host}:{port}")
    except Exception as exc:
        lines.append(f"TCP      : ✕ cannot connect to {host}:{port} ({exc}) "
                     "— firewall, VPN or the service is down")
        return lines

    try:
        req = urllib.request.Request(f"{url}/api/info",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(4000).decode("utf-8", "ignore")
            lines.append(f"API      : ✓ HTTP {r.status} from /api/info")
            try:
                info = json.loads(body)
                ver = info.get("version") or info.get("ayonVersion")
                if ver:
                    lines.append(f"Server   : AYON {ver}")
                if "loginPageBrand" in info or "version" in info:
                    lines.append("Identity : looks like a real AYON server")
            except Exception:
                lines.append("API      : ⚠ /api/info was not JSON — is this "
                             "URL a proxy or the wrong service?")
    except urllib.error.HTTPError as exc:
        lines.append(f"API      : HTTP {exc.code} from /api/info ({exc.reason})")
    except Exception as exc:
        lines.append(f"API      : ✕ {type(exc).__name__}: {exc}")
    return lines


def explain_login_error(exc: Exception) -> str:
    """Turn an ayon_api exception into something actionable."""
    text = f"{exc}".strip()
    low = text.lower()
    if "too many failed" in low or "429" in low:
        return ("The SERVER is rate-limiting this account/machine after "
                "repeated bad passwords — it is not a network problem. "
                "Wait for the lockout to expire (usually a few minutes), "
                "then try ONCE with a known-good password. An AYON admin can "
                "clear it sooner, and can raise the limit in Server Settings.")
    if "unauthor" in low or "401" in low or "invalid" in low or "credential" in low:
        return ("The server rejected these credentials. Check the username "
                "and password, and that this user is enabled on the server.")
    if "forbidden" in low or "403" in low:
        return ("Authenticated but not permitted — this user may be "
                "deactivated or lack access to the project.")
    if any(k in low for k in ("timed out", "timeout", "unreachable",
                              "connection", "refused", "resolve", "ssl",
                              "name or service", "getaddrinfo", "nodename",
                              "no such host", "name resolution", "network",
                              "certificate", "max retries", "socket")):
        return ("Could not reach the server from this machine — check the "
                "URL, VPN/firewall, and that the port is open.")
    return "Unexpected error — see the details below."


class AyonClient:
    """Thin wrapper. After ``login()`` all methods use the global connection."""

    def __init__(self) -> None:
        self.server_url = ""
        self.username = ""
        self._connected = False

    # ------------------------------------------------------------------ #
    # Connection -- same flow as the batch publisher
    # ------------------------------------------------------------------ #
    def login(self, url: str, user: str, password: str) -> str:
        if not _HAVE_AYON:
            raise AyonError(
                "ayon-python-api is not installed. Run: pip install ayon-python-api")
        url = url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise AyonError(f"Server URL must start with http(s)://  (got: {url!r})")
        os.environ["AYON_SERVER_URL"] = url
        with API_LOCK:                     # nothing may use the old connection
            try:
                ayon_api.close_connection()
            except Exception:
                pass
            con = ayon_api.GlobalServerAPI()
            con.login(user, password)      # raises on bad credentials
            me = con.get_user()
        self.username = me.get("name", user) if isinstance(me, dict) else user
        self.server_url = url
        self._connected = True
        log.info("Logged in to %s as %s", url, self.username)
        return self.username

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------ #
    # Queries -- module-level ayon_api.*, verbatim style of the publisher
    # ------------------------------------------------------------------ #
    def list_projects(self) -> list[str]:
        self._require()
        return [p["name"] for p in ayon_api.get_projects()]

    def folders(self, project: str) -> dict[str, str]:
        """All folder paths for a project -> {path: folder_id}.

        Mirrors the batch publisher's cache_project_data(): no folderType
        filtering guesswork -- the user picks by path.
        """
        self._require()
        return {f["path"]: f["id"] for f in ayon_api.get_folders(project)}

    def tasks_for_folder(self, project: str, folder_id: str) -> list[Task]:
        self._require()
        out = [
            Task(name=t["name"], id=t["id"], task_type=t.get("taskType", ""))
            for t in ayon_api.get_tasks(project, folder_ids=[folder_id])
        ]
        out.sort(key=lambda t: t.name)
        return out

    def all_tasks(self, project: str) -> list[tuple[str, Task]]:
        """EVERY task in a project as (folder_id, Task) -- ONE request.

        Used by the offline cache sync: querying per folder would be hundreds
        of round trips per project.
        """
        self._require()
        out: list[tuple[str, Task]] = []
        for t in ayon_api.get_tasks(project):
            fid = t.get("folderId") or ""
            if fid:
                out.append((fid, Task(name=t["name"], id=t["id"],
                                      task_type=t.get("taskType", ""))))
        return out

    # ------------------------------------------------------------------ #
    # Reference load -- latest published version's real file path
    # ------------------------------------------------------------------ #
    def versions_for_task(self, project: str, folder_id: str,
                          task_id: str) -> list[dict]:
        """Published versions on a task, newest first (for the version picker)."""
        self._require()
        versions = self._versions_for_task(project, folder_id, task_id)
        versions.sort(key=lambda v: v.get("version", 0), reverse=True)
        return versions

    # ---- offline copies: plan what to fetch for a task (all shots) ------ #
    def plan_offline_task(self, project: str, task_name: str,
                          playable_exts: set, progress=None) -> list[dict]:
        """Latest version of every product published under `task_name`, on
        EVERY shot of `project`, with the local source path of each playable
        file. Batched: a handful of requests for the whole project rather
        than one per product.

        Each item: {folder_path, folder_id, task, task_id, product, version,
        version_id, author, created, comment, files: [{src, rel, size}],
        server_only: bool}. `server_only` means no file is reachable on disk
        from this machine -- the downloader then fetches the reviewable.
        """
        self._require()
        say = progress or (lambda _m: None)
        folders = self.folders(project)
        id2path = {fid: path for path, fid in folders.items()}
        tasks = {t.id: fid for fid, t in self.all_tasks(project)
                 if t.name.lower() == task_name.lower()}
        if not tasks:
            return []
        say(f"'{task_name}' exists on {len(tasks)} shot(s) — finding versions…")

        # All versions of those tasks in ONE query; older API -> per-shot scan.
        try:
            versions = ayon_api.get_versions(project, task_ids=list(tasks))
        except TypeError:
            versions = []
            for tid, fid in tasks.items():
                versions += self._versions_for_task(project, fid, tid)
        latest: dict = {}
        for v in versions:
            pid = v.get("productId")
            if pid and (pid not in latest
                        or v.get("version", 0) > latest[pid].get("version", 0)):
                latest[pid] = v
        if not latest:
            return []
        prods = {p["id"]: p for p in ayon_api.get_products(
            project, product_ids=list(latest))}
        say(f"{len(latest)} product(s) — resolving files…")
        reps = ayon_api.get_representations(
            project, version_ids=[v["id"] for v in latest.values()])
        by_version: dict = {}
        for r in reps:
            by_version.setdefault(r.get("versionId"), []).append(r)
        try:
            roots = _local_roots(project)
        except Exception:
            roots = {}

        plan: list[dict] = []
        for pid, v in latest.items():
            prod = prods.get(pid, {})
            fid = prod.get("folderId") or tasks.get(v.get("taskId"), "")
            files, used = [], set()
            for rep in by_version.get(v["id"], []):
                rfiles = rep.get("files") or []
                for f in rfiles:
                    name = f.get("name") or Path(f.get("path", "")).name
                    if Path(name).suffix.lower() not in playable_exts:
                        continue
                    src = next((c for c in _expand_candidates(
                        [f.get("path", "")], roots, project)
                        if c and os.path.exists(c)), None)
                    rel = name if len(rfiles) == 1 else f"{rep['name']}/{name}"
                    if rel in used:
                        rel = f"{rep['name']}/{name}"
                    used.add(rel)
                    files.append({"src": src, "rel": rel,
                                  "size": int(f.get("size") or 0)})
            reachable = [f for f in files if f["src"]]
            plan.append({
                "project": project,
                "folder_id": fid,
                "folder_path": id2path.get(fid, fid),
                "task": task_name,
                "task_id": v.get("taskId", ""),
                "product": prod.get("name", pid),
                "version": int(v.get("version", 0)),
                "version_id": v["id"],
                "author": v.get("author") or "",
                "created": str(v.get("createdAt", ""))[:19].replace("T", " "),
                "comment": (v.get("attrib") or {}).get("comment", "") or "",
                "files": reachable,
                "server_only": not reachable,
            })
        plan.sort(key=lambda it: (it["folder_path"], it["product"]))
        return plan

    def download_reviewable_to(self, project: str, version_id: str,
                               dest_dir: Path) -> Path | None:
        """Fetch a version's reviewable straight into `dest_dir`."""
        tmp = self._download_reviewable(project, version_id)
        if not tmp:
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        dst = dest_dir / Path(tmp).name.split("_", 1)[-1]
        shutil.copy2(tmp, dst)
        return dst

    # ---- published take metadata (readable on ANY machine) ------------- #
    def take_sidecars(self, project: str, folder_id: str,
                      task_id: str) -> list[dict]:
        """Read every published take's metadata JSON for a task.

        The recording machine publishes an `ssd_take.json` beside the media;
        this reads it back so a REVIEW machine -- which never saw the local
        manifest -- knows each version's status, comments and tracks.
        Versions without a sidecar (older publishes) come back with whatever
        the server itself knows, flagged `sidecar: False`.
        """
        self._require()
        out: list[dict] = []
        try:
            roots = _local_roots(project)
        except Exception:   # pragma: no cover
            roots = {}
        for version in self.versions_for_task(project, folder_id, task_id):
            entry = {
                "version": int(version.get("version", 0)),
                "version_id": version.get("id", ""),
                "author": version.get("author", "") or "",
                "createdAt": str(version.get("createdAt", "")),
                "server_status": (version.get("status") or ""),
                "comments": (version.get("attrib") or {}).get("comment", ""),
                "review_status": "",
                "take": "",
                "tracks": [],
                "sidecar": False,
            }
            try:
                data = self._read_sidecar(project, version, roots)
                if data:
                    entry.update({
                        "sidecar": True,
                        "take": data.get("take", ""),
                        "take_number": data.get("take_number", 0),
                        "review_status": data.get("status", ""),
                        "comments": data.get("comments") or entry["comments"],
                        "tracks": (["facial video"] if data.get("facial") else [])
                                  + list((data.get("tracks") or {}).keys()),
                        "published_at": data.get("published_at", ""),
                        "author": data.get("author") or entry["author"],
                        "format": " · ".join(str(x) for x in (
                            data.get("samplerate"), data.get("sample_format"))
                            if x),
                    })
            except Exception as exc:    # pragma: no cover
                log.debug("sidecar read failed for v%s: %s",
                          entry["version"], exc)
            out.append(entry)
        return out

    def _read_sidecar(self, project: str, version: dict, roots: dict) -> dict:
        """Find and parse this version's ssd_take.json (on disk)."""
        reps = list(ayon_api.get_representations(
            project, version_ids=[version["id"]]))
        raw: list[str] = []
        for rep in reps:
            p = (rep.get("attrib") or {}).get("path")
            if p and p.lower().endswith(".json"):
                raw.append(p)
            for f in rep.get("files") or []:
                fp = f.get("path")
                if fp and fp.lower().endswith(".json"):
                    raw.append(fp)
        for cand in _expand_candidates(raw, roots, project):
            if os.path.exists(cand):
                with open(cand, "r", encoding="utf-8") as fp:
                    return json.load(fp)
        return {}

    def resolve_version_file(self, project: str, version: dict) -> dict:
        """A playable local file for one specific version.

        1. Try every representation path: literal, {root[x]} template, and the
           tail rebuilt against EVERY of this machine's site roots.
        2. If nothing exists on disk, DOWNLOAD the version's reviewable from
           the AYON server (your batch publisher uploads one per publish) to a
           local cache and play that.
        Raises AyonError listing every candidate when both fail.
        """
        self._require()
        ver_str = f"v{version['version']:03d}"
        reps = list(ayon_api.get_representations(
            project, version_ids=[version["id"]]))
        try:
            roots = _local_roots(project)
        except Exception:  # pragma: no cover
            roots = {}
        raw: list[str] = []
        for rep in reps:
            p = (rep.get("attrib") or {}).get("path")
            if p:
                raw.append(p)
            for f in rep.get("files") or []:
                if f.get("path"):
                    raw.append(f["path"])
        candidates = _expand_candidates(raw, roots, project)
        existing = [c for c in candidates if os.path.exists(c)]
        if existing:
            # Playable media first: video, then audio; a sidecar .json is
            # never what the operator wants opened.
            def rank(c: str) -> int:
                ext = os.path.splitext(c)[1].lower()
                return 0 if ext in VIDEO_EXTS else 1 if ext in AUDIO_EXTS else 2
            existing.sort(key=rank)
            return {"path": existing[0], "version": ver_str, "source": "disk"}

        dl = self._download_reviewable(project, version["id"])
        if dl:
            return {"path": dl, "version": ver_str, "source": "server"}

        note = f"\nReviewable download: {self._last_reviewable_note}" \
            if self._last_reviewable_note else ""
        raise AyonError(
            f"{ver_str}: no file on disk and no usable reviewable on the "
            f"server.\nSite roots here: {roots}{note}\nPaths tried:\n  "
            + ("\n  ".join(candidates) if candidates else "(none stored)"))

    def latest_published_file(self, project: str, folder_id: str,
                              task_id: str) -> dict:
        """Newest version that yields a playable file (disk or server)."""
        versions = self.versions_for_task(project, folder_id, task_id)
        if not versions:
            raise AyonError("No published versions found for this task.")
        first_err: AyonError | None = None
        for ver in versions:
            try:
                return self.resolve_version_file(project, ver)
            except AyonError as exc:
                if first_err is None:
                    first_err = exc
        raise first_err  # type: ignore[misc]

    # ------------------------------------------------------------------ #
    _last_reviewable_note: str = ""

    @staticmethod
    def _valid_media(path: Path) -> tuple[bool, str]:
        """True if the downloaded file looks like real media, not a server
        error payload (JSON/HTML) or a truncated stub."""
        try:
            size = path.stat().st_size
            head = path.read_bytes()[:400]
        except OSError as exc:
            return False, str(exc)
        if head[:1] in (b"{", b"<"):
            return False, f"server returned an error payload: {head.decode(errors='replace')!r}"
        if size < 5120:
            return False, f"only {size} bytes (truncated?)"
        return True, ""

    def _download_reviewable(self, project: str, version_id: str) -> str | None:
        """Fetch the version's reviewable media from the AYON server into a
        local cache. Tries EVERY reviewable entry, validates each download,
        and records the failure reason in _last_reviewable_note."""
        self._last_reviewable_note = ""
        try:
            res = ayon_api.get(
                f"projects/{project}/versions/{version_id}/reviewables")
            data = getattr(res, "data", None) or {}
            reviewables = data.get("reviewables") or []
        except Exception as exc:
            self._last_reviewable_note = f"reviewables query failed: {exc}"
            log.warning(self._last_reviewable_note)
            return None
        if not reviewables:
            self._last_reviewable_note = "version has no reviewables on the server"
            return None
        log.info("reviewables for %s: %s", version_id,
                 [(r.get("filename"), r.get("mimetype"), r.get("availability"))
                  for r in reviewables])
        # video mimetypes first, then everything else
        ordered = sorted(reviewables,
                         key=lambda r: not (r.get("mimetype") or "").startswith("video"))
        cache = Path(tempfile.gettempdir()) / "ssd_dhvani_ref"
        cache.mkdir(parents=True, exist_ok=True)
        notes: list[str] = []
        for pick in ordered:
            fid = pick.get("fileId")
            if not fid:
                continue
            fname = pick.get("filename") or f"{fid}.mp4"
            dst = cache / f"{version_id}_{fname}"
            if dst.exists():
                ok, _why = self._valid_media(dst)
                if ok:
                    return str(dst)     # cache hit
                dst.unlink(missing_ok=True)
            try:
                ayon_api.download_file(f"projects/{project}/files/{fid}",
                                       str(dst))
            except Exception as exc:
                notes.append(f"{fname}: download failed: {exc}")
                continue
            ok, why = self._valid_media(dst)
            if ok:
                return str(dst)
            notes.append(f"{fname}: {why}")
            log.warning("reviewable %s rejected: %s", fname, why)
            dst.unlink(missing_ok=True)
        self._last_reviewable_note = "; ".join(notes) or "no downloadable entry"
        return None

    def _versions_for_task(self, project: str, folder_id: str,
                           task_id: str) -> list[dict]:
        """Versions linked to a task. Primary: get_versions(task_ids=...).
        Fallback (older api): scan the folder's products and keep last
        versions whose taskId matches."""
        try:
            return list(ayon_api.get_versions(project, task_ids=[task_id]))
        except TypeError:
            log.info("get_versions(task_ids=...) unsupported; falling back "
                     "to product scan")
        out: list[dict] = []
        for prod in ayon_api.get_products(project, folder_ids=[folder_id]):
            lv = ayon_api.get_last_version_by_product_id(project, prod["id"])
            if lv and lv.get("taskId") == task_id:
                out.append(lv)
        return out

    # ------------------------------------------------------------------ #
    def _require(self) -> None:
        if not _HAVE_AYON:
            raise AyonError("ayon-python-api is not installed.")
        if not self._connected:
            raise AyonError("Not logged in to AYON.")


class OfflineClient:
    """Stand-in for AyonClient when working away from the network.

    Same read API, but every query returns empty and reference/publish
    helpers raise. The main window detects ``is_connected == False`` and
    switches to typed shot/task entry under {save_root}/offline/.
    """

    server_url = ""
    username = "offline"
    is_connected = False

    def login(self, *_a, **_k) -> str:
        raise AyonError("Offline mode — no AYON connection")

    def list_projects(self) -> list[str]:
        return []

    def folders(self, project: str) -> dict[str, str]:
        return {}

    def tasks_for_folder(self, project: str, folder_id: str) -> list[Task]:
        return []

    def versions_for_task(self, project: str, folder_id: str,
                          task_id: str) -> list[dict]:
        return []

    def resolve_version_file(self, project: str, version: dict) -> dict:
        raise AyonError("Offline mode — reference loading needs AYON")

    def latest_published_file(self, *_a, **_k):
        raise AyonError("Offline mode — reference loading needs AYON")
