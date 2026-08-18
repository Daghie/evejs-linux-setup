#!/usr/bin/env bash
# ==============================================================================
# EveJS Universal Linux Setup v1.8 — Installer + Transactional Updater
# Target: EveJS v0.12.6 / EVE build 3396210
# Architecture baseline: live-accepted v0.12.4.1 Universal Linux Installer v1.4
#
# Goals:
#   * Portable across mainstream x86_64 Linux distributions with Steam installed.
#   * GUI wizard with Zenity/KDialog when available; text fallback otherwise.
#   * Detect official Steam Proton installations across Steam libraries.
#   * Default to the newest numbered official Proton, with user confirmation.
#   * Ask for the EVE TQ client and EveJS destination.
#   * Preserve EveJS core as a clean baseline.
#   * Support removable mods through mods/*/loader.js.
#   * Reproduce the Linux fixes validated during Bazzite testing:
#       - market daemon starts before the main game server
#       - local port 443 permission handled without running EveJS as root
#       - XMPP advertises 127.0.0.1 rather than localhost
#       - stale EveJS CAs removed from client cacert.pem bundles
#       - live TLS verification for XMPP 5222 and gateway 26003
#       - Windows-visible Z:\ path supplied for the EveJS CA under Proton
#
# This installer never patches EveJS core source files after extraction.
# Client preparation modifies only the copied EVE client selected by the user.
# ==============================================================================

set -Eeuo pipefail
IFS=$'\n\t'

INSTALLER_TARGET_VERSION="0.12.6"
INSTALLER_TARGET_BUILD="3396210"
INSTALLER_CANDIDATE_NAME="v1.8 Installer + Transactional Updater"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER_SOURCE="$SCRIPT_DIR/evejs-linux-launcher.sh"
STALE_CLEANUP_SOURCE="$SCRIPT_DIR/evejs-v0.12.6-stale-processes.sh"
MAINTENANCE_SOURCE="$SCRIPT_DIR/evejs-linux-maintenance.py"

FORCE_TEXT=0
ARG_ZIP=""
ARG_CLIENT=""
ARG_DEST=""
ARG_PROTON=""
ARG_MARKET=""
ARG_NO_LAUNCH=0
ARG_NO_DESKTOP=0
ARG_MODE=""
ARG_EXISTING=""
ARG_BACKUP=""

while (($#)); do
    case "$1" in
        --text) FORCE_TEXT=1; shift ;;
        --zip) ARG_ZIP="${2:-}"; shift 2 ;;
        --client) ARG_CLIENT="${2:-}"; shift 2 ;;
        --dest) ARG_DEST="${2:-}"; shift 2 ;;
        --proton) ARG_PROTON="${2:-}"; shift 2 ;;
        --market) ARG_MARKET="${2:-}"; shift 2 ;;
        --no-launch) ARG_NO_LAUNCH=1; shift ;;
        --no-desktop) ARG_NO_DESKTOP=1; shift ;;
        --mode) ARG_MODE="${2:-}"; shift 2 ;;
        --existing) ARG_EXISTING="${2:-}"; shift 2 ;;
        --backup) ARG_BACKUP="${2:-}"; shift 2 ;;
        -h|--help)
            cat <<'EOF'
Usage:
  ./install-evejs-linux.sh [options]

Options:
  --text              Force text-only wizard.
  --zip PATH          EveJS release ZIP.
  --client PATH       EVE tq directory or copied shared-cache root.
  --dest PATH         Destination for clean EveJS installation.
  --proton PATH       Proton directory or proton executable.
  --market PROFILE    v0.12.6 market profile: v1, v2, or v3.
  --no-launch         Do not launch EveJS when setup finishes.
  --no-desktop        Do not offer/create a desktop-menu shortcut.
  --mode MODE         install, update, restore, or backups.
  --existing PATH     Existing EveJS installation for update/restore/backups.
  --backup PATH       Backup record path for non-interactive restore.
EOF
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    echo "Do not run this installer as root. It uses sudo only for narrowly scoped system operations." >&2
    exit 1
fi

# ── UI abstraction ────────────────────────────────────────────────────────────
UI="text"
if (( ! FORCE_TEXT )) && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    if command -v zenity >/dev/null 2>&1; then
        UI="zenity"
    elif command -v kdialog >/dev/null 2>&1; then
        UI="kdialog"
    fi
fi

ui_info() {
    local title="$1" msg="$2"
    case "$UI" in
        zenity) zenity --info --title="$title" --width=620 --text="$msg" ;;
        kdialog) kdialog --title "$title" --msgbox "$msg" ;;
        *) printf '\n=== %s ===\n%s\n\n' "$title" "$msg" ;;
    esac
}

ui_warn() {
    local title="$1" msg="$2"
    case "$UI" in
        zenity) zenity --warning --title="$title" --width=650 --text="$msg" ;;
        kdialog) kdialog --title "$title" --sorry "$msg" ;;
        *) printf '\n[WARNING] %s\n%s\n\n' "$title" "$msg" >&2 ;;
    esac
}

ui_error() {
    local title="$1" msg="$2"
    case "$UI" in
        zenity) zenity --error --title="$title" --width=680 --text="$msg" ;;
        kdialog) kdialog --title "$title" --error "$msg" ;;
        *) printf '\n[ERROR] %s\n%s\n\n' "$title" "$msg" >&2 ;;
    esac
}

ui_yesno() {
    local title="$1" msg="$2"
    case "$UI" in
        zenity) zenity --question --title="$title" --width=650 --text="$msg" ;;
        kdialog) kdialog --title "$title" --yesno "$msg" ;;
        *)
            local ans
            printf '\n%s\n%s\n' "$title" "$msg"
            read -r -p "Continue? [y/N]: " ans
            [[ "${ans,,}" == "y" || "${ans,,}" == "yes" ]]
            ;;
    esac
}

ui_entry() {
    local title="$1" prompt="$2" default="${3:-}"
    case "$UI" in
        zenity) zenity --entry --title="$title" --width=720 --text="$prompt" --entry-text="$default" ;;
        kdialog) kdialog --title "$title" --inputbox "$prompt" "$default" ;;
        *)
            local ans
            printf '\n%s\n%s\n' "$title" "$prompt" >&2
            read -r -p "[$default] " ans
            printf '%s\n' "${ans:-$default}"
            ;;
    esac
}

ui_file() {
    local title="$1" start="${2:-$HOME}"
    case "$UI" in
        zenity) zenity --file-selection --title="$title" --filename="$start/" ;;
        kdialog) kdialog --title "$title" --getopenfilename "$start" ;;
        *)
            local ans
            printf '\n%s\n' "$title" >&2
            read -r -p "File path: " ans
            printf '%s\n' "$ans"
            ;;
    esac
}

ui_dir() {
    local title="$1" start="${2:-$HOME}"
    case "$UI" in
        zenity) zenity --file-selection --directory --title="$title" --filename="$start/" ;;
        kdialog) kdialog --title "$title" --getexistingdirectory "$start" ;;
        *)
            local ans
            printf '\n%s\n' "$title" >&2
            read -r -p "Directory path: " ans
            printf '%s\n' "$ans"
            ;;
    esac
}

ui_choose_proton() {
    local records_file="$1"
    case "$UI" in
        zenity)
            local rows=()
            while IFS=$'\t' read -r score name path steamroot channel version; do
                rows+=(FALSE "$name" "$channel" "$path")
            done < "$records_file"
            zenity --list --radiolist \
                --title="Choose Steam Proton" --width=1000 --height=520 \
                --text="Choose the official Steam Proton build EveJS should use." \
                --column="Use" --column="Proton" --column="Channel" --column="Path" \
                "${rows[@]}" --print-column=4
            ;;
        kdialog)
            local args=()
            local idx=0 score name path steamroot channel version
            while IFS=$'\t' read -r score name path steamroot channel version; do
                idx=$((idx+1))
                args+=("$idx" "$name — $channel — $path")
            done < "$records_file"
            local pick
            pick="$(kdialog --title "Choose Steam Proton" --menu "Choose the official Steam Proton build EveJS should use:" "${args[@]}")" || return 1
            sed -n "${pick}p" "$records_file" | cut -f3
            ;;
        *)
            local idx=0 score name path steamroot channel version
            declare -a paths=()
            printf '\nInstalled Steam Proton versions:\n' >&2
            while IFS=$'\t' read -r score name path steamroot channel version; do
                idx=$((idx+1)); paths[$idx]="$path"
                printf '  [%d] %s — %s\n      %s\n' "$idx" "$name" "$channel" "$path" >&2
            done < "$records_file"
            local pick
            read -r -p "Choose Proton [1-$idx]: " pick
            [[ "$pick" =~ ^[0-9]+$ ]] && [[ -n "${paths[$pick]:-}" ]] || return 1
            printf '%s\n' "${paths[$pick]}"
            ;;
    esac
}


ui_choose_market() {
    case "$UI" in
        zenity)
            zenity --list --radiolist \
                --title="Choose EveJS v0.12.6 market" --width=960 --height=390 \
                --text="Choose one v0.12.6 market seed strategy. v1 is recommended for the first Linux acceptance run because it is deterministic, offline, and removes live market downloads as an extra variable." \
                --column="Use" --column="Profile" --column="Description" \
                TRUE  "v1" "Synthetic Jita + New Caldari — deterministic, offline, recommended first test" \
                FALSE "v2" "Live Tranquility snapshot — Jita live market + NPC catalog; downloads current snapshot" \
                FALSE "v3" "Jita 4-4 manufacturing-cost index + authentic TQ NPC catalog; audited" \
                --print-column=2
            ;;
        kdialog)
            local pick
            pick="$(kdialog --title "Choose EveJS v0.12.6 market" --menu \
                "Choose one market seed strategy. v1 is recommended for the first Linux acceptance run." \
                v1 "Synthetic Jita + New Caldari — deterministic/offline" \
                v2 "Live TQ snapshot — Jita + NPC catalog" \
                v3 "Cost-priced Jita 4-4 + authentic NPC catalog")" || return 1
            printf '%s\n' "$pick"
            ;;
        *)
            local pick
            printf '\nChoose EveJS v0.12.6 market profile:\n' >&2
            printf '  [1] v1 — Synthetic Jita + New Caldari (recommended first test)\n' >&2
            printf '  [2] v2 — Live TQ snapshot, Jita + NPC catalog\n' >&2
            printf '  [3] v3 — Cost-priced Jita 4-4 + authentic NPC catalog\n' >&2
            read -r -p "Choose [1-3, default 1]: " pick
            case "${pick:-1}" in
                1) printf 'v1\n' ;;
                2) printf 'v2\n' ;;
                3) printf 'v3\n' ;;
                *) return 1 ;;
            esac
            ;;
    esac
}


ui_choose_setup_action() {
    case "$UI" in
        zenity)
            zenity --list --radiolist \
                --title="EveJS Universal Linux Setup" --width=880 --height=420 \
                --text="Install a fresh EveJS copy, update an existing installation in place, or manage transactional upgrade backups." \
                --column="Use" --column="Action" --column="Description" \
                TRUE  "install"  "Install EveJS v0.12.6 into an existing empty folder" \
                FALSE "update"   "Update an existing Universal Linux installation to v0.12.6 while preserving player data/mods" \
                FALSE "restore"  "Restore a previous transactional EveJS upgrade backup" \
                FALSE "backups"  "Inspect or delete EveJS upgrade backups" \
                --print-column=2
            ;;
        kdialog)
            kdialog --title "EveJS Universal Linux Setup" --menu \
                "Choose what you want to do:" \
                install "Install EveJS v0.12.6 into an empty folder" \
                update "Update an existing EveJS installation in place" \
                restore "Restore an upgrade backup" \
                backups "Inspect or delete upgrade backups"
            ;;
        *)
            local pick
            printf '\nEveJS Universal Linux Setup\n' >&2
            printf '  [1] Install EveJS v0.12.6\n' >&2
            printf '  [2] Update an existing EveJS installation\n' >&2
            printf '  [3] Restore an upgrade backup\n' >&2
            printf '  [4] Manage upgrade backups\n' >&2
            printf '  [5] Exit\n' >&2
            read -r -p "Choose [1-5]: " pick
            case "$pick" in
                1) printf 'install\n' ;;
                2) printf 'update\n' ;;
                3) printf 'restore\n' ;;
                4) printf 'backups\n' ;;
                5|'') printf 'exit\n' ;;
                *) return 1 ;;
            esac
            ;;
    esac
}


stage() {
    printf '\n\033[1;36m==> %s\033[0m\n' "$*"
}

ok() {
    printf '    \033[0;32m✓\033[0m %s\n' "$*"
}

warn() {
    printf '    \033[1;33m!\033[0m %s\n' "$*" >&2
}

die() {
    local msg="$*"
    ui_error "EveJS Linux Installer" "$msg"
    printf '\n[ERROR] %s\n' "$msg" >&2
    exit 1
}

# ── System dependency handling ────────────────────────────────────────────────
detect_os_family() {
    local id="" like=""
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        id="${ID:-}"
        like="${ID_LIKE:-}"
    fi
    printf '%s %s\n' "$id" "$like"
}


is_steamos() {
    local id="" like="" name=""
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        id="${ID:-}"
        like="${ID_LIKE:-}"
        name="${NAME:-}"
    fi

    [[ "$id" == "steamos" ]] \
        || [[ "$like" == *"steamos"* ]] \
        || [[ "$name" == *"SteamOS"* ]] \
        || command -v steamos-unminimize >/dev/null 2>&1
}

native_toolchain_report() {
    local destination="$1"
    {
        echo "EveJS Linux native build environment"
        echo "===================================="
        echo "Timestamp: $(date -Is 2>/dev/null || date)"
        echo
        echo "/etc/os-release:"
        if [[ -r /etc/os-release ]]; then
            cat /etc/os-release
        else
            echo "(not available)"
        fi
        echo
        echo "Kernel:"
        uname -a || true
        echo
        echo "Filesystem:"
        df -h / 2>/dev/null || true
        echo
        echo "Compiler commands:"
        echo "cc:  $(command -v cc 2>/dev/null || echo missing)"
        echo "c++: $(command -v c++ 2>/dev/null || echo missing)"
        cc --version 2>/dev/null | head -n 2 || true
        c++ --version 2>/dev/null | head -n 2 || true
        echo
        echo "Core header state:"
        for h in /usr/include/stdio.h /usr/include/stdlib.h /usr/include/string.h /usr/include/c++; do
            if [[ -e "$h" ]]; then
                echo "present: $h"
            else
                echo "MISSING: $h"
            fi
        done
        echo
        if command -v pacman >/dev/null 2>&1; then
            echo "Pacman package state:"
            pacman -Q glibc 2>&1 || true
            pacman -Q base-devel 2>&1 || true
            echo
            echo "Does installed glibc metadata expect stdio.h?"
            pacman -Ql glibc 2>/dev/null | grep -F 'usr/include/stdio.h' || true
            echo
        fi
        if command -v steamos-readonly >/dev/null 2>&1; then
            echo "SteamOS read-only state:"
            steamos-readonly status 2>&1 || true
            echo
        fi
    } > "$destination"
}

native_c_probe() {
    local tmp log
    tmp="$(mktemp -d)"
    log="$tmp/c-probe.log"

    cat > "$tmp/probe.c" <<'EOF'
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(void) {
    const char *value = "evejs";
    printf("%zu\n", strlen(value));
    return value[0] == 'e' ? EXIT_SUCCESS : EXIT_FAILURE;
}
EOF

    NATIVE_C_LAST_ERROR=""
    if ! cc "$tmp/probe.c" -o "$tmp/probe-c" >"$log" 2>&1; then
        NATIVE_C_LAST_ERROR="$(cat "$log")"
        rm -rf "$tmp"
        return 1
    fi
    if ! "$tmp/probe-c" >/dev/null 2>&1; then
        NATIVE_C_LAST_ERROR="C compiler produced a test binary, but it did not execute successfully."
        rm -rf "$tmp"
        return 1
    fi
    rm -rf "$tmp"
    return 0
}

native_cpp_probe() {
    local tmp log
    tmp="$(mktemp -d)"
    log="$tmp/cpp-probe.log"

    cat > "$tmp/probe.cpp" <<'EOF'
#include <iostream>
#include <string>
int main() {
    std::string value = "evejs";
    std::cout << value.size() << std::endl;
    return value.empty();
}
EOF

    NATIVE_CPP_LAST_ERROR=""
    if ! c++ "$tmp/probe.cpp" -o "$tmp/probe-cpp" >"$log" 2>&1; then
        NATIVE_CPP_LAST_ERROR="$(cat "$log")"
        rm -rf "$tmp"
        return 1
    fi
    if ! "$tmp/probe-cpp" >/dev/null 2>&1; then
        NATIVE_CPP_LAST_ERROR="C++ compiler produced a test binary, but it did not execute successfully."
        rm -rf "$tmp"
        return 1
    fi
    rm -rf "$tmp"
    return 0
}

native_build_probe() {
    NATIVE_BUILD_LAST_ERROR=""
    NATIVE_BUILD_FAILED_STAGE=""

    if ! native_c_probe; then
        NATIVE_BUILD_FAILED_STAGE="c"
        NATIVE_BUILD_LAST_ERROR="$NATIVE_C_LAST_ERROR"
        return 1
    fi

    if ! native_cpp_probe; then
        NATIVE_BUILD_FAILED_STAGE="cpp"
        NATIVE_BUILD_LAST_ERROR="$NATIVE_CPP_LAST_ERROR"
        return 1
    fi

    return 0
}

declare -A STEAMOS_REPAIR_DONE=()
STEAMOS_REPAIR_LOG=""

steamos_installed_version() {
    LC_ALL=C pacman -Q "$1" 2>/dev/null | awk '{print $2; exit}'
}

steamos_sync_version() {
    LC_ALL=C pacman -Si "$1" 2>/dev/null | awk -F': *' '$1 ~ /^Version[[:space:]]*$/ {print $2; exit}'
}

steamos_sync_repo() {
    LC_ALL=C pacman -Si "$1" 2>/dev/null | awk -F': *' '$1 ~ /^Repository[[:space:]]*$/ {print $2; exit}'
}

steamos_sync_download_size() {
    LC_ALL=C pacman -Si "$1" 2>/dev/null | awk -F': *' '$1 ~ /^Download Size[[:space:]]*$/ {print $2; exit}'
}

steamos_preview_single_package() {
    local pkg="$1" line
    local -a transaction=()

    mapfile -t transaction < <(
        LC_ALL=C pacman -Sp --print-format '%n|%v' "$pkg" 2>/dev/null | awk 'NF'
    )

    ((${#transaction[@]} > 0)) || return 1

    # Absolute guard: This setup will not approve a dependency cascade. The dry-run
    # must contain exactly the single package we deliberately requested.
    ((${#transaction[@]} == 1)) || return 2

    line="${transaction[0]}"
    [[ "${line%%|*}" == "$pkg" ]] || return 2
    printf '%s\n' "$line"
}

steamos_reinstall_exact_package() {
    local pkg="$1" purpose="$2"
    local installed sync repo download preview preview_version ro_state=""

    [[ -n "${STEAMOS_REPAIR_DONE[$pkg]:-}" ]] && return 0

    command -v pacman >/dev/null 2>&1 \
        || die "SteamOS minimal repair requires Pacman, but pacman was not found."

    installed="$(steamos_installed_version "$pkg")"
    [[ -n "$installed" ]] || die "SteamOS minimal repair expected '$pkg' to already be installed, but it is not.

This setup will not add an unbounded package/dependency set automatically. Please send the installer output so this case can be reviewed."

    sync="$(steamos_sync_version "$pkg")"
    repo="$(steamos_sync_repo "$pkg")"
    download="$(steamos_sync_download_size "$pkg")"

    [[ -n "$sync" ]] || die "Could not read the SteamOS repository version for '$pkg'.

This setup deliberately does not run 'pacman -Sy' because refreshing only the package database and then changing core packages can create a partial-upgrade situation.

Update SteamOS normally, reboot if requested, then rerun the EveJS installer."

    if [[ "$installed" != "$sync" ]]; then
        die "SteamOS package-version safety check refused to repair '$pkg'.

Installed version:
  $installed

Configured SteamOS repository version:
  $sync

This setup only reinstalls the EXACT version SteamOS already has installed. It will not partially upgrade a core system package.

Update SteamOS normally first, then rerun the installer."
    fi

    local preview_rc=0
    preview="$(steamos_preview_single_package "$pkg")" || preview_rc=$?
    case "$preview_rc" in
        0) ;;
        1)
            die "Pacman could not preview a reinstall of '$pkg'. No system changes were made."
            ;;
        2)
            local full_preview
            full_preview="$(LC_ALL=C pacman -Sp --print-format '%n %v' "$pkg" 2>/dev/null || true)"
            die "SteamOS minimal-repair safety guard STOPPED the operation.

EveJS requested only:
  $pkg

But Pacman proposed more than that one package:
$full_preview

This setup will not approve a dependency cascade or broad SteamOS restoration. No system changes were made."
            ;;
        *)
            die "Unexpected Pacman preview error for '$pkg'. No system changes were made."
            ;;
    esac

    preview_version="${preview#*|}"
    [[ "$preview_version" == "$installed" ]] || die "Pacman preview version for '$pkg' changed unexpectedly.

Installed: $installed
Preview:   $preview_version

No system changes were made."

    if command -v steamos-readonly >/dev/null 2>&1; then
        ro_state="$(steamos-readonly status 2>&1 || true)"
    fi

    if ! ui_yesno "Repair one SteamOS package?" \
"EveJS proved that a native development file is missing.

Required repair:
  $purpose

This setup proposes to REINSTALL exactly ONE already-installed SteamOS package:

  Package:     $pkg
  Version:     $installed
  Repository:  ${repo:-unknown}
  Download:    ${download:-unknown}

Pacman's dry-run contains exactly this one package and no dependency cascade.

This is NOT steamos-unminimize.
This is NOT base-devel.
This is NOT a 959-package restoration.

SteamOS read-only status:
${ro_state:-unknown}

SteamOS updates may replace system-image changes later.

Reinstall this one exact package and retry the compiler test?"; then
        die "The required SteamOS development file is still missing. Installation stopped without changing '$pkg'."
    fi

    stage "SteamOS minimal repair: reinstalling only $pkg"

    sudo -v || die "sudo authentication failed."

    if command -v steamos-readonly >/dev/null 2>&1; then
        sudo steamos-readonly disable \
            || die "Could not disable SteamOS read-only mode."
    fi

    # Re-run the transaction preview immediately before the write.
    local second_preview second_rc=0
    second_preview="$(steamos_preview_single_package "$pkg")" || second_rc=$?
    [[ "$second_rc" == "0" && "$second_preview" == "$pkg|$installed" ]] \
        || die "Pacman transaction changed after approval. the setup aborted before modifying '$pkg'."

    sudo pacman -S --noconfirm "$pkg" \
        || die "SteamOS failed to reinstall the single approved package '$pkg'."

    STEAMOS_REPAIR_DONE["$pkg"]=1
    STEAMOS_REPAIR_LOG+="${pkg} ${installed} — ${purpose}"$'\n'
    ok "Reinstalled only $pkg $installed."
}

steamos_repair_from_c_failure() {
    local err="$1"

    # The exact tester failure: bundled SQLite -> cc -> stdio.h missing.
    if [[ ! -e /usr/include/stdio.h ]] || grep -Eq 'fatal error: (stdio\.h|stdlib\.h|string\.h): No such file or directory' <<<"$err"; then
        steamos_reinstall_exact_package \
            "glibc" \
            "restore the standard C library development headers (including stdio.h)"
        return 0
    fi

    # Kernel-facing userspace headers are a separate package.
    if grep -Eq 'fatal error: (linux/|asm/|asm-generic/)[^:]*: No such file or directory' <<<"$err"; then
        steamos_reinstall_exact_package \
            "linux-api-headers" \
            "restore Linux userspace API headers required by native compilation"
        return 0
    fi

    # Missing linker binary.
    if ! command -v ld >/dev/null 2>&1 \
        || grep -Eq '(cannot find .*ld|cannot execute .*ld|linker .* not found)' <<<"$err"; then
        steamos_reinstall_exact_package \
            "binutils" \
            "restore the native linker/toolchain utilities"
        return 0
    fi

    # Startup objects and libc link files belong to glibc too.
    if grep -Eq '(cannot find (Scrt1\.o|crt1\.o|crti\.o|crtn\.o)|cannot find -lc)' <<<"$err"; then
        steamos_reinstall_exact_package \
            "glibc" \
            "restore libc startup/link files required by the C compiler"
        return 0
    fi

    return 1
}

steamos_repair_from_cpp_failure() {
    local err="$1"

    if grep -Eq 'fatal error: (iostream|string|bits/[^:]+): No such file or directory' <<<"$err"; then
        steamos_reinstall_exact_package \
            "gcc" \
            "restore the GCC C++ standard-library development headers"
        return 0
    fi

    if ! command -v ld >/dev/null 2>&1 \
        || grep -Eq '(cannot find .*ld|cannot execute .*ld|linker .* not found)' <<<"$err"; then
        steamos_reinstall_exact_package \
            "binutils" \
            "restore the native linker/toolchain utilities"
        return 0
    fi

    if grep -Eq '(cannot find (Scrt1\.o|crt1\.o|crti\.o|crtn\.o)|cannot find -lc)' <<<"$err"; then
        steamos_reinstall_exact_package \
            "glibc" \
            "restore libc startup/link files required by the C++ compiler"
        return 0
    fi

    return 1
}

repair_steamos_native_build_environment() {
    local attempts=0 max_attempts=5

    while (( attempts < max_attempts )); do
        if native_c_probe; then
            break
        fi

        warn "SteamOS C compiler probe failed:"
        printf '%s\n' "$NATIVE_C_LAST_ERROR" >&2

        steamos_repair_from_c_failure "$NATIVE_C_LAST_ERROR" \
            || die "The SteamOS C compiler still fails, but this setup cannot prove that one of its approved minimal packages is the cause.

No broad SteamOS repair will be attempted.

Compiler output:
$NATIVE_C_LAST_ERROR"

        attempts=$((attempts + 1))
    done

    native_c_probe || die "SteamOS C compiler did not recover after minimal targeted repair."

    attempts=0
    while (( attempts < max_attempts )); do
        if native_cpp_probe; then
            break
        fi

        warn "SteamOS C++ compiler probe failed:"
        printf '%s\n' "$NATIVE_CPP_LAST_ERROR" >&2

        steamos_repair_from_cpp_failure "$NATIVE_CPP_LAST_ERROR" \
            || die "The SteamOS C++ compiler still fails, but this setup cannot prove that one of its approved minimal packages is the cause.

No broad SteamOS repair will be attempted.

Compiler output:
$NATIVE_CPP_LAST_ERROR"

        attempts=$((attempts + 1))
    done

    native_cpp_probe || die "SteamOS C++ compiler did not recover after minimal targeted repair."

    ok "SteamOS native C/C++ environment passed after minimal targeted repair."
}

ensure_native_build_environment() {
    HOST_TOOLCHAIN_REPORT="$(mktemp)"
    native_toolchain_report "$HOST_TOOLCHAIN_REPORT"

    if native_build_probe; then
        ok "Native C/C++ build environment passed compile + run probes."
        return 0
    fi

    local first_error="$NATIVE_BUILD_LAST_ERROR"

    if is_steamos; then
        repair_steamos_native_build_environment
        native_toolchain_report "$HOST_TOOLCHAIN_REPORT"
        return 0
    fi

    local family install_cmd
    family="$(detect_os_family)"
    install_cmd="$(dependency_install_command)"

    die "The compiler command exists, but the Linux native development environment is incomplete.

Compiler output:
$first_error

This usually means libc/C/C++ development headers are missing or the compiler/sysroot is incomplete.

Detected package family:
$family

Suggested dependency command:
${install_cmd:-No automatic command is available for this distribution.}

Install/repair the native development packages and rerun the installer."
}

dependency_install_command() {
    local family
    family="$(detect_os_family)"
    if [[ "$family" == *"debian"* || "$family" == *"ubuntu"* ]]; then
        printf '%s\n' 'sudo apt-get update && sudo apt-get install -y python3 curl unzip openssl xz-utils iproute2 procps build-essential pkg-config'
    elif [[ "$family" == *"fedora"* || "$family" == *"rhel"* || "$family" == *"centos"* ]]; then
        if command -v dnf >/dev/null 2>&1; then
            printf '%s\n' 'sudo dnf install -y python3 curl unzip openssl xz iproute procps-ng gcc gcc-c++ make pkgconf-pkg-config'
        else
            printf '%s\n' ''
        fi
    elif [[ "$family" == *"arch"* || "$family" == *"manjaro"* ]]; then
        printf '%s\n' 'sudo pacman -S --needed python curl unzip openssl xz iproute2 procps-ng base-devel'
    elif [[ "$family" == *"suse"* ]]; then
        printf '%s\n' 'sudo zypper install -y python3 curl unzip openssl xz iproute2 procps gcc gcc-c++ make pkg-config'
    else
        printf '%s\n' ''
    fi
}

steamos_package_for_command() {
    case "$1" in
        python3) printf 'python\n' ;;
        curl) printf 'curl\n' ;;
        unzip) printf 'unzip\n' ;;
        openssl) printf 'openssl\n' ;;
        tar) printf 'tar\n' ;;
        xz) printf 'xz\n' ;;
        ss) printf 'iproute2\n' ;;
        sysctl) printf 'procps-ng\n' ;;
        sudo) printf 'sudo\n' ;;
        sha256sum) printf 'coreutils\n' ;;
        awk) printf 'gawk\n' ;;
        grep) printf 'grep\n' ;;
        sed) printf 'sed\n' ;;
        find) printf 'findutils\n' ;;
        make) printf 'make\n' ;;
        cc|c++) printf 'gcc\n' ;;
        *) return 1 ;;
    esac
}

check_system_tools() {
    local missing=()
    local cmd
    for cmd in python3 curl unzip openssl tar xz ss sysctl sudo sha256sum awk grep sed find make cc c++; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if ((${#missing[@]} == 0)); then
        return 0
    fi

    if is_steamos; then
        warn "SteamOS is missing required command(s): ${missing[*]}"
        local pkg
        for cmd in "${missing[@]}"; do
            pkg="$(steamos_package_for_command "$cmd" 2>/dev/null || true)"
            [[ -n "$pkg" ]] || die "No approved minimal SteamOS package mapping exists for missing command '$cmd'."

            steamos_reinstall_exact_package \
                "$pkg" \
                "restore the missing '$cmd' command required by the EveJS installer"

            command -v "$cmd" >/dev/null 2>&1 \
                || die "Reinstalling '$pkg' did not restore required command '$cmd'."
        done
        return 0
    fi

    local install_cmd
    install_cmd="$(dependency_install_command)"
    local msg="The following host tools are missing:

${missing[*]}

EveJS needs them for database creation, native Node modules, Rust market compilation, TLS verification and local networking."

    if [[ -n "$install_cmd" ]]; then
        msg+="

Detected package-family command:
$install_cmd"
        if ui_yesno "Install Linux prerequisites?" "$msg

The installer can run that package-manager command now."; then
            bash -lc "$install_cmd" || die "Package-manager dependency installation failed."
        else
            die "Required Linux tools are missing. Install them and run the installer again."
        fi
    else
        die "$msg

This distribution was not recognized for automatic package installation. Install the equivalent packages with your distro's package manager, then run the installer again."
    fi

    missing=()
    for cmd in python3 curl unzip openssl tar xz ss sysctl sudo sha256sum awk grep sed find make cc c++; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    ((${#missing[@]} == 0)) || die "Some prerequisites are still missing: ${missing[*]}"
}

ensure_node() {
    local major=0 version=""
    if command -v node >/dev/null 2>&1; then
        major="$(node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || echo 0)"
        version="$(node --version 2>/dev/null || true)"
    fi

    # EveJS v0.12.6 README specifies Node.js 24 LTS.
    if [[ "$major" == "24" ]] && command -v npm >/dev/null 2>&1; then
        NODE_BIN="$(command -v node)"
        NPM_BIN="$(command -v npm)"
        ok "Using recommended Node $version from $NODE_BIN"
        return 0
    fi

    # v0.12.6 doc/NON_DOCKER_SETUP_AUDIT.md documents the current lock as
    # compatible with Node 20 and 22-26, but not Node 21.
    if [[ "$major" =~ ^(20|22|23|25|26)$ ]] && command -v npm >/dev/null 2>&1; then
        if ui_yesno "Use compatible Node $version?" \
"EveJS v0.12.6 recommends Node.js 24 LTS.

The detected $version is within the compatibility range documented by the v0.12.6 non-Docker setup audit, but it is not the recommended Node 24 target.

Use this existing Node installation?

Choose No to install a private Node 24.x runtime for EveJS."; then
            NODE_BIN="$(command -v node)"
            NPM_BIN="$(command -v npm)"
            ok "Using compatible Node $version from $NODE_BIN"
            return 0
        fi
    elif [[ "$major" == "21" ]]; then
        ui_warn "Unsupported Node 21 detected" \
"EveJS v0.12.6's non-Docker setup audit specifically excludes Node 21 for the current dependency lock.

The installer will offer Node 24 LTS instead."
    elif [[ "$major" =~ ^[0-9]+$ ]] && (( major > 0 )); then
        ui_warn "Unvalidated Node version" \
"Detected $version.

EveJS v0.12.6 recommends Node 24 LTS. The installer will offer a private Node 24 runtime rather than assume this major is compatible."
    fi

    local arch
    case "$(uname -m)" in
        x86_64|amd64) arch="x64" ;;
        aarch64|arm64) arch="arm64" ;;
        *) die "Automatic Node installation does not support architecture $(uname -m)." ;;
    esac

    if ! ui_yesno "Install Node.js 24 LTS?" \
"EveJS v0.12.6 recommends Node.js 24 LTS.

Install the newest Node 24.x binary in your user account under:

$HOME/.local/share/evejs-linux/node

No system Node packages will be replaced."; then
        die "A compatible Node.js runtime is required."
    fi

    stage "Installing user-local Node.js 24 LTS"
    local base="https://nodejs.org/dist/latest-v24.x"
    local tmp
    tmp="$(mktemp -d)"
    curl -fL --retry 3 "$base/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt"
    local filename
    filename="$(awk -v a="$arch" '$2 ~ ("node-v24\\..*-linux-" a "\\.tar\\.xz$") {print $2; exit}' "$tmp/SHASUMS256.txt")"
    [[ -n "$filename" ]] || die "Could not resolve the latest Node 24.x Linux $arch archive."
    curl -fL --retry 3 "$base/$filename" -o "$tmp/$filename"
    (
        cd "$tmp"
        grep "  $filename\$" SHASUMS256.txt | sha256sum -c -
    ) || die "Node.js download checksum verification failed."

    local node_home="$HOME/.local/share/evejs-linux/node"
    rm -rf "$node_home"
    mkdir -p "$node_home"
    tar -xJf "$tmp/$filename" --strip-components=1 -C "$node_home"
    rm -rf "$tmp"

    export PATH="$node_home/bin:$PATH"
    NODE_BIN="$node_home/bin/node"
    NPM_BIN="$node_home/bin/npm"
    [[ -x "$NODE_BIN" && -x "$NPM_BIN" ]] || die "User-local Node installation failed."
    ok "Installed $("$NODE_BIN" --version) at $node_home"
}

ensure_rust() {
    [[ -f "$HOME/.cargo/env" ]] && . "$HOME/.cargo/env"
    if command -v cargo >/dev/null 2>&1 && command -v rustc >/dev/null 2>&1; then
        CARGO_BIN="$(command -v cargo)"
        ok "Using $(cargo --version)"
        return 0
    fi

    if ! ui_yesno "Install Rust?" "EveJS v0.12.6 includes a Rust standalone market daemon.

Rust/Cargo was not found. Install the official rustup toolchain in your user account under ~/.cargo and ~/.rustup?"; then
        die "Rust/Cargo is required for the standalone market server."
    fi

    stage "Installing Rust through rustup"
    local tmp
    tmp="$(mktemp)"
    curl --proto '=https' --tlsv1.2 -fL https://sh.rustup.rs -o "$tmp"
    sh "$tmp" -y --profile minimal
    rm -f "$tmp"
    # shellcheck disable=SC1091
    . "$HOME/.cargo/env"
    command -v cargo >/dev/null 2>&1 || die "Rustup finished but cargo is still unavailable."
    CARGO_BIN="$(command -v cargo)"
    ok "Installed $(cargo --version)"
}

# ── Steam + Proton discovery ──────────────────────────────────────────────────
scan_steam_protons() {
    local out="$1"
    python3 - "$out" <<'PY'
from pathlib import Path
import os, re, sys

out = Path(sys.argv[1])
home = Path.home()

roots = [
    home / ".steam" / "steam",
    home / ".steam" / "debian-installation",
    home / ".local" / "share" / "Steam",
    home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
]
xdg = os.environ.get("XDG_DATA_HOME")
if xdg:
    roots.append(Path(xdg) / "Steam")

# Preserve realpath uniqueness.
steam_roots = []
seen = set()
for r in roots:
    try:
        rr = r.resolve()
    except Exception:
        rr = r
    if rr.is_dir() and str(rr) not in seen:
        steam_roots.append(rr)
        seen.add(str(rr))

libraries = []
for root in steam_roots:
    libraries.append((root, root))
    vdf = root / "steamapps" / "libraryfolders.vdf"
    if vdf.is_file():
        raw = vdf.read_text(errors="replace")
        for match in re.finditer(r'"path"\s*"([^"]+)"', raw):
            p = Path(match.group(1).replace(r"\\", "\\"))
            if p.is_dir():
                libraries.append((p, root))

records = []
seen_paths = set()
for library, steam_root in libraries:
    common = library / "steamapps" / "common"
    if not common.is_dir():
        continue
    for p in common.iterdir():
        if not p.is_dir() or not p.name.lower().startswith("proton"):
            continue
        proton = p / "proton"
        if not proton.is_file():
            continue
        rp = str(p.resolve())
        if rp in seen_paths:
            continue
        seen_paths.add(rp)

        name = p.name
        low = name.lower()
        m = re.search(r'proton\s+(\d+(?:\.\d+)*)', name, re.I)
        if m and "experimental" not in low and "hotfix" not in low:
            channel = "stable"
            version = m.group(1)
            nums = tuple(int(x) for x in version.split("."))
            # Stable numbered Proton is the installer's preferred automatic pick.
            score = 300000000 + sum(v * (1000 ** (4-i)) for i, v in enumerate(nums[:4]))
        elif "experimental" in low:
            channel = "experimental"
            version = ""
            score = 200000000
        elif "hotfix" in low:
            channel = "hotfix"
            version = ""
            score = 100000000
        else:
            channel = "other"
            version = ""
            score = 1
        records.append((score, name, rp, str(steam_root), channel, version))

records.sort(key=lambda x: (x[0], x[1]), reverse=True)
out.write_text("\n".join("\t".join(map(str, r)) for r in records) + ("\n" if records else ""))
PY
}

resolve_proton() {
    local proton_path="$1" records="$2"
    if [[ -f "$proton_path" && "$(basename "$proton_path")" == "proton" ]]; then
        PROTON_BIN="$(readlink -f "$proton_path")"
        PROTON_DIR="$(dirname "$PROTON_BIN")"
    elif [[ -d "$proton_path" && -f "$proton_path/proton" ]]; then
        PROTON_DIR="$(readlink -f "$proton_path")"
        PROTON_BIN="$PROTON_DIR/proton"
    else
        die "Selected Proton path does not contain a proton executable: $proton_path"
    fi

    STEAM_ROOT="$(awk -F'\t' -v p="$PROTON_DIR" '$3==p {print $4; exit}' "$records")"
    if [[ -z "$STEAM_ROOT" ]]; then
        # Custom path override: use the first detected Steam root as client root.
        STEAM_ROOT="$(awk -F'\t' 'NR==1 {print $4}' "$records")"
    fi
    [[ -d "$STEAM_ROOT" ]] || die "Could not determine the Steam installation root for Proton."
}

# ── EveJS archive inspection ──────────────────────────────────────────────────
resolve_archive_root() {
    local root="$1"

    # Flat archive layout.
    if [[ -f "$root/config/version.json" && -f "$root/package.json" && -d "$root/server" ]]; then
        printf '%s\n' "$root"
        return 0
    fi

    # v0.12.6 is distributed with a top-level v0.12.6/ directory. Search a
    # shallow bounded depth so future packaging wrappers do not break selection.
    local config_file candidate
    mapfile -t version_files < <(
        find "$root" -mindepth 2 -maxdepth 5 -type f -path '*/config/version.json' -print 2>/dev/null | sort
    )

    local -a candidates=()
    for config_file in "${version_files[@]}"; do
        candidate="$(dirname "$(dirname "$config_file")")"
        if [[ -f "$candidate/package.json" && -d "$candidate/server" && -d "$candidate/tools" ]]; then
            candidates+=("$candidate")
        fi
    done

    # De-duplicate.
    mapfile -t candidates < <(printf '%s\n' "${candidates[@]}" | awk 'NF && !seen[$0]++')

    if ((${#candidates[@]} == 1)); then
        printf '%s\n' "${candidates[0]}"
        return 0
    fi

    return 1
}

inspect_evejs_archive() {
    SOURCE_ZIP="$1"
    [[ -f "$SOURCE_ZIP" ]] || die "EveJS ZIP not found: $SOURCE_ZIP"
    INSPECT_TMP="$(mktemp -d)"
    unzip -q "$SOURCE_ZIP" -d "$INSPECT_TMP"
    SOURCE_ROOT="$(resolve_archive_root "$INSPECT_TMP")" || die "Could not locate an EveJS repository root inside the selected ZIP."

    EVEJS_VERSION="$(python3 - "$SOURCE_ROOT/config/version.json" <<'PY'
import json, sys
c=json.load(open(sys.argv[1]))
print(c.get("evejsVersion") or c.get("version") or "")
PY
)"
    [[ -n "$EVEJS_VERSION" ]] || die "Could not read EveJS version from config/version.json."
    [[ "$EVEJS_VERSION" == "$INSTALLER_TARGET_VERSION" ]] || die "Selected archive is EveJS v$EVEJS_VERSION; this setup build is for EveJS v$INSTALLER_TARGET_VERSION."

    BLUE_RECIPE="$SOURCE_ROOT/tools/ClientSETUP/blue_patch_recipe.json"
    [[ -f "$BLUE_RECIPE" ]] || die "This release does not contain tools/ClientSETUP/blue_patch_recipe.json."
    SUPPORTED_BUILD="$(python3 - "$BLUE_RECIPE" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))["supportedBuild"])
PY
)"
    [[ "$SUPPORTED_BUILD" =~ ^[0-9]+$ ]] || die "Invalid supportedBuild in blue patch recipe."
    [[ "$SUPPORTED_BUILD" == "$INSTALLER_TARGET_BUILD" ]] || die "EveJS v$EVEJS_VERSION patch recipe targets build $SUPPORTED_BUILD, but this setup build was audited for build $INSTALLER_TARGET_BUILD."
}

# ── EVE client resolution and preparation ─────────────────────────────────────
resolve_tq() {
    local selected="$1" c
    for c in "$selected" "$selected/tq" "$selected/EVE/tq" "$selected/SharedCache/tq"; do
        if [[ -f "$c/bin64/exefile.exe" && -f "$c/start.ini" ]]; then
            readlink -f "$c"
            return 0
        fi
    done
    return 1
}

resolve_cache_root() {
    local selected="$1" tq="$2" c
    for c in "$selected" "$(dirname "$tq")" "$(dirname "$(dirname "$tq")")"; do
        if [[ -d "$c/ResFiles" && -f "$c/index_tranquility.txt" ]]; then
            readlink -f "$c"; return 0
        fi
    done
    return 1
}

read_client_build() {
    awk -F= 'tolower($1) ~ /^[[:space:]]*build[[:space:]]*$/ {gsub(/[[:space:]]/, "", $2); print $2; exit}' "$1/start.ini"
}

patch_start_ini() {
    local tq="$1"
    local ini="$tq/start.ini"
    [[ -f "$ini" ]] || die "start.ini not found: $ini"
    [[ -f "$ini.original" ]] || cp -a "$ini" "$ini.original"

    python3 - "$ini" <<'PY'
from pathlib import Path
import re, sys
p=Path(sys.argv[1])
lines=p.read_text(encoding="utf-8",errors="replace").splitlines()
out=[]
seen_crypto=False
seen_server=False
for line in lines:
    if re.match(r"^\s*cryptopack\s*=", line, re.I):
        if not seen_crypto:
            out.append("cryptoPack = Placebo"); seen_crypto=True
        continue
    if re.match(r"^\s*(server|serverip)\s*=", line, re.I):
        if not seen_server:
            out.append("server = 127.0.0.1"); seen_server=True
        continue
    out.append(line)
if not seen_crypto: out.append("cryptoPack = Placebo")
if not seen_server: out.append("server = 127.0.0.1")
p.write_text("\n".join(out)+"\n",encoding="utf-8")
PY

    grep -Eiq '^[[:space:]]*cryptopack[[:space:]]*=[[:space:]]*Placebo[[:space:]]*$' "$ini" \
        || die "start.ini cryptoPack verification failed."
    grep -Eiq '^[[:space:]]*(server|serverip)[[:space:]]*=[[:space:]]*127\.0\.0\.1[[:space:]]*$' "$ini" \
        || die "start.ini local-server verification failed."
}

patch_blue_dll() {
    local tq="$1" recipe="$2"
    python3 - "$tq/bin64/blue.dll" "$recipe" <<'PY'
from pathlib import Path
import hashlib, json, shutil, struct, sys

dll=Path(sys.argv[1])
recipe=json.load(open(sys.argv[2], encoding="utf-8"))
data=bytearray(dll.read_bytes())

def sha(b): return hashlib.sha256(b).hexdigest()

def pe_info(b):
    if len(b) < 0x100 or b[:2] != b"MZ":
        raise SystemExit("ERROR: blue.dll is not a valid MZ/PE image.")
    peoff=struct.unpack_from("<I", b, 0x3c)[0]
    if b[peoff:peoff+4] != b"PE\0\0":
        raise SystemExit("ERROR: blue.dll PE signature is invalid.")
    opt=peoff+4+20
    magic=struct.unpack_from("<H", b, opt)[0]
    if magic == 0x20b:
        dd=opt+112
    elif magic == 0x10b:
        dd=opt+96
    else:
        raise SystemExit("ERROR: unsupported blue.dll PE optional header.")
    checksum=opt+64
    sec=dd+(4*8)
    off,size=struct.unpack_from("<II", b, sec)
    return checksum, sec, off, size

def checksum_pe(b, checksum_off):
    total=0
    n=len(b)
    i=0
    while i<n:
        if checksum_off <= i < checksum_off+4:
            i += 2
            continue
        word=b[i]
        if i+1<n:
            word |= b[i+1]<<8
        total += word
        total=(total & 0xffffffff)+(total>>32)
        i += 2
    total=(total & 0xffff)+(total>>16)
    total=total+(total>>16)
    total=(total & 0xffff)+n
    return total & 0xffffffff

source=recipe["source"]
known=recipe.get("knownPatchedVariants",[])
h=sha(data)

for v in known:
    if len(data)==int(v["size"]) and h.lower()==v["sha256"].lower():
        print(f"blue.dll already patched: {v.get('name','known variant')}")
        raise SystemExit(0)

patches=recipe.get("patches",[])
after_ok=True
for p in patches:
    off=int(p["offset"]); after=bytes.fromhex(p["afterHex"])
    if bytes(data[off:off+len(after)]) != after:
        after_ok=False; break

try:
    _,_,sec_off,sec_size=pe_info(data)
    stripped=(sec_off==0 and sec_size==0)
except Exception:
    stripped=False

if after_ok and stripped:
    print("blue.dll already patched with stripped PE security directory.")
    raise SystemExit(0)

if len(data)!=int(source["size"]) or h.lower()!=source["sha256"].lower():
    raise SystemExit(
        "ERROR: blue.dll does not match the exact supported original or a known patched variant.\n"
        f"size={len(data)} sha256={h}\n"
        "The installer will not apply a relaxed binary patch automatically."
    )

backup=Path(str(dll)+".original")
if not backup.exists():
    shutil.copy2(dll, backup)

for p in patches:
    off=int(p["offset"])
    before=bytes.fromhex(p["beforeHex"])
    after=bytes.fromhex(p["afterHex"])
    current=bytes(data[off:off+len(before)])
    if current==after:
        continue
    if current!=before:
        raise SystemExit(f"ERROR: patch precondition failed at {p.get('offsetHex',hex(off))}")
    data[off:off+len(after)]=after

checksum_off, sec_dir, sec_file, sec_size=pe_info(data)
if sec_file>0 and sec_size>0:
    if sec_file>len(data):
        raise SystemExit("ERROR: PE security directory points outside blue.dll.")
    data=data[:sec_file]

checksum_off, sec_dir, _, _=pe_info(data)
struct.pack_into("<II", data, sec_dir, 0, 0)
struct.pack_into("<I", data, checksum_off, 0)
struct.pack_into("<I", data, checksum_off, checksum_pe(data, checksum_off))

for p in patches:
    off=int(p["offset"]); after=bytes.fromhex(p["afterHex"])
    if bytes(data[off:off+len(after)]) != after:
        raise SystemExit("ERROR: patched blue.dll failed branch-byte verification.")
_,_,so,ss=pe_info(data)
if so!=0 or ss!=0:
    raise SystemExit("ERROR: patched blue.dll still contains a PE security directory.")

dll.write_bytes(data)
print(f"blue.dll patch complete: {dll}")
print(f"backup: {backup}")
print(f"patched sha256: {sha(data)}")
PY
}

verify_resource_cache() {
    python3 - "$1/ResFiles" "$1/index_tranquility.txt" <<'PY'
from pathlib import Path
import sys
res=Path(sys.argv[1]); idx=Path(sys.argv[2])
if not res.is_dir() or not idx.is_file():
    raise SystemExit("ERROR: ResFiles/index_tranquility.txt missing.")
hex_dirs=sum(1 for p in res.iterdir() if p.is_dir() and len(p.name)==2 and all(c in "0123456789abcdefABCDEF" for c in p.name))
if hex_dirs < 240:
    raise SystemExit(f"ERROR: resource cache appears incomplete ({hex_dirs} hexadecimal directories).")
count=0
for p in res.rglob("*"):
    if p.is_file():
        count+=1
        if count>=50000: break
if count<50000:
    raise SystemExit(f"ERROR: resource cache appears incomplete ({count} files sampled).")
print(f"Resource cache sanity check passed: {hex_dirs} hexadecimal directories; >=50,000 files sampled.")
PY
}

repair_client_ca_bundles() {
    local tq="$1" ca="$2"
    python3 - "$tq" "$ca" <<'PY'
from pathlib import Path
import datetime, re, subprocess, sys, tempfile

tq=Path(sys.argv[1])
ca_path=Path(sys.argv[2])
ca=ca_path.read_bytes().strip()
PEM=re.compile(rb"-----BEGIN CERTIFICATE-----\s*.*?-----END CERTIFICATE-----", re.S)

def meta(block):
    with tempfile.NamedTemporaryFile(suffix=".pem") as f:
        f.write(block+b"\n"); f.flush()
        p=subprocess.run(["openssl","x509","-in",f.name,"-noout","-subject","-issuer","-fingerprint","-sha256"],
                         capture_output=True,text=True)
        if p.returncode: return None
        d={"subject":"","issuer":"","fp":""}
        for line in p.stdout.splitlines():
            low=line.lower()
            if low.startswith("subject="): d["subject"]=line.split("=",1)[1].strip()
            elif low.startswith("issuer="): d["issuer"]=line.split("=",1)[1].strip()
            elif "fingerprint=" in low: d["fp"]=line.split("=",1)[1].replace(":","").strip().upper()
        return d

cam=meta(ca)
if not cam: raise SystemExit("ERROR: could not parse current EveJS CA.")
cafp=cam["fp"]

fixed=[
    tq/"bin64"/"cacert.pem",
    tq/"bin64"/"packages"/"certifi"/"cacert.pem",
    tq/"bin"/"cacert.pem",
    tq/"bin"/"packages"/"certifi"/"cacert.pem",
]
bundles={p.resolve() for p in fixed if p.is_file()}
bundles.update(p.resolve() for p in tq.rglob("cacert.pem") if p.is_file())
bundles=sorted(bundles)
if not bundles:
    raise SystemExit(f"ERROR: no client cacert.pem bundles found under {tq}")

stamp=datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
for bundle in bundles:
    original=bundle.read_bytes()
    pieces=[]; cursor=0; have=False
    for m in PEM.finditer(original):
        pieces.append(original[cursor:m.start()])
        block=m.group(0).strip()
        cm=meta(block)
        keep=True
        if cm:
            subject=cm["subject"].lower(); issuer=cm["issuer"].lower(); fp=cm["fp"]
            local=("evejs local" in subject or "evejs local" in issuer
                   or "eve.js public gateway tls" in subject
                   or "eve.js public gateway tls" in issuer)
            if fp==cafp:
                if have: keep=False
                else: have=True
            elif local:
                keep=False
        if keep: pieces.append(block)
        cursor=m.end()
    pieces.append(original[cursor:])
    updated=b"".join(pieces).rstrip()+b"\n"
    if not have:
        updated += b"\n"+ca+b"\n"
    if updated != original:
        backup=bundle.with_name(bundle.name+f".pre-evejs-linux-{stamp}")
        backup.write_bytes(original)
        bundle.write_bytes(updated)

    current=0; stale=0
    for m in PEM.finditer(bundle.read_bytes()):
        cm=meta(m.group(0).strip())
        if not cm: continue
        if cm["fp"]==cafp:
            current+=1; continue
        subject=cm["subject"].lower(); issuer=cm["issuer"].lower()
        if ("evejs local" in subject or "evejs local" in issuer
            or "eve.js public gateway tls" in subject
            or "eve.js public gateway tls" in issuer):
            stale+=1
    if current != 1 or stale:
        raise SystemExit(f"ERROR: certificate bundle reconciliation failed: {bundle} current={current} stale={stale}")

print(f"Client certificate bundles reconciled: {len(bundles)} bundle(s).")
PY
}

verify_leaf_against_ca() {
    local label="$1" leaf="$2" ca="$3" tmp
    tmp="$(mktemp)"
    python3 - "$leaf" "$tmp" <<'PY'
from pathlib import Path
import re,sys
raw=Path(sys.argv[1]).read_text(errors="replace")
m=re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",raw,re.S)
if not m: raise SystemExit("no certificate found")
Path(sys.argv[2]).write_text(m.group(0)+"\n")
PY
    openssl verify -CAfile "$ca" "$tmp" >/dev/null || {
        rm -f "$tmp"; die "$label certificate does not validate against the current EveJS CA."
    }
    rm -f "$tmp"
}


# ── Interactive selection helpers ─────────────────────────────────────────────
# In interactive mode, invalid selections never terminate the installer.
# The user is told what was wrong and the relevant chooser is reopened.
# Explicit CLI arguments remain strict/non-interactive and fail immediately.

choose_valid_evejs_zip() {
    local selected=""
    while true; do
        selected="$(ui_file "Select the EveJS v0.12.6 release ZIP" "$SCRIPT_DIR")" || return 1
        [[ -n "$selected" ]] || continue

        if [[ ! -f "$selected" ]]; then
            ui_warn "Invalid EveJS file" \
"The selected path is not a file:

$selected

Please select the EveJS v0.12.6 ZIP."
            continue
        fi

        # inspect_evejs_archive exits via die() on invalid archives, so probe
        # the structure/version in a subshell-like temporary inspection here
        # and only commit to the normal inspection once it passes.
        local probe_tmp probe_root probe_version
        probe_tmp="$(mktemp -d)"
        if ! unzip -q "$selected" -d "$probe_tmp" 2>/dev/null; then
            rm -rf "$probe_tmp"
            ui_warn "Invalid EveJS ZIP" \
"The selected file could not be opened as a ZIP archive:

$selected

Please select the EveJS v0.12.6 release ZIP."
            continue
        fi

        probe_root="$(resolve_archive_root "$probe_tmp" 2>/dev/null || true)"
        if [[ -z "$probe_root" || ! -f "$probe_root/config/version.json" ]]; then
            rm -rf "$probe_tmp"
            ui_warn "Wrong ZIP selected" \
"The selected ZIP does not contain a recognizable EveJS repository.

Please select the EveJS v0.12.6 release ZIP."
            continue
        fi

        probe_version="$(python3 - "$probe_root/config/version.json" <<'PY'
import json, sys
try:
    c=json.load(open(sys.argv[1]))
    print(c.get("evejsVersion") or c.get("version") or "")
except Exception:
    print("")
PY
)"
        rm -rf "$probe_tmp"

        if [[ "$probe_version" != "$INSTALLER_TARGET_VERSION" ]]; then
            ui_warn "Wrong EveJS version" \
"The selected ZIP reports EveJS version:

${probe_version:-unknown}

This setup build is for EveJS v$INSTALLER_TARGET_VERSION.

Please select the correct release ZIP."
            continue
        fi

        printf '%s\n' "$(readlink -f "$selected")"
        return 0
    done
}

choose_valid_client() {
    local selected tq cache build
    while true; do
        selected="$(ui_dir "Select the copied EVE client or its tq directory" "$HOME")" || return 1
        [[ -n "$selected" ]] || continue
        selected="$(readlink -f "$selected" 2>/dev/null || true)"

        if [[ -z "$selected" || ! -d "$selected" ]]; then
            ui_warn "Invalid EVE client location" \
"The selected path is not an existing directory.

Please select either:
• the copied EVE client root, or
• its tq directory."
            continue
        fi

        tq="$(resolve_tq "$selected" 2>/dev/null || true)"
        if [[ -z "$tq" ]]; then
            ui_warn "EVE client not found" \
"The selected directory does not contain a usable EVE TQ client.

The installer could not find:
  bin64/exefile.exe
  start.ini

Please select the copied EVE client root or its tq directory."
            continue
        fi

        cache="$(resolve_cache_root "$selected" "$tq" 2>/dev/null || true)"
        if [[ -z "$cache" ]]; then
            ui_warn "EVE resource cache not found" \
"The selected EVE client was found, but its required shared resource cache was not.

The installer needs:
  ResFiles/
  index_tranquility.txt

Please select the copied EVE client/shared-cache root or its tq directory."
            continue
        fi

        build="$(read_client_build "$tq")"
        if [[ "$build" != "$SUPPORTED_BUILD" ]]; then
            ui_warn "Wrong EVE client build" \
"The selected EVE client is build:

${build:-unknown}

This EveJS release requires build:

$SUPPORTED_BUILD

Please select the correct copied EVE client."
            continue
        fi

        if ! verify_resource_cache "$cache" >/dev/null 2>&1; then
            ui_warn "Incomplete EVE resource cache" \
"The selected client appears to have an incomplete ResFiles cache.

Please select a complete copied EVE shared-cache/client."
            continue
        fi

        CLIENT_SELECTED="$selected"
        TQ_PATH="$tq"
        CACHE_ROOT="$cache"
        return 0
    done
}

choose_existing_empty_destination() {
    local selected
    while true; do
        selected="$(ui_dir "Select an existing EMPTY folder for EveJS v0.12.6" "$HOME")" || return 1
        [[ -n "$selected" ]] || continue
        selected="$(readlink -f "$selected" 2>/dev/null || true)"

        if [[ -z "$selected" || ! -d "$selected" ]]; then
            ui_warn "Destination must already exist" \
"The installer will not create the EveJS destination folder.

Please create the folder yourself in the file chooser or file manager, then select that existing folder."
            continue
        fi

        if [[ -n "$(find "$selected" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
            if [[ -d "$selected/_local/runtime-performance" ]] && \
               [[ -z "$(find "$selected" -mindepth 1 -maxdepth 1 ! -name '_local' -print -quit 2>/dev/null)" ]] && \
               [[ -z "$(find "$selected/_local" -mindepth 1 -maxdepth 1 ! -name 'runtime-performance' -print -quit 2>/dev/null)" ]]; then
                ui_warn "Destination contains stale EveJS runtime telemetry" \
"The selected folder contains only:

_local/runtime-performance/

This is not something the installer creates before installation. It strongly suggests an older EveJS process is/was still writing telemetry to this path and recreated the folder after it had been removed.

Run evejs-v0.12.6-stale-processes.sh, remove the leftover folder after the process is stopped, then select the empty folder again.

The installer will not delete this data automatically."
            else
                ui_warn "Destination is not empty" \
"The selected EveJS installation folder already contains files or directories:

$selected

Please select an existing EMPTY folder.

The installer will not create, rename, delete, or replace the destination folder."
            fi
            continue
        fi

        printf '%s\n' "$selected"
        return 0
    done
}



preflight_existing_evejs_processes() {
    local ports_re=':(443|5222|26000|26001|26002|26003|26400|40110|40111)([[:space:]]|$)'
    local listeners
    listeners="$(ss -H -ltnp 2>/dev/null | grep -E "$ports_re" || true)"
    [[ -n "$listeners" ]] || return 0

    if ui_yesno "Existing EveJS-related listeners detected" \
"One or more ports used by EveJS are already listening before installation begins:

$listeners

A previous EveJS session may still be running. A stale session can recreate a deleted old EveJS folder by continuing to write runtime telemetry, and it can block services in the new installation.

Run the bundled safe stale-process cleanup tool now?

The cleanup tool will refuse to kill a port owner it cannot identify as EveJS."; then
        if ! "$STALE_CLEANUP_SOURCE"; then
            ui_warn "Some listeners remain" \
"The cleanup tool could not safely clear every listener.

You may continue installing, but the new EveJS launcher can fail later and an old EveJS process may recreate its previous runtime directory.

Review the terminal output before proceeding."
        fi
    else
        ui_warn "Existing services left running" \
"The installer will continue without stopping them.

Be aware that a stale EveJS process can recreate its old folder and can occupy ImageEdge, chat, gateway, game, Redshift, or market ports."
    fi
}

# ── Main wizard ───────────────────────────────────────────────────────────────
trap '[[ -n "${INSPECT_TMP:-}" ]] && rm -rf "$INSPECT_TMP"; [[ -n "${HOST_TOOLCHAIN_REPORT:-}" && -f "${HOST_TOOLCHAIN_REPORT:-}" ]] && rm -f "$HOST_TOOLCHAIN_REPORT"' EXIT

SETUP_ACTION="${ARG_MODE,,}"
if [[ -z "$SETUP_ACTION" ]]; then
    SETUP_ACTION="$(ui_choose_setup_action)" || exit 1
fi

case "$SETUP_ACTION" in
    exit) exit 0 ;;
    update)
        [[ -f "$MAINTENANCE_SOURCE" ]] || die "Updater engine is missing beside the Installer: $MAINTENANCE_SOURCE"
        check_system_tools
        ensure_native_build_environment
        ensure_node
        ensure_rust
        export EVEJS_SETUP_NODE_BIN="$NODE_BIN"
        export EVEJS_SETUP_CARGO_BIN="$CARGO_BIN"
        maintenance_args=(--mode update)
        (( FORCE_TEXT )) && maintenance_args+=(--text)
        [[ -n "$ARG_ZIP" ]] && maintenance_args+=(--zip "$ARG_ZIP")
        [[ -n "$ARG_EXISTING" ]] && maintenance_args+=(--existing "$ARG_EXISTING")
        exec python3 "$MAINTENANCE_SOURCE" "${maintenance_args[@]}"
        ;;
    restore)
        [[ -f "$MAINTENANCE_SOURCE" ]] || die "Updater engine is missing beside the Installer: $MAINTENANCE_SOURCE"
        command -v python3 >/dev/null 2>&1 || die "Python 3 is required for backup restore."
        maintenance_args=(--mode restore)
        (( FORCE_TEXT )) && maintenance_args+=(--text)
        [[ -n "$ARG_EXISTING" ]] && maintenance_args+=(--existing "$ARG_EXISTING")
        [[ -n "$ARG_BACKUP" ]] && maintenance_args+=(--backup "$ARG_BACKUP")
        exec python3 "$MAINTENANCE_SOURCE" "${maintenance_args[@]}"
        ;;
    backups)
        [[ -f "$MAINTENANCE_SOURCE" ]] || die "Updater engine is missing beside the Installer: $MAINTENANCE_SOURCE"
        command -v python3 >/dev/null 2>&1 || die "Python 3 is required for backup management."
        maintenance_args=(--mode backups)
        (( FORCE_TEXT )) && maintenance_args+=(--text)
        [[ -n "$ARG_EXISTING" ]] && maintenance_args+=(--existing "$ARG_EXISTING")
        exec python3 "$MAINTENANCE_SOURCE" "${maintenance_args[@]}"
        ;;
    install) ;;
    *)
        die "Unknown setup mode '$SETUP_ACTION'. Choose install, update, restore, or backups."
        ;;
esac

# Fresh installation continues through the established v1.7 path.
check_system_tools
ensure_native_build_environment
ensure_node
ensure_rust

[[ -f "$LAUNCHER_SOURCE" ]] || die "Launcher template is missing beside the installer: $LAUNCHER_SOURCE"
[[ -f "$STALE_CLEANUP_SOURCE" ]] || die "Stale-process cleanup tool is missing beside the installer: $STALE_CLEANUP_SOURCE"
[[ -f "$MAINTENANCE_SOURCE" ]] || die "Updater/backup engine is missing beside the installer: $MAINTENANCE_SOURCE"

preflight_existing_evejs_processes

ui_info "EveJS Universal Linux Setup" \
"This mode installs a clean EveJS v0.12.6 baseline and prepares a copied EVE client for local play.

It is designed for mainstream x86_64 Linux distributions with Steam installed, including SteamOS installations with minimized development files.

The installer:
• keeps EveJS core source untouched after extraction;
• creates a fresh database and certificates;
• prepares the standalone market;
• supports removable mods through mods/*/loader.js;
• configures a Steam Proton launch environment;
• copies the standalone stale-process cleanup tool into the completed EveJS folder;
• compiles tiny C/C++ tests before installation and, on SteamOS, can repair only the exact already-installed package proven missing.

For the EveJS destination, create/select an existing EMPTY folder. The installer
will not create the destination folder for you.

Do not select the EVE installation you use for normal Tranquility play."

# EveJS ZIP
if [[ -n "$ARG_ZIP" ]]; then
    SOURCE_ZIP="$(readlink -f "$ARG_ZIP" 2>/dev/null || true)"
    [[ -f "$SOURCE_ZIP" ]] || die "EveJS ZIP not found: $ARG_ZIP"
    inspect_evejs_archive "$SOURCE_ZIP"
else
    mapfile -t nearby_zips < <(find "$SCRIPT_DIR" -maxdepth 1 -type f -iname 'EveJS*.zip' -print | sort)
    if ((${#nearby_zips[@]} == 1)) && ui_yesno "EveJS release detected" "Use this EveJS release ZIP?

${nearby_zips[0]}"; then
        # Even an auto-detected ZIP gets validated. If it is wrong, fall back
        # into the chooser instead of terminating the installer.
        candidate="$(readlink -f "${nearby_zips[0]}")"
        probe_tmp="$(mktemp -d)"
        probe_ok=0
        if unzip -q "$candidate" -d "$probe_tmp" 2>/dev/null; then
            probe_root="$(resolve_archive_root "$probe_tmp" 2>/dev/null || true)"
            if [[ -n "$probe_root" && -f "$probe_root/config/version.json" ]]; then
                probe_version="$(python3 - "$probe_root/config/version.json" <<'PY'
import json,sys
try:
    c=json.load(open(sys.argv[1]))
    print(c.get("evejsVersion") or c.get("version") or "")
except Exception:
    print("")
PY
)"
                [[ "$probe_version" == "$INSTALLER_TARGET_VERSION" ]] && probe_ok=1
            fi
        fi
        rm -rf "$probe_tmp"

        if (( probe_ok )); then
            SOURCE_ZIP="$candidate"
        else
            ui_warn "Detected ZIP is not EveJS v0.12.6" \
"The ZIP beside the installer is not the required EveJS v0.12.6 release.

Please select the correct file."
            SOURCE_ZIP="$(choose_valid_evejs_zip)" || exit 1
        fi
    else
        SOURCE_ZIP="$(choose_valid_evejs_zip)" || exit 1
    fi
    inspect_evejs_archive "$SOURCE_ZIP"
fi
ok "Selected EveJS v$EVEJS_VERSION (client build $SUPPORTED_BUILD)"

# Steam Proton
PROTON_RECORDS="$(mktemp)"
scan_steam_protons "$PROTON_RECORDS"
[[ -s "$PROTON_RECORDS" ]] || die "No official Steam Proton installation was found.

Open Steam → Settings → Compatibility, install a numbered Proton release, then rerun this installer."

if [[ -n "$ARG_PROTON" ]]; then
    SELECTED_PROTON="$ARG_PROTON"
else
    AUTO_NAME="$(awk -F'\t' 'NR==1 {print $2}' "$PROTON_RECORDS")"
    AUTO_PATH="$(awk -F'\t' 'NR==1 {print $3}' "$PROTON_RECORDS")"
    AUTO_CHANNEL="$(awk -F'\t' 'NR==1 {print $5}' "$PROTON_RECORDS")"
    if ui_yesno "Steam Proton detected" "Use the automatically selected Proton?

$AUTO_NAME
Channel: $AUTO_CHANNEL
$AUTO_PATH

The installer prefers the newest numbered official Proton. Proton Experimental remains selectable if installed."; then
        SELECTED_PROTON="$AUTO_PATH"
    else
        SELECTED_PROTON="$(ui_choose_proton "$PROTON_RECORDS")" || die "No Proton version was selected."
    fi
fi
resolve_proton "$SELECTED_PROTON" "$PROTON_RECORDS"
ok "Selected Proton: $(basename "$PROTON_DIR")"

# v0.12.6 market profile
if [[ -n "$ARG_MARKET" ]]; then
    MARKET_PROFILE="${ARG_MARKET,,}"
else
    MARKET_PROFILE="$(ui_choose_market)" || die "No market profile was selected."
fi

case "$MARKET_PROFILE" in
    v1)
        MARKET_LABEL="v1 — Synthetic Jita + New Caldari"
        ;;
    v2)
        MARKET_LABEL="v2 — Live TQ Jita market + NPC catalog"
        ;;
    v3)
        MARKET_LABEL="v3 — Cost-priced Jita 4-4 + authentic NPC catalog"
        ;;
    *)
        die "Unknown market profile '$MARKET_PROFILE'. Choose v1, v2, or v3."
        ;;
esac
ok "Market profile: $MARKET_LABEL"

# Client
if [[ -n "$ARG_CLIENT" ]]; then
    CLIENT_SELECTED="$(readlink -f "$ARG_CLIENT" 2>/dev/null || true)"
    [[ -d "$CLIENT_SELECTED" ]] || die "EVE client directory not found: $ARG_CLIENT"
    TQ_PATH="$(resolve_tq "$CLIENT_SELECTED")" || die "Could not find bin64/exefile.exe and start.ini under the selected EVE path."
    CACHE_ROOT="$(resolve_cache_root "$CLIENT_SELECTED" "$TQ_PATH")" || die "Could not find ResFiles and index_tranquility.txt beside the selected client."
    CLIENT_BUILD="$(read_client_build "$TQ_PATH")"
    [[ "$CLIENT_BUILD" == "$SUPPORTED_BUILD" ]] || die "The selected EVE client is build '$CLIENT_BUILD', but this EveJS release requires build '$SUPPORTED_BUILD'."
    verify_resource_cache "$CACHE_ROOT"
else
    choose_valid_client || exit 1
fi
ok "EVE client: $TQ_PATH"
ok "Resource cache: $CACHE_ROOT/ResFiles"

# Destination
# v1.2 deliberately NEVER creates the destination root. The researcher creates
# and selects an existing empty folder. This avoids leaving behind an automatic
# EveJS-v0.12.6-Clean directory that then interferes with their own naming.
if [[ -n "$ARG_DEST" ]]; then
    EVEJS_REPO="$(readlink -f "$ARG_DEST" 2>/dev/null || true)"
    [[ -d "$EVEJS_REPO" ]] || die "Destination must already exist as a directory: $ARG_DEST"
    [[ -z "$(find "$EVEJS_REPO" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]] || die "Destination must be empty: $EVEJS_REPO"
else
    EVEJS_REPO="$(choose_existing_empty_destination)" || exit 1
fi

SUMMARY="EveJS release:
  v$EVEJS_VERSION
  $SOURCE_ZIP

Required EVE build:
  $SUPPORTED_BUILD

EVE tq client:
  $TQ_PATH

EVE resource cache:
  $CACHE_ROOT/ResFiles

EveJS destination:
  $EVEJS_REPO

Steam Proton:
  $(basename "$PROTON_DIR")
  $PROTON_DIR

Market:
  $MARKET_LABEL

Steam root:
  $STEAM_ROOT

The selected EVE client will be backed up before start.ini/blue.dll/certificate-bundle changes."

ui_yesno "Confirm EveJS Linux installation" "$SUMMARY

Proceed with installation?" || exit 0

# ── Install EveJS cleanly ─────────────────────────────────────────────────────
stage "Extracting clean EveJS v$EVEJS_VERSION"
[[ -d "$EVEJS_REPO" ]] || die "Selected EveJS destination no longer exists: $EVEJS_REPO"
[[ -z "$(find "$EVEJS_REPO" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]] || die "Selected EveJS destination is no longer empty: $EVEJS_REPO"
cp -a "$SOURCE_ROOT"/. "$EVEJS_REPO"/
mkdir -p "$EVEJS_REPO/mods"

if [[ -n "${HOST_TOOLCHAIN_REPORT:-}" && -f "$HOST_TOOLCHAIN_REPORT" ]]; then
    cp -a "$HOST_TOOLCHAIN_REPORT" "$EVEJS_REPO/LINUX-HOST-TOOLCHAIN.txt"
fi
if [[ -n "${STEAMOS_REPAIR_LOG:-}" ]]; then
    {
        echo "SteamOS packages minimally repaired by EveJS installer"
        echo "====================================================="
        printf '%s' "$STEAMOS_REPAIR_LOG"
    } > "$EVEJS_REPO/STEAMOS-MINIMAL-REPAIR.txt"
fi
ok "Clean repository extracted."

export PATH="$(dirname "$NODE_BIN"):$HOME/.cargo/bin:$PATH"

stage "Installing EveJS root dependencies"
(
    cd "$EVEJS_REPO"
    npm ci --no-audit --no-fund
)
ok "Root dependencies installed."

stage "Generating fresh EveJS local certificates"
CERT_BUILDER="$EVEJS_REPO/tools/ClientSETUP/scripts/build-gateway-cert.js"
CA_CERT="$EVEJS_REPO/server/certs/xmpp-ca-cert.pem"
CA_KEY="$EVEJS_REPO/server/certs/xmpp-ca-key.pem"
XMPP_CERT="$EVEJS_REPO/server/certs/xmpp-dev-cert.pem"
XMPP_KEY="$EVEJS_REPO/server/certs/xmpp-dev-key.pem"
GW_CERT="$EVEJS_REPO/server/src/_secondary/express/certs/gateway-dev-cert.pem"
GW_KEY="$EVEJS_REPO/server/src/_secondary/express/certs/gateway-dev-key.pem"

node "$CERT_BUILDER" \
    --ensure-ca \
    --ca-cert "$CA_CERT" \
    --ca-key "$CA_KEY" \
    --common-name "localhost" \
    --dns "localhost" \
    --ip "127.0.0.1" \
    --out-cert "$XMPP_CERT" \
    --out-key "$XMPP_KEY"

node "$CERT_BUILDER" \
    --ca-cert "$CA_CERT" \
    --ca-key "$CA_KEY" \
    --common-name "dev-public-gateway.evetech.net" \
    --dns "app.launchdarkly.com,clientstream.launchdarkly.com,clientsdk.launchdarkly.com,dev-public-gateway.evetech.net,events.launchdarkly.com,public-gateway.evetech.net,stream.launchdarkly.com,localhost" \
    --ip "127.0.0.1" \
    --out-cert "$GW_CERT" \
    --out-key "$GW_KEY"

verify_leaf_against_ca "XMPP" "$XMPP_CERT" "$CA_CERT"
verify_leaf_against_ca "Gateway" "$GW_CERT" "$CA_CERT"
ok "Fresh local CA, XMPP and gateway certificates created."

stage "Installing EveJS server dependencies"
(
    cd "$EVEJS_REPO/server"
    npm ci --no-audit --no-fund
)
[[ -f "$EVEJS_REPO/server/node_modules/express/package.json" ]] || die "Server dependency installation did not produce express."
ok "Server dependencies installed."

stage "Building clean EveJS static/runtime database"
SDE_URL="https://developers.eveonline.com/static-data/tranquility/eve-online-static-data-${SUPPORTED_BUILD}-jsonl.zip"
SDE_ZIP="$EVEJS_REPO/_local/downloads/sde/eve-online-static-data-${SUPPORTED_BUILD}-jsonl.zip"
SDE_DIR="$EVEJS_REPO/_local/sde/eve-online-static-data-${SUPPORTED_BUILD}-jsonl"
mkdir -p "$(dirname "$SDE_ZIP")" "$SDE_DIR"

if [[ ! -s "$SDE_ZIP" ]]; then
    curl -fL --retry 3 --retry-delay 2 "$SDE_URL" -o "$SDE_ZIP"
fi
if [[ ! -f "$SDE_DIR/_sde.jsonl" ]]; then
    rm -rf "$SDE_DIR"
    mkdir -p "$SDE_DIR"
    unzip -q "$SDE_ZIP" -d "$SDE_DIR"
fi
node --max-old-space-size=8192 \
    "$EVEJS_REPO/tools/DatabaseCreator/database-creator.js" \
    --sde-dir "$SDE_DIR" \
    --out "$EVEJS_REPO/_local/gameStore/data" \
    --build "$SUPPORTED_BUILD" \
    --sde-url "$SDE_URL" \
    --force
[[ -f "$EVEJS_REPO/_local/gameStore/manifest.json" ]] || die "Game database manifest was not created."
ok "Clean game database created."

stage "Building standalone market: $MARKET_LABEL"
MARKET_DIR="$EVEJS_REPO/externalservices/market-server"
MARKET_DB="$MARKET_DIR/data/generated/market.sqlite"
mkdir -p "$(dirname "$MARKET_DB")"

case "$MARKET_PROFILE" in
    v1)
        MARKET_SEED_DIR="$EVEJS_REPO/tools/market-seed"
        (
            cd "$MARKET_SEED_DIR"
            cargo run --release -- \
                --config config/market-seed.local.toml \
                build --force --preset jita_new_caldari
        )
        ;;
    v2)
        MARKET_SEED_DIR="$EVEJS_REPO/tools/market-seederv2"
        [[ -f "$MARKET_SEED_DIR/Cargo.toml" ]] || die "v0.12.6 market-seederv2 is missing."
        (
            cd "$MARKET_SEED_DIR"
            cargo run --release -- \
                --config config/market-seederv2.local.toml \
                build --yes --reuse-download
        )
        ;;
    v3)
        MARKET_SEED_DIR="$EVEJS_REPO/tools/market-seederv3"
        [[ -f "$MARKET_SEED_DIR/Cargo.toml" ]] || die "v0.12.6 market-seederv3 is missing."
        (
            cd "$MARKET_SEED_DIR"

            # Validate config/static data first.
            cargo run --release -- \
                --config config/market-seederv3.local.toml \
                doctor

            # v3 build never downloads. Explicitly cache/reuse the TQ snapshot
            # needed for the authentic NPC catalog overlay.
            cargo run --release -- \
                --config config/market-seederv3.local.toml \
                snapshot-info --download --reuse-download

            # The v0.12.6 seeder contract keeps the pump audit as the meaningful build
            # gate and should be run before every direct seeder build.
            cargo run --release -- \
                --config config/market-seederv3.local.toml \
                audit

            cargo run --release -- \
                --config config/market-seederv3.local.toml \
                build --yes
        )
        ;;
esac

[[ -s "$MARKET_DB" ]] || die "Selected market seeder did not produce market.sqlite."

(
    cd "$MARKET_DIR"
    cargo build --release
)
MARKET_BIN="$MARKET_DIR/target/release/market-server"
[[ -x "$MARKET_BIN" ]] || die "Market server release binary was not created."
(
    cd "$MARKET_DIR"
    "$MARKET_BIN" --config config/market-server.local.toml doctor
)
ok "Standalone market '$MARKET_PROFILE' seeded, built and validated."

stage "Preparing the copied EVE client"
patch_blue_dll "$TQ_PATH" "$EVEJS_REPO/tools/ClientSETUP/blue_patch_recipe.json"
patch_start_ini "$TQ_PATH"
repair_client_ca_bundles "$TQ_PATH" "$CA_CERT"
ok "EVE client is configured for local EveJS and trusts the current clean CA."

# ── Write portable launcher config ─────────────────────────────────────────────
stage "Installing portable Linux launcher"
PREFIX_HASH="$(printf '%s' "$EVEJS_REPO" | sha256sum | awk '{print substr($1,1,12)}')"
PROTON_PREFIX="$HOME/.local/share/evejs-linux/prefixes/${EVEJS_VERSION}-${PREFIX_HASH}"
mkdir -p "$PROTON_PREFIX"

cp -a "$LAUNCHER_SOURCE" "$EVEJS_REPO/evejs-linux-launcher.sh"
cp -a "$STALE_CLEANUP_SOURCE" "$EVEJS_REPO/evejs-stale-process-cleanup.sh"
chmod +x "$EVEJS_REPO/evejs-linux-launcher.sh" "$EVEJS_REPO/evejs-stale-process-cleanup.sh"

CONFIG="$EVEJS_REPO/.evejs-linux.conf"
{
    printf 'EVEJS_REPO=%q\n' "$EVEJS_REPO"
    printf 'EVEJS_VERSION=%q\n' "$EVEJS_VERSION"
    printf 'SUPPORTED_BUILD=%q\n' "$SUPPORTED_BUILD"
    printf 'EVE_CLIENT_SELECTED=%q\n' "$CLIENT_SELECTED"
    printf 'TQ_PATH=%q\n' "$TQ_PATH"
    printf 'CACHE_ROOT=%q\n' "$CACHE_ROOT"
    printf 'RESFILES=%q\n' "$CACHE_ROOT/ResFiles"
    printf 'STEAM_ROOT=%q\n' "$STEAM_ROOT"
    printf 'PROTON_DIR=%q\n' "$PROTON_DIR"
    printf 'PROTON_BIN=%q\n' "$PROTON_BIN"
    printf 'PROTON_PREFIX=%q\n' "$PROTON_PREFIX"
    printf 'NODE_BIN=%q\n' "$NODE_BIN"
    printf 'NODE_BIN_DIR=%q\n' "$(dirname "$NODE_BIN")"
    printf 'CARGO_BIN=%q\n' "$CARGO_BIN"
    printf 'MARKET_PROFILE=%q\n' "$MARKET_PROFILE"
    printf 'MARKET_LABEL=%q\n' "$MARKET_LABEL"
} > "$CONFIG"
chmod 600 "$CONFIG"

cat > "$EVEJS_REPO/INSTALL-LINUX-SUMMARY.txt" <<EOF
EveJS Linux installation
========================
EveJS version:     $EVEJS_VERSION
EVE client build:  $SUPPORTED_BUILD
EveJS repository:  $EVEJS_REPO
EVE tq path:       $TQ_PATH
Resource cache:    $CACHE_ROOT/ResFiles
Steam root:        $STEAM_ROOT
Proton:            $PROTON_DIR
Proton prefix:     $PROTON_PREFIX
Market:            $MARKET_LABEL
Launcher:          $EVEJS_REPO/evejs-linux-launcher.sh
Stale cleanup:     $EVEJS_REPO/evejs-stale-process-cleanup.sh
Host toolchain:    $EVEJS_REPO/LINUX-HOST-TOOLCHAIN.txt
SteamOS repair:    $EVEJS_REPO/STEAMOS-MINIMAL-REPAIR.txt (only if a repair was needed)
Mods directory:    $EVEJS_REPO/mods

Mod convention:
  mods/<mod-name>/loader.js

The launcher discovers each immediate mods/*/loader.js and supplies it through
Node --require before the normal EveJS server entry point.
EOF

ok "Launcher installed: $EVEJS_REPO/evejs-linux-launcher.sh"

# Optional desktop entry
if (( ! ARG_NO_DESKTOP )); then
    if ui_yesno "Create application shortcut?" "Create an application-menu shortcut for this EveJS installation?"; then
        mkdir -p "$HOME/.local/share/applications"
        DESKTOP_FILE="$HOME/.local/share/applications/evejs-${EVEJS_VERSION}-${PREFIX_HASH}.desktop"
        cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=EveJS $EVEJS_VERSION
Comment=Local EveJS server + EVE client
Exec=/bin/bash "$EVEJS_REPO/evejs-linux-launcher.sh"
Terminal=true
Categories=Game;
StartupNotify=false
EOF
        chmod +x "$DESKTOP_FILE"
        command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
        ok "Application shortcut created."
    fi
fi

FINAL="Installation complete.

EveJS:
$EVEJS_REPO

Launcher:
$EVEJS_REPO/evejs-linux-launcher.sh

Stale-process cleanup:
$EVEJS_REPO/evejs-stale-process-cleanup.sh

Steam Proton:
$(basename "$PROTON_DIR")

Market:
$MARKET_LABEL

Mods:
$EVEJS_REPO/mods/<mod-name>/loader.js

The first launcher start may ask for your sudo password so the normal user
process can bind EveJS's localhost HTTPS endpoint on port 443."

ui_info "EveJS installation complete" "$FINAL"

if (( ! ARG_NO_LAUNCH )); then
    if ui_yesno "Launch EveJS now?" "Start the new EveJS Linux launcher now?"; then
        exec "$EVEJS_REPO/evejs-linux-launcher.sh"
    fi
fi

printf '\n%s\n' "$FINAL"
