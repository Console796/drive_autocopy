from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .. import drive as drive_mod

COLUMNS = ["Drive", "Size", "Connection", "Device", "Mounted At"]
UUID_ROLE = Qt.ItemDataRole.UserRole


class DrivePickerDialog(QDialog):
    """Pick a drive by its name/size/bus instead of by raw UUID.

    The UUID is still what gets stored in the config — it's the only stable
    identifier across replugs (SPEC.md §8 Step 1) — but the user never has to
    read or type one.
    """

    def __init__(self, parent=None, drives: list[drive_mod.DriveCandidate] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Select Sync Drive")
        self.resize(820, 380)
        self._injected_drives = drives
        self._drives: list[drive_mod.DriveCandidate] = []

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("Choose the external drive you carry between your two setups:")
        )

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(self._accept_if_selected)
        layout.addWidget(self.table, stretch=1)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: gray;")
        layout.addWidget(self.status_label)

        controls = QHBoxLayout()
        self.show_internal = QCheckBox("Show internal drives too")
        self.show_internal.toggled.connect(self.reload)
        controls.addWidget(self.show_internal)
        controls.addStretch(1)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.reload)
        controls.addWidget(refresh_btn)
        layout.addLayout(controls)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.table.itemSelectionChanged.connect(self._update_ok_state)
        self.reload()

    def reload(self) -> None:
        all_drives = (
            self._injected_drives
            if self._injected_drives is not None
            else drive_mod.list_drives()
        )
        if self.show_internal.isChecked():
            self._drives = list(all_drives)
        else:
            self._drives = [d for d in all_drives if d.is_external]

        self.table.setRowCount(len(self._drives))
        for row, d in enumerate(self._drives):
            values = [d.display_name, d.size, d.bus, str(d.device), d.mountpoint or "not mounted"]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setData(UUID_ROLE, d.uuid)
                    tooltip = f"{d.describe()}\nUUID: {d.uuid}"
                    item.setToolTip(tooltip)
                self.table.setItem(row, col, item)

        hidden = len(all_drives) - len(self._drives)
        if not self._drives:
            if hidden:
                self.status_label.setText(
                    f"No external drives detected ({hidden} internal hidden) — "
                    "plug the drive in and click Refresh, or tick the box above."
                )
            else:
                self.status_label.setText(
                    "No drives detected — plug the drive in and click Refresh."
                )
        elif hidden:
            self.status_label.setText(f"{hidden} internal drive(s) hidden.")
        else:
            self.status_label.setText("")

        if self._drives:
            self.table.selectRow(0)
        self._update_ok_state()

    def _update_ok_state(self) -> None:
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setEnabled(self.selected_drive() is not None)

    def _accept_if_selected(self) -> None:
        if self.selected_drive() is not None:
            self.accept()

    def selected_drive(self) -> drive_mod.DriveCandidate | None:
        rows = {index.row() for index in self.table.selectedIndexes()}
        if len(rows) != 1:
            return None
        row = rows.pop()
        if 0 <= row < len(self._drives):
            return self._drives[row]
        return None
