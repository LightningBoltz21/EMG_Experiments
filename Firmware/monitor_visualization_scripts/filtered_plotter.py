import sys
import os
import csv
from datetime import datetime
import serial
import serial.tools.list_ports
from collections import deque
import numpy as np
from scipy.signal import butter, sosfiltfilt

# --- CRITICAL LINUX/PYQTGRAPH FIXES ---
os.environ["PYQTGRAPH_QT_LIB"] = "PyQt6"
os.environ["QT_QPA_PLATFORM"] = "xcb" 

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QComboBox, QPushButton, 
                             QDoubleSpinBox, QCheckBox, QMessageBox)
from PyQt6.QtCore import QThread, pyqtSignal, Qt
import pyqtgraph as pg


# --- Constants ---
MAX_DATA_POINTS = 500  
SAMPLING_RATE_HZ = 500.0  # MATCHES THE ADS1298 CONFIG1 REGISTER
CHUNK_SIZE = 25  # Lowered chunk size to keep latency low at 500Hz

class SerialWorker(QThread):
    data_received = pyqtSignal(list)
    
    def __init__(self, port, baudrate=115200):
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self.serial_port = None
        self.running = False

    def run(self):
        self.running = True
        try:
            self.serial_port = serial.Serial(self.port, self.baudrate, timeout=1)
            print(f"Connected to {self.port} at {self.baudrate} baud.")
        except serial.SerialException as e:
            print(f"Error opening serial port: {e}")
            self.running = False
            return

        chunk = [] 

        while self.running:
            try:
                if self.serial_port.in_waiting > 0:
                    line = self.serial_port.readline().decode('utf-8').strip()
                    if line.startswith(">CH1:"):
                        try:
                            value = float(line.split(':')[1])
                            chunk.append(value)
                            
                            if len(chunk) >= CHUNK_SIZE:
                                self.data_received.emit(chunk)
                                chunk = [] 
                                
                        except (ValueError, IndexError):
                            pass
            except serial.SerialException:
                print("Serial port disconnected.")
                break
        
        if self.serial_port and self.serial_port.is_open:
            self.serial_port.close()
        print("Serial worker stopped.")

    def stop(self):
        self.running = False
        self.wait()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Real-Time EMG Sensor Plotter (Zero-Phase Filtered)")
        self.setGeometry(100, 100, 1000, 600)

        # --- Filter Setup ---
        lowcut = 20.0
        # Highcut MUST be strictly less than half the sampling rate (Nyquist)
        highcut = 200.0  
        self.sos = butter(4, [lowcut, highcut], btype='bandpass', fs=SAMPLING_RATE_HZ, output='sos')
        self.filter_enabled = True

        # --- Main Layout ---
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # --- Controls Layout (Left Side) ---
        controls_layout = QVBoxLayout()
        controls_layout.setSpacing(10)

        self.port_combo = QComboBox()
        self.refresh_ports()
        controls_layout.addWidget(QLabel("Serial Port:"))
        controls_layout.addWidget(self.port_combo)

        self.refresh_button = QPushButton("Refresh Ports")
        self.refresh_button.clicked.connect(self.refresh_ports)
        controls_layout.addWidget(self.refresh_button)

        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.toggle_connection)
        controls_layout.addWidget(self.connect_button)

        self.filter_checkbox = QCheckBox(f"Enable {int(lowcut)}-{int(highcut)}Hz Zero-Phase Filter")
        self.filter_checkbox.setChecked(self.filter_enabled)
        self.filter_checkbox.stateChanged.connect(self.toggle_filter)
        controls_layout.addWidget(self.filter_checkbox)

        controls_layout.addWidget(QLabel("Scale (Gain):"))
        self.scale_spinbox = QDoubleSpinBox()
        self.scale_spinbox.setRange(0.1, 100.0)
        self.scale_spinbox.setValue(1.0)
        self.scale_spinbox.setSingleStep(0.1)
        self.scale_spinbox.valueChanged.connect(self.update_params)
        controls_layout.addWidget(self.scale_spinbox)

        controls_layout.addWidget(QLabel("Offset (mV):"))
        self.offset_spinbox = QDoubleSpinBox()
        self.offset_spinbox.setRange(-5000.0, 5000.0)
        self.offset_spinbox.setValue(0.0)
        self.offset_spinbox.setSingleStep(1.0)
        self.offset_spinbox.valueChanged.connect(self.update_params)
        controls_layout.addWidget(self.offset_spinbox)

        # --- CSV Save Button ---
        self.save_button = QPushButton("Save Data to CSV")
        self.save_button.clicked.connect(self.export_to_csv)
        self.save_button.setStyleSheet("background-color: #2E8B57; color: white; font-weight: bold; padding: 5px;")
        controls_layout.addWidget(self.save_button)

        controls_layout.addStretch()

        # --- Plot Layout (Right Side) ---
        self.graph_widget = pg.PlotWidget()
        self.graph_widget.setBackground('w')
        self.graph_widget.setLabel('left', 'Voltage (mV)')
        self.graph_widget.setLabel('bottom', 'Sample')
        self.graph_widget.showGrid(x=True, y=True)
        self.graph_widget.enableAutoRange(axis='y', enable=False)
        self.graph_widget.setYRange(-20, 20)
        
        pen = pg.mkPen(color=(0, 0, 255), width=2)
        self.plot_data_item = self.graph_widget.plot(pen=pen)

        # --- COMBINE LAYOUTS ---
        main_layout.addLayout(controls_layout, 1)
        main_layout.addWidget(self.graph_widget, 4)

        # --- Class Members ---
        self.serial_worker = None
        
        # Ring buffer for the live visual graph (keeps only the latest 500 points)
        self.data_buffer = deque([0.0] * MAX_DATA_POINTS, maxlen=MAX_DATA_POINTS)
        
        # Master list to store ALL incoming data since the port was connected
        self.full_recording = []
        
        self.scale = 1.0
        self.offset = 0.0

    def toggle_filter(self):
        self.filter_enabled = self.filter_checkbox.isChecked()
        self._redraw_plot()

    def refresh_ports(self):
        self.port_combo.clear()
        ports = serial.tools.list_ports.comports()
        for port in sorted(ports):
            self.port_combo.addItem(port.device)

    def toggle_connection(self):
        if self.serial_worker is None:
            port = self.port_combo.currentText()
            if not port:
                print("No serial port selected.")
                return
            
            # Reset the master recording list upon a fresh connection
            self.full_recording = []
            
            self.serial_worker = SerialWorker(port)
            self.serial_worker.data_received.connect(self.update_plot)
            self.serial_worker.finished.connect(self.on_worker_finished)
            self.serial_worker.start()
            self.connect_button.setText("Disconnect")
        else:
            self.serial_worker.stop()
            self.serial_worker = None
            self.connect_button.setText("Connect")

    def on_worker_finished(self):
        self.serial_worker = None
        self.connect_button.setText("Connect")

    def update_params(self):
        self.scale = self.scale_spinbox.value()
        self.offset = self.offset_spinbox.value()
        self._redraw_plot()

    def update_plot(self, chunk):
        processed_chunk = np.array(chunk)
        processed_chunk = (processed_chunk * self.scale) + self.offset
        
        # 1. Update the visual screen buffer (pushes out old data)
        self.data_buffer.extend(processed_chunk.tolist())
        
        # 2. Append to the master recording list (keeps everything)
        self.full_recording.extend(processed_chunk.tolist())
        
        self._redraw_plot()
        
    def _redraw_plot(self):
        """Applies the zero-phase filter to the visual window."""
        data_to_plot = np.array(self.data_buffer)
        
        if self.filter_enabled:
            data_to_plot = sosfiltfilt(self.sos, data_to_plot)
            
        self.plot_data_item.setData(data_to_plot)

    def export_to_csv(self):
        """Generates two CSV files (Raw and Filtered) from the master recording."""
        if not self.full_recording:
            print("No data collected yet. Cannot save empty files.")
            return

        # Generate timestamps for unique filenames
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        raw_filename = f"emg_raw_{timestamp}.csv"
        filtered_filename = f"emg_filtered_{timestamp}.csv"

        try:
            # 1. Write the RAW data CSV
            with open(raw_filename, mode='w', newline='') as raw_file:
                writer = csv.writer(raw_file)
                # Write header
                writer.writerow(["Voltage_mV", "Count"])
                for count, value in enumerate(self.full_recording):
                    # Format as: value, count (e.g., 45.1234, 0)
                    writer.writerow([f"{value:.4f}", count])

            # 2. Apply the mathematical zero-phase filter to the ENTIRE recording array
            full_data_np = np.array(self.full_recording)
            filtered_data_np = sosfiltfilt(self.sos, full_data_np)

            # 3. Write the FILTERED data CSV
            with open(filtered_filename, mode='w', newline='') as filt_file:
                writer = csv.writer(filt_file)
                writer.writerow(["Voltage_mV", "Count"])
                for count, value in enumerate(filtered_data_np):
                    writer.writerow([f"{value:.4f}", count])

            print(f"Success: Exported {len(self.full_recording)} samples to CSV.")
            print(f" -> {raw_filename}")
            print(f" -> {filtered_filename}")

        except Exception as e:
            print(f"Failed to export CSV files: {e}")

    def closeEvent(self, event):
        if self.serial_worker:
            self.serial_worker.stop()
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    main_window = MainWindow()
    main_window.show()
    sys.exit(app.exec())