from pathlib import Path

from synctool import engine


def test_build_recall_command_has_delete_and_trailing_slashes():
    cmd = engine.build_recall_command(Path("/home/j/projects"), Path("/mnt/drive/projects"))
    assert cmd == ["rsync", "-auv", "--delete", "/home/j/projects/", "/mnt/drive/projects/"]


def test_build_deploy_command_has_no_delete():
    cmd = engine.build_deploy_command(Path("/mnt/drive/projects"), Path("/home/j/projects"))
    assert cmd == ["rsync", "-auv", "/mnt/drive/projects/", "/home/j/projects/"]
    assert "--delete" not in cmd


def test_dry_run_adds_n_flag():
    cmd = engine.build_recall_command(Path("/a"), Path("/b"), dry_run=True)
    assert "-n" in cmd
    assert cmd.index("-n") < cmd.index("/a/")


def test_run_rsync_captures_output_and_exit_code():
    result = engine.run_rsync(["echo", "hello world"], stream=False)
    assert result.ok
    assert result.returncode == 0
    assert "hello world" in result.output


def test_run_rsync_nonzero_exit_is_not_ok():
    result = engine.run_rsync(["false"], stream=False)
    assert not result.ok
    assert result.returncode != 0
