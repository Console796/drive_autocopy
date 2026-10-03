"""`.syncignore` support: keep files out of a sync set's transfers.

The file lives at the root of a sync set's directory, so it is itself synced
and both setups share one list. A transfer always uses the `.syncignore` at its
*source* — the local directory on Recall, the drive copy on Deploy — so the
setup that pushed the data decides what is left out of it.

Syntax follows .gitignore, which rsync's pattern matching already resembles
(`*.log`, `dir/`, `/anchored`, `**`, `#` comments). One difference is bridged
here: gitignore is last-match-wins and supports `!pattern` to re-include, while
rsync is first-match-wins and treats a bare `!` line in an exclude file as a
literal pattern. Rules are therefore emitted in reverse order as explicit
`--filter` include/exclude rules, which gives gitignore's result. As in
gitignore, a file can't be re-included if a parent directory is excluded.

Patterns apply to the transfer in both directions' *source* only; a file that
is excluded but already present at the destination is left alone (rsync never
deletes excluded files unless asked to, and we don't).
"""

from __future__ import annotations

from pathlib import Path

IGNORE_FILENAME = ".syncignore"


class IgnoreError(Exception):
    pass


def parse(text: str) -> list[str]:
    """Turn .syncignore text into rsync filter rules ('- pat' / '+ pat').

    Blank lines and `#` comments are dropped; trailing whitespace is trimmed.
    The returned order is the one rsync needs (first match wins), i.e. the
    reverse of file order.
    """
    rules: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            pattern = line[1:]
            if pattern:
                rules.append(f"+ {pattern}")
        else:
            rules.append(f"- {line}")
    rules.reverse()
    return rules


def filter_args(rules: list[str]) -> list[str]:
    return [f"--filter={rule}" for rule in rules]


def load(source_dir: Path) -> list[str]:
    """Filter rules from `<source_dir>/.syncignore`; empty if there is no file.

    An unreadable file is an error rather than "no rules": silently syncing
    everything could copy exactly what the user meant to keep off the drive.
    """
    path = source_dir / IGNORE_FILENAME
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        raise IgnoreError(f"can't read {path}: {exc}") from exc
    return parse(text)
