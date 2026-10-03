from __future__ import annotations

from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import config as config_mod
from .. import drive as drive_mod
from .. import ledger as ledger_mod
from .. import pipeline
from .drive_picker import DrivePickerDialog


class SyncSetRow(QWidget):
    def __init__(
        self,
        name: str = "",
        local_path: str = "",
        drive_subpath: str = "",
        on_remove: "Callable[[SyncSetRow], None] | None" = None,
    ):
        super().__init__()
        self._on_remove = on_remove
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("name")
        self.local_edit = QLineEdit(local_path)
        self.local_edit.setPlaceholderText("local path")
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        self.drive_edit = QLineEdit(drive_subpath)
        self.drive_edit.setPlaceholderText("drive subpath")
        remove_btn = QPushButton("✕")
        remove_btn.setFixedWidth(28)
        remove_btn.clicked.connect(self._remove_self)

        layout.addWidget(self.name_edit, 2)
        layout.addWidget(self.local_edit, 4)
        layout.addWidget(browse_btn)
        layout.addWidget(self.drive_edit, 3)
        layout.addWidget(remove_btn)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select local directory")
        if path:
            self.local_edit.setText(path)
            if not self.name_edit.text():
                self.name_edit.setText(Path(path).name)
            if not self.drive_edit.text():
                self.drive_edit.setText(Path(path).name)

    def _remove_self(self) -> None:
        if self._on_remove:
            self._on_remove(self)

    def to_sync_set(self) -> config_mod.SyncSet | None:
        name = self.name_edit.text().strip()
        local_path = self.local_edit.text().strip()
        drive_subpath = self.drive_edit.text().strip()
        if not (name and local_path and drive_subpath):
            return None
        return config_mod.SyncSet(name=name, local_path=local_path, drive_subpath=drive_subpath)


class SetupDialog(QDialog):
    """Edits drive settings and sync sets, writing them to config.toml.

    Per SPEC.md §6, `local_path` is expected to differ between this setup
    and the other one — only `name`/`drive_subpath` need to match across
    both setups' independently-maintained config files.
    """

    def __init__(
        self,
        config_path: Path,
        existing: config_mod.Config | None,
        parent=None,
        prefill: list[config_mod.SyncSet] | None = None,
    ):
        """`prefill` adds extra editable rows after the existing ones — used to
        adopt sync sets that the other setup added to the drive."""
        super().__init__(parent)
        self.setWindowTitle("Drive & Sync Set Setup")
        self.resize(760, 470)
        self.config_path = config_path
        self._auto_label = existing.drive.label if existing else ""
        self._is_new_setup = existing is None
        self._offered_ledger_import = False

        layout = QVBoxLayout(self)

        form = QFormLayout()

        uuid_row = QHBoxLayout()
        self.uuid_edit = QLineEdit(existing.drive.uuid if existing else "")
        self.uuid_edit.setPlaceholderText("click Choose Drive… to pick by name")
        choose_btn = QPushButton("Choose Drive…")
        choose_btn.clicked.connect(self._choose_drive)
        uuid_row.addWidget(self.uuid_edit)
        uuid_row.addWidget(choose_btn)
        uuid_container = QWidget()
        uuid_container.setLayout(uuid_row)
        form.addRow("Sync drive:", uuid_container)

        # Shows which physical drive the UUID above currently refers to, so the
        # stored identifier is never the only thing on screen.
        self.drive_hint = QLabel("")
        self.drive_hint.setWordWrap(True)
        self.drive_hint.setStyleSheet("color: gray;")
        form.addRow("", self.drive_hint)

        # Debounced so typing a UUID by hand doesn't spawn an lsblk per keystroke.
        self._hint_timer = QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.timeout.connect(self._update_drive_hint)
        self.uuid_edit.textChanged.connect(lambda: self._hint_timer.start(400))
        self._update_drive_hint()

        self.label_edit = QLineEdit(existing.drive.label if existing else "")
        form.addRow("Drive label (informational):", self.label_edit)

        self.mount_edit = QLineEdit(
            existing.drive.mount_point if existing and existing.drive.mount_point else "/media/synctool-mount"
        )
        form.addRow("Preferred mount point:", self.mount_edit)

        self.hostname_edit = QLineEdit(existing.setup.hostname_override if existing else "")
        self.hostname_edit.setPlaceholderText("leave blank to use this machine's hostname")
        form.addRow("Hostname override:", self.hostname_edit)

        layout.addLayout(form)

        layout.addWidget(QLabel("Sync sets — local_path is specific to this machine (SPEC.md §6):"))
        header = QHBoxLayout()
        for text, stretch in (("Name", 2), ("Local Path", 4), ("", 1), ("Drive Subpath", 3), ("", 1)):
            lbl = QLabel(f"<b>{text}</b>")
            header.addWidget(lbl, stretch)
        header_widget = QWidget()
        header_widget.setLayout(header)
        layout.addWidget(header_widget)

        self.rows_container = QVBoxLayout()
        rows_widget = QWidget()
        rows_widget.setLayout(self.rows_container)
        layout.addWidget(rows_widget, stretch=1)

        self.rows: list[SyncSetRow] = []
        if existing:
            for s in existing.sync_sets:
                self._add_row(s.name, s.local_path, s.drive_subpath)
        else:
            self._add_row()
        for s in prefill or []:
            self._add_row(s.name, s.local_path, s.drive_subpath)

        add_row_btn = QPushButton("+ Add Sync Set")
        add_row_btn.clicked.connect(lambda: self._add_row())
        layout.addWidget(add_row_btn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_row(self, name: str = "", local_path: str = "", drive_subpath: str = "") -> None:
        row = SyncSetRow(name, local_path, drive_subpath, on_remove=self._remove_row)
        self.rows.append(row)
        self.rows_container.addWidget(row)

    def _remove_row(self, row: SyncSetRow) -> None:
        self.rows_container.removeWidget(row)
        if row in self.rows:
            self.rows.remove(row)
        row.setParent(None)
        row.deleteLater()

    def _choose_drive(self) -> None:
        dialog = DrivePickerDialog(self)
        if not dialog.exec():
            return
        chosen = dialog.selected_drive()
        if chosen is None:
            return
        self.uuid_edit.setText(chosen.uuid)
        # Keep the informational label in step with the drive unless the user
        # deliberately typed something of their own.
        if not self.label_edit.text().strip() or self.label_edit.text().strip() == self._auto_label:
            self.label_edit.setText(chosen.display_name)
            self._auto_label = chosen.display_name

        if self._is_new_setup:
            self._offer_ledger_import(chosen.uuid)

    def _offer_ledger_import(self, uuid: str) -> None:
        """First-time setup only: if the chosen drive already carries a
        ledger left by the other setup, offer to bootstrap the sync-set rows
        from it instead of leaving the user to type every one by hand.

        Mirrors the CLI's `synctool init` behavior (cli.py's
        `_sync_sets_from_ledger`) — `name`/`drive_subpath` come from the
        ledger, `local_path` defaults to `~/<name>`, and the resulting rows
        stay fully editable (and removable) before Save is clicked.
        """
        if self._offered_ledger_import:
            return
        has_content = any(
            row.name_edit.text().strip() or row.local_edit.text().strip() or row.drive_edit.text().strip()
            for row in self.rows
        )
        if has_content:
            return

        try:
            info = drive_mod.ensure_mounted(uuid)
        except drive_mod.DriveError:
            return

        try:
            ledger_obj = ledger_mod.load(info.mount_point)
        except ledger_mod.LedgerError:
            return

        if not ledger_obj.sync_sets:
            return

        self._offered_ledger_import = True

        lines = [
            f"• {name!r} — drive_subpath={state.drive_subpath!r} "
            f"(last {state.last_action} by {state.last_hostname} at {state.last_timestamp})"
            for name, state in ledger_obj.sync_sets.items()
        ]
        reply = QMessageBox.question(
            self,
            "Import sync sets from drive?",
            "This drive already has sync sets recorded from another setup:\n\n"
            + "\n".join(lines)
            + "\n\nImport these as a starting point? Local paths default to "
            "~/<name> and everything stays editable below before you save.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for row in list(self.rows):
            self._remove_row(row)
        for name, state in ledger_obj.sync_sets.items():
            drive_subpath = state.drive_subpath or name
            default_local = str(Path.home() / name)
            self._add_row(name, default_local, drive_subpath)

    def _update_drive_hint(self) -> None:
        uuid = self.uuid_edit.text().strip()
        if not uuid:
            self.drive_hint.setText("No drive selected yet.")
            return
        match = drive_mod.find_drive(uuid)
        if match is None:
            self.drive_hint.setText(
                f"UUID {uuid} — not currently connected (that's fine; it will be "
                "matched when you plug it in)."
            )
        else:
            self.drive_hint.setText(f"✓ {match.describe()}")

    def _save(self) -> None:
        uuid = self.uuid_edit.text().strip()
        if not uuid:
            QMessageBox.warning(self, "Missing UUID", "Drive UUID is required.")
            return

        sync_sets = []
        names: set[str] = set()
        for row in self.rows:
            s = row.to_sync_set()
            if s is None:
                continue
            if s.name in names:
                QMessageBox.warning(self, "Duplicate name", f"Sync set name {s.name!r} is used more than once.")
                return
            names.add(s.name)
            sync_sets.append(s)

        if not sync_sets:
            QMessageBox.warning(self, "No sync sets", "Add at least one complete sync set.")
            return

        cfg = config_mod.Config(
            drive=config_mod.DriveConfig(
                uuid=uuid,
                label=self.label_edit.text().strip(),
                mount_point=self.mount_edit.text().strip() or "/media/synctool-mount",
            ),
            setup=config_mod.SetupConfig(hostname_override=self.hostname_edit.text().strip()),
            sync_sets=sync_sets,
        )
        config_mod.write_config(self.config_path, cfg)
        self._announce_sets_on_drive(cfg)
        self.accept()

    def _announce_sets_on_drive(self, cfg: config_mod.Config) -> None:
        """Best-effort: record new sets in the drive's ledger so the other
        setup can discover them without waiting for a first recall.

        The config is already saved, so an unreachable drive is only a note —
        the next recall registers the set anyway.
        """
        try:
            result = pipeline.register_new_sets(cfg)
        except (drive_mod.DriveError, ledger_mod.LedgerError) as exc:
            QMessageBox.information(
                self,
                "Saved",
                f"Config saved. Couldn't announce the sync sets on the drive ({exc}), so the "
                "other setup won't see any new ones until your next Recall.",
            )
            return
        if result.conflicts:
            QMessageBox.warning(
                self,
                "Drive subpath mismatch",
                "Config saved, but:\n\n" + "\n\n".join(result.conflicts),
            )
