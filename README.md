# FPV Sync for Premiere Pro

A Premiere Pro panel that lines up your **analog FPV DVR** recordings with your **HD camera** footage (GoPro, DJI Action, or any camera that records sound) and puts the DVR in the corner of the frame.

> **For now: Orqa analog DVR only.** It reads the Betaflight OSD burned into the DVR recordings of Orqa goggles (NTSC and PAL). Other goggles and digital systems (DJI, Walksnail, HDZero) aren't supported yet.

It finds:
- **on the DVR:** the frame where Betaflight's OSD shows **ARMED**;
- **on the HD camera:** the moment the props spin up in the audio.

It lines those two up to within a frame or two. It works out which DVR recording goes with which HD clip from the cameras' clocks, the file numbers, and the motion in both videos, so you can drop a whole trip in at once.

## What you need

- **Windows 10 or 11** with **Premiere Pro 2022 or newer**
- **Python 3.10+**: <https://www.python.org/downloads/> (tick **"Add python.exe to PATH"** in the installer)
- **ffmpeg**: easiest is to open a terminal and run `winget install Gyan.FFmpeg`
- Optional: an NVIDIA graphics card makes reading 4K footage faster. Without one it still works.

Footage it expects:
- **Orqa goggles' analog DVR recordings** (`ORQA0123.MP4` …), with the OSD burned in.
- The Betaflight **ARMED** message visible on the DVR when you arm.
- An HD camera that records sound, so the props can be heard.

## Install

1. **[Download the latest release](https://github.com/platy4/fpv-sync/releases/latest)**: under *Assets*, click **FPV-Sync-v….zip**. Unzip it anywhere.
2. Install Python and ffmpeg if you don't have them (see above).
3. Double-click **`install.bat`**. It:
   - copies the panel into Premiere's extensions folder;
   - lets Premiere load it (it isn't signed by Adobe, so this turns on the per-user "allow unsigned panels" setting);
   - finds Python and installs `numpy` if it's missing;
   - checks for ffmpeg.

   Read what it prints: anything marked **[!!]** tells you what's missing.
4. Restart Premiere Pro. In the **menu bar at the top of Premiere**, open **Window → Extensions → FPV Sync**. The panel opens as a small window; you can dock it next to your other panels like any Premiere panel.

**Updating:** download the newest release ZIP and run `install.bat` again. The analysis cache is kept, so footage it has already read stays fast.
**Removing:** run `uninstall.bat`.

## Use

Open the panel from Premiere's **top menu bar: Window → Extensions → FPV Sync**. It has three buttons:

**Match & sync selected**
1. Put your HD clips and DVR clips in a sequence. Their order and placement don't matter.
2. Select them (Ctrl+A for everything) and click the button.

Each DVR is matched to its flight, synced, and put in the bottom-right corner, right where your selection was. Everything is laid out in recording order, 30 s apart. Footage without a match stays in its place in time, so DVR-only or HD-only clips are easy to find.

Clips you didn't select (other cameras, B-roll) are never changed. If the synced flights need more room, the clips after them are pushed later.

**Sync selected**
For flights you've already paired by hand:
1. Drop each DVR roughly over its HD clip (within about 30 s).
2. Select them and click the button. Only those are synced.

**DVR in the Corner**
Scales the DVR, puts it bottom-right and crops the top. It works on the selected DVR clips, or on every DVR clip if none is selected.

The **results table** lists every flight. Rows marked **check** were matched in an unusual way (no ARMED, no prop sound, file order only), so it's worth a quick look.

The **first run on a trip** has to read the footage. Expect a few minutes for a day of flying on a hard drive. After that the analysis is cached and re-runs take seconds. Time estimates appear on the buttons once the panel has done one run on your PC.

## Troubleshooting

- **FPV Sync isn't under Window → Extensions** (Premiere's top menu bar): run `install.bat` again and restart Premiere.
- **"python" or "ffmpeg" errors:** install them (see [What you need](#what-you-need)), run `install.bat` again, and restart the PC so the new PATH is picked up.
- **A flight is off or unmatched:** very loud places (race events, wind) can hide the prop sound. Those flights stay unmatched or get a **check** mark; sync them by hand or with **Sync selected**.
- **Something else:** `%APPDATA%\Adobe\CEP\extensions\fpvsync\panel_log.txt` shows what the panel asked Premiere to do, and `timing_log.jsonl` next to it shows what each run did.

Windows only (it hasn't been tried on macOS).
