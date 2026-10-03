from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Sequence

from . import drive as drive_mod
from . import engine
from . import ledger as ledger_mod
from .config import Config, SyncSet

Action = Literal["recall", "deploy"]
ConfirmFn = Callable[[str], bool]
LineFn = Callable[[str, str], None]
SetStartFn = Callable[[str, str], None]


class PipelineAbort(Exception):
    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class SetOutcome:
    name: str
    warning: str | None
    skipped: bool
    skip_reason: str | None
    rsync_result: engine.RsyncResult | None


@dataclass
class RunReport:
    action: Action
    drive_mount: Path
    outcomes: list[SetOutcome]

    @property
    def ok(self) -> bool:
        return all(
            not o.skipped and o.rsync_result is not None and o.rsync_result.ok
            for o in self.outcomes
        )

    @property
    def any_aborted(self) -> bool:
        return any(o.skip_reason == "aborted by user" for o in self.outcomes)

    @property
    def any_failed(self) -> bool:
        return any(
            (not o.skipped) and o.rsync_result is not None and not o.rsync_result.ok
            for o in self.outcomes
        )


def _default_confirm(message: str) -> bool:
    try:
        answer = input(f"WARNING: {message}\nContinue? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def select_sync_sets(config: Config, only: str | Sequence[str] | None) -> list[SyncSet]:
    if only is None:
        return list(config.sync_sets)
    names = [only] if isinstance(only, str) else list(only)

    selected: list[SyncSet] = []
    missing: list[str] = []
    for name in names:
        s = config.get_sync_set(name)
        if s is None:
            missing.append(name)
        else:
            selected.append(s)
    if missing:
        raise PipelineAbort(f"no sync_set(s) named {', '.join(map(repr, missing))} in config", exit_code=4)
    return selected


def validate_local_paths(sync_sets: list[SyncSet]) -> None:
    missing = [s for s in sync_sets if not Path(s.local_path).exists()]
    if missing:
        names = ", ".join(f"{s.name!r} ({s.local_path})" for s in missing)
        raise PipelineAbort(f"local_path does not exist for: {names}", exit_code=4)


def check_drive_subpath_consistency(ledger_obj: ledger_mod.Ledger, sync_set: SyncSet) -> str | None:
    """Step 1 safeguard: catch configs that have drifted apart between setups."""
    state = ledger_obj.sync_sets.get(sync_set.name)
    if state is None or not state.drive_subpath:
        return None
    if state.drive_subpath != sync_set.drive_subpath:
        return (
            f"sync_set {sync_set.name!r} has drive_subpath={sync_set.drive_subpath!r} "
            f"in this setup's config, but the ledger recorded {state.drive_subpath!r} "
            f"from a previous run — the two setups' configs disagree on where this "
            f"set lives on the drive"
        )
    return None


def find_unconfigured_sets(
    ledger_obj: ledger_mod.Ledger, config: Config
) -> list[tuple[str, ledger_mod.SyncSetState]]:
    """Sync sets the drive knows about that this setup's config doesn't have.

    This is how one setup notices a set that was added on the other. Not an
    error: SPEC.md §12 allows sets that only one setup configures.
    """
    configured = {s.name for s in config.sync_sets}
    return [(name, state) for name, state in ledger_obj.sync_sets.items() if name not in configured]


@dataclass
class RegisterResult:
    registered: list[str]
    conflicts: list[str]  # human-readable drive_subpath disagreements, same wording as Step 1


def register_new_sets(config: Config, sync_sets: Sequence[SyncSet] | None = None) -> RegisterResult:
    """Record sets in the drive's ledger so the other setup can discover them.

    Called when sets are added to the config. Raises DriveError/LedgerError if
    the drive can't be reached; callers treat that as "will be registered by the
    next recall" rather than a failure. A set whose name is already on the drive
    is left alone — and reported as a conflict if its drive_subpath disagrees.
    """
    sync_sets = list(config.sync_sets if sync_sets is None else sync_sets)
    info = drive_mod.ensure_mounted(config.drive.uuid)
    hostname = ledger_mod.current_hostname(config.setup.hostname_override)
    ledger_obj = ledger_mod.load(info.mount_point)

    registered: list[str] = []
    conflicts: list[str] = []
    for s in sync_sets:
        mismatch = check_drive_subpath_consistency(ledger_obj, s)
        if mismatch:
            conflicts.append(mismatch)
        elif ledger_mod.register_set(ledger_obj, s.name, s.drive_subpath, hostname):
            registered.append(s.name)

    if registered:
        ledger_mod.save(info.mount_point, ledger_obj)
    return RegisterResult(registered=registered, conflicts=conflicts)


def evaluate_warning(ledger_obj: ledger_mod.Ledger, sync_set: SyncSet, action: Action, hostname: str) -> str | None:
    """Step 2: warn about likely-redundant or stale operations."""
    state = ledger_obj.sync_sets.get(sync_set.name)
    if state is None:
        return None
    if action == "deploy" and state.last_action == "recall" and state.last_hostname == hostname:
        return f"{sync_set.name!r}: you're pulling data you just pushed from this machine; likely redundant."
    if action == "deploy" and state.last_action == "deploy" and state.last_hostname == hostname:
        return f"{sync_set.name!r}: drive has no new data since your last deploy."
    return None


def run(
    config: Config,
    action: Action,
    only: str | Sequence[str] | None = None,
    dry_run: bool = False,
    assume_yes: bool = False,
    confirm: ConfirmFn = _default_confirm,
    on_line: LineFn | None = None,
    on_set_start: SetStartFn | None = None,
) -> RunReport:
    """Run the 4-step safe execution pipeline (SPEC.md §8).

    `on_line`, if given, receives (set_name, output_line) for every line of
    rsync output instead of it going to stdout — used by the GUI to route
    output into a log widget from a background thread.
    """
    sync_sets = select_sync_sets(config, only)
    validate_local_paths(sync_sets)

    info = drive_mod.ensure_mounted(config.drive.uuid)
    hostname = ledger_mod.current_hostname(config.setup.hostname_override)
    ledger_obj = ledger_mod.load(info.mount_point)

    outcomes: list[SetOutcome] = []
    for s in sync_sets:
        mismatch = check_drive_subpath_consistency(ledger_obj, s)
        if mismatch:
            outcomes.append(SetOutcome(s.name, None, True, mismatch, None))
            continue

        warning = evaluate_warning(ledger_obj, s, action, hostname)
        if warning and not assume_yes and not dry_run:
            if not confirm(warning):
                outcomes.append(SetOutcome(s.name, warning, True, "aborted by user", None))
                continue

        if on_set_start:
            on_set_start(s.name, action)

        local_path = Path(s.local_path)
        drive_target = info.mount_point / s.drive_subpath

        if action == "recall":
            drive_target.mkdir(parents=True, exist_ok=True)
            cmd = engine.build_recall_command(local_path, drive_target, dry_run=dry_run)
        else:
            if not drive_target.exists():
                outcomes.append(
                    SetOutcome(
                        s.name,
                        warning,
                        True,
                        f"drive_subpath {s.drive_subpath!r} does not exist on the drive yet "
                        f"(nothing has been recalled for this set)",
                        None,
                    )
                )
                continue
            cmd = engine.build_deploy_command(drive_target, local_path, dry_run=dry_run)

        line_cb = (lambda line, _name=s.name: on_line(_name, line)) if on_line else None
        result = engine.run_rsync(cmd, stream=(on_line is None), on_line=line_cb)
        outcomes.append(SetOutcome(s.name, warning, False, None, result))

        if result.ok and not dry_run:
            ledger_mod.record_action(ledger_obj, s.name, s.drive_subpath, action, hostname)

    if not dry_run:
        ledger_mod.save(info.mount_point, ledger_obj)

    return RunReport(action=action, drive_mount=info.mount_point, outcomes=outcomes)
