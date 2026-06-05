import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import subprocess
import threading
import sys
import os
import re
import socket
import struct
from datetime import datetime

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

SERVER_SCRIPT = r"C:\\Users\\alexc\\OneDrive\\Desktop\\Claude_GUI_prac\\GPS_DAQ_Server.py"
SERVER_CWD = os.path.dirname(SERVER_SCRIPT)
GPS_DATA_DIR = os.path.join(SERVER_CWD, "GPS Data")

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
    "Det 1": 232,   # placeholder – fill in actual mac_id
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

# gps_cpu_log.txt columns:
# 0 ts 1 ID 2 cpuLoad 3 cpuLoadMax 4 memUsage 5 memUsageMax
# 6 ioUsage 7 ioUsageMax 8 runTime 9 temp 10 notice 11 warn 12 error
PLOT_METRICS = {"CPU load (%)": 2, "Mem usage (%)": 4, "IO usage (%)": 6, "Temp (°C)": 9}


class GPSDAQApp:
    def __init__(self, root):
        self.root = root
        self.root.title("KoForce GUI")
        self.root.geometry("820x720")
        self.root.resizable(True, True)

        self.server_process = None

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

        self.notebook.add(server_tab, text="  Server Control  ")
        self.notebook.add(gps_tab,    text="  GPS Comms ")
        self.notebook.add(cpu_tab,    text="  GPS Slow Control  ")

        self._build_server_tab(server_tab)
        self._build_gps_tab(gps_tab)
        self._build_slowctrl_tab(cpu_tab)

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

        # Small status indicators: valid / active / duration
        self.svin_status_var = tk.StringVar(value="—")
        tk.Label(surv, textvariable=self.svin_status_var,
                 font=("Arial", 8), fg="#555").grid(
            row=3, column=0, columnspan=6, sticky="w", pady=(2, 0))

        # ---- Fixed Position ------------------------------------------ #
        fix = tk.LabelFrame(parent, text="Fixed Position  (CFG-TMODE3)", padx=10, pady=8)
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
            # inst=12, id=GUI_ID, RF=target ESP mac_id, w_num=min_dur, ms=acc_01mm
            msg = (12, GUI_ID, target_id, 0, 0, min_dur, acc_01mm, 0, 0, 0)
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
            # inst=11, id=GUI_ID, RF=target ESP mac_id
            msg = (11, GUI_ID, target_id, 0, 0, 0, 0, 0, 0, 0)
            with socket.socket() as s:
                s.settimeout(20)   # ESP needs time to poll UBX and reply
                s.connect((host, port))
                s.sendall(struct.pack(PACKET_FORMAT, *msg))
                data = self._recv_exact(s, PACKET_SIZE)

            if len(data) < PACKET_SIZE:
                raise ValueError(f"Short packet ({len(data)} bytes)")

            # Response: inst=11, id, RF=valid, Cal=active, ch=dur_s,
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
            # inst=13, id=GUI_ID, RF=target, w_num=X, ms=Y, sub_ms=Z, event_num=Acc
            msg = (13, GUI_ID, target_id, 0, 0, x, y, z, acc, 0)
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
            # inst=14, id=GUI_ID, RF=target ESP mac_id
            msg = (14, GUI_ID, target_id, 0, 0, 0, 0, 0, 0, 0)
            with socket.socket() as s:
                s.settimeout(20)   # ESP needs time to poll UBX and reply
                s.connect((host, port))
                s.sendall(struct.pack(PACKET_FORMAT, *msg))
                data = self._recv_exact(s, PACKET_SIZE)

            if len(data) < PACKET_SIZE:
                raise ValueError(f"Short packet ({len(data)} bytes)")

            # Response: inst=14, id, RF=mode, Cal=is_lla, ch,
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
            # inst=15, RF=target, w_num=navBbrMask, ms=resetMode (0x01 = controlled SW reset)
            msg = (15, GUI_ID, target_id, 0, 0, mask, 0x01, 0, 0, 0)
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
            msg = (16, GUI_ID, target_id, 0, 0, 0, 0, 0, 0, 0)
            with socket.socket() as s:
                s.settimeout(20)
                s.connect((host, port))
                s.sendall(struct.pack(PACKET_FORMAT, *msg))
                data = self._recv_exact(s, PACKET_SIZE)

            if len(data) < PACKET_SIZE:
                raise ValueError(f"Short packet ({len(data)} bytes)")

            # Response: inst=16, id, RF=timeref, Cal, ch, w_num=meas_ms, ms=nav, ...
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
            # inst=17, RF=target, w_num=meas_ms, ms=nav, sub_ms=timeref (<=0 / <0 = skip on ESP)
            msg = (17, GUI_ID, target_id, 0, 0, meas, nav, timeref, 0, 0)
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
            msg = (18, GUI_ID, target_id, 0, 0, 0, 0, 0, 0, 0)
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
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = GPSDAQApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
