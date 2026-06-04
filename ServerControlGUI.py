import tkinter as tk
from tkinter import ttk, scrolledtext
import subprocess
import threading
import sys
import os
import socket
import struct

SERVER_SCRIPT = r"C:\\Users\\alexc\\OneDrive\\Desktop\\Claude_GUI_prac\\GPS_DAQ_Server.py"
SERVER_CWD = os.path.dirname(SERVER_SCRIPT)

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


class GPSDAQApp:
    def __init__(self, root):
        self.root = root
        self.root.title("GPS DAQ Control")
        self.root.geometry("780x560")
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

        self.notebook.add(server_tab, text="  Server Control  ")
        self.notebook.add(gps_tab,    text="  GPS  ")

        self._build_server_tab(server_tab)
        self._build_gps_tab(gps_tab)

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

        tk.Label(ctrl, text=f"Script: {SERVER_SCRIPT}",
                 font=("Arial", 8), fg="gray").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(5, 0))

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

        # ---- Device selector ----------------------------------------- #
        dev = tk.LabelFrame(parent, text="Device", padx=10, pady=6)
        dev.pack(fill="x", padx=10, pady=4)

        tk.Label(dev, text="Select Device:").grid(row=0, column=0, sticky="w")
        self.device_var = tk.StringVar(value=DEVICES[0])
        ttk.Combobox(dev, textvariable=self.device_var,
                     values=DEVICES, state="readonly", width=10
                     ).grid(row=0, column=1, padx=8, sticky="w")

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
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", SERVER_SCRIPT],
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
                s.settimeout(10)   # ESP needs time to poll UBX and reply
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
                s.settimeout(10)   # ESP needs time to poll UBX and reply
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

    # ================================================================== #
    #  Shared helpers
    # ================================================================== #

    def _set_status(self, text, color="#555555"):
        self.status_var.set(text)
        self.status_bar.config(bg=color)

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
