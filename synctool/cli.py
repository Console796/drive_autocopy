from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config as config_mod
from . import drive as drive_mod
from . import engine
from . import ignore as ignore_mod
from . import ledger as ledger_mod
from . import pipeline
from .logging_setup import configure_logging

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_DRIVE_NOT_FOUND = 2
EXIT_ABORTED = 3
EXIT_CONFIG_ERROR = 4


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.config) if args.config else config_mod.default_config_path()
    if path.exists() and not args.force:
        print(f"config already exists at {path} (use --force to overwrite)")
        return EXIT_CONFIG_ERROR

    all_drives = drive_mod.list_drives()
    external = [d for d in all_drives if d.is_external]
    candidates = external or all_drives
    if not candidates:
        print("No drives detected. Plug in the drive and retry.")
        return EXIT_FAILURE

    print("Detected drives:")
    for i, d in enumerate(candidates):
        print(f"  [{i}] {d.describe()}")
    if external and len(all_drives) > len(external):
        print(f"  ({len(all_drives) - len(external)} internal drive(s) hidden)")

    choice = input("Select the sync drive by index: ").strip()
    try:
        chosen = candidates[int(choice)]
    except (ValueError, IndexError):
        print("invalid selection")
        return EXIT_FAILURE
    uuid = chosen.uuid

    label = input(f"Label for this drive [{chosen.display_name}]: ").strip() or chosen.display_name
    mount_point = input("Preferred mount point [/media/synctool-mount]: ").strip() or "/media/synctool-mount"

    sync_sets = _sync_sets_from_ledger(uuid)
    if not sync_sets:
        name = input("First sync set name (e.g. 'projects'): ").strip()
        local_path = input(f"Local path for {name!r} on this machine: ").strip()
        drive_subpath = input(f"Drive subpath for {name!r} [{name}]: ").strip() or name
        sync_sets = [config_mod.SyncSet(name=name, local_path=local_path, drive_subpath=drive_subpath)]

    cfg = config_mod.Config(
        drive=config_mod.DriveConfig(uuid=uuid, label=label, mount_point=mount_point),
        setup=config_mod.SetupConfig(),
        sync_sets=sync_sets,
    )
    config_mod.write_config(path, cfg)
    print(f"\nwrote config to {path}")
    print("Add more [[sync_sets]] blocks by editing that file directly — see SPEC.md §6.")
    print("Run `synctool init` again (independently) on your other setup, with its own local paths.")
    return EXIT_OK


def _sync_sets_from_ledger(uuid: str) -> list[config_mod.SyncSet]:
    """Offer to bootstrap this setup's sync sets from a ledger the *other*
    setup already left on the drive, instead of typing every set by hand.

    Each imported set still gets its local_path (and, if desired, its
    drive_subpath) confirmed interactively — nothing is written until the
    caller writes the resulting config, and the user can always hand-edit
    config.toml afterward (SPEC.md §6).
    """
    try:
        info = drive_mod.ensure_mounted(uuid)
    except drive_mod.DriveError:
        return []

    try:
        ledger_obj = ledger_mod.load(info.mount_point)
    except ledger_mod.LedgerError as exc:
        print(f"note: couldn't read existing ledger on drive ({exc}); skipping import")
        return []

    if not ledger_obj.sync_sets:
        return []

    print(f"\nFound {len(ledger_obj.sync_sets)} existing sync set(s) recorded on this drive:")
    for name, state in ledger_obj.sync_sets.items():
        print(
            f"  {name!r}: drive_subpath={state.drive_subpath!r} "
            f"(last {state.last_action} by {state.last_hostname} at {state.last_timestamp})"
        )

    answer = input("Import these as a starting point for this setup's config? [Y/n]: ").strip().lower()
    if answer == "n":
        return []

    sync_sets: list[config_mod.SyncSet] = []
    for name, state in ledger_obj.sync_sets.items():
        drive_subpath = state.drive_subpath or name
        default_local = str(Path.home() / name)
        local_path = input(f"Local path for {name!r} on this machine [{default_local}]: ").strip() or default_local
        drive_subpath = input(f"Drive subpath for {name!r} [{drive_subpath}]: ").strip() or drive_subpath
        sync_sets.append(config_mod.SyncSet(name=name, local_path=local_path, drive_subpath=drive_subpath))

    return sync_sets


def _load_config_or_exit(args: argparse.Namespace) -> config_mod.Config | None:
    path = Path(args.config) if args.config else None
    try:
        return config_mod.load_config(path)
    except config_mod.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return None


def _confirm(message: str) -> bool:
    try:
        answer = input(f"WARNING: {message}\nContinue? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _run_sync(args: argparse.Namespace, action: str) -> int:
    cfg = _load_config_or_exit(args)
    if cfg is None:
        return EXIT_CONFIG_ERROR

    logger = configure_logging()

    try:
        report = pipeline.run(
            cfg,
            action=action,  # type: ignore[arg-type]
            only=args.only,
            dry_run=args.dry_run,
            assume_yes=args.yes,
            confirm=_confirm,
        )
    except pipeline.PipelineAbort as exc:
        print(f"error: {exc}", file=sys.stderr)
        logger.error("%s aborted: %s", action, exc)
        return exc.exit_code
    except drive_mod.DriveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        logger.error("%s aborted: %s", action, exc)
        return EXIT_DRIVE_NOT_FOUND
    except ledger_mod.LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        logger.error("%s aborted: %s", action, exc)
        return EXIT_FAILURE

    for outcome in report.outcomes:
        if outcome.skipped:
            print(f"[{outcome.name}] skipped: {outcome.skip_reason}")
            logger.info("%s %s skipped: %s", action, outcome.name, outcome.skip_reason)
            continue
        result = outcome.rsync_result
        assert result is not None
        status = "ok" if result.ok else f"FAILED (exit {result.returncode})"
        mode = "dry-run" if args.dry_run else action
        print(f"[{outcome.name}] {mode}: {status}")
        logger.info("%s %s -> %s", action, outcome.name, status)

    if report.any_failed:
        return EXIT_FAILURE
    if report.any_aborted:
        return EXIT_ABORTED
    return EXIT_OK


def cmd_recall(args: argparse.Namespace) -> int:
    return _run_sync(args, "recall")


def cmd_deploy(args: argparse.Namespace) -> int:
    return _run_sync(args, "deploy")


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load_config_or_exit(args)
    if cfg is None:
        return EXIT_CONFIG_ERROR

    try:
        info = drive_mod.ensure_mounted(cfg.drive.uuid)
    except drive_mod.DriveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DRIVE_NOT_FOUND

    candidate = drive_mod.find_drive(cfg.drive.uuid)
    drive_name = candidate.display_name if candidate else (cfg.drive.label or "drive")
    print(f"drive: {drive_name} mounted at {info.mount_point}")
    try:
        ledger_obj = ledger_mod.load(info.mount_point)
    except ledger_mod.LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    hostname = ledger_mod.current_hostname(cfg.setup.hostname_override)
    print(f"this host: {hostname}")

    for s in cfg.sync_sets:
        state = ledger_obj.sync_sets.get(s.name)
        drive_target = info.mount_point / s.drive_subpath
        print(f"\n[{s.name}]")
        print(f"  local: {s.local_path}")
        print(f"  drive: {drive_target}")
        if state is None:
            print("  ledger: no record yet (first run pending)")
        else:
            print(f"  ledger: last {state.last_action} by {state.last_hostname} at {state.last_timestamp}")
            if state.drive_subpath != s.drive_subpath:
                print(
                    f"  WARNING: ledger drive_subpath ({state.drive_subpath!r}) does not match "
                    f"this config's drive_subpath ({s.drive_subpath!r})"
                )

        local_path = Path(s.local_path)
        try:
            ignore_rules = ignore_mod.load(local_path)
        except ignore_mod.IgnoreError as exc:
            print(f"  {exc}")
            continue
        if ignore_rules:
            print(f"  ignore: {len(ignore_rules)} rule(s) from {ignore_mod.IGNORE_FILENAME}")

        if drive_target.exists() and local_path.exists():
            preview = engine.run_rsync(
                engine.build_recall_command(
                    local_path, drive_target, dry_run=True, filter_args=ignore_mod.filter_args(ignore_rules)
                ),
                stream=False,
            )
            changed = [
                line
                for line in preview.output.splitlines()
                if line and not line.startswith(("sending", "sent ", "total size"))
            ]
            print(f"  pending recall changes: {len(changed)}")
        elif not drive_target.exists():
            print("  pending recall changes: n/a (nothing on drive yet)")

    unconfigured = pipeline.find_unconfigured_sets(ledger_obj, cfg)
    if unconfigured:
        print("\nOn the drive but not configured on this machine:")
        for name, state in unconfigured:
            print(
                f"  {name!r}: drive_subpath={state.drive_subpath!r} "
                f"(last {state.last_action} by {state.last_hostname} at {state.last_timestamp})"
            )
        print("To sync one here, add a block like this to your config.toml:")
        for name, state in unconfigured:
            print(
                f'\n  [[sync_sets]]\n  name = "{name}"\n'
                f'  local_path = "{Path.home() / name}"\n'
                f'  drive_subpath = "{state.drive_subpath or name}"'
            )

    return EXIT_OK


def cmd_drives(args: argparse.Namespace) -> int:
    """List connected drives by name — no need to decipher `blkid` output."""
    drives = drive_mod.list_drives()
    if not args.all:
        external = [d for d in drives if d.is_external]
        hidden = len(drives) - len(external)
        drives = external
    else:
        hidden = 0

    if not drives:
        print("No drives detected." if args.all else "No external drives detected (try --all).")
        return EXIT_OK

    for d in drives:
        print(d.describe())
        print(f"    UUID: {d.uuid}")
    if hidden:
        print(f"({hidden} internal drive(s) hidden — use --all to show them)")
    return EXIT_OK


def cmd_config_show(args: argparse.Namespace) -> int:
    cfg = _load_config_or_exit(args)
    if cfg is None:
        return EXIT_CONFIG_ERROR
    print(f"drive: uuid={cfg.drive.uuid} label={cfg.drive.label!r} mount_point={cfg.drive.mount_point}")
    print(f"setup: hostname_override={cfg.setup.hostname_override!r}")
    for s in cfg.sync_sets:
        print(f"sync_set {s.name!r}: local_path={s.local_path} drive_subpath={s.drive_subpath}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="synctool")
    parser.add_argument("--config", help="path to config.toml (default: XDG config dir)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="interactive first-time setup")
    p_init.add_argument("--force", action="store_true", help="overwrite existing config")
    p_init.set_defaults(func=cmd_init)

    for name, func, help_text in (
        ("recall", cmd_recall, "push local files to the drive"),
        ("deploy", cmd_deploy, "pull files from the drive to local"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--only", help="only sync this sync_set name")
        p.add_argument("--yes", action="store_true", help="don't prompt on warnings")
        p.add_argument("--dry-run", action="store_true", help="preview changes without applying")
        p.set_defaults(func=func)

    p_status = sub.add_parser("status", help="show ledger state and pending changes")
    p_status.set_defaults(func=cmd_status)

    p_drives = sub.add_parser("drives", help="list connected drives by name")
    p_drives.add_argument("--all", action="store_true", help="include internal drives")
    p_drives.set_defaults(func=cmd_drives)

    p_config = sub.add_parser("config", help="config-related commands")
    config_sub = p_config.add_subparsers(dest="config_command", required=True)
    p_config_show = config_sub.add_parser("show")
    p_config_show.set_defaults(func=cmd_config_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
