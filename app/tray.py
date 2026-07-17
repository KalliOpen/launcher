from __future__ import annotations

import logging
import os
import signal
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3, GLib, Gtk

APP_ID = "io.kalliopen.Launcher"
logger = logging.getLogger(__name__)


def handle_appindicator_warning(log_domain, log_level, message, _user_data=None) -> None:
    if message.startswith("libayatana-appindicator is deprecated."):
        return
    GLib.log_default_handler(log_domain, log_level, message, None)


def signal_parent(parent_pid: int, requested_signal: signal.Signals) -> bool:
    try:
        os.kill(parent_pid, requested_signal)
    except ProcessLookupError:
        Gtk.main_quit()
        return False
    except PermissionError:
        logger.exception("Could not signal KalliOpen Launcher process %s", parent_pid)
        return False
    return True


def check_parent(parent_pid: int) -> bool:
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        Gtk.main_quit()
        return False
    except PermissionError:
        pass
    return True


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        logger.error("Usage: python -m app.tray PARENT_PID")
        return 2

    try:
        parent_pid = int(arguments[0])
    except ValueError:
        logger.error("Invalid parent process ID: %s", arguments[0])
        return 2

    GLib.log_set_handler(
        "libayatana-appindicator",
        GLib.LogLevelFlags.LEVEL_WARNING,
        handle_appindicator_warning,
    )
    indicator = AyatanaAppIndicator3.Indicator.new(
        APP_ID,
        APP_ID,
        AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
    )
    indicator.set_title("KalliOpen Launcher")
    indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)

    menu = Gtk.Menu()
    open_item = Gtk.MenuItem(label="Open KalliOpen Launcher")
    quit_item = Gtk.MenuItem(label="Quit")

    def open_launcher(*_):
        signal_parent(parent_pid, signal.SIGUSR1)

    def quit_launcher(*_):
        signal_parent(parent_pid, signal.SIGUSR2)
        Gtk.main_quit()

    open_item.connect("activate", open_launcher)
    quit_item.connect("activate", quit_launcher)
    menu.append(open_item)
    menu.append(Gtk.SeparatorMenuItem())
    menu.append(quit_item)
    menu.show_all()
    indicator.set_menu(menu)

    GLib.timeout_add_seconds(2, check_parent, parent_pid)
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
