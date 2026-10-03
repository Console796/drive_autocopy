from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import config as config_mod
from .. import drive as drive_mod
from .. import ledger as ledger_mod
from .setup_dialog import SetupDialog
from .worker import StatusWorker, SyncWorker

DRIVE_POLL_MS = 5000  # how often to look for a changed ledger / newly plugged-in drive

COLUMNS = ["Name", "Local Path", "Drive Path", "Last Action", "Last Host", "Last Time", "Pending"]


class MainWindow(QMainWindow):
    def __init__(self, config_path: Path | None = None):
        super().__init__()
        self.setWindowTitle("synctool")
        self.resize(950, 620)

        self.config_path = config_path or config_mod.default_config_path()
        self.config: config_mod.Config | None = None
        self.sync_worker: SyncWorker | None = None
        self.status_worker: StatusWorker | None = None
        # Sets the drive has that this config doesn't, as (name, SyncSetState).
        self._unconfigured: list = []
        self._known_unconfigured: set[str] | None = None  # None until first status, so startup doesn't announce
        self._last_ledger_sig: tuple[int, int] | None = None

        self._build_ui()
        self._load_config()

        # The two setups never have the drive at the same time, so "the other
        # machine added a set" really means "this drive now has a changed
        # ledger" — either because it was just plugged in or re-read.
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_drive)
        self._poll_timer.start(DRIVE_POLL_MS)

    # ---- UI construction ---------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)

        self.drive_label = QLabel("Drive: unknown")
        layout.addWidget(self.drive_label)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, stretch=2)

        hint = QLabel("Select rows to act on just those sync sets — otherwise all sets are used.")
        hint.setStyleSheet("color: gray;")
        layout.addWidget(hint)

        button_row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh Status")
        self.recall_btn = QPushButton("Recall (push to drive)")
        self.deploy_btn = QPushButton("Deploy (pull from drive)")
        self.dry_run_checkbox = QCheckBox("Dry run")
        self.setup_btn = QPushButton("Edit Sync Sets…")
        self.adopt_btn = QPushButton("Add New Sets from Drive…")
        self.adopt_btn.setEnabled(False)
        for w in (self.refresh_btn, self.recall_btn, self.deploy_btn, self.dry_run_checkbox):
            button_row.addWidget(w)
        button_row.addStretch(1)
        button_row.addWidget(self.adopt_btn)
        button_row.addWidget(self.setup_btn)
        layout.addLayout(button_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        layout.addWidget(self.log, stretch=1)

        self.setCentralWidget(central)

        self.refresh_btn.clicked.connect(self.refresh_status)
        self.recall_btn.clicked.connect(lambda: self.run_sync("recall"))
        self.deploy_btn.clicked.connect(lambda: self.run_sync("deploy"))
        self.setup_btn.clicked.connect(self.open_setup_dialog)
        self.adopt_btn.clicked.connect(self.adopt_unconfigured)

    # ---- config / status -----------------------------------------------

    def _load_config(self) -> None:
        try:
            self.config = config_mod.load_config(self.config_path)
        except config_mod.ConfigError:
            self.config = None
            self.drive_label.setText('No config found — click "Edit Sync Sets…" to set one up.')
            self.table.setRowCount(0)
            self._set_sync_buttons_enabled(False)
            return
        self._set_sync_buttons_enabled(True)
        self.refresh_status()

    def _set_sync_buttons_enabled(self, enabled: bool) -> None:
        for w in (self.refresh_btn, self.recall_btn, self.deploy_btn):
            w.setEnabled(enabled)

    def refresh_status(self) -> None:
        if self.config is None or self.status_worker is not None:
            return
        self.drive_label.setText("Checking drive…")
        self.status_worker = StatusWorker(self.config)
        self.status_worker.result.connect(self._on_status_result)
        self.status_worker.failed.connect(self._on_status_failed)
        self.status_worker.finished.connect(self._status_worker_done)
        self.status_worker.start()

    def _status_worker_done(self) -> None:
        self.status_worker = None

    def _on_status_failed(self, message: str) -> None:
        self.drive_label.setText(f"Drive: {message}")
        self.table.setRowCount(0)
        self._unconfigured = []
        self.adopt_btn.setEnabled(False)
        self._last_ledger_sig = None

    def _poll_drive(self) -> None:
        """Refresh when the ledger on the drive changes or the drive appears.

        Only stats a file at an already-mounted path (never mounts), so it's
        cheap enough to run on a timer; the expensive rsync previews only run
        when something actually changed.
        """
        if self.config is None or self.sync_worker is not None or self.status_worker is not None:
            return
        mount = drive_mod.find_existing_mount(self.config.drive.uuid)
        sig = ledger_mod.signature(mount) if mount else None
        if sig != self._last_ledger_sig:
            self.refresh_status()

    def _on_status_result(self, payload) -> None:
        mount_point, hostname, statuses, drive_name, unconfigured = payload
        self._last_ledger_sig = ledger_mod.signature(mount_point)
        self._unconfigured = unconfigured
        self.adopt_btn.setEnabled(bool(unconfigured))

        label = f"Drive: {drive_name}  ({mount_point})   |   this host: {hostname}"
        if unconfigured:
            label += f"   |   {len(unconfigured)} new set(s) on drive not set up here"
        self.drive_label.setText(label)

        names = {name for name, _ in unconfigured}
        if self._known_unconfigured is not None:
            for name, state in unconfigured:
                if name not in self._known_unconfigured:
                    self.log.appendPlainText(
                        f"New sync set on drive: {name!r} (last {state.last_action} by "
                        f"{state.last_hostname} at {state.last_timestamp}) — "
                        f'click "Add New Sets from Drive…" to set it up here.'
                    )
        self._known_unconfigured = names

        self.table.setRowCount(len(statuses) + len(unconfigured))
        for row, s in enumerate(statuses):
            values = [
                s.name,
                s.local_path,
                s.drive_path,
                s.last_action or "—",
                s.last_hostname or "—",
                s.last_timestamp or "—",
                "n/a" if s.pending_changes is None else str(s.pending_changes),
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))

        # Drive-only sets: shown so they can't go unnoticed, but not selectable
        # since there's no local path to recall/deploy yet.
        muted = QBrush(QColor("gray"))
        italic = QFont()
        italic.setItalic(True)
        for offset, (name, state) in enumerate(unconfigured):
            row = len(statuses) + offset
            values = [
                name,
                "(not set up on this machine)",
                state.drive_subpath,
                state.last_action,
                state.last_hostname,
                state.last_timestamp,
                "NEW",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                item.setForeground(muted)
                item.setFont(italic)
                self.table.setItem(row, col, item)

    # ---- sync actions ----------------------------------------------------

    def _selected_names(self) -> list[str] | None:
        rows = {idx.row() for idx in self.table.selectedIndexes()}
        configured = {s.name for s in self.config.sync_sets} if self.config else set()
        names = [self.table.item(row, 0).text() for row in sorted(rows)]
        names = [n for n in names if n in configured]
        return names or None

    def run_sync(self, action: str) -> None:
        if self.config is None or self.sync_worker is not None:
            return
        only = self._selected_names()
        self.log.clear()
        self.progress.setVisible(True)
        self._set_sync_buttons_enabled(False)

        self.sync_worker = SyncWorker(
            self.config,
            action=action,
            only=only,
            dry_run=self.dry_run_checkbox.isChecked(),
        )
        self.sync_worker.line.connect(self._on_line)
        self.sync_worker.set_started.connect(self._on_set_started)
        self.sync_worker.set_finished.connect(self._on_set_finished)
        self.sync_worker.confirm_needed.connect(
            self._on_confirm_needed, Qt.ConnectionType.BlockingQueuedConnection
        )
        self.sync_worker.failed.connect(self._on_sync_failed)
        self.sync_worker.finished_run.connect(self._on_sync_finished)
        self.sync_worker.finished.connect(self._sync_worker_done)
        self.sync_worker.start()

    def _sync_worker_done(self) -> None:
        self.sync_worker = None
        self.progress.setVisible(False)
        self._set_sync_buttons_enabled(True)

    def _on_line(self, name: str, text: str) -> None:
        self.log.appendPlainText(f"[{name}] {text.rstrip()}")

    def _on_set_started(self, name: str, action: str) -> None:
        self.log.appendPlainText(f"--- {action} {name} ---")

    def _on_set_finished(self, name: str, ok: bool, message: str) -> None:
        status = "OK" if ok else "FAILED/SKIPPED"
        self.log.appendPlainText(f"[{name}] {status}: {message}")

    def _on_confirm_needed(self, message: str) -> None:
        reply = QMessageBox.question(
            self,
            "Confirm sync",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if self.sync_worker is not None:
            self.sync_worker._confirm_result = reply == QMessageBox.StandardButton.Yes

    def _on_sync_failed(self, message: str) -> None:
        QMessageBox.critical(self, "Sync failed", message)

    def _on_sync_finished(self, ok: bool) -> None:
        self.refresh_status()
        if not ok:
            QMessageBox.warning(
                self, "Sync finished with issues", "Some sync sets failed or were skipped — see the log."
            )

    # ---- setup -------------------------------------------------------------

    def open_setup_dialog(self) -> None:
        dialog = SetupDialog(self.config_path, self.config, self)
        if dialog.exec():
            self._load_config()

    def adopt_unconfigured(self) -> None:
        """Open the setup dialog with the drive-only sets pre-filled as new rows.

        name/drive_subpath come from the ledger; local_path defaults to
        ~/<name> and stays editable. Rows can be removed to skip a set.
        """
        if self.config is None or not self._unconfigured:
            return
        prefill = [
            config_mod.SyncSet(
                name=name,
                local_path=str(Path.home() / name),
                drive_subpath=state.drive_subpath or name,
            )
            for name, state in self._unconfigured
        ]
        dialog = SetupDialog(self.config_path, self.config, self, prefill=prefill)
        if dialog.exec():
            self._load_config()
