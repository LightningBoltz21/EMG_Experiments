import csv
import sys
import os
import argparse
import json
import numpy as np
import matplotlib.pyplot as plt
from scipy.signal import butter, sosfiltfilt, welch

# --- Machine Learning Imports ---
from sklearn.neighbors import KNeighborsClassifier
from sklearn.preprocessing import StandardScaler

# --- Argument Parsing ---
parser = argparse.ArgumentParser(description="Machine-Learning-Powered EMG Analyzer using KNN.")
parser.add_argument("csv_file", help="Path to the new EMG CSV file to analyze")
parser.add_argument("--profile", default="my_arm_profile.json", help="Path to your calibrated JSON profile")
parser.add_argument("--gain", type=float, default=1.0, help="Scaling gain factor")
parser.add_argument("--fs", type=float, default=500.0, help="Sampling frequency in Hz")
parser.add_argument("--env_hz", type=float, default=4.0, help="Envelope smoothing (Hz)")
parser.add_argument("--thresh", type=float, default=0.09, help="Base detection threshold (mV) to find squeeze bounds")

args = parser.parse_args()

# --- Colors for Classification ---
LEVEL_COLORS = {
    1: 'lime',
    2: '#FFEA00',
    3: 'orange',
    4: '#FF0000',
    5: '#404040'
}

LEVEL_LABELS = {
    1: 'Level 1 (Predicted)',
    2: 'Level 2 (Predicted)',
    3: 'Level 3 (Predicted)',
    4: 'Level 4 (Predicted)',
    5: 'Level 5 (Predicted)'
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

def detect_squeeze_bounds(envelope, fs, threshold, min_duration_sec=0.3, min_gap_sec=0.45):
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

def extract_features(raw_signal, fs):
    rms = np.sqrt(np.mean(raw_signal**2))
    zcr = np.sum(np.diff(np.sign(raw_signal)) != 0) / len(raw_signal)
    wl = np.sum(np.abs(np.diff(raw_signal)))
    
    nperseg = min(len(raw_signal), 256) 
    freqs, psd = welch(raw_signal, fs, nperseg=nperseg)
    cumulative_power = np.cumsum(psd)
    total_power = cumulative_power[-1]
    mdf_idx = np.where(cumulative_power >= total_power / 2)[0][0]
    mdf = freqs[mdf_idx]

    return [rms, zcr, wl, mdf]

# --- Main Application ---

def main():
    # 1. Load the Calibration Profile
    if not os.path.exists(args.profile):
        print(f"Error: Calibration profile '{args.profile}' not found!")
        sys.exit(1)
        
    with open(args.profile, 'r') as f:
        profile_data = json.load(f)
        
    # 2. Prepare the ML Training Data
    X_train = []
    y_train = []
    for item in profile_data:
        X_train.append([item["RMS"], item["ZCR"], item["WL"], item["MDF_Hz"]])
        y_train.append(item["Level"])
        
    X_train = np.array(X_train)
    y_train = np.array(y_train)

    # 3. Scale the features and train the KNN Classifier
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    
    # We use 3 neighbors to vote on the classification
    knn = KNeighborsClassifier(n_neighbors=3)
    knn.fit(X_train_scaled, y_train)
    print(f"Loaded profile with {len(y_train)} labeled squeezes. Machine Learning Model trained successfully.")

    # 4. Load the Target CSV File
    if not os.path.exists(args.csv_file):
        print(f"Error: File '{args.csv_file}' not found.")
        sys.exit(1)

    x_counts, y_voltages = [], []
    with open(args.csv_file, 'r') as file:
        reader = csv.reader(file)
        next(reader) 
        for row in reader:
            if len(row) >= 2:
                y_voltages.append(float(row[0]))
                x_counts.append(int(row[1]))

    # 5. Signal Processing
    y_voltages = np.array(y_voltages) * args.gain
    filtered_voltages = apply_bandpass_filter(y_voltages, args.fs)
    envelope = get_amplitude_envelope(filtered_voltages, args.fs, args.env_hz)
    x_counts = np.array(x_counts)

    starts, ends = detect_squeeze_bounds(envelope, args.fs, args.thresh)

    # 6. Plot Setup
    fig, ax = plt.subplots(figsize=(14, 7))
    ax.set_ylim(-15, 15)  # Adjust based on your typical max voltage
    
    ax.plot(x_counts, y_voltages, color='lightgray', linewidth=1.0, alpha=0.5, label='Raw (Scaled)')
    ax.plot(x_counts, filtered_voltages, color='blue', linewidth=1.0, alpha=0.7, label='Filtered')
    ax.plot(x_counts, envelope, color='red', linewidth=2.0, label='Envelope')

    # 7. ML Prediction & Coloring
    y_min, y_max = ax.get_ylim()
    
    for s, e in zip(starts, ends):
        block_data = filtered_voltages[s:e]
        
        # Extract features for this new unknown squeeze
        new_features = extract_features(block_data, args.fs)
        
        # Scale the new features using the SAME scaler from training
        new_features_scaled = scaler.transform([new_features])
        
        # Ask the ML to predict the level!
        predicted_level = int(knn.predict(new_features_scaled)[0])
        
        # Paint the block based on the prediction
        ax.fill_between(
            x_counts[s:e], y_min, y_max, 
            color=LEVEL_COLORS[predicted_level], 
            alpha=0.4, 
            label=LEVEL_LABELS[predicted_level]
        )

    ax.set_title(f"AI-Classified EMG Data - {args.csv_file}", fontsize=16, fontweight='bold')
    ax.set_xlabel("Sample Count")
    ax.set_ylabel("Voltage (mV)")
    
    # Deduplicate legend
    handles, labels = ax.get_legend_handles_labels()
    by_label = dict(zip(labels, handles))
    ax.legend(by_label.values(), by_label.keys(), loc="upper right")

    # Enable Zoom to Rectangle
    toolbar = fig.canvas.toolbar
    if toolbar is not None:
        toolbar.zoom()

    plt.show()

if __name__ == '__main__':
    main()