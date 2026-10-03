from pathlib import Path

import pytest

from synctool import config as config_mod

VALID_TOML = """
[drive]
uuid = "1234-ABCD"
label = "SYNCDRIVE"
mount_point = "/media/synctool-mount"

[setup]
hostname_override = ""

[[sync_sets]]
name = "projects"
local_path = "/home/jason/projects"
drive_subpath = "projects"

[[sync_sets]]
name = "dotfiles"
local_path = "/home/jason/.config/nvim"
drive_subpath = "dotfiles/nvim"
"""


def test_load_config_parses_sync_sets(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(VALID_TOML)

    cfg = config_mod.load_config(path)
    assert cfg.drive.uuid == "1234-ABCD"
    assert len(cfg.sync_sets) == 2
    assert cfg.get_sync_set("projects").local_path == "/home/jason/projects"
    assert cfg.get_sync_set("missing") is None


def test_load_config_missing_file_raises(tmp_path: Path):
    with pytest.raises(config_mod.ConfigError):
        config_mod.load_config(tmp_path / "nope.toml")


def test_load_config_missing_drive_section_raises(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("[[sync_sets]]\nname='a'\nlocal_path='/x'\ndrive_subpath='a'\n")
    with pytest.raises(config_mod.ConfigError):
        config_mod.load_config(path)


def test_load_config_no_sync_sets_raises(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[drive]\nuuid = "1234"\n')
    with pytest.raises(config_mod.ConfigError):
        config_mod.load_config(path)


def test_load_config_duplicate_names_raises(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[drive]\nuuid = "1234"\n'
        '[[sync_sets]]\nname = "a"\nlocal_path = "/x"\ndrive_subpath = "a"\n'
        '[[sync_sets]]\nname = "a"\nlocal_path = "/y"\ndrive_subpath = "a2"\n'
    )
    with pytest.raises(config_mod.ConfigError):
        config_mod.load_config(path)


def test_write_config_roundtrip(tmp_path: Path):
    path = tmp_path / "config.toml"
    cfg = config_mod.Config(
        drive=config_mod.DriveConfig(uuid="ABCD-1234", label="SYNCDRIVE", mount_point="/media/x"),
        setup=config_mod.SetupConfig(hostname_override="setup-a"),
        sync_sets=[config_mod.SyncSet(name="projects", local_path="/home/j/projects", drive_subpath="projects")],
    )
    config_mod.write_config(path, cfg)

    reloaded = config_mod.load_config(path)
    assert reloaded.drive.uuid == "ABCD-1234"
    assert reloaded.setup.hostname_override == "setup-a"
    assert reloaded.sync_sets[0].name == "projects"


def test_default_config_path_uses_xdg_config_home(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_mod.default_config_path() == tmp_path / "synctool" / "config.toml"
