from __future__ import annotations

import logging
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

from .. import backups, config, docker
from ..migration.dialog import MigrationDialog

logger = logging.getLogger(__name__)
MANAGED_LIFECYCLE_VERSION = 1


class Window(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title="KalliOpen Launcher")
        self.connect("close-request", self.close_application)
        self.state = config.ensure_state()
        self.last_state = "stopped"
        self.has_been_healthy = bool(self.state.get("ever_healthy", False))
        self.transition: str | None = None
        self.autostart_pending = True
        self.background_action_count = 0
        self.automatic_backup_running = False
        self.automatic_backup_error: str | None = None
        self.last_automatic_backup_attempt: datetime | None = None
        self.initialize_service_state()
        self._install_css()
        self.status_dot = Gtk.Label(label="●")
        self.status_dot.add_css_class("status-dot")
        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("status-value")
        self.current_version = Gtk.Label(label="Not installed", xalign=0)
        self.latest_version = Gtk.Label(label="Checking...", xalign=0)
        self.action = Gtk.Button(label="Start")
        self.action.connect("clicked", self.toggle)
        self.open_button = Gtk.Button(label="Open KalliOpen")
        self.open_button.connect("clicked", self.open_kalliopen)
        migrate = Gtk.Button(label="Migration")
        migrate.connect("clicked", self.show_migration)
        export = Gtk.Button(label="Export database")
        export.connect("clicked", self.export_db)
        import_button = Gtk.Button(label="Import database")
        import_button.connect("clicked", self.import_db)
        delete_button = Gtk.Button(label="Delete database")
        delete_button.add_css_class("destructive-action")
        delete_button.connect("clicked", self.delete_db)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.body.set_margin_top(28); self.body.set_margin_bottom(28)
        self.body.set_margin_start(36); self.body.set_margin_end(36)
        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        status_row.set_halign(Gtk.Align.START)
        status_row.append(self.status_dot)
        status_row.append(self.status)
        self.body.append(status_row)
        info = Gtk.Grid(column_spacing=28, row_spacing=4)
        info.add_css_class("info-grid")
        info.set_hexpand(True)
        info.set_column_homogeneous(True)
        info.attach(Gtk.Label(label="Address", xalign=0, css_classes=["info-label"]), 0, 0, 1, 1)
        self.address = Gtk.Label(label=self.address_text(), xalign=0)
        info.attach(self.address, 0, 1, 1, 1)
        info.attach(Gtk.Label(label="Current version", xalign=0, css_classes=["info-label"]), 1, 0, 1, 1)
        info.attach(self.current_version, 1, 1, 1, 1)
        info.attach(Gtk.Label(label="Latest version", xalign=0, css_classes=["info-label"]), 2, 0, 1, 1)
        info.attach(self.latest_version, 2, 1, 1, 1)
        self.body.append(info)
        service_actions = Gtk.Grid(column_spacing=12)
        service_actions.set_hexpand(True)
        service_actions.attach(self.action, 0, 0, 1, 1)
        update = Gtk.Button(label="Update")
        update.connect("clicked", self.update_kalliopen)
        update.set_sensitive(False)
        self.update_button = update
        self.update_available = False
        service_actions.attach(update, 1, 0, 1, 1)
        service_actions.set_column_homogeneous(True)
        self.body.append(service_actions)
        self.body.append(self.open_button)
        self.body.append(Gtk.Separator())
        self.body.append(Gtk.Label(label="Database tools", xalign=0, css_classes=["section-label"]))
        database_tools = Gtk.Grid(column_spacing=12, row_spacing=12)
        database_tools.set_column_homogeneous(True)
        database_tools.attach(export, 0, 0, 1, 1)
        database_tools.attach(import_button, 1, 0, 1, 1)
        database_tools.attach(migrate, 0, 1, 1, 1)
        database_tools.attach(delete_button, 1, 1, 1, 1)
        self.database_buttons = (export, import_button, migrate, delete_button)
        self.body.append(database_tools)
        self.body.append(Gtk.Separator())
        self.body.append(self.build_automatic_backup_section())
        self.setup = Gtk.Button(label="Run setup")
        self.setup.connect("clicked", self.run_setup)
        self.update_docker_controls()
        self.body.append(self.setup)
        self.set_child(self.body)
        self.refresh()
        self.refresh_versions()
        self.refresh_backup_summary()
        GLib.timeout_add_seconds(5, self.refresh)
        GLib.timeout_add_seconds(2, self.autostart_managed_service)
        GLib.timeout_add_seconds(15, self.check_automatic_backup)

    def build_automatic_backup_section(self) -> Gtk.Widget:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title = Gtk.Label(label="Automatic backups", xalign=0, css_classes=["section-label"])
        title.set_hexpand(True)
        self.automatic_backup_switch = Gtk.Switch()
        self.automatic_backup_switch.set_active(bool(self.state["automatic_backups_enabled"]))
        self.automatic_backup_switch.set_valign(Gtk.Align.CENTER)
        header.append(title)
        header.append(self.automatic_backup_switch)
        section.append(header)

        settings = Gtk.Grid(column_spacing=8, row_spacing=8)
        self.automatic_backup_interval = Gtk.SpinButton.new_with_range(1, 10080, 1)
        self.automatic_backup_interval.set_value(self.state["automatic_backup_interval_minutes"])
        self.automatic_backup_interval.set_width_chars(5)
        self.automatic_backup_retention = Gtk.SpinButton.new_with_range(1, 3650, 1)
        self.automatic_backup_retention.set_value(self.state["automatic_backup_retention_days"])
        self.automatic_backup_retention.set_width_chars(5)
        settings.attach(Gtk.Label(label="Every", xalign=0), 0, 0, 1, 1)
        settings.attach(self.automatic_backup_interval, 1, 0, 1, 1)
        settings.attach(Gtk.Label(label="minutes,", xalign=0), 2, 0, 1, 1)
        settings.attach(Gtk.Label(label="keep for", xalign=0), 3, 0, 1, 1)
        settings.attach(self.automatic_backup_retention, 4, 0, 1, 1)
        settings.attach(Gtk.Label(label="days", xalign=0), 5, 0, 1, 1)
        settings.set_sensitive(self.automatic_backup_switch.get_active())
        self.automatic_backup_settings = settings
        section.append(settings)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.automatic_backup_summary = Gtk.Label(xalign=0)
        self.automatic_backup_summary.add_css_class("backup-summary")
        self.automatic_backup_summary.set_hexpand(True)
        self.automatic_backup_summary.set_wrap(True)
        open_folder = Gtk.LinkButton.new_with_label(
            config.BACKUP_DIR.as_uri(),
            "Open backup folder",
        )
        open_folder.add_css_class("backup-folder-link")
        open_folder.set_valign(Gtk.Align.CENTER)
        open_folder.connect("activate-link", self.open_backup_folder)
        footer.append(self.automatic_backup_summary)
        footer.append(open_folder)
        section.append(footer)

        self.automatic_backup_switch.connect("notify::active", self.automatic_backup_toggled)
        self.automatic_backup_interval.connect("value-changed", self.automatic_backup_values_changed)
        self.automatic_backup_retention.connect("value-changed", self.automatic_backup_values_changed)
        return section

    def automatic_backup_toggled(self, switch, _parameter) -> None:
        enabled = switch.get_active()
        logger.info("Automatic backups %s", "enabled" if enabled else "disabled")
        self.automatic_backup_settings.set_sensitive(enabled)
        self.automatic_backup_error = None
        if enabled:
            self.last_automatic_backup_attempt = None
        self.set_service_state(automatic_backups_enabled=enabled)
        self.refresh_backup_summary()

    def automatic_backup_values_changed(self, *_args) -> None:
        interval = self.automatic_backup_interval.get_value_as_int()
        retention = self.automatic_backup_retention.get_value_as_int()
        logger.info("Automatic backup settings changed: interval=%s minutes retention=%s days", interval, retention)
        self.set_service_state(
            automatic_backup_interval_minutes=interval,
            automatic_backup_retention_days=retention,
        )

    def open_backup_folder(self, *_args) -> bool:
        logger.info("Open backup folder button pressed")
        config.ensure_backup_directory()
        try:
            Gio.AppInfo.launch_default_for_uri(config.BACKUP_DIR.as_uri(), None)
        except GLib.Error as exc:
            self.show_background_error(str(exc))
        return True

    def refresh_backup_summary(self) -> None:
        current = backups.summary()
        latest = current.latest.strftime("%Y-%m-%d %H:%M") if current.latest else "Never"
        text = f"Saved backups: {current.count} | Last: {latest}"
        css_classes = ["backup-summary"]
        if self.automatic_backup_running:
            text += " | Creating backup..."
        elif self.automatic_backup_error:
            text += " | Last attempt failed"
            css_classes.append("backup-error")
            self.automatic_backup_summary.set_tooltip_text(self.automatic_backup_error)
        else:
            self.automatic_backup_summary.set_tooltip_text(None)
        self.automatic_backup_summary.set_text(text)
        self.automatic_backup_summary.set_css_classes(css_classes)
        for button in self.database_buttons:
            button.set_sensitive(not self.automatic_backup_running)
        self.update_button.set_sensitive(self.update_available and not self.automatic_backup_running)
        self.setup.set_sensitive(not self.automatic_backup_running)

    def check_automatic_backup(self) -> bool:
        self.refresh_backup_summary()
        if not self.state.get("automatic_backups_enabled", False):
            return True
        if self.automatic_backup_running or self.background_action_count or self.transition:
            return True
        if not self.state.get("setup_complete", False) or not docker.available():
            return True

        interval = int(self.state["automatic_backup_interval_minutes"])
        if not backups.due(interval, self.last_automatic_backup_attempt):
            return True

        retention = int(self.state["automatic_backup_retention_days"])
        self.last_automatic_backup_attempt = datetime.now().astimezone()
        self.automatic_backup_running = True
        self.automatic_backup_error = None
        self.refresh_backup_summary()
        logger.info("Starting scheduled database backup")

        def worker():
            try:
                destination = backups.create(retention)
            except Exception as exc:
                logger.exception("Scheduled database backup failed")
                GLib.idle_add(self.finish_automatic_backup, None, str(exc))
            else:
                logger.info("Scheduled database backup saved to %s", destination)
                GLib.idle_add(self.finish_automatic_backup, destination, None)

        threading.Thread(target=worker, daemon=True).start()
        return True

    def finish_automatic_backup(self, _destination, error) -> bool:
        self.automatic_backup_running = False
        self.automatic_backup_error = error
        self.refresh_backup_summary()
        return False

    @staticmethod
    def address_text() -> str:
        return docker.application_url().removeprefix("http://")

    def initialize_service_state(self) -> None:
        if not docker.available():
            return
        existing_install = docker.managed_install_exists()
        changed = False
        if existing_install and not self.state.get("setup_complete"):
            self.state["setup_complete"] = True
            changed = True
        if "desired_running" not in self.state:
            self.state["desired_running"] = existing_install and docker.app_container_running()
            changed = True
        if "managed_lifecycle_version" not in self.state:
            self.state["managed_lifecycle_version"] = 0 if existing_install else MANAGED_LIFECYCLE_VERSION
            changed = True
        if changed:
            config.save(self.state)

    def set_service_state(self, **changes) -> None:
        self.state.update(changes)
        config.save(self.state)

    def begin_managed_restart(self) -> None:
        self.autostart_pending = False
        self.set_service_state(desired_running=True)
        self.transition = "starting"
        self.refresh()

    def managed_service_started(self) -> None:
        self.set_service_state(
            setup_complete=True,
            desired_running=True,
            managed_lifecycle_version=MANAGED_LIFECYCLE_VERSION,
        )

    def managed_action_failed(self) -> None:
        self.transition = None

    def autostart_managed_service(self) -> bool:
        if not self.autostart_pending:
            return False
        if not docker.available():
            return True
        self.initialize_service_state()
        self.autostart_pending = False
        configured = self.state.get("setup_complete", False)
        desired = self.state.get("desired_running", False)
        lifecycle_current = self.state.get("managed_lifecycle_version") == MANAGED_LIFECYCLE_VERSION
        if configured and desired and (docker.health() != "running" or not lifecycle_current):
            logger.info("Starting configured KalliOpen service during launcher autostart")
            self.begin_managed_restart()
            self.run_async(
                docker.start,
                "KalliOpen started automatically",
                on_success=self.managed_service_started,
                on_failure=self.managed_action_failed,
            )
        return False

    def close_application(self, *_):
        application = self.get_application()
        if application and hasattr(application, "hide_window"):
            return application.hide_window(self)

        logger.warning("Tray integration is unavailable; minimizing the launcher")
        self.minimize()
        return True

    def refresh(self):
        observed = docker.health()
        if self.transition == "stopping":
            state = "stopping" if observed != "stopped" else "stopped"
            if state == "stopped":
                self.transition = None
        elif self.transition == "starting":
            state = "starting" if observed != "running" else "running"
            if state == "running":
                self.transition = None
        elif observed == "running":
            state = "running"
        elif observed == "stopped" and not self.state.get("desired_running", False):
            state = "stopped"
        elif self.has_been_healthy:
            state = "unhealthy"
        else:
            state = observed
        if state == "running":
            if not self.has_been_healthy:
                self.has_been_healthy = True
                self.set_service_state(ever_healthy=True)
        self.status.set_text({"running": "Running", "starting": "Starting", "stopped": "Stopped", "unhealthy": "Unhealthy"}.get(state, state.title()))
        self.status_dot.set_css_classes(["status-dot", f"status-{state}"])
        stopping = state in {"running", "starting", "stopping"}
        self.action.set_label("Stop" if stopping else "Start")
        self.action.set_css_classes(["action-stop" if stopping else "action-start"])
        self.action.set_sensitive(
            docker.available() and state != "stopping" and not self.automatic_backup_running
        )
        self.open_button.set_sensitive(state == "running")
        self.address.set_text(self.address_text())
        self.update_docker_controls()
        self.last_state = state
        return True

    def refresh_versions(self):
        logger.info("Checking application image versions")
        def worker():
            current, latest, current_digest, latest_digest = docker.image_versions()
            GLib.idle_add(self._set_versions, current, latest, current_digest, latest_digest)
        threading.Thread(target=worker, daemon=True).start()

    def _set_versions(self, current, latest, current_digest, latest_digest):
        self.current_version.set_text(self._version_text(current, "Not installed"))
        self.latest_version.set_text(self._version_text(latest, "Unavailable"))
        self.update_available = bool(current_digest and latest_digest and current_digest != latest_digest)
        self.update_button.set_sensitive(self.update_available and not self.automatic_backup_running)

    @staticmethod
    def _version_text(value, fallback):
        return value.removeprefix("sha256:")[:12] if value else fallback

    def run_async(self, fn, success="Done", on_success=None, on_failure=None):
        logger.info("Starting background action: %s", success)
        self.background_action_count += 1
        def worker():
            try:
                fn()
            except docker.DockerReloginRequired as exc:
                logger.info("Docker configuration requires a new login session")
                GLib.idle_add(self.finish_background_notice, str(exc), on_failure)
            except Exception as exc:
                logger.exception("Background action failed")
                GLib.idle_add(self.finish_background_action, False, str(exc), on_failure)
            else:
                logger.info("Background action completed: %s", success)
                GLib.idle_add(self.finish_background_action, True, success, on_success)
        threading.Thread(target=worker, daemon=True).start()

    def finish_background_notice(self, message, callback):
        self.background_action_count = max(0, self.background_action_count - 1)
        if callback:
            callback()
        dialog = Gtk.AlertDialog(
            message="Docker access configured",
            detail=message,
            buttons=["Close"],
        )
        dialog.show(self)
        self.refresh()
        return False

    def finish_background_action(self, succeeded, message, callback):
        self.background_action_count = max(0, self.background_action_count - 1)
        if callback:
            callback()
        if not succeeded:
            self.show_background_error(message)
        self.refresh()
        self.refresh_versions()
        return False

    def show_background_error(self, message):
        dialog = Gtk.AlertDialog(
            message="KalliOpen operation failed",
            detail=message,
            buttons=["Close"],
        )
        dialog.show(self)

    def toggle(self, *_):
        if self.last_state in {"running", "starting", "stopping"}:
            logger.info("Stop button pressed")
            self.set_service_state(desired_running=False)
            self.transition = "stopping"
            self.refresh()
            self.run_async(
                lambda: docker.compose("down"),
                "Stopped",
                on_failure=self.stop_action_failed,
            )
        else:
            logger.info("Start button pressed")
            self.start_managed_service("Starting KalliOpen")

    def start_managed_service(self, message="Starting KalliOpen"):
        self.begin_managed_restart()
        self.run_async(
            docker.start,
            message,
            on_success=self.managed_service_started,
            on_failure=self.managed_action_failed,
        )

    def stop_action_failed(self):
        self.set_service_state(desired_running=True)
        self.transition = None

    def open_kalliopen(self, *_):
        logger.info("Open KalliOpen button pressed")
        webbrowser.open(docker.application_url())

    def update_kalliopen(self, *_):
        logger.info("Update button pressed")
        self.confirm("Update KalliOpen and run database migrations?", self.run_update)

    def run_update(self):
        self.begin_managed_restart()
        self.run_async(
            docker.update,
            "Updated",
            on_success=self.managed_service_started,
            on_failure=self.managed_action_failed,
        )

    def confirm(self, text: str, callback):
        dialog = Gtk.AlertDialog(message=text, buttons=["Cancel", "Continue"])
        dialog.choose(self, None, lambda d, result: callback() if d.choose_finish(result) == 1 else None)

    def export_db(self, *_):
        logger.info("Export database button pressed")
        dialog = Gtk.FileDialog(title="Choose export folder")
        dialog.select_folder(self, None, lambda d, result: self._export_finish(d, result))

    def _export_finish(self, dialog, result):
        try: folder = dialog.select_folder_finish(result).get_path()
        except GLib.Error: return
        destination = Path(folder) / docker.automatic_backup_path("kalliopen").name
        self.run_async(lambda: docker.export_database(destination), f"Database exported to {destination}")

    def import_db(self, *_):
        logger.info("Import database button pressed")
        dialog = Gtk.FileDialog(title="Import database backup")
        dialog.open(self, None, lambda d, result: self._import_finish(d, result))

    def _import_finish(self, dialog, result):
        try: path = dialog.open_finish(result).get_path()
        except GLib.Error: return
        self.confirm("Importing replaces the current database. A backup will be made first.",
                     lambda: self.run_database_import(Path(path)))

    def run_database_import(self, path: Path):
        self.begin_managed_restart()
        self.run_async(
            lambda: docker.import_database(path),
            "Database imported",
            on_success=self.managed_service_started,
            on_failure=self.managed_action_failed,
        )

    def delete_db(self, *_):
        logger.info("Delete database button pressed")
        self.confirm("Delete all KalliOpen database data? A recovery backup will be created automatically.",
                     self.run_database_delete)

    def run_database_delete(self):
        self.begin_managed_restart()
        self.run_async(
            docker.delete_database,
            "Database deleted",
            on_success=self.managed_service_started,
            on_failure=self.managed_action_failed,
        )

    def run_setup(self, *_):
        logger.info("Docker setup button pressed")
        def setup():
            docker.configure()
            docker.compose("pull")
            docker.start()
        self.begin_managed_restart()
        self.run_async(
            setup,
            "Setup complete",
            on_success=self.managed_service_started,
            on_failure=self.managed_action_failed,
        )

    def update_docker_controls(self):
        ready = docker.available()
        self.setup.set_visible(not ready)
        self.setup.set_label("Configure Docker" if docker.installed() else "Install Docker")

    def show_migration(self, *_):
        logger.info("Migration button pressed")
        MigrationDialog(self).present()

    def _install_css(self):
        css = Gtk.CssProvider()
        css.load_from_data(b"""
        .status-value { font-size: 27px; font-weight: 700; }
        .status-dot { font-size: 22px; }
        .status-stopped { color: #d83a3a; }
        .status-starting, .status-unhealthy, .status-stopping { color: #d98d00; }
        .status-running { color: #2f9e44; }
        button.action-start { background-color: #2f9e44; color: #ffffff; }
        button.action-start:hover { background-color: #27863a; }
        button.action-stop { background-color: #d83a3a; color: #ffffff; }
        button.action-stop:hover { background-color: #bd3030; }
        .section-label { font-weight: 700; margin-top: 4px; }
        .info-grid { margin-top: 5px; margin-bottom: 2px; }
        .info-label { font-size: 12px; font-weight: 700; color: #6b6b6b; }
        .backup-summary { font-size: 14px; color: #6b6b6b; }
        .backup-error { color: #b3261e; }
        .backup-folder-link { min-height: 0; padding: 0; font-size: 14px; color: #6b6b6b; }
        button { min-height: 38px; }
        """)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
