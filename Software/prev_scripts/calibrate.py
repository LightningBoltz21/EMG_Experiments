import csv
import sys
import os
import argparse
import json
import numpy as np
import matplotlib.pyplot as plt
from scipy.signal import butter, sosfiltfilt, welch

# --- Argument Parsing ---
parser = argparse.ArgumentParser(description="Interactive EMG Calibration Tool.")
parser.add_argument("csv_file", help="Path to the input calibration CSV file")
parser.add_argument("--gain", type=float, default=1.0, help="Scaling gain factor")
parser.add_argument("--fs", type=float, default=500.0, help="Sampling frequency in Hz")
parser.add_argument("--env_hz", type=float, default=4.0, help="Envelope smoothing (Hz)")
parser.add_argument("--thresh", type=float, default=0.09, help="Base threshold for detection (mV)")

args = parser.parse_args()

# --- Colors for Classification ---
LEVEL_COLORS = {
    1: 'lime',
    2: '#FFEA00',   # Vibrant Yellow
    3: 'orange',
    4: 'darkorange',
    5: '#404040'    # Black-Gray
}
DEFAULT_COLOR = 'lightgray'
SELECTED_EDGE_COLOR = 'blue'

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

def detect_squeeze_bounds(envelope, fs, threshold, min_duration_sec=0.1, min_gap_sec=0.0):
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
        ends = np.append(ends, len(envelope) - 1)

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

    final_starts, final_ends = [], []
    for s, e in zip(merged_starts, merged_ends):
        if (e - s) >= min_duration_samples:
            final_starts.append(s)
            final_ends.append(e)

    return final_starts, final_ends


# --- Feature Extraction ---

def extract_features(raw_signal, fs):
    """Calculates advanced mathematical features of a specific muscle squeeze."""
    # 1. RMS (Root Mean Square) - Power of the signal
    rms = np.sqrt(np.mean(raw_signal**2))
    
    # 2. ZCR (Zero-Crossing Rate) - How often the signal flips polarity
    # Normalized by the length of the squeeze
    zcr = np.sum(np.diff(np.sign(raw_signal)) != 0) / len(raw_signal)
    
    # 3. WL (Waveform Length) - The complexity/cumulative travel of the wave
    wl = np.sum(np.abs(np.diff(raw_signal)))
    
    # 4. MDF (Median Frequency) - Power spectrum analysis via Welch's method
    # Ensure nperseg is not larger than the data array itself
    nperseg = min(len(raw_signal), 256) 
    freqs, psd = welch(raw_signal, fs, nperseg=nperseg)
    
    cumulative_power = np.cumsum(psd)
    total_power = cumulative_power[-1]
    # Find the frequency where cumulative power hits 50%
    mdf_idx = np.where(cumulative_power >= total_power / 2)[0][0]
    mdf = freqs[mdf_idx]

    return {
        "RMS": float(rms),
        "ZCR": float(zcr),
        "WL": float(wl),
        "MDF_Hz": float(mdf)
    }


# --- Interactive GUI ---

def calibrate_gui(file_path):
    if not os.path.exists(file_path):
        print(f"Error: File '{file_path}' not found.")
        sys.exit(1)

    x_counts, y_voltages = [], []
    with open(file_path, 'r') as file:
        reader = csv.reader(file)
        next(reader) 
        for row in reader:
            if len(row) >= 2:
                y_voltages.append(float(row[0]))
                x_counts.append(int(row[1]))

    # Data Processing
    y_voltages = np.array(y_voltages) * args.gain
    filtered_voltages = apply_bandpass_filter(y_voltages, args.fs)
    envelope = get_amplitude_envelope(filtered_voltages, args.fs, args.env_hz)
    x_counts = np.array(x_counts)

    # Detect Squeezes
    # starts, ends = detect_squeeze_bounds(envelope, args.fs, args.thresh)

    # Detect Squeezes (Added 250ms gap bridging and 150ms minimum duration)
    starts, ends = detect_squeeze_bounds(
        envelope,
        args.fs,
        args.thresh,
        min_duration_sec=0.3,
        min_gap_sec=0.45
    )
    
    # State tracking
    selected_idx = None
    squeeze_labels = {}   # maps squeeze index -> level (1-5)
    squeeze_features = {} # maps squeeze index -> feature dictionary
    axvspans = []         # stores the matplotlib polygon objects for redrawing

    # Plot Setup
    fig, ax = plt.subplots(figsize=(14, 7))
    plt.subplots_adjust(top=0.85) # Leave room for instructions at the top
    ax.set_ylim(-30, 30)
    
    ax.plot(x_counts, y_voltages, color='lightgray', linewidth=1.0, alpha=0.5, label='Raw (Scaled)')
    ax.plot(x_counts, filtered_voltages, color='blue', linewidth=1.0, alpha=0.7, label='Filtered')
    ax.plot(x_counts, envelope, color='red', linewidth=2.0, label='Envelope')

    # Draw initial gray boxes over detected squeezes
    for i, (s, e) in enumerate(zip(starts, ends)):
        span = ax.axvspan(x_counts[s], x_counts[e], color=DEFAULT_COLOR, alpha=0.4, lw=2)
        axvspans.append(span)

    ax.set_title("EMG Calibration Tool", fontsize=16, fontweight='bold')
    ax.set_xlabel("Sample Count")
    ax.set_ylabel("Voltage (mV)")
    
    instructions = (
        "1. CLICK a gray block to select it.\n"
        "2. PRESS 1, 2, 3, 4, or 5 to label the squeeze intensity.\n"
        "3. PRESS 'Enter' when finished to save your muscle profile."
    )
    fig.text(0.5, 0.95, instructions, ha='center', va='top', fontsize=11, 
             bbox=dict(boxstyle="round,pad=0.3", edgecolor="black", facecolor="white"))

    # --- Interaction Logic ---
    
    def update_block_colors():
        """Refreshes the colors of the blocks based on selection and labels."""
        for i, span in enumerate(axvspans):
            # Base color
            if i in squeeze_labels:
                span.set_color(LEVEL_COLORS[squeeze_labels[i]])
                span.set_alpha(0.5)
            else:
                span.set_color(DEFAULT_COLOR)
                span.set_alpha(0.4)
            
            # Edge color for selection
            if i == selected_idx:
                span.set_edgecolor(SELECTED_EDGE_COLOR)
                span.set_linewidth(3)
                span.set_alpha(0.7)
            else:
                span.set_edgecolor('none')
                span.set_linewidth(0)
                
        fig.canvas.draw_idle()

    def on_click(event):
        nonlocal selected_idx
        if event.inaxes != ax: return
        
        # Check which squeeze was clicked
        click_x = event.xdata
        for i, (s, e) in enumerate(zip(starts, ends)):
            if x_counts[s] <= click_x <= x_counts[e]:
                selected_idx = i
                update_block_colors()
                print(f"Selected Squeeze #{i+1}")
                return
        
        # If clicked in empty space, deselect
        selected_idx = None
        update_block_colors()

    def on_key(event):
        nonlocal selected_idx
        
        if event.key == 'enter':
            save_profile()
            return
            
        if selected_idx is None:
            return
            
        if event.key in ['1', '2', '3', '4', '5']:
            level = int(event.key)
            squeeze_labels[selected_idx] = level
            
            # Extract features for this specific block!
            # We use the FILTERED data (not the envelope) to calculate features
            s = starts[selected_idx]
            e = ends[selected_idx]
            block_data = filtered_voltages[s:e]
            
            features = extract_features(block_data, args.fs)
            features["Level"] = level
            squeeze_features[selected_idx] = features
            
            print(f"Labeled Squeeze #{selected_idx+1} as Level {level}.")
            print(f"  -> Features: {features}")
            
            # Move selection to the next unlabeled block automatically
            selected_idx += 1
            if selected_idx >= len(starts):
                selected_idx = None
                
            update_block_colors()

    def save_profile():
        if not squeeze_features:
            print("No squeezes labeled. Profile not saved.")
            return
            
        profile_data = [features for idx, features in squeeze_features.items()]
        
        output_file = "my_arm_profile.json"
        with open(output_file, 'w') as f:
            json.dump(profile_data, f, indent=4)
            
        print(f"\nSUCCESS: Profile saved to {output_file}!")
        print(f"Labeled a total of {len(profile_data)} distinct squeezes.")
        sys.exit(0)

    # Bind events
    fig.canvas.mpl_connect('button_press_event', on_click)
    fig.canvas.mpl_connect('key_press_event', on_key)

    # Enable Zoom to Rectangle by default
    toolbar = fig.canvas.toolbar
    if toolbar is not None:
        toolbar.zoom()

    plt.show()

if __name__ == '__main__':
    calibrate_gui(args.csv_file)