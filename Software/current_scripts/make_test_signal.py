"""Generate a synthetic EMG CSV so you can sanity-check detection without hardware.

Usage:  python make_test_signal.py out.csv --gain-sim 3.0 --seed 1
The --gain-sim flag multiplies the whole recording, simulating a session with
different electrode contact. Auto-thresholding should track it; a hard-coded
--thresh will not.
"""
import argparse
import numpy as np
from scipy.signal import butter, sosfilt

p = argparse.ArgumentParser()
p.add_argument("out")
p.add_argument("--fs", type=float, default=500.0)
p.add_argument("--gain-sim", type=float, default=1.0)
p.add_argument("--noise", type=float, default=0.02)
p.add_argument("--mains", type=float, default=0.05)
p.add_argument("--seed", type=int, default=0)
a = p.parse_args()

rng = np.random.default_rng(a.seed)
fs = a.fs
levels = [1, 2, 3, 4, 5, 3, 1, 5, 2, 4, 4, 1, 3, 5, 2]
amp = {1: 0.15, 2: 0.35, 3: 0.7, 4: 1.3, 5: 2.2}
band = {1: (30, 90), 2: (30, 105), 3: (30, 125), 4: (30, 150), 5: (30, 180)}

sig = []
sig.append(rng.normal(0, a.noise, int(2.0 * fs)))
for lv in levels:
    dur = rng.uniform(0.8, 2.0)
    n = int(dur * fs)
    sos = butter(4, band[lv], btype="bandpass", fs=fs, output="sos")
    burst = sosfilt(sos, rng.normal(0, 1, n))
    burst /= (np.std(burst) or 1)
    ramp = np.minimum(1.0, np.minimum(np.arange(n), np.arange(n)[::-1]) / (0.15 * fs))
    sig.append(burst * amp[lv] * ramp + rng.normal(0, a.noise, n))
    sig.append(rng.normal(0, a.noise, int(rng.uniform(1.0, 2.5) * fs)))

y = np.concatenate(sig) * a.gain_sim
t = np.arange(len(y)) / fs
y = y + a.mains * a.gain_sim * np.sin(2 * np.pi * 60 * t)

with open(a.out, "w") as f:
    f.write("voltage_mV,sample_count\n")
    for i, v in enumerate(y):
        f.write(f"{v:.6f},{i}\n")
print(f"Wrote {a.out}: {len(y)} samples, {len(levels)} bursts, gain_sim={a.gain_sim}")
