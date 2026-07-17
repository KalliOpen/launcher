from __future__ import annotations

import threading
import webbrowser
import logging
from datetime import datetime
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gio

from . import backups, config, docker
from .migration_workflow import MigrationSession, source_versions
from .migrator.postgres import PostgresConfig

logger = logging.getLogger(__name__)
MANAGED_LIFECYCLE_VERSION = 1


class MigrationDialog(Gtk.Window):
    def __init__(self, parent: Gtk.Window):
        super().__init__(title="Migrate ps-biblio data", transient_for=parent, modal=True)
        self.parent_window = parent
        self.session: MigrationSession | None = None
        self.target_config: PostgresConfig | None = None
        self.mdf_path: str | None = None
        self.ldf_path: str | None = None
        self.status = Gtk.Label(xalign=0, wrap=True)
        self.report = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.report.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.report.set_top_margin(10); self.report.set_bottom_margin(10)
        self.report.set_left_margin(10); self.report.set_right_margin(10)
        self.report.add_css_class("migration-report")
        self.report_scroll = Gtk.ScrolledWindow()
        self.report_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.report_scroll.set_min_content_height(150)
        self.report_scroll.set_max_content_height(220)
        self.report_scroll.set_propagate_natural_height(True)
        self.report_scroll.set_child(self.report)
        self.report_scroll.set_visible(False)
        self.version = Gtk.ComboBoxText()
        for version in source_versions():
            self.version.append_text(f"SQL Server {version.upper()}")
        self.version.set_active(source_versions().index("2022"))
        self.custom_postgres = Gtk.CheckButton(label="Use custom PostgreSQL connection")
        self.custom_postgres.connect("toggled", lambda button: self.pg_fields.set_visible(button.get_active()))
        self.pg_fields = Gtk.Grid(column_spacing=10, row_spacing=8)
        self.pg_fields.set_visible(False)
        self.pg_host = self._entry("localhost")
        self.pg_port = self._entry("5432")
        self.pg_user = self._entry("kalliopen")
        self.pg_password = self._entry(""); self.pg_password.set_visibility(False)
        self.pg_database = self._entry("kalliopen")
        self.pg_schema = self._entry("public")
        for row, label, entry in (
            (0, "Host", self.pg_host), (1, "Port", self.pg_port),
            (2, "User", self.pg_user), (3, "Password", self.pg_password),
            (4, "Database", self.pg_database), (5, "Schema", self.pg_schema),
        ):
            self.pg_fields.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            self.pg_fields.attach(entry, 1, row, 1, 1)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_top(24); body.set_margin_bottom(24)
        body.set_margin_start(28); body.set_margin_end(28)
        body.append(Gtk.Label(label="Source files", xalign=0, css_classes=["title-3"]))
        self.mdf_button = Gtk.Button(label="Select MDF data file")
        self.mdf_button.connect("clicked", lambda *_: self.select_file("mdf"))
        self.ldf_button = Gtk.Button(label="Select log file")
        self.ldf_button.connect("clicked", lambda *_: self.select_file("ldf"))
        body.append(self.mdf_button); body.append(self.ldf_button)
        body.append(Gtk.Label(label="Original SQL Server version", xalign=0))
        body.append(self.version)
        body.append(self.custom_postgres)
        body.append(self.pg_fields)
        body.append(self.status); body.append(self.report_scroll)
        self.prepare = Gtk.Button(label="Prepare preview")
        self.prepare.add_css_class("suggested-action")
        self.prepare.connect("clicked", self.prepare_preview)
        self.commit = Gtk.Button(label="Confirm and import")
        self.commit.add_css_class("destructive-action")
        self.commit.set_sensitive(False)
        self.commit.connect("clicked", self.confirm_import)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        actions.append(self.prepare); actions.append(self.commit)
        body.append(actions)
        self.set_child(body)
        self.connect("close-request", self.close_session)

    def select_file(self, kind: str):
        logger.info("Migration file selection opened for %s", kind.upper())
        dialog = Gtk.FileDialog(title="Select ps-biblio source file")
        dialog.open(self, None, lambda d, result: self.file_selected(d, result, kind))

    def file_selected(self, dialog, result, kind: str):
        try:
            path = dialog.open_finish(result).get_path()
        except GLib.Error:
            return
        if kind == "mdf":
            self.mdf_path = path
            self.mdf_button.set_label(Path(path).name)
        else:
            self.ldf_path = path
            self.ldf_button.set_label(Path(path).name)
        logger.info("Migration %s selected: %s", kind.upper(), path)

    def prepare_preview(self, *_):
        if not self.mdf_path or not self.ldf_path:
            self.status.set_text("Select both the MDF data file and transaction log first.")
            return
        version = source_versions()[self.version.get_active()]
        logger.info("Migration preview requested")
        self.prepare.set_sensitive(False)
        self.status.set_text("Reading source data and preparing migration preview...")

        def worker():
            try:
                self.session = MigrationSession.create(self.mdf_path, self.ldf_path, version)
                plan = self.session.prepare_preview()
                message = "Preview ready. Confirming import creates a backup, resets the database, runs schema migrations, and imports this data."
                GLib.idle_add(self.set_preview, plan, message)
            except Exception as exc:
                GLib.idle_add(self.preview_failed, str(exc))
        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _entry(value: str) -> Gtk.Entry:
        entry = Gtk.Entry()
        entry.set_text(value)
        entry.set_hexpand(True)
        return entry

    def postgres_config(self) -> PostgresConfig | None:
        if not self.custom_postgres.get_active():
            return None
        try:
            port = int(self.pg_port.get_text().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError as exc:
            raise ValueError("PostgreSQL port must be between 1 and 65535.") from exc
        values = [self.pg_host, self.pg_user, self.pg_password, self.pg_database, self.pg_schema]
        if any(not field.get_text().strip() for field in values):
            raise ValueError("All custom PostgreSQL fields are required.")
        return PostgresConfig(
            host=self.pg_host.get_text().strip(), port=port,
            user=self.pg_user.get_text().strip(), password=self.pg_password.get_text(),
            database=self.pg_database.get_text().strip(), schema=self.pg_schema.get_text().strip(),
        )

    def set_preview(self, plan, message):
        lines = ["SOURCE ROWS"]
        lines.extend(f"  {label:<18} {count:>8,}" for label, count in plan.source_counts.items())
        lines.extend(("", "ROWS TO IMPORT"))
        lines.extend(f"  {label:<18} {count:>8,}" for label, count in plan.record_counts.items())
        lines.extend(("", "SKIPPED OR ADJUSTED"))
        if plan.skipped:
            lines.extend(f"  {label}: {count:,}" for label, count in plan.skipped.items())
        else:
            lines.append("  None")
        self.set_report("\n".join(lines))
        self.status.set_text(message)
        self.commit.set_sensitive(True)

    def preview_failed(self, message):
        self.status.set_text("Could not prepare the migration preview.")
        self.show_error("Migration preview failed", message)
        self.prepare.set_sensitive(True)

    def set_report(self, text: str):
        self.report.get_buffer().set_text(text)
        self.report_scroll.set_visible(True)

    def show_error(self, title: str, message: str):
        logger.error("%s: %s", title, message)
        dialog = Gtk.AlertDialog(message=title, detail=message, buttons=["Close"])
        dialog.show(self)

    def confirm_import(self, *_):
        logger.info("Migration import confirmation requested")
        try:
            postgres = self.postgres_config()
        except ValueError as exc:
            self.status.set_text("Check the custom PostgreSQL connection details.")
            self.show_error("Invalid PostgreSQL configuration", str(exc))
            return
        self.target_config = postgres
        message = (
            "Import into the custom PostgreSQL target? The target must already contain the empty KalliOpen schema."
            if self.target_config is not None
            else "Import the previewed data? The local database will first be backed up and then replaced."
        )
        dialog = Gtk.AlertDialog(
            message=message,
            buttons=["Cancel", "Import"],
        )
        dialog.choose(self, None, self.import_confirmed)

    def import_confirmed(self, dialog, result):
        if dialog.choose_finish(result) != 1:
            logger.info("Migration import cancelled")
            return
        logger.info("Migration import confirmed")
        self.commit.set_sensitive(False)
        self.status.set_text("Backing up, resetting, migrating schema, and importing data...")
        if self.target_config is None:
            self.parent_window.begin_managed_restart()

        def worker():
            try:
                assert self.session is not None
                backup = self.session.commit(self.target_config)
                GLib.idle_add(self.import_finished, backup)
            except Exception as exc:
                GLib.idle_add(self.import_failed, str(exc))
        threading.Thread(target=worker, daemon=True).start()

    def import_finished(self, backup):
        detail = (
            f"A backup of the previous local database was saved to:\n{backup}"
            if backup else "The custom PostgreSQL target was imported successfully."
        )
        if backup is not None:
            self.parent_window.managed_service_started()
        self.close()
        dialog = Gtk.AlertDialog(
            message="Migration completed successfully",
            detail=detail,
            buttons=["Done"],
        )
        dialog.show(self.parent_window)

    def import_failed(self, message):
        self.status.set_text("Migration failed. Correct the connection details if necessary and try again.")
        self.commit.set_sensitive(True)
        if self.target_config is None:
            self.parent_window.managed_action_failed()
        self.show_error("Migration failed", message)

    def close_session(self, *_):
        logger.info("Migration dialog closed")
        if self.session:
            threading.Thread(target=self.session.close, daemon=True).start()
        return False


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
        self.action.add_css_class("suggested-action")
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
        settings.attach(Gtk.Label(label="minutes", xalign=0), 2, 0, 1, 1)
        settings.attach(Gtk.Label(label="Keep for", xalign=0), 3, 0, 1, 1)
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
        open_folder = Gtk.Button()
        open_folder.set_tooltip_text("Open backup folder")
        open_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        open_content.append(Gtk.Image.new_from_icon_name("folder-open-symbolic"))
        open_content.append(Gtk.Label(label="Open folder"))
        open_folder.set_child(open_content)
        open_folder.connect("clicked", self.open_backup_folder)
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

    def open_backup_folder(self, *_args) -> None:
        logger.info("Open backup folder button pressed")
        config.ensure_backup_directory()
        try:
            Gio.AppInfo.launch_default_for_uri(config.BACKUP_DIR.as_uri(), None)
        except GLib.Error as exc:
            self.show_background_error(str(exc))

    def refresh_backup_summary(self) -> None:
        current = backups.summary()
        latest = current.latest.strftime("%Y-%m-%d %H:%M") if current.latest else "Never"
        text = f"Scheduled: {current.count} | Last: {latest}"
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
        logger.info("Main window closed; quitting application")
        application = self.get_application()
        if application:
            application.quit()
        return False

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
        self.action.set_label("Stop" if state in {"running", "starting", "stopping"} else "Start")
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
        .section-label { font-weight: 700; margin-top: 4px; }
        .info-grid { margin-top: 5px; margin-bottom: 2px; }
        .info-label { font-size: 12px; font-weight: 700; color: #6b6b6b; }
        .backup-summary { font-size: 12px; color: #6b6b6b; }
        .backup-error { color: #b3261e; }
        button { min-height: 38px; }
        """)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.kalliopen.Launcher", flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.window = None

    def do_activate(self):
        if self.window is None:
            self.window = Window(self)
        self.window.present()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return App().run(None)
