# Drive-Sync Tool — Specification

## 1. Overview

A Python CLI tool that automates the "sneakernet" workflow of moving project
files between two computer setups (Setup A, Setup B) via a shared external
drive, on Kubuntu 26.04. It replaces manual drag-and-drop copying with a
single command that performs a safe, delta-based transfer using `rsync`,
guarded by drive-identity verification and a state ledger that prevents
accidental data loss or redundant/stale syncs.

Derived from the design discussion in [prompt.md](prompt.md).

## 2. Goals

- Reduce a manual, error-prone copy process to one command per direction.
- Never sync to the wrong device.
- Never silently overwrite newer local work with older data from the drive.
- Make the current sync state (who last pushed/pulled, and when) visible
  and machine-checkable.
- Keep the transfer fast via `rsync` delta encoding.

## 3. Non-Goals (v1)

- No GUI (CLI only; architecture should not preclude adding one later).
- No support for more than one external drive or more than two setups.
- No conflict *merging* (only conflict *detection/warning*) — the tool warns,
  it does not attempt three-way merges of divergent files.
- No cloud/network sync backend; the drive is always a local block device.

## 4. Terminology

| Term | Meaning |
|---|---|
| **Recall** | Push: local working directory → drive |
| **Deploy** | Pull: drive → local working directory |
| **Setup** | One of the two machines/environments sharing the drive |
| **Ledger** | `.sync_state.json` at the root of the drive, tracking last action |
| **Sync set** | A named (local path, drive subpath) pair synced together. `name` and `drive_subpath` are shared across both setups' configs; `local_path` is set independently per machine (§6) |

## 5. Architecture

```
sync-tool/
├── synctool/
│   ├── __init__.py
│   ├── cli.py          # argument parsing, entrypoint
│   ├── config.py        # load/validate ~/.config/synctool/config.toml
│   ├── drive.py         # UUID resolution, mount detection/mounting
│   ├── ledger.py         # read/write/validate .sync_state.json
│   ├── engine.py         # rsync command construction & subprocess execution
│   ├── pipeline.py        # orchestrates the 4-step safe execution sequence
│   └── logging_setup.py
├── tests/
├── pyproject.toml
└── README.md
```

The tool is a **controller**, not a reimplementation of file-copy logic:
`rsync` remains the transfer engine; Python owns state, safety checks, and UX.

## 6. Configuration

Config lives at `~/.config/synctool/config.toml` (XDG-compliant), created by
`synctool init` on first run. **This file is local to each setup and is
never itself synced via the drive** — Setup A and Setup B each maintain
their own independent copy. That's what makes different filesystem layouts
per machine a non-issue: `local_path` is whatever's correct on *that*
machine.

The one thing that must be **kept in agreement across both setups' configs**
is the pair `(name, drive_subpath)` for each logical sync set — that's the
shared contract that lets both machines agree on where a given set lives on
the drive and lets the ledger (§9) correlate "the same set" across hosts.
`local_path` is the only field expected to differ.

Setup A (`~/.config/synctool/config.toml` on the old machine):

```toml
[drive]
uuid = "1234-ABCD"          # from `blkid` / /dev/disk/by-uuid/
label = "SYNCDRIVE"          # human-readable, informational only
mount_point = "/media/synctool-mount"  # tool-managed mount, not KDE's auto-mount

[setup]
hostname_override = ""       # optional; defaults to `socket.gethostname()`

[[sync_sets]]
name = "projects"
local_path = "/home/jason/projects"
drive_subpath = "projects"

[[sync_sets]]
name = "dotfiles"
local_path = "/home/jason/.config/nvim"
drive_subpath = "dotfiles/nvim"
```

Setup B (a different machine — different disk layout, maybe a different
username or a laptop with everything under `/data` instead of `/home`):

```toml
[drive]
uuid = "1234-ABCD"           # same physical drive
label = "SYNCDRIVE"
mount_point = "/media/synctool-mount"

[setup]
hostname_override = ""

[[sync_sets]]
name = "projects"                       # must match Setup A
local_path = "/data/dev/projects"        # different path — fine
drive_subpath = "projects"               # must match Setup A

[[sync_sets]]
name = "dotfiles"
local_path = "/home/jason.b/.config/nvim"
drive_subpath = "dotfiles/nvim"          # must match Setup A
```

- Multiple `sync_sets` allow syncing several independent directory trees in
  one invocation, each with its own path on the drive.
- A `sync_set` doesn't need to exist in *both* configs — e.g. a set only
  relevant to one machine (a laptop-only config folder) can simply be
  omitted from the other setup's config and it's never touched there.
- `synctool init` interactively resolves the drive UUID (lists candidates
  from `blkid`) and writes the config; run it independently on each setup.
- If the chosen drive already carries a `.sync_state.json` ledger (§9) —
  i.e. the other setup has already recalled one or more sync sets — `init`
  mounts the drive, reads it, and offers to bootstrap this setup's
  `sync_sets` from the ledger's `name`/`drive_subpath` pairs rather than
  requiring them to be typed from scratch. `local_path` isn't in the ledger
  (it's local-only, §6), so it's defaulted to `~/<name>` and, like
  `drive_subpath`, presented per set for the user to accept or override
  before the config is written. Declining the import, or a drive with no
  ledger yet, falls back to the single manually-entered sync set described
  above.
- **Adding a set on one setup makes it discoverable from the other.** When a
  sync set is added through the GUI's setup dialog, the tool also records it
  in the drive's ledger (action `"add"`, no files transferred) if the drive is
  reachable; otherwise the set's first Recall records it. The other setup
  compares the ledger against its own config on every `status` / GUI refresh
  and reports any set the drive has that its config lacks (`status` prints a
  ready-to-paste `[[sync_sets]]` block; the GUI lists it as a greyed `NEW` row
  and offers "Add New Sets from Drive…", which pre-fills the setup dialog with
  `name`/`drive_subpath` from the ledger and `~/<name>` as `local_path`). It is
  informational only — never an error and never auto-written into the config,
  since a set may deliberately exist on one setup only (§12). While the GUI is
  open it also polls the ledger file's mtime/size and the drive's presence
  (without mounting), so plugging the drive in or changing the ledger triggers a
  refresh.
- Step 1 of the pipeline (§8) cross-checks `drive_subpath` against what the
  ledger has on record for that `name`, to catch the case where the two
  configs have drifted out of agreement (typo, copy-paste edit, etc.).

## 7. Core Modes

### 7.1 Recall (push local → drive)

```
rsync -auv --delete <local_path>/ <drive_mount>/<drive_subpath>/
```

- `--delete`: removes files from the drive that no longer exist locally, so
  the drive mirrors the source setup and doesn't accumulate stale artifacts.
- Runs once per configured `sync_set` unless `--only <name>` is given.

### 7.2 Deploy (pull drive → local)

```
rsync -auv <drive_mount>/<drive_subpath>/ <local_path>/
```

- No `--delete`: deploying must never delete local files based on drive
  state, since the drive may be a partial/older view of the world.
- `-u` (update): skips any destination file whose modification time is newer
  than the source, so fresher local work is never clobbered by an older
  drive copy.

### 7.3 Status (read-only)

Not in the original discussion, but required for a safe UX: prints the
ledger contents **per sync set**, current drive mount state, and — via
`rsync -auvn --delete` (dry run) — a preview of what a Recall or Deploy
*would* change for each set, without performing it.

## 8. Safe Execution Pipeline

Every `recall` / `deploy` invocation runs these steps in order; any failure
aborts before touching files.

### Step 1 — Verify Drive Identity

- Resolve the configured `drive.uuid` via `/dev/disk/by-uuid/<uuid>`.
- If absent: abort with "drive not connected" (exit code 2).
- If the tool's managed mount point isn't currently mounted to that device,
  mount it explicitly (`udisksctl mount` or `mount`, invoked via `subprocess`,
  possibly requiring `pkexec`/`sudo` policy — see §12).
- Explicitly ignore KDE's `/media/<user>/<label>/` auto-mount path; the tool
  always addresses the drive via its own UUID-resolved mount point so a
  changed auto-mount path never causes a silent no-op or wrong-target write.
- **Cross-check `drive_subpath` per set:** for each affected `sync_set`,
  compare this setup's configured `drive_subpath` against the value recorded
  in the ledger (§9) for that set's `name`, if the ledger already has an
  entry for it. A mismatch means the two setups' configs have drifted apart
  (e.g. `dotfiles/nvim` on Setup A vs. a typo'd `dotfiles/nvi` on Setup B) —
  abort that set with an explicit error rather than silently reading/writing
  the wrong drive folder. No mismatch is possible on a set's first-ever run
  (nothing in the ledger yet to compare against).

### Step 2 — Validate Sync State

- Read `.sync_state.json` from the drive root (schema in §9). State is
  tracked **per sync set** — each set has its own `last_action` /
  `last_hostname` / `last_timestamp`, so directories with unrelated update
  cadences (e.g. `projects` vs. `dotfiles`) don't mask each other's staleness.
- For each `sync_set` affected by this invocation (all configured sets, or
  just the one named by `--only`):
  - If that set has no entry in the ledger: treat as first run for this
    set — proceed, but log a notice.
  - **Deploying**, and that set's `last_action == "recall"` performed by
    *this same* hostname → warn: "you're pulling `<name>` data you just
    pushed from here; likely redundant." Proceed only with `--yes` or
    interactive confirmation.
  - **Recalling**, and the ledger shows the *other* setup deployed `<name>`
    more recently than this setup's own last recall of `<name>` →
    informational only (this is the normal alternating pattern), no warning.
  - **Deploying**, and no Recall of `<name>` has happened since this setup's
    own last Deploy of `<name>` (i.e., nothing new was pushed for that set)
    → warn: "drive has no new `<name>` data since your last deploy."
- Warnings are evaluated and reported per set (a run over multiple sets may
  warn on some and not others). All warnings are non-fatal by default
  (printed, then prompt `Continue? [y/N]` on a TTY; require `--yes` in
  non-interactive/scripted use — `--yes` applies to the whole run).

### Step 3 — Execute Delta Transfer

- Build and run the `rsync` command per §7 for each affected `sync_set`,
  via `subprocess.run(..., check=False)` so the exit code can be inspected.
- Stream `rsync` output live (`-v`) to the terminal; also capture it to the
  log file (§11).
- Sets are processed independently: if one `sync_set` fails (non-zero exit),
  log the failure, skip Step 4 for *that set only*, and continue on to the
  remaining sets. The run's overall exit code is non-zero if any set failed,
  but sets that succeeded still get their ledger entries updated — a broken
  `documents` sync shouldn't leave a successful `dotfiles` sync unrecorded.

### Step 4 — Update State Ledger

- For each `sync_set` whose transfer in Step 3 exited 0, update that set's
  entry in `.sync_state.json` with the new record (§9), appended to that
  set's bounded history.
- The whole ledger file is rewritten atomically in one pass after Step 3
  completes for all sets (not once per set), so a crash mid-run can't leave
  the file half-updated.

## 9. State Ledger Schema (`.sync_state.json`)

State is keyed by sync-set name, so each configured directory tree tracks
its own independent history:

```json
{
  "version": 2,
  "sync_sets": {
    "projects": {
      "drive_subpath": "projects",
      "last_action": "recall",
      "last_hostname": "setup-a",
      "last_timestamp": "2026-08-22T14:03:11Z",
      "history": [
        {
          "action": "recall",
          "hostname": "setup-a",
          "timestamp": "2026-08-22T14:03:11Z"
        }
      ]
    },
    "dotfiles": {
      "drive_subpath": "dotfiles/nvim",
      "last_action": "deploy",
      "last_hostname": "setup-b",
      "last_timestamp": "2026-08-21T09:12:00Z",
      "history": [
        {
          "action": "deploy",
          "hostname": "setup-b",
          "timestamp": "2026-08-21T09:12:00Z"
        }
      ]
    }
  }
}
```

- Timestamps: UTC, ISO 8601.
- Each set's `history` is capped independently (e.g. last 50 entries per
  set) to keep the file small; older entries are dropped, not archived.
- A sync set with no entry yet under `sync_sets` simply doesn't exist in the
  ledger until it is registered (`last_action: "add"`, see §6) or has its first
  successful Recall or Deploy (see Step 2's first-run handling). An `"add"`
  entry triggers no Step 2 warnings; a Deploy of a set that was only
  registered is skipped with "nothing has been recalled for this set".
- File is written atomically (write to `.sync_state.json.tmp`, then
  `os.replace`) so a crash mid-write can't corrupt the ledger — the whole
  file is rewritten in one pass even though only some sets' entries changed.
- Hostname source: `config.setup.hostname_override` if set, else
  `socket.gethostname()`.
- `version` bumped to `2` for the per-sync-set schema (was a single global
  record in `1`); see §12 for handling a ledger from a mismatched version.

## 10. CLI Interface

```
synctool init                        # interactive first-time setup
synctool recall [--only NAME] [--yes] [--dry-run]
synctool deploy [--only NAME] [--yes] [--dry-run]
synctool status                       # ledger + drive state + pending-change preview
synctool config show
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Generic/rsync failure |
| 2 | Drive not found / not mounted |
| 3 | Aborted by user (declined confirmation) |
| 4 | Config error (missing/invalid config.toml) |

## 11. Logging

- `~/.local/state/synctool/synctool.log` (XDG state dir), rotated at ~5MB,
  keep 3 backups (`logging.handlers.RotatingFileHandler`).
- Each run logs, per sync set: timestamp, mode, set name, full rsync command
  line (excluding nothing sensitive — paths only), rsync exit code, and that
  set's ledger entry before/after.

## 12. Error Handling & Edge Cases

- **Drive unplugged mid-transfer:** `rsync` exits non-zero; pipeline reports
  failure, ledger is not updated, partial transfer is left as-is (rsync's
  own partial-file behavior applies; consider `--partial` flag as a future
  enhancement so interrupted large files can resume).
- **Two Recalls in a row without a Deploy in between:** allowed, just
  overwrites the drive again; ledger history shows both.
- **Mounting requires elevated privileges:** prefer `udisksctl mount -b
  /dev/disk/by-uuid/<uuid>` (user-mountable via polkit, no root needed for a
  normal removable-media rule) over raw `mount`, to avoid requiring `sudo`.
- **Ledger written by a newer tool version than installed locally:**
  `version` field checked; unknown/higher version → warn and refuse to
  auto-update the ledger (require `--force`).
- **Config references a `local_path` that doesn't exist:** abort before
  Step 1 with a clear error (nothing to sync).
- **`drive_subpath` disagreement between setups' configs:** caught by the
  Step 1 cross-check (§8); that `sync_set` is skipped for this run rather
  than syncing to/from a folder the other setup doesn't expect. Fix by
  correcting the config on whichever machine has the typo.
- **A `sync_set` exists on one setup's config but not the other's:** fine by
  design — it simply never runs on the setup that lacks it, and won't
  appear in `--only` there. It still shows up in `status` output and the GUI
  table (drive-side, flagged as not configured locally) even from the setup
  that doesn't configure it; see §6.

## 13. Testing Plan

- Unit tests (`pytest`) for: ledger read/write/atomicity, UUID resolution
  logic, warning-condition matrix in Step 2, rsync command construction
  (assert exact argv, don't actually shell out).
- Integration test using two temp directories and a temp "drive" directory
  (no real removable media needed) to exercise a full recall → deploy cycle
  end-to-end via `subprocess`.

## 14. Future Enhancements

- GUI wrapper (PyQt6 or CustomTkinter) over the same `synctool` package,
  adding progress bars and a confirmation dialog before destructive actions
  (`--delete` recalls in particular).
- `--partial` / resumable transfers for interrupted large-file copies.
- Encryption-at-rest on the drive (e.g., LUKS) as a documented recommendation
  rather than a tool feature, since the drive may hold sensitive project data.
- Multi-drive / multi-setup (>2) support if the workflow grows.
