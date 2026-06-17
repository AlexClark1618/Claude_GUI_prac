import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import subprocess
import threading
import sys
import os
import re
import socket
import struct
import time
from datetime import datetime
from collections import defaultdict, deque

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

SERVER_SCRIPT = r"C:\\Users\\aclark2\\Desktop\\Claude_GUI_prac\\GPS_DAQ_Server.py"
SERVER_CWD = os.path.dirname(SERVER_SCRIPT)
GPS_DATA_DIR = os.path.join(SERVER_CWD, "GPS Data")
SURVEY_RESULTS_DIR = os.path.join(SERVER_CWD, "Survey Results")

# Shell used by the OTA interactive terminal.  A persistent process is spawned
# so working directory / environment changes carry over between commands.
#!!!Change to terminal exe for linux systems!!!
OTA_SHELL = ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", "-"]

PACKET_FORMAT = "!iiiiiiiiii"
PACKET_SIZE = struct.calcsize(PACKET_FORMAT)
GUI_ID = 1

DEVICES = ["BH", "Veto", "Det 1", "Det 2", "Det 3", "Det 4",
           "Det 5", "Det 6", "Det 7", "Det 8", "Det 9", "Det 10"]

# Map device names to ESP mac_id (last byte of MAC address).
# Update these values to match your actual hardware.
DEVICE_IDS = {
    "BH":    164,
    "Veto":  16,
    "Det 1": 116,   # placeholder – fill in actual mac_id
    "Det 2": 0,
    "Det 3": 0,
    "Det 4": 0,
    "Det 5": 0,
    "Det 6": 0,
    "Det 7": 0,
    "Det 8": 0,
    "Det 9": 0,
    "Det 10": 0,
}

# CFG-RST navBbrMask presets (reset "temperature")
RESET_TYPES = {"Hot": 0x0000, "Warm": 0x0001, "Cold": 0xFFFF}

# CFG-RATE-TIMEREF enum.  "(no change)" -> -1 means leave the field untouched.
TIMEREF_OPTIONS  = {"(no change)": -1, "UTC": 0, "GPS": 1, "GLONASS": 2, "BeiDou": 3, "Galileo": 4}
TIMEREF_BY_VALUE = {0: "UTC", 1: "GPS", 2: "GLONASS", 3: "BeiDou", 4: "Galileo"}

# TP1 (PPS) flag bits — must match gps_ubx.py TP1_F_* on the ESP side.
TP1_F_ENABLE   = 1 << 0   # output enabled
TP1_F_ISFREQ   = 1 << 1   # value is frequency (Hz); else period (µs)
TP1_F_POL      = 1 << 2   # polarity: rising edge at top-of-second
TP1_F_ALIGNTOW = 1 << 3   # align to time-of-week
TP1_F_LOCKGPS  = 1 << 4   # sync / use GNSS-locked params
TP1_F_USELOCK  = 1 << 5   # use *_LOCK values when locked
TP1_F_ISLENGTH = 1 << 6   # pulse width is length (µs); else duty cycle (%)
TP1_DUTY_SCALE = 1000     # duty % carried as round(percent * scale)

# gps_cpu_log.txt columns:
# 0 ts 1 ID 2 cpuLoad 3 cpuLoadMax 4 memUsage 5 memUsageMax
# 6 ioUsage 7 ioUsageMax 8 runTime 9 temp 10 notice 11 warn 12 error
PLOT_METRICS = {"CPU load (%)": 2, "Mem usage (%)": 4, "IO usage (%)": 6, "Temp (°C)": 9}

# stats_log.txt — data slow control.  Two on-disk layouts are handled:
#   future:  ts; inst; ID; d0; d1; d2; d3; d4; d5; d6; d7   (11 fields, datetime first)
#   current:     inst; ID; d0; d1; d2; d3; d4; d5; d6; d7   (10 fields, no datetime)
# Cycle delimiter / header lines ("Start Cycle N:...") are skipped by the parser.
#   inst 98 (rate in):   d0=NEvents0   d1=NEvents1   d2=deltaT(ms)
#   inst 96 (integrity): d0=rx_count   d1=data_count d2=null_count  d3=unreas_count
# The file grows without bound, so the GUI reads it incrementally (byte offset)
# and keeps only the most recent STATS_MAX_POINTS samples per detector in memory.
STATS_MAX_POINTS = 3000
ID_TO_NAME = {v: k for k, v in DEVICE_IDS.items() if v}


class GPSDAQApp:
    def __init__(self, root):
        self.root = root
        self.root.title("KoForce GUI")
        self.root.geometry("820x720")
        self.root.resizable(True, True)

        self.server_process = None
        self.ota_process = None
        self.autosurvey_stop = threading.Event()
        self.autosurvey_thread = None

        # Data slow control: incremental-read state (see _update_datactrl)
        self._stats_path = None
        self._reset_stats_state()

        self._build_ui()

    # ================================================================== #
    #  Top-level layout
    # ================================================================== #

    def _build_ui(self):
        # Shared status bar at the very bottom
        self.status_var = tk.StringVar(value="Ready")
        self.status_bar = tk.Label(
            self.root,
            textvariable=self.status_var,
            relief="sunken",
            anchor="w",
            padx=10,
            bg="#555555",
            fg="white",
            font=("Arial", 9, "bold"),
        )
        self.status_bar.pack(side="bottom", fill="x")

        # Notebook
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=6)

        server_tab = tk.Frame(self.notebook)
        gps_tab    = tk.Frame(self.notebook)
        cpu_tab    = tk.Frame(self.notebook)
        data_tab   = tk.Frame(self.notebook)
        auto_tab   = tk.Frame(self.notebook)
        ota_tab    = tk.Frame(self.notebook)
        pps_tab = tk.Frame(self.notebook)

        self.notebook.add(server_tab, text="  Server Control  ")
        self.notebook.add(gps_tab,    text="  GPS Comms ")
        self.notebook.add(cpu_tab,    text="  GPS Slow Control  ")
        self.notebook.add(data_tab,   text="  Data Slow Control  ")
        self.notebook.add(auto_tab,   text="  Auto Survey  ")
        self.notebook.add(pps_tab,    text="  PPS Controls  ")
        self.notebook.add(ota_tab,    text="  OTA  ")

        self._build_server_tab(server_tab)
        self._build_gps_tab(gps_tab)
        self._build_slowctrl_tab(cpu_tab)
        self._build_datactrl_tab(data_tab)
        self._build_autosurvey_tab(auto_tab)
        self._build_ppsctrl_tab(pps_tab)
        self._build_ota_tab(ota_tab)

    # ================================================================== #
    #  Tab 1 – Server Control
    # ================================================================== #

    def _build_server_tab(self, parent):
        ctrl = tk.LabelFrame(parent, text="Server Controls", padx=10, pady=8)
        ctrl.pack(fill="x", padx=10, pady=10)

        self.start_btn = tk.Button(
            ctrl, text="Start Server", command=self.start_server,
            bg="#2ecc71", fg="white", activebackground="#27ae60",
            width=15, font=("Arial", 10),
        )
        self.start_btn.grid(row=0, column=0, padx=5)

        self.stop_btn = tk.Button(
            ctrl, text="Stop Server", command=self.stop_server,
            bg="#e74c3c", fg="white", activebackground="#c0392b",
            width=15, font=("Arial", 10), state="disabled",
        )
        self.stop_btn.grid(row=0, column=1, padx=5)

        tk.Button(
            ctrl, text="Clear Log", command=self.clear_log,
            width=12, font=("Arial", 10),
        ).grid(row=0, column=2, padx=20)

        # Run configuration (applied when the server is started)
        cfg = tk.Frame(ctrl)
        cfg.grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
        tk.Label(cfg, text="File time (hrs/cycle):").grid(row=0, column=0, sticky="w")
        self.file_hours_entry = tk.Entry(cfg, width=7)
        self.file_hours_entry.insert(0, "1")
        self.file_hours_entry.grid(row=0, column=1, padx=(4, 16), sticky="w")
        tk.Label(cfg, text="Max cycles (blank = no limit):").grid(row=0, column=2, sticky="w")
        self.max_cycles_entry = tk.Entry(cfg, width=7)
        self.max_cycles_entry.grid(row=0, column=3, padx=(4, 0), sticky="w")

        tk.Label(ctrl, text=f"Script: {SERVER_SCRIPT}",
                 font=("Arial", 8), fg="gray").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(5, 0))

        log_frame = tk.LabelFrame(parent, text="Server Output", padx=5, pady=5)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.log_text = scrolledtext.ScrolledText(
            log_frame, state="disabled",
            font=("Courier", 9), bg="#1e1e1e", fg="#d4d4d4",
        )
        self.log_text.pack(fill="both", expand=True)

    # ================================================================== #
    #  Tab 2 – GPS Communication
    # ================================================================== #

    def _build_gps_tab(self, parent):

        # ---- Connection row ------------------------------------------ #
        conn = tk.LabelFrame(parent, text="Connection", padx=10, pady=6)
        conn.pack(fill="x", padx=10, pady=(10, 4))

        tk.Label(conn, text="Host:").grid(row=0, column=0, sticky="w")
        self.host_entry = tk.Entry(conn, width=18)
        self.host_entry.insert(0, "127.0.0.1")
        self.host_entry.grid(row=0, column=1, padx=(4, 12), sticky="w")

        tk.Label(conn, text="Ctrl Port:").grid(row=0, column=2, sticky="w")
        self.port_entry = tk.Entry(conn, width=7)
        self.port_entry.insert(0, "12347")   # dedicated control channel (not the data port 12345)
        self.port_entry.grid(row=0, column=3, padx=4, sticky="w")

        # ---- GPS Controls (device select + restart + version) -------- #
        dev = tk.LabelFrame(parent, text="GPS Controls", padx=10, pady=6)
        dev.pack(fill="x", padx=10, pady=4)

        tk.Label(dev, text="Select Device:").grid(row=0, column=0, sticky="w")
        self.device_var = tk.StringVar(value=DEVICES[0])
        ttk.Combobox(dev, textvariable=self.device_var,
                     values=DEVICES, state="readonly", width=10
                     ).grid(row=0, column=1, padx=8, sticky="w")

        # Restart receiver
        tk.Label(dev, text="Restart:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.reset_type_var = tk.StringVar(value="Warm")
        ttk.Combobox(dev, textvariable=self.reset_type_var,
                     values=["Hot", "Warm", "Cold"], state="readonly", width=7
                     ).grid(row=1, column=1, padx=8, sticky="w", pady=(8, 0))
        tk.Button(dev, text="Restart GPS", width=12,
                  command=self.restart_gps).grid(row=1, column=2, sticky="w", pady=(8, 0))

        # Comms test — version
        tk.Button(dev, text="GPS Version", width=12,
                  command=self.get_version).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.version_var = tk.StringVar(value="—")
        tk.Label(dev, textvariable=self.version_var, font=("Arial", 8), fg="#555"
                 ).grid(row=2, column=1, columnspan=3, sticky="w", pady=(8, 0))

        # ---- Survey -------------------------------------------------- #
        surv = tk.LabelFrame(parent, text="Survey-In  (TIM-SVIN)", padx=10, pady=8)
        surv.pack(fill="x", padx=10, pady=4)

        # Parameters row
        tk.Label(surv, text="Min dur (s):").grid(row=0, column=0, sticky="w")
        self.svin_dur_entry = tk.Entry(surv, width=7)
        self.svin_dur_entry.insert(0, "60")
        self.svin_dur_entry.grid(row=0, column=1, padx=(2, 14), sticky="w")

        tk.Label(surv, text="Acc limit (0.1 mm):").grid(row=0, column=2, sticky="w")
        self.svin_acc_entry = tk.Entry(surv, width=8)
        self.svin_acc_entry.insert(0, "3000")   # 300 mm default
        self.svin_acc_entry.grid(row=0, column=3, padx=(2, 14), sticky="w")

        tk.Button(surv, text="Start Survey-In", width=16,
                  command=self.start_survey).grid(row=0, column=4, padx=(0, 8), sticky="w")
        tk.Button(surv, text="Get Status", width=12,
                  command=self.get_survey_results).grid(row=0, column=5, sticky="w")

        # Status / result fields (read-only)
        result_labels = ["ECEF X (cm)", "ECEF Y (cm)", "ECEF Z (cm)", "Acc (0.1mm)"]
        self.survey_entries = {}
        for col, name in enumerate(result_labels):
            tk.Label(surv, text=name, font=("Arial", 8)).grid(row=1, column=col, sticky="s", pady=(6,0))
            e = tk.Entry(surv, width=14, bg="#f0f0f0")
            e.grid(row=2, column=col, padx=4, pady=2)
            e.config(state="readonly")
            self.survey_entries[name] = e

        # Extra readouts: observation time (TIM-SVIN dur) and 3D std (meanAcc, in mm)
        for col, name in ((4, "Obs time (s)"), (5, "3D std (mm)")):
            tk.Label(surv, text=name, font=("Arial", 8)).grid(row=1, column=col, sticky="s", pady=(6, 0))
        self.svin_obstime_entry = tk.Entry(surv, width=12, bg="#f0f0f0")
        self.svin_obstime_entry.grid(row=2, column=4, padx=4, pady=2)
        self.svin_obstime_entry.config(state="readonly")
        self.svin_std3d_entry = tk.Entry(surv, width=12, bg="#f0f0f0")
        self.svin_std3d_entry.grid(row=2, column=5, padx=4, pady=2)
        self.svin_std3d_entry.config(state="readonly")

        # Small status indicators: valid / active / duration
        self.svin_status_var = tk.StringVar(value="—")
        tk.Label(surv, textvariable=self.svin_status_var,
                 font=("Arial", 8), fg="#555").grid(
            row=3, column=0, columnspan=6, sticky="w", pady=(2, 0))

        # ---- Fixed Position ------------------------------------------ #
        fix = tk.LabelFrame(parent, text="Fixed Position  (CFG-TMODE)", padx=10, pady=8)
        fix.pack(fill="x", padx=10, pady=4)

        # Action buttons
        tk.Button(fix, text="Send Fixed Coords", width=16,
                  command=self.send_fix_coordinates).grid(
            row=0, column=0, padx=(0, 6), pady=(0, 8), sticky="w")
        tk.Button(fix, text="Probe Fixed Coords", width=16,
                  command=self.probe_fixed_coordinates).grid(
            row=0, column=1, padx=6, pady=(0, 8), sticky="w")
        tk.Button(fix, text="← Use Survey Result", width=18,
                  command=self.copy_survey_to_fix).grid(
            row=0, column=2, columnspan=2, padx=6, pady=(0, 8), sticky="w")

        # Editable input row (values to send)
        tk.Label(fix, text="Set position  (X / Y / Z in cm, Acc in 0.1 mm):",
                 font=("Arial", 8)).grid(row=1, column=0, columnspan=4, sticky="w")
        self.fix_entries = self._make_coord_row(fix, row=2)

        # Read-only row (what the receiver currently reports)
        tk.Label(fix, text="Receiver reports:", font=("Arial", 8)).grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        read_labels = ["X (cm)", "Y (cm)", "Z (cm)", "Acc (0.1mm)"]
        self.fix_read_entries = {}
        for col, name in enumerate(read_labels):
            tk.Label(fix, text=name, font=("Arial", 8)).grid(row=5, column=col, sticky="s")
            e = tk.Entry(fix, width=14, bg="#f0f0f0")
            e.grid(row=6, column=col, padx=4, pady=2)
            e.config(state="readonly")
            self.fix_read_entries[name] = e

        self.fix_mode_var = tk.StringVar(value="mode: —")
        tk.Label(fix, textvariable=self.fix_mode_var,
                 font=("Arial", 8), fg="#555").grid(
            row=7, column=0, columnspan=4, sticky="w", pady=(2, 0))

        # ---- Measurement rate (CFG-RATE) ----------------------------- #
        rcv = tk.LabelFrame(parent, text="Measurement Rate  (CFG-RATE)", padx=10, pady=8)
        rcv.pack(fill="x", padx=10, pady=4)

        tk.Label(rcv, text="Meas (ms):").grid(row=0, column=0, sticky="w")
        self.rate_meas_entry = tk.Entry(rcv, width=8)
        self.rate_meas_entry.grid(row=0, column=1, padx=(2, 10), sticky="w")
        tk.Label(rcv, text="Nav (cyc):").grid(row=0, column=2, sticky="w")
        self.rate_nav_entry = tk.Entry(rcv, width=8)
        self.rate_nav_entry.grid(row=0, column=3, padx=(2, 10), sticky="w")

        tk.Label(rcv, text="TimeRef:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.timeref_var = tk.StringVar(value="(no change)")
        ttk.Combobox(rcv, textvariable=self.timeref_var,
                     values=list(TIMEREF_OPTIONS.keys()), state="readonly", width=11
                     ).grid(row=1, column=1, padx=(2, 10), sticky="w", pady=(6, 0))
        tk.Button(rcv, text="Read Rate", width=10,
                  command=self.read_rate).grid(row=1, column=2, sticky="w", pady=(6, 0))
        tk.Button(rcv, text="Set Rate", width=10,
                  command=self.set_rate).grid(row=1, column=3, sticky="w", pady=(6, 0))

    def _make_coord_row(self, parent, row, bg=None):
        """Helper: place x / y / z / Acc label+entry pairs and return the entry dict."""
        fields = ["x", "y", "z", "Acc"]
        entries = {}
        for col, name in enumerate(fields):
            tk.Label(parent, text=f"{name}:").grid(row=row, column=col, sticky="s")
            kw = {"width": 14}
            if bg:
                kw["bg"] = bg
            e = tk.Entry(parent, **kw)
            e.grid(row=row + 1, column=col, padx=4, pady=2, sticky="w")
            entries[name] = e
        return entries

    # ================================================================== #
    #  Server methods
    # ================================================================== #

    def start_server(self):
        if self.server_process is not None:
            return

        # Build run config from the entries (validated; sensible fallbacks)
        try:
            file_hours = float(self.file_hours_entry.get().strip() or "1")
            if file_hours <= 0:
                raise ValueError
        except ValueError:
            self._set_status("Bad 'File time' — enter a positive number of hours", "#e74c3c")
            return
        mc = self.max_cycles_entry.get().strip()
        try:
            max_cycles = int(mc) if mc else 0
            if max_cycles < 0:
                raise ValueError
        except ValueError:
            self._set_status("Bad 'Max cycles' — enter a whole number (or leave blank)", "#e74c3c")
            return

        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", SERVER_SCRIPT,
                 "--file-hours", str(file_hours), "--max-cycles", str(max_cycles)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                cwd=SERVER_CWD,
            )
        except Exception as e:
            self._append_log(f"[GUI ERROR] Failed to start server: {e}\n")
            return

        self.server_process = proc
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._set_status(f"Server running  (PID {proc.pid})", "#27ae60")
        self._append_log(f"[GUI] Server started  (PID {proc.pid})\n")
        threading.Thread(target=self._stream_logs, args=(proc,), daemon=True).start()

    def stop_server(self):
        proc = self.server_process
        if proc is None:
            return
        self.server_process = None
        try:
            proc.terminate()
        except Exception:
            pass
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self._set_status("Server: Stopped", "#555555")
        self._append_log("[GUI] Server stopped by user\n")

    def clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state="disabled")

    def _stream_logs(self, proc):
        try:
            for line in proc.stdout:
                self._append_log(line)
        except Exception:
            pass
        self.root.after(0, lambda: self._on_server_exit(proc))

    def _on_server_exit(self, proc):
        if self.server_process is not proc:
            return
        self.server_process = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self._set_status("Server stopped (process exited unexpectedly)", "#e67e22")
        self._append_log("[GUI] Server process exited unexpectedly\n")

    # ================================================================== #
    #  GPS methods
    # ================================================================== #

    def _get_host_port(self):
        return self.host_entry.get().strip(), int(self.port_entry.get().strip())

    def _target_esp_id(self):
        """Return the mac_id for the currently selected device."""
        return DEVICE_IDS.get(self.device_var.get(), 0)

    @staticmethod
    def _recv_exact(sock, n):
        """Read exactly n bytes from sock (TCP may deliver a reply in pieces)."""
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:        # peer closed before sending the full packet
                break
            buf.extend(chunk)
        return bytes(buf)

    # ------------------------------------------------------------------ #
    #  Survey-In
    # ------------------------------------------------------------------ #

    def start_survey(self):
        try:
            min_dur  = int(self.svin_dur_entry.get())
            acc_01mm = int(self.svin_acc_entry.get())
        except ValueError:
            self._set_status("Bad survey parameters — enter integers", "#e74c3c")
            return
        self._set_status("Sending survey-in command…", "#2980b9")
        threading.Thread(target=self._send_start_survey,
                         args=(min_dur, acc_01mm), daemon=True).start()

    def _send_start_survey(self, min_dur, acc_01mm):
        try:
            host, port = self._get_host_port()
            target_id  = self._target_esp_id()
            # inst=202, id=GUI_ID, RF=target ESP mac_id, w_num=min_dur, ms=acc_01mm
            msg = (202, GUI_ID, target_id, 0, 0, min_dur, acc_01mm, 0, 0, 0)
            with socket.socket() as s:
                s.settimeout(5)
                s.connect((host, port))
                s.sendall(struct.pack(PACKET_FORMAT, *msg))
            self.root.after(0, lambda: self._set_status(
                f"Survey-in started on ESP {target_id}  "
                f"(min {min_dur} s, acc {acc_01mm*0.1:.0f} mm)",
                "#27ae60"))
        except Exception as e:
            msg = f"Start survey error: {e}"
            self.root.after(0, lambda: self._set_status(msg, "#e74c3c"))

    def get_survey_results(self):
        self._set_status("Polling survey-in status…", "#2980b9")
        threading.Thread(target=self._fetch_survey, daemon=True).start()

    def _fetch_survey(self):
        try:
            host, port = self._get_host_port()
            target_id  = self._target_esp_id()
            # inst=201, id=GUI_ID, RF=target ESP mac_id
            msg = (201, GUI_ID, target_id, 0, 0, 0, 0, 0, 0, 0)
            with socket.socket() as s:
                s.settimeout(20)   # ESP needs time to poll UBX and reply
                s.connect((host, port))
                s.sendall(struct.pack(PACKET_FORMAT, *msg))
                data = self._recv_exact(s, PACKET_SIZE)

            if len(data) < PACKET_SIZE:
                raise ValueError(f"Short packet ({len(data)} bytes)")

            # Response: inst=201, id, RF=valid, Cal=active, ch=dur_s,
            #           w_num=meanX_cm, ms=meanY_cm, sub_ms=meanZ_cm,
            #           event_num=meanAcc_01mm, count=obs
            _, _, valid, active, dur_s, meanX, meanY, meanZ, meanAcc, obs = \
                struct.unpack(PACKET_FORMAT, data)

            def update():
                keys = list(self.survey_entries.keys())   # ECEF X, Y, Z, Acc
                vals = [meanX, meanY, meanZ, meanAcc]
                for key, val in zip(keys, vals):
                    e = self.survey_entries[key]
                    e.config(state="normal")
                    e.delete(0, tk.END)
                    e.insert(0, str(val))
                    e.config(state="readonly")
                # Extra readouts: observation time (s) and 3D std (meanAcc → mm)
                for entry, text in ((self.svin_obstime_entry, str(dur_s)),
                                    (self.svin_std3d_entry, f"{meanAcc * 0.1:.1f}")):
                    entry.config(state="normal")
                    entry.delete(0, tk.END)
                    entry.insert(0, text)
                    entry.config(state="readonly")
                status_str = (
                    f"valid={'yes' if valid else 'no'}  "
                    f"active={'yes' if active else 'no'}  "
                    f"dur={dur_s} s  obs={obs}"
                )
                self.svin_status_var.set(status_str)
                self._set_status("Survey-in status received", "#27ae60")

            self.root.after(0, update)

        except Exception as e:
            msg = f"GPS error: {e}"
            self.root.after(0, lambda: self._set_status(msg, "#e74c3c"))

    # ------------------------------------------------------------------ #
    #  Fixed position  (set / probe)
    # ------------------------------------------------------------------ #

    def send_fix_coordinates(self):
        try:
            x   = int(self.fix_entries["x"].get())
            y   = int(self.fix_entries["y"].get())
            z   = int(self.fix_entries["z"].get())
            acc = int(self.fix_entries["Acc"].get())
        except ValueError:
            self._set_status("Bad fix coords — enter integers (cm / 0.1 mm)", "#e74c3c")
            return
        self._set_status("Sending fixed coordinates…", "#2980b9")
        threading.Thread(target=self._send_fix,
                         args=(x, y, z, acc), daemon=True).start()
        self._clear_entries(self.fix_entries["x"], self.fix_entries["y"],
                            self.fix_entries["z"], self.fix_entries["Acc"])

    def _send_fix(self, x, y, z, acc):
        try:
            host, port = self._get_host_port()
            target_id  = self._target_esp_id()
            # inst=203, id=GUI_ID, RF=target, w_num=X, ms=Y, sub_ms=Z, event_num=Acc
            msg = (203, GUI_ID, target_id, 0, 0, x, y, z, acc, 0)
            with socket.socket() as s:
                s.settimeout(5)
                s.connect((host, port))
                s.sendall(struct.pack(PACKET_FORMAT, *msg))
            self.root.after(0, lambda: self._set_status(
                f"Fixed coords sent to ESP {target_id}  "
                f"(X={x} Y={y} Z={z} cm, acc {acc*0.1:.0f} mm)", "#27ae60"))
        except Exception as e:
            msg = f"Send fix error: {e}"
            self.root.after(0, lambda: self._set_status(msg, "#e74c3c"))

    def probe_fixed_coordinates(self):
        self._set_status("Probing fixed position…", "#2980b9")
        threading.Thread(target=self._fetch_fixed, daemon=True).start()

    def _fetch_fixed(self):
        try:
            host, port = self._get_host_port()
            target_id  = self._target_esp_id()
            # inst=204, id=GUI_ID, RF=target ESP mac_id
            msg = (204, GUI_ID, target_id, 0, 0, 0, 0, 0, 0, 0)
            with socket.socket() as s:
                s.settimeout(20)   # ESP needs time to poll UBX and reply
                s.connect((host, port))
                s.sendall(struct.pack(PACKET_FORMAT, *msg))
                data = self._recv_exact(s, PACKET_SIZE)

            if len(data) < PACKET_SIZE:
                raise ValueError(f"Short packet ({len(data)} bytes)")

            # Response: inst=204, id, RF=mode, Cal=is_lla, ch,
            #           w_num=X_cm, ms=Y_cm, sub_ms=Z_cm, event_num=acc_01mm, count
            _, _, mode, _, _, X, Y, Z, Acc, _ = struct.unpack(PACKET_FORMAT, data)

            def update():
                vals = {"X (cm)": X, "Y (cm)": Y, "Z (cm)": Z, "Acc (0.1mm)": Acc}
                for key, val in vals.items():
                    e = self.fix_read_entries[key]
                    e.config(state="normal")
                    e.delete(0, tk.END)
                    e.insert(0, str(val))
                    e.config(state="readonly")
                mode_names = {0: "disabled", 1: "survey-in", 2: "fixed"}
                self.fix_mode_var.set(f"mode: {mode_names.get(mode, mode)}")
                self._set_status("Fixed-position config received", "#27ae60")

            self.root.after(0, update)

        except Exception as e:
            msg = f"Probe fix error: {e}"
            self.root.after(0, lambda: self._set_status(msg, "#e74c3c"))

    def copy_survey_to_fix(self):
        """Copy the survey-in ECEF result into the fixed-position input fields."""
        mapping = [("ECEF X (cm)", "x"), ("ECEF Y (cm)", "y"),
                   ("ECEF Z (cm)", "z"), ("Acc (0.1mm)", "Acc")]
        for src, dst in mapping:
            val = self.survey_entries[src].get()
            e = self.fix_entries[dst]
            e.delete(0, tk.END)
            e.insert(0, val)
        self._set_status("Survey result copied to fix fields — review, then Send Fixed Coords", "#2980b9")

    # ------------------------------------------------------------------ #
    #  Receiver: restart + measurement rate
    # ------------------------------------------------------------------ #

    def restart_gps(self):
        rtype = self.reset_type_var.get()
        if not messagebox.askyesno(
                "Restart GPS",
                f"Send a {rtype} restart to the selected GPS?\n\n"
                "This briefly interrupts data collection while the receiver "
                "restarts. (Survey/fixed/rate config is saved to flash, so it "
                "persists across the restart.)"):
            return
        self._set_status(f"Sending {rtype} restart…", "#2980b9")
        threading.Thread(target=self._send_restart,
                         args=(RESET_TYPES.get(rtype, 0x0001),), daemon=True).start()

    def _send_restart(self, mask):
        try:
            host, port = self._get_host_port()
            target_id  = self._target_esp_id()
            # inst=205, RF=target, w_num=navBbrMask, ms=resetMode (0x01 = controlled SW reset)
            msg = (205, GUI_ID, target_id, 0, 0, mask, 0x01, 0, 0, 0)
            with socket.socket() as s:
                s.settimeout(5)
                s.connect((host, port))
                s.sendall(struct.pack(PACKET_FORMAT, *msg))
            self.root.after(0, lambda: self._set_status(
                f"Restart sent to ESP {target_id}", "#27ae60"))
        except Exception as e:
            m = f"Restart error: {e}"
            self.root.after(0, lambda: self._set_status(m, "#e74c3c"))

    def read_rate(self):
        self._set_status("Reading measurement rate…", "#2980b9")
        threading.Thread(target=self._fetch_rate, daemon=True).start()

    def _fetch_rate(self):
        try:
            host, port = self._get_host_port()
            target_id  = self._target_esp_id()
            msg = (206, GUI_ID, target_id, 0, 0, 0, 0, 0, 0, 0)
            with socket.socket() as s:
                s.settimeout(20)
                s.connect((host, port))
                s.sendall(struct.pack(PACKET_FORMAT, *msg))
                data = self._recv_exact(s, PACKET_SIZE)

            if len(data) < PACKET_SIZE:
                raise ValueError(f"Short packet ({len(data)} bytes)")

            # Response: inst=206, id, RF=timeref, Cal, ch, w_num=meas_ms, ms=nav, ...
            _, _, timeref, _, _, meas, nav, _, _, _ = struct.unpack(PACKET_FORMAT, data)

            def update():
                self.rate_meas_entry.delete(0, tk.END); self.rate_meas_entry.insert(0, str(meas))
                self.rate_nav_entry.delete(0, tk.END);  self.rate_nav_entry.insert(0, str(nav))
                self.timeref_var.set(TIMEREF_BY_VALUE.get(timeref, "(no change)"))
                hz = (1000.0 / meas) if meas else 0
                self._set_status(
                    f"Rate: {meas} ms ({hz:.2f} Hz), nav {nav} cyc, "
                    f"timeref {TIMEREF_BY_VALUE.get(timeref, timeref)}", "#27ae60")

            self.root.after(0, update)

        except Exception as e:
            m = f"Read rate error: {e}"
            self.root.after(0, lambda: self._set_status(m, "#e74c3c"))

    def set_rate(self):
        try:
            meas = int(self.rate_meas_entry.get()) if self.rate_meas_entry.get().strip() else 0
            nav  = int(self.rate_nav_entry.get())  if self.rate_nav_entry.get().strip()  else 0
        except ValueError:
            self._set_status("Bad rate values — enter integers (blank = leave unchanged)", "#e74c3c")
            return
        timeref = TIMEREF_OPTIONS.get(self.timeref_var.get(), -1)
        if meas <= 0 and nav <= 0 and timeref < 0:
            self._set_status("Nothing to set — fill Meas/Nav or pick a TimeRef", "#e67e22")
            return
        self._set_status("Setting measurement rate…", "#2980b9")
        threading.Thread(target=self._send_rate, args=(meas, nav, timeref), daemon=True).start()
        self._clear_entries(self.rate_meas_entry, self.rate_nav_entry)
        self.timeref_var.set("(no change)")

    def _send_rate(self, meas, nav, timeref):
        try:
            host, port = self._get_host_port()
            target_id  = self._target_esp_id()
            # inst=207, RF=target, w_num=meas_ms, ms=nav, sub_ms=timeref (<=0 / <0 = skip on ESP)
            msg = (207, GUI_ID, target_id, 0, 0, meas, nav, timeref, 0, 0)
            with socket.socket() as s:
                s.settimeout(5)
                s.connect((host, port))
                s.sendall(struct.pack(PACKET_FORMAT, *msg))
            self.root.after(0, lambda: self._set_status(
                f"Rate set on ESP {target_id}  (meas {meas} ms, nav {nav}, timeref {timeref})",
                "#27ae60"))
        except Exception as e:
            m = f"Set rate error: {e}"
            self.root.after(0, lambda: self._set_status(m, "#e74c3c"))

    def get_version(self):
        self._set_status("Reading GPS version…", "#2980b9")
        threading.Thread(target=self._fetch_version, daemon=True).start()

    def _fetch_version(self):
        try:
            host, port = self._get_host_port()
            target_id  = self._target_esp_id()
            msg = (208, GUI_ID, target_id, 0, 0, 0, 0, 0, 0, 0)
            with socket.socket() as s:
                s.settimeout(20)
                s.connect((host, port))
                s.sendall(struct.pack(PACKET_FORMAT, *msg))
                data = self._recv_exact(s, PACKET_SIZE)
            if len(data) < PACKET_SIZE:
                raise ValueError(f"Short packet ({len(data)} bytes)")
            ints = struct.unpack(PACKET_FORMAT, data)
            # 8 data ints carry 32 bytes of the version string
            vbytes = struct.pack("!8i", *ints[2:10])
            ver = vbytes.split(b"\x00")[0].decode("ascii", "replace").strip()

            def update():
                self.version_var.set(ver or "(empty)")
                self._set_status(f"GPS version: {ver}", "#27ae60")
            self.root.after(0, update)
        except Exception as e:
            m = f"Version error: {e}"
            self.root.after(0, lambda: self._set_status(m, "#e74c3c"))

    # ================================================================== #
    #  Tab 3 – GPS Slow Control (live health plot + per-unit stats)
    # ================================================================== #

    def _build_slowctrl_tab(self, parent):
        ctl = tk.Frame(parent)
        ctl.pack(fill="x", padx=8, pady=(6, 2))

        tk.Label(ctl, text="Plot metric:").pack(side="left")
        self.cpu_metric_var = tk.StringVar(value="CPU load (%)")
        ttk.Combobox(ctl, textvariable=self.cpu_metric_var,
                     values=list(PLOT_METRICS.keys()), state="readonly", width=14
                     ).pack(side="left", padx=(4, 16))

        tk.Label(ctl, text="Stats for GPS:").pack(side="left")
        self.cpu_unit_var = tk.StringVar(value="—")
        self.cpu_unit_combo = ttk.Combobox(ctl, textvariable=self.cpu_unit_var,
                                            values=[], state="readonly", width=8)
        self.cpu_unit_combo.pack(side="left", padx=4)

        self.cpu_fig = Figure(figsize=(6, 3.0), dpi=100)
        self.cpu_ax = self.cpu_fig.add_subplot(111)
        self.cpu_canvas = FigureCanvasTkAgg(self.cpu_fig, master=parent)
        self.cpu_canvas.get_tk_widget().pack(fill="both", expand=True, padx=8, pady=(0, 4))

        # Per-unit stats readout (latest values for the selected GPS)
        stats = tk.LabelFrame(parent, text="Latest stats (selected GPS)", padx=8, pady=4)
        stats.pack(fill="x", padx=8, pady=(0, 8))
        self.cpu_stat_vars = {}
        labels = ["Max CPU %", "Max Mem %", "Max IO %", "Runtime (s)",
                  "Notices", "Warnings", "Errors"]
        for i, name in enumerate(labels):
            tk.Label(stats, text=name, font=("Arial", 8), fg="#444").grid(row=0, column=i, padx=6)
            v = tk.StringVar(value="—")
            tk.Label(stats, textvariable=v, font=("Arial", 10, "bold")).grid(row=1, column=i, padx=6)
            self.cpu_stat_vars[name] = v

        self._update_slowctrl()

    def _latest_cpu_log(self):
        """Path to the gps_cpu_log.txt in the highest-numbered run folder, or None."""
        best, best_n = None, -1
        try:
            for name in os.listdir(GPS_DATA_DIR):
                m = re.match(r"Run_(\d+)_", name)
                p = os.path.join(GPS_DATA_DIR, name)
                if m and os.path.isdir(p):
                    n = int(m.group(1))
                    if n > best_n:
                        best_n, best = n, os.path.join(p, "gps_cpu_log.txt")
        except OSError:
            pass
        return best

    def _update_slowctrl(self):
        try:
            metric_col = PLOT_METRICS.get(self.cpu_metric_var.get(), 2)
            path = self._latest_cpu_log()
            times = {}    # id -> [datetime]
            yvals = {}    # id -> [metric value]
            last = {}     # id -> full parts list (latest row)
            if path and os.path.exists(path):
                with open(path) as f:
                    for line in f:
                        parts = [p.strip() for p in line.split(";")]
                        if len(parts) < 13:
                            continue
                        try:
                            uid = int(parts[1])
                            y = int(parts[metric_col])
                            t = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S.%f")
                        except (ValueError, IndexError):
                            continue   # skips the header row too
                        times.setdefault(uid, []).append(t)
                        yvals.setdefault(uid, []).append(y)
                        last[uid] = parts

            # Keep the unit dropdown in sync with the IDs we've seen
            ids = [str(u) for u in sorted(last)]
            if list(self.cpu_unit_combo["values"]) != ids:
                self.cpu_unit_combo["values"] = ids
                if self.cpu_unit_var.get() not in ids and ids:
                    self.cpu_unit_var.set(ids[0])

            # Plot the selected metric for every unit
            self.cpu_ax.clear()
            self.cpu_ax.set_title(self.cpu_metric_var.get())
            self.cpu_ax.set_ylabel(self.cpu_metric_var.get())
            if metric_col != 9:           # 0-100 for %, autoscale for temp
                self.cpu_ax.set_ylim(0, 100)
            for uid in sorted(yvals):
                self.cpu_ax.plot(times[uid], yvals[uid], marker="o", ms=3, label=f"ID {uid}")
            if yvals:
                self.cpu_ax.legend(loc="upper left", fontsize=8)
                self.cpu_fig.autofmt_xdate()
            else:
                self.cpu_ax.text(0.5, 0.5, "waiting for telemetry…", ha="center", va="center",
                                 transform=self.cpu_ax.transAxes, color="#888")
            self.cpu_canvas.draw_idle()

            # Stats readout for the selected unit
            sel = self.cpu_unit_var.get()
            try:
                p = last.get(int(sel)) if sel not in ("", "—") else None
            except ValueError:
                p = None
            keys = ["Max CPU %", "Max Mem %", "Max IO %", "Runtime (s)",
                    "Notices", "Warnings", "Errors"]
            cols = [3, 5, 7, 8, 10, 11, 12]
            for name, col in zip(keys, cols):
                self.cpu_stat_vars[name].set(p[col] if p else "—")
        except Exception:
            pass
        finally:
            self.root.after(3000, self._update_slowctrl)

    # ================================================================== #
    #  Tab 4 – Data Slow Control (per-detector rates + wifi integrity)
    # ================================================================== #

    def _build_datactrl_tab(self, parent):
        ctl = tk.Frame(parent)
        ctl.pack(fill="x", padx=8, pady=(6, 2))

        tk.Label(ctl, text="Stats for detector:").pack(side="left")
        self.data_unit_var = tk.StringVar(value="—")
        self.data_unit_combo = ttk.Combobox(ctl, textvariable=self.data_unit_var,
                                             values=[], state="readonly", width=14)
        self.data_unit_combo.pack(side="left", padx=4)
        # display-string -> detector id, rebuilt on every refresh
        self._data_disp_to_id = {}

        # Two stacked plots: observed rate (top) and wifi integrity (bottom).
        self.data_fig = Figure(figsize=(6, 4.6), dpi=100)
        self.data_ax_rate = self.data_fig.add_subplot(211)
        self.data_ax_integ = self.data_fig.add_subplot(212)
        self.data_canvas = FigureCanvasTkAgg(self.data_fig, master=parent)
        self.data_canvas.get_tk_widget().pack(fill="both", expand=True, padx=8, pady=(0, 4))

        # Latest-values readout for the selected detector
        stats = tk.LabelFrame(parent, text="Latest stats (selected detector)", padx=8, pady=4)
        stats.pack(fill="x", padx=8, pady=(0, 8))
        self.data_stat_vars = {}
        labels = ["Ch0 (Hz)", "Ch1 (Hz)", "Total (Hz)", "Wifi integrity", "Unreas %"]
        for i, name in enumerate(labels):
            tk.Label(stats, text=name, font=("Arial", 8), fg="#444").grid(row=0, column=i, padx=8)
            v = tk.StringVar(value="—")
            tk.Label(stats, textvariable=v, font=("Arial", 10, "bold")).grid(row=1, column=i, padx=8)
            self.data_stat_vars[name] = v

        self._update_datactrl()

    def _reset_stats_state(self):
        """Clear the incremental-read buffers (new run, rotation, or first use)."""
        self._stats_offset = 0
        self._stats_rate = defaultdict(lambda: deque(maxlen=STATS_MAX_POINTS))
        self._stats_integ = defaultdict(lambda: deque(maxlen=STATS_MAX_POINTS))

    def _latest_stats_log(self):
        """Path to stats_log.txt in the highest-numbered run folder, or None."""
        best, best_n = None, -1
        try:
            for name in os.listdir(GPS_DATA_DIR):
                m = re.match(r"Run_(\d+)_", name)
                p = os.path.join(GPS_DATA_DIR, name)
                if m and os.path.isdir(p):
                    n = int(m.group(1))
                    if n > best_n:
                        best_n, best = n, os.path.join(p, "stats_log.txt")
        except OSError:
            pass
        return best

    @staticmethod
    def _parse_stats_line(line):
        """Parse one stats_log line into (inst, det_id, ts_or_None, data[0:8]).

        Handles both the current (no datetime) and future (datetime-prefixed)
        layouts, and returns None for cycle headers / blanks / malformed rows.
        """
        line = line.strip()
        if not line:
            return None
        parts = [p.strip() for p in line.split(";")]
        # Detect datetime prefix: first token is an int only in the no-ts layout.
        ts = None
        try:
            int(parts[0])
            body = parts                      # current layout: inst; ID; d0..d7
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    ts = datetime.strptime(parts[0], fmt)
                    break
                except ValueError:
                    pass
            body = parts[1:]                  # future layout: drop the datetime
        if len(body) < 10:
            return None                       # cycle header / truncated line
        try:
            inst = int(body[0])
            det = int(body[1])
            data = [int(x) for x in body[2:10]]
        except ValueError:
            return None
        return inst, det, ts, data

    def _read_new_stats(self):
        """Incrementally consume newly-appended bytes of the latest stats_log."""
        path = self._latest_stats_log()
        if path != self._stats_path:          # new run -> start fresh
            self._stats_path = path
            self._reset_stats_state()
        if not path or not os.path.exists(path):
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size < self._stats_offset:         # file shrank / rotated -> re-read
            self._reset_stats_state()
        if size == self._stats_offset:
            return                            # nothing new

        # Read in binary so byte offsets are exact on Windows, and only consume
        # up to the last complete line (the writer may be mid-append).
        with open(path, "rb") as f:
            f.seek(self._stats_offset)
            raw = f.read()
        nl = raw.rfind(b"\n")
        if nl == -1:
            return                            # no complete line yet
        self._stats_offset += nl + 1
        text = raw[:nl + 1].decode("utf-8", errors="replace")

        for line in text.splitlines():
            parsed = self._parse_stats_line(line)
            if not parsed:
                continue
            inst, det, ts, d = parsed
            if inst == 98:                    # rate in: events / (deltaT/1000)
                dt_ms = d[2]
                if dt_ms > 0:
                    ch0 = d[0] / (dt_ms / 1000.0)
                    ch1 = d[1] / (dt_ms / 1000.0)
                    self._stats_rate[det].append((ts, ch0, ch1, ch0 + ch1))
            elif inst == 96:                  # integrity: (data+null)/req, unreas/req
                req = d[0]
                if req > 0:
                    wifi = (d[1] + d[2]) / req
                    unreas = d[3] / req
                    self._stats_integ[det].append((ts, wifi, unreas))

    def _det_label(self, det_id):
        name = ID_TO_NAME.get(det_id)
        return f"{name} ({det_id})" if name else str(det_id)

    def _update_datactrl(self):
        try:
            self._read_new_stats()

            # Keep the detector dropdown in sync with the ids we've seen.
            ids = sorted(set(self._stats_rate) | set(self._stats_integ))
            disp = [self._det_label(i) for i in ids]
            self._data_disp_to_id = dict(zip(disp, ids))
            if list(self.data_unit_combo["values"]) != disp:
                self.data_unit_combo["values"] = disp
                if self.data_unit_var.get() not in disp and disp:
                    self.data_unit_var.set(disp[0])

            sel_id = self._data_disp_to_id.get(self.data_unit_var.get())
            used_time = False

            # ---- Observed rate (top) ---------------------------------- #
            self.data_ax_rate.clear()
            self.data_ax_rate.set_title(
                f"Observed rate — {self._det_label(sel_id)}" if sel_id is not None
                else "Observed rate")
            self.data_ax_rate.set_ylabel("Hz")
            rate = list(self._stats_rate.get(sel_id, [])) if sel_id is not None else []
            if rate:
                xr, t_used = self._stats_x([r[0] for r in rate])
                used_time |= t_used
                self.data_ax_rate.plot(xr, [r[1] for r in rate], lw=1, label="Ch0")
                self.data_ax_rate.plot(xr, [r[2] for r in rate], lw=1, label="Ch1")
                self.data_ax_rate.plot(xr, [r[3] for r in rate], lw=1.4, color="k",
                                       label="Total")
                self.data_ax_rate.set_ylim(bottom=0)
                self.data_ax_rate.legend(loc="upper left", fontsize=8, ncol=3)
                self.data_ax_rate.grid(True, alpha=0.3)
            else:
                self._waiting_text(self.data_ax_rate)

            # ---- Wifi integrity (bottom) ------------------------------ #
            self.data_ax_integ.clear()
            self.data_ax_integ.set_title("Wifi integrity & unreasonable requests")
            self.data_ax_integ.set_ylabel("%")
            integ = list(self._stats_integ.get(sel_id, [])) if sel_id is not None else []
            if integ:
                xi, t_used = self._stats_x([r[0] for r in integ])
                used_time |= t_used
                self.data_ax_integ.plot(xi, [r[1] * 100 for r in integ], lw=1.2,
                                        color="#2980b9", label="Wifi integrity")
                self.data_ax_integ.plot(xi, [r[2] * 100 for r in integ], lw=1,
                                        color="#e74c3c", label="Unreasonable")
                # Integrity sits near 100%; pad headroom so the line isn't clipped.
                self.data_ax_integ.set_ylim(0, 110)
                self.data_ax_integ.axhline(100, color="#aaa", lw=0.8, ls="--")
                self.data_ax_integ.legend(loc="lower left", fontsize=8, ncol=2)
                self.data_ax_integ.grid(True, alpha=0.3)
            else:
                self._waiting_text(self.data_ax_integ)

            self.data_ax_integ.set_xlabel("time" if used_time else "sample")
            if used_time:
                self.data_fig.autofmt_xdate()
            self.data_fig.tight_layout()
            self.data_canvas.draw_idle()

            # ---- Readout --------------------------------------------- #
            last_r = rate[-1] if rate else None
            last_i = integ[-1] if integ else None
            self.data_stat_vars["Ch0 (Hz)"].set(f"{last_r[1]:.1f}" if last_r else "—")
            self.data_stat_vars["Ch1 (Hz)"].set(f"{last_r[2]:.1f}" if last_r else "—")
            self.data_stat_vars["Total (Hz)"].set(f"{last_r[3]:.1f}" if last_r else "—")
            self.data_stat_vars["Wifi integrity"].set(f"{last_i[1] * 100:.1f}%" if last_i else "—")
            self.data_stat_vars["Unreas %"].set(f"{last_i[2] * 100:.1f}%" if last_i else "—")
        except Exception:
            pass
        finally:
            self.root.after(3000, self._update_datactrl)

    @staticmethod
    def _stats_x(timestamps):
        """Return (x_values, used_time).

        Real datetimes if every row has one (future log format), else the
        sample index (current format has no datetime column yet).
        """
        if timestamps and all(t is not None for t in timestamps):
            return timestamps, True
        return list(range(len(timestamps))), False

    @staticmethod
    def _waiting_text(ax):
        ax.text(0.5, 0.5, "waiting for telemetry…", ha="center", va="center",
                transform=ax.transAxes, color="#888")

    # ================================================================== #
    #  Tab 4 – Auto Survey (batch survey-in → auto-fix all devices)
    # ================================================================== #

    def _build_autosurvey_tab(self, parent):
        # ---- Parameters ---------------------------------------------- #
        params = tk.LabelFrame(parent, text="Survey Parameters", padx=10, pady=8)
        params.pack(fill="x", padx=10, pady=(10, 4))

        tk.Label(params, text="Min dur (s):").grid(row=0, column=0, sticky="w")
        self.as_dur_entry = tk.Entry(params, width=8)
        self.as_dur_entry.insert(0, "60")
        self.as_dur_entry.grid(row=0, column=1, padx=(2, 14), sticky="w")

        tk.Label(params, text="Acc limit (0.1 mm):").grid(row=0, column=2, sticky="w")
        self.as_acc_entry = tk.Entry(params, width=8)
        self.as_acc_entry.insert(0, "3000")   # 300 mm default
        self.as_acc_entry.grid(row=0, column=3, padx=(2, 14), sticky="w")

        tk.Label(params, text="Re-probe every (min):").grid(row=0, column=4, sticky="w")
        self.as_interval_entry = tk.Entry(params, width=8)
        self.as_interval_entry.insert(0, "60")
        self.as_interval_entry.grid(row=0, column=5, padx=(2, 0), sticky="w")

        tk.Label(params,
                 text="Uses Host/Ctrl Port from the GPS Comms tab.  Each device "
                      "is surveyed, then auto-fixed as soon as it reports valid.",
                 font=("Arial", 8), fg="gray").grid(
            row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))

        # ---- Device checklist ---------------------------------------- #
        devbox = tk.LabelFrame(parent, text="Devices (pick which to survey)", padx=10, pady=8)
        devbox.pack(fill="x", padx=10, pady=4)

        self.autosurvey_vars = {}
        cols = 4
        for i, name in enumerate(DEVICES):
            var = tk.BooleanVar(value=False)
            self.autosurvey_vars[name] = var
            mac = DEVICE_IDS.get(name, 0)
            label = f"{name}" if mac else f"{name} (no id)"
            tk.Checkbutton(devbox, text=label, variable=var,
                           state=("normal" if mac else "disabled")
                           ).grid(row=i // cols, column=i % cols, sticky="w", padx=4, pady=1)

        btns = tk.Frame(devbox)
        btns.grid(row=(len(DEVICES) - 1) // cols + 1, column=0, columnspan=cols,
                  sticky="w", pady=(6, 0))
        tk.Button(btns, text="Select All", width=10,
                  command=lambda: self._autosurvey_select_all(True)).pack(side="left", padx=(0, 4))
        tk.Button(btns, text="Clear", width=10,
                  command=lambda: self._autosurvey_select_all(False)).pack(side="left")

        # ---- Controls ------------------------------------------------ #
        ctrl = tk.Frame(parent)
        ctrl.pack(fill="x", padx=10, pady=4)
        self.as_start_btn = tk.Button(
            ctrl, text="Start Auto Survey", command=self.start_autosurvey,
            bg="#2ecc71", fg="white", activebackground="#27ae60",
            width=18, font=("Arial", 10))
        self.as_start_btn.pack(side="left", padx=(0, 6))
        self.as_stop_btn = tk.Button(
            ctrl, text="Stop", command=self.stop_autosurvey,
            bg="#e74c3c", fg="white", activebackground="#c0392b",
            width=12, font=("Arial", 10), state="disabled")
        self.as_stop_btn.pack(side="left")
        tk.Button(ctrl, text="Probe Status", width=14,
                  command=self.probe_autosurvey).pack(side="left", padx=(6, 0))

        # ---- Progress table ------------------------------------------ #
        prog = tk.LabelFrame(parent, text="Progress", padx=5, pady=5)
        prog.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        columns = ("device", "macid", "state", "dur", "valid", "obs", "std3d", "result")
        headings = {"device": "Device", "macid": "mac_id", "state": "State",
                    "dur": "Dur (s)", "valid": "Valid", "obs": "Obs",
                    "std3d": "3D std (mm)",
                    "result": "Result (X / Y / Z cm, acc 0.1mm)"}
        widths = {"device": 70, "macid": 60, "state": 125, "dur": 55,
                  "valid": 45, "obs": 55, "std3d": 70, "result": 215}
        self.as_tree = ttk.Treeview(prog, columns=columns, show="headings", height=8)
        for c in columns:
            self.as_tree.heading(c, text=headings[c])
            self.as_tree.column(c, width=widths[c],
                                anchor=("w" if c in ("state", "result") else "center"))
        self.as_tree.pack(fill="both", expand=True)

    def _autosurvey_select_all(self, value):
        for name, var in self.autosurvey_vars.items():
            if DEVICE_IDS.get(name, 0):      # leave id-less devices untouched (disabled)
                var.set(value)

    # ------------------------------------------------------------------ #
    #  Auto-survey orchestration
    # ------------------------------------------------------------------ #

    def start_autosurvey(self):
        if self.autosurvey_thread and self.autosurvey_thread.is_alive():
            return

        selected = [(n, DEVICE_IDS.get(n, 0))
                    for n, v in self.autosurvey_vars.items() if v.get()]
        selected = [(n, t) for n, t in selected if t]    # drop id-less just in case
        if not selected:
            self._set_status("Auto survey: select at least one device", "#e67e22")
            return

        try:
            min_dur = int(self.as_dur_entry.get())
            acc     = int(self.as_acc_entry.get())
            interval_min = float(self.as_interval_entry.get())
            if min_dur <= 0 or acc <= 0 or interval_min <= 0:
                raise ValueError
        except ValueError:
            self._set_status("Auto survey: bad parameters (positive numbers required)", "#e74c3c")
            return
        interval_s = int(interval_min * 60)

        # Initialise the table: one row per selected device
        for item in self.as_tree.get_children():
            self.as_tree.delete(item)
        for name, tid in selected:
            self.as_tree.insert("", "end", iid=name,
                                values=(name, tid, "queued", "—", "—", "—", "—", "—"))

        self.autosurvey_stop.clear()
        self.as_start_btn.config(state="disabled")
        self.as_stop_btn.config(state="normal")
        self._set_status(f"Auto survey started on {len(selected)} device(s)…", "#2980b9")
        self.autosurvey_thread = threading.Thread(
            target=self._autosurvey_worker,
            args=(selected, min_dur, acc, interval_s), daemon=True)
        self.autosurvey_thread.start()

    def stop_autosurvey(self):
        self.autosurvey_stop.set()
        self.as_stop_btn.config(state="disabled")
        self._set_status("Auto survey: stopping after current poll…", "#e67e22")

    def _autosurvey_finish(self):
        self.as_start_btn.config(state="normal")
        self.as_stop_btn.config(state="disabled")

    def probe_autosurvey(self):
        """On-demand poll of every device still being surveyed (status only —
        does not fix; the worker handles fixing on its own schedule)."""
        terminal = ("FIXED", "stopped", "skipped", "queued")
        targets = []
        for name in self.as_tree.get_children():
            state = self.as_tree.set(name, "state")
            if state in terminal or "error" in state:
                continue
            try:
                tid = int(self.as_tree.set(name, "macid"))
            except ValueError:
                continue
            targets.append((name, tid))
        if not targets:
            self._set_status("Probe: no active surveys to check", "#e67e22")
            return
        self._set_status(f"Probing {len(targets)} device(s)…", "#2980b9")
        threading.Thread(target=self._probe_worker, args=(targets,), daemon=True).start()

    def _probe_worker(self, targets):
        try:
            host, port = self._get_host_port()
        except Exception as e:
            self.root.after(0, lambda e=e: self._set_status(
                f"Probe: bad host/port ({e})", "#e74c3c"))
            return
        for name, tid in targets:
            try:
                valid, _active, dur_s, mx, my, mz, macc, obs = self._svin_poll(host, port, tid)
            except Exception as e:
                self._as_update(name, result=f"probe err: {e}")
                continue
            self._as_update(name, dur=dur_s, obs=obs,
                            valid=("yes" if valid else "no"),
                            std3d=f"{macc * 0.1:.1f}")
        self.root.after(0, lambda: self._set_status("Probe complete", "#27ae60"))

    def _as_update(self, name, **cols):
        """Thread-safe Treeview row update (column -> value)."""
        def _update():
            if not self.as_tree.exists(name):
                return
            for col, val in cols.items():
                self.as_tree.set(name, col, val)
        self.root.after(0, _update)

    def _autosurvey_worker(self, selected, min_dur, acc, interval_s):
        try:
            host, port = self._get_host_port()
        except Exception as e:
            self.root.after(0, lambda e=e: self._set_status(
                f"Auto survey: bad host/port ({e})", "#e74c3c"))
            self.root.after(0, self._autosurvey_finish)
            return

        results_path = self._init_results_file(min_dur, acc, len(selected))

        # Phase 1 — kick off survey-in on every selected device
        pending = {}    # name -> mac_id
        for name, tid in selected:
            if self.autosurvey_stop.is_set():
                break
            try:
                self._svin_start(host, port, tid, min_dur, acc)
                pending[name] = tid
                self._as_update(name, state="waiting min dur")
            except Exception as e:
                self._as_update(name, state="start error", result=str(e))

        # Phase 2 — poll each device; first poll after min_dur, then every interval
        next_poll = {name: time.monotonic() + min_dur for name in pending}

        while pending and not self.autosurvey_stop.is_set():
            now = time.monotonic()
            for name in [n for n in pending if now >= next_poll[n]]:
                if self.autosurvey_stop.is_set():
                    break
                tid = pending[name]
                self._as_update(name, state="polling")
                try:
                    valid, _active, dur_s, mx, my, mz, macc, obs = \
                        self._svin_poll(host, port, tid)
                except Exception as e:
                    # Transient (timeout / ESP busy) — retry on the normal interval
                    self._as_update(name, state="poll error (retry)", result=str(e))
                    next_poll[name] = time.monotonic() + interval_s
                    continue

                self._as_update(name, dur=dur_s, obs=obs,
                                valid=("yes" if valid else "no"),
                                std3d=f"{macc * 0.1:.1f}")
                if valid:
                    try:
                        self._fix_send(host, port, tid, mx, my, mz, macc)
                        self._as_update(name, state="FIXED",
                                        result=f"{mx} / {my} / {mz}  acc {macc}")
                    except Exception as e:
                        self._as_update(name, state="fix error", result=str(e))
                    # Record the finished survey (poll NAV-POSLLH for LLH + accuracy)
                    try:
                        pos = self._posllh_poll(host, port, tid)
                    except Exception:
                        pos = None
                    self._append_result(results_path, name, tid, min_dur, acc,
                                         dur_s, obs, macc, mx, my, mz, pos)
                    del pending[name]
                else:
                    next_poll[name] = time.monotonic() + interval_s
                    self._as_update(name, state=f"retry in {interval_s // 60} min")

            self.autosurvey_stop.wait(2)    # responsive idle between checks

        if self.autosurvey_stop.is_set():
            for name in pending:
                self._as_update(name, state="stopped")
            self.root.after(0, lambda: self._set_status("Auto survey stopped", "#e67e22"))
        else:
            self.root.after(0, lambda: self._set_status(
                "Auto survey complete — all selected devices fixed", "#27ae60"))
        self.root.after(0, self._autosurvey_finish)

    # --- Results file (one timestamped file per auto-survey run) -------- #

    def _init_results_file(self, min_dur, acc, ndev):
        """Create the per-run results file and write its header. Returns the
        path, or None if it couldn't be created (survey still proceeds)."""
        try:
            os.makedirs(SURVEY_RESULTS_DIR, exist_ok=True)
            ts = datetime.now()
            path = os.path.join(SURVEY_RESULTS_DIR,
                                "auto_survey_" + ts.strftime("%Y%m%d_%H%M%S") + ".txt")
            with open(path, "w") as f:
                f.write("Auto Survey Results\n")
                f.write("Run started: " + ts.strftime("%Y-%m-%d %H:%M:%S") + "\n")
                f.write(f"Devices: {ndev}\n")
                f.write(f"Configured min duration: {min_dur} s\n")
                f.write(f"Configured accuracy limit: {acc} (0.1 mm) = {acc * 0.1:.1f} mm\n")
                f.write("=" * 64 + "\n\n")
            self.root.after(0, lambda: self._set_status(
                f"Auto survey results -> {path}", "#2980b9"))
            return path
        except OSError as e:
            self.root.after(0, lambda e=e: self._set_status(
                f"Results file error: {e}", "#e67e22"))
            return None

    def _append_result(self, path, name, tid, min_dur, acc, dur_s, obs, macc, mx, my, mz, pos):
        """Append one finished device's block to the results file."""
        if not path:
            return
        try:
            with open(path, "a") as f:
                f.write(f"Device: {name}  (mac_id {tid})\n")
                f.write("  Finished: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n")
                f.write(f"  Min duration (config): {min_dur} s\n")
                f.write(f"  Observation time (surveyed): {dur_s} s    Observations: {obs}\n")
                f.write(f"  Accuracy limit (config): {acc * 0.1:.1f} mm\n")
                f.write(f"  3D std (SVIN): {macc * 0.1:.2f} mm\n")
                f.write(f"  ECEF  X: {mx} cm   Y: {my} cm   Z: {mz} cm\n")
                if pos:
                    lon, lat, height, hMSL, hAcc, vAcc = pos
                    f.write(f"  Lat: {lat * 1e-7:.7f} deg   Lon: {lon * 1e-7:.7f} deg\n")
                    f.write(f"  Height (ellipsoidal): {height} mm   Height (MSL): {hMSL} mm\n")
                    f.write(f"  Horizontal acc (2D): {hAcc} mm   Vertical acc: {vAcc} mm\n")
                else:
                    f.write("  Lat/Lon/Height: (NAV-POSLLH unavailable)\n")
                f.write("-" * 64 + "\n\n")
        except OSError as e:
            self.root.after(0, lambda e=e: self._set_status(
                f"Results write error: {e}", "#e67e22"))

    # --- Low-level packet I/O (no GUI; safe to call from the worker) --- #

    def _svin_start(self, host, port, target_id, min_dur, acc_01mm):
        msg = (202, GUI_ID, target_id, 0, 0, min_dur, acc_01mm, 0, 0, 0)
        with socket.socket() as s:
            s.settimeout(5)
            s.connect((host, port))
            s.sendall(struct.pack(PACKET_FORMAT, *msg))

    def _svin_poll(self, host, port, target_id):
        msg = (201, GUI_ID, target_id, 0, 0, 0, 0, 0, 0, 0)
        with socket.socket() as s:
            s.settimeout(20)
            s.connect((host, port))
            s.sendall(struct.pack(PACKET_FORMAT, *msg))
            data = self._recv_exact(s, PACKET_SIZE)
        if len(data) < PACKET_SIZE:
            raise ValueError(f"Short packet ({len(data)} bytes)")
        _, _, valid, active, dur_s, mx, my, mz, macc, obs = struct.unpack(PACKET_FORMAT, data)
        return valid, active, dur_s, mx, my, mz, macc, obs

    def _fix_send(self, host, port, target_id, x, y, z, acc):
        msg = (203, GUI_ID, target_id, 0, 0, x, y, z, acc, 0)
        with socket.socket() as s:
            s.settimeout(5)
            s.connect((host, port))
            s.sendall(struct.pack(PACKET_FORMAT, *msg))

    def _posllh_poll(self, host, port, target_id):
        """Poll NAV-POSLLH (inst 211). Returns
        (lon_1e7, lat_1e7, height_mm, hMSL_mm, hAcc_mm, vAcc_mm)."""
        msg = (211, GUI_ID, target_id, 0, 0, 0, 0, 0, 0, 0)
        with socket.socket() as s:
            s.settimeout(20)
            s.connect((host, port))
            s.sendall(struct.pack(PACKET_FORMAT, *msg))
            data = self._recv_exact(s, PACKET_SIZE)
        if len(data) < PACKET_SIZE:
            raise ValueError(f"Short packet ({len(data)} bytes)")
        # inst, id, RF=vAcc, Cal, ch, w_num=lon, ms=lat, sub_ms=height, event_num=hMSL, count=hAcc
        _, _, vAcc, _, _, lon, lat, height, hMSL, hAcc = struct.unpack(PACKET_FORMAT, data)
        return lon, lat, height, hMSL, hAcc, vAcc

    # ================================================================== #
    #  Tab 5 – PPS Controls (TP1 time-pulse read / set, per device)
    # ================================================================== #

    def _build_ppsctrl_tab(self, parent):
        # ---- Device select ------------------------------------------- #
        top = tk.LabelFrame(parent, text="Time Pulse 1 (TP1 / PPS)", padx=10, pady=8)
        top.pack(fill="x", padx=10, pady=(10, 4))

        tk.Label(top, text="Select Device:").grid(row=0, column=0, sticky="w")
        self.pps_device_var = tk.StringVar(value=DEVICES[0])
        ttk.Combobox(top, textvariable=self.pps_device_var,
                     values=DEVICES, state="readonly", width=10
                     ).grid(row=0, column=1, padx=8, sticky="w")
        tk.Button(top, text="Read TP1", width=12,
                  command=self.read_tp1).grid(row=0, column=2, padx=(8, 4), sticky="w")
        tk.Button(top, text="Set TP1", width=12,
                  command=self.set_tp1).grid(row=0, column=3, padx=4, sticky="w")

        tk.Label(top,
                 text="Uses Host/Ctrl Port from the GPS Comms tab.  Only TP1 is "
                      "exposed; values apply to both locked and unlocked.",
                 font=("Arial", 8), fg="gray").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # ---- Settings ------------------------------------------------ #
        cfg = tk.LabelFrame(parent, text="TP1 Settings", padx=10, pady=8)
        cfg.pack(fill="x", padx=10, pady=4)

        # Output type / value
        tk.Label(cfg, text="Output as:").grid(row=0, column=0, sticky="w")
        self.pps_mode_var = tk.StringVar(value="Frequency (Hz)")
        ttk.Combobox(cfg, textvariable=self.pps_mode_var,
                     values=["Frequency (Hz)", "Period (µs)"], state="readonly", width=14
                     ).grid(row=0, column=1, padx=(4, 14), sticky="w")
        self.pps_value_label = tk.StringVar(value="Freq (Hz):")
        tk.Label(cfg, textvariable=self.pps_value_label).grid(row=0, column=2, sticky="w")
        self.pps_value_entry = tk.Entry(cfg, width=12)
        self.pps_value_entry.grid(row=0, column=3, padx=(4, 0), sticky="w")
        self.pps_mode_var.trace_add("write", self._pps_mode_changed)

        # Pulse width: length (µs) or duty cycle (%)
        tk.Label(cfg, text="Pulse as:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.pps_pulse_mode_var = tk.StringVar(value="Length (µs)")
        ttk.Combobox(cfg, textvariable=self.pps_pulse_mode_var,
                     values=["Length (µs)", "Duty cycle (%)"], state="readonly", width=14
                     ).grid(row=1, column=1, padx=(4, 14), sticky="w", pady=(8, 0))
        self.pps_pulse_label = tk.StringVar(value="Length (µs):")
        tk.Label(cfg, textvariable=self.pps_pulse_label).grid(row=1, column=2, sticky="w", pady=(8, 0))
        self.pps_pulse_entry = tk.Entry(cfg, width=12)
        self.pps_pulse_entry.grid(row=1, column=3, padx=(4, 0), sticky="w", pady=(8, 0))
        self.pps_pulse_mode_var.trace_add("write", self._pps_pulse_mode_changed)

        # Antenna cable delay
        tk.Label(cfg, text="Ant cable delay (ns):").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.pps_cable_entry = tk.Entry(cfg, width=12)
        self.pps_cable_entry.grid(row=2, column=1, padx=(4, 14), sticky="w", pady=(8, 0))

        # Boolean flags
        flags = tk.Frame(cfg)
        flags.grid(row=3, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.pps_enable_var   = tk.BooleanVar(value=True)
        self.pps_pol_var      = tk.BooleanVar(value=True)
        self.pps_aligntow_var = tk.BooleanVar(value=True)
        self.pps_lockgps_var  = tk.BooleanVar(value=True)
        self.pps_uselock_var  = tk.BooleanVar(value=True)
        tk.Checkbutton(flags, text="Enable output", variable=self.pps_enable_var
                       ).grid(row=0, column=0, sticky="w", padx=(0, 12))
        tk.Checkbutton(flags, text="Polarity: rising edge", variable=self.pps_pol_var
                       ).grid(row=0, column=1, sticky="w", padx=(0, 12))
        tk.Checkbutton(flags, text="Align to TOW", variable=self.pps_aligntow_var
                       ).grid(row=0, column=2, sticky="w", padx=(0, 12))
        tk.Checkbutton(flags, text="Lock to GPS", variable=self.pps_lockgps_var
                       ).grid(row=1, column=0, sticky="w", padx=(0, 12), pady=(2, 0))
        tk.Checkbutton(flags, text="Use locked values", variable=self.pps_uselock_var
                       ).grid(row=1, column=1, sticky="w", padx=(0, 12), pady=(2, 0))

        # Read-only report line (raw unlocked / locked values from the receiver)
        self.pps_report_var = tk.StringVar(value="Receiver reports: —")
        tk.Label(parent, textvariable=self.pps_report_var,
                 font=("Arial", 8), fg="#555").pack(anchor="w", padx=14, pady=(2, 0))

    def _pps_mode_changed(self, *_):
        is_freq = self.pps_mode_var.get().startswith("Freq")
        self.pps_value_label.set("Freq (Hz):" if is_freq else "Period (µs):")

    def _pps_pulse_mode_changed(self, *_):
        is_length = self.pps_pulse_mode_var.get().startswith("Length")
        self.pps_pulse_label.set("Length (µs):" if is_length else "Duty (%):")

    # ------------------------------------------------------------------ #
    #  TP1 read / set
    # ------------------------------------------------------------------ #

    def _pps_target_id(self):
        return DEVICE_IDS.get(self.pps_device_var.get(), 0)

    def read_tp1(self):
        self._set_status("Reading TP1 config…", "#2980b9")
        threading.Thread(target=self._fetch_tp1, daemon=True).start()

    def _fetch_tp1(self):
        try:
            host, port = self._get_host_port()
            target_id  = self._pps_target_id()
            msg = (209, GUI_ID, target_id, 0, 0, 0, 0, 0, 0, 0)
            with socket.socket() as s:
                s.settimeout(20)
                s.connect((host, port))
                s.sendall(struct.pack(PACKET_FORMAT, *msg))
                data = self._recv_exact(s, PACKET_SIZE)
            if len(data) < PACKET_SIZE:
                raise ValueError(f"Short packet ({len(data)} bytes)")
            # inst, id, RF=flags, Cal, ch, w_num=val_un, ms=val_lock,
            #   sub_ms=pw_un, event_num=pw_lock, count=cable
            _, _, flags, _, _, val_un, val_lock, pw_un, pw_lock, cable = \
                struct.unpack(PACKET_FORMAT, data)

            def update():
                is_freq   = bool(flags & TP1_F_ISFREQ)
                is_length = bool(flags & TP1_F_ISLENGTH)
                self.pps_mode_var.set("Frequency (Hz)" if is_freq else "Period (µs)")
                self.pps_pulse_mode_var.set("Length (µs)" if is_length else "Duty cycle (%)")
                self.pps_enable_var.set(bool(flags & TP1_F_ENABLE))
                self.pps_pol_var.set(bool(flags & TP1_F_POL))
                self.pps_aligntow_var.set(bool(flags & TP1_F_ALIGNTOW))
                self.pps_lockgps_var.set(bool(flags & TP1_F_LOCKGPS))
                self.pps_uselock_var.set(bool(flags & TP1_F_USELOCK))
                # Editable fields show the locked (operational) values
                self._clear_entries(self.pps_value_entry, self.pps_pulse_entry, self.pps_cable_entry)
                self.pps_value_entry.insert(0, str(val_lock))
                # Pulse field: length is a raw int µs; duty is scaled back to %
                if is_length:
                    self.pps_pulse_entry.insert(0, str(pw_lock))
                    pw_un_disp, pw_lock_disp, pw_unit = pw_un, pw_lock, "µs"
                else:
                    self.pps_pulse_entry.insert(0, f"{pw_lock / TP1_DUTY_SCALE:g}")
                    pw_un_disp = f"{pw_un / TP1_DUTY_SCALE:g}"
                    pw_lock_disp = f"{pw_lock / TP1_DUTY_SCALE:g}"
                    pw_unit = "%"
                self.pps_cable_entry.insert(0, str(cable))
                unit = "Hz" if is_freq else "µs"
                self.pps_report_var.set(
                    f"Receiver reports: value {val_un}/{val_lock} {unit} (unlocked/locked), "
                    f"pulse {pw_un_disp}/{pw_lock_disp} {pw_unit}, cable {cable} ns, "
                    f"{'enabled' if flags & TP1_F_ENABLE else 'disabled'}")
                self._set_status("TP1 config received", "#27ae60")

            self.root.after(0, update)
        except Exception as e:
            m = f"Read TP1 error: {e}"
            self.root.after(0, lambda: self._set_status(m, "#e74c3c"))

    def set_tp1(self):
        is_length = self.pps_pulse_mode_var.get().startswith("Length")
        try:
            value = int(self.pps_value_entry.get())
            cable = int(self.pps_cable_entry.get() or "0")
            if is_length:
                pulse_width = int(self.pps_pulse_entry.get())
                if pulse_width < 0:
                    raise ValueError
            else:
                duty_pct = float(self.pps_pulse_entry.get())
                if not 0.0 <= duty_pct <= 100.0:
                    raise ValueError
                pulse_width = int(round(duty_pct * TP1_DUTY_SCALE))
            if value <= 0:
                raise ValueError
        except ValueError:
            self._set_status(
                "Bad TP1 values — value > 0, length ≥ 0 (µs) or duty 0–100 (%), cable an integer",
                "#e74c3c")
            return

        flags = 0
        if self.pps_mode_var.get().startswith("Freq"): flags |= TP1_F_ISFREQ
        if is_length:                   flags |= TP1_F_ISLENGTH
        if self.pps_enable_var.get():   flags |= TP1_F_ENABLE
        if self.pps_pol_var.get():      flags |= TP1_F_POL
        if self.pps_aligntow_var.get(): flags |= TP1_F_ALIGNTOW
        if self.pps_lockgps_var.get():  flags |= TP1_F_LOCKGPS
        if self.pps_uselock_var.get():  flags |= TP1_F_USELOCK

        self._set_status("Setting TP1 config…", "#2980b9")
        threading.Thread(target=self._send_tp1,
                         args=(value, pulse_width, flags, cable), daemon=True).start()

    def _send_tp1(self, value, pulse_width, flags, cable):
        try:
            host, port = self._get_host_port()
            target_id  = self._pps_target_id()
            # inst=210, w_num=value, ms=pulse_width, sub_ms=flags, event_num=cable
            msg = (210, GUI_ID, target_id, 0, 0, value, pulse_width, flags, cable, 0)
            with socket.socket() as s:
                s.settimeout(5)
                s.connect((host, port))
                s.sendall(struct.pack(PACKET_FORMAT, *msg))
            self.root.after(0, lambda: self._set_status(
                f"TP1 set on ESP {target_id}  (value {value}, pulse {pulse_width}, flags 0x{flags:02x})",
                "#27ae60"))
        except Exception as e:
            m = f"Set TP1 error: {e}"
            self.root.after(0, lambda: self._set_status(m, "#e74c3c"))

    # ================================================================== #
    #  Tab 6 – OTA (instructions + interactive terminal)
    # ================================================================== #

    def _build_ota_tab(self, parent):
        # ---- Instructions placeholder (fill in later) ---------------- #
        instr = tk.LabelFrame(parent, text="Instructions", padx=10, pady=8)
        instr.pack(fill="x", padx=10, pady=(10, 4))

        self.ota_instructions = tk.Text(
            instr, height=6, wrap="word", font=("Arial", 9),
            bg="#f7f7f7", relief="flat",
        )
        self.ota_instructions.insert(
            "1.0",
            "OTA instructions go here.\n"
            "(Reserved space — written guidance on how to push OTA updates "
            "will be added later.)",
        )
        self.ota_instructions.config(state="disabled")
        self.ota_instructions.pack(fill="x")

        # ---- Interactive terminal ------------------------------------ #
        term = tk.LabelFrame(parent, text="OTA Terminal", padx=5, pady=5)
        term.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.ota_text = scrolledtext.ScrolledText(
            term, state="disabled",
            font=("Courier", 9), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="#d4d4d4",
        )
        self.ota_text.pack(fill="both", expand=True)

        # Command input row
        entry_row = tk.Frame(term)
        entry_row.pack(fill="x", pady=(5, 0))

        tk.Label(entry_row, text=">").pack(side="left")
        self.ota_entry = tk.Entry(entry_row, font=("Courier", 9))
        self.ota_entry.pack(side="left", fill="x", expand=True, padx=(4, 6))
        self.ota_entry.bind("<Return>", self._send_ota_command)

        tk.Button(entry_row, text="Send", width=8,
                  command=self._send_ota_command).pack(side="left", padx=(0, 4))
        tk.Button(entry_row, text="Clear", width=8,
                  command=self.clear_ota_log).pack(side="left")

        self._start_ota_shell()

    def _start_ota_shell(self):
        """Spawn the persistent shell that backs the OTA terminal."""
        if self.ota_process is not None:
            return
        try:
            proc = subprocess.Popen(
                OTA_SHELL,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                cwd=SERVER_CWD,
            )
        except Exception as e:
            self._append_ota(f"[GUI ERROR] Failed to start shell: {e}\n")
            return
        self.ota_process = proc
        self._append_ota(f"[GUI] OTA terminal ready  ({OTA_SHELL[0]})\n")
        threading.Thread(target=self._ota_stream, args=(proc,), daemon=True).start()

    def _send_ota_command(self, event=None):
        cmd = self.ota_entry.get()
        if self.ota_process is None:
            self._append_ota("[GUI] Shell not running — restarting…\n")
            self._start_ota_shell()
            if self.ota_process is None:
                return
        self.ota_entry.delete(0, tk.END)
        self._append_ota(f"> {cmd}\n")        # echo (piped stdin isn't echoed)
        try:
            self.ota_process.stdin.write(cmd + "\n")
            self.ota_process.stdin.flush()
        except Exception as e:
            self._append_ota(f"[GUI ERROR] Could not send command: {e}\n")
        return "break"   # stop Tk inserting a newline into the Entry

    def _ota_stream(self, proc):
        try:
            for line in proc.stdout:
                self._append_ota(line)
        except Exception:
            pass
        self.root.after(0, lambda: self._on_ota_exit(proc))

    def _on_ota_exit(self, proc):
        if self.ota_process is not proc:
            return
        self.ota_process = None
        self._append_ota("[GUI] OTA shell exited\n")

    def clear_ota_log(self):
        self.ota_text.config(state="normal")
        self.ota_text.delete("1.0", tk.END)
        self.ota_text.config(state="disabled")

    def _append_ota(self, text):
        def _update():
            self.ota_text.config(state="normal")
            self.ota_text.insert(tk.END, text)
            self.ota_text.see(tk.END)
            self.ota_text.config(state="disabled")
        self.root.after(0, _update)

    # ================================================================== #
    #  Shared helpers
    # ================================================================== #

    def _set_status(self, text, color="#555555"):
        self.status_var.set(text)
        self.status_bar.config(bg=color)

    @staticmethod
    def _clear_entries(*entries):
        """Blank out the given tk.Entry widgets (called after a send so a second
        button press doesn't re-fire stale values)."""
        for e in entries:
            e.delete(0, tk.END)

    def _append_log(self, text):
        def _update():
            self.log_text.config(state="normal")
            self.log_text.insert(tk.END, text)
            self.log_text.see(tk.END)
            self.log_text.config(state="disabled")
        self.root.after(0, _update)

    def on_close(self):
        self.stop_server()
        self.autosurvey_stop.set()
        if self.ota_process is not None:
            proc, self.ota_process = self.ota_process, None
            try:
                proc.terminate()
            except Exception:
                pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = GPSDAQApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
