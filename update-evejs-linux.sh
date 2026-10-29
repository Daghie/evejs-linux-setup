#!/usr/bin/env bash
# ==============================================================================
# EveJS updater for Linux — installed as tools/update-evejs-linux.sh
#
#   tools/update-evejs-linux.sh update ~/Downloads/EveJS-<version>.zip [--preview]
#   tools/update-evejs-linux.sh list
#   tools/update-evejs-linux.sh restore [backup-name] [--program-only|--data-only]
#   tools/update-evejs-linux.sh prune --keep 3
#   tools/update-evejs-linux.sh reseed-market v1|v2|v3
#
# The Linux twin of EveJS's UpdateEveJS.bat. It follows the release's own
# tools/Updater/update-policy.json, keeps your world, Market, settings and
# certificates in place, moves the old program files to _backup/, and also keeps a
# verified copy of your data there so `restore` can undo an update completely.
# Stop the launcher session (server, Market and clients) before running it.
# ==============================================================================
set -euo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# Installed as <EveJS>/tools/update-evejs-linux.sh; also works beside the engine.
if [[ -f "$SELF_DIR/evejs-linux-update.py" ]]; then
    ENGINE="$SELF_DIR/evejs-linux-update.py"
    REPO_DEFAULT="$(pwd -P)"
else
    REPO_DEFAULT="$(dirname "$SELF_DIR")"
    ENGINE="$REPO_DEFAULT/.evejs-linux-tools/evejs-linux-update.py"
fi
[[ -f "$ENGINE" ]] || {
    echo "[ERROR] The updater engine was not found: $ENGINE" >&2
    echo "        Run the EveJS Universal Linux Setup and choose 'Prepare/verify runtime compatibility'." >&2
    exit 1
}
command -v python3 >/dev/null 2>&1 || { echo "[ERROR] python3 is required." >&2; exit 1; }

case " $* " in
    *" --repo "*|*" --repo="*) exec python3 "$ENGINE" "$@" ;;
    *) exec python3 "$ENGINE" --repo "$REPO_DEFAULT" "$@" ;;
esac
