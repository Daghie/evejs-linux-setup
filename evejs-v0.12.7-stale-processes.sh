#!/usr/bin/env bash
# Recovery helper for EveJS v0.12.7 Universal Linux Setup v1.10.
# It never kills an unidentified process.

set -Eeuo pipefail
ports=(443 5222 26000 26001 26002 26003 26400 40110 40111)

port_pids() {
    local port="$1"
    ss -H -ltnp 2>/dev/null \
      | awk -v needle=":${port}" '$4 ~ (needle "$") {print}' \
      | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -nu
}
pid_cwd() { readlink -f "/proc/$1/cwd" 2>/dev/null || true; }
pid_cmdline() { tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null || true; }
pid_ppid() { awk '/^PPid:/ {print $2; exit}' "/proc/$1/status" 2>/dev/null || true; }

evejs_repo_for_pid() {
    local pid="$1" cwd parent cmd env repo_guess cwd_clean
    cwd="$(pid_cwd "$pid")"
    cmd="$(pid_cmdline "$pid")"
    cwd_clean="${cwd% (deleted)}"

    if [[ -n "$cwd_clean" && "$(basename "$cwd_clean")" == "server" ]]; then
        parent="$(dirname "$cwd_clean")"
        if [[ -f "$parent/config/version.json" && -f "$parent/server/index.js" ]]; then
            printf '%s\n' "$parent"; return 0
        fi
    fi

    if [[ "$cwd_clean" == */externalservices/market-server ]]; then
        parent="$(dirname "$(dirname "$cwd_clean")")"
        if [[ -f "$parent/config/version.json" && -f "$parent/server/index.js" ]]; then
            printf '%s\n' "$parent"; return 0
        fi
    fi

    if [[ "$cmd" =~ (.*)/server/src/edge/(chat|gateway|game|image|redshift)/[^[:space:]]*\.worker\.js ]]; then
        repo_guess="${BASH_REMATCH[1]}"
        printf '%s\n' "${repo_guess% (deleted)}"
        return 0
    fi

    if [[ "$cmd" == *node* && -r "/proc/$pid/environ" ]]; then
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
    local pid="$1" repo current parent parent_repo best hops=0
    repo="$(evejs_repo_for_pid "$pid" 2>/dev/null || true)"
    [[ -n "$repo" ]] || return 1
    current="$pid"; best="$pid"
    while (( hops < 128 )); do
        parent="$(pid_ppid "$current")"
        [[ "$parent" =~ ^[0-9]+$ ]] || break
        (( parent <= 1 )) && break
        parent_repo="$(evejs_repo_for_pid "$parent" 2>/dev/null || true)"
        [[ -n "$parent_repo" && "$parent_repo" == "$repo" ]] || break
        best="$parent"; current="$parent"; hops=$((hops + 1))
    done
    printf '%s\n' "$best"
}

collect_descendants() {
    local parent="$1" child
    while read -r child; do
        [[ -n "$child" ]] || continue
        collect_descendants "$child"
        printf '%s\n' "$child"
    done < <(pgrep -P "$parent" 2>/dev/null || true)
}

stop_tree() {
    local root="$1" label="$2"
    kill -TERM "$root" 2>/dev/null || true
    for _ in $(seq 1 300); do
        kill -0 "$root" 2>/dev/null || { echo "Stopped $label gracefully."; return 0; }
        sleep 0.1
    done

    echo "$label did not stop within 30s; terminating surviving tree."
    mapfile -t descendants < <(collect_descendants "$root" | awk 'NF' | sort -rn)
    local pid
    for pid in "${descendants[@]}"; do kill -TERM "$pid" 2>/dev/null || true; done
    kill -TERM "$root" 2>/dev/null || true
    sleep 2
    for pid in "${descendants[@]}"; do kill -KILL "$pid" 2>/dev/null || true; done
    kill -KILL "$root" 2>/dev/null || true
}

wait_exit() {
    local pid="$1"
    for _ in $(seq 1 80); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.1
    done
    return 1
}

declare -A seen=()
declare -A seen_root=()
evejs=()
unknown=()

echo "Relevant listeners before cleanup:"
ss -H -ltnp 2>/dev/null | grep -E ':(443|5222|26000|26001|26002|26003|26400|40110|40111)([[:space:]]|$)' || echo "  none"
echo

for port in "${ports[@]}"; do
    while read -r pid; do
        [[ -n "$pid" ]] || continue
        [[ -n "${seen[$pid]:-}" ]] && continue
        seen[$pid]=1

        if repo="$(evejs_repo_for_pid "$pid" 2>/dev/null)"; then
            root="$(evejs_tree_root_for_pid "$pid" 2>/dev/null || true)"
            [[ "$root" =~ ^[0-9]+$ ]] || root="$pid"
            if [[ -z "${seen_root[$root]:-}" ]]; then
                seen_root[$root]=1
                evejs+=("$root")
                echo "EveJS process tree root: PID $root"
                echo "  repo:    $repo"
                echo "  cwd:     $(pid_cwd "$root")"
                echo "  command: $(pid_cmdline "$root")"
                echo
            fi
        else
            unknown+=("$pid")
            echo "UNKNOWN process: PID $pid"
            echo "  cwd:     $(pid_cwd "$pid")"
            echo "  command: $(pid_cmdline "$pid")"
            echo
        fi
    done < <(port_pids "$port")
done

if ((${#unknown[@]} > 0)); then
    echo "[STOP] At least one required port belongs to a process that is not safely identifiable as EveJS."
    echo "Nothing was killed. Close that application manually, or inspect it with:"
    echo "  sudo ss -ltnp | grep -E ':(443|5222|26000|26001|26002|26003|26400|40110|40111)\\b'"
    exit 1
fi

if ((${#evejs[@]} == 0)); then
    echo "No stale EveJS listeners were found."
    exit 0
fi

read -r -p "Stop the EveJS process(es) listed above? [Y/n]: " ans
case "${ans,,}" in n|no) exit 0 ;; esac

for pid in "${evejs[@]}"; do
    repo="$(evejs_repo_for_pid "$pid" 2>/dev/null || true)"
    stop_tree "$pid" "EveJS process tree PID $pid${repo:+ from $(basename "$repo")}"
done

sleep 1
echo
echo "Relevant listeners after cleanup:"
ss -H -ltnp 2>/dev/null | grep -E ':(443|5222|26000|26001|26002|26003|26400|40110|40111)([[:space:]]|$)' || echo "  none"
