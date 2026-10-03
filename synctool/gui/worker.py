from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from .. import drive as drive_mod
from .. import engine
from .. import ignore as ignore_mod
from .. import ledger as ledger_mod
from .. import pipeline
from ..config import Config


class SyncWorker(QThread):
    """Runs pipeline.run() off the GUI thread.

    Confirmation warnings from the pipeline are relayed back to the GUI
    thread via `confirm_needed`, which the caller must connect with
    Qt.ConnectionType.BlockingQueuedConnection: that blocks this worker
    thread until the main-thread slot has shown a QMessageBox and written
    its answer into `_confirm_result`, giving synchronous confirm() semantics
    without ever touching Qt widgets off the main thread.
    """

    line = pyqtSignal(str, str)  # set_name, output line
    set_started = pyqtSignal(str, str)  # set_name, action
    set_finished = pyqtSignal(str, bool, str)  # set_name, ok, message
    confirm_needed = pyqtSignal(str)  # message
    finished_run = pyqtSignal(bool)  # overall ok
    failed = pyqtSignal(str)  # fatal error message

    def __init__(
        self,
        config: Config,
        action: str,
        only: list[str] | None = None,
        dry_run: bool = False,
        assume_yes: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.config = config
        self.action = action
        self.only = only
        self.dry_run = dry_run
        self.assume_yes = assume_yes
        self._confirm_result = False

    def _confirm(self, message: str) -> bool:
        self._confirm_result = False
        self.confirm_needed.emit(message)
        return self._confirm_result

    def run(self) -> None:
        try:
            report = pipeline.run(
                self.config,
                action=self.action,  # type: ignore[arg-type]
                only=self.only,
                dry_run=self.dry_run,
                assume_yes=self.assume_yes,
                confirm=self._confirm,
                on_set_start=lambda name, action: self.set_started.emit(name, action),
                on_line=lambda name, text: self.line.emit(name, text),
            )
        except (pipeline.PipelineAbort, drive_mod.DriveError, ledger_mod.LedgerError) as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # surface unexpected errors rather than crash silently
            self.failed.emit(f"unexpected error: {exc}")
            return

        for outcome in report.outcomes:
            if outcome.skipped:
                self.set_finished.emit(outcome.name, False, outcome.skip_reason or "skipped")
            else:
                result = outcome.rsync_result
                ok = bool(result and result.ok)
                message = "ok" if ok else f"rsync exited {result.returncode if result else '?'}"
                self.set_finished.emit(outcome.name, ok, message)

        self.finished_run.emit(report.ok)


@dataclass
class SetStatus:
    name: str
    local_path: str
    drive_path: str
    last_action: str | None
    last_hostname: str | None
    last_timestamp: str | None
    pending_changes: int | None  # None = unknown (e.g. drive path doesn't exist yet)


class StatusWorker(QThread):
    """Computes drive/ledger/pending-change status off the GUI thread."""

    # (mount_point, hostname, statuses: list[SetStatus], drive_name,
    #  unconfigured: list[(name, SyncSetState)] — on the drive, not in this config)
    result = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config

    def run(self) -> None:
        try:
            info = drive_mod.ensure_mounted(self.config.drive.uuid)
            ledger_obj = ledger_mod.load(info.mount_point)
        except (drive_mod.DriveError, ledger_mod.LedgerError) as exc:
            self.failed.emit(str(exc))
            return

        candidate = drive_mod.find_drive(self.config.drive.uuid)
        drive_name = candidate.display_name if candidate else (self.config.drive.label or "drive")

        hostname = ledger_mod.current_hostname(self.config.setup.hostname_override)
        statuses: list[SetStatus] = []
        for s in self.config.sync_sets:
            state = ledger_obj.sync_sets.get(s.name)
            drive_target = info.mount_point / s.drive_subpath
            local_path = Path(s.local_path)

            pending: int | None = None
            try:
                filters = ignore_mod.filter_args(ignore_mod.load(local_path))
            except ignore_mod.IgnoreError:
                filters = None  # unreadable .syncignore: pending count unknown; the sync itself will say why
            if filters is not None and drive_target.exists() and local_path.exists():
                preview = engine.run_rsync(
                    engine.build_recall_command(local_path, drive_target, dry_run=True, filter_args=filters),
                    stream=False,
                )
                pending = sum(
                    1
                    for line in preview.output.splitlines()
                    if line and not line.startswith(("sending", "sent ", "total size"))
                )

            statuses.append(
                SetStatus(
                    name=s.name,
                    local_path=s.local_path,
                    drive_path=str(drive_target),
                    last_action=state.last_action if state else None,
                    last_hostname=state.last_hostname if state else None,
                    last_timestamp=state.last_timestamp if state else None,
                    pending_changes=pending,
                )
            )

        unconfigured = pipeline.find_unconfigured_sets(ledger_obj, self.config)
        self.result.emit((info.mount_point, hostname, statuses, drive_name, unconfigured))
