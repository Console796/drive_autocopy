from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass
class DriveConfig:
    uuid: str
    label: str = ""
    mount_point: str = ""


@dataclass
class SetupConfig:
    hostname_override: str = ""


@dataclass
class SyncSet:
    name: str
    local_path: str
    drive_subpath: str


@dataclass
class Config:
    drive: DriveConfig
    setup: SetupConfig
    sync_sets: list[SyncSet] = field(default_factory=list)

    def get_sync_set(self, name: str) -> SyncSet | None:
        for s in self.sync_sets:
            if s.name == name:
                return s
        return None


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "synctool" / "config.toml"


def load_config(path: Path | None = None) -> Config:
    path = path or default_config_path()
    if not path.exists():
        raise ConfigError(f"no config found at {path}; run `synctool init` first")

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    try:
        drive_raw = raw["drive"]
        drive = DriveConfig(
            uuid=drive_raw["uuid"],
            label=drive_raw.get("label", ""),
            mount_point=drive_raw.get("mount_point", ""),
        )
    except KeyError as exc:
        raise ConfigError(f"missing required [drive] field in {path}: {exc}") from exc

    setup_raw = raw.get("setup", {})
    setup = SetupConfig(hostname_override=setup_raw.get("hostname_override", ""))

    sync_sets: list[SyncSet] = []
    seen_names: set[str] = set()
    for entry in raw.get("sync_sets", []):
        try:
            name = entry["name"]
            local_path = entry["local_path"]
            drive_subpath = entry["drive_subpath"]
        except KeyError as exc:
            raise ConfigError(f"sync_sets entry missing required field: {exc}") from exc
        if name in seen_names:
            raise ConfigError(f"duplicate sync_set name: {name!r}")
        seen_names.add(name)
        sync_sets.append(SyncSet(name=name, local_path=local_path, drive_subpath=drive_subpath))

    if not sync_sets:
        raise ConfigError(f"no [[sync_sets]] configured in {path}")

    return Config(drive=drive, setup=setup, sync_sets=sync_sets)


def write_config(path: Path, config: Config) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[drive]",
        f'uuid = "{config.drive.uuid}"',
        f'label = "{config.drive.label}"',
        f'mount_point = "{config.drive.mount_point}"',
        "",
        "[setup]",
        f'hostname_override = "{config.setup.hostname_override}"',
        "",
    ]
    for s in config.sync_sets:
        lines += [
            "[[sync_sets]]",
            f'name = "{s.name}"',
            f'local_path = "{s.local_path}"',
            f'drive_subpath = "{s.drive_subpath}"',
            "",
        ]
    path.write_text("\n".join(lines))
