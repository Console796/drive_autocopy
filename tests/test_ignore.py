import shutil
from pathlib import Path

import pytest

from synctool import config as config_mod
from synctool import drive as drive_mod
from synctool import engine, ignore, pipeline


def test_parse_skips_blanks_and_comments_and_trims_trailing_space():
    assert ignore.parse("# build output\n\n*.log   \n  \nnode_modules/\n") == ["- node_modules/", "- *.log"]


def test_parse_reverses_order_and_turns_negation_into_include():
    # gitignore is last-match-wins; rsync is first-match-wins, so order flips.
    assert ignore.parse("*.log\n!keep.log\n") == ["+ keep.log", "- *.log"]


def test_parse_ignores_bare_bang():
    assert ignore.parse("!\n*.tmp\n") == ["- *.tmp"]


def test_load_missing_file_means_no_rules(tmp_path):
    assert ignore.load(tmp_path) == []


def test_load_unreadable_file_is_an_error_not_silently_empty(tmp_path):
    (tmp_path / ignore.IGNORE_FILENAME).write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ignore.IgnoreError):
        ignore.load(tmp_path)


def test_filter_args_and_command_placement():
    args = ignore.filter_args(["+ keep.log", "- *.log"])
    assert args == ["--filter=+ keep.log", "--filter=- *.log"]

    recall = engine.build_recall_command(Path("/a"), Path("/b"), dry_run=True, filter_args=args)
    assert recall == ["rsync", "-auv", "--delete", *args, "-n", "/a/", "/b/"]
    deploy = engine.build_deploy_command(Path("/b"), Path("/a"), filter_args=args)
    assert deploy == ["rsync", "-auv", *args, "/b/", "/a/"]


# ---- end to end with the real rsync -------------------------------------------

needs_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


@pytest.fixture
def drive_root(tmp_path, monkeypatch):
    drive_dir = tmp_path / "drive"
    drive_dir.mkdir()
    monkeypatch.setattr(
        pipeline.drive_mod,
        "ensure_mounted",
        lambda uuid: drive_mod.DriveInfo(uuid=uuid, device=Path("/dev/fake"), mount_point=drive_dir),
    )
    return drive_dir


def _cfg(local: Path, host: str) -> config_mod.Config:
    return config_mod.Config(
        drive=config_mod.DriveConfig(uuid="1234-ABCD"),
        setup=config_mod.SetupConfig(hostname_override=host),
        sync_sets=[config_mod.SyncSet("projects", str(local), "projects")],
    )


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def _make_tree(root: Path) -> None:
    for rel in (
        "a.py",
        "b.log",
        "keep.log",
        "sub/c.log",
        "node_modules/x/i.js",
        "sub/node_modules/y.js",
        "build/o",
        "sub/build/o",
        ".syncignore",
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x")
    (root / ".syncignore").write_text("# junk\n*.log\n!keep.log\nnode_modules/\n/build/\n")


@needs_rsync
def test_recall_honours_syncignore_and_ships_it_to_the_drive(tmp_path, drive_root):
    local = tmp_path / "a"
    _make_tree(local)

    report = pipeline.run(_cfg(local, "setup-a"), action="recall")

    assert report.ok
    assert _files(drive_root / "projects") == {"a.py", "keep.log", "sub/build/o", ".syncignore"}


@needs_rsync
def test_deploy_uses_the_drives_syncignore_and_other_setup_gets_same_result(tmp_path, drive_root):
    local_a = tmp_path / "a"
    _make_tree(local_a)
    pipeline.run(_cfg(local_a, "setup-a"), action="recall")

    # Something the drive's ignore list covers, sitting on the drive anyway
    # (e.g. recalled before the rule existed): deploy must not pull it.
    (drive_root / "projects" / "stale.log").write_text("old")

    local_b = tmp_path / "b"
    local_b.mkdir()
    report = pipeline.run(_cfg(local_b, "setup-b"), action="deploy")

    assert report.ok
    assert _files(local_b) == {"a.py", "keep.log", "sub/build/o", ".syncignore"}


@needs_rsync
def test_newly_ignored_file_already_on_drive_is_not_deleted_by_recall(tmp_path, drive_root):
    local = tmp_path / "a"
    local.mkdir()
    (local / "a.py").write_text("x")
    (local / "scratch.tmp").write_text("x")
    pipeline.run(_cfg(local, "setup-a"), action="recall")
    assert "scratch.tmp" in _files(drive_root / "projects")

    (local / ".syncignore").write_text("*.tmp\n")
    pipeline.run(_cfg(local, "setup-a"), action="recall")

    assert "scratch.tmp" in _files(drive_root / "projects")  # protected, not mirrored-away


@needs_rsync
def test_dry_run_preview_respects_syncignore(tmp_path, drive_root):
    local = tmp_path / "a"
    local.mkdir()
    (local / "a.py").write_text("x")
    (local / "junk.tmp").write_text("x")
    (local / ".syncignore").write_text("*.tmp\n")

    report = pipeline.run(_cfg(local, "setup-a"), action="recall", dry_run=True)

    output = report.outcomes[0].rsync_result.output
    assert "a.py" in output and "junk.tmp" not in output


def test_unreadable_syncignore_skips_the_set_instead_of_syncing_everything(tmp_path, drive_root, monkeypatch):
    local = tmp_path / "a"
    local.mkdir()
    (local / ignore.IGNORE_FILENAME).write_bytes(b"\xff\xfe\x00bad")
    called = []
    monkeypatch.setattr(pipeline.engine, "run_rsync", lambda *a, **k: called.append(a))

    report = pipeline.run(_cfg(local, "setup-a"), action="recall")

    assert report.outcomes[0].skipped and "can't read" in report.outcomes[0].skip_reason
    assert called == []
    assert not (drive_root / ".sync_state.json").exists() or "projects" not in pipeline.ledger_mod.load(drive_root).sync_sets
