"""Live audio input + per-take recording.

Design (read this before changing anything)
-------------------------------------------
* ONE input stream stays open while the app runs (`AudioInput`). The level
  meters therefore move BEFORE you press record -- which is how you set the
  gain. Pressing record only attaches a writer; nothing is re-opened, so a
  take starts instantly.

* THREADS. PortAudio's control calls (open / start / stop / close) are made
  on the GUI thread ONLY. PortAudio is not thread-safe, and on Windows the
  WASAPI objects belong to the thread that created them: opening on a worker
  and closing on the GUI thread corrupted memory and crashed the app later at
  an unrelated spot (v1.0.0, "crash on Publish").
  PortAudio then calls `_callback` on ITS real-time thread. That thread must
  never wait on the disk (a slow write = a dropout = a click), so it only
  copies the block into a queue; `_TakeWriter` (a normal thread) writes.

* NO CONVERSION ON THE WAY IN. We capture in the format the driver already
  delivers, so PortAudio never converts (and never adds dither):
    - Windows WASAPI *shared* and macOS Core Audio run a FLOAT mixer ->
      capture float32.
    - WDM-KS, WASAPI *exclusive*, ASIO, MME, DirectSound hand over integer
      samples straight from the driver -> capture int32 (a 24-bit sample sits
      in the top 24 bits; libsndfile writes those bits into a 24-bit WAV
      bit-exact).
  `dither_off=True` guarantees PortAudio never adds noise of its own.

* CRASH-SAFE FILES. A WAV's header stores the audio length. Normally it is
  written when the file closes -- so a crash / power cut mid-take leaves a
  header saying "0 bytes" and most apps open an empty file. The writer
  rewrites the header every second, so at most the last second is at risk.

* SIGNAL-PATH CHECK. For every track we note whether the samples sit on the
  converter's 24-bit grid. On the grid = bit-perfect from the driver. Off the
  grid = something (the Windows mixer, its level slider, an 'enhancement')
  processed the audio before us. Reported after each take.

* Timing: every block's capture time is converted to `time.perf_counter()`
  (the same clock ssd_abhinaya's cameras use). A take's EPOCH (t=0) is set
  `countdown_secs` in the future; samples before it are discarded,
  sample-accurately.

* One mono file per TRACK (= a role name + one device channel).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import devices

log = logging.getLogger("ssd_dhvani.recorder")

try:
    import soundfile as sf   # type: ignore
    _HAVE_SF = True
except Exception:            # pragma: no cover
    sf = None                # type: ignore
    _HAVE_SF = False

#: Full scale of an int32 sample (what "0 dBFS" is for int32 capture).
_INT32_FS = float(2 ** 31)
#: A sample at or above this (0..1 of full scale) counts as clipped.
CLIP_LEVEL = 0.999
#: libsndfile command "rewrite the header now" (SFC_UPDATE_HEADER_NOW).
_SFC_UPDATE_HEADER_NOW = 0x1060
#: How often the writer makes the files crash-safe (seconds of audio).
HEADER_EVERY_SECS = 1.0


def to_db(level: float) -> float:
    """0..1 of full scale -> dBFS (0 = maximum, -inf = silence)."""
    return 20.0 * np.log10(level) if level > 0 else float("-inf")


def capture_is_float(hostapi: str, exclusive: bool) -> bool:
    """Does this driver path hand us FLOAT samples (a software mixer)?"""
    if hostapi == "Core Audio":
        return True
    if "WASAPI" in (hostapi or "") and not exclusive:
        return True
    return False


@dataclass
class TrackSpec:
    """What to record: which device channel, under which role, to where."""
    role: str
    channel: int        # 0-based
    path: Path


@dataclass
class TrackResult:
    role: str
    channel: int
    path: Path
    frames: int = 0
    peak: float = 0.0             # 0..1 of full scale
    clipped: int = 0              # samples at/over CLIP_LEVEL
    off_grid: int = 0             # samples NOT on the 24-bit grid
    checked: int = 0              # samples inspected for the grid test

    @property
    def peak_db(self) -> float:
        return to_db(self.peak)

    @property
    def bit_perfect(self) -> bool:
        """Every inspected sample was a whole 24-bit step (or silence)."""
        return self.checked > 0 and self.off_grid == 0


@dataclass
class TakeResult:
    tracks: list[TrackResult] = field(default_factory=list)
    samplerate: int = 48000
    subtype: str = "PCM_24"
    file_format: str = "WAV"
    capture_dtype: str = "int32"
    first_sample_time: float = 0.0     # perf_counter of the first KEPT sample
    epoch: float = 0.0
    xruns: int = 0                      # input overflows during the take
    error: str = ""

    @property
    def offset(self) -> float:
        """Seconds from take t=0 to the first sample (≈0; tiny jitter)."""
        if not self.first_sample_time:
            return 0.0
        return self.first_sample_time - self.epoch

    @property
    def duration(self) -> float:
        frames = max((t.frames for t in self.tracks), default=0)
        return frames / float(self.samplerate or 1)


def _on_24bit_grid(col: np.ndarray, dtype: str) -> int:
    """How many samples of `col` are NOT whole 24-bit steps."""
    if dtype == "int32":
        return int(np.count_nonzero(col & 0xFF))      # bits below the 24
    scaled = col.astype(np.float64) * float(2 ** 23)
    return int(np.count_nonzero(scaled != np.round(scaled)))


class _TakeWriter:
    """Consumes blocks from the queue and writes one file per track."""

    def __init__(self, specs: list[TrackSpec], samplerate: int, subtype: str,
                 file_format: str, epoch: float, dtype: str) -> None:
        self.specs = specs
        self.samplerate = samplerate
        self.subtype = subtype
        self.file_format = file_format
        self.epoch = epoch
        self.dtype = dtype
        self.q: "queue.Queue" = queue.Queue()
        self.result = TakeResult(
            tracks=[TrackResult(s.role, s.channel, s.path) for s in specs],
            samplerate=samplerate, subtype=subtype, file_format=file_format,
            capture_dtype=dtype, epoch=epoch)
        self._files: list = []
        self.xruns_at_stop = 0
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ssd_dhvani-writer")

    def open(self) -> None:
        try:
            for s in self.specs:
                s.path.parent.mkdir(parents=True, exist_ok=True)
                self._files.append(sf.SoundFile(
                    str(s.path), "w", samplerate=self.samplerate, channels=1,
                    subtype=self.subtype, format=self.file_format))
        except Exception:
            for f in self._files:          # don't leave half a take open
                try:
                    f.close()
                except Exception:
                    pass
            raise
        self._thread.start()

    def _make_crash_safe(self) -> None:
        """Rewrite each file's header so its length is correct right now."""
        for f in self._files:
            try:
                sf._snd.sf_command(f._file, _SFC_UPDATE_HEADER_NOW,
                                   sf._ffi.NULL, 0)
                f.flush()
            except Exception:
                pass     # older soundfile internals: files still close fine

    def _run(self) -> None:
        full_scale = _INT32_FS if self.dtype == "int32" else 1.0
        res = self.result
        since_header = 0
        header_every = int(self.samplerate * HEADER_EVERY_SECS)
        try:
            while True:
                item = self.q.get()
                if item is None:            # sentinel: recording is over
                    break
                block, t_first = item
                # Count-in: drop every sample captured before the epoch.
                if t_first < self.epoch:
                    skip = int(round((self.epoch - t_first) * self.samplerate))
                    if skip >= len(block):
                        continue
                    block = block[skip:]
                    t_first = t_first + skip / self.samplerate
                if not res.first_sample_time:
                    res.first_sample_time = t_first
                for f, spec, tr in zip(self._files, self.specs, res.tracks):
                    col = block[:, spec.channel]
                    f.write(col)
                    tr.frames += len(col)
                    lvl = np.abs(col.astype(np.float64)) / full_scale
                    if len(lvl):
                        tr.peak = max(tr.peak, float(lvl.max()))
                        tr.clipped += int((lvl >= CLIP_LEVEL).sum())
                    tr.off_grid += _on_24bit_grid(col, self.dtype)
                    tr.checked += len(col)
                since_header += len(block)
                if since_header >= header_every:
                    self._make_crash_safe()
                    since_header = 0
        except Exception as exc:          # disk full, share gone, ...
            res.error = f"{type(exc).__name__}: {exc}"
            log.exception("take writer failed")
            # Keep draining so the callback's queue can't grow forever.
            while self.q.get() is not None:
                pass
        finally:
            for f in self._files:
                try:
                    f.close()
                except Exception:
                    pass

    def finish(self) -> TakeResult:
        """Drain the queue and close the files. Blocking: call from a worker."""
        self.q.put(None)
        self._thread.join()
        self.result.xruns = self.xruns_at_stop
        return self.result


class AudioInput:
    """One open input stream: live meters always, a writer during takes.

    open() / close() MUST be called on the GUI thread (see module notes).
    """

    def __init__(self, device: "devices.InputDevice", samplerate: int = 48000,
                 subtype: str = "PCM_24", exclusive: bool = False,
                 latency: str = "high") -> None:
        self.device = device
        self.samplerate = int(samplerate)
        self.subtype = subtype
        # Exclusive only exists on Windows WASAPI; elsewhere it's ignored.
        self.exclusive = exclusive and devices.supports_exclusive(device.hostapi)
        # 'high' = bigger buffers = fewer dropouts. Latency doesn't matter
        # for recording (nothing is monitored through the app), stability does.
        self.latency = latency
        self.channels = max(1, min(device.channels, 16))
        self.error = ""
        self.exclusive_used = False
        self.dtype = "int32"
        self._stream = None
        self._lock = threading.Lock()
        self._writer: _TakeWriter | None = None
        self._peaks = np.zeros(self.channels)
        self._clip = np.zeros(self.channels, dtype=bool)
        self._xruns = 0
        self._last_block_t = 0.0
        self.callback_errors = 0
        self.last_callback_error = ""

    def _dtype_for(self, exclusive: bool) -> str:
        if self.subtype == "FLOAT":
            return "float32"
        return ("float32" if capture_is_float(self.device.hostapi, exclusive)
                else "int32")

    @property
    def path_label(self) -> str:
        """Plain-words description of the signal path, for the UI."""
        api = self.device.hostapi
        if "WASAPI" in api:
            return ("direct (exclusive)" if self.exclusive_used
                    else "Windows mixer")
        if "WDM-KS" in api or "ASIO" in api:
            return "direct (driver)"
        if api == "Core Audio":
            return "Core Audio"
        return "legacy (resampled)"

    # ---- stream --------------------------------------------------------- #
    def open(self) -> bool:
        """Open the stream (GUI thread). With exclusive mode requested but
        refused (another app holds the device, or the format doesn't match)
        it falls back to shared mode and says so in `self.error`."""
        if not devices.available():
            self.error = devices.why_unavailable()
            return False
        attempts = [True, False] if self.exclusive else [False]
        last = ""
        for excl in attempts:
            stream = None
            dtype = self._dtype_for(excl)
            try:
                stream = devices.sd.InputStream(
                    device=self.device.index, channels=self.channels,
                    samplerate=self.samplerate, dtype=dtype,
                    latency=self.latency, callback=self._callback,
                    dither_off=True,
                    extra_settings=devices._extra(excl, self.device.hostapi))
                self.dtype = dtype        # before start: the callback reads it
                stream.start()
                self._stream = stream
                self.exclusive_used = excl
                self.error = ("" if excl == self.exclusive else
                              f"exclusive mode refused ({last}) — using shared")
                return True
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
                if stream is not None:     # opened but failed to start:
                    try:                   # never leave the device half-held
                        stream.close()
                    except Exception:
                        pass
        self._stream = None
        self.error = last
        return False

    def close(self) -> None:
        """Close the stream (GUI thread). Stops a running take first."""
        if self._writer is not None:
            self.stop_take()
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    @property
    def is_live(self) -> bool:
        """Blocks arrived in the last second (device not frozen/unplugged)."""
        return self.is_open and (time.perf_counter() - self._last_block_t) < 1.0

    # ---- PortAudio thread ---------------------------------------------- #
    def _callback(self, indata, frames, time_info, status) -> None:
        # An exception escaping this function makes PortAudio ABORT the
        # stream: meters freeze and takes come out empty. So nothing may
        # raise here -- a failure is counted and reported, the audio flows on.
        try:
            self._handle_block(indata, frames, time_info, status)
        except Exception as exc:          # pragma: no cover - defensive
            self.callback_errors += 1
            self.last_callback_error = f"{type(exc).__name__}: {exc}"

    def _handle_block(self, indata, frames, time_info, status) -> None:
        now = time.perf_counter()
        if status and (getattr(status, "input_overflow", False)
                       or getattr(status, "input_underflow", False)):
            self._xruns += 1
        # When was the FIRST sample of this block captured, on our clock?
        # PortAudio says how long ago the ADC produced it; if the host API
        # doesn't report that (0), assume the block just finished.
        try:
            ago = float(time_info.currentTime - time_info.inputBufferAdcTime)
            if not (0.0 <= ago < 2.0):
                raise ValueError
        except Exception:
            ago = frames / float(self.samplerate)
        t_first = now - ago
        self._last_block_t = now
        # Hand the audio over FIRST (the take matters more than the meters),
        # under the lock: once detach_take() returns, no block can reach the
        # old writer, so a take ends exactly at Stop. queue.put is instant.
        with self._lock:
            w = self._writer
            if w is not None:
                w.q.put((indata.copy(), t_first))    # NEVER write to disk here
        fs = _INT32_FS if self.dtype == "int32" else 1.0
        # float first: abs() of the most negative int32 overflows.
        blk = np.abs(indata.astype(np.float32)).max(axis=0) / fs
        with self._lock:
            np.maximum(self._peaks, blk, out=self._peaks)
            self._clip |= blk >= CLIP_LEVEL

    # ---- meters -------------------------------------------------------- #
    def read_levels(self) -> tuple[np.ndarray, np.ndarray]:
        """(peak per channel 0..1, clipped flag per channel) since the last
        call. The UI calls this ~25x a second and draws the meters."""
        with self._lock:
            peaks, clip = self._peaks.copy(), self._clip.copy()
            self._peaks[:] = 0
            self._clip[:] = False
        return peaks, clip

    # ---- takes --------------------------------------------------------- #
    @property
    def recording(self) -> bool:
        return self._writer is not None

    def start_take(self, specs: list[TrackSpec], epoch: float,
                   file_format: str = "WAV") -> None:
        """Start writing. Raises if a file can't be created."""
        if not _HAVE_SF:
            raise RuntimeError("soundfile not installed (pip install soundfile)")
        if not self.is_open:
            raise RuntimeError("audio input is not open")
        for s in specs:
            if not 0 <= s.channel < self.channels:
                raise ValueError(f"{s.role}: channel {s.channel + 1} does not "
                                 f"exist on this device ({self.channels} ch)")
        if file_format == "FLAC" and self.subtype == "FLOAT":
            raise ValueError("FLAC cannot store 32-bit float — use WAV")
        w = _TakeWriter(specs, self.samplerate, self.subtype, file_format,
                        epoch, self.dtype)
        w.open()
        self._xruns = 0
        with self._lock:
            self._writer = w

    def detach_take(self) -> "_TakeWriter | None":
        """End the take NOW (instant, no I/O): no further block reaches the
        writer. Call `.finish()` on the result from a worker thread -- it
        drains the queue and closes the files, which can take a moment."""
        with self._lock:
            w, self._writer = self._writer, None
        if w is not None:
            w.xruns_at_stop = self._xruns
        return w

    def stop_take(self) -> TakeResult | None:
        """detach + finish in one call (blocking)."""
        w = self.detach_take()
        return w.finish() if w is not None else None

