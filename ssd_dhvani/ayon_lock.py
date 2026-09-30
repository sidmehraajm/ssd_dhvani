"""One lock for the ONE shared AYON connection.

The app deliberately uses a single global ayon_api connection (a split
connection was the original "nothing works" bug). But that connection is now
used from several threads at once -- the UI, the background shot sync, the
offline-cache download and publishing -- and its underlying HTTP session is
not guaranteed thread-safe. Every metadata call is therefore serialised here.

Two details matter:

* ayon_api's query functions (get_folders, get_products, ...) return
  GENERATORS that do their HTTP requests lazily while being iterated. They are
  materialised into lists INSIDE the lock; otherwise the lock would be
  released before any request actually happened.
* Bulk transfers (upload/download of media) are NOT held under the lock.
  They are long and bandwidth-bound; holding the lock for a whole video
  upload would freeze every other query -- including the UI's -- for its
  duration.
"""
from __future__ import annotations

import functools
import inspect
import threading

#: Re-entrant, so code already holding it may call helpers that take it too.
API_LOCK = threading.RLock()

#: Long transfers: run without the lock (see module docstring).
_UNLOCKED = frozenset({
    "upload_reviewable", "upload_file", "download_file",
    "download_file_to_stream", "upload_file_from_stream",
})


class LockedAPI:
    """Drop-in proxy for the ayon_api module: `api.get_folders(...)` etc."""

    def __init__(self, module) -> None:
        self._mod = module

    def __getattr__(self, name):
        attr = getattr(self._mod, name)
        if not callable(attr) or inspect.isclass(attr) or name in _UNLOCKED:
            return attr

        @functools.wraps(attr)
        def call(*args, **kwargs):
            with API_LOCK:
                result = attr(*args, **kwargs)
                if inspect.isgenerator(result):
                    result = list(result)       # run the lazy requests NOW
                return result
        return call
