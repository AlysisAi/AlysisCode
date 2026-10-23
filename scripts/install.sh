#!/bin/sh
# Alysis Code installer for Linux, macOS, and Windows through WSL.
#
#   curl -LsSf https://raw.githubusercontent.com/AlysisAi/AlysisCode/main/scripts/install.sh | sh
#
# Installs uv (https://docs.astral.sh/uv/) when it is missing, then installs the
# `alysis-code` package from PyPI as an isolated uv tool with its own Python.
# The system Python, pip, pipx, and venv packages are not used, and nothing runs
# with sudo. Re-run the command at any time to upgrade.
#
# Environment variables:
#   ALYSIS_VERSION=<version> install a specific release instead of the latest one
#   ALYSIS_PYTHON=3.12       Python version for the tool environment (default 3.12)
#   ALYSIS_NO_MODIFY_PATH=1  never edit shell startup files

set -eu

UV_INSTALLER_URL="https://astral.sh/uv/install.sh"
PACKAGE="alysis-code"
# Every console script the package ships; uv refuses to overwrite files it does
# not own, so an existing pipx or pip install is detected up front.
ENTRY_POINTS="alysis alysis-code alysis-ide-bridge"

say() {
    printf '%s\n' "$*"
}

warn() {
    printf 'warning: %s\n' "$*" >&2
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

has() {
    command -v "$1" >/dev/null 2>&1
}

download() {
    if has curl; then
        curl --proto '=https' --tlsv1.2 -LsSf "$1"
    elif has wget; then
        wget --https-only -qO- "$1"
    else
        die "curl or wget is required to download uv. Install one of them and re-run."
    fi
}

check_platform() {
    case "$(uname -s)" in
        Linux | Darwin) ;;
        MINGW* | MSYS* | CYGWIN*)
            die "this installer runs on Linux, macOS, and WSL. On Windows, open your WSL terminal (set it up with 'wsl --install') and run it there."
            ;;
        *)
            die "unsupported operating system: $(uname -s)"
            ;;
    esac
}

find_uv() {
    if has uv; then
        command -v uv
        return 0
    fi
    # Where uv's own installer puts the binary when that directory is not on PATH yet.
    for candidate in \
        "${UV_INSTALL_DIR:+$UV_INSTALL_DIR/uv}" \
        "${XDG_BIN_HOME:+$XDG_BIN_HOME/uv}" \
        "${XDG_DATA_HOME:+$XDG_DATA_HOME/../bin/uv}" \
        "$HOME/.local/bin/uv" \
        "$HOME/.cargo/bin/uv"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

install_uv() {
    say "Installing uv, the Python package manager Alysis Code is installed with..."
    installer="$(mktemp)"
    if ! download "$UV_INSTALLER_URL" >"$installer"; then
        rm -f "$installer"
        die "could not download the uv installer from $UV_INSTALLER_URL"
    fi
    if [ "${ALYSIS_NO_MODIFY_PATH:-0}" = "1" ]; then
        sh "$installer" --quiet --no-modify-path || status=$?
    else
        sh "$installer" --quiet || status=$?
    fi
    rm -f "$installer"
    if [ "${status:-0}" -ne 0 ]; then
        die "the uv installer failed (exit code $status)"
    fi
}

is_uv_managed() {
    "$UV" tool list 2>/dev/null | grep -q "^$PACKAGE "
}

check_conflicts() {
    if "$UV" tool list 2>/dev/null | grep -q '^sylliptor-agent-cli '; then
        die "the old Sylliptor package is installed with uv. Remove it with 'uv tool uninstall sylliptor-agent-cli' and re-run this installer. Your settings are kept and migrate on first run."
    fi
    for name in $ENTRY_POINTS; do
        if [ -e "$BIN_DIR/$name" ] || [ -L "$BIN_DIR/$name" ]; then
            die "$BIN_DIR/$name already exists from another install (usually pipx). Keep that install and upgrade it with 'alysis update', or remove it first (for example 'pipx uninstall $PACKAGE', or 'pipx uninstall sylliptor-agent-cli' for the old package name) and re-run this installer."
        fi
    done
}

path_contains() {
    case ":$PATH:" in
        *":$1:"*) return 0 ;;
        *) return 1 ;;
    esac
}

sandbox_hint() {
    if has bwrap || has docker; then
        return 0
    fi
    say ""
    if [ "$(uname -s)" = "Darwin" ]; then
        say "Sandbox: Alysis Code runs shell commands in Docker on macOS. Install Docker Desktop,"
        say "then run 'alysis sandbox setup'."
        return 0
    fi
    if has apt-get; then
        cmd="sudo apt-get update && sudo apt-get install -y bubblewrap"
    elif has dnf; then
        cmd="sudo dnf install -y bubblewrap"
    elif has pacman; then
        cmd="sudo pacman -S bubblewrap"
    elif has zypper; then
        cmd="sudo zypper install bubblewrap"
    elif has apk; then
        cmd="sudo apk add bubblewrap"
    else
        cmd=""
    fi
    say "Sandbox: Alysis Code runs shell commands inside a sandbox (bubblewrap on Linux)."
    if [ -n "$cmd" ]; then
        say "Install it now with:"
        say ""
        say "    $cmd"
        say ""
        say "or let the setup wizard offer it the first time you run 'alysis'."
    else
        say "Install bubblewrap with your package manager, or let the setup wizard help"
        say "the first time you run 'alysis'."
    fi
}

is_wsl() {
    [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qi microsoft /proc/version 2>/dev/null
}

main() {
    check_platform
    [ -n "${HOME:-}" ] || die "HOME is not set"

    uv_installed_now=0
    if ! UV="$(find_uv)"; then
        install_uv
        uv_installed_now=1
        UV="$(find_uv)" || die "uv was installed, but its executable was not found. Open a new terminal and re-run this installer."
    fi

    BIN_DIR="$("$UV" tool dir --bin)" || die "could not determine uv's tool directory"

    python_version="${ALYSIS_PYTHON:-3.12}"
    spec="$PACKAGE"
    if [ -n "${ALYSIS_VERSION:-}" ]; then
        spec="$PACKAGE==${ALYSIS_VERSION#v}"
    fi

    if is_uv_managed; then
        say "Upgrading Alysis Code..."
        # --reinstall re-resolves against the index, so a re-run also upgrades.
        "$UV" tool install --quiet --reinstall --python "$python_version" "$spec" ||
            die "the upgrade failed. See the uv output above."
    else
        check_conflicts
        say "Installing Alysis Code (Python $python_version is downloaded automatically if needed)..."
        "$UV" tool install --quiet --python "$python_version" "$spec" ||
            die "the install failed. See the uv output above."
    fi

    alysis_bin="$BIN_DIR/alysis"
    [ -x "$alysis_bin" ] || die "the install finished, but $alysis_bin is missing"
    # `alysis --version` prints "<version> (build details)"; keep the version.
    version="$("$alysis_bin" --version 2>/dev/null)" || die "$alysis_bin does not start. Run it directly to see the error."
    version="${version%% *}"

    path_note=""
    if ! path_contains "$BIN_DIR"; then
        if [ "${ALYSIS_NO_MODIFY_PATH:-0}" = "1" ]; then
            path_note="Add $BIN_DIR to your PATH to run 'alysis'."
        elif { [ "$uv_installed_now" -eq 1 ] && [ "$(dirname "$UV")" = "$BIN_DIR" ]; } ||
            "$UV" tool update-shell --quiet >/dev/null 2>&1; then
            # Either uv's own installer already put this directory on PATH for new
            # shells, or `uv tool update-shell` just did.
            path_note="Open a new terminal (or run: export PATH=\"$BIN_DIR:\$PATH\") so 'alysis' is on your PATH."
        else
            path_note="Add $BIN_DIR to your PATH to run 'alysis', for example with this line in ~/.profile: export PATH=\"$BIN_DIR:\$PATH\""
        fi
    else
        found="$(command -v alysis 2>/dev/null || true)"
        if [ -n "$found" ] && [ "$found" != "$alysis_bin" ]; then
            warn "'alysis' on your PATH is $found, which comes before the new install at $alysis_bin."
        fi
    fi

    say ""
    say "Alysis Code $version is installed."
    if [ -n "$path_note" ]; then
        say ""
        say "$path_note"
    fi
    sandbox_hint
    if is_wsl; then
        say ""
        say "Tip: keep your projects in the Linux file system (for example ~/projects), not"
        say "under /mnt/c. File access there is much slower."
    fi
    say ""
    say "Next: run 'alysis login' and approve the code in your browser to use your Alysis credits."
    say "Then go to your project folder and run 'alysis'. You can also choose your own provider in setup."
}

main "$@"
