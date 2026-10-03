from pathlib import Path

import pytest

from synctool import drive


def test_resolve_device_missing_raises(tmp_path: Path):
    by_uuid_dir = tmp_path / "by-uuid"
    by_uuid_dir.mkdir()
    with pytest.raises(drive.DriveError):
        drive.resolve_device("does-not-exist", by_uuid_dir=by_uuid_dir)


def test_resolve_device_follows_symlink(tmp_path: Path):
    by_uuid_dir = tmp_path / "by-uuid"
    by_uuid_dir.mkdir()
    fake_device = tmp_path / "fake-sdb1"
    fake_device.write_text("")
    (by_uuid_dir / "1234-ABCD").symlink_to(fake_device)

    resolved = drive.resolve_device("1234-ABCD", by_uuid_dir=by_uuid_dir)
    assert resolved == fake_device.resolve()


def test_find_mount_point_matches_device(tmp_path: Path):
    device = tmp_path / "fake-sdb1"
    device.write_text("")
    mount_dir = tmp_path / "media" / "synctool-mount"
    mount_dir.mkdir(parents=True)

    mounts_file = tmp_path / "mounts"
    mounts_file.write_text(
        f"proc /proc proc rw 0 0\n"
        f"{device} {mount_dir} ext4 rw 0 0\n"
        f"tmpfs /run tmpfs rw 0 0\n"
    )

    result = drive.find_mount_point(device, mounts_path=mounts_file)
    assert result == mount_dir


def test_find_mount_point_no_match_returns_none(tmp_path: Path):
    device = tmp_path / "fake-sdb1"
    device.write_text("")
    mounts_file = tmp_path / "mounts"
    mounts_file.write_text("proc /proc proc rw 0 0\n")

    result = drive.find_mount_point(device, mounts_path=mounts_file)
    assert result is None


def test_find_mount_point_missing_mounts_file_returns_none(tmp_path: Path):
    device = tmp_path / "fake-sdb1"
    result = drive.find_mount_point(device, mounts_path=tmp_path / "does-not-exist")
    assert result is None


# --- friendly drive enumeration -------------------------------------------

LSBLK_SAMPLE = """
{
  "blockdevices": [
    {"name": "loop0", "kname": "loop0", "size": "406.2M", "type": "loop", "rm": false,
     "label": null, "fstype": null, "uuid": null, "mountpoint": "/snap/x",
     "model": null, "vendor": null, "tran": null},
    {"name": "sdb", "kname": "sdb", "size": "4.5T", "type": "disk", "rm": false,
     "label": null, "fstype": null, "uuid": null, "mountpoint": null,
     "model": "BUP BK          ", "vendor": "Seagate ", "tran": "usb",
     "children": [
       {"name": "sdb1", "kname": "sdb1", "size": "4.5T", "type": "part", "rm": false,
        "label": "RADIA", "fstype": "ext4", "uuid": "uuid-radia", "mountpoint": "/mnt/radia",
        "model": null, "vendor": null, "tran": null}
     ]},
    {"name": "nvme0n1", "kname": "nvme0n1", "size": "931.5G", "type": "disk", "rm": false,
     "label": null, "fstype": null, "uuid": null, "mountpoint": null,
     "model": "KINGSTON SNV2S1000G", "vendor": null, "tran": "nvme",
     "children": [
       {"name": "nvme0n1p2", "kname": "nvme0n1p2", "size": "931.2G", "type": "part", "rm": false,
        "label": "kubuntu", "fstype": "ext4", "uuid": "uuid-internal", "mountpoint": "/",
        "model": null, "vendor": null, "tran": null}
     ]}
  ]
}
"""


def test_parse_lsblk_inherits_hardware_details_from_parent_disk():
    meta = drive.parse_lsblk_metadata(LSBLK_SAMPLE)
    partition = meta["sdb1"]
    assert partition["model"] == "BUP BK"       # trailing padding stripped
    assert partition["vendor"] == "Seagate"
    assert partition["transport"] == "usb"      # inherited from the disk
    assert partition["size"] == "4.5T"
    assert meta["/dev/sdb1"] is partition        # addressable both ways


def test_parse_lsblk_handles_invalid_json():
    assert drive.parse_lsblk_metadata("not json") == {}


def _make_link_dirs(tmp_path: Path, uuids: dict[str, str], labels: dict[str, str] | None = None):
    dev_dir = tmp_path / "dev"
    dev_dir.mkdir(exist_ok=True)
    by_uuid = tmp_path / "by-uuid"
    by_uuid.mkdir(exist_ok=True)
    by_label = tmp_path / "by-label"
    by_label.mkdir(exist_ok=True)

    for uuid, devname in uuids.items():
        device = dev_dir / devname
        device.touch(exist_ok=True)
        (by_uuid / uuid).symlink_to(device)
    for label, devname in (labels or {}).items():
        (by_label / label).symlink_to(dev_dir / devname)
    return by_uuid, by_label


def test_list_drives_combines_uuid_labels_and_lsblk(tmp_path: Path):
    by_uuid, by_label = _make_link_dirs(
        tmp_path,
        uuids={"uuid-radia": "sdb1", "uuid-internal": "nvme0n1p2"},
        labels={"RADIA": "sdb1"},
    )

    drives = drive.list_drives(by_uuid_dir=by_uuid, by_label_dir=by_label, lsblk_json=LSBLK_SAMPLE)
    names = [d.display_name for d in drives]
    assert names[0] == "RADIA"  # external sorts first

    radia = drives[0]
    assert radia.uuid == "uuid-radia"
    assert radia.size == "4.5T"
    assert radia.bus == "USB"
    assert radia.product == "Seagate BUP BK"
    assert radia.is_external
    assert "RADIA" in radia.describe() and "4.5T" in radia.describe()

    internal = [d for d in drives if d.uuid == "uuid-internal"][0]
    assert not internal.is_external
    assert internal.bus == "NVMe"


def test_list_drives_without_lsblk_still_lists_uuids(tmp_path: Path):
    by_uuid, by_label = _make_link_dirs(tmp_path, uuids={"uuid-x": "sdb1"}, labels={"MYDRIVE": "sdb1"})

    drives = drive.list_drives(by_uuid_dir=by_uuid, by_label_dir=by_label, lsblk_json="")
    assert len(drives) == 1
    # Label still resolves from /dev/disk/by-label even with no lsblk metadata.
    assert drives[0].display_name == "MYDRIVE"
    assert drives[0].uuid == "uuid-x"


def test_display_name_falls_back_to_hardware_then_device():
    hardware_only = drive.DriveCandidate(
        uuid="u", device=Path("/dev/sdb1"), vendor="SanDisk", model="Extreme"
    )
    assert hardware_only.display_name == "SanDisk Extreme"

    bare = drive.DriveCandidate(uuid="u", device=Path("/dev/sdb1"))
    assert bare.display_name == "sdb1"


def test_removable_flag_marks_external_even_without_usb_transport():
    candidate = drive.DriveCandidate(uuid="u", device=Path("/dev/sdb1"), removable=True)
    assert candidate.is_external


def test_find_drive_returns_none_when_not_connected(tmp_path: Path):
    by_uuid, by_label = _make_link_dirs(tmp_path, uuids={"uuid-a": "sdb1"})
    kwargs = {"by_uuid_dir": by_uuid, "by_label_dir": by_label, "lsblk_json": LSBLK_SAMPLE}

    assert drive.find_drive("uuid-a", **kwargs) is not None
    assert drive.find_drive("uuid-missing", **kwargs) is None


def test_find_existing_mount_never_mounts_and_handles_unplugged(tmp_path: Path, monkeypatch):
    by_uuid = tmp_path / "by-uuid"
    by_uuid.mkdir()
    assert drive.find_existing_mount("1234-ABCD", by_uuid_dir=by_uuid) is None  # unplugged

    device = tmp_path / "sdb1"
    device.touch()
    (by_uuid / "1234-ABCD").symlink_to(device)
    monkeypatch.setattr(drive, "find_mount_point", lambda dev: Path("/media/x") if dev == device.resolve() else None)
    monkeypatch.setattr(drive, "mount_device", lambda dev: pytest.fail("must not mount"))

    assert drive.find_existing_mount("1234-ABCD", by_uuid_dir=by_uuid) == Path("/media/x")
