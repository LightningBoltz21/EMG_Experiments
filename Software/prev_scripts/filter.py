import csv
import matplotlib.pyplot as plt
from matplotlib.widgets import Slider
import sys
import os
import argparse
import numpy as np
from scipy.signal import butter, sosfiltfilt

# --- Argument Parsing ---
parser = argparse.ArgumentParser(description="Filter and Analyze EMG CSV data.")
parser.add_argument("csv_file", help="Path to the input CSV file")
parser.add_argument("--gain", type=float, default=1.0, help="Scaling gain factor")
parser.add_argument("--fs", type=float, default=500.0, help="Sampling frequency in Hz")
# UPDATED: Default envelope smoothing is now 10.0 Hz
parser.add_argument("--env_hz", type=float, default=10.0, help="Initial low-pass cutoff for the envelope (Hz)")

args = parser.parse_args()

# --- Intensity Classification Thresholds & Colors ---
LEVEL_THRESHOLDS = {
    1: 0.15, # UPDATED: Level 1 Default Base Threshold
    2: 0.5,  # Level 2 
    3: 1.0,  # Level 3 
    4: 1.5,  # Level 4 
    5: 2.0   # Level 5 (Max)
}

LEVEL_COLORS = {
    1: 'lime',
    2: '#FFEA00',
    3: 'orange',
    4: 'darkorange',
    5: '#404040'
}

LEVEL_LABELS = {
    1: 'Level 1 (Base+ mV)',
    2: 'Level 2 (0.5+ mV)',
    3: 'Level 3 (1.0+ mV)',
    4: 'Level 4 (1.5+ mV)',
    5: 'Level 5 (2.0+ mV)'
}

# --- Signal Processing Functions ---

def apply_bandpass_filter(data, fs, lowcut=20.0, highcut=200.0):
    nyquist = 0.5 * fs
    if highcut >= nyquist:
        highcut = nyquist - 1.0  
    sos = butter(4, [lowcut, highcut], btype='bandpass', fs=fs, output='sos')
    return sosfiltfilt(sos, data)

def get_amplitude_envelope(data, fs, lp_cutoff):
    rectified_data = np.abs(data)
    sos = butter(4, lp_cutoff, btype='lowpass', fs=fs, output='sos')
    return sosfiltfilt(sos, rectified_data)

def detect_squeeze_bounds(envelope, fs, threshold, min_duration_sec, min_gap_sec):
    min_duration_samples = int(min_duration_sec * fs)
    min_gap_samples = int(min_gap_sec * fs)

    is_active = (envelope > threshold).astype(int)
    if np.sum(is_active) == 0:
        return [], []

    diff = np.diff(is_active)
    starts = np.where(diff == 1)[0] + 1
    ends = np.where(diff == -1)[0] + 1

    if is_active[0]:
        starts = np.insert(starts, 0, 0)
    if is_active[-1]:
        ends = np.append(ends, len(envelope))

    # MERGE: Bridge gaps
    merged_starts, merged_ends = [], []
    for i in range(len(starts)):
        if i == 0:
            merged_starts.append(starts[i])
            merged_ends.append(ends[i])
        else:
            if starts[i] - merged_ends[-1] < min_gap_samples:
                merged_ends[-1] = ends[i] 
            else:
                merged_starts.append(starts[i])
                merged_ends.append(ends[i])

    # REJECT: Throw out squeezes that are too short
    final_starts, final_ends = [], []
    for s, e in zip(merged_starts, merged_ends):
        if (e - s) >= min_duration_samples:
            final_starts.append(s)
            final_ends.append(e)

    return final_starts, final_ends


# --- Main Plotting Routine ---

def plot_emg_csv(file_path):
    if not os.path.exists(file_path):
        print(f"Error: Could not find the file '{file_path}'.")
        sys.exit(1)

    x_counts, y_voltages = [], []

    print(f"Loading data from {file_path}...")
    try:
        with open(file_path, 'r') as file:
            reader = csv.reader(file)
            header = next(reader) 
            for row in reader:
                if len(row) >= 2:
                    y_voltages.append(float(row[0]))
                    x_counts.append(int(row[1]))
    except Exception as e:
        print(f"Error parsing the CSV file: {e}")
        sys.exit(1)

    y_voltages = np.array(y_voltages) * args.gain
    filtered_voltages = apply_bandpass_filter(y_voltages, args.fs, lowcut=20.0, highcut=200.0)
    x_counts = np.array(x_counts)

    # --- Plot Setup ---
    fig, ax = plt.subplots(figsize=(14, 8))
    plt.subplots_adjust(bottom=0.35) 
    
    ax.set_ylim(-30, 30)
    
    ax.plot(x_counts, y_voltages, color='lightgray', linewidth=1.0, alpha=0.5, label='Raw (Scaled)')
    ax.plot(x_counts, filtered_voltages, color='blue', linewidth=1.0, alpha=0.7, label='Filtered (20-200Hz)')
    
    initial_envelope = get_amplitude_envelope(filtered_voltages, args.fs, lp_cutoff=args.env_hz)
    envelope_line, = ax.plot(x_counts, initial_envelope, color='red', linewidth=2.5, label=f'Amplitude Envelope ({args.env_hz}Hz LP)')
    
    ax.set_title(f"EMG Sensor Data Analysis - {file_path}", fontsize=14, fontweight='bold')
    ax.set_xlabel("Sample Count", fontsize=12)
    ax.set_ylabel("Voltage (mV)", fontsize=12)
    ax.grid(True, linestyle='--', alpha=0.6)

    # --- Shading Logic ---
    poly_collections = [] 

    def draw_highlights(env_data, base_thresh, min_dur, min_gap):
        nonlocal poly_collections
        for coll in poly_collections:
            try:
                coll.remove()
            except ValueError:
                pass
        poly_collections.clear()

        # Update the Level 1 threshold dynamically based on the slider
        LEVEL_THRESHOLDS[1] = base_thresh

        starts, ends = detect_squeeze_bounds(
            env_data, 
            fs=args.fs, 
            threshold=base_thresh, 
            min_duration_sec=min_dur, 
            min_gap_sec=min_gap
        )

        y_min, y_max = ax.get_ylim()

        for s, e in zip(starts, ends):
            peak_mv = np.max(env_data[s:e])
            
            if peak_mv >= LEVEL_THRESHOLDS[5]:
                lvl = 5
            elif peak_mv >= LEVEL_THRESHOLDS[4]:
                lvl = 4
            elif peak_mv >= LEVEL_THRESHOLDS[3]:
                lvl = 3
            elif peak_mv >= LEVEL_THRESHOLDS[2]:
                lvl = 2
            else:
                lvl = 1

            c = ax.fill_between(
                x_counts[s:e], y_min, y_max, 
                color=LEVEL_COLORS[lvl], alpha=0.4, label=LEVEL_LABELS[lvl]
            )
            poly_collections.append(c)

    # UPDATED: Initial Draw uses the new defaults
    draw_highlights(initial_envelope, base_thresh=0.15, min_dur=0.1, min_gap=0.0)
    
    handles, labels = ax.get_legend_handles_labels()
    by_label = dict(zip(labels, handles))
    ax.legend(by_label.values(), by_label.keys(), loc="upper right")
    
    # --- Interactive Sliders Setup ---
    # [left, bottom, width, height]
    ax_slider_env    = plt.axes([0.25, 0.22, 0.65, 0.02])
    ax_slider_thresh = plt.axes([0.25, 0.17, 0.65, 0.02])
    ax_slider_gap    = plt.axes([0.25, 0.12, 0.65, 0.02])
    ax_slider_dur    = plt.axes([0.25, 0.07, 0.65, 0.02])

    # UPDATED: valinit parameters match the requested defaults
    env_slider    = Slider(ax=ax_slider_env,    label='Envelope Smoothing (Hz)', valmin=0.5, valmax=20.0, valinit=args.env_hz, valstep=0.5)
    thresh_slider = Slider(ax=ax_slider_thresh, label='Base Threshold (mV)',   valmin=0.01, valmax=0.45, valinit=0.15, valstep=0.01)
    gap_slider    = Slider(ax=ax_slider_gap,    label='Merge Gap (sec)',       valmin=0.0, valmax=1.0, valinit=0.0, valstep=0.05)
    dur_slider    = Slider(ax=ax_slider_dur,    label='Min Duration (sec)',    valmin=0.0, valmax=1.0, valinit=0.1, valstep=0.05)

    def update(val):
        # 1. Grab all current slider values
        new_hz     = env_slider.val
        new_thresh = thresh_slider.val
        new_gap    = gap_slider.val
        new_dur    = dur_slider.val
        
        # 2. Recalculate envelope based on new Hz
        new_envelope = get_amplitude_envelope(filtered_voltages, args.fs, lp_cutoff=new_hz)
        envelope_line.set_ydata(new_envelope)
        envelope_line.set_label(f'Amplitude Envelope ({new_hz:.1f}Hz LP)')
        
        # 3. Redraw the shaded blocks using the new debouncing parameters
        draw_highlights(new_envelope, new_thresh, new_dur, new_gap)
        
        # 4. Refresh legend and canvas
        handles, labels = ax.get_legend_handles_labels()
        by_label = dict(zip(labels, handles))
        ax.legend(by_label.values(), by_label.keys(), loc="upper right")
        
        fig.canvas.draw_idle()

    # Link all 4 sliders to the single update function
    env_slider.on_changed(update)
    thresh_slider.on_changed(update)
    gap_slider.on_changed(update)
    dur_slider.on_changed(update)

    # Activate 'Zoom to rectangle' by default
    toolbar = fig.canvas.toolbar
    if toolbar is not None:
        toolbar.zoom()

    plt.show()

if __name__ == '__main__':
    plot_emg_csv(args.csv_file)