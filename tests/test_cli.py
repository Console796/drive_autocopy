from pathlib import Path

import pytest

from synctool import cli
from synctool import config as config_mod
from synctool import drive as drive_mod
from synctool import ledger as ledger_mod


@pytest.fixture
def drive_root(tmp_path, monkeypatch):
    drive_dir = tmp_path / "drive"
    drive_dir.mkdir()

    def fake_ensure_mounted(uuid: str) -> drive_mod.DriveInfo:
        return drive_mod.DriveInfo(uuid=uuid, device=Path("/dev/fake"), mount_point=drive_dir)

    monkeypatch.setattr(cli.drive_mod, "ensure_mounted", fake_ensure_mounted)
    return drive_dir


def _feed_input(monkeypatch, answers: list[str]):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


def test_sync_sets_from_ledger_imports_with_defaults(drive_root, monkeypatch):
    led = ledger_mod.Ledger()
    ledger_mod.record_action(led, "projects", "projects", "recall", "setup-a", timestamp="2026-08-22T10:00:00Z")
    ledger_mod.record_action(led, "dotfiles", "dotfiles/nvim", "recall", "setup-a", timestamp="2026-08-22T10:05:00Z")
    ledger_mod.save(drive_root, led)

    # Accept the import, then accept every default (blank input) for both sets.
    _feed_input(monkeypatch, ["y", "", "", "", ""])

    sync_sets = cli._sync_sets_from_ledger("1234-ABCD")

    assert [s.name for s in sync_sets] == ["projects", "dotfiles"]
    assert sync_sets[0].drive_subpath == "projects"
    assert sync_sets[0].local_path == str(Path.home() / "projects")
    assert sync_sets[1].drive_subpath == "dotfiles/nvim"
    assert sync_sets[1].local_path == str(Path.home() / "dotfiles")


def test_sync_sets_from_ledger_lets_user_override_defaults(drive_root, monkeypatch):
    led = ledger_mod.Ledger()
    ledger_mod.record_action(led, "projects", "projects", "recall", "setup-a", timestamp="2026-08-22T10:00:00Z")
    ledger_mod.save(drive_root, led)

    _feed_input(monkeypatch, ["y", "/data/dev/projects", "projects-renamed"])

    sync_sets = cli._sync_sets_from_ledger("1234-ABCD")

    assert sync_sets == [
        config_mod.SyncSet(name="projects", local_path="/data/dev/projects", drive_subpath="projects-renamed")
    ]


def test_sync_sets_from_ledger_declined_returns_empty(drive_root, monkeypatch):
    led = ledger_mod.Ledger()
    ledger_mod.record_action(led, "projects", "projects", "recall", "setup-a", timestamp="2026-08-22T10:00:00Z")
    ledger_mod.save(drive_root, led)

    _feed_input(monkeypatch, ["n"])

    assert cli._sync_sets_from_ledger("1234-ABCD") == []


def test_sync_sets_from_ledger_no_ledger_file_returns_empty(drive_root):
    # drive_root exists but has no .sync_state.json yet
    assert cli._sync_sets_from_ledger("1234-ABCD") == []


def test_sync_sets_from_ledger_drive_not_connected_returns_empty(monkeypatch):
    def fake_ensure_mounted(uuid: str) -> drive_mod.DriveInfo:
        raise drive_mod.DriveError("drive not connected")

    monkeypatch.setattr(cli.drive_mod, "ensure_mounted", fake_ensure_mounted)

    assert cli._sync_sets_from_ledger("1234-ABCD") == []


def test_status_lists_sets_on_drive_that_are_not_configured_here(drive_root, tmp_path, monkeypatch, capsys):
    led = ledger_mod.Ledger()
    ledger_mod.register_set(led, "notes", "docs/notes", "setup-a", timestamp="2026-10-02T10:00:00Z")
    ledger_mod.register_set(led, "projects", "projects", "setup-a", timestamp="2026-10-02T10:00:00Z")
    ledger_mod.save(drive_root, led)

    local = tmp_path / "proj"
    local.mkdir()
    cfg_path = tmp_path / "config.toml"
    config_mod.write_config(
        cfg_path,
        config_mod.Config(
            drive=config_mod.DriveConfig(uuid="1234-ABCD"),
            setup=config_mod.SetupConfig(hostname_override="setup-b"),
            sync_sets=[config_mod.SyncSet("projects", str(local), "projects")],
        ),
    )
    monkeypatch.setattr(cli.drive_mod, "find_drive", lambda uuid: None)

    assert cli.main(["--config", str(cfg_path), "status"]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "not configured on this machine" in out
    assert "'notes'" in out and "docs/notes" in out
    assert 'name = "notes"' in out
    assert "'projects': drive_subpath" not in out  # configured set isn't listed as new
