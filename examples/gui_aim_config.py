"""GUI for Mouse/Keyboard → PS5 with real-time aim adjustment.

A tkinter GUI that lets you adjust HIP and ADS sensitivity, curve,
smoothing, and deadzone in real-time while playing on PS5.

Features:
- Separate HIP (normal) and ADS (aiming down sights) sensitivity
- Auto-switch to ADS settings when right-click is held
- Real-time sliders - change sensitivity without restarting
- Curve preset dropdown
- Smoothing and deadzone controls
- Connection status indicator
- Stats display (events/sec)

Usage:
    python gui_aim_config.py --host 192.168.100.5

Requirements:
    - Windows 7+ (tkinter included with Python)
    - Python 3.8+
    - pyremoteplay registered with PS5
"""

import asyncio
import threading
import logging
import tkinter as tk
from tkinter import ttk, messagebox

from pyremoteplay.session import Session
from pyremoteplay.controller import Controller
from pyremoteplay.profile import Profiles
from pyremoteplay.wininput.mouse_translator import MouseTranslator, TranslatorConfig
from pyremoteplay.wininput.aim_curves import AimCurve, list_presets
from pyremoteplay.wininput.rawinput import (
    RI_MOUSE_RIGHT_BUTTON_DOWN, RI_MOUSE_RIGHT_BUTTON_UP,
)

logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)


class AimConfigGUI:
    """GUI for real-time aim configuration."""

    def __init__(self, host: str):
        self._host = host
        self._session = None
        self._controller = None
        self._translator = None
        self._running = False
        self._ads_active = False

        self._hip_sens = 15.0
        self._ads_sens = 8.0
        self._curve_preset = "ballistic"
        self._smoothing = 0.0
        self._deadzone = 0.02
        self._invert_y = False

        self._root = tk.Tk()
        self._root.title("PyRemotePlay - Aim Config (XIM Style)")
        self._root.geometry("500x700")
        self._root.resizable(False, False)
        self._build_ui()

    def _build_ui(self):
        root = self._root

        conn_frame = ttk.LabelFrame(root, text="Connection", padding=10)
        conn_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(conn_frame, text=f"Host: {self._host}").pack(side="left")
        self._status_label = ttk.Label(conn_frame, text="DISCONNECTED", foreground="red")
        self._status_label.pack(side="right")
        self._connect_btn = ttk.Button(conn_frame, text="Connect", command=self._on_connect)
        self._connect_btn.pack(side="right", padx=10)

        hip_frame = ttk.LabelFrame(root, text="HIP Fire (Normal)", padding=10)
        hip_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(hip_frame, text="Sensitivity:").pack(anchor="w")
        self._hip_sens_var = tk.DoubleVar(value=self._hip_sens)
        ttk.Scale(hip_frame, from_=1, to=50, variable=self._hip_sens_var, orient="horizontal", command=self._on_hip_change).pack(fill="x")
        self._hip_sens_label = ttk.Label(hip_frame, text=f"{self._hip_sens:.1f}")
        self._hip_sens_label.pack(anchor="e")

        ads_frame = ttk.LabelFrame(root, text="ADS (Right Click Held)", padding=10)
        ads_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(ads_frame, text="Sensitivity:").pack(anchor="w")
        self._ads_sens_var = tk.DoubleVar(value=self._ads_sens)
        ttk.Scale(ads_frame, from_=1, to=50, variable=self._ads_sens_var, orient="horizontal", command=self._on_ads_change).pack(fill="x")
        self._ads_sens_label = ttk.Label(ads_frame, text=f"{self._ads_sens:.1f}")
        self._ads_sens_label.pack(anchor="e")
        self._ads_indicator = ttk.Label(ads_frame, text="", foreground="blue")
        self._ads_indicator.pack(anchor="w")

        curve_frame = ttk.LabelFrame(root, text="Aim Curve", padding=10)
        curve_frame.pack(fill="x", padx=10, pady=5)
        presets = list(list_presets().keys())
        self._curve_var = tk.StringVar(value=self._curve_preset)
        ttk.Label(curve_frame, text="Preset:").pack(side="left")
        self._curve_combo = ttk.Combobox(curve_frame, textvariable=self._curve_var, values=presets, state="readonly", width=15)
        self._curve_combo.pack(side="left", padx=10)
        self._curve_combo.bind("<<ComboboxSelected>>", self._on_curve_change)
        self._curve_desc = ttk.Label(curve_frame, text="", wraplength=300)
        self._curve_desc.pack(anchor="w", pady=5)
        self._update_curve_desc()

        smooth_frame = ttk.LabelFrame(root, text="Smoothing & Deadzone", padding=10)
        smooth_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(smooth_frame, text="Smoothing (0=raw, 0.9=smooth):").pack(anchor="w")
        self._smooth_var = tk.DoubleVar(value=self._smoothing)
        ttk.Scale(smooth_frame, from_=0, to=0.9, variable=self._smooth_var, orient="horizontal", command=self._on_smooth_change).pack(fill="x")
        self._smooth_label = ttk.Label(smooth_frame, text=f"{self._smoothing:.2f}")
        self._smooth_label.pack(anchor="e")
        ttk.Label(smooth_frame, text="Deadzone:").pack(anchor="w")
        self._dz_var = tk.DoubleVar(value=self._deadzone)
        ttk.Scale(smooth_frame, from_=0, to=0.15, variable=self._dz_var, orient="horizontal", command=self._on_dz_change).pack(fill="x")
        self._dz_label = ttk.Label(smooth_frame, text=f"{self._deadzone:.3f}")
        self._dz_label.pack(anchor="e")

        opt_frame = ttk.LabelFrame(root, text="Options", padding=10)
        opt_frame.pack(fill="x", padx=10, pady=5)
        self._invert_var = tk.BooleanVar(value=self._invert_y)
        ttk.Checkbutton(opt_frame, text="Invert Y Axis", variable=self._invert_var, command=self._on_invert_change).pack(anchor="w")

        stats_frame = ttk.LabelFrame(root, text="Stats", padding=10)
        stats_frame.pack(fill="x", padx=10, pady=5)
        self._stats_label = ttk.Label(stats_frame, text="Mouse: 0 | Keys: 0 | Mode: HIP")
        self._stats_label.pack(anchor="w")

        self._update_stats()

    def _on_connect(self):
        if self._running:
            self._disconnect()
        else:
            self._connect_btn.config(state="disabled")
            threading.Thread(target=self._connect_async, daemon=True).start()

    def _connect_async(self):
        asyncio.run(self._do_connect())

    async def _do_connect(self):
        try:
            profiles = Profiles.load("")
            if not profiles:
                self._root.after(0, lambda: messagebox.showerror("Error", "No profile found"))
                self._root.after(0, lambda: self._connect_btn.config(state="normal"))
                return

            profile_name = list(profiles.keys())[0]
            profile = profiles[profile_name]

            self._session = Session(host=self._host, profile=profile, controller_only=True)
            self._controller = Controller()
            self._controller.connect(self._session)

            success = await self._session.start()
            if not success:
                self._root.after(0, lambda: messagebox.showerror("Error", f"Failed: {self._session.error}"))
                self._root.after(0, lambda: self._connect_btn.config(state="normal"))
                return

            ready = await self._session.async_wait(timeout=15)
            if not ready:
                self._root.after(0, lambda: messagebox.showerror("Error", "Session not ready"))
                self._root.after(0, lambda: self._connect_btn.config(state="normal"))
                return

            self._controller.start()

            config = TranslatorConfig(
                sensitivity_x=self._hip_sens, sensitivity_y=self._hip_sens,
                curve_preset=self._curve_preset, smoothing=self._smoothing,
                stick_deadzone=self._deadzone, invert_y=self._invert_y,
            )
            self._translator = MouseTranslator(config=config)

            original_handler = self._translator._process_mouse_buttons
            def _custom_mouse_buttons(flags):
                if flags & RI_MOUSE_RIGHT_BUTTON_DOWN:
                    self._ads_active = True
                    self._apply_sensitivity()
                if flags & RI_MOUSE_RIGHT_BUTTON_UP:
                    self._ads_active = False
                    self._apply_sensitivity()
                original_handler(flags)
            self._translator._process_mouse_buttons = _custom_mouse_buttons

            self._translator.connect(self._controller)
            self._translator.start()
            self._running = True
            self._root.after(0, self._on_connected)

        except Exception as e:
            self._root.after(0, lambda: messagebox.showerror("Error", str(e)))
            self._root.after(0, lambda: self._connect_btn.config(state="normal"))

    def _on_connected(self):
        self._status_label.config(text="CONNECTED", foreground="green")
        self._connect_btn.config(text="Disconnect", state="normal")

    def _disconnect(self):
        self._running = False
        if self._translator:
            self._translator.stop()
            self._translator = None
        if self._controller:
            self._controller.disconnect()
            self._controller = None
        if self._session:
            self._session.stop()
            self._session = None
        self._status_label.config(text="DISCONNECTED", foreground="red")
        self._connect_btn.config(text="Connect")

    def _apply_sensitivity(self):
        if not self._translator:
            return
        if self._ads_active:
            self._translator._config.sensitivity_x = self._ads_sens
            self._translator._config.sensitivity_y = self._ads_sens
        else:
            self._translator._config.sensitivity_x = self._hip_sens
            self._translator._config.sensitivity_y = self._hip_sens

    def _on_hip_change(self, val):
        self._hip_sens = float(val)
        self._hip_sens_label.config(text=f"{self._hip_sens:.1f}")
        if not self._ads_active:
            self._apply_sensitivity()

    def _on_ads_change(self, val):
        self._ads_sens = float(val)
        self._ads_sens_label.config(text=f"{self._ads_sens:.1f}")
        if self._ads_active:
            self._apply_sensitivity()

    def _on_curve_change(self, event=None):
        self._curve_preset = self._curve_var.get()
        self._update_curve_desc()
        if self._translator:
            self._translator.curve_preset = self._curve_preset

    def _on_smooth_change(self, val):
        self._smoothing = float(val)
        self._smooth_label.config(text=f"{self._smoothing:.2f}")
        if self._translator:
            self._translator._config.smoothing = self._smoothing

    def _on_dz_change(self, val):
        self._deadzone = float(val)
        self._dz_label.config(text=f"{self._deadzone:.3f}")
        if self._translator:
            self._translator._config.stick_deadzone = self._deadzone

    def _on_invert_change(self):
        self._invert_y = self._invert_var.get()
        if self._translator:
            self._translator._config.invert_y = self._invert_y

    def _update_curve_desc(self):
        presets = list_presets()
        desc = presets.get(self._curve_preset, "")
        self._curve_desc.config(text=desc)

    def _update_stats(self):
        if self._translator:
            mode = "ADS" if self._ads_active else "HIP"
            sens = self._ads_sens if self._ads_active else self._hip_sens
            self._stats_label.config(
                text=f"Mouse: {self._translator.total_mouse_events} | "
                     f"Keys: {self._translator.total_key_events} | "
                     f"Mode: {mode} (sens={sens:.1f})"
            )
            self._ads_indicator.config(text=">>> ADS ACTIVE <<<" if self._ads_active else "")
        self._root.after(200, self._update_stats)

    def run(self):
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._root.mainloop()

    def _on_close(self):
        self._disconnect()
        self._root.destroy()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="GUI Aim Config for PS5")
    parser.add_argument("--host", "-H", required=True, help="PS5 IP address")
    args = parser.parse_args()
    app = AimConfigGUI(args.host)
    app.run()
