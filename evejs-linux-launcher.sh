#!/usr/bin/env bash
# ==============================================================================
# EveJS Portable Linux Launcher v1.16 — Stock-Only Edition (standard Linux + SteamOS portable)
#
# Reads .evejs-linux.conf from the EveJS repository.
#
# Startup order:
#   1. Migrate/verify constrained local TLS and exact-hash offline client patches.
#   2. Select the configured localhost HTTPS/CDN port (8443 on SteamOS).
#   3. Reconcile the selected copied EVE client's EveJS CA bundles.
#   4. Start standalone market server; wait for HTTP 40110 + RPC 40111.
#   5. Start game server LAST among servers; wait for process-owned:
#        HTTPS/CDN, XMPP 5222, game 26000, image 26001,
#        gateway 26002/26003, and Redshift 26400.
#   6. Initialize the selected Steam Proton prefix if needed.
#   7. Launch EVE only after all backend/TLS checks pass.
#   8. Menu option [N] launches any number of additional EVE clients against
#      the same running backend, each with its own PID file and log.
#
# Audited runtime policy:
#   * Stock EveJS releases installed or updated by this Setup are supported (an audited
#     Market source, or the official release recorded by the installer/updater).
#   * Living Universe is intentionally not supported by this Stock-only edition.
#   * Any Living Universe/X-Patch/unknown integrated source state is fail-closed.
#   * Older X-Patch source overlays remain rejected by the compatibility authority.
#
# The launcher never installs/updates/repairs EveJS or Living Universe. It may perform the
# mandatory EveJS CA/client migration, limited by exact certificate and binary identities.
# Market binaries are verified external runtime builds matched to installed source.
# ==============================================================================

set -uo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/.evejs-linux.conf"
[[ -f "$CONFIG" ]] || { echo "[ERROR] Missing launcher config: $CONFIG" >&2; exit 1; }
# shellcheck disable=SC1090
. "$CONFIG"

PLATFORM_MODE="${EVEJS_PLATFORM_MODE:-standard-linux}"
CDN_PORT="${EVEJS_CDN_PORT:-443}"
if [[ -r /etc/os-release ]] && grep -Eiq '^(ID|ID_LIKE|NAME)=.*steamos' /etc/os-release; then
    PLATFORM_MODE="steamos-portable"
    CDN_PORT="8443"
fi
[[ "$CDN_PORT" == "443" || "$CDN_PORT" == "8443" ]] \
    || { echo "[ERROR] Unsupported EveJS CDN port profile: $CDN_PORT" >&2; exit 1; }

export PATH="$NODE_BIN_DIR:$HOME/.cargo/bin:$PATH"

MODS_DIR="$EVEJS_REPO/mods"
MARKET_DIR="$EVEJS_REPO/externalservices/market-server"
MARKET_CONFIG="$MARKET_DIR/config/market-server.local.toml"
MARKET_DB="$MARKET_DIR/data/generated/market.sqlite"
MARKET_BIN=""
MARKET_HTTP="http://127.0.0.1:40110"
COMPAT_HELPER="$EVEJS_REPO/.evejs-linux-tools/evejs-linux-lu-compat.py"
[[ -f "$COMPAT_HELPER" ]] || COMPAT_HELPER="$EVEJS_REPO/evejs-linux-lu-compat.py"
CLIENT_PREFLIGHT_HELPER="$EVEJS_REPO/.evejs-linux-tools/evejs-linux-client-preflight.py"
[[ -f "$CLIENT_PREFLIGHT_HELPER" ]] || CLIENT_PREFLIGHT_HELPER="$EVEJS_REPO/evejs-linux-client-preflight.py"
EVEJS_RUNTIME_MODE=""
EVEJS_RUNTIME_LABEL=""
EVEJS_MARKET_PROFILE=""
EVEJS_MARKET_SOURCE_DIGEST=""
EVEJS_MARKET_RUNTIME_SHA256=""
EVEJS_RUNTIME_ROOT=""
EVEJS_XPATCH_VERSION=""
EVEJS_INTEGRATED_LU_ACTIVE="0"
EVEJS_INTEGRATED_LU_AUTHORITY=""
MARKET_RPC_PORT="40111"

XMPP_BIND_HOST="127.0.0.1"
XMPP_CONNECT_HOST="127.0.0.1"
XMPP_PORT="5222"
GATEWAY_TLS_HOST="127.0.0.1"
GATEWAY_TLS_PORT="26003"
GATEWAY_TLS_SNI="dev-public-gateway.evetech.net"
IMAGE_EDGE_PORT="26001"
REDSHIFT_EDGE_PORT="26400"

GAME_PORT="26000"
PROXY_URL="http://127.0.0.1:26002"
if [[ -f "$EVEJS_REPO/config/server.json" ]]; then
    GAME_PORT="$(cd "$EVEJS_REPO" && "$NODE_BIN" -e \
        'const c=require("./config/server.json"); process.stdout.write(String(c.network?.serverPort ?? 26000))' \
        2>/dev/null || echo 26000)"
    PROXY_URL="$(cd "$EVEJS_REPO" && "$NODE_BIN" -e \
        'const c=require("./config/server.json"); process.stdout.write(String(c.gateway?.microservicesPublicUrl ?? "http://127.0.0.1:26002/"))' \
        2>/dev/null || echo 'http://127.0.0.1:26002/')"
    PROXY_URL="${PROXY_URL%/}"
fi

CERT_DIR="$EVEJS_REPO/_local/certs"
CA_CERT="$CERT_DIR/xmpp-ca-cert.pem"
CA_KEY="$CERT_DIR/xmpp-ca-key.pem"
XMPP_CERT="$CERT_DIR/xmpp-dev-cert.pem"
XMPP_KEY="$CERT_DIR/xmpp-dev-key.pem"
GATEWAY_CERT="$CERT_DIR/gateway-dev-cert.pem"
GATEWAY_KEY="$CERT_DIR/gateway-dev-key.pem"
GATEWAY_DNS_CERT="$CERT_DIR/gateway-dns-cert.pem"
GATEWAY_DNS_KEY="$CERT_DIR/gateway-dns-key.pem"
TLS_DNS_NAMES="app.launchdarkly.com,clientstream.launchdarkly.com,clientsdk.launchdarkly.com,dev-public-gateway.evetech.net,events.launchdarkly.com,mobile.launchdarkly.com,public-gateway.evetech.net,sdk.launchdarkly.com,sentry.io,stream.launchdarkly.com,localhost"

LOG_DIR="$EVEJS_REPO/logs/launcher-linux"
ARCHIVE_DIR="$LOG_DIR/archive"
RUNNER_DIR="$LOG_DIR/.runners"
PID_DIR="$LOG_DIR/.pids"
MARKET_LOG="$LOG_DIR/market-latest.log"
SERVER_LOG="$LOG_DIR/server-latest.log"
CLIENT_LOG="$LOG_DIR/client-latest.log"
mkdir -p "$LOG_DIR" "$ARCHIVE_DIR" "$RUNNER_DIR" "$PID_DIR" "$PROTON_PREFIX"

MARKET_PID=""
SERVER_PID=""
CLIENT_PID=""
FULLLOG_PID=""

MOD_REQUIRE_ARGS=()
MOD_NAMES=()
MOD_COUNT=0

fail() { echo "[ERROR] $*" >&2; return 1; }

is_pid_running() { [[ -n "${1:-}" ]] && kill -0 "$1" 2>/dev/null; }

read_pidfile() {
    local f="$1"
    [[ -s "$f" ]] && cat "$f" 2>/dev/null || true
}

port_listening() {
    local port="$1"
    ss -H -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}$"
}

port_pids() {
    local port="$1"
    ss -H -ltnp 2>/dev/null \
      | awk -v needle=":${port}" '$4 ~ (needle "$") {print}' \
      | grep -oE 'pid=[0-9]+' \
      | cut -d= -f2 \
      | sort -nu
}

pid_cwd() {
    local pid="$1"
    readlink -f "/proc/$pid/cwd" 2>/dev/null || true
}

pid_cmdline() {
    local pid="$1"
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true
}

pid_ppid() {
    local pid="$1"
    awk '/^PPid:/ {print $2; exit}' "/proc/$pid/status" 2>/dev/null || true
}

pid_belongs_to_tree() {
    # True when candidate is root itself or has root somewhere in its PPid chain.
    local candidate="$1" root="$2" current ppid hops=0
    [[ "$candidate" =~ ^[0-9]+$ && "$root" =~ ^[0-9]+$ ]] || return 1

    current="$candidate"
    while (( hops < 128 )); do
        [[ "$current" == "$root" ]] && return 0
        ppid="$(pid_ppid "$current")"
        [[ "$ppid" =~ ^[0-9]+$ ]] || return 1
        (( ppid <= 1 )) && return 1
        [[ "$ppid" == "$current" ]] && return 1
        current="$ppid"
        hops=$((hops + 1))
    done
    return 1
}

port_exclusively_owned_by_tree() {
    # A port is healthy only when at least one PID is visible and EVERY visible
    # listener PID belongs to the expected process tree. This rejects a foreign
    # or stale process even if SO_REUSEPORT allows multiple owners.
    local port="$1" root="$2" pid found=0
    is_pid_running "$root" || return 1

    while read -r pid; do
        [[ -n "$pid" ]] || continue
        found=1
        pid_belongs_to_tree "$pid" "$root" || return 1
    done < <(port_pids "$port")

    (( found == 1 ))
}

port_owner_report() {
    local port="$1" root="${2:-}" label="${3:-port $1}" pid
    local -a pids=()
    mapfile -t pids < <(port_pids "$port")

    if ((${#pids[@]} == 0)); then
        echo "    $label ($port): no PID-visible listener"
        return
    fi

    echo "    $label ($port):"
    for pid in "${pids[@]}"; do
        if [[ -n "$root" ]] && pid_belongs_to_tree "$pid" "$root"; then
            echo "      ✓ PID $pid belongs to expected tree rooted at $root"
        else
            echo "      ✗ PID $pid is outside expected tree${root:+ rooted at $root}"
            describe_pid "$pid"
        fi
    done
}

evejs_repo_for_pid() {
    # Normal case: an EveJS process has cwd=<repo>/server or the market dir.
    # Recovery case: if the repository was deleted while EveJS was still
    # running, /proc/<pid>/cwd may end in " (deleted)". In that case identify
    # official EveJS edge workers from their absolute worker path or inherited
    # EVEJS_PROXY_LOCAL_INTERCEPT environment instead of requiring the old
    # repository to still exist.
    local pid="$1" cwd parent cmd env repo_guess
    cwd="$(pid_cwd "$pid")"
    cmd="$(pid_cmdline "$pid")"

    if [[ -n "$cwd" ]]; then
        local cwd_clean="${cwd% (deleted)}"
        if [[ "$(basename "$cwd_clean")" == "server" ]]; then
            parent="$(dirname "$cwd_clean")"
            if [[ -f "$parent/config/version.json" && -f "$parent/server/index.js" ]]; then
                printf '%s\n' "$parent"
                return 0
            fi
        fi

        if [[ "$cwd_clean" == */externalservices/market-server ]]; then
            parent="$(dirname "$(dirname "$cwd_clean")")"
            if [[ -f "$parent/config/version.json" && -f "$parent/server/index.js" ]]; then
                printf '%s\n' "$parent"
                return 0
            fi
        fi
    fi

    # Forked v0.12.x edge workers have an absolute
    # .../server/src/edge/<edge>/<name>.worker.js command path.
    if [[ "$cmd" =~ (.*)/server/src/edge/(chat|gateway|game|image|redshift)/[^[:space:]]*\.worker\.js ]]; then
        repo_guess="${BASH_REMATCH[1]}"
        printf '%s\n' "${repo_guess% (deleted)}"
        return 0
    fi

    # Fallback for a surviving main EveJS process whose cwd was deleted.
    # The universal launcher sets this variable on the EveJS process and all
    # forked edge workers. Require a Node command as an additional guard.
    if [[ "$cmd" == *node* ]] && [[ -r "/proc/$pid/environ" ]]; then
        env="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null || true)"
        if grep -qx 'EVEJS_PROXY_LOCAL_INTERCEPT=1' <<<"$env"; then
            if [[ "$cwd" == */server*" (deleted)"* || "$cmd" == *"/server"* ]]; then
                printf '%s\n' "${cwd%/server (deleted)}"
                return 0
            fi
        fi
    fi

    return 1
}


evejs_tree_root_for_pid() {
    # Given any EveJS listener/edge PID, climb through EveJS ancestors that map
    # to the same repository. This reaches the main Node server instead of
    # stopping only an edge worker.
    local pid="$1" repo current parent parent_repo best hops=0
    repo="$(evejs_repo_for_pid "$pid" 2>/dev/null || true)"
    [[ -n "$repo" ]] || return 1

    current="$pid"
    best="$pid"

    while (( hops < 128 )); do
        parent="$(pid_ppid "$current")"
        [[ "$parent" =~ ^[0-9]+$ ]] || break
        (( parent <= 1 )) && break

        parent_repo="$(evejs_repo_for_pid "$parent" 2>/dev/null || true)"
        [[ -n "$parent_repo" && "$parent_repo" == "$repo" ]] || break

        best="$parent"
        current="$parent"
        hops=$((hops + 1))
    done

    printf '%s\n' "$best"
}

pid_allowed_by_roots() {
    local pid="$1"; shift
    local root
    for root in "$@"; do
        [[ "$root" =~ ^[0-9]+$ ]] || continue
        if pid_belongs_to_tree "$pid" "$root"; then
            return 0
        fi
    done
    return 1
}

describe_pid() {
    local pid="$1" cwd cmd repo
    cwd="$(pid_cwd "$pid")"
    cmd="$(pid_cmdline "$pid")"
    repo="$(evejs_repo_for_pid "$pid" 2>/dev/null || true)"
    echo "      PID $pid"
    [[ -n "$repo" ]] && echo "        EveJS repo: $repo"
    [[ -n "$cwd" ]] && echo "        cwd:        $cwd"
    [[ -n "$cmd" ]] && echo "        command:    $cmd"
}

collect_descendants() {
    local parent="$1" child
    while read -r child; do
        [[ -n "$child" ]] || continue
        collect_descendants "$child"
        printf '%s\n' "$child"
    done < <(pgrep -P "$parent" 2>/dev/null || true)
}

wait_pid_exit() {
    local pid="$1" timeout_s="$2"
    local ticks=$(( timeout_s * 10 ))
    local i
    for ((i=0; i<ticks; i++)); do
        is_pid_running "$pid" || return 0
        sleep 0.1
    done
    return 1
}

stop_process_tree() {
    # IMPORTANT: EveJS owns forked chat/gateway/game/image edge children.
    # Give the parent its full graceful-shutdown opportunity first so it can
    # close those workers over IPC. Only if it exceeds the grace period do we
    # terminate the surviving tree ourselves.
    local pid="$1" label="$2" grace="${3:-20}"
    is_pid_running "$pid" || return 0

    echo "  Stopping $label (PID $pid) gracefully..."
    kill -TERM "$pid" 2>/dev/null || true

    if wait_pid_exit "$pid" "$grace"; then
        echo "  ✓ $label stopped gracefully"
        return 0
    fi

    echo "  ! $label did not exit within ${grace}s; terminating remaining process tree."
    mapfile -t descendants < <(collect_descendants "$pid" | awk 'NF' | sort -rn)

    local child
    for child in "${descendants[@]}"; do
        is_pid_running "$child" && kill -TERM "$child" 2>/dev/null || true
    done
    kill -TERM "$pid" 2>/dev/null || true
    sleep 2

    for child in "${descendants[@]}"; do
        is_pid_running "$child" && kill -KILL "$child" 2>/dev/null || true
    done
    is_pid_running "$pid" && kill -KILL "$pid" 2>/dev/null || true

    wait_pid_exit "$pid" 3 || true
    echo "  ✓ $label process tree terminated"
}

cleanup_evejs_port_pid() {
    local pid="$1" repo root label
    repo="$(evejs_repo_for_pid "$pid" 2>/dev/null || true)"
    [[ -n "$repo" ]] || return 1
    root="$(evejs_tree_root_for_pid "$pid" 2>/dev/null || true)"
    [[ "$root" =~ ^[0-9]+$ ]] || root="$pid"

    label="stale EveJS process tree from $(basename "$repo")"
    stop_process_tree "$root" "$label" 30
}

reconcile_required_ports() {
    # Detect leftovers from interrupted/forced prior launchers.
    #
    # Optional arguments are process-tree roots that are EXPECTED to remain
    # alive (for example the market daemon during a game-only restart). Ports
    # owned exclusively by those trees are allowed. Everything else must either
    # be safely identified as stale EveJS or cause startup to stop.
    local -a allowed_roots=("$@")
    local ports=("$CDN_PORT" 5222 26000 26001 26002 26003 26400 40110 40111)
    local port pid repo root
    local -a stale_roots=()
    local -a unknown_pids=()
    declare -A seen_pid=()
    declare -A seen_root=()

    for port in "${ports[@]}"; do
        while read -r pid; do
            [[ -n "$pid" ]] || continue
            [[ -n "${seen_pid[$pid]:-}" ]] && continue
            seen_pid[$pid]=1

            if pid_allowed_by_roots "$pid" "${allowed_roots[@]}"; then
                continue
            fi

            repo="$(evejs_repo_for_pid "$pid" 2>/dev/null || true)"
            if [[ -n "$repo" ]]; then
                root="$(evejs_tree_root_for_pid "$pid" 2>/dev/null || true)"
                [[ "$root" =~ ^[0-9]+$ ]] || root="$pid"
                if [[ -z "${seen_root[$root]:-}" ]]; then
                    seen_root[$root]=1
                    stale_roots+=("$root")
                fi
            else
                unknown_pids+=("$pid")
            fi
        done < <(port_pids "$port")
    done

    if ((${#unknown_pids[@]} > 0)); then
        echo
        echo "[ERROR] One or more EveJS-required ports are occupied by processes"
        echo "that the launcher cannot safely identify or allow:"
        for pid in "${unknown_pids[@]}"; do describe_pid "$pid"; done
        echo
        echo "Current relevant listeners:"
        ss -H -ltnp 2>/dev/null | grep -E ":(${CDN_PORT}|5222|26000|26001|26002|26003|26400|40110|40111)([[:space:]]|$)" || true
        return 1
    fi

    if ((${#stale_roots[@]} == 0)); then
        return 0
    fi

    echo
    echo "The launcher found older EveJS process tree(s) holding required ports."
    echo "Each listed PID is the highest EveJS ancestor resolved for that tree:"
    echo
    for root in "${stale_roots[@]}"; do describe_pid "$root"; done
    echo
    read -r -p "Stop these stale EveJS process trees and continue? [Y/n]: " answer
    case "${answer,,}" in
        n|no) return 1 ;;
    esac

    for root in "${stale_roots[@]}"; do
        if is_pid_running "$root"; then
            repo="$(evejs_repo_for_pid "$root" 2>/dev/null || true)"
            stop_process_tree "$root" "stale EveJS process tree${repo:+ from $(basename "$repo")}" 30
        fi
    done

    sleep 1

    # Final gate: remaining listeners are acceptable ONLY if they belong
    # exclusively to one of the explicitly allowed trees.
    for port in "${ports[@]}"; do
        port_listening "$port" || continue

        local okay=0 allowed_root
        for allowed_root in "${allowed_roots[@]}"; do
            if port_exclusively_owned_by_tree "$port" "$allowed_root"; then
                okay=1
                break
            fi
        done

        if (( ! okay )); then
            echo "[ERROR] Port $port remains occupied by an unexpected process after cleanup." >&2
            ss -H -ltnp 2>/dev/null | grep -E ":${port}([[:space:]]|$)" || true
            return 1
        fi
    done

    echo "  ✓ Stale EveJS process-tree cleanup complete"
}

detect_terminal() {
    if [[ -n "${EVEJS_TERMINAL:-}" ]]; then
        TERMINAL_KIND="custom"
        TERMINAL_CMD="$EVEJS_TERMINAL"
        return
    fi
    local t
    for t in konsole gnome-terminal kgx xfce4-terminal kitty alacritty wezterm foot xterm; do
        if command -v "$t" >/dev/null 2>&1; then
            TERMINAL_KIND="$t"
            TERMINAL_CMD="$(command -v "$t")"
            return
        fi
    done
    TERMINAL_KIND="none"
    TERMINAL_CMD=""
}

open_terminal() {
    local title="$1" runner="$2"
    case "$TERMINAL_KIND" in
        custom)
            # Custom command may use {title} and {script} placeholders.
            local cmd="${TERMINAL_CMD//\{title\}/$title}"
            cmd="${cmd//\{script\}/$runner}"
            bash -lc "$cmd" &
            ;;
        konsole) "$TERMINAL_CMD" --separate -p tabtitle="$title" -e bash "$runner" & ;;
        gnome-terminal) "$TERMINAL_CMD" --title="$title" -- bash "$runner" & ;;
        kgx) "$TERMINAL_CMD" --title="$title" -- bash "$runner" & ;;
        xfce4-terminal) "$TERMINAL_CMD" --title="$title" --command="bash $(printf '%q' "$runner")" & ;;
        kitty) "$TERMINAL_CMD" --title "$title" bash "$runner" & ;;
        alacritty) "$TERMINAL_CMD" --title "$title" -e bash "$runner" & ;;
        wezterm) "$TERMINAL_CMD" start --always-new-process -- bash "$runner" & ;;
        foot) "$TERMINAL_CMD" -T "$title" bash "$runner" & ;;
        xterm) "$TERMINAL_CMD" -T "$title" -e bash "$runner" & ;;
        none)
            echo "  ! No supported terminal emulator detected; running '$title' in background."
            nohup bash "$runner" >/dev/null 2>&1 &
            ;;
    esac
}

wait_for_pidfile() {
    local f="$1" i pid
    for i in $(seq 1 100); do
        pid="$(read_pidfile "$f")"
        if is_pid_running "$pid"; then
            printf '%s\n' "$pid"
            return 0
        fi
        sleep 0.1
    done
    return 1
}

status_line() {
    local pid="$1"
    if is_pid_running "$pid"; then
        printf '\033[0;32m● RUNNING\033[0m (PID %s)' "$pid"
    else
        printf '\033[0;31m○ STOPPED\033[0m'
    fi
}

rotate_one_log() {
    local src="$1" prefix="$2" stamp
    stamp="$(date '+%Y%m%d-%H%M%S')"
    [[ -s "$src" ]] && mv "$src" "$ARCHIVE_DIR/${prefix}-${stamp}.log"
    : > "$src"
}

detect_mods() {
    MOD_REQUIRE_ARGS=()
    MOD_NAMES=()
    MOD_COUNT=0
    DISABLED_LU_LOADER=0
    local mod_dir loader name
    shopt -s nullglob
    for mod_dir in "$MODS_DIR"/*/; do
        loader="${mod_dir}loader.js"
        [[ -f "$loader" ]] || continue
        name="$(basename "${mod_dir%/}")"
        if [[ "$name" == "x-eve-living-universe" ]]; then
            # This Stock-only Launcher never activates the Living Universe loader variant.
            DISABLED_LU_LOADER=1
            continue
        fi
        MOD_REQUIRE_ARGS+=( --require "$loader" )
        MOD_NAMES+=( "$name" )
        MOD_COUNT=$((MOD_COUNT+1))
    done
    shopt -u nullglob
}

show_mods() {
    if (( DISABLED_LU_LOADER )); then
        echo "  [LU] mods/x-eve-living-universe is present but DISABLED on EveJS $EVEJS_VERSION"
        echo "       (Living Universe is unavailable in the Stock-only Setup v1.16 edition)"
    fi
    if (( MOD_COUNT == 0 )); then
        echo "  [MODS] no enabled loader mods detected"
    else
        echo "  [MODS] $MOD_COUNT enabled loader(s):"
        local n
        for n in "${MOD_NAMES[@]}"; do echo "         • $n"; done
    fi
}


refresh_runtime_compatibility() {
    [[ -f "$COMPAT_HELPER" ]] || {
        echo "[ERROR] Missing runtime compatibility helper: $COMPAT_HELPER" >&2
        echo "Run the EveJS Universal Linux Installer and choose Prepare/Verify Runtime Compatibility." >&2
        return 1
    }
    local assignments
    if ! assignments="$(python3 "$COMPAT_HELPER" verify-runtime --repo "$EVEJS_REPO" --shell)"; then
        echo >&2
        echo "[RUNTIME COMPATIBILITY BLOCKED]" >&2
        echo "No protected files were changed." >&2
        echo "Run the EveJS Universal Linux Installer and choose Prepare/Verify Runtime Compatibility." >&2
        return 1
    fi
    # Output comes only from our packaged helper and every value is shell-quoted.
    eval "$assignments"
    MARKET_BIN="$EVEJS_MARKET_RUNTIME_BIN"
    [[ -x "$MARKET_BIN" ]] || { echo "[ERROR] Verified Market runtime binary is not executable: $MARKET_BIN" >&2; return 1; }
    return 0
}

market_process_binary_matches() {
    local pid="$1" expected actual
    is_pid_running "$pid" || return 1
    expected="$(readlink -f "$MARKET_BIN" 2>/dev/null || true)"
    actual="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
    [[ -n "$expected" && "$actual" == "$expected" ]]
}

market_rpc_probe_integrated_lu() { return 0; }

ensure_loopback_cdn_permission() {
    local current
    if port_listening "$CDN_PORT"; then
        echo "[ERROR] TCP port $CDN_PORT is already in use." >&2
        echo "Current owner information:" >&2
        ss -H -ltnp 2>/dev/null | grep -E ":${CDN_PORT}([[:space:]]|$)" >&2 || true
        return 1
    fi
    if (( CDN_PORT >= 1024 )); then
        return 0
    fi
    current="$(sysctl -n net.ipv4.ip_unprivileged_port_start 2>/dev/null || echo 1024)"
    if [[ "$current" =~ ^[0-9]+$ ]] && (( current > CDN_PORT )); then
        echo
        echo "EveJS needs its localhost HTTPS/CDN responder on 127.0.0.1:$CDN_PORT."
        echo "Linux currently protects ports below $current."
        echo "You may be asked for your sudo password."
        sudo sysctl -w "net.ipv4.ip_unprivileged_port_start=$CDN_PORT" >/dev/null || return 1
        current="$(sysctl -n net.ipv4.ip_unprivileged_port_start 2>/dev/null || echo 1024)"
    fi
    [[ "$current" =~ ^[0-9]+$ ]] && (( current <= CDN_PORT ))
}

repair_client_ca_bundles() {
    python3 - "$TQ_PATH" "$CA_CERT" <<'PY'
from pathlib import Path
import datetime, re, subprocess, sys, tempfile
tq=Path(sys.argv[1]); ca=Path(sys.argv[2]).read_bytes().strip()
PEM=re.compile(rb"-----BEGIN CERTIFICATE-----\s*.*?-----END CERTIFICATE-----",re.S)
def meta(block):
    with tempfile.NamedTemporaryFile(suffix=".pem") as f:
        f.write(block+b"\n"); f.flush()
        p=subprocess.run(["openssl","x509","-in",f.name,"-noout","-subject","-issuer","-fingerprint","-sha256"],
                         capture_output=True,text=True)
        if p.returncode:return None
        d={"subject":"","issuer":"","fp":""}
        for line in p.stdout.splitlines():
            low=line.lower()
            if low.startswith("subject="):d["subject"]=line.split("=",1)[1].strip()
            elif low.startswith("issuer="):d["issuer"]=line.split("=",1)[1].strip()
            elif "fingerprint=" in low:d["fp"]=line.split("=",1)[1].replace(":","").strip().upper()
        return d
cam=meta(ca)
if not cam: raise SystemExit("cannot parse current EveJS CA")
cafp=cam["fp"]
fixed=[tq/"bin64"/"cacert.pem",tq/"bin64"/"packages"/"certifi"/"cacert.pem",
       tq/"bin"/"cacert.pem",tq/"bin"/"packages"/"certifi"/"cacert.pem"]
bundles={p.resolve() for p in fixed if p.is_file()}
bundles.update(p.resolve() for p in tq.rglob("cacert.pem") if p.is_file())
bundles=sorted(bundles)
if not bundles: raise SystemExit("no client cacert.pem bundles found")
stamp=datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
for bundle in bundles:
    original=bundle.read_bytes(); pieces=[]; cursor=0; have=False
    for m in PEM.finditer(original):
        pieces.append(original[cursor:m.start()])
        block=m.group(0).strip(); cm=meta(block); keep=True
        if cm:
            subject=cm["subject"].lower(); issuer=cm["issuer"].lower(); fp=cm["fp"]
            local=("evejs local" in subject or "evejs local" in issuer
                   or "eve.js public gateway tls" in subject or "eve.js public gateway tls" in issuer)
            if fp==cafp:
                if have:keep=False
                else:have=True
            elif local:keep=False
        if keep:pieces.append(block)
        cursor=m.end()
    pieces.append(original[cursor:])
    updated=b"".join(pieces).rstrip()+b"\n"
    if not have:updated+=b"\n"+ca+b"\n"
    if updated!=original:
        backup=bundle.with_name(bundle.name+f".pre-evejs-launch-{stamp}")
        backup.write_bytes(original); bundle.write_bytes(updated)
    current=0; stale=0
    for m in PEM.finditer(bundle.read_bytes()):
        cm=meta(m.group(0).strip())
        if not cm:continue
        if cm["fp"]==cafp:current+=1;continue
        subject=cm["subject"].lower();issuer=cm["issuer"].lower()
        if ("evejs local" in subject or "evejs local" in issuer
            or "eve.js public gateway tls" in subject or "eve.js public gateway tls" in issuer):stale+=1
    if current!=1 or stale:
        raise SystemExit(f"bundle trust verification failed: {bundle} current={current} stale={stale}")
print(f"  ✓ Client CA bundles reconciled: {len(bundles)}")
PY
}

ensure_local_tls_material() {
    local builder="$EVEJS_REPO/tools/ClientSETUP/scripts/build-gateway-cert.js"
    [[ -f "$builder" ]] || { echo "[ERROR] Missing EveJS TLS builder: $builder" >&2; return 1; }
    mkdir -p "$CERT_DIR"

    # v0.12.9 refuses the older unconstrained CA. --rotate-ca replaces it only
    # when it is missing/invalid; an already-valid constrained CA is retained.
    "$NODE_BIN" "$builder" \
        --ca-only --ensure-ca --rotate-ca \
        --ca-cert "$CA_CERT" --ca-key "$CA_KEY" || return 1

    if ! "$NODE_BIN" "$builder" --check-only \
        --ca-cert "$CA_CERT" --ca-key "$CA_KEY" \
        --dns "localhost" --ip "127.0.0.1" \
        --out-cert "$XMPP_CERT" --out-key "$XMPP_KEY" >/dev/null 2>&1; then
        "$NODE_BIN" "$builder" \
            --ca-cert "$CA_CERT" --ca-key "$CA_KEY" \
            --dns "localhost" --ip "127.0.0.1" \
            --out-cert "$XMPP_CERT" --out-key "$XMPP_KEY" || return 1
    fi

    if ! "$NODE_BIN" "$builder" --check-only \
        --ca-cert "$CA_CERT" --ca-key "$CA_KEY" \
        --dns "$TLS_DNS_NAMES" --ip "127.0.0.1" \
        --out-cert "$GATEWAY_CERT" --out-key "$GATEWAY_KEY" >/dev/null 2>&1; then
        "$NODE_BIN" "$builder" \
            --ca-cert "$CA_CERT" --ca-key "$CA_KEY" \
            --dns "$TLS_DNS_NAMES" --ip "127.0.0.1" \
            --out-cert "$GATEWAY_CERT" --out-key "$GATEWAY_KEY" || return 1
    fi

    if ! "$NODE_BIN" "$builder" --check-only \
        --ca-cert "$CA_CERT" --ca-key "$CA_KEY" --dns-only \
        --dns "$TLS_DNS_NAMES" \
        --out-cert "$GATEWAY_DNS_CERT" --out-key "$GATEWAY_DNS_KEY" >/dev/null 2>&1; then
        "$NODE_BIN" "$builder" \
            --ca-cert "$CA_CERT" --ca-key "$CA_KEY" --dns-only \
            --dns "$TLS_DNS_NAMES" \
            --out-cert "$GATEWAY_DNS_CERT" --out-key "$GATEWAY_DNS_KEY" || return 1
    fi

    echo "  ✓ Constrained CA and XMPP/numeric/DNS TLS leaves verified in _local/certs"
}

verify_tls_endpoint() {
    local host="$1" port="$2" sni="$3" label="$4" output
    output="$(
        timeout 6 openssl s_client \
            -connect "${host}:${port}" -servername "$sni" \
            -CAfile "$CA_CERT" -verify_return_error </dev/null 2>&1 || true
    )"
    grep -q "Verify return code: 0 (ok)" <<<"$output" && return 0
    echo "  ✗ $label TLS verification failed at ${host}:${port} (SNI $sni)"
    echo "$output" | tail -n 16
    return 1
}

ensure_proton_prefix() {
    if [[ -e "$PROTON_PREFIX/pfx/dosdevices/z:" ]]; then
        return 0
    fi
    echo "  Initializing Steam Proton prefix..."
    mkdir -p "$PROTON_PREFIX"
    STEAM_COMPAT_DATA_PATH="$PROTON_PREFIX" \
    STEAM_COMPAT_CLIENT_INSTALL_PATH="$STEAM_ROOT" \
    STEAM_COMPAT_APP_ID=0 \
    SteamAppId=0 SteamGameId=0 \
    WINEDEBUG=-all \
    "$PROTON_BIN" run cmd.exe /c exit >/dev/null 2>&1 || {
        echo "  ! Proton prefix initialization command returned an error."
    }
    for _ in $(seq 1 40); do
        [[ -e "$PROTON_PREFIX/pfx/dosdevices/z:" ]] && return 0
        sleep 0.25
    done
    echo "[ERROR] Proton prefix did not create its normal Z: drive mapping." >&2
    return 1
}

windows_z_path() {
    local p="$1"
    p="${p//\//\\}"
    printf 'Z:%s\n' "$p"
}

stop_pid() {
    local pid="$1" label="$2" grace="${3:-10}"
    stop_process_tree "$pid" "$label" "$grace"
}

cleanup() {
    echo
    echo "[eve.js] Stopping this launcher session..."
    # Defined further down; an early preflight exit runs this trap before that.
    declare -F stop_extra_clients >/dev/null && stop_extra_clients
    stop_pid "$CLIENT_PID" "EVE client" 10
    stop_pid "$SERVER_PID" "game server" 30
    stop_pid "$MARKET_PID" "market server" 10
}
trap cleanup EXIT INT TERM

# ── Preflight ─────────────────────────────────────────────────────────────────
required_commands=("$NODE_BIN" python3 openssl curl ss timeout pgrep)
if (( CDN_PORT < 1024 )); then
    required_commands+=(sysctl sudo)
fi
for cmd in "${required_commands[@]}"; do
    [[ -x "$cmd" ]] || command -v "$cmd" >/dev/null 2>&1 || { echo "[ERROR] Missing command: $cmd" >&2; exit 1; }
done

[[ -d "$EVEJS_REPO" ]] || { echo "[ERROR] EveJS repo missing: $EVEJS_REPO" >&2; exit 1; }
[[ -x "$PROTON_BIN" || -f "$PROTON_BIN" ]] || { echo "[ERROR] Proton missing: $PROTON_BIN" >&2; exit 1; }
[[ -s "$MARKET_DB" ]] || { echo "[ERROR] Market database missing." >&2; exit 1; }
[[ -f "$COMPAT_HELPER" ]] || { echo "[ERROR] LU compatibility helper missing. Run Installer → Prepare/Verify Runtime Compatibility." >&2; exit 1; }
[[ -f "$CLIENT_PREFLIGHT_HELPER" ]] || { echo "[ERROR] Client preflight helper missing. Run Installer → Prepare/Verify Runtime Compatibility." >&2; exit 1; }
[[ -f "$EVEJS_REPO/_local/gameStore/manifest.json" ]] || { echo "[ERROR] EveJS game database missing." >&2; exit 1; }
[[ -f "$TQ_PATH/bin64/exefile.exe" ]] || { echo "[ERROR] EVE client missing." >&2; exit 1; }

ensure_local_tls_material || exit 1
[[ -f "$CA_CERT" && -f "$XMPP_CERT" && -f "$GATEWAY_CERT" && -f "$GATEWAY_DNS_CERT" ]] || {
    echo "[ERROR] EveJS TLS certificate set is incomplete." >&2; exit 1;
}

CLIENT_BUILD="$(awk -F= 'tolower($1) ~ /^[[:space:]]*build[[:space:]]*$/ {gsub(/[[:space:]]/, "", $2); print $2; exit}' "$TQ_PATH/start.ini")"
[[ "$CLIENT_BUILD" == "$SUPPORTED_BUILD" ]] || { echo "[ERROR] EVE client build changed: $CLIENT_BUILD (expected $SUPPORTED_BUILD)." >&2; exit 1; }

grep -Eiq '^[[:space:]]*cryptopack[[:space:]]*=[[:space:]]*Placebo[[:space:]]*$' "$TQ_PATH/start.ini" || {
    echo "[ERROR] start.ini no longer contains cryptoPack = Placebo." >&2; exit 1;
}
grep -Eiq '^[[:space:]]*(server|serverip)[[:space:]]*=[[:space:]]*127\.0\.0\.1[[:space:]]*$' "$TQ_PATH/start.ini" || {
    echo "[ERROR] start.ini no longer targets 127.0.0.1." >&2; exit 1;
}

# A folder set up by v1.14 keeps its v1.14 helper, which has no --cdn-port option
# and always uses the standard port-443 patch. Use it as-is on port 443.
client_preflight_args=(--repo "$EVEJS_REPO" --tq "$TQ_PATH" --build "$SUPPORTED_BUILD")
client_preflight_usage="$(python3 "$CLIENT_PREFLIGHT_HELPER" -h 2>/dev/null || true)"
if [[ "$client_preflight_usage" == *--cdn-port* ]]; then
    client_preflight_args+=(--cdn-port "$CDN_PORT")
elif [[ "$CDN_PORT" != "443" ]]; then
    echo "[ERROR] This folder's client preflight helper predates v1.15 and cannot use port $CDN_PORT. Run Installer → Prepare/Verify Runtime Compatibility." >&2
    exit 1
else
    echo "  ! Older client preflight helper found (no --cdn-port); using its standard port-443 check."
    echo "    Installer → Prepare/Verify Runtime Compatibility refreshes the helper files."
fi
python3 "$CLIENT_PREFLIGHT_HELPER" apply "${client_preflight_args[@]}" || exit 1

detect_terminal
repair_client_ca_bundles || exit 1
detect_mods

# ── Runners ───────────────────────────────────────────────────────────────────
write_market_runner() {
    local runner="$RUNNER_DIR/run-market.sh" pidfile="$PID_DIR/market.pid"
    cat > "$runner" <<EOF
#!/usr/bin/env bash
set -o pipefail
echo \$\$ > $(printf '%q' "$pidfile")
trap 'rm -f $(printf '%q' "$pidfile")' EXIT
cd $(printf '%q' "$MARKET_DIR") || exit 1
exec > >(tee -a $(printf '%q' "$MARKET_LOG")) 2>&1
echo '══════════════════════════════════════════'
echo '  EveJS $EVEJS_VERSION Standalone Market'
echo '  Runtime: $(printf '%s' "$EVEJS_RUNTIME_LABEL")'
echo '  Binary:  $(printf '%s' "$MARKET_BIN")'
echo '  SHA256:  $(printf '%s' "$EVEJS_MARKET_RUNTIME_SHA256")'
echo '  Source:  $(printf '%s' "$EVEJS_MARKET_SOURCE_DIGEST")'
echo '  HTTP: 127.0.0.1:40110'
echo '  RPC : 127.0.0.1:40111'
echo '══════════════════════════════════════════'
exec $(printf '%q' "$MARKET_BIN") --config config/market-server.local.toml serve
EOF
    chmod +x "$runner"
    printf '%s\n' "$runner"
}

launch_market_server() {
    refresh_runtime_compatibility || return 1
    if is_pid_running "$MARKET_PID"; then
        if market_process_binary_matches "$MARKET_PID"; then
            return 0
        fi
        echo "[ERROR] Running Market process is not the binary verified for the current installed source." >&2
        echo "Perform a Full ordered reload after preparing compatibility." >&2
        return 1
    fi
    rotate_one_log "$MARKET_LOG" market
    rm -f "$PID_DIR/market.pid"
    local runner
    runner="$(write_market_runner)"
    echo
    echo "[1/2 SERVER STARTUP] Starting standalone market server..."
    open_terminal "EveJS $EVEJS_VERSION — Market Server" "$runner"
    MARKET_PID="$(wait_for_pidfile "$PID_DIR/market.pid")" || { echo "  ✗ Market process did not start."; return 1; }

    local ready=0
    for _ in $(seq 1 120); do
        if curl -sf --max-time 2 "$MARKET_HTTP/health" >/dev/null 2>&1 \
            && port_exclusively_owned_by_tree 40110 "$MARKET_PID" \
            && port_exclusively_owned_by_tree "$MARKET_RPC_PORT" "$MARKET_PID"; then
            ready=1
            break
        fi
        is_pid_running "$MARKET_PID" || break
        sleep 1
    done

    if (( ! ready )); then
        echo "  ✗ Market server did not become healthy with correct process ownership."
        port_owner_report 40110 "$MARKET_PID" "Market HTTP"
        port_owner_report "$MARKET_RPC_PORT" "$MARKET_PID" "Market RPC"
        echo "    Log: $MARKET_LOG"
        return 1
    fi

    if ! market_process_binary_matches "$MARKET_PID"; then
        echo "  ✗ Market PID does not execute the exact verified runtime binary."
        stop_pid "$MARKET_PID" "market server" 10; MARKET_PID=""
        return 1
    fi
    if ! market_rpc_probe_integrated_lu; then
        echo "  ✗ Integrated LU Market RPC contract probe failed."
        stop_pid "$MARKET_PID" "market server" 10; MARKET_PID=""
        return 1
    fi
    echo "  ✓ Market HTTP: 127.0.0.1:40110 (owned by current market tree)"
    echo "  ✓ Market RPC:  127.0.0.1:$MARKET_RPC_PORT (owned by current market tree)"
    echo "  ✓ Market binary: $MARKET_BIN"
}

write_server_runner() {
    local runner="$RUNNER_DIR/run-server.sh" pidfile="$PID_DIR/server.pid"
    local mod_info="${EVEJS_RUNTIME_LABEL:-Stock EveJS}"
    (( MOD_COUNT > 0 )) && mod_info+=" + $MOD_COUNT loader(s): ${MOD_NAMES[*]}"

    local node_args=(
        "$NODE_BIN"
        --report-on-fatalerror
        --report-uncaught-exception
        --report-dir=./logs/node-reports
        --max-old-space-size=8192
    )
    (( MOD_COUNT > 0 )) && node_args+=( "${MOD_REQUIRE_ARGS[@]}" )
    node_args+=( . )

    {
        echo '#!/usr/bin/env bash'
        echo 'set -o pipefail'
        printf 'echo $$ > %q\n' "$pidfile"
        printf "trap 'rm -f %q' EXIT\n" "$pidfile"
        printf 'EVEJS_REPO=%q\n' "$EVEJS_REPO"
        printf 'SERVER_LOG=%q\n' "$SERVER_LOG"
        printf 'MOD_INFO=%q\n' "$mod_info"
        printf 'CDN_PORT=%q\n' "$CDN_PORT"
        printf 'NODE_ARGS=('
        printf ' %q' "${node_args[@]}"
        echo ' )'
        cat <<'EOF'
cd "$EVEJS_REPO/server" || exit 1
export EVEJS_PROXY_LOCAL_INTERCEPT=1
export EVEJS_PROXY_LOOPBACK_CDN_LISTEN_PORT="$CDN_PORT"
export EVEJS_XMPP_SERVER_BIND_HOST=127.0.0.1
export EVEJS_XMPP_CONNECT_HOST=127.0.0.1
export FORCE_COLOR=1
mkdir -p logs/node-reports

if [[ -f "$EVEJS_REPO/server/src/gameStore/migrateLegacyNewDatabase.js" ]]; then
    "${NODE_ARGS[0]}" "$EVEJS_REPO/server/src/gameStore/migrateLegacyNewDatabase.js"
fi
if [[ -f "$EVEJS_REPO/server/src/gameStore/migrateRuntimeImages.js" ]]; then
    "${NODE_ARGS[0]}" "$EVEJS_REPO/server/src/gameStore/migrateRuntimeImages.js" --quiet
fi

exec > >(tee -a "$SERVER_LOG") 2>&1
echo '══════════════════════════════════════════'
echo '  EveJS Game Server'
echo "  $MOD_INFO"
echo '  market RPC:      127.0.0.1:40111'
echo "  HTTPS/CDN:       127.0.0.1:$CDN_PORT"
echo '  XMPP/TLS:        127.0.0.1:5222'
echo '  XMPP advertised: 127.0.0.1'
echo '══════════════════════════════════════════'
exec "${NODE_ARGS[@]}"
EOF
    } > "$runner"
    chmod +x "$runner"
    printf '%s\n' "$runner"
}

launch_game_server() {
    if is_pid_running "$SERVER_PID"; then return 0; fi
    refresh_runtime_compatibility || return 1
    market_process_binary_matches "$MARKET_PID" || {
        echo "  Market process binary does not match the current verified source; game start blocked."
        return 1
    }
    curl -sf --max-time 2 "$MARKET_HTTP/health" >/dev/null 2>&1 \
        && port_exclusively_owned_by_tree 40110 "$MARKET_PID" \
        && port_exclusively_owned_by_tree "$MARKET_RPC_PORT" "$MARKET_PID" || {
        echo "  Market is not ready or is not owned by the current market process tree;"
        echo "  refusing to start the game server out of order."
        port_owner_report 40110 "$MARKET_PID" "Market HTTP"
        port_owner_report "$MARKET_RPC_PORT" "$MARKET_PID" "Market RPC"
        return 1
    }

    detect_mods
    rotate_one_log "$SERVER_LOG" server
    rm -f "$PID_DIR/server.pid"
    local runner
    runner="$(write_server_runner)"
    echo
    echo "[2/2 SERVER STARTUP] Market is ready — starting game server LAST..."
    show_mods
    open_terminal "EveJS $EVEJS_VERSION — Game Server" "$runner"
    SERVER_PID="$(wait_for_pidfile "$PID_DIR/server.pid")" || { echo "  ✗ Game process did not start."; return 1; }

    local ready=0 cdn=0 gateway=0 game=0 image=0 redshift=0 xmpp=0 gtls=0
    echo "  Waiting for OWNED game + image + redshift + gateway + XMPP/TLS readiness..."

    for _ in $(seq 1 100); do
        cdn=0; gateway=0; game=0; image=0; redshift=0; xmpp=0; gtls=0

        port_exclusively_owned_by_tree "$CDN_PORT" "$SERVER_PID" && cdn=1

        if port_exclusively_owned_by_tree 26002 "$SERVER_PID" \
            && curl -sf --max-time 2 "$PROXY_URL/health" >/dev/null 2>&1; then
            gateway=1
        fi

        port_exclusively_owned_by_tree "$GAME_PORT" "$SERVER_PID" && game=1
        port_exclusively_owned_by_tree "$IMAGE_EDGE_PORT" "$SERVER_PID" && image=1
        port_exclusively_owned_by_tree "$REDSHIFT_EDGE_PORT" "$SERVER_PID" && redshift=1

        if port_exclusively_owned_by_tree "$XMPP_PORT" "$SERVER_PID" \
            && verify_tls_endpoint "$XMPP_BIND_HOST" "$XMPP_PORT" localhost XMPP >/dev/null 2>&1; then
            xmpp=1
        fi

        if port_exclusively_owned_by_tree "$GATEWAY_TLS_PORT" "$SERVER_PID" \
            && verify_tls_endpoint "$GATEWAY_TLS_HOST" "$GATEWAY_TLS_PORT" "$GATEWAY_TLS_SNI" gateway >/dev/null 2>&1; then
            gtls=1
        fi

        if (( cdn && gateway && game && image && redshift && xmpp && gtls )); then
            ready=1
            break
        fi

        is_pid_running "$SERVER_PID" || break
        sleep 1
    done

    if (( ready )); then
        echo "  ✓ HTTPS/CDN:       127.0.0.1:$CDN_PORT   (owned by current game tree)"
        echo "  ✓ Game listener:   127.0.0.1:$GAME_PORT (owned by current game tree)"
        echo "  ✓ ImageEdge:       127.0.0.1:$IMAGE_EDGE_PORT (owned by current game tree)"
        echo "  ✓ Gateway health:  $PROXY_URL/health (port owned by current game tree)"
        echo "  ✓ Gateway TLS:     127.0.0.1:$GATEWAY_TLS_PORT (owned + TLS verified)"
        echo "  ✓ XMPP TLS:        127.0.0.1:$XMPP_PORT (owned + TLS verified)"
        echo "  ✓ RedshiftEdge:    127.0.0.1:$REDSHIFT_EDGE_PORT (owned by current game tree)"
        return 0
    fi

    echo "  ✗ Game server did not reach full readiness with correct process ownership."
    echo "    Expected game tree root PID: $SERVER_PID"
    (( cdn )) || port_owner_report "$CDN_PORT" "$SERVER_PID" "HTTPS/CDN"
    (( game )) || port_owner_report "$GAME_PORT" "$SERVER_PID" "Game"
    (( image )) || port_owner_report "$IMAGE_EDGE_PORT" "$SERVER_PID" "ImageEdge"
    (( gateway )) || port_owner_report 26002 "$SERVER_PID" "Gateway HTTP"
    (( gtls )) || port_owner_report "$GATEWAY_TLS_PORT" "$SERVER_PID" "Gateway TLS"
    (( xmpp )) || port_owner_report "$XMPP_PORT" "$SERVER_PID" "XMPP"
    (( redshift )) || port_owner_report "$REDSHIFT_EDGE_PORT" "$SERVER_PID" "RedshiftEdge"
    echo "    Log: $SERVER_LOG"
    return 1
}

write_client_runner() {
    # Optional arguments let additional clients use their own runner, PID file
    # and log while sharing the same verified backend, prefix and client copy.
    local runner="${1:-$RUNNER_DIR/run-client.sh}" pidfile="${2:-$PID_DIR/client.pid}"
    local client_log="${3:-$CLIENT_LOG}" client_label="${4:-primary}"
    local ca_win
    ca_win="$(windows_z_path "$CA_CERT")"

    cat > "$runner" <<EOF
#!/usr/bin/env bash
set -o pipefail
echo \$\$ > $(printf '%q' "$pidfile")
trap 'rm -f $(printf '%q' "$pidfile")' EXIT
export STEAM_COMPAT_DATA_PATH=$(printf '%q' "$PROTON_PREFIX")
export STEAM_COMPAT_CLIENT_INSTALL_PATH=$(printf '%q' "$STEAM_ROOT")
export STEAM_COMPAT_APP_ID=0
export SteamAppId=0
export SteamGameId=0

export EVEJS_PROXY_LOCAL_INTERCEPT=1
export EVEJS_PROXY_UNHANDLED_HOST_POLICY=block
export EVEJS_PROXY_BLOCKED_HOSTS="api.ipify.org,sentry.io,.sentry.io,google-analytics.com,.google-analytics.com,launchdarkly.com,.launchdarkly.com,clientstream.launchdarkly.com,events.launchdarkly.com,mobile.launchdarkly.com,app.launchdarkly.com,sdk.launchdarkly.com,stream.launchdarkly.com,launchdarkly.us,.launchdarkly.us,launchdarkly.eu,.launchdarkly.eu"

export http_proxy=$(printf '%q' "$PROXY_URL")
export https_proxy=$(printf '%q' "$PROXY_URL")
export HTTP_PROXY=$(printf '%q' "$PROXY_URL")
export HTTPS_PROXY=$(printf '%q' "$PROXY_URL")
export all_proxy=$(printf '%q' "$PROXY_URL")
export ALL_PROXY=$(printf '%q' "$PROXY_URL")
export no_proxy="127.0.0.1,localhost,::1"
export NO_PROXY="\$no_proxy"

export EVE_CLIENT_SENTRY_DSN="http://evejs@127.0.0.1:26002/1"
export EVEJS_RESOURCE_SERVER="http://127.0.0.1:26002/resfiles/"
export SSL_CERT_FILE=$(printf '%q' "$ca_win")
export REQUESTS_CA_BUNDLE=$(printf '%q' "$ca_win")
export CURL_CA_BUNDLE=$(printf '%q' "$ca_win")
unset SSL_CERT_DIR

export LD_OFFLINE=true
export LAUNCHDARKLY_OFFLINE=true
export LAUNCHDARKLY_SEND_EVENTS=false
export LD_SEND_EVENTS=false
export EO_REMOTEFILECACHEFOLDER=$(printf '%q' "$RESFILES")

cd $(printf '%q' "$TQ_PATH") || exit 1
exec > >(tee -a $(printf '%q' "$client_log")) 2>&1

echo '══════════════════════════════════════════'
echo '  EVE client via Steam Proton'
echo '  Client:  $client_label'
echo '  EveJS:   $EVEJS_VERSION'
echo '  Build:   $SUPPORTED_BUILD'
echo '  Proton:  $(basename "$PROTON_DIR")'
echo '  tq:      $TQ_PATH'
echo '  ResFiles:$RESFILES'
echo '  CA path: $ca_win'
echo '══════════════════════════════════════════'

exec $(printf '%q' "$PROTON_BIN") run $(printf '%q' "$TQ_PATH/bin64/exefile.exe") "/port:$GAME_PORT"
EOF
    chmod +x "$runner"
    printf '%s\n' "$runner"
}

client_backend_ready() {
    curl -sf --max-time 2 "$MARKET_HTTP/health" >/dev/null 2>&1 \
        && port_exclusively_owned_by_tree 40110 "$MARKET_PID" \
        && port_exclusively_owned_by_tree "$MARKET_RPC_PORT" "$MARKET_PID" \
        || { echo "Market is not healthy/owned by the current market tree; client blocked."; return 1; }

    curl -sf --max-time 2 "$PROXY_URL/health" >/dev/null 2>&1 \
        && port_exclusively_owned_by_tree 26002 "$SERVER_PID" \
        || { echo "Gateway is not healthy/owned by the current game tree; client blocked."; return 1; }

    port_exclusively_owned_by_tree "$CDN_PORT" "$SERVER_PID" \
        || { echo "HTTPS/CDN port $CDN_PORT is not owned by the current game tree; client blocked."; return 1; }
    port_exclusively_owned_by_tree "$GAME_PORT" "$SERVER_PID" \
        || { echo "Game listener is not owned by the current game tree; client blocked."; return 1; }
    port_exclusively_owned_by_tree "$IMAGE_EDGE_PORT" "$SERVER_PID" \
        || { echo "ImageEdge is not owned by the current game tree; client blocked."; return 1; }
    port_exclusively_owned_by_tree "$GATEWAY_TLS_PORT" "$SERVER_PID" \
        || { echo "Gateway TLS is not owned by the current game tree; client blocked."; return 1; }
    port_exclusively_owned_by_tree "$XMPP_PORT" "$SERVER_PID" \
        || { echo "XMPP is not owned by the current game tree; client blocked."; return 1; }
    port_exclusively_owned_by_tree "$REDSHIFT_EDGE_PORT" "$SERVER_PID" \
        || { echo "RedshiftEdge is not owned by the current game tree; client blocked."; return 1; }

    repair_client_ca_bundles || return 1
    verify_tls_endpoint "$XMPP_BIND_HOST" "$XMPP_PORT" localhost XMPP || return 1
    verify_tls_endpoint "$GATEWAY_TLS_HOST" "$GATEWAY_TLS_PORT" "$GATEWAY_TLS_SNI" gateway || return 1
    ensure_proton_prefix || return 1
}

launch_client() {
    if is_pid_running "$CLIENT_PID"; then return 0; fi
    client_backend_ready || return 1

    rotate_one_log "$CLIENT_LOG" client
    rm -f "$PID_DIR/client.pid"
    local runner
    runner="$(write_client_runner)"
    echo
    echo "[CLIENT] Backend and TLS checks passed — launching EVE..."
    open_terminal "EVE $SUPPORTED_BUILD — EveJS $EVEJS_VERSION" "$runner"
    CLIENT_PID="$(wait_for_pidfile "$PID_DIR/client.pid")" || {
        echo "  ! Client runner PID was not observed. Check: $CLIENT_LOG"
        return 1
    }
    echo "  ✓ EVE client started through $(basename "$PROTON_DIR")"
}

# ── Additional clients ────────────────────────────────────────────────────────
# Each additional client gets its own slot number (2, 3, ...), runner, PID file
# and log. There is no upper limit; the lowest free slot is reused.
extra_client_pidfiles() {
    local f
    shopt -s nullglob
    for f in "$PID_DIR"/client-*.pid; do printf '%s\n' "$f"; done
    shopt -u nullglob
}

extra_client_count() {
    local f count=0
    while read -r f; do
        [[ -n "$f" ]] || continue
        is_pid_running "$(read_pidfile "$f")" && count=$((count + 1))
    done < <(extra_client_pidfiles)
    printf '%s\n' "$count"
}

next_extra_client_slot() {
    local slot=2
    while is_pid_running "$(read_pidfile "$PID_DIR/client-$slot.pid")"; do
        slot=$((slot + 1))
    done
    printf '%s\n' "$slot"
}

launch_extra_client() {
    client_backend_ready || return 1

    local slot pidfile log runner pid
    slot="$(next_extra_client_slot)"
    pidfile="$PID_DIR/client-$slot.pid"
    log="$LOG_DIR/client-$slot-latest.log"

    rotate_one_log "$log" "client-$slot"
    rm -f "$pidfile"
    runner="$(write_client_runner "$RUNNER_DIR/run-client-$slot.sh" "$pidfile" "$log" "additional #$slot")"
    echo
    echo "[CLIENT $slot] Backend and TLS checks passed — launching additional EVE client..."
    open_terminal "EVE $SUPPORTED_BUILD — EveJS $EVEJS_VERSION — Client $slot" "$runner"
    pid="$(wait_for_pidfile "$pidfile")" || {
        echo "  ! Client $slot runner PID was not observed. Check: $log"
        return 1
    }
    echo "  ✓ EVE client $slot started through $(basename "$PROTON_DIR") (PID $pid)"
}

stop_extra_clients() {
    local f pid slot
    local -a pidfiles=()
    mapfile -t pidfiles < <(extra_client_pidfiles)
    for f in "${pidfiles[@]}"; do
        pid="$(read_pidfile "$f")"
        slot="$(basename "$f" .pid)"
        slot="${slot#client-}"
        is_pid_running "$pid" && stop_pid "$pid" "EVE client $slot" 10
        rm -f "$f"
    done
}

start_all() {
    refresh_runtime_compatibility || return 1
    reconcile_required_ports || return 1
    ensure_loopback_cdn_permission || return 1
    repair_client_ca_bundles || return 1
    launch_market_server || return 1
    launch_game_server || return 1
    launch_client || return 1
}

restart_game_and_client() {
    stop_extra_clients
    stop_pid "$CLIENT_PID" "EVE client" 10; CLIENT_PID=""
    stop_pid "$SERVER_PID" "game server" 30; SERVER_PID=""
    sleep 1
    reconcile_required_ports "$MARKET_PID" && ensure_loopback_cdn_permission && launch_game_server && launch_client
}

restart_all() {
    stop_extra_clients
    stop_pid "$CLIENT_PID" "EVE client" 10; CLIENT_PID=""
    stop_pid "$SERVER_PID" "game server" 30; SERVER_PID=""
    stop_pid "$MARKET_PID" "market server" 10; MARKET_PID=""
    sleep 1
    start_all
}

echo
echo "╔════════════════════════════════════════════════════════════╗"
printf "║  %-58s║\n" "EveJS $EVEJS_VERSION Linux Launcher - v1.16 Stock Only"
echo "╚════════════════════════════════════════════════════════════╝"
echo " EveJS:   v$EVEJS_VERSION"
echo " Repo:    $EVEJS_REPO"
echo " EVE tq:  $TQ_PATH"
echo " Proton:  $(basename "$PROTON_DIR")"
echo " Market:  ${MARKET_LABEL:-${MARKET_PROFILE:-configured standalone market}}"
echo " Platform:$PLATFORM_MODE"
echo " CDN:     127.0.0.1:$CDN_PORT"
echo " Terminal:${TERMINAL_KIND}"
echo " Mods:    $MODS_DIR"
echo

detect_mods
if refresh_runtime_compatibility; then
    show_mods
    echo "  [RUNTIME] $EVEJS_RUNTIME_LABEL"
    echo "  [MARKET]  verified external binary"
else
    show_mods
    echo "  [RUNTIME] compatibility preparation required"
fi
if ! start_all; then
    echo
    echo "[STARTUP STOPPED] One required stage failed. Inspect the service windows/logs."
fi

while true; do
    detect_mods
    MARKET_PID="$(read_pidfile "$PID_DIR/market.pid")"
    SERVER_PID="$(read_pidfile "$PID_DIR/server.pid")"
    CLIENT_PID="$(read_pidfile "$PID_DIR/client.pid")"
    echo
    echo "Market: $(status_line "$MARKET_PID")"
    echo "Game:   $(status_line "$SERVER_PID")"
    if is_pid_running "$SERVER_PID" && port_exclusively_owned_by_tree "$XMPP_PORT" "$SERVER_PID"; then
        echo -e "Chat:   \033[0;32m● OWNED\033[0m (127.0.0.1:$XMPP_PORT)"
    elif port_listening "$XMPP_PORT"; then
        echo -e "Chat:   \033[1;31m● FOREIGN/STALE\033[0m (127.0.0.1:$XMPP_PORT)"
    else
        echo -e "Chat:   \033[0;31m○ NOT LISTENING\033[0m"
    fi

    if is_pid_running "$SERVER_PID" && port_exclusively_owned_by_tree "$IMAGE_EDGE_PORT" "$SERVER_PID"; then
        echo -e "Image:  \033[0;32m● OWNED\033[0m (127.0.0.1:$IMAGE_EDGE_PORT)"
    elif port_listening "$IMAGE_EDGE_PORT"; then
        echo -e "Image:  \033[1;31m● FOREIGN/STALE\033[0m (127.0.0.1:$IMAGE_EDGE_PORT)"
    else
        echo -e "Image:  \033[0;31m○ NOT LISTENING\033[0m"
    fi

    if is_pid_running "$SERVER_PID" && port_exclusively_owned_by_tree "$REDSHIFT_EDGE_PORT" "$SERVER_PID"; then
        echo -e "Redshift: \033[0;32m● OWNED\033[0m (127.0.0.1:$REDSHIFT_EDGE_PORT)"
    elif port_listening "$REDSHIFT_EDGE_PORT"; then
        echo -e "Redshift: \033[1;31m● FOREIGN/STALE\033[0m (127.0.0.1:$REDSHIFT_EDGE_PORT)"
    else
        echo -e "Redshift: \033[0;31m○ NOT LISTENING\033[0m"
    fi
    echo "Client: $(status_line "$CLIENT_PID")"
    echo "Extra:  $(extra_client_count) additional client(s) running"
    echo "Mods:   $MOD_COUNT"
    echo "Mode:   ${EVEJS_RUNTIME_LABEL:-unverified}"
    echo
    echo "[1] Start/stop Market"
    echo "[2] Start/stop Game"
    echo "[3] Start/stop EVE client"
    echo "[N] Launch another EVE client (no limit)"
    echo "[X] Close all additional EVE clients"
    echo "[R] Restart Game + client"
    echo "[M] Reload mods (restart Game + client)"
    echo "[A] Full ordered reload (Market → Game → Client)"
    echo "[S] Show mods"
    echo "[D] Diagnose required ports"
    echo "[P] Show paths"
    echo "[Q] Quit"
    read -r -p "Choose [1/2/3/N/X/R/M/A/S/D/P/Q]: " choice

    case "${choice,,}" in
        1)
            if is_pid_running "$MARKET_PID"; then
                if is_pid_running "$SERVER_PID"; then
                    echo "Stop the game server before stopping its market dependency."
                else
                    stop_pid "$MARKET_PID" "market server" 10; MARKET_PID=""
                fi
            else
                launch_market_server
            fi
            ;;
        2)
            if is_pid_running "$SERVER_PID"; then
                stop_extra_clients
                stop_pid "$CLIENT_PID" "EVE client" 10; CLIENT_PID=""
                stop_pid "$SERVER_PID" "game server" 30; SERVER_PID=""
            else
                reconcile_required_ports "$MARKET_PID" && ensure_loopback_cdn_permission && launch_game_server
            fi
            ;;
        3)
            if is_pid_running "$CLIENT_PID"; then
                stop_pid "$CLIENT_PID" "EVE client" 10; CLIENT_PID=""
            else
                launch_client
            fi
            ;;
        n) launch_extra_client ;;
        x)
            if (( $(extra_client_count) > 0 )); then
                stop_extra_clients
            else
                echo "No additional EVE clients are running."
            fi
            ;;
        r) restart_game_and_client ;;
        m)
            echo "Re-scanning mods/*/loader.js..."
            detect_mods
            show_mods
            restart_game_and_client
            ;;
        a) restart_all ;;
        s) show_mods ;;
        d)
            echo
            echo "Ownership-aware EveJS listener diagnostic:"
            echo "  Expected market root PID: ${MARKET_PID:-none}"
            echo "  Expected game root PID:   ${SERVER_PID:-none}"
            echo
            port_owner_report 40110 "$MARKET_PID" "Market HTTP"
            port_owner_report 40111 "$MARKET_PID" "Market RPC"
            port_owner_report "$CDN_PORT" "$SERVER_PID" "HTTPS/CDN"
            port_owner_report 5222 "$SERVER_PID" "XMPP"
            port_owner_report 26000 "$SERVER_PID" "Game"
            port_owner_report 26001 "$SERVER_PID" "ImageEdge"
            port_owner_report 26002 "$SERVER_PID" "Gateway HTTP"
            port_owner_report 26003 "$SERVER_PID" "Gateway TLS"
            port_owner_report 26400 "$SERVER_PID" "RedshiftEdge"
            echo
            ;;
        p)
            echo
            echo "EveJS repo:    $EVEJS_REPO"
            echo "EVE tq:        $TQ_PATH"
            echo "ResFiles:      $RESFILES"
            echo "Steam root:    $STEAM_ROOT"
            echo "Proton:        $PROTON_DIR"
            echo "Proton prefix: $PROTON_PREFIX"
            echo "Market:        ${MARKET_LABEL:-${MARKET_PROFILE:-configured standalone market}}"
            echo "Platform:      $PLATFORM_MODE"
            echo "HTTPS/CDN:     127.0.0.1:$CDN_PORT"
            echo "Runtime mode:  ${EVEJS_RUNTIME_LABEL:-unverified}"
            echo "Market binary: ${MARKET_BIN:-unverified}"
            echo "Market SHA256: ${EVEJS_MARKET_RUNTIME_SHA256:-unverified}"
            echo "Source digest: ${EVEJS_MARKET_SOURCE_DIGEST:-unverified}"
            echo "Runtime cache: ${EVEJS_RUNTIME_ROOT:-unverified}"
            if [[ -r /etc/os-release ]]; then
                . /etc/os-release
                echo "Host OS:       ${PRETTY_NAME:-${NAME:-Linux}}"
            fi
            echo "Mods:          $MODS_DIR"
            echo "Market log:    $MARKET_LOG"
            echo "Server log:    $SERVER_LOG"
            echo "Client log:    $CLIENT_LOG"
            echo "Extra logs:    $LOG_DIR/client-<N>-latest.log"
            echo
            ;;
        q) exit 0 ;;
        *) echo "Invalid choice." ;;
    esac
done
