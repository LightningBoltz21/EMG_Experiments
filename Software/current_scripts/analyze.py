"""
analyze.py - classify squeezes in a new recording using a calibrated profile.

Detection parameters are re-estimated from the new file (they never transfer);
feature normalization uses the profile's recipe so the KNN sees comparable
numbers. Low-confidence predictions are hatched rather than silently colored.
"""

import argparse
import os
import sys

import numpy as np
import matplotlib.pyplot as plt
from sklearn.neighbors import KNeighborsClassifier
from sklearn.model_selection import LeaveOneOut, cross_val_score

import emg_core as ec


def build_parser():
    p = argparse.ArgumentParser(description="KNN-powered EMG analyzer.")
    p.add_argument("csv_file", help="Path to the new EMG CSV file to analyze")
    p.add_argument("--profile", default="my_arm_profile.json")
    p.add_argument("--gain", type=float, default=1.0)
    p.add_argument("--fs", type=float, default=None, help="Default: from profile")
    p.add_argument("--env_hz", type=float, default=None)
    p.add_argument("--mains", type=float, default=None)
    p.add_argument("--thresh", default="auto", help="Threshold in mV or 'auto'")
    p.add_argument("--min_dur", type=float, default=ec.DEFAULT_MIN_DURATION)
    p.add_argument("--min_gap", type=float, default=ec.DEFAULT_MIN_GAP)
    p.add_argument("--hysteresis", type=float, default=ec.DEFAULT_HYSTERESIS)
    p.add_argument("--split_valleys", type=float, default=None)
    p.add_argument("--onset_taper", type=float, default=None,
                   help="Down-weight this fraction of each block's onset before "
                        "computing amplitude features (0 disables). Default: "
                        "whatever the profile itself was built with.")
    p.add_argument("--release_taper", type=float, default=None,
                   help="Down-weight this fraction of each block's release/tail before "
                        "computing amplitude features (0 disables). Default: "
                        "whatever the profile itself was built with.")
    p.add_argument("--k", type=int, default=3, help="KNN neighbors")
    p.add_argument("--min_conf", type=float, default=0.5)
    p.add_argument("--csv_out", default=None, help="Optional CSV of per-squeeze results")
    p.add_argument("--show_sweep", action="store_true")
    return p


args = build_parser().parse_args()
LEVEL_COLORS = ec.LEVEL_COLORS


def main():
    profile = ec.load_profile(args.profile)
    meta = profile.get("meta", {})
    fs = args.fs or float(meta.get("fs", 500.0))
    env_hz = args.env_hz or float(meta.get("env_hz", 4.0))
    mains = args.mains if args.mains is not None else float(meta.get("mains_hz", 60.0))
    onset_taper = (args.onset_taper if args.onset_taper is not None
                  else float(meta.get("onset_taper", ec.DEFAULT_ONSET_TAPER)))
    release_taper = (args.release_taper if args.release_taper is not None
                     else float(meta.get("release_taper", ec.DEFAULT_RELEASE_TAPER)))
    norm_mode = meta.get("norm_mode", "none")

    if profile.get("version", 1) < 2:
        print("WARNING: legacy profile with un-normalized features. Valid only if "
              "this recording used identical electrode placement and gain.")

    knn, scaler, order = ec.train_classifier(profile, k=args.k)
    k = knn.n_neighbors
    y_train = np.array([int(s["Level"]) for s in profile["samples"]], int)
    X_train = np.array([[float(s[kk]) for kk in order] for s in profile["samples"]], float)
    Xs = scaler.transform(X_train)

    counts = np.bincount(y_train, minlength=6)[1:]
    print(f"Profile: {len(y_train)} squeezes, per-level "
          f"{dict(enumerate(counts.tolist(), 1))}, k={k}")

    if len(y_train) >= 4:
        exact = cross_val_score(KNeighborsClassifier(n_neighbors=k, weights="distance"),
                                Xs, y_train, cv=LeaveOneOut()).mean()
        loo = np.array([KNeighborsClassifier(n_neighbors=k, weights="distance")
                        .fit(np.delete(Xs, i, 0), np.delete(y_train, i))
                        .predict(Xs[i:i + 1])[0] for i in range(len(y_train))])
        print(f"Leave-one-out: {exact * 100:.0f}% exact, "
              f"{np.mean(np.abs(loo - y_train) <= 1) * 100:.0f}% within +/-1 level")

    raw, x_counts = ec.load_emg_csv(args.csv_file, gain=args.gain)
    filtered, envelope = ec.preprocess(raw, fs, env_hz=env_hz, mains_hz=mains)

    info = ec.auto_threshold(envelope, fs, args.min_dur, args.min_gap,
                             args.hysteresis, args.split_valleys)
    if str(args.thresh).lower() != "auto":
        t = float(args.thresh)
        info.update(thr_hi=t, thr_lo=min(args.hysteresis * t, 0.95 * t),
                    method="manual", active_frac=float(np.mean(envelope > t)))
    print(ec.describe_threshold(info, "Detection "))
    if args.show_sweep:
        for t, c in zip(info["sweep_thresholds"], info["sweep_counts"]):
            print(f"    thr={t:.4f} -> {c} blocks")

    starts, ends = ec.detect_squeeze_bounds(envelope, fs, info["thr_hi"],
                                            info["thr_lo"], args.min_dur,
                                            args.min_gap, args.split_valleys)
    if not starts:
        print("Nothing detected; check --gain / --fs or set --thresh manually.")
        sys.exit(1)
    print(f"Detected {len(starts)} squeezes.")
    flags = dict(ec.flag_blocks(starts, ends, fs))
    for i, d in flags.items():
        print(f"  ! block #{i + 1} is {d:.1f}s - sustained hold or fused pair.")

    amp_ref = ec.amplitude_reference(filtered, envelope, starts, ends,
                                     mode=norm_mode, noise_sigma=info["sigma"])
    print(f"Amplitude reference ({norm_mode}) = {amp_ref:.5f}")

    rows = []
    for i, (s, e) in enumerate(zip(starts, ends)):
        feat = ec.extract_features(filtered[s:e + 1], fs, amp_ref,
                                   envelope_segment=envelope[s:e + 1],
                                   onset_taper_frac=onset_taper, release_taper_frac=release_taper)
        if feat is None:
            continue
        vs = scaler.transform(np.array([[feat[kk] for kk in order]], float))
        proba = knn.predict_proba(vs)[0]
        rows.append(dict(idx=i + 1, start=int(s), end=int(e),
                         level=int(knn.classes_[np.argmax(proba)]),
                         conf=float(proba.max()),
                         dist=float(knn.kneighbors(vs, n_neighbors=1)[0][0][0]),
                         flagged=i in flags, **feat))

    dists = np.array([r["dist"] for r in rows])
    novelty_cut = max(3.0, float(np.median(dists) * 4))

    print("\n  #   samples            lvl  conf  dist   RMS_n    MDF")
    for r in rows:
        note = ""
        if r["conf"] < args.min_conf:
            note += " [low-conf]"
        if r["dist"] > novelty_cut:
            note += " [out-of-profile]"
        if r["flagged"]:
            note += " [long block]"
        print(f"{r['idx']:3d}  {r['start']:7d}-{r['end']:<7d}   L{r['level']}  "
              f"{r['conf']:.2f}  {r['dist']:5.2f}  {r['RMS_n']:.3f}  "
              f"{r['MDF_Hz']:5.1f}{note}")

    if args.csv_out:
        import csv as _csv
        keys = ["idx", "start", "end", "level", "conf", "dist", "duration_sec",
                "RMS", "RMS_n", "ZCR", "WL", "WL_n", "MDF_Hz", "MNF_Hz", "ENV_PEAK_n"]
        with open(args.csv_out, "w", newline="") as fh:
            w = _csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"Wrote per-squeeze results to {args.csv_out}")

    fig, ax = plt.subplots(figsize=(14, 7))
    pad = 1.3 * float(np.percentile(np.abs(filtered), 99.9))
    ax.set_ylim(-pad, pad)
    ax.plot(x_counts, raw, color="lightgray", lw=1.0, alpha=0.5, label="Raw (scaled)")
    ax.plot(x_counts, filtered, color="blue", lw=1.0, alpha=0.7, label="Filtered")
    ax.plot(x_counts, envelope, color="red", lw=2.0, label="Envelope")
    ax.axhline(info["thr_hi"], color="k", ls="--", lw=1)
    ax.axhline(info["thr_lo"], color="k", ls=":", lw=1)
    for r in rows:
        uncertain = (r["conf"] < args.min_conf or r["dist"] > novelty_cut
                     or r["flagged"])
        ax.axvspan(x_counts[r["start"]], x_counts[r["end"]],
                   color=LEVEL_COLORS[r["level"]], alpha=0.4,
                   hatch="//" if uncertain else None, label=f"Level {r['level']}")
        ax.text(x_counts[(r["start"] + r["end"]) // 2], pad * 0.92,
                f"L{r['level']}\n{r['conf']:.0%}", ha="center", va="top", fontsize=8)
    ax.set_title(f"Classified EMG - {os.path.basename(args.csv_file)}",
                 fontsize=15, fontweight="bold")
    ax.set_xlabel("Sample count")
    ax.set_ylabel("Voltage (mV)")
    h, l = ax.get_legend_handles_labels()
    seen = dict(zip(l, h))
    ax.legend(seen.values(), seen.keys(), loc="upper right", fontsize=8)
    plt.show()


if __name__ == "__main__":
    main()
