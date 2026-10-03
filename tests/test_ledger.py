from pathlib import Path

import pytest

from synctool import ledger


def test_load_missing_returns_empty_ledger(tmp_path: Path):
    result = ledger.load(tmp_path)
    assert result.version == ledger.LEDGER_VERSION
    assert result.sync_sets == {}


def test_record_action_creates_and_updates_entry(tmp_path: Path):
    led = ledger.Ledger()
    ledger.record_action(led, "projects", "projects", "recall", "setup-a", timestamp="2026-08-22T10:00:00Z")

    state = led.sync_sets["projects"]
    assert state.last_action == "recall"
    assert state.last_hostname == "setup-a"
    assert state.drive_subpath == "projects"
    assert len(state.history) == 1

    ledger.record_action(led, "projects", "projects", "deploy", "setup-b", timestamp="2026-08-23T10:00:00Z")
    state = led.sync_sets["projects"]
    assert state.last_action == "deploy"
    assert state.last_hostname == "setup-b"
    assert len(state.history) == 2


def test_history_capped_per_set(tmp_path: Path):
    led = ledger.Ledger()
    for i in range(ledger.MAX_HISTORY_PER_SET + 10):
        ledger.record_action(led, "projects", "projects", "recall", "setup-a", timestamp=f"t{i}")
    state = led.sync_sets["projects"]
    assert len(state.history) == ledger.MAX_HISTORY_PER_SET
    assert state.history[-1].timestamp == f"t{ledger.MAX_HISTORY_PER_SET + 9}"


def test_save_and_load_roundtrip(tmp_path: Path):
    led = ledger.Ledger()
    ledger.record_action(led, "projects", "projects", "recall", "setup-a", timestamp="2026-08-22T10:00:00Z")
    ledger.record_action(led, "dotfiles", "dotfiles/nvim", "deploy", "setup-b", timestamp="2026-08-21T09:00:00Z")

    ledger.save(tmp_path, led)
    assert (tmp_path / ledger.LEDGER_FILENAME).exists()

    reloaded = ledger.load(tmp_path)
    assert set(reloaded.sync_sets) == {"projects", "dotfiles"}
    assert reloaded.sync_sets["projects"].last_hostname == "setup-a"
    assert reloaded.sync_sets["dotfiles"].drive_subpath == "dotfiles/nvim"


def test_save_is_atomic_no_leftover_tmp_files(tmp_path: Path):
    led = ledger.Ledger()
    ledger.record_action(led, "projects", "projects", "recall", "setup-a")
    ledger.save(tmp_path, led)

    leftovers = list(tmp_path.glob(".sync_state.*.tmp"))
    assert leftovers == []


def test_load_rejects_newer_version(tmp_path: Path):
    (tmp_path / ledger.LEDGER_FILENAME).write_text('{"version": 99, "sync_sets": {}}')
    with pytest.raises(ledger.LedgerError):
        ledger.load(tmp_path)


def test_load_rejects_older_version(tmp_path: Path):
    (tmp_path / ledger.LEDGER_FILENAME).write_text(
        '{"version": 1, "last_action": "recall", "last_hostname": "setup-a", '
        '"last_timestamp": "2026-01-01T00:00:00Z"}'
    )
    with pytest.raises(ledger.LedgerError):
        ledger.load(tmp_path)


def test_current_hostname_prefers_override():
    assert ledger.current_hostname("my-override") == "my-override"
    assert ledger.current_hostname("") != ""


def test_register_set_adds_entry_once(tmp_path):
    led = ledger.Ledger()

    assert ledger.register_set(led, "notes", "notes", "setup-a", timestamp="2026-10-02T10:00:00Z") is True
    assert ledger.register_set(led, "notes", "elsewhere", "setup-b", timestamp="2026-10-02T11:00:00Z") is False

    state = led.sync_sets["notes"]
    assert (state.drive_subpath, state.last_hostname, state.last_action) == ("notes", "setup-a", "add")
    assert len(state.history) == 1


def test_signature_changes_when_ledger_is_rewritten(tmp_path):
    assert ledger.signature(tmp_path) is None

    led = ledger.Ledger()
    ledger.save(tmp_path, led)
    first = ledger.signature(tmp_path)
    assert first is not None

    ledger.register_set(led, "notes", "notes", "setup-a")
    ledger.save(tmp_path, led)
    assert ledger.signature(tmp_path) != first
