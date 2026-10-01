"""
calibrate.py - interactive EMG labelling tool.

Keys
----
  click / <- / ->   select a block
  1 2 3 4 5         label the selected block, jump to the next unlabelled one
  u                 unlabel
  s                 split the selected block at its deepest internal dip
  m                 merge the selected block with the next one
  d                 discard the selected block
  enter             save the profile

Blocks whose duration is a big outlier are drawn hatched: they are usually a
sustained hold or two squeezes fused together, which no threshold can tell
apart. Fix them with 's' or 'd' before labelling.
"""

import argparse
import os
import sys

import numpy as np
import matplotlib.pyplot as plt

import emg_core as ec


def build_parser():
    p = argparse.ArgumentParser(description="Interactive EMG Calibration Tool.")
    p.add_argument("csv_file", help="Path to the input calibration CSV file")
    p.add_argument("--out", default="my_arm_profile.json", help="Output profile path")
    p.add_argument("--gain", type=float, default=1.0)
    p.add_argument("--fs", type=float, default=500.0, help="Sampling frequency (Hz)")
    p.add_argument("--env_hz", type=float, default=4.0, help="Envelope smoothing (Hz)")
    p.add_argument("--mains", type=float, default=60.0, help="Mains notch (0 disables)")
    p.add_argument("--thresh", default="auto", help="Threshold in mV, or 'auto'")
    p.add_argument("--min_dur", type=float, default=ec.DEFAULT_MIN_DURATION)
    p.add_argument("--min_gap", type=float, default=ec.DEFAULT_MIN_GAP)
    p.add_argument("--hysteresis", type=float, default=ec.DEFAULT_HYSTERESIS)
    p.add_argument("--split_valleys", type=float, default=None,
                   help="Auto-split fused blocks (try 0.5). Off by default; it "
                        "can over-segment a single rippled squeeze.")
    p.add_argument("--norm", default="p95", choices=["p95", "mvc", "noise", "none"],
                   help="Amplitude reference for cross-session normalization")
    p.add_argument("--mvc_index", type=int, default=0)
    p.add_argument("--onset_taper", type=float, default=ec.DEFAULT_ONSET_TAPER,
                   help="Down-weight this fraction of each block's onset before "
                        "computing amplitude features (0 disables)")
    p.add_argument("--release_taper", type=float, default=ec.DEFAULT_RELEASE_TAPER,
                   help="Down-weight this fraction of each block's release/tail before "
                        "computing amplitude features (0 disables)")
    p.add_argument("--show_sweep", action="store_true",
                   help="Print the threshold-vs-count sweep used to pick the threshold")
    return p


args = build_parser().parse_args()

LEVEL_COLORS = ec.LEVEL_COLORS
DEFAULT_COLOR = "lightgray"


def main():
    raw, x_counts = ec.load_emg_csv(args.csv_file, gain=args.gain)
    filtered, envelope = ec.preprocess(raw, args.fs, env_hz=args.env_hz,
                                       mains_hz=args.mains)

    info = ec.auto_threshold(envelope, args.fs, args.min_dur, args.min_gap,
                             args.hysteresis, args.split_valleys)
    if str(args.thresh).lower() != "auto":
        t = float(args.thresh)
        info.update(thr_hi=t, thr_lo=min(args.hysteresis * t, 0.95 * t),
                    method="manual", active_frac=float(np.mean(envelope > t)))
    print(ec.describe_threshold(info, "Detection "))
    if info["method"] == "stability" and info["plateau_width"] < 0.25:
        print("NOTE: the stable threshold range is narrow. Eyeball the blocks "
              "before trusting them.")
    if args.show_sweep:
        for t, c in zip(info["sweep_thresholds"], info["sweep_counts"]):
            print(f"    thr={t:.4f} -> {c} blocks")

    starts, ends = ec.detect_squeeze_bounds(envelope, args.fs, info["thr_hi"],
                                            info["thr_lo"], args.min_dur,
                                            args.min_gap, args.split_valleys)
    if not starts:
        print("No squeezes detected. Check --gain / --fs, or set --thresh manually.")
        sys.exit(1)

    blocks = [{"s": int(s), "e": int(e), "level": None} for s, e in zip(starts, ends)]
    print(f"Detected {len(blocks)} candidate squeezes.")
    for i, d in ec.flag_blocks(starts, ends, args.fs):
        print(f"  ! block #{i + 1} is {d:.1f}s - sustained hold or fused pair; "
              f"press 's' to split or 'd' to discard.")

    state = {"sel": 0, "spans": []}

    fig, ax = plt.subplots(figsize=(14, 7))
    plt.subplots_adjust(top=0.84)
    pad = 1.3 * float(np.percentile(np.abs(filtered), 99.9))
    ax.set_ylim(-pad, pad)
    ax.plot(x_counts, raw, color="lightgray", lw=1.0, alpha=0.5, label="Raw (scaled)")
    ax.plot(x_counts, filtered, color="blue", lw=1.0, alpha=0.7, label="Filtered")
    ax.plot(x_counts, envelope, color="red", lw=2.0, label="Envelope")
    ax.axhline(info["thr_hi"], color="k", ls="--", lw=1, label="thr hi")
    ax.axhline(info["thr_lo"], color="k", ls=":", lw=1, label="thr lo")
    ax.set_title(f"EMG Calibration - {os.path.basename(args.csv_file)}",
                 fontsize=15, fontweight="bold")
    ax.set_xlabel("Sample count")
    ax.set_ylabel("Voltage (mV)")
    ax.legend(loc="upper right", fontsize=8)
    fig.text(0.5, 0.965,
             "click/arrows select | 1-5 label | u unlabel | s split | m merge | "
             "d discard | enter save",
             ha="center", va="top", fontsize=10,
             bbox=dict(boxstyle="round,pad=0.3", ec="black", fc="white"))
    status = fig.text(0.5, 0.90, "", ha="center", va="top", fontsize=10)

    def redraw():
        for sp in state["spans"]:
            sp.remove()
        state["spans"] = []
        flags = dict(ec.flag_blocks([b["s"] for b in blocks],
                                    [b["e"] for b in blocks], args.fs))
        for i, b in enumerate(blocks):
            color = LEVEL_COLORS[b["level"]] if b["level"] else DEFAULT_COLOR
            sp = ax.axvspan(x_counts[b["s"]], x_counts[b["e"]], color=color,
                            alpha=0.7 if i == state["sel"] else
                            (0.5 if b["level"] else 0.4),
                            hatch="//" if i in flags else None, lw=0)
            if i == state["sel"]:
                sp.set_edgecolor("blue")
                sp.set_linewidth(3)
            state["spans"].append(sp)
        sel = state["sel"]
        txt = "none" if sel is None else f"#{sel + 1} ({(blocks[sel]['e'] - blocks[sel]['s']) / args.fs:.1f}s)"
        n_lab = sum(1 for b in blocks if b["level"])
        status.set_text(f"Selected: {txt}    Labeled: {n_lab}/{len(blocks)}")
        fig.canvas.draw_idle()

    def next_unlabeled(start):
        order = list(range(start, len(blocks))) + list(range(0, start))
        for i in order:
            if blocks[i]["level"] is None:
                return i
        return None

    def on_click(event):
        if event.inaxes != ax or event.xdata is None:
            return
        for i, b in enumerate(blocks):
            if x_counts[b["s"]] <= event.xdata <= x_counts[b["e"]]:
                state["sel"] = i
                redraw()
                return
        state["sel"] = None
        redraw()

    def on_key(event):
        sel = state["sel"]
        if event.key == "enter":
            save()
            return
        if event.key in ("left", "right") and blocks:
            base = 0 if sel is None else sel
            state["sel"] = int(np.clip(base + (1 if event.key == "right" else -1),
                                       0, len(blocks) - 1))
            redraw()
            return
        if sel is None:
            return
        b = blocks[sel]

        if event.key == "d":
            blocks.pop(sel)
            state["sel"] = min(sel, len(blocks) - 1) if blocks else None
            redraw()
        elif event.key == "m" and sel < len(blocks) - 1:
            nxt = blocks.pop(sel + 1)
            b["e"] = nxt["e"]
            b["level"] = None
            redraw()
        elif event.key == "s":
            # cut at the deepest dip inside the central 80% of the block
            lo = b["s"] + int(0.1 * (b["e"] - b["s"]))
            hi = b["e"] - int(0.1 * (b["e"] - b["s"]))
            if hi - lo < int(0.2 * args.fs):
                print("Block too short to split.")
                return
            cut = lo + int(np.argmin(envelope[lo:hi]))
            new = {"s": cut + 1, "e": b["e"], "level": None}
            b["e"], b["level"] = cut, None
            blocks.insert(sel + 1, new)
            print(f"Split block #{sel + 1} at sample {x_counts[cut]} "
                  f"(envelope {envelope[cut]:.4f} mV).")
            redraw()
        elif event.key == "u":
            b["level"] = None
            redraw()
        elif event.key in "12345":
            b["level"] = int(event.key)
            state["sel"] = next_unlabeled(sel + 1)
            redraw()

    def save():
        labeled = [b for b in blocks if b["level"]]
        if not labeled:
            print("No squeezes labeled. Profile not saved.")
            return
        amp_ref = ec.amplitude_reference(
            filtered, envelope, [b["s"] for b in labeled], [b["e"] for b in labeled],
            mode=args.norm, mvc_index=args.mvc_index, noise_sigma=info["sigma"])

        samples = []
        for b in labeled:
            f = ec.extract_features(filtered[b["s"]:b["e"] + 1], args.fs, amp_ref,
                                    envelope_segment=envelope[b["s"]:b["e"] + 1],
                                    onset_taper_frac=args.onset_taper,
                                    release_taper_frac=args.release_taper)
            if f is None:
                continue
            f.update(Level=b["level"], start_sample=b["s"], end_sample=b["e"])
            samples.append(f)

        counts = {lv: sum(1 for s in samples if s["Level"] == lv) for lv in range(1, 6)}
        ec.save_profile(args.out, samples, {
            "source_csv": os.path.abspath(args.csv_file), "fs": args.fs,
            "gain": args.gain, "env_hz": args.env_hz, "mains_hz": args.mains,
            "norm_mode": args.norm, "amp_ref": amp_ref, "threshold": info,
            "min_duration_sec": args.min_dur, "min_gap_sec": args.min_gap,
            "hysteresis": args.hysteresis, "split_valleys": args.split_valleys,
            "onset_taper": args.onset_taper, "release_taper": args.release_taper,
            "level_counts": counts})
        print(f"\nSaved {len(samples)} labeled squeezes -> {args.out}")
        print(f"Per-level counts: {counts}   (amp_ref={amp_ref:.5f}, {args.norm})")
        thin = [lv for lv, c in counts.items() if 0 < c < 3]
        if thin:
            print(f"WARNING: levels {thin} have <3 examples; KNN will be shaky there.")
        plt.close(fig)

    fig.canvas.mpl_connect("button_press_event", on_click)
    fig.canvas.mpl_connect("key_press_event", on_key)
    redraw()
    plt.show()


if __name__ == "__main__":
    main()
