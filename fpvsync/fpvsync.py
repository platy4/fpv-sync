r"""
fpvsync - sync analog DVR clips to HD camera footage (Action 2, GoPro, ...) in Premiere Pro.

Workflow
  1. In Premiere, put each DVR clip roughly over (or under) its HD clip.
  2. Select the sequence > File > Export > Final Cut Pro XML.
  3. Run:  python fpvsync.py "path\to\sequence.xml"      (or drag the XML onto sync.bat)
  4. Import the "<name>_synced.xml" it writes (File > Import). A new sequence
     "<name> - synced" appears with every DVR clip moved into sync.

Rule: the first DVR frame showing "ARMED" is placed one sequence frame before
the frame in which the HD footage's prop sound starts.  HD clips never move.
"""
import argparse, json, math, os, re, subprocess, sys, threading, time, uuid
import urllib.parse
import xml.etree.ElementTree as ET
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "armed_template.npy")            # ARMED as an NTSC camera draws it
TEMPLATE_PAL = os.path.join(HERE, "armed_template_pal.npy")    # ... and a PAL one (shorter letters)
DISARMED_S = os.path.join(HERE, "disarmed_s.npy")    # the "S" of DISARMED, the letter right before its "ARMED"
CACHE = os.path.join(HERE, "detect_cache.json")
PROPS = "props4"                 # cache key; bump when the prop detector changes
ARMED = "armed6"                 # cache key; bump when the ARMED detector changes (6: DISARMED is not ARMED, box 48 px wider left)
TICKS = 254016000000            # Premiere ticks per second

# ARMED template: where it was cut from a 1280x960 DVR frame (full-res px)
TX, TY, TW, TH = 555, 432, 220, 64
# search box: ARMED has shown up in two places in the DVR picture so far (~(555,432) on the
# Nov 2026 trips, ~(512,503) on the Aug 2026 one). "auto" covers both, and falls back to the
# whole middle of the frame ("wide") for a DVR where it finds no ARMED at all. The box reaches one
# letter left of ARMED, so DISARMED (same spot, "DIS" in front) can be told apart.
OSD_BOXES = {"auto": (440, 408, 360, 184), "wide": (360, 300, 560, 360)}
OSD_BOXES["narrow"] = OSD_BOXES["auto"]          # older configs said "narrow"
OSD_MODE = "auto"
SX, SY, SW, SH = OSD_BOXES["auto"]


def set_osd_search(mode):
    global SX, SY, SW, SH, OSD_MODE
    OSD_MODE = mode if mode in OSD_BOXES else "auto"
    SX, SY, SW, SH = OSD_BOXES[OSD_MODE]
ARM_THRESHOLD = 0.55            # NCC score that counts as "ARMED visible"
DISARMED_S_MIN = 0.4            # NCC of the letter before ARMED with an "S": DISARMED (it scores 0.75-0.95, ARMED <= 0.25)
ARM_MIN_RUN = 8                 # consecutive frames needed (ARMED shows ~35)
DVR_NAME = "orqa"               # DVR recordings: file names with "orqa" anywhere (ORQA0123.MP4, day1_orqa_12.46.mp4)
ARMED_STEP = 6                  # coarse scan: every 6th frame; a real ARMED can be as short as 7 frames


# ----------------------------------------------------------------- utilities
def run(cmd):
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def run_skippable(cmd, path):
    """run(), but a Skip click on `path` while it reads (when _abort is on) kills ffmpeg: SkipFile."""
    if not getattr(_abort, "on", False):
        return run(cmd)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    parts = []
    try:
        while True:
            buf = p.stdout.read(1 << 20)
            if not buf:
                break
            parts.append(buf)
            if is_skipped(path):
                raise SkipFile(path)
    finally:
        p.stdout.close()
        p.kill()
        p.wait()
    return b"".join(parts)


STOP_FILE = None          # set per panel job; the panel creates it when you click Stop
UNREAD = set()            # recordings a stopped run never got to (first file of each)


def stop_requested():
    """Stop clicked: finish the file in hand, read no more footage, place what's known."""
    return bool(STOP_FILE) and os.path.exists(STOP_FILE)


SKIP_FILE = None          # set per panel job: the files you clicked Skip on (JSON list of paths)
SKIPPED_SEEN = set()      # files this run left out because they were skipped (processed if un-skipped)
_skip_state = [None, frozenset()]
_abort = threading.local()            # on while a file is scanned or listened to: a Skip click stops it right away


class SkipFile(Exception):
    pass


def is_skipped(path):
    """Is this file skipped right now? (the panel rewrites skip.json on every click; re-read on change)"""
    if not SKIP_FILE or not path:
        return False
    try:
        st = os.stat(SKIP_FILE)
    except OSError:
        return False
    m = (st.st_mtime_ns, st.st_size)
    if m != _skip_state[0]:
        try:
            _skip_state[1] = frozenset(json.load(open(SKIP_FILE, encoding="utf-8")))
            _skip_state[0] = m              # only once read whole: a half-written file is read again
        except Exception:
            pass
    return path in _skip_state[1]


def skip_now(path):
    """Skipped right now: noted (so an un-skip processes it again) and left alone by this step."""
    if is_skipped(path):
        SKIPPED_SEEN.add(path)
        return True
    return False


DVR_MIC = set()           # DVR files whose audio is kept (goggles with a mic): their sound helps matching
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".psd", ".bmp", ".gif", ".webp", ".exr", ".dpx")


def ensure_ffmpeg():
    """Make sure ffmpeg and ffprobe can be started. Premiere hands its panels the PATH it had when it
    started, so an ffmpeg installed since then isn't on it: look in the PATH Windows has now and in the
    usual install folders, and add the folder found. Raises a plain-language error if there's none."""
    import shutil, glob
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    dirs = []
    if os.name == "nt":
        try:
            import winreg
            for root, sub in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                              (winreg.HKEY_LOCAL_MACHINE,
                               r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
                try:
                    with winreg.OpenKey(root, sub) as k:
                        dirs += os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]).split(";")
                except OSError:
                    pass
        except ImportError:
            pass
        la, up = os.environ.get("LOCALAPPDATA", ""), os.environ.get("USERPROFILE", "")
        dirs.append(os.path.join(la, r"Microsoft\WinGet\Links"))                       # winget
        dirs += glob.glob(os.path.join(la, r"Microsoft\WinGet\Packages\*FFmpeg*\*\bin"))
        dirs += [r"C:\ffmpeg\bin", r"C:\ProgramData\chocolatey\bin", os.path.join(up, r"scoop\shims")]
        dirs += glob.glob(r"C:\Program Files*\ffmpeg*\bin")
    for d in dirs:
        d = d.strip().strip('"')
        exe = ".exe" if os.name == "nt" else ""
        if d and os.path.isfile(os.path.join(d, "ffprobe" + exe)) and os.path.isfile(os.path.join(d, "ffmpeg" + exe)):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return
    raise RuntimeError("ffmpeg isn't installed, or Windows can't find it. Open a terminal (cmd), run "
                       "'winget install Gyan.FFmpeg', then restart the PC and try again.")


def probe_fps(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=r_frame_rate,avg_frame_rate", "-of", "json", path])
    s = json.loads(out)["streams"][0]
    num, den = map(int, s["r_frame_rate"].split("/"))
    anum, aden = map(int, s["avg_frame_rate"].split("/"))
    fps = num / den
    cfr = aden != 0 and abs(anum / aden - fps) < 0.01
    return fps, cfr


def highpass(f, k=7):
    """signed local contrast: pixel minus k x k box mean (2-D, float32)."""
    p = np.pad(f, k // 2, mode="edge")
    c = np.pad(np.cumsum(np.cumsum(p, 0), 1), ((1, 0), (1, 0)))
    box = (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)
    return f - box


def half(f):
    h, w = f.shape
    return f[: h // 2 * 2, : w // 2 * 2].reshape(h // 2, 2, w // 2, 2).mean((1, 3))


# ------------------------------------------------------------ ARMED detector
def dvr_frames(path, start=0.0, dur=None, step=1, box=None):
    """yield (index, gray search-box frame) from a DVR file, scaled to 1280x960."""
    bx, by, bw, bh = box or (SX, SY, SW, SH)
    cmd = ["ffmpeg", "-v", "error"]
    if start:
        cmd += ["-ss", f"{start:.4f}"]
    cmd += ["-i", path]
    if dur:
        cmd += ["-t", f"{dur:.4f}"]
    sel = rf"select='not(mod(n\,{step}))'," if step > 1 else ""
    cmd += ["-map", "0:v:0", "-vf", f"{sel}scale=1280:960,crop={bw}:{bh}:{bx}:{by},format=gray",
            "-fps_mode", "passthrough", "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n, size = 0, bw * bh
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield n, np.frombuffer(buf, np.uint8).reshape(bh, bw).astype(np.float32)
            n += 1
    finally:
        p.stdout.close()
        p.kill()
        p.wait()


def make_template(dvr_path, frame_index):
    fps, _ = probe_fps(dvr_path)
    for _, f in dvr_frames(dvr_path, start=frame_index / fps, dur=1.5 / fps):
        oy, ox = (TY - SY) // 2, (TX - SX) // 2
        t = half(highpass(f))[oy: oy + TH // 2, ox: ox + TW // 2]
        np.save(TEMPLATE, t.astype(np.float32))
        print(f"template saved from {os.path.basename(dvr_path)} frame {frame_index}: {TEMPLATE}")
        return
    sys.exit("could not read that frame")


_TFFT = {}


_ARMED_REFS = None


def armed_refs():
    """(zero-mean template, norm) for each ARMED reference image (NTSC, PAL)."""
    global _ARMED_REFS
    if _ARMED_REFS is None:
        _ARMED_REFS = []
        for path in (TEMPLATE, TEMPLATE_PAL):
            if os.path.exists(path):
                t = np.load(path)
                tz = t - t.mean()
                _ARMED_REFS.append((tz, float(np.sqrt((tz * tz).sum()))))
    return _ARMED_REFS


_S_REF = None


def _disarmed(g, x, y, cw):
    """Is there an "S" one letter left of the ARMED found at (x, y)? Then it's DISARMED, which the
    OSD shows in the same place whenever the quad is disarmed (start of a recording, after landing)."""
    global _S_REF
    if _S_REF is None:
        s_ = np.load(DISARMED_S) if os.path.exists(DISARMED_S) else None
        _S_REF = False if s_ is None else (s_ - s_.mean(), float(np.sqrt(((s_ - s_.mean()) ** 2).sum())))
    if _S_REF is False:
        return False
    sz, sn = _S_REF
    h, w = sz.shape
    y0, y1 = max(0, y - 3), min(g.shape[0], y + 3 + h)
    x0, x1 = max(0, x - cw - 6), min(g.shape[1], x - cw + 6 + w)
    if y1 - y0 < h or x1 - x0 < w:
        return False                     # no room to look (ARMED at the very edge of the box)
    win = np.lib.stride_tricks.sliding_window_view(g[y0:y1, x0:x1], (h, w))
    pz = win - win.mean(axis=(2, 3), keepdims=True)
    ncc = (pz * sz).sum(axis=(2, 3)) / (np.sqrt((pz * pz).sum(axis=(2, 3))) * sn + 1e-9)
    return float(ncc.max()) >= DISARMED_S_MIN


def armed_score(f):
    """Best normalised cross-correlation of any ARMED reference in the search box (0 when it's the
    ARMED of a DISARMED). The frame's filtering, FFT and local variance are shared by the references
    (padded to a fast FFT size)."""
    refs = armed_refs()
    g = half(highpass(f))
    H, W = g.shape
    S = (-(-H // 16) * 16, -(-W // 16) * 16)
    Gf = np.fft.rfft2(g, s=S)
    c1 = c2 = None
    sd = {}
    best, at = -1.0, None
    for tz, tn in refs:
        th, tw = tz.shape
        key = (id(tz), S)
        if key not in _TFFT:
            _TFFT[key] = np.conj(np.fft.rfft2(tz, s=S))
        if (th, tw) not in sd:
            if c1 is None:
                c1 = np.pad(np.cumsum(np.cumsum(g, 0), 1), ((1, 0), (1, 0)))
                c2 = np.pad(np.cumsum(np.cumsum(g * g, 0), 1), ((1, 0), (1, 0)))
            box = lambda c: c[th:, tw:] - c[:-th, tw:] - c[th:, :-tw] + c[:-th, :-tw]
            s1, s2 = box(c1), box(c2)
            sd[(th, tw)] = np.sqrt(np.maximum(s2 - s1 * s1 / (th * tw), 1e-6))
        num = np.fft.irfft2(Gf * _TFFT[key], s=S)[: H - th + 1, : W - tw + 1]
        sc = num / (sd[(th, tw)] * tn)
        k = int(sc.argmax())
        if sc.flat[k] > best:
            best, at = float(sc.flat[k]), (k // sc.shape[1], k % sc.shape[1], tw // 5)
    if best >= ARM_THRESHOLD and _disarmed(g, at[1], at[0], at[2]):
        return 0.0
    return best


def arm_score(f, tz, tnorm):
    """best normalised cross-correlation of the template anywhere in the search box (via FFT)."""
    g = half(highpass(f))
    th, tw = tz.shape
    H, W = g.shape
    key = (id(tz), H, W)
    if key not in _TFFT:
        _TFFT[key] = np.conj(np.fft.rfft2(tz, s=(H, W)))
    num = np.fft.irfft2(np.fft.rfft2(g) * _TFFT[key], s=(H, W))[: H - th + 1, : W - tw + 1]
    c1 = np.pad(np.cumsum(np.cumsum(g, 0), 1), ((1, 0), (1, 0)))
    c2 = np.pad(np.cumsum(np.cumsum(g * g, 0), 1), ((1, 0), (1, 0)))
    box = lambda c: c[th:, tw:] - c[:-th, tw:] - c[th:, :-tw] + c[:-th, :-tw]
    s1, s2 = box(c1), box(c2)
    var = np.maximum(s2 - s1 * s1 / (th * tw), 1e-6)
    return float((num / (np.sqrt(var) * tnorm)).max())


def detect_armed(path, verbose=False):
    """(time_s, frame_index, peak_score, frames_seen) of the first ARMED, or None."""
    runs = detect_armed_runs(path, first_only=True)
    return tuple(runs[0]) if runs else None


def detect_armed_runs(path, step=ARMED_STEP, first_only=False):
    """ARMED runs in the usual OSD area; if none at all, look across the whole middle of the frame."""
    prefetch(path)
    runs = _armed_runs(path, step, first_only)
    if not runs and OSD_MODE == "auto":
        set_osd_search("wide")
        try:
            runs = _armed_runs(path, step, first_only, dur=180.0)
        finally:
            set_osd_search("auto")
    return runs


def _armed_runs(path, step=ARMED_STEP, first_only=False, dur=None, start=0.0):
    """Every ARMED in the recording: [(time_s, frame_index, peak_score, frames_seen)], frame-exact starts.
    Scans every `step`-th frame (scoring is most of the cost), then checks each hit frame by frame:
    ARM_MIN_RUN frames in a row, so one stray frame can't pass for an ARMED."""
    fps, _ = probe_fps(path)
    f0 = int(round(start * fps))
    t0 = (f0 - 0.5) / fps if f0 > 0 else 0.0     # mid-frame, so the first frame read is exactly f0
    out, cur, missed = [], None, 0
    for i, f in dvr_frames(path, start=t0, step=step, dur=dur):
        if i % 25 == 0 and getattr(_abort, "on", False) and is_skipped(path):
            raise SkipFile(path)
        s_ = armed_score(f)
        if s_ >= ARM_THRESHOLD:
            if cur is None:
                cur = [f0 + i * step, 0, 0.0]
            cur[1] += missed + 1
            cur[2] = max(cur[2], s_)
            missed = 0
            continue
        if cur is not None and missed == 0:
            missed = 1                      # one glitch frame: wait for the next sample
            continue
        missed = 0
        if cur is not None:
            r = _confirm_armed(path, fps, cur, step)
            cur = None
            if r:
                out.append(r)
                if first_only:
                    return out
    if cur is not None:
        r = _confirm_armed(path, fps, cur, step)
        if r:
            out.append(r)
    return out


WINDOW_USE_FULL = True     # tests set False to exercise the windowed reads on cached projects


def armed_runs_in(path, spans):
    """ARMED runs inside the given (t0, t1) stretches of a DVR only (the clock says where to look);
    the whole-file result instead if it's already known."""
    full = peek_cache(ARMED + "_all", path) if WINDOW_USE_FULL else None
    if full is not None:
        full = [] if full == NONE else full
        return [r for r in full if any(a <= r[0] <= b for a, b in spans)]
    dur = media_info(path)[0]
    merged = []
    for a, b in sorted((max(0.0, a), min(dur, b)) for a, b in spans):
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out = []
    for a, b in merged:
        out += [r for r in _armed_runs(path, start=a, dur=b - a) if not any(abs(r[1] - o[1]) < 2 for o in out)]
    return out


def _confirm_armed(path, fps, cand, step):
    """Frame by frame around a coarse hit: first frame of ARM_MIN_RUN ARMED frames in a row, or None."""
    k, n, peak = cand
    lo = max(0, k - step + 1)
    hi = k + max(n - 1, 0) * step + ARM_MIN_RUN + 1
    start, run, miss = None, 0, 0
    t0 = (lo - 0.5) / fps if lo > 0 else 0.0
    for j, f in dvr_frames(path, start=t0, dur=(hi - lo + 0.5 + 1) / fps):
        if armed_score(f) >= ARM_THRESHOLD:
            start = lo + j if start is None else start
            run += 1
            miss = 0
            if run >= ARM_MIN_RUN - 1:           # 7 frames, what the old every-3rd scan accepted
                return [start / fps, start, peak, n * step]
        elif start is not None and miss == 0:
            miss = 1                        # one analog glitch frame inside the ARMED
        else:
            start, run, miss = None, 0, 0
    return None


# ------------------------------------------- DISARM detector (experimental)
# On disarm Betaflight replaces the OSD with its STATS page; the "--- STATS ---" header appears on
# the disarm frame. Template cut at (555,228) in a 1280x960 frame; searched at the same two
# OSD positions as ARMED (the second one is 43 px left / 71 px down).
STATS_TEMPLATE = os.path.join(HERE, "stats_template.npy")
STX, STY = 555, 228
STATS_BOX = (488, 40, 312, 680)
# NOTE: experimental, not used by matching. The prop-stop timing is tight (whine drops ~2.8 frames
# after the disarm, +-2), but the STATS header moves with the Betaflight stats settings (centre on
# short pages, top-left on long ones), and a whole-frame search is slow and gives false hits.
DISARM_LAG = 2.8 / (30000 / 1001)         # s from the disarm frame to the motor whine dropping


def make_stats_template(dvr_path, t):
    for _, f in dvr_frames(dvr_path, start=t, dur=0.02, box=(STX, STY, TW, TH)):
        np.save(STATS_TEMPLATE, half(highpass(f)).astype(np.float32))
        print(f"STATS template saved from {os.path.basename(dvr_path)} at {t:.3f}s")
        return


def detect_disarms(path, step=3):
    """Start times (s, frame-exact) of every STATS page = every disarm."""
    t = np.load(STATS_TEMPLATE)
    tz = t - t.mean()
    tnorm = float(np.sqrt((tz * tz).sum()))
    fps, _ = probe_fps(path)
    runs, cur = [], None
    for i, f in dvr_frames(path, step=step, box=STATS_BOX):
        if arm_score(f, tz, tnorm) >= ARM_THRESHOLD:
            cur = cur or [i * step, 0]
            cur[1] += 1
        elif cur is not None:
            if cur[1] * step >= ARM_MIN_RUN:
                runs.append(cur[0])
            cur = None
    if cur is not None and cur[1] * step >= ARM_MIN_RUN:
        runs.append(cur[0])
    out = []
    for k in runs:
        first, lo = k, max(0, k - step)
        for j, f in dvr_frames(path, start=lo / fps, dur=(k - lo + 0.5) / fps, box=STATS_BOX):
            if arm_score(f, tz, tnorm) >= ARM_THRESHOLD:
                first = lo + j
                break
        out.append(first / fps)
    return out


# ------------------------------------------------------- prop-sound detector
def audio_envelope(path, sr=48000, block_ms=5, start=0.0, dur=None):
    cmd = ["ffmpeg", "-v", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]          # input seek: only reads that part of the file
    cmd += ["-i", path]
    if dur:
        cmd += ["-t", f"{dur:.3f}"]
    with DISK:
        raw = run_skippable(cmd + ["-map", "0:a:0", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], path)
    a = np.frombuffer(raw, np.float32).astype(np.float64)
    if len(a) < sr // 10:                       # nothing there (window past the end of the file)
        return np.zeros(0), block_ms / 1000
    if start <= 0:
        keep_sound_print(path, a, sr)           # for the stickcam, at no extra read
    # motor whine band 1-6 kHz (no wind rumble, no high hiss such as cicadas), zero-phase so
    # no delay, then tilted toward the highs for a sharp onset
    A = np.fft.rfft(a)
    f = np.fft.rfftfreq(len(a), 1 / sr)
    A[(f < 1000) | (f > 6000)] = 0
    d = np.diff(np.fft.irfft(A, len(a)), prepend=0.0)
    win = sr * block_ms // 1000
    n = len(d) // win
    rms = np.sqrt((d[: n * win].reshape(n, win) ** 2).mean(1)) + 1e-9
    return 20 * np.log10(rms), block_ms / 1000


def detect_prop_stops(path, start=0.0, dur=None, drop_db=12.0):
    """Times (s) where the motors stop: first point the 4-10 kHz motor whine is 6 dB down and stays
    down (the whine stops at the disarm; the prop wash fades out more slowly)."""
    start = max(0.0, start)
    sr = 48000
    cmd = ["ffmpeg", "-v", "error"] + (["-ss", f"{start:.3f}"] if start > 0 else []) + ["-i", path]
    cmd += (["-t", f"{dur:.3f}"] if dur else []) + ["-map", "0:a:0", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    x = np.frombuffer(run(cmd), np.float32).astype(np.float64)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / sr)
    X[(f < 4000) | (f > 10000)] = 0
    x = np.fft.irfft(X, len(x))
    win = sr // 200
    n = len(x) // win
    env = 20 * np.log10(np.sqrt((x[: n * win].reshape(n, win) ** 2).mean(1)) + 1e-12)
    bt, pre_n, post_n, hold = 0.005, 300, 160, 20
    out, k = [], pre_n
    while k < n - post_n:
        level = np.median(env[k - pre_n:k])
        if level - np.median(env[k + 40:k + post_n]) >= drop_db:
            j = k
            while j < k + 120 and not (env[j] < level - 6 and np.median(env[j:j + hold]) < level - 6):
                j += 1
            out.append((start + j * bt, float(level - np.median(env[k + 40:k + post_n]))))
            k = j + int(2.0 / bt)
        else:
            k += 4
    return out


def detect_prop_onsets(path, jump_db=18.0, start=0.0, dur=None):
    """candidate prop starts (time_s, jump_db) where the level jumps and stays up.
    With start/dur only that window of the file is read (times stay file-relative)."""
    start = max(0.0, start)
    env, bt = audio_envelope(path, start=start, dur=dur)
    return [(t + start, j) for t, j in _onsets(env, bt, jump_db)]


SUSTAIN_S = 0.5          # a prop start must stay up this long (short bursts: ESC tones, twitches)


BURST_DB = None          # if set: ignore a "start" whose level is back within this many dB of the
                         # background 0.6-1.6 s later (ESC tones, a quick motor twitch)


def _onsets(env, bt, jump_db, sustain=None, burst_db=None):
    pre_n, post_n, gap = int(1.0 / bt), int((sustain or SUSTAIN_S) / bt), int(0.02 / bt)
    n = len(env)
    if n < pre_n + post_n + gap + 2:
        return []
    sw = np.lib.stride_tricks.sliding_window_view
    pre = np.full(n, np.nan)
    post = np.full(n, np.nan)
    pre[pre_n:] = np.median(sw(env, pre_n), 1)[: n - pre_n]
    m = np.median(sw(env, post_n), 1)
    post[: n - post_n - gap] = m[gap: n - post_n]
    jump = post - pre
    hits = np.where(jump >= jump_db)[0]
    burst_db = BURST_DB if burst_db is None else burst_db
    if burst_db:
        a_, b_ = int(0.6 / bt), int(1.6 / bt)
        hits = [i for i in hits if i + b_ >= n or np.median(env[i + a_:i + b_]) - pre[i] >= burst_db]
    events, last = [], -10**9
    for i in hits:
        if i - last > int(2.0 / bt):          # new event
            events.append(i)
        last = i
    out = []
    for i in events:
        # refine: within +-0.6 s, first block halfway (in dB) between floor and new level
        lo = max(pre_n, i - int(0.6 / bt))
        hi = min(n - post_n - gap - 1, i + int(0.6 / bt))
        j = lo + int(np.nanargmax(jump[lo:hi]))
        floor, level = min(pre[i], pre[j]), post[j]
        mid = (floor + level) / 2
        # earliest block that crosses halfway AND stays up for 0.2 s (ignores lone clicks);
        # search well before the peak, since a loud start can push the peak later
        hold = int(0.2 / bt)
        k = max(0, i - int(1.0 / bt))
        end = min(n - hold, j + int(0.3 / bt))
        while k < end and not (env[k] >= mid and np.median(env[k:k + hold]) >= mid):
            k += 1
        out.append((k * bt, float(level - floor)))
    return out


# ----------------------------------------------------------------- progress
PROGRESS = None          # set by the panel job: called with (fraction 0..1, label)
TIMING = None            # panel jobs record progress + per-step timings here (timing_log.jsonl)


EVENT = None                # panel jobs: per-file step events for the live file list


def ev(path, chip=None, state=None, text="", done=None):
    """One step of one file for the panel: chip (step name) in state ok / bad / run, its text, and
    when the file is finished, done = matched / nomatch / part (split-file continuation) / unread."""
    if EVENT and path:
        EVENT("ev", {"p": path, "c": chip, "s": state, "t": text, "d": done})


def mmss(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def progress(fraction, label, eta=None):
    """fraction 0..1 of the current job, a label, and (when known) the seconds left"""
    fraction = max(0.0, min(1.0, fraction))
    if TIMING is not None:
        TIMING["progress"].append([round(time.time() - TIMING["t0"], 2), round(fraction, 4), label,
                                   None if eta is None else round(eta, 1)])
    if PROGRESS:
        PROGRESS(fraction, label, eta)


def _timed(name, fn):
    """wrap a step so panel jobs log how long it took, on which file, and what it read"""
    def w(*a, **k):
        t = time.time()
        out = None
        try:
            out = fn(*a, **k)
            return out
        finally:
            if TIMING is not None:
                f = next((x for x in a if isinstance(x, str)), "")
                nums = [x for x in a if isinstance(x, (int, float)) and not isinstance(x, bool)]
                info = {"args": [round(float(x), 3) for x in nums][:4], **{k_: v for k_, v in k.items()
                        if isinstance(v, (int, float, str)) and not isinstance(v, bool)}}
                try:
                    info["size"] = os.path.getsize(f)
                except Exception:
                    pass
                if name == "detect_armed_runs" and out:
                    info["found_at"] = round(out[0][0], 2)
                TIMING["steps"].append([name, os.path.basename(f), round(time.time() - t, 3),
                                        round(t - TIMING["t0"], 2), threading.current_thread().name, info])
    return w


def _read_uncached(path, offset, nbytes):
    """Read straight from the drive, bypassing Windows' file cache (so a file read a minute ago
    doesn't make a hard drive look like RAM). Returns bytes read."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.VirtualAlloc.restype = ctypes.c_void_p
    GENERIC_READ, SHARE_RW, OPEN_EXISTING = 0x80000000, 0x3, 3
    NO_BUFFERING, SEQUENTIAL = 0x20000000, 0x08000000
    h = k32.CreateFileW(path, GENERIC_READ, SHARE_RW, None, OPEN_EXISTING, NO_BUFFERING | SEQUENTIAL, None)
    if h in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error())
    chunk = 8 << 20
    buf = k32.VirtualAlloc(None, chunk, 0x3000, 0x04)         # page-aligned, as unbuffered reads need
    try:
        off = offset - offset % 65536
        lo = wintypes.LONG(off & 0xFFFFFFFF)
        hi = wintypes.LONG(off >> 32)
        k32.SetFilePointer(wintypes.HANDLE(h), lo, ctypes.byref(hi), 0)
        done, got = 0, wintypes.DWORD(0)
        while done < nbytes:
            if not k32.ReadFile(wintypes.HANDLE(h), ctypes.c_void_p(buf), chunk, ctypes.byref(got), None) \
                    or got.value == 0:
                break
            done += got.value
        return done
    finally:
        k32.VirtualFree(ctypes.c_void_p(buf), 0, 0x8000)
        k32.CloseHandle(wintypes.HANDLE(h))


def disk_speed(paths, mb=48, samples=3):
    """Sequential read speed (MB/s) of the drive holding the footage, measured past the file cache.
    A hard drive is faster on its outer tracks than its inner ones, so it samples a few large files
    and takes the middle value."""
    files = sorted((p for p in paths if p and os.path.exists(p)), key=os.path.getsize, reverse=True)
    if not files:
        return None
    pick = files[:: max(1, len(files) // samples)][:samples]
    speeds = []
    for f in pick:
        size = os.path.getsize(f)
        n = min(mb << 20, size // 2)
        off = max(0, size // 2 - n // 2)
        t = time.time()
        try:
            got = _read_uncached(f, off, n)
        except Exception:
            with open(f, "rb", buffering=0) as fh:
                fh.seek(off)
                got = len(fh.read(n))
        speeds.append(got / (1 << 20) / max(1e-3, time.time() - t))
    speeds.sort()
    return round(speeds[len(speeds) // 2], 1)


# ---------------------------------------------------------------- disk use
import threading
DISK = threading.RLock()    # big reads take turns: a hard drive thrashes when two files are read at once
DISK_MBPS = None            # measured read speed of the footage drive (MB/s), for time estimates
_prefetched = set()


def prefetch(path, cap_mb=120):
    """Read the start of a DVR file (where ARMED usually is: ~80 s) in one sequential pass, so the
    scan that follows runs from memory; a later ARMED just reads on from the drive."""
    if path in _prefetched:
        return
    _prefetched.add(path)
    with DISK:
        try:
            with open(path, "rb") as fh:
                left = cap_mb << 20
                while left > 0:
                    b = fh.read(16 << 20)
                    if not b:
                        break
                    left -= len(b)
        except OSError:
            pass


def drive_speed(paths, max_age_h=24):
    """Read speed (MB/s) of the drive holding these files; measured once a day per drive."""
    files = [p for p in paths if p and os.path.exists(p)]
    if not files:
        return None
    drive = os.path.splitdrive(os.path.abspath(files[0]))[0].upper() or "?"
    store = os.path.join(HERE, "disk_speed.json")
    try:
        known = json.load(open(store))
    except Exception:
        known = {}
    if drive in known and time.time() - known[drive][1] < max_age_h * 3600:
        return known[drive][0]
    mbps = disk_speed(files)
    if mbps:
        known[drive] = [mbps, time.time()]
        try:
            json.dump(known, open(store, "w"))
        except Exception:
            pass
    return mbps


# -------------------------------------------------------------------- cache
_CACHE_LOCK = threading.RLock()


NONE = "__none__"           # cached "nothing found" (e.g. a DVR with no ARMED)


def peek_cache(kind, path):
    try:
        st = os.stat(path)
        key = f"{kind}|{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}"
        with _CACHE_LOCK:
            return json.load(open(CACHE)).get(key)
    except Exception:
        return None


def cached(kind, path, fn):
    st = os.stat(path)
    key = f"{kind}|{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}"
    hit = peek_cache(kind, path)
    if hit is not None:
        return None if hit == NONE else hit
    value = fn()                                   # computed outside the lock (can take a while)
    with _CACHE_LOCK:
        try:
            cache = json.load(open(CACHE))
        except Exception:
            cache = {}
        cache[key] = NONE if value is None else value
        try:
            tmp = CACHE + ".tmp"
            json.dump(cache, open(tmp, "w"), indent=1)
            os.replace(tmp, CACHE)
        except Exception:
            pass
    return value


# --------------------------------------------------------------- sync core
def sync_clips(fps, clips, dvr_re, act_re, lead=1, prior_window=30.0, only_selected=False,
               verbose=False, log=print):
    """
    clips: list of dicts with
      id, kind ('video'|'audio'), track (int), path, start, end (sequence frames),
      src0 (source seconds at the clip's first sequence frame), linked (ids), selected (bool)
    Returns one report row per flight (a DVR clip plus any split-file pieces butted after it).
    Rows with status 'move' carry move_frames, the ids to move, and 'order' (apply in that order).
    """
    fd = 1 / fps
    by_id = {c["id"]: c for c in clips}
    video = [c for c in clips if c["kind"] == "video" and c.get("path")]
    base = lambda c: os.path.basename(c["path"])
    dvr_all = sorted([c for c in video if is_dvr(c["path"], dvr_re)],
                     key=lambda c: (c["track"], c["start"]))
    act = [c for c in video if is_hd(c["path"], dvr_re, act_re)]
    armed = lambda c: (cached(ARMED, c["path"], lambda: detect_armed(c["path"], verbose))
                       if os.path.exists(c["path"]) else None)

    # 1. group split DVR files: a clip butted right after another on the same track,
    #    with no ARMED of its own, is the continuation of that flight. The cheap test (butted)
    #    first, so the selection is narrowed before any footage is decoded.
    sel_act = [a for a in act if a.get("selected")]

    def picked(g):                      # a flight is picked when its DVR, or an HD clip under it, is selected
        if not only_selected or any(c.get("selected") for c in g):
            return True
        g0, g1 = g[0]["start"], g[-1]["end"]
        return any(min(g1, a["end"]) > max(g0, a["start"]) for a in sel_act)

    chains = []
    for c in dvr_all:
        p = chains[-1][-1] if chains else None
        if p and p["track"] == c["track"] and 0 <= c["start"] - p["end"] <= 1:
            chains[-1].append(c)
        else:
            chains.append([c])
    chains = [ch for ch in chains if picked(ch)]
    groups = []
    if any(len(ch) > 1 for ch in chains):
        progress(0.0, "Checking which DVR files belong together", None)
    for ch in chains:
        groups.append([ch[0]])
        for c in ch[1:]:
            if dvr_continues(groups[-1][-1]["path"], c["path"]) and not armed_early(c["path"]):
                groups[-1].append(c)
            else:
                groups.append([c])
    groups = [g for g in groups if picked(g)]
    log(f"{len(groups)} DVR flight(s), {len(act)} HD clip(s), {fps:.3f} fps")
    sticks = [c for c in video if is_stick(c["path"])]
    if only_selected:                   # a stickcam clip is picked when it, or the HD clip under it, is
        sticks = [c for c in sticks if c.get("selected") or any(
            a.get("selected") and min(c["end"], a["end"]) > max(c["start"], a["start"]) for a in act)]
    if EVENT:
        EVENT("files", [{"p": g[0]["path"], "r": "dvr", "id": g[0]["id"]} for g in groups] +
              [{"p": c["path"], "r": "stick", "id": c["id"]} for c in sticks])
    prewarm_flights(groups, act, fd, prior_window)

    rows = []
    t_sync = time.time()
    # the bar creeps toward the end of each flight's share while it's being lined up
    # (motion checks decode 4K and can take a few seconds), one cheap update a second
    creep = {"f0": 0.85, "f1": 0.85, "t": time.time(), "label": "Syncing", "stop": False}

    def creep_to(f1, label):
        now = time.time()
        f = creep["f0"] + (creep["f1"] - creep["f0"]) * (1 - math.exp(-(now - creep["t"]) / 3.0))
        creep.update(f0=f, f1=f1, t=now, label=label)
        progress(f, label, None)

    def creeper():
        while not creep["stop"]:
            time.sleep(1.0)
            if not creep["stop"]:
                f = creep["f0"] + (creep["f1"] - creep["f0"]) * (1 - math.exp(-(time.time() - creep["t"]) / 3.0))
                progress(f, creep["label"], None)

    threading.Thread(target=creeper, name="creep", daemon=True).start()
    # a flight you skip is left out; one you un-skip while it runs is done after the others
    work, row_of, requeued = list(enumerate(groups)), {}, set()

    def requeue():
        for gi_, g_ in enumerate(groups):
            k_ = g_[0]["path"]
            if k_ in SKIPPED_SEEN and not is_skipped(k_) and k_ not in requeued:
                requeued.add(k_)
                SKIPPED_SEEN.discard(k_)
                if row_of.get(id(g_[0])) in rows:
                    rows.remove(row_of[id(g_[0])])
                work.append((gi_, g_))

    for gi, g in work:
        try:
            d = g[0]
            creep_to(0.85 + 0.15 * (gi + 0.9) / max(1, len(groups)), f"Syncing {base(d)}")
            row = {"id": d["id"], "dvr": base(d) + (f" (+{len(g) - 1} split)" if len(g) > 1 else ""),
                   "status": None, "path": d["path"]}
            rows.append(row)
            row_of[id(d)] = row
            if is_skipped(d["path"]):
                SKIPPED_SEEN.add(d["path"])
                row["status"] = "skipped"; row["why"] = "skipped by you"
                ev(d["path"], done="skipped")
                continue
            if stop_requested() and peek_cache(ARMED, d["path"]) is None:
                row["status"] = "skipped"; row["why"] = "stopped before it was analysed"; continue
            g_start, g_end = g[0]["start"], g[-1]["end"]
            over = [a for a in act if min(g_end, a["end"]) > max(g_start, a["start"])]
            if not over:
                row["status"] = "skipped"; row["why"] = "no overlapping HD clip"; continue
            missing = [c["path"] for c in g + over if not os.path.exists(c["path"])]
            if missing:
                row["status"] = "skipped"; row["why"] = "file not found: " + missing[0]; continue
            ev(d["path"], "arm", "run", "finding ARMED")
            arm = armed(d)
            if not arm:
                ev(d["path"], "arm", "bad", "no ARMED")
                row["status"] = "skipped"; row["why"] = "ARMED not found on DVR"; continue
            ev(d["path"], "arm", "ok", f"ARMED {mmss(arm[0])}")
            t_arm, f_arm, score, _ = arm
            row.update(armed_s=round(t_arm, 3), armed_score=round(score, 2))
            arm_seq = d["start"] + (t_arm - d["src0"]) / fd       # where ARMED sits now (seq frames)

            # the pair is whatever HD footage the DVR overlaps (placed roughly over it is enough): its prop
            # starts, heard from the start of each overlapping HD clip as Match & sync does (first 45 s, then
            # to 75 s), kept if visible on the timeline. Only if none: wider around where the DVR was dropped
            # (a takeoff later in the recording)
            def gather(stage):
                cands, hidden = [], 0
                for a in over:
                    if stage < 2:
                        found = head_props(a["path"], further=stage == 1)
                    else:
                        pred = a["src0"] + (arm_seq - a["start"]) * fd
                        w0, wd = round(max(0.0, pred - SYNC_WIDE)), round(2 * SYNC_WIDE)
                        if pred + SYNC_WIDE < 0 or w0 >= media_info(a["path"])[0]:
                            continue
                        found = cached(f"{PROPS}@{w0}+{wd}", a["path"],
                                       lambda: detect_prop_onsets(a["path"], start=w0, dur=wd)) or []
                    for t, jump in found:
                        pos = a["start"] + (t - a["src0"]) / fd
                        if a["start"] <= pos < a["end"]:
                            cands.append((pos, t, jump, a))
                        else:
                            hidden += 1
                return cands, hidden

            ev(d["path"], "props", "run", "listening to the HD")
            cands, hidden = gather(0)
            for stage in (1, 2):
                if cands or stop_requested():
                    break
                ev(d["path"], "props", "run", "Listening further")
                cands, hidden = gather(stage)
            ev(d["path"], "props", "ok" if cands else "bad",
               f"props → {base(cands[0][3])}" if len(cands) == 1 else f"{len(cands)} prop starts" if cands else "no props")
            if not cands:
                row["status"] = "skipped"
                row["why"] = ("prop start is trimmed out of the HD clip" if hidden
                              else "no prop-sound start found on HD")
                continue
            if len(cands) > 1:
                creep["label"] = f"{base(d)}: {len(cands)} prop sounds, comparing the motion"
                # several prop sounds: the one whose following flight moves like the DVR's is the takeoff
                ds = motion_signal(d["path"], t_arm + MOTION_T0, True)
                scored = [(motion_score(ds, motion_signal(c[3]["path"], c[1] + MOTION_T0, False)), c) for c in cands]
                sc, best = max(scored, key=lambda x: x[0])
                if sc >= 0.3:
                    pos, t_prop, jump, a = best
                    row["note"] = f"{len(cands)} prop sounds; picked the one whose motion matches (score {sc:.2f})"
                else:
                    cands_near = min(cands, key=lambda c: abs(c[0] - arm_seq))
                    pos, t_prop, jump, a = cands_near
                    row["note"] = f"{len(cands)} prop sounds and motion unclear; used the one nearest where the DVR sits"
            else:
                pos, t_prop, jump, a = cands[0]
            t_pos = d["src0"] + (pos - d["start"]) * fd            # DVR time of the prop start, as placed now
            arm_win = [(t_pos - prior_window - 3, t_pos + prior_window + 3)]
            # (where the DVR was dropped doesn't pick the ARMED: it's only roughly placed. The motion does:)
            # motion doesn't fit: maybe armed, disarmed and armed again; try every ARMED in the DVR
            try:
                ap_sig = motion_signal(a["path"], t_prop + MOTION_T0, False)
                s0 = motion_score(motion_signal(d["path"], t_arm + MOTION_T0, True), ap_sig)
            except Exception:
                s0, ap_sig = 1.0, None
            if s0 < WEAK_SCORE and ap_sig is not None:
                best = (s0, t_arm)
                creep["label"] = f"{base(d)}: motion doesn't fit, trying its other ARMEDs"
                tried = set()
                # around the prop start first; the whole DVR only if nothing there fits (the clip may
                # sit far from where it belongs)
                for runs_ in (lambda: armed_runs_in(d["path"], arm_win),
                              lambda: cached(ARMED + "_all", d["path"], lambda: detect_armed_runs(d["path"])) or []):
                    for r in runs_():
                        if abs(r[0] - t_arm) < 2 or round(r[0], 2) in tried:
                            continue
                        tried.add(round(r[0], 2))
                        sc = motion_score(motion_signal(d["path"], r[0] + MOTION_T0, True), ap_sig)
                        if sc > best[0]:
                            best = (sc, r[0])
                    if best[0] >= s0 + 0.1:
                        break
                if best[1] != t_arm and best[0] >= s0 + 0.1:
                    t_arm = best[1]
                    row["armed_s"] = round(t_arm, 3)
                    row["note"] = (row.get("note", "") + " armed more than once: used the arming whose motion "
                                   f"matches ({best[0]:.2f})").strip()
            row.update(action=base(a), props_s=round(t_prop, 3), jump_db=round(jump, 1))

            sound_frame = a["start"] + math.floor((t_prop - a["src0"]) / fd + 1e-9)
            edge = ((t_prop - a["src0"]) / fd) % 1
            new_start = sound_frame - dvr_lead(d["path"], lead) - math.ceil((t_arm - d["src0"]) / fd - 1e-9)
            delta = new_start - d["start"]
            if edge < 0.15 or edge > 0.85:
                row["note"] = (row.get("note", "") + " prop start near a frame edge, check by eye").strip()

            movers = []
            for c in g:
                linked = c.get("linked") or [x["id"] for x in clips if x["kind"] == "audio"
                                             and x.get("path") == c["path"]
                                             and (x["start"], x["end"]) == (c["start"], c["end"])]
                movers += [c["id"]] + [i for i in linked if i in by_id and i != c["id"]]
            row.update(move_frames=delta, movers=list(dict.fromkeys(movers)),
                       status="in sync" if delta == 0 else "move")
            log(f"  {row['dvr']}: ARMED {t_arm:.3f}s (score {score:.2f}) | props {base(a)} "
                f"{t_prop:.3f}s (+{jump:.0f} dB) -> {delta:+d} frames")

        finally:
            requeue()

    # stickcam clips: lined up by sound with the HD clip they sit over
    swork, srow_of, srequeued = list(sticks), {}, set()

    def srequeue():
        for c_ in sticks:
            if c_["path"] in SKIPPED_SEEN and not is_skipped(c_["path"]) and c_["path"] not in srequeued:
                srequeued.add(c_["path"])
                SKIPPED_SEEN.discard(c_["path"])
                if srow_of.get(id(c_)) in rows:
                    rows.remove(srow_of[id(c_)])
                swork.append(c_)

    for st_ in swork:
        try:
            if stop_requested():
                break
            creep_to(creep["f1"], f"Stickcam {base(st_)}: listening")
            row = {"id": st_["id"], "dvr": base(st_), "kind": "stick", "status": None, "path": st_["path"]}
            rows.append(row)
            srow_of[id(st_)] = row
            if is_skipped(st_["path"]):
                SKIPPED_SEEN.add(st_["path"])
                row["status"] = "skipped"; row["why"] = "skipped by you"
                ev(st_["path"], done="skipped")
                continue
            under = [a for a in act if min(st_["end"], a["end"]) > max(st_["start"], a["start"])]
            if not under:
                row["status"] = "skipped"; row["why"] = "no HD clip under it"
                ev(st_["path"], "arm", "bad", "no HD clip under it")
                continue
            a = max(under, key=lambda a: min(st_["end"], a["end"]) - max(st_["start"], a["start"]))
            if not (os.path.exists(st_["path"]) and os.path.exists(a["path"])):
                row["status"] = "skipped"; row["why"] = "file not found"
                continue
            ev(st_["path"], "arm", "run", f"listening with {base(a)}")
            h = sound_print(a["path"], STICK_HD_LISTEN)
            r = None
            for dur in (STICK_LISTEN, None):
                r = sound_offset(h, sound_print(st_["path"], dur))
                if r and r[1] >= STICK_MIN_Z and r[2] >= STICK_MIN_RATIO:
                    break
                r = None
            if not r:
                row["status"] = "skipped"
                row["why"] = f"its sound doesn't match the start of {base(a)}"
                ev(st_["path"], "arm", "bad", "no sound match")
                continue
            lag, z, _ = r
            new_start = a["start"] + int(round((st_["src0"] - a["src0"] - lag) / fd))
            delta = new_start - st_["start"]
            linked = st_.get("linked") or [x["id"] for x in clips if x["kind"] == "audio" and x.get("path") == st_["path"]
                                           and (x["start"], x["end"]) == (st_["start"], st_["end"])]
            row.update(action=base(a), detail=f"stickcam · sound → {base(a)}", move_frames=delta,
                       movers=list(dict.fromkeys([st_["id"]] + [i for i in linked if i in by_id and i != st_["id"]])),
                       status="in sync" if delta == 0 else "move")
            ev(st_["path"], "arm", "ok", f"sound → {base(a)}")
            log(f"  {base(st_)}: sound lines up with {base(a)} (z {z:.0f}) -> {delta:+d} frames")

        finally:
            srequeue()

    creep["stop"] = True

    # 2. overlaps, judged on where every clip ends up (other DVRs move too)
    def final(cid, moving):
        c = by_id[cid]
        dl = moving.get(cid, 0)
        return c["start"] + dl, c["end"] + dl

    def clash_of(r, moving, place):
        for mid in r["movers"]:
            m = by_id[mid]
            s, e = place(mid)
            for o in clips:
                if o["id"] in r["movers"] or (o["kind"], o["track"]) != (m["kind"], m["track"]):
                    continue
                os_, oe = place(o["id"])
                if o["start"] >= 0 and s < oe and os_ < e:
                    return os.path.basename(o.get("path") or "") or o["id"]
        return None

    for _ in range(len(rows) + 1):
        moving = {i: r["move_frames"] for r in rows if r["status"] == "move" for i in r["movers"]}
        changed = False
        for r in rows:
            if r["status"] == "move":
                hit = clash_of(r, moving, lambda cid: final(cid, moving))
                if hit:
                    r["status"] = "blocked"; r["why"] = f"would overlap '{hit}' on its track"
                    changed = True
                    break
        if not changed:
            break

    # 3. an order for one-at-a-time moves (the panel): never move onto a clip still in the way
    current = {}
    pending = [r for r in rows if r["status"] == "move"]
    order = 0
    while pending:
        place_now = lambda cid: final(cid, current)
        ready = None
        for r in pending:
            trial = dict(current, **{i: r["move_frames"] for i in r["movers"]})
            if not clash_of(r, trial, lambda cid, t=trial: final(cid, t) if cid in r["movers"]
                            else place_now(cid)):
                ready = r
                break
        if not ready:
            for r in pending:
                r["status"] = "blocked"; r["why"] = "clips would have to swap places; move by hand"
            break
        ready["order"] = order; order += 1
        current.update({i: ready["move_frames"] for i in ready["movers"]})
        pending.remove(ready)
    for r in rows:
        ev(r.get("path"), done="matched" if r["status"] in ("move", "in sync")
           else "skipped" if r["status"] == "skipped" and r.get("why") == "skipped by you" else "nomatch")
    return rows


# ---------------------------------------------------------------------- XML
def url_to_path(u):
    p = urllib.parse.urlparse(u)
    path = urllib.parse.unquote(p.path)
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return path.replace("/", os.sep)


def seq_rate(seq):
    tb = int(seq.findtext("rate/timebase"))
    ntsc = (seq.findtext("rate/ntsc") or "FALSE").upper() == "TRUE"
    return tb * 1000 / 1001 if ntsc else float(tb)


def process_xml(xml_path, dvr_re, act_re, lead, prior_window, verbose):
    root = ET.fromstring(open(xml_path, "rb").read())
    files = {f.get("id"): url_to_path(f.findtext("pathurl"))
             for f in root.iter("file") if f.get("id") and f.findtext("pathurl")}
    seqs = root.findall("sequence") + root.findall(".//children/sequence")
    if not seqs:
        sys.exit("No sequence found in that XML.")
    report = []
    for seq in seqs:
        fps = seq_rate(seq)
        print(f"\nSequence '{seq.findtext('name')}'")
        els, clips = {}, []
        for kind in ("video", "audio"):
            for ti, tr in enumerate(seq.findall(f"media/{kind}/track")):
                for ci in tr.findall("clipitem"):
                    fe = ci.find("file")
                    ticks = ci.findtext("pproTicksIn")
                    inp = int(ci.findtext("in", "0"))
                    els[ci.get("id")] = ci
                    clips.append({
                        "id": ci.get("id"), "kind": kind, "track": ti,
                        "path": files.get(fe.get("id")) if fe is not None else None,
                        "start": int(ci.findtext("start")), "end": int(ci.findtext("end")),
                        "src0": int(ticks) / TICKS if ticks else inp / fps,
                        "linked": [l.findtext("linkclipref") for l in ci.findall("link")],
                    })
        rows = sync_clips(fps, clips, dvr_re, act_re, lead, prior_window, verbose=verbose)
        for r in rows:
            if r["status"] == "move":
                for mid in r["movers"]:
                    for tag in ("start", "end"):
                        n = els[mid].find(tag)
                        n.text = str(int(n.text) + r["move_frames"])
                r["status"] = "moved"
        report += rows
        name = seq.find("name")
        if name is not None:
            name.text = (name.text or "Sequence") + " - synced"
        u = seq.find("uuid")
        if u is not None:
            u.text = str(uuid.uuid4())
    base = os.path.splitext(xml_path)[0]
    with open(base + "_synced.xml", "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
        fh.write(ET.tostring(root, encoding="unicode"))
    print(f"\nWrote {base}_synced.xml\n\nSummary:")
    for r in report:
        print(f"  {r['dvr']:<20} {r['status']:<10} {r.get('move_frames', ''):>6}  "
              f"{r.get('why', '')} {r.get('note', '')}")
    return report


# ------------------------------------------------- matching DVR <-> HD
SIG_DIR = os.path.join(HERE, "cache")
MOTION_T0, MOTION_DUR, MOTION_HZ = 3.0, 15.0, 60.0   # motion compared over 15 s from 3 s after the cue
MATCH_TAU = 0.2                                       # weakest motion score accepted as a match
WEAK_SCORE = 0.3                                      # below this, look for a later ARMED too


STILL_S = 5.0                 # a still image (logo, title card) counts as Premiere's default still length


def media_info(path):
    """[duration_s, creation_time_iso] (cached). A still image has no duration: STILL_S; anything else
    without one (unreadable file) 0, so one odd file in the selection can't stop a run."""
    def f():
        try:
            out = json.loads(run(["ffprobe", "-v", "error", "-show_entries",
                                  "format=duration:format_tags=creation_time", "-of", "json", path]))["format"]
        except Exception:
            out = {}
        dur = out.get("duration")
        if dur in (None, "N/A"):
            dur = STILL_S if path.lower().endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".psd", ".bmp", ".gif")) else 0.0
        return [float(dur), out.get("tags", {}).get("creation_time", "")]
    return cached("info", path, f)


# --------------------------------------------------------------- which camera
# GoPro firmware prefix -> model (the FIRM box: "HD5.03.02.51.00" is a HERO5 Session)
GOPRO_MODELS = {
    "HD2": "HD HERO2", "HD3.01": "HERO3 White", "HD3.02": "HERO3 Silver", "HD3.03": "HERO3 Black",
    "HD3.10": "HERO3+ Silver", "HD3.11": "HERO3+ Black", "HD3.20": "HERO (2014)", "HD3.21": "HERO+ LCD",
    "HD3.22": "HERO+", "HD4.01": "HERO4 Silver", "HD4.02": "HERO4 Black", "HX1": "HERO4 Session",
    "HD5.02": "HERO5 Black", "HD5.03": "HERO5 Session", "HD6": "HERO6 Black", "H18": "HERO (2018)",
    "HD7": "HERO7 Black", "H19": "MAX", "HD8": "HERO8 Black", "HD9": "HERO9 Black", "H21": "HERO10 Black",
    "H22.01": "HERO11 Black", "H22.03": "HERO11 Black Mini", "H23": "HERO12 Black", "H24": "HERO13 Black",
}


def _mp4_udta(path):
    """{box name: first bytes} of the moov/udta boxes (and meta/ilst text) of an MP4/MOV: where
    cameras write their model, firmware and serial numbers. Only box headers are read."""
    import struct
    found = {}
    try:
        end = os.path.getsize(path)
        with open(path, "rb") as f:
            def walk(start, stop, inside):
                pos = start
                while pos + 8 <= stop:
                    f.seek(pos)
                    hdr = f.read(16)
                    size, typ = struct.unpack(">I4s", hdr[:8])
                    hl = 8
                    if size == 1:
                        size, hl = struct.unpack(">Q", hdr[8:16])[0], 16
                    elif size == 0:
                        size = stop - pos
                    if size < 8:
                        return
                    t = typ.decode("latin1")
                    if t in ("moov", "udta", "meta"):
                        walk(pos + hl + (4 if t == "meta" else 0), pos + size, inside + [t])
                    elif "udta" in inside:
                        f.seek(pos + hl)
                        found.setdefault(t, f.read(min(size - hl, 512)))
                    pos += size
            walk(0, end, [])
    except Exception:
        pass
    return found


def camera_info(path):
    """What recorded this file, from its metadata: {"name", "id", "w", "h", "fps"}. "id" tells two
    cameras of the same model apart (serial numbers where the camera writes them). A file whose
    metadata was stripped (stabilised exports, re-encodes) is an "Unknown source"."""
    def f():
        try:
            j = json.loads(run(["ffprobe", "-v", "error", "-show_entries",
                                "format_tags:stream=codec_type,width,height,r_frame_rate:stream_tags=handler_name,encoder",
                                "-of", "json", path]))
        except Exception:                                    # unreadable file: still listed, as unknown
            j = {}
        v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
        tags = {k.lower(): str(x) for k, x in j.get("format", {}).get("tags", {}).items()}
        vtags = {k.lower(): str(x).strip() for k, x in v.get("tags", {}).items()}
        num, _, den = v.get("r_frame_rate", "0/1").partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 0.0
        box = _mp4_udta(path)
        text = lambda k: re.sub(r"[^ -~]", "", box.get(k, b"").decode("latin1")).strip()
        name, ident = None, ""
        firm = text("FIRM") or tags.get("firmware", "")
        if firm:                                             # GoPro
            model = next((m for pre, m in sorted(GOPRO_MODELS.items(), key=lambda kv: -len(kv[0]))
                          if firm.startswith(pre)), None)
            minf = re.search(r"MINF.{0,8}?(HERO[ -~]{1,30}|MAX[ -~]{0,20})", box.get("GPMF", b"").decode("latin1"))
            name = "GoPro " + (minf.group(1).strip() if minf else model or f"(firmware {firm.split('.')[0]})")
            ident = text("LENS") or text("CAME")
        enc = tags.get("encoder", "") or vtags.get("encoder", "")
        if not name and enc.upper().startswith("DJI"):       # "DJIAction2" -> "DJI Action 2"
            name = "DJI " + re.sub(r"(?<=[a-z])(?=[A-Z0-9])|(?<=[0-9])(?=[A-Z])", " ", enc[3:]).strip()
        if not name and "ICAT" in box and DVR_NAME in os.path.basename(path).lower():
            name = "Orqa DVR"
        if not name and DVR_NAME in os.path.basename(path).lower():
            name = "Orqa DVR"
        if not name:                                         # make/model as most cameras and phones write it
            make = text("manu") or tags.get("make", "") or tags.get("com.apple.quicktime.make", "") \
                or tags.get("com.android.manufacturer", "")
            model = text("modl") or tags.get("model", "") or tags.get("com.apple.quicktime.model", "") \
                or tags.get("com.android.model", "")
            make, model = make.strip(), model.strip()
            if model:
                name = model if not make or model.lower().startswith(make.lower()) else f"{make} {model}"
        if not name and re.search(r"gopro", enc + vtags.get("handler_name", ""), re.I):
            name = "GoPro"
        return {"name": name or "Unknown source", "id": ident, "w": v.get("width", 0), "h": v.get("height", 0),
                "fps": round(fps, 3)}
    return f()


_CAM = {}                      # cache key -> camera_info, in memory (the disk cache is one big JSON)
_UNKNOWN_CAM = {"name": "Unknown source", "id": "", "w": 0, "h": 0, "fps": 0.0}


def camera_infos(paths):
    """camera_info of many files: from memory or the disk cache, the rest probed 8 at a time and
    written to the cache in one go. {path: info}"""
    import concurrent.futures
    keys = {}
    for p in dict.fromkeys(paths):
        try:
            st = os.stat(p)
            keys[p] = f"cam1|{os.path.abspath(p)}|{st.st_size}|{int(st.st_mtime)}"
        except OSError:
            pass
    missing = [p for p, k in keys.items() if k not in _CAM]
    if missing:
        with _CACHE_LOCK:
            try:
                disk = json.load(open(CACHE))
            except Exception:
                disk = {}
        for p in missing:
            if keys[p] in disk:
                _CAM[keys[p]] = disk[keys[p]]
        todo = [p for p in missing if keys[p] not in _CAM]
        if todo:
            with concurrent.futures.ThreadPoolExecutor(8) as ex:
                for p, info in zip(todo, ex.map(camera_info, todo)):
                    _CAM[keys[p]] = info
            with _CACHE_LOCK:
                try:
                    cache = json.load(open(CACHE))
                except Exception:
                    cache = {}
                cache.update({keys[p]: _CAM[keys[p]] for p in todo})
                try:
                    tmp = CACHE + ".tmp"
                    json.dump(cache, open(tmp, "w"), indent=1)
                    os.replace(tmp, CACHE)
                except Exception:
                    pass
    return {p: _CAM.get(keys.get(p), _UNKNOWN_CAM) for p in paths}


def camera_groups(paths):
    """Files grouped by the camera that recorded them and the resolution it recorded in (model + serial +
    size; frame rates stay together): one role per group. A camera that flies in one resolution and films the sticks
    in another shows up as two groups. The folders are listed for the user to recognise them.
    [{"name", "key", "formats": ["2.7K 29.97p"], "folders": {folder name: [paths]}}]"""
    def fmt(c):
        h = c["h"] or 0
        res = {2160: "4K", 1520: "2.7K", 2028: "2.7K", 2016: "2.7K", 1440: "1440p", 1080: "1080p",
               960: "960p", 720: "720p"}.get(h, f"{c['w']}×{h}")
        fps = c["fps"]
        return f"{res} {fps:g}p" if abs(fps - round(fps)) < 0.01 else f"{res} {fps:.2f}p"
    groups = {}
    infos = camera_infos(paths)
    for p in paths:
        c = infos[p]
        key = f"{c['name']}|{c['id']}|{c['w']}x{c['h']}"     # frame rate apart: a DVR switches PAL/NTSC
        g = groups.setdefault(key, {"name": c["name"], "key": key, "formats": [], "folders": {}, "size": [c["w"], c["h"]]})
        if fmt(c) not in g["formats"]:
            g["formats"].append(fmt(c))
        folder = os.path.basename(os.path.dirname(p)) or "(top)"
        folder = next((k for k in g["folders"] if k.lower() == folder.lower()), folder)   # "Stab" = "stab"
        g["folders"].setdefault(folder, []).append(p)
    return list(groups.values())


def file_number(path):
    """Recording order from the file name. GoPro numbers split files by chapter in front
    (GP01 0123, GP02 0123, GH01 0123…): the recording is the last four digits, the chapter the first two."""
    name = os.path.basename(path).upper()
    m = re.match(r"G[PHXL](\d\d)(\d{4})", name)
    if m:
        return int(m.group(2)) * 100 + int(m.group(1))
    m = re.match(r"GOPR(\d{4})", name)
    if m:
        return int(m.group(1)) * 100
    m = re.search(r"\d+", name)
    return int(m.group()) * 100 if m else -1


ROLES = {}                 # path -> "dvr" | "hd" | "stick" | "leave", from the panel's camera picker
ROLES_FILE = os.path.join(HERE, "camera_roles.json")      # camera group key -> role, remembered


def is_dvr(path, dvr_re):
    """DVR footage: the role the user gave its camera, else a file name matching the DVR pattern."""
    if path in ROLES:
        return ROLES[path] == "dvr"
    return bool(re.search(dvr_re, os.path.basename(path or ""), re.I))


def is_hd(path, dvr_re, hd_re=""):
    """HD footage: the role the user gave its camera; else any video that isn't a DVR recording, or
    matches the HD name pattern if one is set."""
    if path in ROLES:
        return ROLES[path] == "hd"
    name = os.path.basename(path or "")
    if re.search(dvr_re, name, re.I):
        return False
    return bool(re.search(hd_re, name, re.I)) if hd_re else bool(name)


def is_stick(path):
    return ROLES.get(path) == "stick"


def load_roles():
    try:
        return json.load(open(ROLES_FILE, encoding="utf-8"))
    except Exception:
        return {}


def apply_roles(paths, roles):
    """Set ROLES for these files from {camera group key: role}, and remember the roles."""
    ROLES.clear()
    if not roles:
        return
    for g in camera_groups(paths):
        r = roles.get(g["key"])
        if r:
            for ps in g["folders"].values():
                for p in ps:
                    ROLES[p] = r
    try:
        mem = load_roles()
        mem.update(roles)
        tmp = ROLES_FILE + ".tmp"
        json.dump(mem, open(tmp, "w", encoding="utf-8"), indent=1)
        os.replace(tmp, ROLES_FILE)
    except Exception:
        pass


def known_roles(groups):
    """Role of each camera group as the user set it before ({key: role}); a camera seen for the first
    time has none: the user picks it in the panel (the tool doesn't guess)."""
    mem = load_roles()
    return {g["key"]: mem.get(g["key"]) for g in groups}


def _ts(ct):
    import datetime
    try:
        return datetime.datetime.fromisoformat(ct[:19]).timestamp()
    except Exception:
        return None


def chain_files(paths, max_gap, follower_ok=lambda p, gap: True):
    """Group split recordings: the next file starts (by its own clock) right where the previous ended.
    follower_ok(path, gap_s) has the last word on joining."""
    chains = []
    for p in sorted(paths, key=lambda p: (os.path.dirname(p), file_number(p), os.path.basename(p))):
        if chains:
            q = chains[-1][-1]
            (dq, cq), (_, cp_) = media_info(q), media_info(p)
            tq, tp = _ts(cq), _ts(cp_)
            if tq is not None and tp is not None and -3 <= tp - (tq + dq) <= max_gap and follower_ok(p, tp - (tq + dq)):
                chains[-1].append(p)
                continue
        chains.append([p])
    return chains


SEAMLESS_S = 2.0             # a DVR file starting this soon after the last ended: the DVR split it itself
CLOCK_MIN = 1262304000.0     # 2010-01-01: an earlier date means the camera lost its clock
RUN_GAP = 50                 # file numbers; a bigger jump (or a reset) starts a new run


def _clock(path):
    t = _ts(media_info(path)[1])
    return t if t is not None and t >= CLOCK_MIN else None


def number_runs(chains):
    """One device's recordings split into runs: same folder, file numbers climbing without a big
    jump. Inside a run the numbers give the order; a counter reset or another card starts a new run."""
    byf = {}
    for ch in chains:
        byf.setdefault(os.path.dirname(ch[0]), []).append(ch)
    runs = []
    for _, chs in sorted(byf.items()):
        chs.sort(key=lambda ch: (file_number(ch[0]), os.path.basename(ch[0])))
        cur = []
        for ch in chs:
            if cur and file_number(ch[0]) // 100 - file_number(cur[-1][0]) // 100 > RUN_GAP:
                runs.append(cur)
                cur = []
            cur.append(ch)
        runs.append(cur)
    return [r for r in runs if r]


def chrono_keys(dchains, achains, links):
    """Every recording placed on one timeline, from all the clues together: each device's clock (when
    it looks set), the file numbers inside a run, and the matched pairs, which say how far the DVR
    clock is off. The HD camera's clock is the reference.
    links: (dvr chain, t_arm, HD chain, t_prop) per matched flight.
    Returns ({id(chain): seconds}, {id(chain) whose time comes from a clock that looks set})."""
    runs = [(r, "hd") for r in number_runs(achains)] + [(r, "dvr") for r in number_runs(dchains)]
    run_of = {id(ch): k for k, (r, _) in enumerate(runs) for ch in r}
    idx = {id(ch): i for r, _ in runs for i, ch in enumerate(r)}
    clock = {id(ch): _clock(ch[0]) for r, _ in runs for ch in r}
    hd_clocks = [clock[id(ch)] for ch in achains if clock.get(id(ch)) is not None]
    lo_ok, hi_ok = (min(hd_clocks) - 30 * 86400, max(hd_clocks) + 30 * 86400) if hd_clocks else (1, 0)

    # each pair votes on how far its DVR clock is off: HD time = DVR clock + offset
    votes = {}
    for dch, t_arm, ach, t_prop in links:
        kd, ca, cd = run_of.get(id(dch)), clock.get(id(ach)), clock.get(id(dch))
        if kd is not None and ca is not None and cd is not None:
            votes.setdefault(kd, []).append((idx[id(dch)], (ca + t_prop) - (cd + t_arm)))
    agreed, typical = {}, []           # per DVR run: the votes of its biggest agreeing group
    for kd in sorted(votes, key=lambda k: -len(votes[k])):
        vs = sorted(votes[kd], key=lambda v: v[1])
        groups_, cur = [], [vs[0]]
        for v in vs[1:]:
            if v[1] - cur[-1][1] <= 3600:
                cur.append(v)
            else:
                groups_.append(cur)
                cur = [v]
        groups_.append(cur)
        ref = float(np.median(typical)) if typical else 0.0
        best = max(groups_, key=lambda g: (len(g), -abs(float(np.median([v[1] for v in g])) - ref)))
        agreed[kd] = best
        typical.append(float(np.median([v[1] for v in best])))

    def mid_clock(k):
        c = [clock[id(ch)] for ch in runs[k][0] if clock[id(ch)] is not None]
        return float(np.median(c)) if c else None

    trip = float(np.median(typical)) if typical else 0.0

    def dvr_offset(k, i):
        if k in votes:                  # what the 3 pairs nearest to it in its run agree on: follows
            near = sorted(votes[k], key=lambda v: abs(v[0] - i))[:3]   # drift and per-session jumps
            near.sort(key=lambda v: v[1])
            groups_, cur = [], [near[0]]
            for v in near[1:]:
                if v[1] - cur[-1][1] <= 3600:
                    cur.append(v)
                else:
                    groups_.append(cur)
                    cur = [v]
            groups_.append(cur)
            best = max(groups_, key=lambda g: (len(g), -abs(float(np.median([v[1] for v in g])) - trip)))
            return float(np.median([v[1] for v in best]))
        mk = mid_clock(k)               # no pairs: off like the DVR run nearest in time
        known = [(abs(mid_clock(k2) - mk), float(np.median([v[1] for v in agreed[k2]]))) for k2 in agreed]
        return min(known)[1] if known else 0.0

    T, sure = {}, set()
    for k, (r, dev) in enumerate(runs):
        for i, ch in enumerate(r):
            c = clock[id(ch)]
            if c is None:
                continue
            T[id(ch)] = c if dev == "hd" else c + dvr_offset(k, i)
            if lo_ok <= c <= hi_ok:
                sure.add(id(ch))
    for _ in range(2):                  # no clock: the time of the recording it was paired with
        for dch, t_arm, ach, t_prop in links:
            if id(dch) not in T and id(ach) in T:
                T[id(dch)] = T[id(ach)] + t_prop - t_arm
            elif id(ach) not in T and id(dch) in T:
                T[id(ach)] = T[id(dch)] + t_arm - t_prop
    # still nothing: the file numbers - between its run neighbours, or after the run before it
    # in the same folder
    for rnd in range(len(runs) + 1):
        for k, (r, dev) in enumerate(runs):
            known = [i for i, ch in enumerate(r) if id(ch) in T]
            if not known:
                prev = [runs[k2][0] for k2 in range(k) if runs[k2][1] == dev
                        and os.path.dirname(runs[k2][0][0][0]) == os.path.dirname(r[0][0])
                        and any(id(ch) in T for ch in runs[k2][0])]
                anywhere = [T[id(ch)] for r2, _ in runs for ch in r2 if id(ch) in T]
                if not prev and anywhere and rnd < len(runs):
                    continue                # wait: an earlier run may still get a time first
                base = max([T[id(ch)] for ch in prev[-1] if id(ch) in T] if prev else anywhere or [0.0])
                T[id(r[0])] = base + 1.0
                known = [0]
            for i, ch in enumerate(r):
                if id(ch) in T:
                    continue
                lo = max((i2 for i2 in known if i2 < i), default=None)
                hi = min((i2 for i2 in known if i2 > i), default=None)
                if lo is not None and hi is not None:
                    a_, b_ = T[id(r[lo])], T[id(r[hi])]
                    T[id(ch)] = a_ + (b_ - a_) * (i - lo) / (hi - lo)
                elif lo is not None:
                    T[id(ch)] = T[id(r[lo])] + (i - lo)
                else:
                    T[id(ch)] = T[id(r[hi])] - (hi - i)
    for r, _ in runs:                   # ties (times filled in from the same neighbours): the file
        prev = None                     # numbers decide; a clock that clearly disagrees still wins
        for ch in r:
            if prev is not None and abs(T[id(ch)] - prev) < 1.0 and T[id(ch)] <= prev:
                T[id(ch)] = prev + 0.001
            prev = T[id(ch)]
    return T, sure


def motion_signal(path, start, dvr):
    """Frame-to-frame change over MOTION_DUR seconds, resampled to MOTION_HZ (cached on disk)."""
    os.makedirs(SIG_DIR, exist_ok=True)
    st = os.stat(path)
    key = os.path.join(SIG_DIR, f"{os.path.basename(path)}_{st.st_size}_{int(st.st_mtime)}_"
                                f"{start:.3f}_{MOTION_DUR:g}.npy")
    if os.path.exists(key):
        return np.load(key)
    w, h = (160, 120) if dvr else (160, 90)
    fr = _decode_small(path, start, MOTION_DUR + 1, w, h, gpu=not dvr)
    if dvr:
        fr = fr[:, 15:105, 20:140]                     # keep clear of most OSD text
    d = np.r_[0.0, np.abs(np.diff(fr, axis=0)).mean((1, 2))] if len(fr) > 1 else np.zeros(2)
    fps, _ = probe_fps(path)
    sig = np.interp(np.arange(0, MOTION_DUR, 1 / MOTION_HZ), np.arange(len(d)) / fps, d)
    np.save(key, sig)
    return sig


_DECODERS = [  # fastest first; the first one that works is remembered
    # GPU decodes and shrinks 4K to 960x540; the CPU does the last step (a direct GPU shrink to
    # 160x90 skips pixels and gives a noisy signal)
    (["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"],
     "scale_cuda=960:540,hwdownload,format=nv12,scale={w}:{h},format=gray"),
    (["-hwaccel", "auto"], "scale={w}:{h},format=gray"),
    ([], "scale={w}:{h},format=gray"),
]
_decoder_ok = None


def _decode_small(path, start, dur, w, h, gpu):
    """Gray w x h frames of a stretch of video; big footage goes through the GPU when it can."""
    global _decoder_ok
    order = list(range(len(_DECODERS))) if gpu else [len(_DECODERS) - 1]
    if gpu and _decoder_ok is not None:
        order.remove(_decoder_ok)
        order.insert(0, _decoder_ok)
    for k in order:
        pre, vf = _DECODERS[k]
        cmd = ["ffmpeg", "-v", "error"] + pre + ["-ss", f"{max(0.0, start):.3f}", "-i", path,
                                                 "-t", f"{dur:.3f}", "-map", "0:v:0",
                                                 "-vf", vf.format(w=w, h=h), "-f", "rawvideo", "-"]
        if gpu:                                  # big HD footage: take turns on the drive
            with DISK:
                r = subprocess.run(cmd, capture_output=True)
        else:
            r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0 and len(r.stdout) >= w * h * 2:
            if gpu:
                _decoder_ok = k
            return np.frombuffer(r.stdout, np.uint8)[: len(r.stdout) // (w * h) * w * h] \
                .reshape(-1, h, w).astype(np.float32)
    return np.zeros((0, h, w), np.float32)


def dvr_motion_long(path):
    """Frame-to-frame change over a whole DVR recording at MOTION_HZ (small decode, cached on disk)."""
    os.makedirs(SIG_DIR, exist_ok=True)
    st = os.stat(path)
    key = os.path.join(SIG_DIR, f"{os.path.basename(path)}_{st.st_size}_{int(st.st_mtime)}_whole.npy")
    if os.path.exists(key):
        return np.load(key)
    w, h = 160, 120
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-map", "0:v:0", "-vf", f"scale={w}:{h},format=gray",
           "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    d, prev, size = [0.0], None, w * h
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            f = np.frombuffer(buf, np.uint8).reshape(h, w)[15:105, 20:140].astype(np.float32)  # clear of most OSD
            if prev is not None:
                d.append(float(np.abs(f - prev).mean()))
            prev = f
    finally:
        proc.stdout.close()
        proc.wait()
    fps, _ = probe_fps(path)
    n = len(d)
    sig = np.interp(np.arange(0, n / fps, 1 / MOTION_HZ), np.arange(n) / fps, np.array(d)) if n > 1 else np.zeros(2)
    np.save(key, sig)
    return sig


def slide_match(short, long):
    """Best normalised correlation of `short` anywhere along `long`: (offset in s, score)."""
    n = len(short)
    if len(long) < n + 1 or n < 2:
        return 0.0, -1.0
    a = (short - short.mean()) / (short.std() + 1e-9)
    dot = np.correlate(long, a, "valid")                       # sum(long window * a)
    c1 = np.concatenate([[0.0], np.cumsum(long)])
    c2 = np.concatenate([[0.0], np.cumsum(long * long)])
    s1, s2 = c1[n:] - c1[:-n], c2[n:] - c2[:-n]
    sd = np.sqrt(np.maximum(s2 / n - (s1 / n) ** 2, 1e-12))
    sc = dot / (n * sd)
    k = int(np.argmax(sc))
    return k / MOTION_HZ, float(sc[k])


def motion_score(a, b, maxlag=int(0.4 * MOTION_HZ)):
    a = (a - a.mean()) / (a.std() + 1e-9)
    b = (b - b.mean()) / (b.std() + 1e-9)
    return max(float(np.mean(a[max(0, -L): len(a) - max(0, L)] * b[max(0, L): len(b) - max(0, -L)]))
               for L in range(-maxlag, maxlag + 1))


CLOCK_TOL = 2.5          # s; true pairs' clock gaps differ by well under this from flight to flight


class LazyMotion:
    """Motion match of DVR i (from its ARMED) and HD prop start j, worked out the first time it's
    asked for: each signal costs a short 4K (or DVR) decode, and most pairs never need one."""

    def __init__(self, D, A):
        self.D, self.A = D, A
        self.sd, self.sa, self.v = {}, {}, {}

    @property
    def shape(self):
        return len(self.D), len(self.A)

    def sig_d(self, i):
        if i not in self.sd:
            ch, t = self.D[i]
            self.sd[i] = motion_signal(ch[0], t + MOTION_T0, True)
        return self.sd[i]

    def sig_a(self, j):
        if j not in self.sa:
            ch, t = self.A[j]
            self.sa[j] = motion_signal(ch[0], t + MOTION_T0, False)
        return self.sa[j]

    def __getitem__(self, key):
        r, c = key
        if isinstance(r, np.ndarray) or isinstance(c, np.ndarray):
            rows, cols = np.asarray(r).ravel(), np.asarray(c).ravel()
            return np.array([[self[int(i), int(j)] for j in cols] for i in rows]).reshape(len(rows), len(cols))
        i, j = int(r), int(c)
        if (i, j) not in self.v and stop_requested():
            return 0.0
        if (i, j) not in self.v:
            self.v[(i, j)] = motion_score(self.sig_d(i), self.sig_a(j))
        return self.v[(i, j)]


def _clock_pairs(G, M, chain_id, tol=CLOCK_TOL, min_size=3):
    """Clock periods: (DVR clock + ARMED) - (HD clock + prop start) is the same for every true pair
    between two clock resets, so true pairs pile up in a tight cluster of gaps while coincidences
    spread thin. Peel the densest cluster, pair one-to-one inside it by closeness to its gap, repeat.
    Returns ([(i, j, "clock")], [period: [(gap, i, j)]]); motion isn't needed for this."""
    ii, jj = np.where(np.isfinite(G))
    items = sorted((float(G[i, j]), int(i), int(j)) for i, j in zip(ii, jj))
    remaining = list(items)
    cands = []
    for _ in range(30):
        if len(remaining) < min_size:
            break
        gs = [x[0] for x in remaining]
        best, e = None, 0
        for k in range(len(remaining)):                   # densest 2*tol window, in distinct DVRs
            e = max(e, k)
            while e + 1 < len(remaining) and gs[e + 1] - gs[k] <= 2 * tol:
                e += 1
            n = len({x[1] for x in remaining[k:e + 1]})
            if best is None or n > best[0]:
                best = (n, k, e)
        n, k, e = best
        if n < min_size:
            break
        center = float(np.median(gs[k:e + 1]))
        core = [x for x in remaining if abs(x[0] - center) <= tol]
        sel, gd, ga = [], set(), set()
        for g, i, j in sorted(core, key=lambda x: abs(x[0] - center)):
            if i in gd or chain_id(j) in ga:
                continue
            sel.append((g, i, j))
            gd.add(i)
            ga.add(chain_id(j))
        if len(sel) >= min_size:
            cands.append(sel)
        remaining = [x for x in remaining if abs(x[0] - center) > tol]
    chosen, kept_groups, used_d, used_a = [], [], set(), set()
    for sel in sorted(cands, key=len, reverse=True):
        kept = [(g, i, j) for g, i, j in sel if i not in used_d and chain_id(j) not in used_a]
        if len(kept) < min_size:
            continue
        for g, i, j in kept:
            used_d.add(i)
            used_a.add(chain_id(j))
            chosen.append((i, j, "clock"))
        kept_groups.append(kept)
    alts = {}
    for g, i, j in items:
        alts.setdefault((int(i), chain_id(j)), []).append((float(g), int(j)))
    # clock periods follow each other in time. A flight whose neighbours on both sides belong to
    # another period got into its own by a wrong prop start (an arm-disarm-rearm that happens to
    # line up): it moves to the neighbours' period if one of its prop starts fits there.
    for _ in range(3):
        where = sorted((x[1], gi, x) for gi, grp in enumerate(kept_groups) for x in grp)
        moved = False
        for k, (i, gi, x) in enumerate(where):
            if k == 0 or k == len(where) - 1:
                continue
            (_, gp, xp), (_, gn, xn) = where[k - 1], where[k + 1]
            if gp != gn or gp == gi:
                continue
            local = (xp[0] + xn[0]) / 2
            fits = [y for y in alts[(i, chain_id(x[2]))] if abs(y[0] - local) <= tol]
            if fits:
                g2, j2 = min(fits, key=lambda y: abs(y[0] - local))
                kept_groups[gi].remove(x)
                kept_groups[gp].append((g2, i, j2))
                moved = True
        if not moved:
            break
    kept_groups = [sorted(grp, key=lambda x: x[1]) for grp in kept_groups if len(grp) >= min_size]
    chosen, fixed_groups = [], []
    for grp in kept_groups:
        new = []
        for g, i, j in grp:
            near = sorted((x for x in grp if x[1] != i), key=lambda x: abs(x[1] - i))[:4]
            if near:
                local = float(np.median([x[0] for x in near]))
                fits = [a for a in alts[(i, chain_id(j))] if abs(a[0] - local) <= 1.5]
                if abs(g - local) > 3.0 and fits:
                    g, j = min(fits, key=lambda a: abs(a[0] - local))
            new.append((g, i, j))
            chosen.append((i, j, "clock"))
        fixed_groups.append(new)
    return sorted(chosen), fixed_groups


def armed_near(path, t, win=2.5):
    """Start of an ARMED within +-win s of t (frame-exact), or None."""
    fps, _ = probe_fps(path)
    lo = max(0.0, t - win)
    run_start, n = None, 0
    for k, f in dvr_frames(path, start=lo, dur=2 * win):
        if armed_score(f) >= ARM_THRESHOLD:
            run_start = k if run_start is None else run_start
            n += 1
            if n >= ARM_MIN_RUN:
                return lo + run_start / fps
        else:
            run_start, n = None, 0
    return None


def refine_by_motion(dvr_path, t_arm, act_path, t_guess, search=3.0):
    """Takeoff time in the HD recording from motion alone, around a clock-based guess."""
    ds = motion_signal(dvr_path, t_arm + MOTION_T0, True)
    start = max(0.0, t_guess + MOTION_T0 - search)
    fr = _decode_small(act_path, start, MOTION_DUR + 2 * search + 1, 160, 90, gpu=True)
    if len(fr) < 2:
        return t_guess, -1.0
    d = np.r_[0.0, np.abs(np.diff(fr, axis=0)).mean((1, 2))]
    fps, _ = probe_fps(act_path)
    long = np.interp(np.arange(0, MOTION_DUR + 2 * search, 1 / MOTION_HZ), np.arange(len(d)) / fps, d)
    n = len(ds)
    a = (ds - ds.mean()) / (ds.std() + 1e-9)
    best = (-2.0, 0)
    for lag in range(0, len(long) - n + 1):
        b = long[lag: lag + n]
        sc = float(np.mean(a * (b - b.mean()) / (b.std() + 1e-9)))
        if sc > best[0]:
            best = (sc, lag)
    return start + best[1] / MOTION_HZ - MOTION_T0, best[0]


FIRST_LISTEN = 45          # s of HD audio read first: 95 % of 185 hand-synced takeoffs are in it (30 s: 84 %, lost Bando pairs)
REUSE_75 = True            # tests set False to read as a first run would


def find_prop_starts(path, whole=True, first=75):
    """Prop starts near the beginning of a recording (its first `first` s; what an earlier run heard
    in the first 75 s if that's known); the whole file if none (and whole=True)."""
    found = None
    if first < 75 and REUSE_75:
        hit = peek_cache(f"{PROPS}@0+75", path)
        if hit is not None and hit != NONE:
            found = hit
    if found is None:
        found = cached(f"{PROPS}@0+{first}", path, lambda: detect_prop_onsets(path, start=0, dur=first))
    if not found and whole:
        found = cached(PROPS, path, lambda: detect_prop_onsets(path))
    return [t for t, _ in found]


WEAK_JUMP_DB = 12.0        # second listen for silent clips: noisy places (race events) hide the props


def listen_weaker(path):
    """Prop starts in the first listen's stretch with a lower bar (12 dB instead of 18): for clips where
    nothing was heard, e.g. when crowd or other quads keep the background loud."""
    return [t for t, _ in cached(f"{PROPS}@0+{FIRST_LISTEN}@{WEAK_JUMP_DB:g}dB", path,
                                 lambda: detect_prop_onsets(path, jump_db=WEAK_JUMP_DB, start=0, dur=FIRST_LISTEN))]


def listen_further(path):
    """Prop starts in the rest of the first 75 s, after a short first listen (overlapping it a little,
    so a start right at the edge isn't lost)."""
    if FIRST_LISTEN >= 75:
        return []
    a = FIRST_LISTEN - 5
    if media_info(path)[0] <= FIRST_LISTEN:          # already heard all of it
        return []
    return [t for t, _ in cached(f"{PROPS}@{a}+{75 - a}", path,
                                 lambda: detect_prop_onsets(path, start=a, dur=75 - a))]


MACHINE = os.path.join(HERE, "machine.json")


def machine_pace():
    """Model-seconds this PC gets through per real second, per side ({} before the first run)."""
    try:
        return json.load(open(MACHINE)).get("pace", {})
    except Exception:
        return {}


def learn_pace(model, finished):
    """After a run: blend this run's pace per side into what's known about this PC."""
    try:
        m = json.load(open(MACHINE))
    except Exception:
        m = {"pace": {}, "runs": 0}
    for side in ("hd", "dvr"):
        if model.get(side, 0) > 20 and finished.get(side, 0) > 5:       # skip mostly-cached runs
            new = model[side] / finished[side]
            old = m["pace"].get(side)
            m["pace"][side] = round(new if old is None else 0.5 * old + 0.5 * new, 3)
    m["runs"] = m.get("runs", 0) + 1
    try:
        json.dump(m, open(MACHINE, "w"), indent=1)
    except Exception:
        pass


def predict_costs(dvr_paths, act_paths, disk=None):
    """Expected seconds per file, from its size and length, the drive's speed, and what's cached.
    Calibrated on cold runs (Aug Dolomites, HDD 168 MB/s): an HD recording costs its first 75 s of
    audio read (~1.1x the bytes / drive speed) plus a 16 s 4K decode (~2.5 s on the GPU); a DVR is
    read in one go (bytes / drive speed) then scanned for ARMED at ~0.17 s per DVR second."""
    disk = disk or 150.0
    hd_cost, dvr_cost = {}, {}
    achains = chain_files(act_paths, 3)              # metadata only: which HD files are one recording
    for ch in achains:
        d_, _ = media_info(ch[0])
        mbps = os.path.getsize(ch[0]) / (1 << 20) / max(1.0, d_)                  # footage MB per second
        if peek_cache(f"{PROPS}@0+75", ch[0]) is not None:
            hd_cost[ch[0]] = 0.1                                                  # analysed before
        else:
            hd_cost[ch[0]] = 1.1 * min(75.0, d_) * mbps / disk + 0.8 + max(2.5, 1.1 * 16 * mbps / disk)
        for p_ in ch[1:]:
            hd_cost[p_] = 0.05
    for p_ in dvr_paths:
        d_, _ = media_info(p_)
        if peek_cache(ARMED, p_) is not None:
            dvr_cost[p_] = 0.1
        else:
            dvr_cost[p_] = os.path.getsize(p_) / (1 << 20) / disk + 0.17 * min(60.0, d_) + 1.2
    return achains, hd_cost, dvr_cost


def estimate_actions(job):
    """Seconds each panel button would take on the current selection (no footage is read)."""
    dvr_re, act_re = job.get("dvr", DVR_NAME), job.get("action", "")
    clips = [c for c in job.get("clips", []) if c.get("kind") == "video" and c.get("path")]
    is_dvr = lambda c: re.search(dvr_re, os.path.basename(c["path"]), re.I)
    is_act = lambda c: is_hd(c["path"], dvr_re, act_re)
    sel = [c for c in clips if c.get("selected")]
    paths = list(dict.fromkeys(c["path"] for c in sel))
    pace = machine_pace()
    if not pace:
        return {"learned": False}                  # no estimates until this PC has done one run
    disk = drive_speed(paths or [c["path"] for c in clips])
    out = {"disk_MBps": disk, "learned": True}
    # Match & sync selected: the two sides run side by side, then placing (~0.25 s a clip) + corner
    if sel:
        dv = [p for p in paths if re.search(dvr_re, os.path.basename(p), re.I)]
        ac = [p for p in paths if is_hd(p, dvr_re, act_re)]
        _, hc, dc = predict_costs(dv, ac, disk)
        out["match"] = round(max(sum(hc.values()) / pace.get("hd", 1.0), sum(dc.values()) / pace.get("dvr", 1.0))
                             + 0.25 * len(paths) + 0.6 * len(dv) + 2)
        out["match_cached"] = all(v <= 0.1 for v in list(hc.values()) + list(dc.values()))
    # Sync selected: the picked flights, both sides at once (like Match & sync), then lining up
    sel_act = [a for a in clips if a.get("selected") and is_act(a)]
    flights = [d for d in clips if is_dvr(d) and (d.get("selected") or any(
        min(d["end"], a["end"]) > max(d["start"], a["start"]) for a in sel_act))]
    if flights:
        groups = [[d] for d in flights]
        acts = [a for a in clips if is_act(a)]
        dvr_files, hd_files = flight_files(groups, acts)
        _, hc, dc = predict_costs(dvr_files, hd_files, disk)
        share = (2 * float(job.get("prior_window", 30.0)) + 6) / 75.0
        hd_s = sum(max([hc.get(a["path"], 0.1) for a in acts if a.get("path")
                        and min(d["end"], a["end"]) > max(d["start"], a["start"])] or [0.1]) * share
                   for d in flights)
        out["sync"] = round(max(sum(dc.values()) / pace.get("dvr", 1.0), hd_s / pace.get("hd", 1.0))
                            + 0.3 * len(flights))
        out["sync_cached"] = all(v <= 0.1 for v in list(hc.values()) + list(dc.values()))
    # DVR in the Corner: selected DVRs, or all of them
    n = len([c for c in sel if is_dvr(c)]) or len([c for c in clips if is_dvr(c)])
    out["corner"] = round(0.6 * n + 0.5) if n else None
    return out


def armed_early(path, within=60.0):
    """ARMED in the first minute? (a split continuation picks up mid-flight and never shows it;
    a new flight that started right after a false-start recording does)"""
    full = peek_cache(ARMED, path)
    if full is not None and full != NONE:
        return full[0] <= within
    hit = cached(f"{ARMED}_first{int(within)}", path, lambda: _armed_runs(path, first_only=True, dur=within))
    if hit:                                 # that is its first ARMED: don't look for it again later
        cached(ARMED, path, lambda: tuple(hit[0]))
    return bool(hit)


def dvr_continues(prev, path, max_gap=15):
    """Does this DVR recording start (by its own clock) right after the previous one ends?"""
    try:
        (d0, c0), (_, c1) = media_info(prev), media_info(path)
        t0, t1 = _ts(c0), _ts(c1)
        return t0 is not None and t1 is not None and -3 <= t1 - (t0 + d0) <= max_gap
    except Exception:
        return False


PAL_EXTRA_LEAD = 1        # frames; measured on Bando (PAL Orqa DVR, GoPro): median +1 vs hand sync


def dvr_lead(path, lead):
    """Frames ARMED leads the prop sound for this DVR: one more for PAL (50 fps) recordings."""
    try:
        fps, _ = probe_fps(path)
        return lead + (PAL_EXTRA_LEAD if abs(fps - 50) < 1 else 0)
    except Exception:
        return lead


def flight_files(groups, act):
    """The DVR files of the picked flights and the HD files under them."""
    dvr_files = list(dict.fromkeys(c["path"] for g in groups for c in g if c.get("path")))
    hd_files = list(dict.fromkeys(
        a["path"] for g in groups for a in act
        if min(g[-1]["end"], a["end"]) > max(g[0]["start"], a["start"]) and a.get("path")))
    return [p for p in dvr_files if os.path.exists(p)], [p for p in hd_files if os.path.exists(p)]


def head_props(path, further=False):
    """(time, jump) prop starts in an HD clip's first 45 s, or (further=True) in the rest of its first
    75 s: the same listens and cache entries Match & sync uses, so a matched trip syncs from cache."""
    full = peek_cache(PROPS, path)
    if full is not None:
        return [] if full == NONE else full
    first75 = peek_cache(f"{PROPS}@0+75", path)
    if first75 is not None:
        return [] if further or first75 == NONE else first75
    if not further:
        return cached(f"{PROPS}@0+{FIRST_LISTEN}", path,
                      lambda: detect_prop_onsets(path, start=0, dur=FIRST_LISTEN)) or []
    a = FIRST_LISTEN - 5
    if FIRST_LISTEN >= 75 or media_info(path)[0] <= FIRST_LISTEN:
        return []
    return cached(f"{PROPS}@{a}+{75 - a}", path, lambda: detect_prop_onsets(path, start=a, dur=75 - a)) or []


SYNC_WIDE = 90.0       # s around a roughly placed DVR's ARMED, heard only if the HD clip's first 75 s have no
                       # prop start (a takeoff later in the recording)


def sync_window(a, arm_seq, fd, prior_window):
    """Sync selected reads an HD clip's audio around where the DVR's ARMED sits now:
    (predicted source time, window start, window length) in seconds."""
    pred = a["src0"] + (arm_seq - a["start"]) * fd
    return pred, round(max(0.0, pred - prior_window - 3)), round(2 * prior_window + 6)


def prewarm_flights(groups, act, fd, prior_window):
    """Sync selected, step 1: each picked flight's first ARMED (the head file only), then just the
    stretch of HD audio that ARMED points at, in whichever clip it falls in - the same windows the
    sync step reads, so lining up then runs on cached results. The DVRs and the HD reads overlap."""
    import queue
    heads = [g[0] for g in groups if g[0].get("path") and os.path.exists(g[0]["path"])]
    if not heads:
        return
    over_of = lambda g: [x for x in act if min(g[-1]["end"], x["end"]) > max(g[0]["start"], x["start"])
                         and x.get("path") and os.path.exists(x["path"])]
    _, hc, dc = predict_costs([h["path"] for h in heads],
                              list(dict.fromkeys(x["path"] for g in groups for x in over_of(g))), DISK_MBPS)
    win_share = FIRST_LISTEN / 75.0
    hd_guess = {id(g): max([hc.get(x["path"], 0.1) for x in over_of(g)] or [0.1]) * win_share for g in groups}
    est = {"dvr": max(1e-6, sum(dc.get(h["path"], 0.1) for h in heads)),
           "hd": max(1e-6, sum(hd_guess.values())), "dvr_done": 0.0, "hd_done": 0.0,
           "dvr_cur": None, "hd_cur": None, "label": "Reading the DVR", "stop": False}
    pace = machine_pace()
    t0 = time.time()
    lock = threading.Lock()

    def report():
        with lock:
            e = max(1e-6, time.time() - t0)
            left = 0.0
            for sd in ("dvr", "hd"):
                d_ = est[sd + "_done"]
                if est[sd + "_cur"]:                 # the file in hand counts as far as its expected time
                    d_ += min(est[sd + "_cur"][1] * 0.95, time.time() - est[sd + "_cur"][0])
                w = min(1.0, d_ / est[sd] / 0.8)
                rate = (1 - w) * pace.get(sd, 1.0) + w * d_ / e
                left = max(left, (est[sd] - d_) / max(0.05, rate))
            label = est["label"]
        progress(0.85 * e / (e + left), label, left + 0.3 * len(groups))

    def begin(side, cost, label):
        with lock:
            est[side + "_cur"] = (time.time(), cost)
            est["label"] = label
        report()

    def end(side, cost):
        with lock:
            est[side + "_done"] += cost
            est[side + "_cur"] = None
        report()

    def ticker():                       # one cheap update a second, so the bar moves inside long files
        while not est["stop"]:
            time.sleep(1.0)
            if not est["stop"]:
                report()

    found = queue.Queue()

    def dvr_part():
        try:
            for g in groups:
                if stop_requested():
                    break
                d = g[0]
                if d not in heads:
                    continue
                cost = dc.get(d["path"], 1.0)
                begin("dvr", cost, f"DVR {os.path.basename(d['path'])}: finding ARMED")
                try:
                    arm = cached(ARMED, d["path"], lambda: detect_armed(d["path"]))
                except Exception:
                    arm = None
                end("dvr", cost)
                found.put((g, arm))
        finally:
            found.put(None)

    th = threading.Thread(target=dvr_part, name="dvr")
    tk = threading.Thread(target=ticker, name="ticker", daemon=True)
    th.start()
    tk.start()
    try:
        while True:
            item = found.get()
            if item is None:
                break
            g, arm = item
            guess = hd_guess[id(g)]
            if not arm:
                end("hd", guess)
                continue
            d = g[0]
            arm_seq = d["start"] + (arm[0] - d["src0"]) / fd
            spent = 0.0
            for x in over_of(g):
                if (peek_cache(PROPS, x["path"]) is not None or peek_cache(f"{PROPS}@0+75", x["path"]) is not None
                        or peek_cache(f"{PROPS}@0+{FIRST_LISTEN}", x["path"]) is not None):
                    continue                    # already heard
                if stop_requested():
                    continue
                cost = hc.get(x["path"], 0.1) * win_share
                begin("hd", cost, f"HD {os.path.basename(x['path'])}: listening for the takeoff")
                try:
                    head_props(x["path"])
                except Exception:
                    pass
                with lock:
                    est["hd_cur"] = None
                spent += cost
            end("hd", max(guess, spent) if spent else guess)
    finally:
        th.join()
        est["stop"] = True


def match_flights(dvr_paths, act_paths, log=print, order=None):
    """Pair DVR recordings with HD recordings.
    Returns (pairs, unmatched_dvr_chains, unmatched_action_chains); a pair is a dict."""
    armed = lambda p: cached(ARMED, p, lambda: detect_armed(p))
    log(f"Reading {len(dvr_paths)} DVR and {len(act_paths)} HD files…")
    D, A = [], []                          # D: (chain, t_arm)  A: (chain, t_prop), one per prop start
    no_arm = []                            # DVR recordings that never show ARMED (started mid-flight)
    quiet = []                             # HD recordings with no prop start in their first 75 s
    out = {}
    # time model: each file's expected cost (s) from its size, length and the drive's speed;
    # the live time-left rescales it by how fast this machine is actually going
    achains_pre, hd_cost, dvr_cost = predict_costs(dvr_paths, act_paths, DISK_MBPS)
    est = {"hd": sum(hd_cost.values()), "dvr": sum(dvr_cost.values()), "hd_done": 0.0, "dvr_done": 0.0,
           "hd_cur": None, "dvr_cur": None, "hd_end": 0.0, "dvr_end": 0.0,
           "label": "Reading footage", "t0": time.time(), "stop": False}
    learned = machine_pace()
    plock = threading.Lock()

    def time_left():
        e = time.time() - est["t0"]
        left = 0.0
        for side in ("hd", "dvr"):
            d = est[side + "_done"]
            cur = est[side + "_cur"]                           # (start time, model cost) of the file in hand
            if cur:
                d += min(cur[1], time.time() - cur[0])
            total = max(1e-6, est[side])
            w = min(1.0, d / total / 0.8)                      # trust the live pace more as work gets done
            live = d / max(e, 1e-6)
            rate = (1 - w) * learned.get(side, 1.0) + w * live  # model-seconds per real second
            left = max(left, (est[side] - d) / max(0.05, rate))
        return e, left

    def report():
        e, left = time_left()
        progress(0.95 * e / max(1e-6, e + left), est["label"], left)

    def tick(cost, side, label):
        with plock:
            est[side + "_done"] += cost
            est[side + "_cur"] = None
            est[side + "_end"] = time.time() - est["t0"]
            est["label"] = label
        report()

    def start(side, cost, label):
        with plock:
            est[side + "_cur"] = (time.time(), cost)
            est["label"] = label

    def ticker():                       # one cheap update a second, so the clock and estimate keep moving
        while not est["stop"]:
            time.sleep(1.0)
            if not est["stop"]:
                report()

    credited = set()

    def follower_ok(p, gap):               # starts within 15 s of the previous DVR ending: same flight,
        if gap <= SEAMLESS_S:              # unless it shows ARMED in its first minute; but a file the
            return True                    # DVR split on its own (no gap) is one recording, re-arm or not
        start("dvr", 8.0, f"DVR {os.path.basename(p)}")
        res = not armed_early(p)
        credited.add(p)
        tick(dvr_cost.get(p, 1.0), "dvr", f"DVR {os.path.basename(p)}")
        return res

    def dvr_side():                        # mostly CPU: find ARMED, short DVR clips
        dchains = chain_files(dvr_paths, 15, follower_ok=follower_ok)
        out["dchains"] = dchains
        for i, ch in enumerate(dchains):
            if stop_requested():
                UNREAD.update(c[0] for c in dchains[i:])
                for c in dchains[i:]:
                    ev(c[0], "arm", "bad", "not analysed", done="unread")
                log(f"Stopped: {len(dchains) - i} DVR recording(s) not analysed")
                break
            log(f"DVR {i + 1}/{len(dchains)}: {os.path.basename(ch[0])}")
            todo = [p_ for p_ in ch if p_ not in credited]
            for p_ in ch[1:]:
                ev(p_, "arm", "ok", f"continues {os.path.basename(ch[0])}", done="part")
            if is_skipped(ch[0]):
                SKIPPED_SEEN.add(ch[0])
                ev(ch[0], done="skipped")
                continue
            start("dvr", sum(dvr_cost.get(p_, 1.0) for p_ in todo), f"DVR {os.path.basename(ch[0])}")
            ev(ch[0], "arm", "run", "finding ARMED")
            _abort.on = True
            try:
                a = armed(ch[0])
            except SkipFile:                # skipped while its scan ran
                SKIPPED_SEEN.add(ch[0])
                ev(ch[0], done="skipped")
                continue
            finally:
                _abort.on = False
            if a:
                D.append((ch, a[0]))
                ev(ch[0], "arm", "ok", f"ARMED {mmss(a[0])}")
            else:
                no_arm.append(ch)
                ev(ch[0], "arm", "bad", "no ARMED")
            tick(sum(dvr_cost.get(p_, 1.0) for p_ in todo), "dvr", f"DVR {os.path.basename(ch[0])}")

    def action_side():                     # mostly disk: audio + a short stretch of 4K per flight
        achains = achains_pre
        out["achains"] = achains
        for i, ch in enumerate(achains):
            if stop_requested():
                UNREAD.update(c[0] for c in achains[i:])
                for c in achains[i:]:
                    ev(c[0], "arm", "bad", "not analysed", done="unread")
                log(f"Stopped: {len(achains) - i} HD recording(s) not analysed")
                break
            log(f"HD {i + 1}/{len(achains)}: {os.path.basename(ch[0])}")
            for p_ in ch[1:]:
                ev(p_, "arm", "ok", f"continues {os.path.basename(ch[0])}", done="part")
            if is_skipped(ch[0]):
                SKIPPED_SEEN.add(ch[0])
                ev(ch[0], done="skipped")
                continue
            start("hd", sum(hd_cost.get(p_, 1.0) for p_ in ch), f"HD {os.path.basename(ch[0])}")
            ev(ch[0], "arm", "run", "listening")
            _abort.on = True
            try:
                cues = find_prop_starts(ch[0], whole=False, first=FIRST_LISTEN)
            except SkipFile:                # skipped while it listened
                SKIPPED_SEEN.add(ch[0])
                ev(ch[0], done="skipped")
                continue
            finally:
                _abort.on = False
            ev(ch[0], "arm", "ok" if cues else "bad",
               (f"props {mmss(cues[0])}" + (f" +{len(cues) - 1}" if len(cues) > 1 else "")) if cues else "no props")
            if not cues:
                quiet.append(ch)           # nothing heard yet: listen harder only if the clocks can't help
            for t in cues:
                A.append((ch, t))
            # a split file's continuation costs almost nothing; its head carries the work
            tick(sum(hd_cost.get(p_, 1.0) for p_ in ch), "hd", f"HD {os.path.basename(ch[0])}")

    errors = []

    def guard(fn):
        try:
            fn()
        except Exception as e:             # re-raised below, in this thread
            errors.append(e)

    th = threading.Thread(target=guard, args=(dvr_side,), name="dvr")
    tk = threading.Thread(target=ticker, name="ticker", daemon=True)
    th.start()
    tk.start()
    try:
        guard(action_side)
        th.join()
    finally:
        est["stop"] = True
    if errors:
        raise errors[0]
    learn_pace({"hd": est["hd"], "dvr": est["dvr"]}, {"hd": est["hd_end"], "dvr": est["dvr_end"]})
    dchains, achains = out["dchains"], out["achains"]
    for lst in (D, A):
        drop = [x for x in lst if is_skipped(x[0][0])]
        for x in drop:
            lst.remove(x)
            SKIPPED_SEEN.add(x[0][0])
            ev(x[0][0], done="skipped")
    for lst in (no_arm, quiet):
        for ch in [c for c in lst if is_skipped(c[0])]:
            lst.remove(ch)
            SKIPPED_SEEN.add(ch[0])
            ev(ch[0], done="skipped")
    log(f"{len(dchains)} DVR recordings, {len(achains)} HD recordings (split files joined)")
    progress(0.95, "Stopped: lining up what's analysed" if stop_requested() else "Matching flights", 1.0)
    M = LazyMotion(D, A)                   # motion compared only where it's needed

    nd, na = M.shape
    chain_id = lambda j: id(A[j][0])
    TD = [_ts(media_info(ch[0])[1]) for ch, _ in D]          # recording clock times
    TA = [_ts(media_info(ch[0])[1]) for ch, _ in A]

    # 1. clock agreement: for true pairs, (DVR clock at ARMED) - (HD clock at prop start) is the
    #    same for every flight recorded between two clock resets, to within a few seconds
    G = np.full((nd, na), np.nan)
    for i in range(nd):
        for j in range(na):
            if TD[i] is not None and TA[j] is not None:
                G[i, j] = (TD[i] + D[i][1]) - (TA[j] + A[j][1])
    got, groups = _clock_pairs(G, M, chain_id)
    if not groups and (quiet or FIRST_LISTEN < 75) and not stop_requested():
        # the clocks can't say where the flights are: listen further into the HD recordings
        log(f"Clocks don't line up: listening further into the HD recordings")
        progress(0.95, "Clocks don't line up: listening further into the HD clips", 3.0 * len(achains))
        for ch in achains:
            if skip_now(ch[0]) or stop_requested():
                continue
            have = [t for c_, t in A if c_ is ch]
            if not have:
                ev(ch[0], "more", "run", "Listening further")
            new = [t for t in listen_further(ch[0]) if all(abs(t - h) > 0.1 for h in have)]
            if not have:                       # nothing at 18 dB: listen again with a lower bar (noisy
                new += [t for t in listen_weaker(ch[0]) if all(abs(t - h) > 0.1 for h in new)]  # places)
                ev(ch[0], "more", "ok" if new else "bad", f"props {mmss(new[0])}" if new else "Listening further")
            for t in new:
                A.append((ch, t))
        nd, na = M.shape
        TA = [_ts(media_info(ch[0])[1]) for ch, _ in A]
        G = np.full((nd, na), np.nan)
        for i in range(nd):
            for j in range(na):
                if TD[i] is not None and TA[j] is not None:
                    G[i, j] = (TD[i] + D[i][1]) - (TA[j] + A[j][1])
        got, groups = _clock_pairs(G, M, chain_id)
    if groups:
        log(f"Clocks agree on {len(got)} flights ({len(groups)} clock period(s))")

    def gaps_for(i):
        """clock gap per clock period, taken from the matched flight closest in time to DVR i"""
        out = []
        for grp in groups:
            if TD[i] is None:
                continue
            dt, g = min((abs(TD[i2] - TD[i]), g) for g, i2, _ in grp)
            out.append((dt, g))
        return [g for _, g in sorted(out)]

    extra = []                                                # pairs found from the clock alone
    if groups and not stop_requested():
        used_a = {chain_id(j) for _, j, _ in got}
        used_d = {i for i, _, _ in got}
        left = [i for i in range(nd) if i not in used_d]
        for i in range(nd):
            if stop_requested():
                break
            if i in used_d:
                continue
            k_ = left.index(i)
            progress(0.95 + 0.05 * (k_ / max(1, len(left))), f"Double-checking {os.path.basename(D[i][0][0])}",
                     12.0 * (len(left) - k_))
            gs = gaps_for(i)
            # a) its first ARMED fits a free HD prop start we already heard
            js = [j for j in range(na) if chain_id(j) not in used_a and np.isfinite(G[i, j])
                  and any(abs(G[i, j] - g) <= 2 * CLOCK_TOL for g in gs)]
            if js:
                j = max(js, key=lambda j: M[i, j])
                got.append((i, j, "clock")); used_d.add(i); used_a.add(chain_id(j))
                continue
            # b) armed more than once, took off late, or the prop sound is masked: try every ARMED in
            #    the DVR against every free HD recording, where each clock period puts it
            dpath = D[i][0][0]
            dd = media_info(dpath)[0]

            def listen(runs):
                heard, unheard = [], []
                for g in gs:
                    for r in runs:
                        for ch in achains:
                            ta = _ts(media_info(ch[0])[1])
                            if id(ch) in used_a or ta is None or skip_now(ch[0]):
                                continue
                            t_p = TD[i] + r[0] - ta - g
                            if not 0 < t_p < media_info(ch[0])[0] - 1:
                                continue
                            ons = [t for t, _ in detect_prop_onsets(ch[0], start=max(0.0, t_p - 4), dur=8)
                                   if abs(t - t_p) <= 2 * CLOCK_TOL]
                            if ons:
                                heard.append((r[0], ch, min(ons, key=lambda t: abs(t - t_p))))
                            else:
                                unheard.append((r[0], ch, t_p))
                return heard, unheard

            # its first ARMED first: no scanning needed
            heard, unheard = listen([[D[i][1]]])
            if not heard:
                # then every other ARMED, only where a free HD recording overlaps this DVR
                spans = []
                for g in gs:
                    for ch in achains:
                        ta = _ts(media_info(ch[0])[1])
                        if id(ch) in used_a or ta is None or skip_now(ch[0]):
                            continue
                        a0 = ta + g - TD[i]              # that HD recording's start, in this DVR's time
                        a1 = a0 + media_info(ch[0])[0]
                        if a1 > 0 and a0 < dd:
                            spans.append((a0 - 2 * CLOCK_TOL, a1 + 2 * CLOCK_TOL))
                more = [r for r in (armed_runs_in(dpath, spans) if spans else []) if abs(r[0] - D[i][1]) > 0.05]
                if more:
                    heard, unheard = listen(sorted([[D[i][1]]] + more, key=lambda r: r[0]))
            if heard:
                t_arm, ch, t_prop = heard[0]
                sc = motion_score(motion_signal(dpath, t_arm + MOTION_T0, True),
                                  motion_signal(ch[0], t_prop + MOTION_T0, False))
                extra.append({"dvr": D[i][0], "t_arm": t_arm, "action": ch, "t_prop": t_prop,
                              "score": round(sc, 2), "how": "clock" if t_arm == D[i][1] else "clock, re-armed"})
                used_d.add(i); used_a.add(id(ch))
                continue
            # c) nothing heard where the clock says: line the takeoff up by motion instead
            best = None
            for t_arm, ch, t_p in unheard[:6]:
                if t_p > media_info(ch[0])[0] - MOTION_DUR - 4:
                    continue
                t_prop, sc = refine_by_motion(dpath, t_arm, ch[0], t_p)
                if best is None or sc > best[0]:
                    best = (sc, t_arm, ch, t_prop)
            if best and best[0] >= 0.5:
                sc, t_arm, ch, t_prop = best
                extra.append({"dvr": D[i][0], "t_arm": t_arm, "action": ch, "t_prop": t_prop,
                              "score": round(sc, 2), "how": "clock, no prop sound"})
                used_d.add(i); used_a.add(id(ch))

    announced = {}                      # DVR chain -> HD chain the file list was told about

    def phase(name, found, tried):
        for dch, ach, how in found:
            if id(dch) not in announced:
                announced[id(dch)] = id(ach)
                ev(dch[0], name, "ok", f"{how} → {os.path.basename(ach[0])}")
        for dch in tried:
            if id(dch) not in announced:
                ev(dch[0], name, "bad", name)

    if not stop_requested():
        phase("clock", [(D[i][0], A[j][0], "clock") for i, j, _ in got] +
              [(p_["dvr"], p_["action"], "clock") for p_ in extra],
              list({id(ch): ch for ch, _ in D}.values()))

    # 1b. DVRs that never show ARMED (recording started mid-flight): the clock says where they sit,
    #     motion fine-tunes it
    if groups and not stop_requested():
        used_a = {chain_id(j) for _, j, _ in got} | {id(p["action"]) for p in extra}
        for ch in no_arm:
            td = _ts(media_info(ch[0])[1])
            if td is None or skip_now(ch[0]):
                continue
            near = sorted((min(abs(TD[i2] - td) for _, i2, _ in grp), grp) for grp in groups)
            best = None
            for _, grp in near:
                g = min(grp, key=lambda x: abs(TD[x[1]] - td))[0]
                for ach in achains:
                    ta = _ts(media_info(ach[0])[1])
                    if id(ach) in used_a or ta is None or skip_now(ach[0]):
                        continue
                    # several stretches through the flight; each gives the offset between the files
                    offs = []
                    for t0 in (10.0, 30.0, 60.0, 90.0, 120.0):
                        guess = td + t0 - ta - g                 # same instant on the HD
                        if t0 + MOTION_DUR + 4 > media_info(ch[0])[0] or                                 not 3 < guess < media_info(ach[0])[0] - MOTION_DUR - 8:
                            continue
                        t_a, sc = refine_by_motion(ch[0], t0, ach[0], guess)
                        if sc >= 0.5:
                            offs.append((t_a - t0, sc))
                    if len(offs) >= 2 or (offs and offs[0][1] >= 0.8):
                        off = float(np.median([o for o, _ in offs]))
                        sc = float(np.median([c for _, c in offs]))
                        if best is None or sc > best[0]:
                            best = (sc, ach, off, len(offs))
            if best:
                sc, ach, off, n = best
                how = "clock, no ARMED (motion)"
                extra.append({"dvr": ch, "t_arm": 0.0, "action": ach, "t_prop": off, "score": round(sc, 2),
                              "how": how, "aligned": True})
                used_a.add(id(ach))

    # HD recordings still free may have taken off after the first listen: hear the rest of their 75 s
    # (only these: the clock already placed the others)
    if FIRST_LISTEN < 75 and groups and not stop_requested():
        taken_a = {chain_id(j) for _, j, _ in got} | {id(p["action"]) for p in extra}
        add = []
        for ch in achains:
            if id(ch) in taken_a or skip_now(ch[0]):
                continue
            have = [t for c_, t in A if c_ is ch]
            ev(ch[0], "more", "run", "Listening further")
            new = [(ch, t) for t in listen_further(ch[0]) if all(abs(t - h) > 0.1 for h in have)]
            ev(ch[0], "more", "ok" if new else "bad", f"props {mmss(new[0][1])}" if new else "Listening further")
            add += new
        if add:
            log(f"Listening further into {len({id(c) for c, _ in add})} unmatched HD recording(s)")
            for ch, t in add:
                A.append((ch, t))
                TA.append(_ts(media_info(ch[0])[1]))
            new_g = np.full((nd, len(add)), np.nan)
            for i in range(nd):
                for k, j in enumerate(range(na, len(A))):
                    if TD[i] is not None and TA[j] is not None:
                        new_g[i, k] = (TD[i] + D[i][1]) - (TA[j] + A[j][1])
            G = np.hstack([G, new_g])
            na = len(A)

    # 2. the rest: best in-order one-to-one pairing on motion (both cameras number files in flying order)
    taken_d = {i for i, _, _ in got} | {id(p["dvr"]) for p in extra}
    taken_a = {chain_id(j) for _, j, _ in got} | {id(p["action"]) for p in extra}
    di = [i for i in range(nd) if i not in taken_d and id(D[i][0]) not in taken_d]
    aj = [j for j in range(na) if chain_id(j) not in taken_a]
    tau = MATCH_TAU if not groups else 0.5           # with working clocks, leftovers need a strong match
    # DVR numbering follows the flying order (one goggles DVR per trip). HD numbering only does
    # within one camera/card, so each HD folder gets its own in-order pass against the DVRs.
    T0, sure0 = chrono_keys(dchains, achains, [(D[i][0], D[i][1], A[j][0], A[j][1]) for i, j, _ in got] +
                     [(p["dvr"], p["t_arm"], p["action"], p["t_prop"]) for p in extra])
    di.sort(key=lambda i: T0.get(id(D[i][0]), float("inf")))
    folders = {}
    for j in sorted(aj, key=lambda j: (T0.get(chain_id(j), float("inf")), A[j][1])):
        folders.setdefault(os.path.dirname(A[j][0][0]), []).append(j)
    dp, free_d = [], list(di)
    for _, fj in sorted(folders.items(), key=lambda kv: -len(kv[1])):
        sub = M[np.ix_(free_d, fj)].copy() if free_d and fj else np.zeros((len(free_d), len(fj)))
        for a_, i in enumerate(free_d):
            for b_, j in enumerate(fj):
                di_, aj_ = id(D[i][0]), chain_id(j)
                if groups and di_ in sure0 and aj_ in sure0 and abs(T0[di_] - T0[aj_]) > 86400:
                    sub[a_, b_] = -1.0
        n1, n2 = sub.shape
        best = np.zeros((n1 + 1, n2 + 1))
        move = np.zeros((n1 + 1, n2 + 1), int)
        for a_ in range(1, n1 + 1):
            for b_ in range(1, n2 + 1):
                opts = (best[a_ - 1, b_], best[a_, b_ - 1], best[a_ - 1, b_ - 1] + sub[a_ - 1, b_ - 1] - tau)
                move[a_, b_] = int(np.argmax(opts))
                best[a_, b_] = opts[move[a_, b_]]
        a_, b_ = n1, n2
        here = []
        while a_ > 0 and b_ > 0:
            if move[a_, b_] == 2:
                here.append((free_d[a_ - 1], fj[b_ - 1], "motion")); a_ -= 1; b_ -= 1
            elif move[a_, b_] == 0:
                a_ -= 1
            else:
                b_ -= 1
        dp += here
        matched_here = {i for i, _, _ in here}
        free_d = [i for i in free_d if i not in matched_here]
    # a recording can only be used once (two prop starts of one recording could both be picked)
    seen = set(taken_a)
    for i, j, how in sorted(dp, key=lambda x: -M[x[0], x[1]]):
        if chain_id(j) not in seen:
            got.append((i, j, how)); seen.add(chain_id(j))
    got.sort()

    # fill gaps by order (only when the clocks can't help): one DVR and one HD recording left
    # between two matches belong together
    if not groups:
        filled = []
        bounds = [(-1, -1)] + [(g_[0], g_[1]) for g_ in got] + [(nd, na)]
        used_chains = {chain_id(g_[1]) for g_ in got}
        for (i0, j0), (i1, j1) in zip(bounds, bounds[1:]):
            ds = list(range(i0 + 1, i1))
            js = [j for j in range(j0 + 1, j1) if chain_id(j) not in used_chains]
            chains_left = {chain_id(j) for j in js}
            if len(ds) == 1 and len(chains_left) == 1:
                jbest = max(js, key=lambda j: M[ds[0], j])
                filled.append((ds[0], jbest, "order"))
        got = sorted(got + filled)

    pairs = []
    for i, j, how in got:
        pairs.append({"dvr": D[i][0], "t_arm": D[i][1], "action": A[j][0], "t_prop": A[j][1],
                      "score": round(float(M[i, j]), 2), "how": how})
    pairs += extra
    if not stop_requested():
        phase("motion", [(p_["dvr"], p_["action"], p_["how"]) for p_ in pairs if not p_["how"].startswith("clock")],
              list({id(ch): ch for ch, _ in D}.values()))

    if groups and not stop_requested():
        used_hd = {id(p["action"]) for p in pairs}

        def gaps_near(td):
            out = []
            for grp in groups:
                dt, g = min((abs(TD[i2] - td), g) for g, i2, _ in grp)
                out.append((dt, g))
            return [g for dt, g in sorted(out) if dt < 36 * 3600]

        for ach in achains:
            if id(ach) in used_hd:
                continue
            ta = _ts(media_info(ach[0])[1])
            cues = [t for ch, t in A if ch is ach]
            if ta is None or not cues:
                continue
            found = None
            for t_p in cues:
                for dch in dchains:
                    td = _ts(media_info(dch[0])[1])
                    if td is None or skip_now(dch[0]):
                        continue
                    for g in gaps_near(td):
                        expect = ta + t_p + g - td               # where ARMED should be in this DVR
                        if not 0 < expect < media_info(dch[0])[0]:
                            continue
                        near = armed_runs_in(dch[0], [(expect - 2 * CLOCK_TOL, expect + 2 * CLOCK_TOL)])
                        if near:
                            r = min(near, key=lambda r: abs(r[0] - expect))
                            found = (dch, r[0], t_p)
                            break
                    if found:
                        break
                if found:
                    break
            if found:
                dch, t_arm, t_p = found
                log(f"{os.path.basename(ach[0])}: second flight inside DVR {os.path.basename(dch[0])}")
                pairs.append({"dvr": dch, "t_arm": t_arm, "action": ach, "t_prop": t_p, "score": 0.0,
                              "how": "clock, same DVR"})
                used_hd.add(id(ach))

    # doubtful pairs: maybe armed, disarmed and armed again - try every ARMED in that DVR
    for p in pairs:
        if p["score"] >= WEAK_SCORE or p["how"].startswith("clock") or stop_requested():
            continue
        log(f"Weak match for {os.path.basename(p['dvr'][0])}: looking for another ARMED…")
        runs = cached(ARMED + "_all", p["dvr"][0], lambda: detect_armed_runs(p["dvr"][0]))
        cues = [t for ch, t in A if ch is p["action"]]
        best = (p["score"], p["t_arm"], p["t_prop"])
        for r in runs:
            if abs(r[0] - p["t_arm"]) < 2:
                continue
            ds = motion_signal(p["dvr"][0], r[0] + MOTION_T0, True)
            for c in cues:
                sc = motion_score(ds, motion_signal(p["action"][0], c + MOTION_T0, False))
                if sc > best[0]:
                    best = (sc, r[0], c)
        if best[1] != p["t_arm"] and best[0] >= p["score"] + 0.1:
            p.update(score=round(float(best[0]), 2), t_arm=best[1], t_prop=best[2], how="re-armed")
    for p in pairs:
        if not p["how"].startswith("clock") or p.get("aligned") or p["score"] >= WEAK_SCORE or stop_requested():
            continue
        dpath, apath = p["dvr"][0], p["action"][0]
        offs = []
        for k in (0.0, 20.0, 40.0):
            if p["t_arm"] + k + MOTION_T0 + MOTION_DUR + 1 > media_info(dpath)[0] or \
                    p["t_prop"] + k + MOTION_T0 + MOTION_DUR + 4 > media_info(apath)[0]:
                continue
            t_a, sc = refine_by_motion(dpath, p["t_arm"] + k, apath, p["t_prop"] + k)
            if sc >= 0.5:
                offs.append(t_a - k)
        if len(offs) >= 2 and max(offs) - min(offs) <= 0.1:
            t_m = float(np.median(offs))
            if abs(t_m - p["t_prop"]) > 0.5:
                log(f"{os.path.basename(dpath)}: motion moves it {t_m - p['t_prop']:+.2f} s from the prop sound")
                p.update(t_prop=t_m, aligned=True, how=p["how"] + " (motion)")

    # unmatched DVRs: maybe the first ARMED was an arm-disarm; try its other ARMEDs against
    # the HD flights still free at its place in the flying order
    matched_d = {id(p["dvr"]) for p in pairs}
    for i, (ch, t0) in enumerate(D):
        if id(ch) in matched_d or groups or stop_requested():      # with working clocks, step 1 already looked
            continue
        before = [g for g in got if g[0] < i]
        after = [g for g in got if g[0] > i]
        j_lo = before[-1][1] if before else -1
        j_hi = after[0][1] if after else len(A)
        used = {id(p["action"]) for p in pairs}
        free = [j for j in range(j_lo + 1, j_hi) if id(A[j][0]) not in used]
        if not free:
            continue
        log(f"No match for {os.path.basename(ch[0])}: looking for another ARMED…")
        runs = cached(ARMED + "_all", ch[0], lambda: detect_armed_runs(ch[0]))
        best = (WEAK_SCORE, None, None)
        for r in runs:
            if abs(r[0] - t0) < 2:
                continue
            ds = motion_signal(ch[0], r[0] + MOTION_T0, True)
            for j in free:
                sc = motion_score(ds, motion_signal(A[j][0][0], A[j][1] + MOTION_T0, False))
                if sc > best[0]:
                    best = (sc, r[0], j)
        if best[1] is not None:
            j = best[2]
            pairs.append({"dvr": ch, "t_arm": best[1], "action": A[j][0], "t_prop": A[j][1],
                          "score": round(float(best[0]), 2), "how": "re-armed"})
    # HD recordings with no prop start heard (an export that starts mid-flight, muted audio) and DVRs
    # still free: line them up by motion alone - the clip's opening against each whole DVR flight
    used_d = {id(p["dvr"]) for p in pairs}
    used_a = {id(p["action"]) for p in pairs}
    deaf = [ch for ch in achains if id(ch) not in used_a and not any(c_ is ch for c_, _ in A)
            and media_info(ch[0])[0] >= MOTION_DUR + 4 and not skip_now(ch[0])]
    free = [ch for ch in dchains if id(ch) not in used_d and not skip_now(ch[0])]
    if deaf and free and not stop_requested():
        log(f"No prop sound in {len(deaf)} HD recording(s): lining them up with the free DVRs by motion")
        cand = []
        for k_, ch in enumerate(deaf):
            progress(0.96 + 0.03 * k_ / len(deaf),
                     f"No prop sound in {os.path.basename(ch[0])}: lining it up by motion",
                     2.5 * (len(deaf) - k_) + (2.0 * len(free) if k_ == 0 else 0))
            # a short stretch of motion fits many flights; two stretches 56 s apart that land on the
            # same DVR, 56 s apart there too, don't happen by chance
            t1, t2 = 4.0, 60.0
            if media_info(ch[0])[0] < t2 + MOTION_DUR + 4:
                continue
            h1, h2 = motion_signal(ch[0], t1, False), motion_signal(ch[0], t2, False)
            best1, best2 = [], []
            for dch in free:
                long = dvr_motion_long(dch[0])
                o1, s1 = slide_match(h1, long)
                o2, s2 = slide_match(h2, long)
                best1.append((s1, o1, dch))
                best2.append((s2, o2, dch))
            if not best1:
                continue
            s1, o1, d1 = max(best1, key=lambda x: x[0])
            s2, o2, d2 = max(best2, key=lambda x: x[0])
            if d1 is d2 and abs((o2 - o1) - (t2 - t1)) <= 0.5 and min(s1, s2) >= 0.6:
                cand.append((min(s1, s2), 0.0, ch, d1, o1, t1))
        taken = set()
        for sc, margin, ch, dch, t_d, t_h in sorted(cand, key=lambda x: -x[0]):
            if id(dch) in taken:
                continue
            taken.add(id(dch))
            log(f"{os.path.basename(ch[0])}: no prop sound, lined up with {os.path.basename(dch[0])} by motion ({sc:.2f})")
            pairs.append({"dvr": dch, "t_arm": t_d, "action": ch, "t_prop": t_h, "score": round(sc, 2),
                          "how": "motion only", "aligned": True})

    # the goggles' mic (DVR audio kept): the HD recording a DVR's sound lines up with. A DVR nothing
    # matched gets that HD recording; one paired by motion only (not the clocks) moves to it when the
    # mic is clearly sure and that recording is still free. The sync itself still comes from ARMED and
    # the prop start where the sound says they are, or from the sound when there's no ARMED there.
    if DVR_MIC and not stop_requested():
        mic_d = [ch for ch in dchains if ch[0] in DVR_MIC and not is_skipped(ch[0])]
        if mic_d:
            paired = {id(p["dvr"]): p for p in pairs}
            used_a = {id(p["action"]) for p in pairs}
            free_a = [ach for ach in achains if not is_skipped(ach[0])]
            Hs = {id(ach): sound_print(ach[0], STICK_HD_LISTEN) for ach in free_a}
            for k_, dch in enumerate(mic_d):
                p = paired.get(id(dch))
                if (p and p["how"].startswith("clock")) or stop_requested():
                    continue                        # the clocks are trusted
                progress(0.99, f"Goggles mic {os.path.basename(dch[0])}: listening", None)
                ev(dch[0], "mic", "run", "mic sound")
                S_ = sound_print(dch[0], STICK_LISTEN)
                sc_ = []
                for ach in free_a:
                    r = sound_offset(Hs[id(ach)], S_)
                    if r and r[1] >= STICK_MIN_Z and r[2] >= STICK_MIN_RATIO:
                        sc_.append((r[1] * min(r[2], 3.0), ach, r))
                if not sc_:
                    ev(dch[0], "mic", "bad", "mic sound")
                    continue
                _, ach, (lag, z, ratio) = max(sc_, key=lambda x: x[0])
                if p and p["action"] is ach:
                    ev(dch[0], "mic", "ok", "mic sound agrees")
                    continue
                if (p and not (z >= MIC_OVERRIDE_Z and ratio >= MIC_OVERRIDE_RATIO)) or id(ach) in used_a:
                    ev(dch[0], "mic", "bad", "mic sound unsure")
                    continue
                best = None                         # ARMED + prop start where the sound puts them
                for c_, t_p in A:
                    if c_ is not ach:
                        continue
                    expect = t_p + lag              # DVR time = HD time + lag
                    near = armed_runs_in(dch[0], [(expect - 3, expect + 3)]) if 0 < expect < media_info(dch[0])[0] else []
                    for r_ in near:
                        d_ = abs(r_[0] - expect)
                        if d_ <= 2.0 and (best is None or d_ < best[0]):
                            best = (d_, r_[0], t_p)
                if best:
                    newp = {"dvr": dch, "t_arm": best[1], "action": ach, "t_prop": best[2], "score": round(z, 1),
                            "how": "mic sound"}
                else:                               # no ARMED there: the sound lines them up
                    newp = {"dvr": dch, "t_arm": lag, "action": ach, "t_prop": 0.0, "score": round(z, 1),
                            "how": "mic sound only", "aligned": True}
                if p:
                    pairs.remove(p)
                    used_a.discard(id(p["action"]))
                    log(f"{os.path.basename(dch[0])}: the goggles mic says {os.path.basename(ach[0])}, "
                        f"not {os.path.basename(p['action'][0])} (z {z:.0f})")
                pairs.append(newp)
                used_a.add(id(ach))
    pairs = [p for p in pairs if not is_skipped(p["dvr"][0]) and not is_skipped(p["action"][0])]

    # flying order: the HD footage leads; unmatched recordings slot in on the same timeline
    T, _ = chrono_keys(dchains, achains, [(p["dvr"], p["t_arm"], p["action"], p["t_prop"]) for p in pairs])
    when = lambda c: T.get(id(c), float("inf"))
    pairs.sort(key=lambda p: (when(p["action"]), p["t_prop"]))
    if order is not None:
        order.update(T)
    md = {id(p["dvr"]) for p in pairs}
    ma = {id(p["action"]) for p in pairs}
    for p in pairs:                     # matched by a later rescue (re-armed, same DVR, motion only)
        if announced.get(id(p["dvr"])) != id(p["action"]):
            announced[id(p["dvr"])] = id(p["action"])
            ev(p["dvr"][0], "rescue", "ok", f"{p['how']} → {os.path.basename(p['action'][0])}")
        ev(p["dvr"][0], done="matched")
        ev(p["action"][0], "match", "ok", f"← {os.path.basename(p['dvr'][0])}", done="matched")
    for c in [c for c in dchains if id(c) not in md] + [c for c in achains if id(c) not in ma]:
        if is_skipped(c[0]):
            SKIPPED_SEEN.add(c[0])
            ev(c[0], done="skipped")
        elif c[0] not in UNREAD:
            ev(c[0], "match", "bad", "no match", done="nomatch")
    return (pairs, sorted([c for c in dchains if id(c) not in md], key=when),
            sorted([c for c in achains if id(c) not in ma], key=when))


# ------------------------------------------------------------------ stickcam (experimental)
# A camera filming the pilot's hands on the radio: no OSD, no prop start of its own, and its clock is
# often reset at every power-on (GoPro: 2013-01-01 00:00). What it shares with the HD camera is sound:
# the pilot talking, clapping or snapping fingers next to the quad before takeoff, the props spooling
# up. Each stickcam recording is lined up with the HD recordings by cross-correlating their sound
# onsets over every possible offset; a true pair gives one sharp peak.
STICK_HD_LISTEN = 45.0       # s of each HD recording compared: its start, the pilot's claps and voice
                             # before takeoff (the first prop listen reads it anyway; longer only adds
                             # flight noise: 45 s scored better than 75 s on the Julian trip)
STICK_LISTEN = 60.0          # s of each stickcam recording compared (claps are before takeoff; reading a
                             # whole clip means reading the whole file: 6.5 GB = +340 s on the Julian trip)
MIC_OVERRIDE_Z = 12.0        # the goggles' mic overrides a motion-only pair only when this sure...
MIC_OVERRIDE_RATIO = 2.0     # ... and its best HD recording beats the next best by this much
STICK_MIN_Z = 8.0          # the best offset must stand out this far (z) from all the others...
STICK_MIN_RATIO = 1.25       # ... and beat the next best offset (more than 1 s away) by this factor
SND_SR, SND_HOP = 16000, 160  # sound print: 10 ms steps


def _sound_key(path, dur):
    st = os.stat(path)
    return os.path.join(SIG_DIR, f"{os.path.basename(path)}_{st.st_size}_{int(st.st_mtime)}_snd{dur or 'all'}.npy")


def _sound_onsets(x, sr):
    """10 ms sound onsets (rises in loudness, z-scored) of mono samples x."""
    x = np.asarray(x, np.float32)
    k = sr // SND_SR
    if k > 1:                                          # 48 kHz from the prop listen: average down to 16 kHz
        x = x[:len(x) // k * k].reshape(-1, k).mean(1)
    x = np.diff(x, prepend=0)                          # tilted toward the highs: claps and snaps are sharp
    n = len(x) // SND_HOP
    if n < 200:
        return np.zeros(0, np.float32)
    e = 10 * np.log10((x[:n * SND_HOP].reshape(n, SND_HOP) ** 2).mean(1) + 1e-10)
    on = np.minimum(np.maximum(np.diff(e, prepend=e[0]), 0), 20)
    return ((on - on.mean()) / (on.std() + 1e-9)).astype(np.float32)


def _save_sound(path, dur, on):
    try:
        os.makedirs(SIG_DIR, exist_ok=True)
        np.save(_sound_key(path, dur), on)
    except Exception:
        pass


def keep_sound_print(path, a, sr):
    """The prop listen read a recording's start anyway: keep its sound print for the stickcam."""
    try:
        if len(a) >= sr and not os.path.exists(_sound_key(path, STICK_HD_LISTEN)):
            _save_sound(path, STICK_HD_LISTEN, _sound_onsets(a[:int(STICK_HD_LISTEN * sr)], sr))
    except Exception:
        pass


def sound_print(path, dur=None):
    """Sound onsets of a recording's first `dur` s, or all of it; kept on disk next to the motion signals."""
    key = _sound_key(path, dur)
    if os.path.exists(key):
        return np.load(key)
    cmd = ["ffmpeg", "-v", "error", "-i", path] + (["-t", f"{dur:.3f}"] if dur else [])
    with DISK:
        raw = run(cmd + ["-map", "0:a:0", "-ac", "1", "-ar", str(SND_SR), "-f", "s16le", "-"])
    on = _sound_onsets(np.frombuffer(raw, np.int16).astype(np.float32) / 32768, SND_SR)
    _save_sound(path, dur, on)
    return on


def sound_offset(hd, stick, min_overlap=10.0):
    """Best offset between two sound prints: (lag_s, z, ratio) with stick time = HD time + lag;
    z: how far the peak stands out, ratio: peak over the best offset more than 1 s away."""
    a, b = hd, stick
    if len(a) < 200 or len(b) < 200:
        return None
    n = len(a) + len(b) - 1
    N = 1 << (n - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(b, N) * np.conj(np.fft.rfft(a, N)), N)
    lags = np.r_[np.arange(0, len(b)), np.arange(-len(a) + 1, 0)]
    c = np.r_[c[:len(b)], c[N - len(a) + 1:]]
    ov = np.minimum(len(b), len(a) + lags) - np.maximum(0, lags)
    ok = ov >= min(min_overlap * 100, 0.8 * min(len(a), len(b)))
    lags, sc = lags[ok], c[ok] / np.sqrt(ov[ok])
    if len(sc) < 10:
        return None
    k = int(sc.argmax())
    far = np.abs(lags - lags[k]) > 100
    second = float(sc[far].max()) if far.any() else 0.0
    z = float((sc[k] - sc.mean()) / (sc.std() + 1e-9))
    return lags[k] / 100.0, z, float(sc[k] / second) if second > 0 else 99.0


def match_stickcams(stick_chains, hd_chains, log=print):
    """Each stickcam recording paired with at most one HD recording (and the other way round), by
    sound. Returns ([{"stick": chain, "action": chain, "lag": s, "z": .., "ratio": ..}], unmatched sticks)."""
    if not stick_chains or not hd_chains:
        return [], list(stick_chains)
    log(f"Stickcam: comparing the sound of {len(stick_chains)} stickcam and {len(hd_chains)} HD recordings")
    H = [sound_print(ch[0], STICK_HD_LISTEN) for ch in hd_chains]
    cands = []
    for sc in stick_chains:
        for p_ in sc[1:]:
            ev(p_, "arm", "ok", f"continues {os.path.basename(sc[0])}", done="part")
    for i, sc in enumerate(stick_chains):
        if stop_requested():
            for sc_ in stick_chains[i:]:
                UNREAD.add(sc_[0])
                ev(sc_[0], "arm", "bad", "not analysed", done="unread")
            break
        if is_skipped(sc[0]):
            SKIPPED_SEEN.add(sc[0])
            ev(sc[0], done="skipped")
            continue
        progress(0.95 + 0.04 * i / len(stick_chains), f"Stickcam {os.path.basename(sc[0])}: listening")
        ev(sc[0], "arm", "run", "listening")
        for dur in (STICK_LISTEN, None):         # its start; all of it only if the start matched nothing
            S = sound_print(sc[0], dur)
            mine = []
            for j, h in enumerate(H):
                r = sound_offset(h, S)
                if r and r[1] >= STICK_MIN_Z and r[2] >= STICK_MIN_RATIO:
                    mine.append((r[1] * min(r[2], 3.0), i, j, r))
            if mine or len(S) < STICK_LISTEN * 100 - 50:    # found, or that already was the whole clip
                break
        cands += mine
    pairs, used_s, used_h = [], set(), set()
    for _, i, j, (lag, z, ratio) in sorted(cands, key=lambda c: -c[0]):
        if i in used_s or j in used_h:
            continue
        used_s.add(i)
        used_h.add(j)
        pairs.append({"stick": stick_chains[i], "action": hd_chains[j], "lag": lag, "z": z, "ratio": ratio})
        ev(stick_chains[i][0], "arm", "ok", f"sound → {os.path.basename(hd_chains[j][0])}", done="matched")
    for i, sc in enumerate(stick_chains):
        if i not in used_s and sc[0] not in UNREAD and sc[0] not in SKIPPED_SEEN:
            ev(sc[0], "arm", "bad", "no sound match", done="nomatch")
    log(f"Stickcam: {len(pairs)} of {len(stick_chains)} lined up by sound")
    return pairs, [ch for i, ch in enumerate(stick_chains) if i not in used_s]


def plan_layout(fps, dvr_paths, act_paths, other_paths, gap_s=30.0, lead=1, log=print, stick_paths=()):
    """Timeline plan: each HD flight on V1 with its DVR synced on V2 (and its stickcam above), gap_s
    apart; everything unmatched afterwards. Placements: {path, track (1-based video track), start (frames)}.
    'after_previous' marks a split file that goes right after the previous clip on its track."""
    fd = 1 / fps
    T = {}
    for _round in range(4):
        pairs, lone_d, lone_a = match_flights(dvr_paths, act_paths, log, order=T)
        hd_chains = [c for c in {id(c): c for c in [p["action"] for p in pairs] + list(lone_a)}.values()
                     if not skip_now(c[0])]          # a skipped HD clip gets no stickcam either
        sticks, lone_s = match_stickcams(chain_files(list(stick_paths), 3), hd_chains, log) if stick_paths else ([], [])
        back = [x for x in SKIPPED_SEEN if not is_skipped(x)]       # un-skipped while it ran: again, with them
        if not back or stop_requested():
            break
        log(f"Processing again: {', '.join(os.path.basename(x) for x in back)}")
        SKIPPED_SEEN.difference_update(back)
        T = {}
    stick_of = {id(sp["action"]): sp for sp in sticks}
    with_dvr = {id(p["action"]) for p in pairs}
    for sp in sticks:                            # the HD clip's row: its stickcam is a match too
        hd, st_name = sp["action"][0], os.path.basename(sp["stick"][0])
        if id(sp["action"]) in with_dvr:
            ev(hd, "stick", "ok", f"← {st_name}")
        else:                                    # no DVR, but a stickcam: not "no match" any more
            ev(hd, "match", "ok", f"← {st_name}", done="matched")
    nframes = lambda p: int(math.ceil(media_info(p)[0] * fps))
    chain_len = lambda ch: sum(nframes(p) for p in ch)
    gap = int(round(gap_s * fps))
    placements, report = [], []
    cursor = 0

    def put(chain, track, start):
        at = start
        for k, p in enumerate(chain):
            # length (frames) lets the panel check the layout fits before placing anything
            placements.append({"path": p, "track": track, "start": start, "after_previous": k > 0,
                               "length": nframes(p), "est_start": at})
            at += nframes(p)

    groups = []                                  # HD recording + its DVR(s), in flying order
    for p in pairs:
        if groups and groups[-1]["action"] is p["action"]:
            groups[-1]["pairs"].append(p)
        else:
            groups.append({"action": p["action"], "pairs": [p]})
    when = lambda c: T.get(id(c), float("inf"))
    groups += [{"action": c, "pairs": []} for c in lone_a if id(c) in stick_of]     # HD + stickcam, no DVR
    blocks = [(when(g["action"]), 0, g) for g in groups] + \
             [(when(c), 1, (c, 1, "HD")) for c in lone_a if id(c) not in stick_of] + \
             [(when(c), 1, (c, 2, "DVR")) for c in lone_d]
    blocks.sort(key=lambda b: b[:2])

    def put_lone(chain, track, kind):
        nonlocal cursor
        put(chain, track, cursor)
        report.append({"dvr" if kind == "DVR" else "action": os.path.basename(chain[0]), "path": chain[0],
                       "how": "no match", "kind": kind, "start": cursor,
                       "unread": chain[0] in UNREAD, "skipped": is_skipped(chain[0])})
        cursor += chain_len(chain) + gap

    for _, lone_block, g in blocks:
        if lone_block:
            put_lone(*g)
            continue
        rel = []                                 # DVR start relative to the HD start
        for p in g["pairs"]:
            if p.get("aligned"):                 # DVR time t_arm shows the same moment as HD t_prop
                rel.append(round((p["t_prop"] - p["t_arm"]) / fd))
                continue
            sound = math.floor(p["t_prop"] / fd + 1e-9)
            rel.append(sound - dvr_lead(p["dvr"][0], lead) - math.ceil(p["t_arm"] / fd - 1e-9))
        lo = min([0] + rel)
        hi = max([chain_len(g["action"])] + [r + chain_len(p["dvr"]) for r, p in zip(rel, g["pairs"])])
        sp = stick_of.get(id(g["action"]))
        if sp:                                   # stick time = HD time + lag: it starts lag s before the HD
            srel = int(round(-sp["lag"] / fd))
            lo, hi = min(lo, srel), max(hi, srel + chain_len(sp["stick"]))
        base = cursor - lo
        put(g["action"], 1, base)
        last_end = {}
        for r, p in zip(rel, g["pairs"]):
            track = 2 if last_end.get(2, -1) <= base + r else 3
            put(p["dvr"], track, base + r)
            last_end[track] = base + r + chain_len(p["dvr"])
            report.append({"dvr": os.path.basename(p["dvr"][0]), "action": os.path.basename(g["action"][0]),
                           "dvr_path": p["dvr"][0], "action_path": g["action"][0],
                           "split": len(p["dvr"]) + len(g["action"]) - 2, "score": p["score"], "how": p["how"],
                           "start": base + r})
        if sp:                                   # above the DVR(s)
            put(sp["stick"], max([3] + [t + 1 for t in last_end]), base + srel)
            report.append({"stick": os.path.basename(sp["stick"][0]), "action": os.path.basename(g["action"][0]),
                           "stick_path": sp["stick"][0], "action_path": g["action"][0], "kind": "stick",
                           "how": "sound", "score": round(sp["z"], 1), "start": base + srel})
        cursor = base + hi + gap
    for ch in lone_s:                            # stickcam clips no HD recording's sound matched
        put(ch, 1, cursor)
        report.append({"stick": os.path.basename(ch[0]), "stick_path": ch[0], "kind": "stick", "how": "no match",
                       "start": cursor, "skipped": is_skipped(ch[0])})
        cursor += chain_len(ch) + gap
    for p in sorted(other_paths, key=os.path.basename):
        put_lone([p], 1, "other")
    log(f"{len(pairs)} matched ({sum(p['how'] == 'order' for p in pairs)} by order only), "
        f"{len(lone_d)} DVR + {len(lone_a)} HD unmatched, in recording order"
        + (f"; {len(other_paths)} other clips at the end" if other_paths else ""))
    return placements, report


# -------------------------------------------------- job mode (Premiere panel)
TIMED_STEPS = ["detect_armed_runs", "detect_prop_onsets", "motion_signal", "refine_by_motion", "armed_near",
               "media_info", "audio_envelope", "_decode_small"]


def process_job(job_path, out_path):
    global TIMING, STOP_FILE
    job = json.load(open(job_path, encoding="utf-8"))
    STOP_FILE = os.path.join(os.path.dirname(os.path.abspath(job_path)), "stop")
    global SKIP_FILE
    SKIP_FILE = os.path.join(os.path.dirname(os.path.abspath(job_path)), "skip.json")
    SKIPPED_SEEN.clear()
    DVR_MIC.clear()
    DVR_MIC.update(job.get("dvr_audio") or [])
    if job.get("paths"):                             # still images are never part of a run
        job["paths"] = [p_ for p_ in job["paths"] if not p_.lower().endswith(IMAGE_EXT)]
    UNREAD.clear()
    try:
        ensure_ffmpeg()
    except RuntimeError as e:                          # the panel shows this message as it is
        json.dump({"error": str(e)}, open(out_path, "w", encoding="utf-8"))
        return
    if job.get("mode") == "estimate":
        try:
            res = estimate_actions(job)
        except Exception as e:
            res = {"error": str(e)}
        json.dump(res, open(out_path, "w", encoding="utf-8"))
        return
    if job.get("mode") == "cameras":                # the picker: camera groups with their roles
        try:
            paths = list(dict.fromkeys(job["paths"]))
            groups = camera_groups(paths)
            if job.get("save_roles"):                # DVR in the Corner: remember what the user set
                mem = load_roles()
                mem.update({k: v for k, v in job["save_roles"].items() if v})
                json.dump(mem, open(ROLES_FILE, "w", encoding="utf-8"), indent=1)
            roles = known_roles(groups)
            res = {"groups": [{"key": g["key"], "name": g["name"], "formats": g["formats"],
                               "clips": sum(len(v) for v in g["folders"].values()), "folders": list(g["folders"]),
                               "paths": [p for v in g["folders"].values() for p in v], "size": g["size"],
                               "role": roles[g["key"]], "known": roles[g["key"]] is not None} for g in groups]}
        except Exception as e:
            import traceback
            res = {"error": str(e), "trace": traceback.format_exc()}
        json.dump(res, open(out_path, "w", encoding="utf-8"))
        return
    TIMING = {"t0": time.time(), "progress": [], "steps": []}
    global DISK_MBPS
    try:
        TIMING["disk_MBps"] = DISK_MBPS = drive_speed(job.get("paths") or [c.get("path") for c in job.get("clips", [])
                                                                           if c.get("path")])
    except Exception:
        TIMING["disk_MBps"] = None
    g = globals()
    for name in TIMED_STEPS:
        if not getattr(g[name], "_wrapped", False):
            g[name] = _timed(name, g[name])
            g[name]._wrapped = True
    lines = []
    try:
        set_osd_search(job.get("osd_search", "auto"))
        global PROGRESS
        PROGRESS = lambda f, label, eta=None: print(f"@@ {f:.4f} {-1 if eta is None else round(eta)} {label}",
                                                     flush=True)
        global EVENT
        EVENT = lambda kind, obj: print(f"@@{kind} " + json.dumps(obj), flush=True)
        if job.get("mode") == "build":
            apply_roles(list(dict.fromkeys(job["paths"])), job.get("roles"))
        else:
            apply_roles(list(dict.fromkeys(c["path"] for c in job["clips"] if c.get("kind") == "video" and c.get("path"))),
                        job.get("roles"))
        if job.get("mode") == "build":
            log = lambda m: (lines.append(m), print(m, flush=True))
            dvr_re, act_re = job.get("dvr", DVR_NAME), job.get("action", "")
            paths = list(dict.fromkeys(job["paths"]))
            dv = [p for p in paths if is_dvr(p, dvr_re)]
            st = [p for p in paths if is_stick(p)]
            ac = [p for p in paths if is_hd(p, dvr_re, act_re)]
            ot = [p for p in paths if p not in dv and p not in ac and p not in st]
            EVENT("files", [{"p": p, "r": r} for r, ps in (("dvr", dv), ("hd", ac), ("stick", st)) for p in ps])
            placements, report = plan_layout(job["fps"], dv, ac, ot, job.get("gap", 30.0),
                                             job.get("lead", 1), log, stick_paths=st)
            res = {"placements": placements, "report": report, "log": lines, "stopped": stop_requested()}
        else:
            rows = sync_clips(job["fps"], job["clips"], job.get("dvr", DVR_NAME),
                              job.get("action", ""), job.get("lead", 1),
                              job.get("prior_window", 30.0), job.get("only_selected", True),
                              log=lines.append)
            res = {"rows": rows, "log": lines, "stopped": stop_requested()}
    except Exception as e:
        import traceback
        res = {"error": str(e), "trace": traceback.format_exc(), "log": lines}
    for p_ in sorted(SKIPPED_SEEN):              # the panel's last word: these were left out of this run,
        if EVENT:                                # even if un-skipped too late to be processed again
            EVENT("ev", {"p": p_, "c": None, "s": None, "t": "", "d": "skipped", "final": True})
    json.dump(res, open(out_path, "w", encoding="utf-8"), indent=1)
    try:
        rec = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": job.get("mode", "sync"),
               "files": len(job.get("paths") or job.get("clips") or []),
               "total_s": round(time.time() - TIMING["t0"], 1), "disk_MBps": TIMING.get("disk_MBps"),
               "progress": TIMING["progress"], "steps": TIMING["steps"]}
        with open(os.path.join(HERE, "timing_log.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xml", nargs="?", help="Final Cut Pro XML exported from Premiere")
    ap.add_argument("--dvr", default=DVR_NAME, help="regex for DVR file names (default: 'orqa' anywhere in the name)")
    ap.add_argument("--action", default="", help="regex for HD file names (default: every non-DVR video)")
    ap.add_argument("--lead", type=int, default=1, help="frames ARMED leads the prop sound (default 1)")
    ap.add_argument("--prior-window", type=float, default=30.0,
                    help="seconds the rough placement may be off before it is ignored")
    ap.add_argument("--make-template", nargs=2, metavar=("DVR_FILE", "FRAME"),
                    help="rebuild the ARMED template from a DVR frame that shows ARMED")
    ap.add_argument("--armed", metavar="DVR_FILE", help="just detect ARMED in one DVR file")
    ap.add_argument("--props", metavar="ACTION_FILE", help="just list prop starts in one HD file")
    ap.add_argument("--job", nargs=2, metavar=("JOB_JSON", "OUT_JSON"), help=argparse.SUPPRESS)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    if a.job:
        process_job(*a.job)
        return
    try:
        ensure_ffmpeg()
    except RuntimeError as e:
        sys.exit(str(e))
    if a.make_template:
        make_template(a.make_template[0], int(a.make_template[1]))
    elif a.armed:
        print(detect_armed(a.armed, a.verbose))
    elif a.props:
        for t, j in detect_prop_onsets(a.props):
            print(f"{t:9.3f}s  +{j:.0f} dB")
    elif a.xml:
        process_xml(a.xml, a.dvr, a.action, a.lead, a.prior_window, a.verbose)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
