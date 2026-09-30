# ssd_dhvani — handoff for a new Claude session on the Mac

Paste this whole file into the new chat. The deep engineering reference is
`HANDOFF.md` in the same repo — read §3 (hard rules), §3b (facial), §3c
(product type) and §6 (macOS) before changing code.

## 1. What this is

**ssd_dhvani** (v1.3.2) = the audio twin of ssd_abhinaya: a PySide6 desktop
app for a VFX/animation studio. Sign in to AYON (or work offline) → pick
Project / Shot / Task → **record** mic takes (live meters, count-in) and/or
**drop a facial-recording video or .zip** onto a take → **review**
(waveforms, status Reject/View/Okay/Approve, comments) → **publish** to
AYON (loadable in DaVinci/Maya/Blender) → **Published** tab.

- Repo: **https://github.com/sidmehraajm/ssd_dhvani** (public, branch `main`)
- Master copy + development: Windows PC `C:\pipeline\ssd_dhvani` (the rig).
  Code changes are normally made there and pushed; the Mac `git pull`s.
  If you change code on the Mac, commit + push from the Mac and tell the
  user to `git pull` on Windows — never let the two drift.
- Runs on Windows and macOS. Python 3.12/3.13, PySide6, sounddevice
  (PortAudio), soundfile (libsndfile), numpy, ayon-python-api. ffmpeg/ffprobe
  only for video/review work — recording never needs them.

## 2. The user

- Sid (KLEEM RND), pipeline/rigging TD. Not an audio engineer.
- **Teach while working**: short 1–2 line explanations of what/why; prefers
  steps they run themselves with a simple explanation over Claude silently
  running everything.
- Tool names always `ssd_` prefix. UI **titles** say "KRISHNA" instead of
  AYON; buttons/messages/logs say "AYON"; never rename technical identifiers
  (`ayon_api`, env vars, hostnames). AYON server: `http://ayon:5000`.
- Git: don't suggest commit/push after every change; they ask for it.

## 3. This Mac

| | |
|---|---|
| Machine | MacBook Pro, **Intel x86_64**, **macOS 15.7.9** (Sequoia), user `audioteam` |
| Network | 192.168.0.245, router 192.168.0.1 |
| Homebrew | **NOT available** (Homebrew no longer supports Intel) — never suggest `brew` |
| git | Xcode Command Line Tools (installed) |
| Python | python.org **3.13** universal2 installer (`python3.13`) |
| ffmpeg | static evermeet.cx build in `~/pipeline/ffmpeg/{ffmpeg,ffprobe}` — the app finds it there automatically (v1.3.2) |
| Repo | `~/pipeline/ssd_dhvani`, venv `.venv` |
| Save root | `~/Music/ssd_dhvani` (logs: `~/Music/ssd_dhvani/.ssd_dhvani/{crash,fault,session}.log`) |
| AYON darwin roots | `work` + `publish` = **`/Volumes/projects`**, `render` = `/mnt/share/projects` |

Setup commands (for reference / if redoing):
```bash
cd ~/pipeline && git clone https://github.com/sidmehraajm/ssd_dhvani.git
cd ssd_dhvani && python3.13 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip && python -m pip install -r requirements.txt
mkdir -p ~/pipeline/ffmpeg && cd ~/pipeline/ffmpeg
curl -L -o ffmpeg.zip https://evermeet.cx/ffmpeg/getrelease/zip
curl -L -o ffprobe.zip https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip
unzip -o ffmpeg.zip && unzip -o ffprobe.zip && rm *.zip
chmod +x ffmpeg ffprobe && xattr -d com.apple.quarantine ffmpeg ffprobe
cd ~/pipeline/ssd_dhvani && source .venv/bin/activate && python main.py
```
Microphone permission: the first run asks for **Terminal** (System Settings →
Privacy & Security → Microphone). The built `.app` (`bash build.sh`) asks for
itself (Info.plist `NSMicrophoneUsageDescription` is set in the spec).

## 4. Where things stand (2026-09-30, end of day)

Working on the Mac: sign-in, shot cache sync (16,201 shots / 101 projects),
project/shot/task pickers, facial import.

Last Mac session log (before v1.3.2 was pulled):
- Task `SQ_deathlunchseq / … / pipe_test_shot_1010 / facialrecording`:
  `LiveLinkFace_20260930_MySlate_81_iPhone 2.zip` → **Take_01** (video-only,
  8 files, main `MySlate_81_iPhone.mov`). ffmpeg was missing, so its details
  are unknown (`probed=false`). v1.3.2 re-reads it automatically when the
  task is opened / ffmpeg is set / at publish — **no re-import needed**.
- Publish **refused on purpose**: `Publish root '/Volumes/projects' is not
  reachable` — the studio share wasn't mounted. (This guard prevents creating
  a local `/Volumes/projects` folder and "publishing" to the Mac's own disk.)

### Immediate next steps
1. `git pull` (get v1.3.2) — check `ssd_dhvani/__init__.py` says `1.3.2`.
2. Install ffmpeg into `~/pipeline/ffmpeg` (commands above) if not done.
3. Mount the share: Finder → Go → Connect to Server → `smb://<server>/projects`,
   then `ls /Volumes`.
   - shows `projects` → publish again.
   - other name (e.g. `projects-1`) → in `~/pipeline/ssd_dhvani/config.json` add
     `"root_overrides": {"publish": "/Volumes/projects-1"}` and restart the app.
   - unknown server address → on a Windows PC `net use` shows what `P:` maps to.
4. Publish Take_01. Expected log: `video details read now — …`,
   `review video ready`, product `pipe_test_shot_1010_facialrecording_facial`
   (type `render`), reviewable uploaded.
5. Then test recording on the Mac: `python -m ssd_dhvani.devices` (Core Audio
   device list) → record a take with the receiver → check the log line per
   track (peak, "bit-perfect"/"processed", no overflows).

## 5. How the app works (short)

- **Recording** (`recorder.py`, `devices.py`): ONE PortAudio input stream
  stays open (meters live before recording). Record attaches a writer thread;
  the callback only queues blocks. One mono file per **track** (role +
  device channel). Devices stored by **name + host API**, never index.
  macOS uses Core Audio (float capture; device switched to the chosen rate,
  `CoreAudioSettings(change_device_parameters=True)`).
- **Takes**: `{save_root}/{Project}/{Seq}/{Scene}/{Shot}/{Task}/Take_NN/…` +
  `manifest.json`. New record pass = new take. Published takes are locked.
- **Facial** (`facial.py`, `media.py`, `sync.py`): drop zone under the tracks;
  attaches to the SELECTED take (or a new video-only take). Original copied
  untouched into `Take_NN/facial/`; zips unpacked (junk/zip-slip skipped),
  largest video = main. Auto-sync = FFT cross-correlation of the video's own
  audio vs `mic_main` (trusted at confidence ≥ 2.5). At publish a browser-safe
  `review` mp4 is built with the clean mic in sync.
- **Publish** (`publish.py`): one AYON version per take. Product type
  `auto`: audio-only → `audio` in `<shot>_<task>`; with video → `render` in
  `<shot>_<task>_facial`. Representations: track roles (`mic_main`…),
  `facial`, `review`, `facial_<ext>`, `ssd_take` (sidecar JSON). Rootless
  `{root[publish]}/…` paths + `data.context` (DaVinci loaders need these).
- **Mac paths**: roots come from AYON's darwin anatomy (no AYON launcher on
  this Mac); `config.json` `root_overrides` replaces a root for THIS machine
  only. Reading Windows-published paths rebuilds them at every
  `/{project}/` against the local roots.

## 6. Rules that were learned the hard way — don't undo

- PortAudio open/start/stop/close **only on the GUI thread** (cross-thread use
  corrupted memory → native crash on Windows, "crash on Publish").
- Keep every QThread referenced until finished (`MainWindow._keep`).
- Never `setParent(None)` a widget and then `deleteLater()` it — use
  hide + `deleteLater` (double free / use-after-free inside click handlers).
- The audio callback must never raise and never touch the disk.
- 16/24-bit takes capture `int32` from integer drivers (bit-exact), float
  from Core Audio / WASAPI shared; `dither_off=True`.
- WAV headers are rewritten every second (crash-safe files).
- Publish never creates a missing root folder; never flips an existing
  product between `audio` and `render`.
- `_refresh_takes` keeps the user's selected take (drops target it).
- Native crashes print nothing: read `fault.log` in the save root.

## 7. Testing without hardware

There are no automated tests in the repo; the Windows sessions used
throwaway scripts with a fake `sounddevice` module and a fake AYON
(`types.SimpleNamespace` wrapped in `ayon_lock.LockedAPI`, assigned to
`publish.ayon_api` / `ayon_client.ayon_api`, plus a fake `EntityHub`).
Offscreen Qt works with `QT_QPA_PLATFORM=offscreen`. On the Mac you have the
real thing — prefer testing with the actual receiver and share.

## 8. Not yet verified on real hardware

- Mac: Core Audio recording with the Wireless Mic Rx; the `.app` build
  (`bash build.sh`, first open = right-click → Open); publishing to the real
  share; DaVinci loading `render`/`audio` products published from the Mac.
- AYON web viewer playing the review mp4 from a real publish.
- Whether Maya/Blender audio loaders list `audio` products (config can force
  another type: `"publish_product_type": "render"`).
