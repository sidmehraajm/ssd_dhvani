"""Local mirror of the AYON project / shot / task names (SQLite).

Why: away from the network the tool still needs the real shot names so takes
land in the correct folder structure and can be published later from any
machine. Only NAMES and ids are cached -- no media, no credentials.

The cache refreshes whenever a project's folders are fetched while online, so
signing in on the network keeps it current. Everything here is best-effort:
any failure degrades to "no cache" and is logged, never raised at the UI.
"""
from __future__ import annotations

import functools
import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("ssd_dhvani.shotdb")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name     TEXT PRIMARY KEY,
    synced   REAL
);
CREATE TABLE IF NOT EXISTS folders (
    project   TEXT,
    path      TEXT,
    folder_id TEXT,
    PRIMARY KEY (project, path)
);
CREATE TABLE IF NOT EXISTS tasks (
    project   TEXT,
    folder_id TEXT,
    name      TEXT,
    task_id   TEXT,
    task_type TEXT,
    PRIMARY KEY (project, folder_id, name)
);
CREATE INDEX IF NOT EXISTS idx_folders_project ON folders(project);
CREATE INDEX IF NOT EXISTS idx_tasks_folder ON tasks(project, folder_id);
"""


def _locked(fn):
    """Serialise every DB call on the instance lock.

    One connection is shared by the UI thread and the background shot-sync
    thread (check_same_thread=False). sqlite3 connections are NOT safe for
    concurrent use -- overlapping calls can raise 'recursive use of cursors'
    or corrupt a transaction -- so every public method runs under this lock.
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


class ShotDB:
    """Tiny wrapper around a local SQLite file. Thread-safe: the UI thread
    and the background sync share it, serialised by an instance lock."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._con: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._con = sqlite3.connect(str(self.path), check_same_thread=False)
            self._con.executescript(_SCHEMA)
            self._con.commit()
        except Exception as exc:    # pragma: no cover
            log.warning("shot cache unavailable (%s): %s", self.path, exc)
            self._con = None

    @property
    def ok(self) -> bool:
        return self._con is not None

    # ---- writes (online) ------------------------------------------------- #
    @_locked
    def sync_projects(self, names: list[str]) -> None:
        if not self._con:
            return
        try:
            with self._con:
                for n in names:
                    self._con.execute(
                        "INSERT INTO projects(name, synced) VALUES(?, ?) "
                        "ON CONFLICT(name) DO NOTHING", (n, 0.0))
        except Exception as exc:    # pragma: no cover
            log.warning("sync_projects failed: %s", exc)

    @_locked
    def sync_folders(self, project: str, folders: dict[str, str]) -> None:
        """Replace the cached folder list for one project."""
        if not self._con or not project:
            return
        try:
            with self._con:
                self._con.execute("DELETE FROM folders WHERE project = ?",
                                  (project,))
                self._con.executemany(
                    "INSERT OR REPLACE INTO folders(project, path, folder_id) "
                    "VALUES(?, ?, ?)",
                    [(project, p, fid) for p, fid in folders.items()])
                self._con.execute(
                    "INSERT INTO projects(name, synced) VALUES(?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET synced = excluded.synced",
                    (project, time.time()))
        except Exception as exc:    # pragma: no cover
            log.warning("sync_folders failed: %s", exc)

    @_locked
    def sync_tasks(self, project: str, folder_id: str, tasks) -> None:
        if not self._con or not (project and folder_id):
            return
        try:
            with self._con:
                self._con.execute(
                    "DELETE FROM tasks WHERE project = ? AND folder_id = ?",
                    (project, folder_id))
                self._con.executemany(
                    "INSERT OR REPLACE INTO tasks"
                    "(project, folder_id, name, task_id, task_type) "
                    "VALUES(?, ?, ?, ?, ?)",
                    [(project, folder_id, t.name, t.id,
                      getattr(t, "task_type", "")) for t in tasks])
        except Exception as exc:    # pragma: no cover
            log.warning("sync_tasks failed: %s", exc)

    @_locked
    def sync_project_tasks(self, project: str,
                           rows: list[tuple[str, object]]) -> None:
        """Replace ALL cached tasks for a project. `rows` = [(folder_id, Task)]."""
        if not self._con or not project:
            return
        try:
            with self._con:
                self._con.execute("DELETE FROM tasks WHERE project = ?",
                                  (project,))
                self._con.executemany(
                    "INSERT OR REPLACE INTO tasks"
                    "(project, folder_id, name, task_id, task_type) "
                    "VALUES(?, ?, ?, ?, ?)",
                    [(project, fid, t.name, t.id, getattr(t, "task_type", ""))
                     for fid, t in rows])
        except Exception as exc:    # pragma: no cover
            log.warning("sync_project_tasks failed: %s", exc)

    # ---- reads (offline) -------------------------------------------------- #
    @_locked
    def projects(self) -> list[str]:
        if not self._con:
            return []
        try:
            return [r[0] for r in self._con.execute(
                "SELECT name FROM projects ORDER BY name")]
        except Exception:
            return []

    @_locked
    def folders(self, project: str) -> dict[str, str]:
        if not self._con:
            return {}
        try:
            return {p: fid for p, fid in self._con.execute(
                "SELECT path, folder_id FROM folders WHERE project = ? "
                "ORDER BY path", (project,))}
        except Exception:
            return {}

    @_locked
    def tasks(self, project: str, folder_id: str) -> list[tuple[str, str, str]]:
        """[(name, task_id, task_type), ...]"""
        if not self._con:
            return []
        try:
            return list(self._con.execute(
                "SELECT name, task_id, task_type FROM tasks "
                "WHERE project = ? AND folder_id = ? ORDER BY name",
                (project, folder_id)))
        except Exception:
            return []

    @_locked
    def last_sync(self, project: str) -> float:
        if not self._con:
            return 0.0
        try:
            row = self._con.execute(
                "SELECT synced FROM projects WHERE name = ?",
                (project,)).fetchone()
            return float(row[0]) if row and row[0] else 0.0
        except Exception:
            return 0.0

    @_locked
    def stats(self) -> tuple[int, int]:
        """(projects, folders) currently cached."""
        if not self._con:
            return (0, 0)
        try:
            p = self._con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            f = self._con.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
            return (int(p), int(f))
        except Exception:
            return (0, 0)

    @_locked
    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            except Exception:
                pass
            self._con = None
