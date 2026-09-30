# ssd_dhvani

Audio recording, review and publishing for the AYON pipeline — the sound twin
of **ssd_abhinaya**. Same flow, same AYON rules, microphones instead of
cameras. Runs on **Windows and macOS**.

**Flow:** Sign in (or *Work Offline*) → **Project** → **Shot** → **Task** →
**RECORD** (live meters, 3 s count-in, takes) → **REVIEW** (waveforms, status,
comments) → **PUBLISH** → **PUBLISHED** (what's on AYON, from any machine).

## Run

```powershell
cd C:\pipeline\ssd_dhvani
python -m venv .venv
.venv\Scripts\activate            # macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

Build: `build.bat` (Windows → `dist\ssd_dhvani_v1.exe`) or `./build.sh`
(macOS → `dist/ssd_dhvani_v1.app`). Build each on its own OS.

Probe your devices from a terminal: `python -m ssd_dhvani.devices`.

## Recording settings (Record tab, top row)

| Setting | Default | What it means |
|---|---|---|
| Driver | WASAPI (Win) / Core Audio (Mac) | How the app talks to the device. WDM-KS = straight to the driver. MME/DirectSound are marked *(resamples)* — avoid. |
| Input | system default | Remembered **by name**, never by number. |
| Rate | the device's own (48 kHz) | Only rates the device really accepts are listed. |
| Depth | 24-bit | 24-bit = master standard. 32-bit float = extra headroom. 16-bit = only if a spec asks. |
| File | WAV | FLAC = lossless, ~½ size. RF64 = WAV beyond 4 GB. |
| Exclusive | off | WASAPI only: bypass the Windows mixer; falls back to shared if refused. |

**Tracks:** one mono file per track = a *role* (`mic_main`, `lav`, …) + one
input channel. `＋ Add track` for a second transmitter. Roles are unique
(picking a used role swaps the two).

**Levels:** aim for speech peaks between −12 and −6 dBFS (yellow on the
meter). The CLIP light latches — click the meter to reset. Set gain at the
mic/receiver, not afterwards.

## Facial recording (drop zone under the tracks)

Drop the facial video — or a **.zip** with it (e.g. a Live Link Face export)
— onto the big **FACIAL RECORDING** area, or click it to browse.

* It attaches to the **selected take** (record the audio, then drop that
  performance's video). A published take, no take, or **"as a new take"**
  ticked → a new, video-only take. Video-only takes publish normally.
* The original is copied untouched into `Take_NN/facial/`. From a zip,
  everything is kept (CSV/JSON data too); the largest video is the main one.
* **Auto-sync:** if the take has a mic track and the video has sound, the
  mic is lined up with the video automatically (the log shows the offset).
* On publish the take gets a `review` mp4 (H.264, ≤1920 wide) whose sound is
  the **clean mic, in sync** — it is also the AYON reviewable.

## Files

```
{save_root}/{Project}/{Seq}/{Scene}/{Shot}/{Task}/
    manifest.json
    Take_03/6010_chrn_1010_mic_main_take3.wav
    Take_03/facial/MySlate_3_iPhone.mov (+ anything else from the zip)
    Take_03/6010_chrn_1010_review_take3.mp4   (made at publish)
```

Default save root: `~/Music/ssd_dhvani` (change it in the top bar).

## Publish

One AYON version per take. The product type follows the take
(`publish_product_type: "auto"` in `config.json`): audio-only takes →
`audio` in `<shot>_<task>`; takes with a facial video → `render` in
`<shot>_<task>_facial` (loads as a clip in DaVinci). Set a type such as
`"audio"` to force it for every take. Representations: one per track
(named by role), `facial` (the original video), `review` (synced mp4),
`facial_<ext>` (other zip files) and `ssd_take` (sidecar JSON). The
reviewable is the review mp4 — or, without a video, a waveform video with
sound. ffmpeg is needed for the video parts; recording never needs it.

See `HANDOFF.md` for the engineering details.
