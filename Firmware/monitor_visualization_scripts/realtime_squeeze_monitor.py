"""
realtime_squeeze_monitor.py
============================
Live sibling to filtered_plotter.py: connects to the same serial board,
shows a real-time filtered trace, and classifies squeeze level (1-5) as you
squeeze, using a calibration profile from Filtering_Software/current_scripts
(my_arm_profile.json by default).

Two deliberate compromises vs. the offline analyze.py pipeline, both
explained in emg_core.py's streaming classes:
  * Filtering is genuinely zero-phase (WindowedZeroPhaseFilter), at the cost
    of a small fixed delay (~confirm_sec) instead of a phase-distorted
    causal filter.
  * Detection thresholds are not the profile's static calibration numbers --
    they're rescaled live from the profile's noise-sigma ratio against a
    continuously re-measured noise floor (RollingThresholdTracker), since
    absolute EMG amplitude drifts across sessions and electrode placements.

Usage:
  python realtime_squeeze_monitor.py                     # connect to a real board
  python realtime_squeeze_monitor.py --replay path.csv   # replay a saved recording,
                                                           # no hardware needed
  python realtime_squeeze_monitor.py --profile my_arm_profile.json
"""
import argparse
import csv
import json
import os
import queue
import random
import sys
import time
from collections import deque
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "Filtering_Software", "current_scripts"))
import emg_core as ec  # noqa: E402  (after sys.path shim)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filtered_plotter import SerialWorker, SAMPLING_RATE_HZ, CHUNK_SIZE  # noqa: E402

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QLabel, QComboBox, QPushButton,
                             QLineEdit, QTableWidget, QTableWidgetItem,
                             QHeaderView, QProgressBar)
from PyQt6.QtCore import QThread, QObject, QTimer, pyqtSignal, Qt
from PyQt6.QtGui import QFont
import pyqtgraph as pg

DEFAULT_PROFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "..", "Filtering_Software", "my_arm_profile.json")
DATA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
PLOT_WINDOW_SEC = 6.0


class GrowingBuffer:
    """Amortized-O(1)-append numpy buffer -- avoids the O(n^2) cost of
    np.concatenate on every chunk over a long session."""

    def __init__(self, initial_capacity=1 << 14):
        self.data = np.empty(initial_capacity, dtype=float)
        self.length = 0

    def append(self, chunk):
        chunk = np.asarray(chunk, dtype=float)
        n = len(chunk)
        if n == 0:
            return
        if self.length + n > len(self.data):
            new_cap = max(len(self.data) * 2, self.length + n)
            new_data = np.empty(new_cap, dtype=float)
            new_data[:self.length] = self.data[:self.length]
            self.data = new_data
        self.data[self.length:self.length + n] = chunk
        self.length += n

    def array(self):
        return self.data[:self.length]

    def slice(self, s, e):
        return self.data[s:e]


class ReplayWorker(QThread):
    """Same interface as SerialWorker, but replays a saved raw CSV at
    real-time-equivalent pacing. Lets the monitor (and its detection logic)
    be exercised without the board attached."""
    data_received = pyqtSignal(list)

    def __init__(self, csv_path, fs=SAMPLING_RATE_HZ, chunk_size=CHUNK_SIZE):
        super().__init__()
        self.csv_path = csv_path
        self.fs = fs
        self.chunk_size = chunk_size
        self.running = False

    def run(self):
        self.running = True
        raw, _ = ec.load_emg_csv(self.csv_path)
        dt = self.chunk_size / self.fs
        i, n = 0, len(raw)
        print(f"Replaying {self.csv_path}: {n} samples at {self.fs} Hz.")
        while self.running and i < n:
            t0 = time.monotonic()
            self.data_received.emit(raw[i:i + self.chunk_size].tolist())
            i += self.chunk_size
            elapsed = time.monotonic() - t0
            if dt > elapsed:
                time.sleep(dt - elapsed)
        print(f"Replay finished: {i} of {n} samples.")
        self.running = False

    def stop(self):
        self.running = False
        self.wait()


class DSPWorker(QThread):
    """Runs the streaming filter -> rolling threshold -> squeeze detector ->
    classifier pipeline in its own thread, fed via a thread-safe queue so it
    never blocks (or is blocked by) serial I/O or the GUI event loop."""
    chunk_ready = pyqtSignal(object, object)          # filt_chunk, env_chunk
    threshold_updated = pyqtSignal(float, float)       # thr_hi, thr_lo
    amp_ref_updated = pyqtSignal(float)
    squeeze_started = pyqtSignal()
    squeeze_provisional = pyqtSignal(dict)
    squeeze_final = pyqtSignal(dict)

    def __init__(self, profile_path):
        super().__init__()
        profile = ec.load_profile(profile_path)
        meta = profile["meta"]
        self.fs = float(meta.get("fs", SAMPLING_RATE_HZ))
        # Must match whatever taper the profile's own samples were built
        # with, not just today's default -- otherwise train/inference skew.
        self.onset_taper = float(meta.get("onset_taper", ec.DEFAULT_ONSET_TAPER))
        self.release_taper = float(meta.get("release_taper", ec.DEFAULT_RELEASE_TAPER))
        self.amp_ref_tracker = ec.AdaptiveAmplitudeReference(profile)
        self.amp_ref = self.amp_ref_tracker.value
        thr = meta["threshold"]

        self.knn, self.scaler, self.order = ec.train_classifier(profile)
        self.wf = ec.WindowedZeroPhaseFilter(
            self.fs, mains_hz=meta.get("mains_hz", 60.0), env_hz=meta.get("env_hz", 4.0),
            buffer_sec=2.5, confirm_sec=0.4)
        self.tracker = ec.RollingThresholdTracker(
            self.fs, thr["thr_hi"], thr["thr_lo"], thr["baseline"], thr["sigma"],
            noise_window_sec=18.0, update_period_sec=1.0)
        self.detector = ec.LiveSqueezeDetector(
            self.fs, min_duration_sec=meta.get("min_duration_sec", ec.DEFAULT_MIN_DURATION),
            min_gap_sec=meta.get("min_gap_sec", ec.DEFAULT_MIN_GAP))
        # Samples while a squeeze is active, and for this long after it ends,
        # are never trusted as "quiet" for the threshold tracker's noise
        # floor -- a hard contraction's envelope decays slowly, not
        # instantly (Session_008: baseline kept climbing for ~10s+ after a
        # Level 5 squeeze even though the block had already ended cleanly,
        # because that decay tail still got sampled as if it were rest).
        self.post_squeeze_cooldown_len = int(round(2.0 * self.fs))
        self.cooldown_until_sample = 0

        self.raw_hist = GrowingBuffer()
        self.filt_hist = GrowingBuffer()
        self.env_hist = GrowingBuffer()

        self._queue = queue.Queue()
        self._stop = False

    def enqueue_chunk(self, chunk):
        self._queue.put(chunk)

    def report_cue_ground_truth(self, requested_level, raw_rms):
        """Called (from the GUI thread) whenever a detected squeeze gets
        paired with a cue's requested level -- the only source of ground
        truth this app has for what today's raw signal *should* look like at
        a given level. Rescales amp_ref for all future classifications."""
        self.amp_ref = self.amp_ref_tracker.update(requested_level, raw_rms)
        self.amp_ref_updated.emit(self.amp_ref)

    def run(self):
        self._stop = False
        while not self._stop:
            try:
                chunk = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._process(chunk)

    def stop(self):
        self._stop = True
        self.wait()

    def _process(self, raw_chunk):
        self.raw_hist.append(raw_chunk)
        filt_chunk, env_chunk = self.wf.process_chunk(raw_chunk)
        if len(filt_chunk) == 0:
            return

        s0 = self.filt_hist.length
        self.filt_hist.append(filt_chunk)
        self.env_hist.append(env_chunk)

        # Gating this on detector.active too (not just the fixed cooldown
        # timer) was tried and reverted: it creates a circular dependency --
        # if thr_lo is ever wrong enough that a block can't close, tracker
        # updates get blocked, so thr_lo can never correct itself, so the
        # block still can't close. Validated against Sessions 7/8/9's real
        # raw.csv: that version silently starved the tracker mid-session in
        # Session_008 (zero detections for 45+ seconds) and dropped pairing
        # rate in all three sessions. The cooldown alone -- a fixed duration
        # from an already-happened 'end' event -- can't create that loop: it
        # always expires on its own regardless of what the thresholds are.
        exclude = s0 < self.cooldown_until_sample
        thr_hi, thr_lo = self.tracker.update(env_chunk, exclude=exclude)
        self.threshold_updated.emit(thr_hi, thr_lo)
        self.chunk_ready.emit(filt_chunk, env_chunk)

        indices = np.arange(s0, self.filt_hist.length)
        for ev in self.detector.update(indices, env_chunk, thr_hi, thr_lo):
            if ev["type"] == "start":
                self.squeeze_started.emit()
                continue
            s, e = ev["start"], ev["end"]
            if ev["type"] == "end":
                self.cooldown_until_sample = e + self.post_squeeze_cooldown_len
            seg = self.filt_hist.slice(s, e + 1)
            env_seg = self.env_hist.slice(s, e + 1)
            feat = ec.extract_features(seg, self.fs, self.amp_ref, envelope_segment=env_seg,
                                       onset_taper_frac=self.onset_taper,
                                       release_taper_frac=self.release_taper)
            if feat is None:
                continue
            vs = self.scaler.transform(np.array([[feat[k] for k in self.order]], float))
            proba = self.knn.predict_proba(vs)[0]
            result = dict(start=s, end=e, level=int(self.knn.classes_[np.argmax(proba)]),
                         conf=float(proba.max()), duration=(e - s + 1) / self.fs,
                         t=s / self.fs, raw_rms=feat["RMS"], amp_ref=self.amp_ref)
            (self.squeeze_provisional if ev["type"] == "provisional"
             else self.squeeze_final).emit(result)


class LevelIndicator(QLabel):
    def __init__(self):
        super().__init__("IDLE")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFont(QFont("Sans", 36, QFont.Weight.Bold))
        self.setMinimumHeight(90)
        self.set_idle()

    def set_idle(self):
        self.setText("IDLE")
        self.setStyleSheet("background-color: #303030; color: white; border-radius: 6px;")

    def set_level(self, level, conf, final):
        color = ec.LEVEL_COLORS[level]
        tag = "" if final else " (live)"
        self.setText(f"Level {level} - {conf * 100:.0f}%{tag}")
        self.setStyleSheet(f"background-color: {color}; color: black; "
                           f"border-radius: 6px; border: {'4px solid black' if final else 'none'};")


class CueGenerator(QObject):
    """Drives a guided squeeze-to-level practice sequence -- GET READY ->
    SQUEEZE -> RELAX, cycling through randomized target levels -- so the
    real-time monitor can be used to *train* the classifier (and the user)
    instead of only observing it. Runs on a QTimer in the GUI thread; it's
    just timing/state, not DSP, so it doesn't need its own thread.

    Emits state_changed on every tick (for the prompt widget's countdown),
    plus cue_active_started/cue_active_ended around the SQUEEZE window so
    the caller can pair a requested level against whatever the detector
    reports during that window.
    """
    state_changed = pyqtSignal(dict)
    cue_active_started = pyqtSignal(int, float)  # level, hold_duration

    def __init__(self, levels=(1, 2, 3, 4, 5), lead_sec=3, rest_sec=8,
                hold_sec=5, tick_ms=100):
        super().__init__()
        self.levels = list(levels)
        self.lead_sec = lead_sec
        self.rest_sec = rest_sec
        self.hold_sec = hold_sec
        self.timer = QTimer()
        self.timer.setInterval(tick_ms)
        self.timer.timeout.connect(self._tick)
        self.running = False
        self.phase = "idle"
        self.level = None
        self._rng = random.Random()
        self._last_level = None

    def start(self):
        self.running = True
        self._last_level = None
        self._enter_phase("get_ready")
        self.timer.start()

    def stop(self):
        self.running = False
        self.timer.stop()
        self.phase = "idle"
        self.state_changed.emit({"phase": "idle", "level": None, "remaining": 0.0})

    def _pick_level(self):
        choices = [lv for lv in self.levels if lv != self._last_level] or self.levels
        self._last_level = self._rng.choice(choices)
        return self._last_level

    def _enter_phase(self, phase):
        self.phase = phase
        self.phase_start = time.monotonic()
        if phase == "get_ready":
            self.level = self._pick_level()
            self.duration = self.lead_sec
        elif phase == "active":
            self.duration = self.hold_sec
            self.cue_active_started.emit(self.level, self.duration)
        elif phase == "rest":
            self.duration = self.rest_sec

    def _tick(self):
        if not self.running:
            return
        elapsed = time.monotonic() - self.phase_start
        remaining = max(0.0, self.duration - elapsed)
        self.state_changed.emit({"phase": self.phase, "level": self.level,
                                 "remaining": remaining, "duration": self.duration})
        if elapsed >= self.duration:
            nxt = {"get_ready": "active", "active": "rest", "rest": "get_ready"}[self.phase]
            self._enter_phase(nxt)


class CuePromptWidget(QWidget):
    """Full-width, high-contrast banner telling the user what to do right
    now. Deliberately loud (huge text, saturated color, live countdown) --
    this is meant to be readable at a glance while you're mid-squeeze, not
    read closely like the rest of the controls panel."""

    PHASE_STYLE = {
        "idle": ("#303030", "white", "Cue practice not running"),
        "get_ready": ("#1EA83C", "white", "GET READY"),
        "active": ("#E8760A", "black", "SQUEEZE NOW"),
        "rest": ("#2A5DB0", "white", "RELAX"),
    }

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(110)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)

        self.title = QLabel("Cue practice not running")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setFont(QFont("Sans", 30, QFont.Weight.Black))

        self.subtitle = QLabel("")
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle.setFont(QFont("Sans", 14))

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(10)

        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)
        layout.addWidget(self.progress)
        self._apply_style("idle")

    def _apply_style(self, phase):
        bg, fg, _ = self.PHASE_STYLE[phase]
        self.setStyleSheet(f"background-color: {bg};")
        self.title.setStyleSheet(f"color: {fg};")
        self.subtitle.setStyleSheet(f"color: {fg};")

    def update_state(self, state):
        phase = state["phase"]
        self._apply_style(phase)
        if phase == "idle":
            self.title.setText("Cue practice not running")
            self.subtitle.setText("Press \"Start Cue Practice\" for a guided squeeze-to-level session")
            self.progress.setValue(0)
            return

        _, _, verb = self.PHASE_STYLE[phase]
        level = state["level"]
        remaining = state["remaining"]
        total = state["duration"]
        if phase == "get_ready":
            self.title.setText(f"{verb}: Level {level}")
            self.subtitle.setText(f"Squeezing starts in {remaining:.1f}s")
        elif phase == "active":
            self.title.setText(f"{verb} -- LEVEL {level}")
            self.subtitle.setText(f"Hold for {remaining:.1f} more second(s)")
        else:  # rest
            self.title.setText(verb)
            self.subtitle.setText(f"Next cue in {remaining:.1f}s")

        frac = 0.0 if total <= 0 else max(0.0, min(1.0, 1.0 - remaining / total))
        self.progress.setValue(int(frac * 1000))


class MainWindow(QMainWindow):
    def __init__(self, profile_path, replay_path=None, data_root=DATA_ROOT):
        super().__init__()
        self.setWindowTitle("Real-Time EMG Squeeze Monitor")
        self.setGeometry(100, 100, 1100, 750)
        self.replay_path = replay_path
        self.data_root = data_root
        self.worker = None  # SerialWorker or ReplayWorker

        self.dsp = DSPWorker(profile_path)
        self.dsp.chunk_ready.connect(self.on_chunk_ready)
        self.dsp.threshold_updated.connect(self.on_threshold_updated)
        self.dsp.amp_ref_updated.connect(self.on_amp_ref_updated)
        self.dsp.squeeze_provisional.connect(self.on_squeeze_provisional)
        self.dsp.squeeze_final.connect(self.on_squeeze_final)
        self.dsp.start()
        self.amp_ref_updates = 0

        self.fs = self.dsp.fs
        self.plot_points = int(PLOT_WINDOW_SEC * self.fs)
        self.filt_buf = deque([0.0] * self.plot_points, maxlen=self.plot_points)
        self.env_buf = deque([0.0] * self.plot_points, maxlen=self.plot_points)
        self.squeeze_log = []  # for CSV export
        self.cue_log = []      # requested-vs-detected pairs, for CSV export
        self.session_dir = None
        self.profile_path = profile_path

        self.cue_gen = CueGenerator()
        self.cue_gen.state_changed.connect(self.on_cue_state_changed)
        self.cue_gen.cue_active_started.connect(self.on_cue_active_started)
        self._pending_cue = None
        self._cue_seq = 0

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)

        self.cue_prompt = CuePromptWidget()
        outer.addWidget(self.cue_prompt)

        body = QWidget()
        outer.addWidget(body, 1)
        layout = QHBoxLayout(body)

        controls = QVBoxLayout()
        controls.setSpacing(8)

        if not self.replay_path:
            self.port_combo = QComboBox()
            self.refresh_ports()
            controls.addWidget(QLabel("Serial Port:"))
            controls.addWidget(self.port_combo)
            refresh_btn = QPushButton("Refresh Ports")
            refresh_btn.clicked.connect(self.refresh_ports)
            controls.addWidget(refresh_btn)
            self.connect_button = QPushButton("Connect")
            self.connect_button.clicked.connect(self.toggle_connection)
            controls.addWidget(self.connect_button)
        else:
            controls.addWidget(QLabel(f"Replay source:\n{os.path.basename(self.replay_path)}"))
            self.connect_button = QPushButton("Start Replay")
            self.connect_button.clicked.connect(self.toggle_connection)
            controls.addWidget(self.connect_button)

        controls.addWidget(QLabel("Profile:"))
        self.profile_field = QLineEdit(os.path.abspath(profile_path))
        self.profile_field.setReadOnly(True)
        controls.addWidget(self.profile_field)

        self.level_indicator = LevelIndicator()
        controls.addWidget(self.level_indicator)

        self.cue_button = QPushButton("Start Cue Practice")
        self.cue_button.clicked.connect(self.toggle_cue_practice)
        self.cue_button.setStyleSheet("background-color: #2A5DB0; color: white; "
                                      "font-weight: bold; padding: 5px;")
        controls.addWidget(self.cue_button)

        self.thr_label = QLabel("thr_hi=-- thr_lo=--")
        controls.addWidget(self.thr_label)

        self.amp_ref_label = QLabel(f"amp_ref={self.dsp.amp_ref:.4f} (calibration, 0 cue updates)")
        self.amp_ref_label.setWordWrap(True)
        controls.addWidget(self.amp_ref_label)

        self.signal_quality_label = QLabel("Signal quality: baseline=-- (initializing...)")
        self.signal_quality_label.setWordWrap(True)
        controls.addWidget(self.signal_quality_label)

        self.session_label = QLabel("Session: (not started)")
        self.session_label.setWordWrap(True)
        controls.addWidget(self.session_label)

        self.save_button = QPushButton("Save Data to CSV")
        self.save_button.clicked.connect(self.export_to_csv)
        self.save_button.setStyleSheet("background-color: #2E8B57; color: white; "
                                       "font-weight: bold; padding: 5px;")
        controls.addWidget(self.save_button)

        controls.addWidget(QLabel("Squeeze log:"))
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["#", "Time (s)", "Dur (s)", "Level", "Conf", "Cue"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        controls.addWidget(self.table, 1)

        controls.addStretch()

        plots = QVBoxLayout()
        self.filt_plot = pg.PlotWidget()
        self.filt_plot.setBackground('w')
        self.filt_plot.setLabel('left', 'Filtered (mV)')
        self.filt_plot.showGrid(x=True, y=True)
        self.filt_curve = self.filt_plot.plot(pen=pg.mkPen(color=(0, 0, 255), width=1.5))

        self.env_plot = pg.PlotWidget()
        self.env_plot.setBackground('w')
        self.env_plot.setLabel('left', 'Envelope (mV)')
        self.env_plot.setLabel('bottom', 'Sample (recent window)')
        self.env_plot.showGrid(x=True, y=True)
        self.env_curve = self.env_plot.plot(pen=pg.mkPen(color=(200, 0, 0), width=2))
        self.thr_hi_line = pg.InfiniteLine(angle=0, pen=pg.mkPen('k', style=Qt.PenStyle.DashLine))
        self.thr_lo_line = pg.InfiniteLine(angle=0, pen=pg.mkPen('k', style=Qt.PenStyle.DotLine))
        self.env_plot.addItem(self.thr_hi_line)
        self.env_plot.addItem(self.thr_lo_line)

        plots.addWidget(self.filt_plot, 2)
        plots.addWidget(self.env_plot, 2)

        layout.addLayout(controls, 1)
        layout.addLayout(plots, 3)

        if self.replay_path:
            self.toggle_connection()  # replay mode: start immediately, no button needed

    def refresh_ports(self):
        import serial.tools.list_ports
        self.port_combo.clear()
        for port in sorted(serial.tools.list_ports.comports()):
            self.port_combo.addItem(port.device)

    def toggle_connection(self):
        if self.worker is None:
            if self.replay_path:
                self.worker = ReplayWorker(self.replay_path, fs=self.fs)
                source = f"replay:{os.path.abspath(self.replay_path)}"
            else:
                port = self.port_combo.currentText()
                if not port:
                    print("No serial port selected.")
                    return
                self.worker = SerialWorker(port)
                source = f"serial:{port}"

            self.start_new_session(source)
            self.worker.finished.connect(self.on_worker_finished)
            self.worker.data_received.connect(self.dsp.enqueue_chunk)
            self.worker.start()
            self.connect_button.setText("Disconnect" if not self.replay_path else "Stop Replay")
        else:
            self.worker.stop()
            self.worker = None
            self.connect_button.setText("Connect" if not self.replay_path else "Start Replay")
            if self.cue_gen.running:
                self.toggle_cue_practice()

    def start_new_session(self, source):
        """Allocate the next sequentially-numbered session folder (see
        emg_core.next_session_dir) and drop a small metadata file in it so
        each recording is self-describing even if Save is never clicked."""
        self.session_dir = ec.next_session_dir(self.data_root)
        self.session_start_monotonic = time.monotonic()
        open(os.path.join(self.session_dir, "notes.txt"), "w").close()
        self.session_info = {
            "started": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "profile": os.path.abspath(self.profile_path),
            "fs": self.dsp.fs,
        }
        with open(os.path.join(self.session_dir, "session_info.json"), "w") as fh:
            json.dump(self.session_info, fh, indent=2)
        self.session_label.setText(f"Session: {os.path.basename(self.session_dir)}\n"
                                   f"Profile: {os.path.basename(self.profile_path)}")
        print(f"New session -> {self.session_dir}  (profile: {self.session_info['profile']})")

    def toggle_cue_practice(self):
        if not self.cue_gen.running:
            if self.worker is None:
                print("Connect (or start replay) before starting cue practice.")
                return
            self.cue_gen.start()
            self.cue_button.setText("Stop Cue Practice")
        else:
            self.cue_gen.stop()
            self._pending_cue = None
            self.cue_button.setText("Start Cue Practice")

    def on_worker_finished(self):
        self.worker = None
        self.connect_button.setText("Connect" if not self.replay_path else "Start Replay")

    def on_cue_state_changed(self, state):
        self.cue_prompt.update_state(state)

    def on_cue_active_started(self, level, duration):
        """A SQUEEZE window just opened for `level`. Record it as pending so
        the next detected squeeze gets paired against it, and schedule a
        grace-period check to log a miss if nothing gets detected in time
        (grace covers the streaming pipeline's own confirm/min_gap latency,
        not just the nominal hold duration)."""
        self._cue_seq += 1
        cue_id = self._cue_seq
        self._pending_cue = {
            "id": cue_id, "level": level, "duration": duration,
            "t": time.monotonic() - self.session_start_monotonic,
        }
        grace_ms = int((duration + 1.0) * 1000)
        QTimer.singleShot(grace_ms, lambda: self._check_cue_miss(cue_id))

    def _check_cue_miss(self, cue_id):
        if self._pending_cue is not None and self._pending_cue["id"] == cue_id:
            pending = self._pending_cue
            self._pending_cue = None
            self.cue_log.append(dict(cue_t=pending["t"], requested=pending["level"],
                                     requested_duration=pending["duration"],
                                     detected=None, conf=None, detected_duration=None,
                                     match=False, raw_rms=None, amp_ref_before=None,
                                     amp_ref_after=None))
            print(f"[{pending['t']:6.1f}s] cue L{pending['level']}: no squeeze detected (missed)")

    def on_chunk_ready(self, filt_chunk, env_chunk):
        self.filt_buf.extend(filt_chunk.tolist())
        self.env_buf.extend(env_chunk.tolist())
        self.filt_curve.setData(np.asarray(self.filt_buf))
        self.env_curve.setData(np.asarray(self.env_buf))

    def on_threshold_updated(self, thr_hi, thr_lo):
        self.thr_hi_line.setPos(thr_hi)
        self.thr_lo_line.setPos(thr_lo)
        self.thr_label.setText(f"thr_hi={thr_hi:.4f}  thr_lo={thr_lo:.4f}")
        # Show signal quality: baseline value and if baseline tracker is rejecting updates
        # (consecutive_rejections rising = today's noise floor keeps landing far from what
        # the tracker currently trusts -- electrode contact change, or sustained activity).
        tracker = self.dsp.tracker
        baseline = tracker.baseline
        stuck = tracker.consecutive_rejections
        if stuck > 2:
            quality = f"⚠ HIGH ACTIVITY (stuck={stuck}) — muscles not relaxing, or electrode issue?"
        elif stuck > 0:
            quality = f"CAUTION: some rejection (stuck={stuck})"
        else:
            quality = "GOOD"
        self.signal_quality_label.setText(f"Signal quality: baseline={baseline:.5f}  {quality}")

    def on_amp_ref_updated(self, amp_ref):
        self.amp_ref_updates += 1
        self.amp_ref_label.setText(
            f"amp_ref={amp_ref:.4f} (live, {self.amp_ref_updates} cue update"
            f"{'s' if self.amp_ref_updates != 1 else ''})")

    def on_squeeze_provisional(self, result):
        self.level_indicator.set_level(result["level"], result["conf"], final=False)

    def on_squeeze_final(self, result):
        self.level_indicator.set_level(result["level"], result["conf"], final=True)
        self.squeeze_log.append(result)
        print(f"[{result['t']:6.1f}s] squeeze #{len(self.squeeze_log)}: "
              f"L{result['level']} conf={result['conf']:.2f} dur={result['duration']:.2f}s")

        cue_text, cue_color = "--", None
        if self._pending_cue is not None:
            pending = self._pending_cue
            self._pending_cue = None
            match = result["level"] == pending["level"]
            amp_ref_before = result["amp_ref"]
            # Ground truth is the requested level, not the (possibly wrong)
            # detection -- rescale amp_ref for every future classification.
            self.dsp.report_cue_ground_truth(pending["level"], result["raw_rms"])
            self.cue_log.append(dict(cue_t=pending["t"], requested=pending["level"],
                                     requested_duration=pending["duration"],
                                     detected=result["level"], conf=result["conf"],
                                     detected_duration=result["duration"], match=match,
                                     raw_rms=result["raw_rms"], amp_ref_before=amp_ref_before,
                                     amp_ref_after=self.dsp.amp_ref))
            cue_text = f"L{pending['level']} OK" if match else f"L{pending['level']} != L{result['level']}"
            cue_color = "#1EA83C" if match else "#D02020"
            print(f"           cue: requested L{pending['level']}, "
                  f"detected L{result['level']} ({'match' if match else 'mismatch'}), "
                  f"amp_ref {amp_ref_before:.4f} -> {self.dsp.amp_ref:.4f}")

        row = self.table.rowCount()
        self.table.insertRow(row)
        vals = [str(row + 1), f"{result['t']:.1f}", f"{result['duration']:.2f}",
                f"L{result['level']}", f"{result['conf']:.2f}", cue_text]
        for col, v in enumerate(vals):
            item = QTableWidgetItem(v)
            if col == 5 and cue_color:
                item.setBackground(pg.mkColor(cue_color))
                item.setForeground(pg.mkColor("white"))
            else:
                item.setBackground(pg.mkColor(ec.LEVEL_COLORS[result["level"]]))
            self.table.setItem(row, col, item)
        self.table.scrollToBottom()

    def export_to_csv(self):
        raw_arr = self.dsp.raw_hist.array()
        filt_arr = self.dsp.filt_hist.array()
        if len(raw_arr) == 0:
            print("No data collected yet. Cannot save empty files.")
            return
        if self.session_dir is None:
            self.start_new_session(source="unknown (Save clicked before any connect)")

        raw_path = os.path.join(self.session_dir, "raw.csv")
        filt_path = os.path.join(self.session_dir, "filtered.csv")
        log_path = os.path.join(self.session_dir, "squeeze_log.csv")
        cue_path = os.path.join(self.session_dir, "cue_log.csv")
        thr_path = os.path.join(self.session_dir, "threshold_log.csv")

        with open(raw_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Voltage_mV", "Count"])
            for count, value in enumerate(raw_arr):
                w.writerow([f"{value:.4f}", count])

        # filt_arr trails raw_arr by ~confirm_sec worth of samples (not yet
        # settled) -- the real-time filtered stream, no final re-filter pass.
        with open(filt_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Voltage_mV", "Count"])
            for count, value in enumerate(filt_arr):
                w.writerow([f"{value:.4f}", count])

        with open(log_path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["idx", "t", "duration", "start", "end",
                                               "level", "conf"])
            w.writeheader()
            for i, r in enumerate(self.squeeze_log, 1):
                w.writerow({"idx": i, "t": f"{r['t']:.3f}", "duration": f"{r['duration']:.3f}",
                           "start": r["start"], "end": r["end"], "level": r["level"],
                           "conf": f"{r['conf']:.3f}"})

        # One row per RollingThresholdTracker re-estimate *attempt*, whether
        # accepted or rejected by the max_jump_factor clamp -- lets you see
        # directly whether the tracker got stuck refusing a real drift,
        # rather than just suspecting it from a high miss rate.
        history = self.dsp.tracker.history
        if history:
            with open(thr_path, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=["idx", "t_sec", "accepted", "forced",
                                                   "baseline_candidate",
                                                   "sigma_candidate", "jump_ratio", "baseline",
                                                   "sigma", "thr_hi", "thr_lo"])
                w.writeheader()
                for i, r in enumerate(history, 1):
                    w.writerow({
                        "idx": i, "t_sec": f"{r['t_sec']:.3f}", "accepted": r["accepted"],
                        "forced": r["forced"],
                        "baseline_candidate": f"{r['baseline_candidate']:.5f}",
                        "sigma_candidate": f"{r['sigma_candidate']:.5f}",
                        "jump_ratio": f"{r['jump_ratio']:.3f}" if r["jump_ratio"] is not None else "",
                        "baseline": f"{r['baseline']:.5f}", "sigma": f"{r['sigma']:.5f}",
                        "thr_hi": f"{r['thr_hi']:.5f}", "thr_lo": f"{r['thr_lo']:.5f}",
                    })

        def _fmt(v, spec=".3f"):
            return "" if v is None else format(v, spec)

        if self.cue_log:
            with open(cue_path, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=["idx", "cue_t", "requested",
                                                   "requested_duration", "detected", "conf",
                                                   "detected_duration", "match", "raw_rms",
                                                   "amp_ref_before", "amp_ref_after"])
                w.writeheader()
                for i, r in enumerate(self.cue_log, 1):
                    w.writerow({
                        "idx": i, "cue_t": _fmt(r["cue_t"]), "requested": r["requested"],
                        "requested_duration": _fmt(r["requested_duration"]),
                        "detected": r["detected"] if r["detected"] is not None else "",
                        "conf": _fmt(r["conf"]),
                        "detected_duration": _fmt(r["detected_duration"]),
                        "match": r["match"], "raw_rms": _fmt(r["raw_rms"], ".5f"),
                        "amp_ref_before": _fmt(r["amp_ref_before"], ".5f"),
                        "amp_ref_after": _fmt(r["amp_ref_after"], ".5f"),
                    })

        self.session_info["ended"] = datetime.now().isoformat(timespec="seconds")
        self.session_info["n_raw_samples"] = int(len(raw_arr))
        self.session_info["n_filtered_samples"] = int(len(filt_arr))
        self.session_info["n_squeezes"] = len(self.squeeze_log)
        self.session_info["n_cues"] = len(self.cue_log)
        self.session_info["amp_ref_calibration"] = self.dsp.amp_ref_tracker.calib_amp_ref
        self.session_info["amp_ref_final"] = self.dsp.amp_ref
        self.session_info["amp_ref_cue_updates"] = self.amp_ref_updates
        if self.cue_log:
            n_matched = sum(1 for r in self.cue_log if r["match"])
            self.session_info["cue_accuracy"] = n_matched / len(self.cue_log)
        if history:
            n_rejected = sum(1 for r in history if not r["accepted"])
            n_forced = sum(1 for r in history if r["forced"])
            self.session_info["n_threshold_estimates"] = len(history)
            self.session_info["n_threshold_rejections"] = n_rejected
            self.session_info["n_threshold_forced_accepts"] = n_forced
        with open(os.path.join(self.session_dir, "session_info.json"), "w") as fh:
            json.dump(self.session_info, fh, indent=2)

        print(f"Saved {len(raw_arr)} raw / {len(filt_arr)} filtered samples, "
              f"{len(self.squeeze_log)} squeezes, {len(self.cue_log)} cues, "
              f"{len(history)} threshold estimates ->")
        print(f"  {self.session_dir}/")

    def closeEvent(self, event):
        self.cue_gen.stop()
        if self.worker:
            self.worker.stop()  # no more new chunks; let DSPWorker drain what's queued
        deadline = time.monotonic() + 2.0
        while not self.dsp._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.05)
        print("Closing -- auto-saving session data...")
        self.export_to_csv()
        self.dsp.stop()
        event.accept()


def build_parser():
    p = argparse.ArgumentParser(description="Real-time EMG squeeze-level monitor.")
    p.add_argument("--profile", default=DEFAULT_PROFILE,
                   help="Calibration profile JSON (default: Filtering_Software/my_arm_profile.json)")
    p.add_argument("--replay", default=None,
                   help="Replay a saved raw CSV instead of connecting to a serial board")
    p.add_argument("--data-root", default=DATA_ROOT,
                   help="Where session folders are created (default: EMG_Linux/data). "
                        "Point this elsewhere for test/demo runs so real sessions are never "
                        "at risk from cleanup of test output.")
    return p


if __name__ == '__main__':
    os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    args = build_parser().parse_args()
    if not os.path.exists(args.profile):
        print(f"Profile not found: {args.profile}")
        sys.exit(1)
    app = QApplication(sys.argv)
    win = MainWindow(args.profile, replay_path=args.replay, data_root=args.data_root)
    win.show()
    sys.exit(app.exec())
