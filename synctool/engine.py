from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class RsyncResult:
    returncode: int
    command: list[str]
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _as_dir(path: Path) -> str:
    s = str(path)
    return s if s.endswith("/") else s + "/"


def build_recall_command(local_path: Path, drive_target: Path, dry_run: bool = False) -> list[str]:
    cmd = ["rsync", "-auv", "--delete"]
    if dry_run:
        cmd.append("-n")
    cmd += [_as_dir(local_path), _as_dir(drive_target)]
    return cmd


def build_deploy_command(drive_target: Path, local_path: Path, dry_run: bool = False) -> list[str]:
    cmd = ["rsync", "-auv"]
    if dry_run:
        cmd.append("-n")
    cmd += [_as_dir(drive_target), _as_dir(local_path)]
    return cmd


def run_rsync(
    command: list[str],
    stream: bool = True,
    on_line: Callable[[str], None] | None = None,
) -> RsyncResult:
    """Run an rsync command, streaming output live while also capturing it.

    If `on_line` is given, each output line is handed to it instead of being
    printed (used by the GUI to route output into a log widget rather than
    stdout); otherwise falls back to printing when `stream` is True.
    """
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        if on_line:
            on_line(line)
        elif stream:
            print(line, end="")
        lines.append(line)
    proc.wait()
    return RsyncResult(returncode=proc.returncode, command=command, output="".join(lines))
