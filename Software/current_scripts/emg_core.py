"""
emg_core.py
===========
Shared signal-processing core for the EMG calibration/analysis toolchain.

Both calibrate.py and analyze.py import from here so that a squeeze is
detected and described *identically* in both scripts. Any parameter that
cannot be trusted to transfer between recordings (detection threshold,
amplitude scale) is estimated from the recording itself and written into
the profile JSON.

v3 changes (fixes missed squeezes in recordings with lots of activity):
  * noise floor is measured from the quietest windows, not from the median
    of the whole envelope. The old estimator broke on any file where more
    than ~half the recording was active or that opened with a long hold.
  * the threshold is chosen by scanning for the most *stable* value rather
    than by a fixed baseline + k*sigma formula.
  * optional prominence-based splitting of blocks that never relax to rest.
"""

import csv
import json
import os
import re
import numpy as np
from scipy.signal import (butter, find_peaks, iirnotch, peak_prominences,
                          sosfiltfilt, tf2sos, welch)
from sklearn.neighbors import KNeighborsClassifier
from sklearn.preprocessing import StandardScaler

FEATURE_ORDER = ["RMS_n", "ZCR", "WL_n", "MDF_Hz", "ENV_PEAK_n"]
PROFILE_VERSION = 3
LEVEL_COLORS = {1: "lime", 2: "#FFEA00", 3: "orange", 4: "red", 5: "#404040"}

# Detection defaults. These are timing/shape priors about squeezing, not
# amplitude facts, so they do transfer between recordings.
DEFAULT_MIN_DURATION = 0.40   # s   - shorter blocks are startup artifacts
DEFAULT_MIN_GAP = 0.10        # s   - bridge envelope flicker, nothing more
DEFAULT_HYSTERESIS = 0.60     # thr_lo / thr_hi


# ----------------------------------------------------------------------
# I/O
# ----------------------------------------------------------------------

def load_emg_csv(path, gain=1.0, voltage_col=0, count_col=1):
    """Read a two-column CSV (voltage, sample_count) with a header row."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"File '{path}' not found.")

    voltages, counts = [], []
    with open(path, "r", newline="") as fh:
        reader = csv.reader(fh)
        first = next(reader, None)
        if first is not None:
            try:
                voltages.append(float(first[voltage_col]))
                counts.append(int(float(first[count_col])))
            except (ValueError, IndexError):
                pass  # it was a header
        for row in reader:
            if len(row) <= max(voltage_col, count_col):
                continue
            try:
                voltages.append(float(row[voltage_col]))
                counts.append(int(float(row[count_col])))
            except ValueError:
                continue

    if len(voltages) < 100:
        raise ValueError(f"Only {len(voltages)} samples read from {path}.")
    return np.asarray(voltages, float) * gain, np.asarray(counts, np.int64)


def next_session_dir(base_dir, prefix="Session", pad=3):
    """Allocate the next sequentially-numbered subdirectory under base_dir,
    e.g. base_dir/Session_001, Session_002, ... Creates base_dir if needed.
    Numbering picks up after the highest existing '{prefix}_<digits>' folder,
    so it survives folders being renamed or removed out of order (matches
    the Data_1, Data_2, ... convention already used under Filtering_Software/
    data/, just zero-padded so it keeps sorting correctly past 9)."""
    os.makedirs(base_dir, exist_ok=True)
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)$")
    existing = [int(m.group(1)) for name in os.listdir(base_dir)
               if (m := pattern.match(name)) and os.path.isdir(os.path.join(base_dir, name))]
    n = max(existing, default=0) + 1
    path = os.path.join(base_dir, f"{prefix}_{n:0{pad}d}")
    os.makedirs(path, exist_ok=False)
    return path


# ----------------------------------------------------------------------
# Filtering
# ----------------------------------------------------------------------

def _bandpass_sos(fs, lowcut=20.0, highcut=200.0, order=4):
    nyq = 0.5 * fs
    highcut = min(highcut, nyq * 0.98)
    lowcut = max(lowcut, 1.0)
    if lowcut >= highcut:
        raise ValueError(f"Invalid band {lowcut}-{highcut} Hz for fs={fs} Hz.")
    return butter(order, [lowcut, highcut], btype="bandpass", fs=fs, output="sos")


def _notch_sos_list(fs, mains_hz=60.0, q=30.0, n_harmonics=3):
    """SOS for each mains harmonic notch below Nyquist. Empty if mains_hz
    is falsy (notch disabled)."""
    if not mains_hz:
        return []
    nyq = 0.5 * fs
    socs = []
    for h in range(1, n_harmonics + 1):
        f0 = mains_hz * h
        if f0 >= nyq * 0.95:
            break
        b, a = iirnotch(f0, q, fs)
        socs.append(tf2sos(b, a))
    return socs


def _envelope_sos(fs, lp_cutoff=4.0, order=4):
    return butter(order, lp_cutoff, btype="lowpass", fs=fs, output="sos")


def apply_bandpass_filter(data, fs, lowcut=20.0, highcut=200.0, order=4):
    return sosfiltfilt(_bandpass_sos(fs, lowcut, highcut, order), data)


def apply_notch(data, fs, mains_hz=60.0, q=30.0, n_harmonics=3):
    """Remove mains hum. A 20-200 Hz bandpass passes 60 and 120 Hz straight
    through; in your recordings 60 Hz is among the strongest components in
    the whole spectrum, and it inflates RMS/WL and drags MDF toward 60 Hz."""
    out = data
    for sos in _notch_sos_list(fs, mains_hz, q, n_harmonics):
        out = sosfiltfilt(sos, out)
    return out


def get_amplitude_envelope(data, fs, lp_cutoff=4.0, order=4):
    return np.abs(sosfiltfilt(_envelope_sos(fs, lp_cutoff, order), np.abs(data)))


def preprocess(raw, fs, env_hz=4.0, lowcut=20.0, highcut=200.0, mains_hz=60.0):
    """raw -> (filtered, envelope). Single source of truth for both scripts."""
    filt = apply_bandpass_filter(raw, fs, lowcut, highcut)
    filt = apply_notch(filt, fs, mains_hz)
    return filt, get_amplitude_envelope(filt, fs, env_hz)


# ----------------------------------------------------------------------
# Noise floor
# ----------------------------------------------------------------------

def estimate_noise_floor(envelope, fs, win_sec=0.20, quiet_pct=10.0, factor=2.0):
    """Rest level and noise sigma, measured from the quietest windows only.

    Why not a median or sigma-clip over the whole envelope: that assumes rest
    is the majority of the recording. In emg_raw_2 and emg_raw_3 it is not --
    both open with 14-18 s of continuous contraction and are active well over
    half the time -- so the median lands *inside* the active population and
    clipping the upper tail never walks it back down. The resulting sigma is
    ~30x too large and the threshold ends up 6-8x too high, which is exactly
    why every level-1 and level-2 squeeze vanished.

    Instead: chop the envelope into short windows, take the quietest decile as
    a seed for "what rest looks like here", then keep every window within
    `factor` of that level and pool their samples. This only assumes that
    *some* genuine rest exists somewhere in the file, at any duty cycle.
    """
    env = np.asarray(envelope, float)
    w = max(int(win_sec * fs), 8)
    n = len(env) // w
    if n < 4:
        med = float(np.median(env))
        return med, float(1.4826 * np.median(np.abs(env - med)) or np.std(env))

    blocks = env[:n * w].reshape(n, w)
    bmean = blocks.mean(axis=1)
    seed = np.percentile(bmean, quiet_pct)
    quiet = blocks[bmean <= max(seed * factor, bmean.min() * 1.001)]

    vals = quiet.ravel()
    base = float(np.median(vals))
    sigma = float(1.4826 * np.median(np.abs(vals - base)))
    if sigma <= 0:
        sigma = float(np.std(vals)) or 1e-9
    return base, sigma


def robust_baseline(envelope, n_iter=6, clip=3.0):
    """Legacy sigma-clipping estimator. Kept for diagnostics only -- see the
    docstring of estimate_noise_floor for why it must not be trusted."""
    x = np.asarray(envelope, float)
    x = x[np.isfinite(x)]
    floor = max(len(x) * 0.02, 50)
    for _ in range(n_iter):
        med = np.median(x)
        sigma = 1.4826 * np.median(np.abs(x - med))
        if sigma <= 0:
            break
        keep = x <= med + clip * sigma
        if keep.all() or keep.sum() < floor:
            break
        x = x[keep]
    med = float(np.median(x))
    sigma = float(1.4826 * np.median(np.abs(x - med))) or float(np.std(x)) or 1e-9
    return med, sigma


def _otsu(values, nbins=256):
    values = values[np.isfinite(values)]
    hist, edges = np.histogram(values, bins=nbins)
    centers = 0.5 * (edges[:-1] + edges[1:])
    w = np.cumsum(hist).astype(float)
    total = w[-1]
    if total == 0:
        return float(np.median(values)), 0.0
    w0 = w / total
    cum_mean = np.cumsum(hist * centers) / total
    gmean = cum_mean[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (gmean * w0 - cum_mean) ** 2 / (w0 * (1.0 - w0))
    between[~np.isfinite(between)] = -np.inf
    i = int(np.argmax(between))
    return float(centers[i]), float(between[i] / (np.var(values) + 1e-30))


# ----------------------------------------------------------------------
# Burst detection
# ----------------------------------------------------------------------

def _runs(mask):
    """Contiguous True runs as [(start, end_inclusive), ...]."""
    m = np.asarray(mask).astype(np.int8)
    if m.size == 0 or m.sum() == 0:
        return []
    d = np.diff(m)
    starts = list(np.where(d == 1)[0] + 1)
    ends = list(np.where(d == -1)[0])
    if m[0]:
        starts.insert(0, 0)
    if m[-1]:
        ends.append(len(m) - 1)
    return list(zip(starts, ends))


def _split_on_valleys(envelope, s, e, fs, thr_hi, valley_frac, min_sub_sec):
    """Cut a block at internal valleys deep enough to be real relaxations.

    Needed when two squeezes run together because the envelope dips between
    them but never gets back down to rest. A cut is made only where the dip
    falls below thr_hi *and* is at least `valley_frac` of the smaller
    neighbouring peak, which keeps ordinary mid-squeeze ripple intact.
    """
    seg = envelope[s:e + 1]
    d = max(int(min_sub_sec * fs), 3)
    if len(seg) < 2 * d:
        return [(s, e)]
    peaks, _ = find_peaks(seg, distance=d, height=thr_hi)
    if len(peaks) < 2:
        return [(s, e)]
    prom = peak_prominences(seg, peaks)[0]
    peaks = peaks[prom >= valley_frac * seg[peaks]]
    if len(peaks) < 2:
        return [(s, e)]

    cuts = []
    for a, b in zip(peaks[:-1], peaks[1:]):
        v = a + int(np.argmin(seg[a:b + 1]))
        flank = min(seg[a], seg[b])
        if seg[v] < thr_hi and (flank - seg[v]) >= valley_frac * flank:
            cuts.append(v)
    if not cuts:
        return [(s, e)]
    edges = [0] + cuts + [len(seg) - 1]
    return [(s + edges[i], s + edges[i + 1]) for i in range(len(edges) - 1)]


def detect_squeeze_bounds(envelope, fs, thr_hi, thr_lo=None,
                          min_duration_sec=DEFAULT_MIN_DURATION,
                          min_gap_sec=DEFAULT_MIN_GAP,
                          valley_frac=None):
    """Hysteresis burst detector.

    Candidate regions are runs above thr_lo kept only if they peak above
    thr_hi -- Schmitt-trigger behaviour, so a firm squeeze gets its full
    onset/offset flanks while noise that tickles thr_lo is ignored.

    min_gap_sec now defaults to 0.10 s, not 0.45 s. The old value merged
    genuinely separate squeezes: emg_raw_3 contains a pair 0.11 s apart.
    Bridging flicker is the hysteresis band's job, not the gap rule's.

    valley_frac (e.g. 0.5) additionally splits blocks that never relax to
    rest. Off by default -- it recovers merged pairs but can over-segment a
    single squeeze with a ripple in it.
    """
    env = np.asarray(envelope, float)
    if thr_lo is None or thr_lo >= thr_hi:
        thr_lo = DEFAULT_HYSTERESIS * thr_hi

    cand = [(a, b) for a, b in _runs(env > thr_lo) if env[a:b + 1].max() >= thr_hi]
    if not cand:
        return [], []

    min_gap = int(round(min_gap_sec * fs))
    merged = [list(cand[0])]
    for a, b in cand[1:]:
        if a - merged[-1][1] < min_gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])

    blocks = []
    for a, b in merged:
        if valley_frac:
            blocks += _split_on_valleys(env, a, b, fs, thr_hi, valley_frac,
                                        min_duration_sec)
        else:
            blocks.append((a, b))

    min_dur = int(round(min_duration_sec * fs))
    keep = [(a, b) for a, b in blocks
            if (b - a) >= min_dur and env[a:b + 1].max() >= thr_hi]
    return [a for a, b in keep], [b for a, b in keep]


def auto_threshold(envelope, fs, min_duration_sec=DEFAULT_MIN_DURATION,
                   min_gap_sec=DEFAULT_MIN_GAP, hysteresis=DEFAULT_HYSTERESIS,
                   valley_frac=None, n_grid=60, k_fallback=4.0):
    """Pick the detection threshold for THIS recording by stability search.

    Rather than trusting a formula, sweep a log-spaced grid of candidate
    thresholds from the noise floor up to the 99.5th percentile of the
    envelope, run the *whole* detector at each one, and record how many
    squeezes come out. Plot that curve and it is a staircase: nonsense at the
    bottom (everything fuses into a few giant blocks), nonsense at the top
    (only the hardest squeezes survive), and a wide flat step in between where
    the answer stops depending on the exact number you picked. The middle of
    the widest step is the threshold.

    This is the same idea as MSER in vision -- trust the answer that is stable
    under perturbation of its own parameter. It needs no assumption about duty
    cycle, which is what the old baseline + k*sigma rule got wrong.

    Returns a dict with thr_hi/thr_lo plus diagnostics.
    """
    env = np.asarray(envelope, float)
    base, sigma = estimate_noise_floor(env, fs)

    lo = base + 3.0 * sigma
    hi = max(float(np.percentile(env, 99.5)), lo * 4.0)
    grid = np.geomspace(lo, hi, n_grid)
    log_grid = np.log(grid)

    counts = np.empty(n_grid, int)
    for i, t in enumerate(grid):
        t_lo = max(base + 1.5 * sigma, hysteresis * t)
        s, _ = detect_squeeze_bounds(env, fs, t, t_lo, min_duration_sec,
                                     min_gap_sec, valley_frac)
        counts[i] = len(s)

    best = None  # (log-width, count, threshold)
    for c in set(counts.tolist()):
        if c == 0:
            continue
        idx = np.where(counts == c)[0]
        for run in np.split(idx, np.where(np.diff(idx) != 1)[0] + 1):
            if len(run) < 2:
                continue
            width = log_grid[run[-1]] - log_grid[run[0]]
            if best is None or width > best[0]:
                best = (width, c,
                        float(np.exp(0.5 * (log_grid[run[0]] + log_grid[run[-1]]))))

    if best is None:  # no stable step at all -- fall back to the noise formula
        thr = base + k_fallback * sigma
        width, count, method = 0.0, int(counts.max()), "noise-fallback"
    else:
        width, count, thr = best
        method = "stability"

    thr_lo = min(max(base + 1.5 * sigma, hysteresis * thr), 0.95 * thr)
    eps = max(sigma * 1e-3, 1e-12)
    otsu_log, sep = _otsu(np.log10(env + eps))

    return {
        "thr_hi": float(thr),
        "thr_lo": float(thr_lo),
        "baseline": float(base),
        "sigma": float(sigma),
        "method": method,
        "plateau_width": float(width),
        "plateau_count": int(count),
        "snr_db": float(20 * np.log10(max(np.percentile(env, 99.5), 1e-12) /
                                      max(sigma, 1e-12))),
        "active_frac": float(np.mean(env > thr)),
        "thr_noise": float(base + k_fallback * sigma),
        "thr_otsu": float(10.0 ** otsu_log - eps),
        "sweep_thresholds": grid.tolist(),
        "sweep_counts": counts.tolist(),
    }


def flag_blocks(starts, ends, fs, long_ratio=3.0):
    """Report blocks whose duration is a wild outlier.

    A block several times the median length is usually either a sustained hold
    or two squeezes fused together, and no threshold will tell them apart --
    emg_raw_2 opens with 17.9 s and emg_raw_3 with 14.4 s of unbroken
    contraction with no internal dip at all. Worth a human's eyes.
    """
    if len(starts) < 3:
        return []
    dur = (np.asarray(ends) - np.asarray(starts)) / fs
    med = float(np.median(dur))
    return [(i, float(dur[i])) for i in range(len(dur)) if dur[i] > long_ratio * med]


# ----------------------------------------------------------------------
# Streaming / real-time
# ----------------------------------------------------------------------
#
# The functions above are all whole-file: sosfiltfilt needs the entire
# signal, and auto_threshold needs a whole recording's worth of squeezes to
# find its stable plateau. Neither can run as data arrives. The classes
# below are the streaming counterparts used by the real-time monitor; they
# reuse the same filter designs and the same estimate_noise_floor()/
# detect_squeeze_bounds() logic so live behaviour tracks offline behaviour
# as closely as a causal system can.

class WindowedZeroPhaseFilter:
    """Streaming zero-phase filter: sosfiltfilt over a rolling raw buffer,
    emitting only the newest slice old enough to have fully settled.

    A causal filter (sosfilt with persistent state) would be fully online
    but phase-distorted relative to the sosfiltfilt used everywhere offline.
    Since a real squeeze lasts far longer than a fraction of a second, it is
    worth trading a small *fixed* latency (confirm_sec) for the real thing:
    every update re-runs sosfiltfilt over the whole rolling buffer_sec of
    raw samples, then returns only the portion at least confirm_sec old,
    which has settled on both sides. Bandpass -> notch -> envelope reuse the
    exact same SOS as preprocess(), so the live signal is the offline signal
    delayed by confirm_sec, not a different filter.
    """

    def __init__(self, fs, lowcut=20.0, highcut=200.0, mains_hz=60.0,
                 env_hz=4.0, order=4, buffer_sec=2.5, confirm_sec=0.4):
        if confirm_sec >= buffer_sec:
            raise ValueError("confirm_sec must be smaller than buffer_sec.")
        self.fs = fs
        self.bp_sos = _bandpass_sos(fs, lowcut, highcut, order)
        self.notch_sos = _notch_sos_list(fs, mains_hz)
        self.env_sos = _envelope_sos(fs, env_hz, order)
        self.buffer_len = int(round(buffer_sec * fs))
        self.confirm_len = int(round(confirm_sec * fs))
        self.raw = np.zeros(0, float)
        self.confirmed = 0  # samples already emitted, indexed into self.raw

    def process_chunk(self, raw_chunk):
        """Feed newly-arrived raw samples. Returns (filtered, envelope) for
        whatever portion (possibly empty) has newly settled."""
        chunk = np.asarray(raw_chunk, float)
        self.raw = np.concatenate([self.raw, chunk])
        if len(self.raw) > self.buffer_len:
            drop = len(self.raw) - self.buffer_len
            self.raw = self.raw[drop:]
            self.confirmed = max(0, self.confirmed - drop)

        confirm_upto = len(self.raw) - self.confirm_len
        if confirm_upto <= self.confirmed:
            return np.zeros(0, float), np.zeros(0, float)

        try:
            filt = sosfiltfilt(self.bp_sos, self.raw)
            for sos in self.notch_sos:
                filt = sosfiltfilt(sos, filt)
            env = np.abs(sosfiltfilt(self.env_sos, np.abs(filt)))
        except ValueError:
            return np.zeros(0, float), np.zeros(0, float)  # buffer still too short to settle

        new_filt = filt[self.confirmed:confirm_upto]
        new_env = env[self.confirmed:confirm_upto]
        self.confirmed = confirm_upto
        return new_filt, new_env


class RollingThresholdTracker:
    """Live thr_hi/thr_lo, rescaled from a continuously re-measured noise
    floor rather than trusted verbatim from calibration.

    Absolute EMG amplitude drifts across sessions (electrode contact, gel,
    skin) and within one (sweat, fatigue), so a profile's raw thr_hi/thr_lo
    in mV is not trustworthy in a new live session. What should transfer is
    the *shape* of the calibrated threshold -- how many noise-sigmas above
    rest counts as a real squeeze (k_hi/k_lo below) -- rescaled against a
    live baseline/sigma re-measured every update_period_sec via the same
    estimate_noise_floor() used offline, over a trailing noise_window_sec of
    envelope.

    A short window (sub-2s) can't do this: estimate_noise_floor needs the
    window to contain genuine rest, and a sustained hold routinely runs well
    past a couple of seconds (see flag_blocks -- the calibration data this
    shipped with opens with 14-18s of unbroken contraction). noise_window_sec
    defaults much longer so it is likely to catch a real rest gap even
    around a long hold, while still adapting within a session. If a window
    is fully active for its whole span, the tracker holds its last accepted
    estimate rather than update to a bad one.

    That safety clamp (max_jump_factor) has a failure mode of its own,
    though: if today's true baseline is legitimately far from calibration
    day's (different electrode contact), every window keeps landing outside
    the clamp and the tracker gets stuck refusing its own correct answer
    indefinitely. Session_007 showed exactly this -- 41 consecutive rejected
    updates (jump 5-9x) held the tracker at a stale calibration threshold for
    the first 49s of a session, missing every cue in that window, before one
    window happened to slip under the clamp by chance. max_stuck_updates
    breaks that deadlock: after this many consecutive rejections, a
    persistent signal is trusted over the one-shot safety check.
    """

    def __init__(self, fs, thr_hi, thr_lo, baseline, sigma,
                 noise_window_sec=18.0, update_period_sec=1.0,
                 max_jump_factor=5.0, max_stuck_updates=5):
        sigma = sigma or 1e-9
        self.k_hi = (thr_hi - baseline) / sigma
        self.k_lo = (thr_lo - baseline) / sigma
        self.fs = fs
        self.window_len = int(round(noise_window_sec * fs))
        self.update_len = int(round(update_period_sec * fs))
        self.min_samples = max(int(0.5 * self.window_len), int(2.0 * fs))
        self.max_jump_factor = max_jump_factor
        # A single window landing >max_jump_factor away from the current
        # baseline is exactly the "mid-hold, not real rest" case the clamp
        # is meant to guard against. But *consistently* landing there for
        # many updates in a row isn't that -- it's calibration day's baseline
        # just not matching today's electrode contact (seen directly in
        # Session_007's threshold_log.csv: 41 straight rejections, jump 5-9x,
        # stuck at calibration's stale threshold for the first 49s of the
        # session before one window happened to slip under the clamp).
        # After this many consecutive rejections, trust the persistent signal
        # over the one-shot safety check and accept anyway.
        self.max_stuck_updates = max_stuck_updates
        self.consecutive_rejections = 0

        self.env_buf = np.zeros(0, float)
        self.since_update = 0
        self.baseline, self.sigma = baseline, sigma
        self.thr_hi, self.thr_lo = thr_hi, thr_lo
        self.samples_seen = 0
        # One row per re-estimate *attempt* (accepted or rejected), not per
        # chunk -- this is the record that lets you actually see the
        # max_jump_factor clamp reject a legitimate drift instead of just
        # suspecting it happened. See threshold_log.csv in realtime_squeeze_monitor.py.
        self.history = []

    def update(self, env_chunk, exclude=False):
        """Feed newly-confirmed envelope samples. Returns the current
        (thr_hi, thr_lo) -- unchanged unless a new estimate was accepted.

        exclude=True means the caller already knows this chunk isn't real
        rest (e.g. mid-squeeze, or within a post-squeeze settling window) --
        skip it entirely rather than trusting estimate_noise_floor's
        quietest-decile trick to filter it out after the fact. Cannot cause
        a lockup: worst case is simply fewer samples to work with, so
        updates fire less often (holding the last good value longer), never
        a value that can't be moved off of."""
        env_chunk = np.asarray(env_chunk, float)
        self.samples_seen += len(env_chunk)
        if env_chunk.size == 0:
            return self.thr_hi, self.thr_lo

        if not exclude:
            self.env_buf = np.concatenate([self.env_buf, env_chunk])
            if len(self.env_buf) > self.window_len:
                self.env_buf = self.env_buf[-self.window_len:]
        self.since_update += len(env_chunk)

        if self.since_update >= self.update_len and len(self.env_buf) >= self.min_samples:
            self.since_update = 0
            base_new, sigma_new = estimate_noise_floor(self.env_buf, self.fs)
            accepted, forced, jump = False, False, None
            if sigma_new > 0 and np.isfinite(base_new) and np.isfinite(sigma_new):
                jump = max(base_new / max(self.baseline, 1e-12),
                           self.baseline / max(base_new, 1e-12))
                stuck = self.consecutive_rejections + 1 >= self.max_stuck_updates
                if jump <= self.max_jump_factor or stuck:
                    accepted = True
                    forced = jump > self.max_jump_factor
                    self.consecutive_rejections = 0
                    self.baseline, self.sigma = base_new, sigma_new
                    self.thr_hi = self.baseline + self.k_hi * self.sigma
                    self.thr_lo = self.baseline + self.k_lo * self.sigma
                else:
                    self.consecutive_rejections += 1
            else:
                self.consecutive_rejections += 1
            self.history.append({
                "t_sec": self.samples_seen / self.fs, "accepted": accepted, "forced": forced,
                "baseline_candidate": base_new, "sigma_candidate": sigma_new,
                "jump_ratio": jump, "baseline": self.baseline, "sigma": self.sigma,
                "thr_hi": self.thr_hi, "thr_lo": self.thr_lo,
            })
        return self.thr_hi, self.thr_lo


class LiveSqueezeDetector:
    """Causal, streaming re-implementation of detect_squeeze_bounds()'s
    hysteresis + min_gap + min_duration logic.

    A run above thr_lo only becomes a real squeeze once its max reaches
    thr_hi (the Schmitt trigger); two runs separated by less than min_gap
    are bridged into one continuous block, same as offline. What it can't
    do is retroactively split a block that never dips back to rest --
    detect_squeeze_bounds's optional valley-splitting needs to see the whole
    block first -- so a sustained fused hold shows up as one long block
    here, the same case offline flags via flag_blocks as worth a human's eyes.
    """

    def __init__(self, fs, min_duration_sec=DEFAULT_MIN_DURATION,
                 min_gap_sec=DEFAULT_MIN_GAP):
        self.fs = fs
        self.min_duration = int(round(min_duration_sec * fs))
        self.min_gap = max(1, int(round(min_gap_sec * fs)))
        self._reset()

    def _reset(self):
        self.active = False
        self.start = None
        self.running_max = -np.inf
        self.confirmed = False
        self.below_since = None

    def update(self, indices, env_chunk, thr_hi, thr_lo):
        """indices/env_chunk: aligned 1D arrays for the newly-confirmed
        chunk. thr_hi/thr_lo: current live thresholds (may drift chunk to
        chunk as RollingThresholdTracker adapts). Returns a list of event
        dicts: {'type': 'start'|'provisional'|'end', 'start', 'end'}."""
        events = []
        for idx, env in zip(indices, env_chunk):
            if not self.active:
                if env > thr_lo:
                    self.active = True
                    self.start = int(idx)
                    self.running_max = env
                    self.confirmed = env >= thr_hi
                    self.below_since = None
                    events.append({"type": "start", "start": self.start})
                continue

            self.running_max = max(self.running_max, env)
            if not self.confirmed and self.running_max >= thr_hi:
                self.confirmed = True

            if env > thr_lo:
                self.below_since = None
            else:
                if self.below_since is None:
                    self.below_since = idx
                elif idx - self.below_since >= self.min_gap:
                    end = int(self.below_since) - 1
                    if self.confirmed and (end - self.start + 1) >= self.min_duration:
                        events.append({"type": "end", "start": self.start, "end": end})
                    self._reset()

        if self.active and self.confirmed and len(indices):
            events.append({"type": "provisional", "start": self.start,
                           "end": int(indices[-1])})
        return events


class AdaptiveAmplitudeReference:
    """Live-rescales amp_ref -- the divisor behind RMS_n/WL_n/ENV_PEAK_n --
    using this session's own cue-labeled squeezes, instead of trusting the
    calibration profile's static amp_ref for the whole session.

    Absolute EMG amplitude for "the same" effort level drifts across
    sessions with electrode placement/gel/contact impedance. amp_ref exists
    specifically to correct for that, but a value fixed at calibration time
    only corrects for *that day's* contact -- a real-time session with
    different placement gets every classification skewed the same direction
    (e.g. everything reading as the lowest level, confidently). When cue
    practice supplies a ground-truth requested level for a detected squeeze,
    that squeeze's raw RMS can be compared against the calibration profile's
    own raw RMS for that same level, giving a live scale factor. Same "trust
    the calibrated shape, rescale by a live ratio" idea RollingThresholdTracker
    already uses for detection thresholds, applied to the classification
    features instead.

    Only updates when cue practice provides ground truth -- without it there
    is nothing to compare a raw measurement against, so the value just holds
    at whatever it last was (calibration's own amp_ref, at minimum).
    """

    def __init__(self, profile):
        self.calib_amp_ref = float(profile.get("meta", {}).get("amp_ref", 1.0)) or 1.0
        self.value = self.calib_amp_ref
        by_level = {}
        for s in profile.get("samples", []):
            if "RMS" in s and "Level" in s:
                by_level.setdefault(int(s["Level"]), []).append(float(s["RMS"]))
        self.calib_rms_by_level = {lv: float(np.mean(v)) for lv, v in by_level.items() if v}
        self.ratios = []

    def update(self, requested_level, raw_rms):
        """Feed one cue-labeled squeeze's (requested_level, raw RMS).
        Returns the new amp_ref (self.value), unchanged if this level has no
        calibration data to compare against."""
        calib_rms = self.calib_rms_by_level.get(int(requested_level))
        if not calib_rms or raw_rms <= 0:
            return self.value
        self.ratios.append(raw_rms / calib_rms)
        self.value = self.calib_amp_ref * float(np.median(self.ratios))
        return self.value


# ----------------------------------------------------------------------
# Amplitude reference
# ----------------------------------------------------------------------

def amplitude_reference(filtered, envelope, starts, ends, mode="p95",
                        mvc_index=0, noise_sigma=None):
    """Per-recording amplitude scale that RMS/WL are divided by, so a profile
    survives re-electroding. See notes in the README section of calibrate.py.

      'mvc'   - a designated maximum-effort burst (most correct)
      'p95'   - 95th percentile of the envelope inside detected bursts
      'noise' - baseline sigma, i.e. express everything as SNR
      'none'  - raw volts (single-session only)
    """
    if mode == "none":
        return 1.0
    if mode == "noise":
        return float(noise_sigma or 1.0)
    if not starts:
        return 1.0
    if mode == "mvc":
        i = int(np.clip(mvc_index, 0, len(starts) - 1))
        seg = filtered[starts[i]:ends[i] + 1]
        return float(np.sqrt(np.mean(seg ** 2))) or 1.0
    active = np.concatenate([envelope[s:e + 1] for s, e in zip(starts, ends)])
    return float(np.percentile(active, 95)) or 1.0


# ----------------------------------------------------------------------
# Features
# ----------------------------------------------------------------------

DEFAULT_ONSET_TAPER = 0.25
DEFAULT_RELEASE_TAPER = 0.08


def _taper_weights(n, onset_frac, release_frac):
    """Linear ramp 0->1 over the first onset_frac of the segment and 1->0
    over the last release_frac, flat at 1.0 in between. Either frac<=0
    disables that end. If the two tapers overlap (short segment), the
    smaller weight wins at each sample."""
    w = np.ones(n)
    if onset_frac > 0:
        onset_len = min(n, max(1, int(round(onset_frac * n))))
        w[:onset_len] = np.minimum(w[:onset_len], np.linspace(0.0, 1.0, onset_len, endpoint=True))
    if release_frac > 0:
        release_len = min(n, max(1, int(round(release_frac * n))))
        w[n - release_len:] = np.minimum(
            w[n - release_len:], np.linspace(1.0, 0.0, release_len, endpoint=True))
    return w


def extract_features(segment, fs, amp_ref=1.0, envelope_segment=None,
                     onset_taper_frac=0.0, release_taper_frac=0.0):
    """Feature dict for one squeeze. `segment` is BANDPASS-FILTERED data.

    onset_taper_frac (e.g. DEFAULT_ONSET_TAPER) down-weights the first
    fraction of the segment before computing amplitude features. A ballistic
    squeeze onset -- ramping up fast to reach a target level -- is a motor
    control transient common to *every* effort level, not something that
    distinguishes them; left unweighted it can dominate RMS/ENV_PEAK enough
    that a settled lower-level hold reads as whatever level the onset spike
    resembles (see Session_007's notes).

    release_taper_frac (e.g. DEFAULT_RELEASE_TAPER) does the same at the
    *end* of the segment, but is deliberately much smaller than the onset
    taper by default: reaching a target level is a deliberate ramp that can
    take a meaningful fraction of the hold, while letting go is comparatively
    quick -- a smaller fraction of the segment is actually "release".

    Both default to 0.0, reproducing the original unweighted behaviour
    exactly.

    WL is per sample, not summed over the block: as a raw sum it scales with
    how long you held the squeeze, so a long gentle one outscores a short
    violent one. RMS/WL/peak are divided by amp_ref; ZCR and MDF are already
    scale-free.
    """
    seg = np.asarray(segment, float)
    n = len(seg)
    if n < 8:
        return None

    w = _taper_weights(n, onset_taper_frac, release_taper_frac)
    rms = float(np.sqrt(np.sum(w * seg ** 2) / w.sum()))
    zcr = float(np.sum(np.diff(np.sign(seg)) != 0) / n)

    d = np.abs(np.diff(seg))
    wl_total = float(np.sum(d))
    wd = w[1:]
    wl = float(np.sum(wd * d) / wd.sum()) if wd.sum() > 0 else wl_total / (n - 1)

    freqs, psd = welch(seg, fs, nperseg=int(min(n, 256)), detrend="constant")
    cum = np.cumsum(psd)
    total = cum[-1]
    if total <= 0:
        mdf = mnf = 0.0
    else:
        mdf = float(freqs[int(np.searchsorted(cum, total / 2.0))])
        mnf = float(np.sum(freqs * psd) / total)

    if envelope_segment is not None:
        env_peak = float(np.max(np.abs(envelope_segment) * w))
    else:
        env_peak = rms
    ref = amp_ref or 1.0
    return {
        "RMS": rms, "ZCR": zcr, "WL": wl, "WL_total": wl_total,
        "MDF_Hz": mdf, "MNF_Hz": mnf, "ENV_PEAK": env_peak,
        "RMS_n": rms / ref, "WL_n": wl / ref, "ENV_PEAK_n": env_peak / ref,
        "duration_sec": n / fs, "n_samples": n,
    }


def feature_vector(feat):
    return [float(feat[k]) for k in FEATURE_ORDER]


# ----------------------------------------------------------------------
# Profile I/O
# ----------------------------------------------------------------------

def save_profile(path, samples, meta):
    with open(path, "w") as fh:
        json.dump({"version": PROFILE_VERSION, "feature_order": FEATURE_ORDER,
                   "meta": meta, "samples": samples}, fh, indent=2)


def load_profile(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Calibration profile '{path}' not found.")
    with open(path) as fh:
        data = json.load(fh)
    if isinstance(data, list):  # legacy v1 flat list
        return {"version": 1, "feature_order": ["RMS", "ZCR", "WL", "MDF_Hz"],
                "meta": {}, "samples": data}
    return data


def train_classifier(profile, k=3):
    """KNN trained on a calibration profile's labeled squeezes. Shared by
    analyze.py and the real-time monitor so both classify identically."""
    order = profile.get("feature_order", FEATURE_ORDER)
    X, y = [], []
    for item in profile["samples"]:
        try:
            X.append([float(item[kk]) for kk in order])
        except KeyError:
            order = ["RMS", "ZCR", "WL", "MDF_Hz"]
            X.append([float(item[kk]) for kk in order])
        y.append(int(item["Level"]))
    X, y = np.asarray(X, float), np.asarray(y, int)

    scaler = StandardScaler().fit(X)
    Xs = scaler.transform(X)
    counts = np.bincount(y, minlength=6)[1:]
    kk = int(max(1, min(k, counts[counts > 0].min(), len(y) - 1)))
    knn = KNeighborsClassifier(n_neighbors=kk, weights="distance").fit(Xs, y)
    return knn, scaler, order


def describe_threshold(info, prefix=""):
    return (f"{prefix}thr_hi={info['thr_hi']:.4f} thr_lo={info['thr_lo']:.4f} "
            f"| rest={info['baseline']:.4f} sigma={info['sigma']:.4f} "
            f"| {info['method']}, stable over {np.exp(info['plateau_width']):.1f}x "
            f"range, SNR={info['snr_db']:.0f} dB, active={info['active_frac'] * 100:.0f}%")
