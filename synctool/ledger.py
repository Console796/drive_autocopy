from __future__ import annotations

import json
import os
import socket
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

LEDGER_FILENAME = ".sync_state.json"
LEDGER_VERSION = 2
MAX_HISTORY_PER_SET = 50
REGISTER_ACTION = "add"  # set configured on a setup, nothing transferred yet


class LedgerError(Exception):
    pass


@dataclass
class LedgerEntry:
    action: str
    hostname: str
    timestamp: str


@dataclass
class SyncSetState:
    drive_subpath: str
    last_action: str
    last_hostname: str
    last_timestamp: str
    history: list[LedgerEntry] = field(default_factory=list)


@dataclass
class Ledger:
    version: int = LEDGER_VERSION
    sync_sets: dict[str, SyncSetState] = field(default_factory=dict)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def current_hostname(override: str = "") -> str:
    return override or socket.gethostname()


def ledger_path(drive_mount: Path) -> Path:
    return drive_mount / LEDGER_FILENAME


def load(drive_mount: Path) -> Ledger:
    path = ledger_path(drive_mount)
    if not path.exists():
        return Ledger()

    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise LedgerError(f"corrupt ledger at {path}: {exc}") from exc

    version = raw.get("version", 1)
    if version > LEDGER_VERSION:
        raise LedgerError(
            f"ledger at {path} is version {version}, newer than this tool supports "
            f"({LEDGER_VERSION}); upgrade synctool"
        )
    if version < LEDGER_VERSION:
        raise LedgerError(
            f"ledger at {path} is version {version} (pre per-sync-set schema); "
            f"migrate it manually before continuing"
        )

    sync_sets: dict[str, SyncSetState] = {}
    for name, entry in raw.get("sync_sets", {}).items():
        history = [LedgerEntry(**h) for h in entry.get("history", [])]
        sync_sets[name] = SyncSetState(
            drive_subpath=entry.get("drive_subpath", ""),
            last_action=entry["last_action"],
            last_hostname=entry["last_hostname"],
            last_timestamp=entry["last_timestamp"],
            history=history,
        )
    return Ledger(version=LEDGER_VERSION, sync_sets=sync_sets)


def save(drive_mount: Path, ledger: Ledger) -> None:
    path = ledger_path(drive_mount)
    payload = {
        "version": ledger.version,
        "sync_sets": {
            name: {
                "drive_subpath": state.drive_subpath,
                "last_action": state.last_action,
                "last_hostname": state.last_hostname,
                "last_timestamp": state.last_timestamp,
                "history": [
                    {"action": h.action, "hostname": h.hostname, "timestamp": h.timestamp}
                    for h in state.history
                ],
            }
            for name, state in ledger.sync_sets.items()
        },
    }

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".sync_state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def signature(drive_mount: Path) -> tuple[int, int] | None:
    """Cheap change-detector for the ledger file: (mtime_ns, size), or None if absent."""
    try:
        st = ledger_path(drive_mount).stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def register_set(
    ledger: Ledger,
    name: str,
    drive_subpath: str,
    hostname: str,
    timestamp: str | None = None,
) -> bool:
    """Announce a newly configured sync set on the drive before its first transfer.

    Lets the other setup discover the set without waiting for a first recall.
    No-op (returns False) if the ledger already has an entry for `name`, so an
    existing set's history and drive_subpath are never touched.
    """
    if name in ledger.sync_sets:
        return False
    record_action(ledger, name, drive_subpath, REGISTER_ACTION, hostname, timestamp)
    return True


def record_action(
    ledger: Ledger,
    name: str,
    drive_subpath: str,
    action: str,
    hostname: str,
    timestamp: str | None = None,
) -> None:
    timestamp = timestamp or now_iso()
    entry = LedgerEntry(action=action, hostname=hostname, timestamp=timestamp)

    state = ledger.sync_sets.get(name)
    if state is None:
        ledger.sync_sets[name] = SyncSetState(
            drive_subpath=drive_subpath,
            last_action=action,
            last_hostname=hostname,
            last_timestamp=timestamp,
            history=[entry],
        )
        return

    state.drive_subpath = drive_subpath
    state.last_action = action
    state.last_hostname = hostname
    state.last_timestamp = timestamp
    state.history.append(entry)
    if len(state.history) > MAX_HISTORY_PER_SET:
        state.history = state.history[-MAX_HISTORY_PER_SET:]
