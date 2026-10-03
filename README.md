# synctool

A tool that automates copying files between computers using an intermediary drive.

## Install

```sh
python3 -m pip install --user -e .
```

Requires Python 3.11+ and `rsync` on the system (`sudo apt install rsync`).

## Usage

On each setup, independently:

```sh
synctool init
```

This walks you through picking the drive's UUID (via `/dev/disk/by-uuid`)
and your first sync set. If the drive already has a `.sync_state.json`
ledger on it (e.g. the other setup already recalled some sync sets), `init`
finds it, lists what it knows about, and offers to bootstrap this setup's
sync sets from it — `name` and `drive_subpath` come from the ledger,
`local_path` defaults to `~/<name>`, and every value is shown to you and
editable (blank input just accepts the default) before anything is written.
Add more `[[sync_sets]]` entries by editing `~/.config/synctool/config.toml`
directly — see [SPEC.md §6](SPEC.md#6-configuration). Each setup keeps its
own config; only `name` and `drive_subpath` need to match between the two
setups' configs, `local_path` can differ.

Day to day:

```sh
synctool recall            # push local files to the drive (before you swap setups)
synctool deploy             # pull files from the drive (after you swap setups)
synctool status              # see ledger state and pending changes, no writes
synctool recall --only projects --dry-run
```

## Ignoring files

Put a `.syncignore` in the root of a sync set's directory to keep files out of
its transfers. Syntax is the same as `.gitignore`:

```
# build output and caches
node_modules/
__pycache__/
*.log
!important.log
/dist/
```

The file is part of the tree, so it syncs along with everything else and both
setups use the same list. A transfer reads the ignore file at its source — your
local copy on `recall`, the drive's copy on `deploy`. `status`, `--dry-run` and
the GUI's pending counts all honour it. Already-recalled files that you later
ignore stay on the drive (they aren't deleted); remove them by hand if you want
them gone. See [SPEC.md §7.3](SPEC.md#73-ignoring-files-syncignore).

## GUI

A PyQt6 front end over the same `synctool` package (SPEC.md §14). The
launcher installs everything it needs on first run:

```sh
./run-gui.sh
```

It picks an interpreter that can actually install packages rather than
trusting whatever `python3` resolves to — a third-party Python in
`/usr/local/bin` frequently ships without `pip` *and* without `ensurepip`,
which breaks both `pip install` and `python3 -m venv`, while
`sudo apt install python3-pip` silently fixes only `/usr/bin/python3`.
It prefers building an isolated `.venv`, and falls back to a `--user`
install. To force a specific interpreter:

```sh
PYTHON=/usr/bin/python3 ./run-gui.sh
```

If you'd rather install manually and use the `synctool-gui` entry point:

```sh
/usr/bin/python3 -m pip install --user -e '.[gui]'
synctool-gui
```

- **"Choose Drive…"** lists your connected drives by name, size and bus
  (e.g. `RADIA — 4.5T — USB — Seagate BUP BK`), external drives first, so you
  never have to look up a UUID. The UUID is still what gets stored, since
  it's the only identifier stable across replugs — it's just no longer
  something you have to read. `synctool drives` prints the same list in the
  terminal.
- The sync-set table shows each set's local/drive paths, last recall/deploy
  action, and a live count of pending changes (via an `rsync --dry-run`
  preview).
- "Edit Sync Sets…" opens a setup wizard if no config exists yet, or lets
  you add/remove sync sets and change drive settings — writes the same
  `config.toml` the CLI reads. On first-time setup, once you pick a drive
  that already has a `.sync_state.json` ledger on it, the wizard offers to
  populate the sync-set rows from that ledger (same as `synctool init` on
  the CLI) — local paths default to `~/<name>` and every row stays editable
  before you hit Save.
- **Sets added on the other setup show up automatically.** Saving a new sync
  set in the setup dialog also records it on the drive (if it's plugged in), so
  when you move the drive to the other machine the window lists it as a greyed
  `NEW` row, logs a notice, and enables "Add New Sets from Drive…" — which
  opens the setup dialog with the name/drive subpath filled in and a default
  local path you can change. The window rechecks the drive every few seconds,
  so plugging it in is enough. `synctool status` reports the same, with a
  config block to paste.
- Select rows in the table before clicking Recall/Deploy to act on just
  those sync sets; with nothing selected, all configured sets are used.
- Redundant/stale-sync warnings from the pipeline (SPEC.md §8 Step 2) show
  up as a confirm dialog instead of a terminal prompt.
- Transfers run on a background thread so the window stays responsive; rsync
  output streams live into the log pane at the bottom.

## Development

```sh
python3 -m pip install --user -e '.[dev]'
python3 -m pytest
```

Tests stub out the actual `rsync` subprocess call so they don't require
rsync or a real removable drive to be present.

## LLM Disclosure
This project was quickly vibe coded together to make my personal workflow easier.
