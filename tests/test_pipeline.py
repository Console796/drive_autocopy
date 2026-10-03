import shutil
from pathlib import Path

import pytest

from synctool import config as config_mod
from synctool import drive as drive_mod
from synctool import engine
from synctool import ledger as ledger_mod
from synctool import pipeline


def _mirror(src: Path, dest: Path, delete: bool) -> None:
    """Stand-in for `rsync -auv[--delete]` used so tests don't depend on the
    real rsync binary being installed."""
    dest.mkdir(parents=True, exist_ok=True)
    src_names = set()
    for item in src.iterdir():
        src_names.add(item.name)
        target = dest / item.name
        if item.is_dir():
            _mirror(item, target, delete)
        else:
            shutil.copy2(item, target)
    if delete:
        for item in dest.iterdir():
            if item.name not in src_names:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()


def _fake_run_rsync(command: list[str], stream: bool = True, on_line=None) -> engine.RsyncResult:
    delete = "--delete" in command
    dry_run = "-n" in command
    src, dest = Path(command[-2]), Path(command[-1])
    if not dry_run:
        _mirror(src, dest, delete)
    if on_line:
        on_line("mirrored\n")
    return engine.RsyncResult(returncode=0, command=command, output="")


@pytest.fixture(autouse=True)
def fake_rsync(monkeypatch):
    monkeypatch.setattr(pipeline.engine, "run_rsync", _fake_run_rsync)


@pytest.fixture
def drive_root(tmp_path, monkeypatch):
    drive_dir = tmp_path / "drive"
    drive_dir.mkdir()

    def fake_ensure_mounted(uuid: str) -> drive_mod.DriveInfo:
        return drive_mod.DriveInfo(uuid=uuid, device=Path("/dev/fake"), mount_point=drive_dir)

    monkeypatch.setattr(pipeline.drive_mod, "ensure_mounted", fake_ensure_mounted)
    return drive_dir


def _make_config(local_path: Path, hostname: str, name: str = "projects", drive_subpath: str = "projects") -> config_mod.Config:
    return config_mod.Config(
        drive=config_mod.DriveConfig(uuid="1234-ABCD"),
        setup=config_mod.SetupConfig(hostname_override=hostname),
        sync_sets=[config_mod.SyncSet(name=name, local_path=str(local_path), drive_subpath=drive_subpath)],
    )


def test_recall_copies_files_and_updates_ledger(tmp_path, drive_root):
    local = tmp_path / "local"
    local.mkdir()
    (local / "a.txt").write_text("hello")

    cfg = _make_config(local, "setup-a")
    report = pipeline.run(cfg, action="recall")

    assert report.ok
    assert (drive_root / "projects" / "a.txt").read_text() == "hello"

    led = ledger_mod.load(drive_root)
    state = led.sync_sets["projects"]
    assert state.last_action == "recall"
    assert state.last_hostname == "setup-a"
    assert state.drive_subpath == "projects"


def test_recall_delete_removes_stale_drive_files(tmp_path, drive_root):
    local = tmp_path / "local"
    local.mkdir()
    (local / "keep.txt").write_text("keep")

    (drive_root / "projects").mkdir()
    (drive_root / "projects" / "stale.txt").write_text("stale")

    cfg = _make_config(local, "setup-a")
    pipeline.run(cfg, action="recall")

    assert not (drive_root / "projects" / "stale.txt").exists()
    assert (drive_root / "projects" / "keep.txt").exists()


def test_deploy_pulls_files_from_drive(tmp_path, drive_root):
    (drive_root / "projects").mkdir()
    (drive_root / "projects" / "b.txt").write_text("from drive")

    local = tmp_path / "local"
    local.mkdir()

    cfg = _make_config(local, "setup-b")
    report = pipeline.run(cfg, action="deploy")

    assert report.ok
    assert (local / "b.txt").read_text() == "from drive"

    state = ledger_mod.load(drive_root).sync_sets["projects"]
    assert state.last_action == "deploy"
    assert state.last_hostname == "setup-b"


def test_deploy_skips_when_drive_subpath_missing(tmp_path, drive_root):
    local = tmp_path / "local"
    local.mkdir()

    cfg = _make_config(local, "setup-b")
    report = pipeline.run(cfg, action="deploy")

    assert len(report.outcomes) == 1
    assert report.outcomes[0].skipped
    assert "does not exist on the drive" in report.outcomes[0].skip_reason


def test_local_path_missing_raises_pipeline_abort(tmp_path, drive_root):
    cfg = _make_config(tmp_path / "does-not-exist", "setup-a")
    with pytest.raises(pipeline.PipelineAbort) as exc_info:
        pipeline.run(cfg, action="recall")
    assert exc_info.value.exit_code == 4


def test_only_filters_to_single_sync_set(tmp_path, drive_root):
    local_a = tmp_path / "a"
    local_a.mkdir()
    local_b = tmp_path / "b"
    local_b.mkdir()
    cfg = config_mod.Config(
        drive=config_mod.DriveConfig(uuid="1234-ABCD"),
        setup=config_mod.SetupConfig(hostname_override="setup-a"),
        sync_sets=[
            config_mod.SyncSet(name="a", local_path=str(local_a), drive_subpath="a"),
            config_mod.SyncSet(name="b", local_path=str(local_b), drive_subpath="b"),
        ],
    )
    report = pipeline.run(cfg, action="recall", only="a")
    assert [o.name for o in report.outcomes] == ["a"]


def test_only_accepts_list_of_names(tmp_path, drive_root):
    local_a = tmp_path / "a"
    local_a.mkdir()
    local_b = tmp_path / "b"
    local_b.mkdir()
    local_c = tmp_path / "c"
    local_c.mkdir()
    cfg = config_mod.Config(
        drive=config_mod.DriveConfig(uuid="1234-ABCD"),
        setup=config_mod.SetupConfig(hostname_override="setup-a"),
        sync_sets=[
            config_mod.SyncSet(name="a", local_path=str(local_a), drive_subpath="a"),
            config_mod.SyncSet(name="b", local_path=str(local_b), drive_subpath="b"),
            config_mod.SyncSet(name="c", local_path=str(local_c), drive_subpath="c"),
        ],
    )
    report = pipeline.run(cfg, action="recall", only=["a", "c"])
    assert [o.name for o in report.outcomes] == ["a", "c"]


def test_only_with_unknown_name_in_list_raises(tmp_path, drive_root):
    local_a = tmp_path / "a"
    local_a.mkdir()
    cfg = _make_config(local_a, "setup-a", name="a", drive_subpath="a")
    with pytest.raises(pipeline.PipelineAbort):
        pipeline.run(cfg, action="recall", only=["a", "does-not-exist"])


def test_on_line_and_on_set_start_callbacks_invoked(tmp_path, drive_root):
    local = tmp_path / "local"
    local.mkdir()
    (local / "a.txt").write_text("hi")

    started = []
    lines = []
    cfg = _make_config(local, "setup-a")
    pipeline.run(
        cfg,
        action="recall",
        on_set_start=lambda name, action: started.append((name, action)),
        on_line=lambda name, line: lines.append((name, line)),
    )
    assert started == [("projects", "recall")]
    assert lines == [("projects", "mirrored\n")]


def test_deploy_after_own_recall_warns_and_can_be_declined(tmp_path, drive_root):
    local = tmp_path / "local"
    local.mkdir()
    (local / "a.txt").write_text("hi")

    cfg = _make_config(local, "setup-a")
    pipeline.run(cfg, action="recall")  # sets ledger last_action=recall by setup-a

    report = pipeline.run(cfg, action="deploy", confirm=lambda msg: False)
    assert report.outcomes[0].skipped
    assert report.outcomes[0].skip_reason == "aborted by user"
    assert report.any_aborted


def test_deploy_after_own_recall_proceeds_with_assume_yes(tmp_path, drive_root):
    local = tmp_path / "local"
    local.mkdir()
    (local / "a.txt").write_text("hi")

    cfg = _make_config(local, "setup-a")
    pipeline.run(cfg, action="recall")

    report = pipeline.run(cfg, action="deploy", assume_yes=True)
    assert report.ok
    assert not report.outcomes[0].skipped
    assert report.outcomes[0].warning is not None


def test_drive_subpath_mismatch_skips_set(tmp_path, drive_root):
    local = tmp_path / "local"
    local.mkdir()

    led = ledger_mod.Ledger()
    ledger_mod.record_action(led, "projects", "old-path", "recall", "setup-a")
    ledger_mod.save(drive_root, led)

    cfg = _make_config(local, "setup-b", drive_subpath="new-path")
    report = pipeline.run(cfg, action="recall")

    assert report.outcomes[0].skipped
    assert "disagree" in report.outcomes[0].skip_reason


def test_dry_run_does_not_write_ledger_or_files(tmp_path, drive_root):
    local = tmp_path / "local"
    local.mkdir()
    (local / "a.txt").write_text("hi")

    cfg = _make_config(local, "setup-a")
    report = pipeline.run(cfg, action="recall", dry_run=True)

    assert report.ok
    assert not (drive_root / ledger_mod.LEDGER_FILENAME).exists()
    assert not (drive_root / "projects" / "a.txt").exists()


def test_recall_then_deploy_from_other_setup_full_cycle(tmp_path, drive_root):
    local_a = tmp_path / "setup-a-local"
    local_a.mkdir()
    (local_a / "shared.txt").write_text("v1")

    cfg_a = _make_config(local_a, "setup-a")
    pipeline.run(cfg_a, action="recall")

    local_b = tmp_path / "setup-b-local"
    local_b.mkdir()
    cfg_b = _make_config(local_b, "setup-b")
    report = pipeline.run(cfg_b, action="deploy")

    assert report.ok
    assert (local_b / "shared.txt").read_text() == "v1"


# ---- discovering sets added on the other setup --------------------------------


def test_register_new_sets_announces_set_on_drive_without_a_transfer(tmp_path, drive_root):
    local_a = tmp_path / "a"
    local_a.mkdir()
    cfg_a = _make_config(local_a, "setup-a", name="notes", drive_subpath="docs/notes")

    result = pipeline.register_new_sets(cfg_a)

    assert result.registered == ["notes"]
    assert result.conflicts == []
    state = ledger_mod.load(drive_root).sync_sets["notes"]
    assert state.drive_subpath == "docs/notes"
    assert state.last_action == ledger_mod.REGISTER_ACTION
    assert state.last_hostname == "setup-a"
    assert not (drive_root / "docs").exists()  # nothing copied


def test_other_setup_sees_registered_set_as_unconfigured(tmp_path, drive_root):
    local_a = tmp_path / "a"
    local_a.mkdir()
    pipeline.register_new_sets(_make_config(local_a, "setup-a", name="notes", drive_subpath="notes"))

    local_b = tmp_path / "b"
    local_b.mkdir()
    cfg_b = _make_config(local_b, "setup-b", name="projects", drive_subpath="projects")

    found = pipeline.find_unconfigured_sets(ledger_mod.load(drive_root), cfg_b)

    assert [name for name, _ in found] == ["notes"]
    assert found[0][1].drive_subpath == "notes"
    assert found[0][1].last_hostname == "setup-a"


def test_find_unconfigured_sets_ignores_sets_already_configured(tmp_path, drive_root):
    local = tmp_path / "a"
    local.mkdir()
    cfg = _make_config(local, "setup-a")
    pipeline.register_new_sets(cfg)

    assert pipeline.find_unconfigured_sets(ledger_mod.load(drive_root), cfg) == []


def test_register_new_sets_leaves_existing_entry_and_history_alone(tmp_path, drive_root):
    local = tmp_path / "a"
    local.mkdir()
    (local / "f.txt").write_text("x")
    cfg = _make_config(local, "setup-a")
    pipeline.run(cfg, action="recall")
    before = ledger_mod.load(drive_root).sync_sets["projects"]

    result = pipeline.register_new_sets(_make_config(local, "setup-b"))

    assert result.registered == []
    after = ledger_mod.load(drive_root).sync_sets["projects"]
    assert after == before


def test_register_new_sets_reports_drive_subpath_conflict(tmp_path, drive_root):
    local = tmp_path / "a"
    local.mkdir()
    pipeline.register_new_sets(_make_config(local, "setup-a", name="dotfiles", drive_subpath="dotfiles/nvim"))

    result = pipeline.register_new_sets(_make_config(local, "setup-b", name="dotfiles", drive_subpath="dotfiles/nvi"))

    assert result.registered == []
    assert len(result.conflicts) == 1 and "dotfiles/nvi" in result.conflicts[0]
    assert ledger_mod.load(drive_root).sync_sets["dotfiles"].drive_subpath == "dotfiles/nvim"


def test_register_new_sets_raises_when_drive_missing(tmp_path, monkeypatch):
    def boom(uuid):
        raise drive_mod.DriveError("drive not connected")

    monkeypatch.setattr(pipeline.drive_mod, "ensure_mounted", boom)
    local = tmp_path / "a"
    local.mkdir()

    with pytest.raises(drive_mod.DriveError):
        pipeline.register_new_sets(_make_config(local, "setup-a"))


def test_deploy_of_registered_but_never_recalled_set_is_skipped_cleanly(tmp_path, drive_root):
    local_a = tmp_path / "a"
    local_a.mkdir()
    pipeline.register_new_sets(_make_config(local_a, "setup-a", name="notes", drive_subpath="notes"))

    local_b = tmp_path / "b"
    local_b.mkdir()
    report = pipeline.run(_make_config(local_b, "setup-b", name="notes", drive_subpath="notes"), action="deploy")

    outcome = report.outcomes[0]
    assert outcome.skipped and "nothing has been recalled" in outcome.skip_reason
    assert outcome.warning is None


def test_first_recall_after_registration_updates_the_same_entry(tmp_path, drive_root):
    local = tmp_path / "a"
    local.mkdir()
    (local / "f.txt").write_text("x")
    cfg = _make_config(local, "setup-a", name="notes", drive_subpath="notes")
    pipeline.register_new_sets(cfg)

    pipeline.run(cfg, action="recall")

    state = ledger_mod.load(drive_root).sync_sets["notes"]
    assert state.last_action == "recall"
    assert [h.action for h in state.history] == ["add", "recall"]
