from __future__ import annotations

import logging
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from .workflow import MigrationSession, source_versions
from .postgres import PostgresConfig

logger = logging.getLogger(__name__)


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
