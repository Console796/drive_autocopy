from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

BY_UUID_DIR = Path("/dev/disk/by-uuid")
BY_LABEL_DIR = Path("/dev/disk/by-label")

# Buses that indicate a drive you can physically unplug, i.e. a plausible
# sneakernet drive. `rm` (the kernel "removable" flag) is unreliable — plenty
# of USB disks report rm=0 — so transport is the better signal.
EXTERNAL_TRANSPORTS = {"usb", "ieee1394", "mmc"}

_BUS_NAMES = {
    "usb": "USB",
    "nvme": "NVMe",
    "sata": "SATA",
    "ata": "ATA",
    "scsi": "SCSI",
    "mmc": "SD/MMC",
    "ieee1394": "FireWire",
}

_LSBLK_COLUMNS = "NAME,KNAME,SIZE,TYPE,RM,LABEL,FSTYPE,UUID,MOUNTPOINT,MODEL,VENDOR,TRAN"


class DriveError(Exception):
    pass


@dataclass
class DriveInfo:
    uuid: str
    device: Path
    mount_point: Path


@dataclass
class DriveCandidate:
    """A mountable filesystem with human-readable identifying details."""

    uuid: str
    device: Path
    label: str = ""
    size: str = ""
    fstype: str = ""
    mountpoint: str = ""
    model: str = ""
    vendor: str = ""
    transport: str = ""
    removable: bool = False

    @property
    def product(self) -> str:
        """e.g. 'SanDisk Extreme 55AE' — the hardware, not the filesystem."""
        return " ".join(part for part in (self.vendor, self.model) if part).strip()

    @property
    def display_name(self) -> str:
        """Best human-readable name: filesystem label, else hardware, else device."""
        return self.label or self.product or self.device.name

    @property
    def bus(self) -> str:
        return _BUS_NAMES.get(self.transport, self.transport.upper() if self.transport else "")

    @property
    def is_external(self) -> bool:
        return self.removable or self.transport in EXTERNAL_TRANSPORTS

    def describe(self) -> str:
        """One-line summary for menus and status output."""
        bits = [self.display_name]
        details = [d for d in (self.size, self.bus) if d]
        if self.label and self.product:
            details.append(self.product)
        if details:
            bits.append(f"({', '.join(details)})")
        bits.append(f"— {self.device}")
        if self.mountpoint:
            bits.append(f"mounted at {self.mountpoint}")
        return " ".join(bits)


def _read_link_dir(directory: Path) -> dict[str, str]:
    """Map resolved device path -> symlink name for /dev/disk/by-* directories."""
    result: dict[str, str] = {}
    if not directory.exists():
        return result
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return result
    for link in entries:
        try:
            result[str(link.resolve())] = link.name
        except OSError:
            continue
    return result


def _run_lsblk() -> str | None:
    try:
        result = subprocess.run(
            ["lsblk", "--json", "-o", _LSBLK_COLUMNS],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def parse_lsblk_metadata(json_text: str) -> dict[str, dict]:
    """Flatten lsblk's device tree into a lookup keyed by device name and path.

    Partitions inherit model/vendor/transport from their parent disk, since
    lsblk only reports those on the disk itself.
    """
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return {}

    out: dict[str, dict] = {}

    def walk(node: dict, inherited: dict) -> None:
        info = {
            "size": node.get("size") or "",
            "type": node.get("type") or "",
            "fstype": node.get("fstype") or "",
            "label": node.get("label") or "",
            "uuid": node.get("uuid") or "",
            "mountpoint": node.get("mountpoint") or "",
            "model": (node.get("model") or inherited.get("model") or "").strip(),
            "vendor": (node.get("vendor") or inherited.get("vendor") or "").strip(),
            "transport": node.get("tran") or inherited.get("transport") or "",
            "removable": bool(node.get("rm")) or bool(inherited.get("removable")),
        }
        for key in (node.get("name"), node.get("kname")):
            if key:
                out[key] = info
                out[f"/dev/{key}"] = info
        for child in node.get("children", []):
            walk(child, info)

    for device in data.get("blockdevices", []):
        walk(device, {})
    return out


def list_drives(
    by_uuid_dir: Path = BY_UUID_DIR,
    by_label_dir: Path = BY_LABEL_DIR,
    lsblk_json: str | None = None,
) -> list[DriveCandidate]:
    """Enumerate mountable filesystems with friendly names, external drives first.

    UUIDs and labels come from /dev/disk/by-*, which stay readable even where
    lsblk's own UUID/LABEL fields come back empty; lsblk then supplies the
    size/model/bus details that make an entry recognisable to a human.
    """
    uuid_by_device = _read_link_dir(by_uuid_dir)
    label_by_device = _read_link_dir(by_label_dir)

    if lsblk_json is None:
        lsblk_json = _run_lsblk()
    metadata = parse_lsblk_metadata(lsblk_json) if lsblk_json else {}

    candidates: list[DriveCandidate] = []
    for device_str, uuid in uuid_by_device.items():
        device = Path(device_str)
        meta = metadata.get(device_str) or metadata.get(device.name) or {}
        if meta.get("type") in ("loop", "rom"):
            continue
        if meta.get("fstype") == "swap":
            continue
        candidates.append(
            DriveCandidate(
                uuid=uuid,
                device=device,
                label=label_by_device.get(device_str) or meta.get("label", ""),
                size=meta.get("size", ""),
                fstype=meta.get("fstype", ""),
                mountpoint=meta.get("mountpoint", ""),
                model=meta.get("model", ""),
                vendor=meta.get("vendor", ""),
                transport=meta.get("transport", ""),
                removable=meta.get("removable", False),
            )
        )

    candidates.sort(key=lambda c: (not c.is_external, c.display_name.lower()))
    return candidates


def find_drive(uuid: str, **kwargs) -> DriveCandidate | None:
    """Look up a single drive by UUID, or None if it isn't currently connected."""
    for candidate in list_drives(**kwargs):
        if candidate.uuid == uuid:
            return candidate
    return None


def resolve_device(uuid: str, by_uuid_dir: Path = Path("/dev/disk/by-uuid")) -> Path:
    link = by_uuid_dir / uuid
    if not link.exists():
        raise DriveError(
            f"drive not connected: is it plugged in? "
            f"(run `synctool drives` to see connected drives by name)"
        )
    return link.resolve()


def find_mount_point(device: Path, mounts_path: Path = Path("/proc/mounts")) -> Path | None:
    device = device.resolve() if device.exists() else device
    try:
        lines = mounts_path.read_text().splitlines()
    except FileNotFoundError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        mounted_device_raw, mount_point_raw = parts[0], parts[1]
        mounted_device = Path(mounted_device_raw)
        if mounted_device.exists():
            mounted_device = mounted_device.resolve()
        if mounted_device == device:
            # /proc/mounts escapes spaces as \040 and similar octal escapes
            mount_point = mount_point_raw.encode().decode("unicode_escape") if "\\" in mount_point_raw else mount_point_raw
            return Path(mount_point)
    return None


def find_existing_mount(uuid: str, by_uuid_dir: Path = BY_UUID_DIR) -> Path | None:
    """Where the drive is mounted right now, without ever mounting it.

    Safe to call from a background poll: returns None if the drive is
    unplugged or simply not mounted.
    """
    link = by_uuid_dir / uuid
    if not link.exists():
        return None
    return find_mount_point(link.resolve())


def mount_device(device: Path) -> Path:
    result = subprocess.run(
        ["udisksctl", "mount", "-b", str(device)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise DriveError(f"failed to mount {device}: {result.stderr.strip() or result.stdout.strip()}")

    stdout = result.stdout.strip()
    marker = " at "
    if marker in stdout:
        path_str = stdout.split(marker, 1)[1].rstrip(".")
        return Path(path_str)

    mount_point = find_mount_point(device)
    if mount_point is None:
        raise DriveError(f"mounted {device} but could not determine the resulting mount point")
    return mount_point


def ensure_mounted(uuid: str) -> DriveInfo:
    device = resolve_device(uuid)
    mount_point = find_mount_point(device)
    if mount_point is None:
        mount_point = mount_device(device)
    return DriveInfo(uuid=uuid, device=device, mount_point=mount_point)
