from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GLibUnix", "2.0")
from gi.repository import Gio, GLib, GLibUnix, Gtk

from .window import Window

logger = logging.getLogger(__name__)


def add_unix_signal(signum: signal.Signals, callback) -> int:
    modern_signal_add = getattr(GLibUnix, "signal_add", None)
    if modern_signal_add is not None:
        return modern_signal_add(GLib.PRIORITY_DEFAULT, signum, callback)
    return GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, callback)


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.kalliopen.Launcher", flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.window: Window | None = None
        self.tray_process: subprocess.Popen | None = None
        self.tray_check_source: int | None = None
        self._held = False
        self._quitting = False

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()
        self._held = True
        add_unix_signal(signal.SIGUSR1, self.restore_window)
        add_unix_signal(signal.SIGUSR2, self.quit_from_tray)

    def do_activate(self):
        if self.window is None:
            self.window = Window(self)
        self.start_tray()
        self.window.present()

    def start_tray(self) -> bool:
        if self.tray_process and self.tray_process.poll() is None:
            return True

        if self.tray_check_source is not None:
            GLib.source_remove(self.tray_check_source)
            self.tray_check_source = None

        try:
            self.tray_process = subprocess.Popen(
                [sys.executable, "-m", "app.tray", str(os.getpid())],
                start_new_session=True,
            )
        except OSError:
            logger.exception("Could not start the system tray helper")
            self.tray_process = None
            return False

        logger.info("System tray helper started with PID %s", self.tray_process.pid)
        self.tray_check_source = GLib.timeout_add_seconds(1, self.check_tray_process)
        return True

    def check_tray_process(self) -> bool:
        process = self.tray_process
        if process and process.poll() is None:
            return True

        self.tray_process = None
        self.tray_check_source = None
        if not self._quitting:
            exit_code = process.returncode if process else "unknown"
            logger.warning("System tray helper exited with status %s", exit_code)
            if self.window and not self.window.get_visible():
                logger.info("Restoring the launcher because the tray helper is unavailable")
                self.window.present()
        return False

    def hide_window(self, window: Gtk.Window) -> bool:
        if not self.start_tray():
            logger.warning("System tray is unavailable; minimizing the launcher")
            window.minimize()
            return True

        logger.info("Main window closed; hiding it in the system tray")
        window.hide()
        return True

    def restore_window(self) -> bool:
        logger.info("Restoring the launcher from the system tray")
        if self.window:
            self.window.present()
        return True

    def quit_from_tray(self) -> bool:
        logger.info("Quit selected from the system tray")
        self.quit_launcher()
        return False

    def quit_launcher(self):
        if self._quitting:
            return
        self._quitting = True
        self.stop_tray()
        if self._held:
            self.release()
            self._held = False
        self.quit()

    def stop_tray(self):
        if self.tray_check_source is not None:
            GLib.source_remove(self.tray_check_source)
            self.tray_check_source = None

        process = self.tray_process
        self.tray_process = None
        if not process or process.poll() is not None:
            return

        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    def do_shutdown(self):
        self._quitting = True
        self.stop_tray()
        if self._held:
            self.release()
            self._held = False
        Gtk.Application.do_shutdown(self)
