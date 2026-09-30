# ssd_dhvani — Engineering Handoff

**Repo:** `C:\pipeline\ssd_dhvani` · **Version:** `1.3.0` (`ssd_dhvani_v1`)
**Origin:** forked from ssd_abhinaya 1.0.0 on 2026-09-30; the camera
pipeline was replaced by an audio pipeline. Everything AYON-, shot-, take-,
offline- and publish-related is abhinaya's proven code — its hard rules
(abhinaya `HANDOFF.md` §2) still apply and are not repeated here unless
they changed.

**Status:** built and verified in the sandbox (compile, lint, the real Qt
window offscreen with a simulated device, publish against a fake AYON, a
PyInstaller build). **Needs an operator pass on real hardware** (Windows rig
with the Wireless Mic Rx, and a Mac) — see §8.

---

## 1. First five minutes

```powershell
cd C:\pipeline\ssd_dhvani
.venv\Scripts\activate
python -m pip install -r requirements.txt   # PySide6, ayon-python-api,
                                            # sounddevice, soundfile, numpy
python main.py
python -m ssd_dhvani.devices                # what each device accepts
build.bat            # Windows exe      |   ./build.sh   # macOS .app
```

Logs: `<save root>\.ssd_dhvani\` — `crash.log`, `fault.log`, `session.log`.

## 2. Architecture

```
main.py                   entry: DPI, crash hooks, ffmpeg, product type, Login -> Main
ssd_dhvani/
  devices.py      NEW   enumerate/find devices by NAME + host API, supported rates
  recorder.py     NEW   AudioInput (always-open stream, meters) + _TakeWriter
  waveform.py     NEW   min/max peaks for review tiles (blockwise, cached)
  widgets.py      NEW*  RecordButton, LevelMeter, TrackStrip, WaveformView, ReviewTile
  main_window.py  EDIT  Record tab rebuilt for audio; review/published/offline kept
  manifest.py     EDIT  TakeData.tracks {role: TrackEntry}; audio format fields
  take_manager.py EDIT  flat layout Take_NN/{shot}_{role}_takeN.wav, reconcile
  publish.py      EDIT  audio product, role representations, waveform reviewable
  config.py       EDIT  audio settings; ~/Music default; macOS .app config path
  tools.py        EDIT  per-OS ffmpeg fallbacks (Homebrew on Mac), no console flash
  ayon_client.py, offline_cache.py   small EDITs: audio extensions, 'tracks'
  ayon_lock.py, shotdb.py, login_window.py, crashlog.py, theme.py  unchanged
removed: camera_source.py, overview.py, audio.py, vendor/capture_footage_v2.py
```

## 3. Hard rules for the audio path

- **PortAudio control calls (open/start/stop/close) only on the GUI thread.**
  1.0.0 opened the stream on a QThread and closed it on the GUI thread; on
  Windows (WASAPI objects are thread-bound, PortAudio isn't thread-safe)
  that corrupted memory and the app died later with an access violation in
  `fault.log` ("crash on Publish"). Opening takes milliseconds.
- **Every QThread is held in `self._threads` until it has finished**
  (`_keep()`). Dropping the last reference to a running QThread kills the
  process natively.
- **Never `setParent(None)` a widget you then `deleteLater()`** (Python and Qt
  both own it -> double free). Strips are removed with `_dispose_strip()`.
  Widget slots are bound methods, not lambdas capturing `self`.
- **The audio callback cannot raise.** An exception there makes PortAudio
  abort the stream (frozen meters, empty takes). `_callback` wraps
  `_handle_block`; failures are counted and logged, audio keeps flowing.
- **Capture in the driver's own format -- no conversion, no dither.**
  WASAPI shared and Core Audio mix in float -> capture `float32`; WDM-KS,
  WASAPI exclusive, ASIO, MME -> `int32`. `dither_off=True`. The file depth is
  the user's choice (24-bit default; float->24-bit requantisation sits at
  -144 dBFS, far under any mic's noise).
- **Crash-safe WAVs:** the writer rewrites each file's header every second
  (libsndfile `SFC_UPDATE_HEADER_NOW`); a crash loses at most ~1 s.
- **Signal-path check:** each track records whether its samples sit on the
  24-bit grid. The operator's first real takes (WASAPI shared) did NOT --
  the Windows mixer processed them (its Levels slider was worth +6 dB vs
  WDM-KS on this receiver). The Record tab shows the path badge; the log says
  "bit-perfect" or "processed by the OS audio engine" per track.
- **A stream that opens but fails to start is closed** (no half-held device).
- **macOS:** `CoreAudioSettings(change_device_parameters=True,
  conversion_quality="max")` -- switch the device's rate instead of
  resampling.

- **Never store a device index.** Indices change between Python installs and
  after re-plugging (seen on day one: the receiver moved from 4/10 to 11/18).
  Config stores `audio_device` (name) + `host_api`; `devices.find_device()`
  resolves it every time.
- **The PortAudio callback never touches the disk.** It copies the block into
  a queue; `_TakeWriter` (a normal thread) writes. Disk waits in the callback =
  dropouts = clicks.
- **16/24-bit takes capture `int32`, float takes `float32`.** libsndfile writes
  the top bits of int32 straight into a 24-bit WAV: verified **bit-exact**
  against a real receiver recording. Don't switch 24-bit capture to float.
- **One stream, always open.** Meters work before recording (that is how gain
  gets set). Record = attach a writer; Stop = detach (instant, on the UI
  thread) + `finish()` in `TakeFinalizeWorker`. The UI never blocks on stop.
- **Settings are locked while a take records or saves** (`_busy_recording`).
  Changing rate/device mid-take would cut it.
- **Count-in discards samples before the epoch sample-accurately**, using
  PortAudio's ADC time mapped onto `perf_counter()` (the clock abhinaya's
  cameras use). `TrackEntry.offset` ≈ 0 and exists for future A/V alignment.
- **One mono file per track.** A receiver exposing 4 channels with 1 live
  channel records 1 file, not 3 files of silence.
- **Exclusive mode only exists on WASAPI** (`devices.supports_exclusive`); if
  refused, the stream falls back to shared and says so.
- **FLAC can't hold float** — the UI switches such a take to WAV.
- **`latency="high"`**: bigger buffers, fewer dropouts. Recording doesn't
  need low latency (nothing is monitored through the app).
- **A 0-sample file (stopped during the count-in) is deleted**, and the take
  is auto-marked Reject — same rule as abhinaya's empty takes.

## 3b. Facial recordings (1.2.0)

Files: `facial.py` (import), `media.py` (ffprobe/ffmpeg), `sync.py`
(auto-sync), `widgets.DropZone`, `main_window` "Facial recording" section,
`publish.build_facial_review` / `repre_name`.

- **Target take:** the SELECTED take; a new video-only take if none, if the
  selected one is published (locked), or "as a new take" is ticked.
  `_refresh_takes` KEEPS the selection now (it used to jump to the last take,
  which would have put a video on the wrong take).
- **Import** runs in `FacialImportWorker` (kept alive via `_keep`). It writes
  into `facial.importing/` and swaps it in only on success, so a failed or
  cancelled REPLACE never loses the old video. Zip: junk skipped, zip-slip
  entries refused, a single top folder flattened; largest video = main.
- **Never re-encoded:** the published `facial` representation is the
  camera's file. Only the `review` mp4 is encoded (at publish).
- **Auto-sync** = FFT cross-correlation of the video's own audio vs the
  mic, both at 8 kHz mono via ffmpeg. `offset` = mic start in seconds after
  the video start. Confidence = best match / runner-up (outside ±100 ms);
  trusted at ≥ 2.5 (60 unrelated pairs max 1.5; real matches 4–37). Verified:
  exact to 0.01 ms on known offsets; the review mp4's audio lines up with the
  picture at 0.0000 s.
- **Review audio rules:** synced → mic shifted by the offset; video silent
  but mic present → mic from t=0 (logged as a guess); else the video's own
  sound; never `-shortest` (the video defines the length).
- **Publish:** frame range / fps / resolution come from the VIDEO when there
  is one (real frames), else audio duration at shot fps. Representation
  names are stable across versions: `facial`, `review`, `facial_<ext>`,
  track roles, `ssd_take`. Hidden files (`.thumb.jpg`) and `*.importing`
  are never published.
- Take rows are removed with hide + `deleteLater` (never `setParent(None)`
  -- the row being clicked was deleted inside its own click handler).

## 3c. Product type (1.3.0)

`config.publish_product_type` = `"auto"` (default): `publish.product_for()`
picks per take -- audio-only -> `audio` in `<folder>_<task>`; facial video ->
`render` in `<folder>_<task>_facial`. The type lives on the PRODUCT, so each
type has its own product (own version numbers). An existing product is only
converted from `generic`; a real type (audio<->render) is never flipped --
the publish logs it and keeps the existing type. Any other config value
forces that type and the plain product name (pre-1.3 behaviour).

## 4. Takes

- Layout: `{save_root}/{Project}/{Seq}/{Scene}/{Shot}/{Task}/Take_NN/{shot}_{role}_take{N}.wav`
  + `manifest.json` per task (written atomically: temp file + rename).
- Take fields: `samplerate`, `sample_format` (PCM_16/PCM_24/FLOAT),
  `file_format` (WAV/FLAC/RF64), `device`, `host_api`, `duration`,
  `tracks{role: {path, channel, offset, duration, peak_db, clipped}}`.
- After every take the log reports per track: size, duration, peak, and
  warnings for clipping, near-clipping, very quiet, and **digital silence**
  (on macOS with the microphone-permission hint).
- Default task on shot pick: first of `audio`, `sound`, `voice`,
  `dialogue`, `recording_package` that the shot has, else its first task.

## 5. Publish

- Product type from `config.publish_product_type` (default **`audio`**, the
  ayon-core type audio loaders list). If a loader (e.g. DaVinci) doesn't show
  the takes, set it to `render` — publish then converts the existing product
  (same patch logic as abhinaya).
- Representations: one per track, named by role (`mic_main`, `lav`) +
  `ssd_take`. Rootless paths, `template`, op3 hashes, `data.context` — all
  unchanged from abhinaya's integrator-style port.
- Version attribs: `families [product_type, review]`, `fps` = shot fps,
  `frameStart/End` = the **duration** at the shot fps from the shot's
  frameStart (1001), so the clip is the right length on a timeline.
- Reviewable: `make_review_proxy()` renders the main track (`mic_main`, else
  the first) as a 1280×360 H.264 `showwaves` video + AAC. No ffmpeg → masters
  still publish, no reviewable, and the log says so.
- Sidecar `ssd_take.json` carries status, comments, format, device, tracks.
  The PUBLISHED tab reads it back (column "Tracks").

## 6. macOS

- Build on a Mac: `./build.sh` → `dist/ssd_dhvani_v1.app` (+ `dist/config.json`
  next to it; `config.py` resolves the config beside the `.app`).
- `Info.plist` gets `NSMicrophoneUsageDescription` (spec `BUNDLE`). Without
  it macOS delivers zeros. First launch: right-click → Open (not notarised),
  then allow the microphone. Running from source, Terminal asks instead.
- Finder-launched apps don't get the shell PATH: `tools.py` also checks
  `/opt/homebrew/bin` and `/usr/local/bin` for ffmpeg.
- AYON roots: `get_project_roots_by_site_id` returns the **darwin** root
  values from the project's anatomy. If publish says "Publish root is empty",
  the project needs its macOS root filled in on the AYON server.

## 7. Config (`config.json`, git-ignored)

| Key | Default | Notes |
|---|---|---|
| `host_api`, `audio_device` | "" | "" = best API for the OS / system default input |
| `samplerate` | 48000 | falls back to the device's own rate if unsupported |
| `sample_format` | PCM_24 | PCM_16 · PCM_24 · FLOAT |
| `file_format` | WAV | WAV · FLAC · RF64 |
| `wasapi_exclusive` | false | Windows WASAPI only |
| `latency` | high | PortAudio latency hint |
| `audio_tracks` | `[{"role":"mic_main","channel":0}]` | one file per entry |
| `countdown_secs` | 3 | 0 disables |
| `fps_display` | 24 | timecode display rate |
| `publish_product_type` | audio | see §5 |
| `save_root` | `~/Music/ssd_dhvani` | env `SSD_SAVE_ROOT` overrides |
| `ffmpeg_path` | "" | only for the reviewable |
| `export_dir` | "" | Review tab → right-click → Export copy |

## 8. Verified here vs. still to check on hardware

Verified (sandbox): real receiver recording replayed through `AudioInput` →
bit-exact 24-bit output; the full Qt window with a simulated Windows device
set (record, count-in abort → Reject, settings lock, role swap, 2 tracks,
FLAC, float, WDM-KS 4-ch switch, reconcile, clean close); publish through a
fake AYON (product, representations, attribs, sidecar, reviewable, lock);
PyInstaller build starts.

**Operator checklist:**
1. Windows, WASAPI, Wireless Mic Rx: meters move, a 30 s take plays back,
   log shows 0 overflows.
2. Same on WDM-KS (expect ~6 dB hotter than WASAPI on this receiver).
3. Exclusive on WASAPI: works, or falls back with a log line.
4. Publish one take to `Training`: loaders in DaVinci/Maya list it with
   product type `audio`; AYON web viewer plays the waveform video.
5. Mac: source run + `.app` build; microphone prompt appears; a take has
   sound (not "DIGITAL SILENCE").
6. `build.bat` exe records (PortAudio/libsndfile collected).

## 9. Open items

1. AYON loaders vs product type `audio` — confirm on the real server (§5).
2. No in-app monitoring/playback by design (OS default player, like abhinaya).
3. Syncing with abhinaya takes (same `perf_counter` clock, offsets stored) is
   possible later; not wired.
4. Password is base64 in config (same as abhinaya; keyring would fix it).
