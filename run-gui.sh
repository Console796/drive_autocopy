#!/usr/bin/env bash
# Launches the synctool GUI, installing it first if needed.
# Safe to run from anywhere, e.g. a desktop shortcut (SPEC.md §14).
#
# Why this is more than a one-liner: `python3` on PATH is not necessarily an
# interpreter that can install anything. A locally-built/third-party Python in
# /usr/local/bin often ships without `pip` AND without `ensurepip`, which makes
# both `pip install` and `python3 -m venv` fail — while `sudo apt install
# python3-pip` cheerfully succeeds, because it installs pip for /usr/bin/python3
# instead. So rather than trusting PATH, probe the candidate interpreters and
# pick one that can actually build an environment.
#
# Preference order:
#   1. an interpreter that can create a venv with pip  -> isolated .venv (best)
#   2. an interpreter that has a usable pip            -> pip install --user
# Override the auto-detection with:  PYTHON=/path/to/python3 ./run-gui.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
VENV_DIR="$SCRIPT_DIR/.venv"

log() { printf '%s\n' "$*" >&2; }

if ! command -v rsync >/dev/null 2>&1; then
    log "warning: rsync not found on PATH — recall/deploy will fail until you"
    log "         install it: sudo apt install rsync"
fi

# ---------------------------------------------------------------- candidates

# Gather plausible interpreters, best-known-good first. /usr/bin/python3 (the
# distro's own, the one apt installs pip/venv for) deliberately outranks
# whatever PATH resolves to.
candidates=()
[ -n "${PYTHON:-}" ] && candidates+=("$PYTHON")
candidates+=(/usr/bin/python3)
if path_py="$(command -v python3 2>/dev/null)"; then candidates+=("$path_py"); fi
for v in 3.14 3.13 3.12 3.11; do
    for d in /usr/bin /usr/local/bin; do
        [ -x "$d/python$v" ] && candidates+=("$d/python$v")
    done
done

# Deduplicate, resolving symlinks so /usr/bin/python3 and /usr/bin/python3.14
# don't both get probed.
seen=""
unique_candidates=()
for c in "${candidates[@]}"; do
    command -v "$c" >/dev/null 2>&1 || continue
    real="$(readlink -f "$c" 2>/dev/null || echo "$c")"
    case ":$seen:" in *":$real:"*) continue ;; esac
    seen="$seen:$real"
    unique_candidates+=("$c")
done

py_ok()        { "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; }
py_has_pip()   { "$1" -m pip --version >/dev/null 2>&1; }
py_can_venv()  { "$1" -c 'import venv, ensurepip' >/dev/null 2>&1; }

VENV_CAPABLE=""
PIP_CAPABLE=""
for c in "${unique_candidates[@]}"; do
    py_ok "$c" || continue
    [ -z "$VENV_CAPABLE" ] && py_can_venv "$c" && VENV_CAPABLE="$c"
    [ -z "$PIP_CAPABLE" ]  && py_has_pip  "$c" && PIP_CAPABLE="$c"
done

if [ -z "$VENV_CAPABLE" ] && [ -z "$PIP_CAPABLE" ]; then
    log "error: found no Python 3.11+ that can install packages."
    log ""
    log "Interpreters probed:"
    for c in "${unique_candidates[@]}"; do
        ver="$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")"
        note=""
        py_ok "$c"       || note="$note version<3.11"
        py_has_pip "$c"  || note="$note no-pip"
        py_can_venv "$c" || note="$note no-venv/ensurepip"
        log "  $c (python $ver):$note"
    done
    log ""
    log "Install pip and venv for the distro Python, then retry:"
    log "  sudo apt install python3-pip python3-venv"
    exit 1
fi

# -------------------------------------------------------------------- launch

# install_and_run <python> [extra pip args...] — never returns.
install_and_run() {
    local python="$1"; shift
    if ! "$python" -c "import synctool.gui.app" >/dev/null 2>&1; then
        log "Installing synctool ..."
        if "$python" -c "import PyQt6" >/dev/null 2>&1; then
            "$python" -m pip install "$@" -e .          # PyQt6 already present
        else
            "$python" -m pip install "$@" -e '.[gui]'
        fi
    fi
    exec "$python" -m synctool.gui.app
}

if [ -n "$VENV_CAPABLE" ]; then
    # Reuse an existing .venv only if it still has a working pip; a venv left
    # behind by a half-failed bootstrap is worse than no venv at all.
    if [ -x "$VENV_DIR/bin/python3" ] && ! "$VENV_DIR/bin/python3" -m pip --version >/dev/null 2>&1; then
        if [ -f "$VENV_DIR/pyvenv.cfg" ]; then
            log "Existing .venv has no working pip — recreating it ..."
            rm -rf "$VENV_DIR"
        fi
    fi

    if [ ! -x "$VENV_DIR/bin/python3" ]; then
        log "Creating .venv using $VENV_CAPABLE ..."
        # --system-site-packages so an apt-installed python3-pyqt6 can be reused.
        if ! "$VENV_CAPABLE" -m venv --system-site-packages "$VENV_DIR"; then
            log "warning: venv creation failed; falling back to a --user install."
            rm -rf "$VENV_DIR"
            [ -n "$PIP_CAPABLE" ] || exit 1
            install_and_run "$PIP_CAPABLE" --user --break-system-packages --quiet
        fi
    fi

    if "$VENV_DIR/bin/python3" -m pip --version >/dev/null 2>&1; then
        install_and_run "$VENV_DIR/bin/python3" --quiet
    fi
    log "warning: .venv still has no usable pip; falling back to a --user install."
fi

# Fallback: install into the user site-packages of whichever interpreter has
# pip. --break-system-packages satisfies PEP 668 on Ubuntu; --user keeps the
# install out of apt-managed directories, so nothing system-wide is touched.
install_and_run "$PIP_CAPABLE" --user --break-system-packages --quiet
