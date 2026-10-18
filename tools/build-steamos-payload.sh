#!/usr/bin/env bash
# ==============================================================================
# Build the SteamOS portable payload for one EveJS release.
#
#   tools/build-steamos-payload.sh <EveJS-release.zip> [output-dir]
#
# Output (default: portable/steamos-x86_64 beside this repository's installer):
#   payload.tar.xz   root + server node_modules and four static-PIE musl programs
#   manifest.json    sizes, SHA-256s, and what each part is bound to
#
# The payload is bound to the parts of the release it was built from, not to the
# release as a whole, so it keeps working across EveJS updates that leave them
# alone:
#   * node_modules     -> dependency fingerprint of each package-lock.json
#                         (the project's own version field is ignored, exactly
#                         as the EveJS updater does)
#   * each Rust program -> source digest of its crate (Cargo.toml/lock, build.rs,
#                         every .rs file; the same digest the launcher verifies)
#
# Needs podman (or docker) with the images below; nothing is installed on the
# host and the host toolchain is never used.
# ==============================================================================
set -euo pipefail

RUST_IMAGE="${RUST_IMAGE:-docker.io/library/rust:1.97-alpine}"
NODE_IMAGE="${NODE_IMAGE:-docker.io/library/node:24-bookworm-slim}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SETUP_ROOT="$(dirname "$SCRIPT_DIR")"

ZIP="${1:-}"
OUT="${2:-$SETUP_ROOT/portable/steamos-x86_64}"
[[ -f "$ZIP" ]] || { echo "usage: $0 <EveJS-release.zip> [output-dir]" >&2; exit 2; }
ZIP="$(readlink -f "$ZIP")"
mkdir -p "$OUT"
OUT="$(readlink -f "$OUT")"

ENGINE="$(command -v podman || command -v docker || true)"
[[ -n "$ENGINE" ]] || { echo "podman or docker is required." >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/evejs-steamos-payload.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

echo "[1/5] Unpacking $(basename "$ZIP")"
mkdir "$WORK/src"
unzip -q "$ZIP" -d "$WORK/src"
SRC="$WORK/src"
# Releases are zipped either flat or with one version folder at the top.
if [[ ! -f "$SRC/server/index.js" ]]; then
    mapfile -t tops < <(find "$SRC" -mindepth 1 -maxdepth 1)
    (( ${#tops[@]} == 1 )) && [[ -f "${tops[0]}/server/index.js" ]] && SRC="${tops[0]}"
fi
[[ -f "$SRC/server/index.js" && -f "$SRC/config/version.json" ]] || { echo "Not an EveJS release zip." >&2; exit 1; }
VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["evejsVersion"])' "$SRC/config/version.json")"
BUILD="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["supportedBuild"])' "$SRC/tools/ClientSETUP/blue_patch_recipe.json")"
echo "      EveJS $VERSION, EVE client build $BUILD"

CRATES=(externalservices/market-server tools/market-seed tools/market-seederv2 tools/market-seederv3)

echo "[2/5] Building static musl programs in $RUST_IMAGE"
"$ENGINE" run --rm --memory=6g --cpus=4 \
    -v "$SRC:/src:Z" -w /src "$RUST_IMAGE" sh -euc '
        apk add --no-cache musl-dev >/dev/null
        for crate in '"${CRATES[*]}"'; do
            echo "      cargo build --release --locked ($crate)"
            (cd "$crate" && CARGO_HOME=/tmp/cargo cargo build --release --locked --quiet)
        done
        rustc --version > /src/.rustc-version'

echo "[3/5] Installing Node packages in $NODE_IMAGE (official prebuilds only)"
"$ENGINE" run --rm --memory=4g --cpus=4 \
    -e npm_config_build_from_source=false -e npm_config_update_notifier=false \
    -v "$SRC:/src:Z" -w /src "$NODE_IMAGE" sh -euc '
        npm ci --no-audit --no-fund --loglevel=error
        (cd server && npm ci --no-audit --no-fund --loglevel=error)
        node --version > /src/.node-version; npm --version > /src/.npm-version
        cd server && node -e "const D=require(\"better-sqlite3\");const d=new D(\":memory:\");d.exec(\"create table t(x)\");d.close();require(\"protobufjs\");console.log(\"      Node dependency smoke test PASS\")"'

echo "[4/5] Packing payload"
LIST="$WORK/members.txt"
{
    echo node_modules
    echo server/node_modules
    for crate in "${CRATES[@]}"; do echo "$crate/target/release/$(basename "$crate")"; done
} > "$LIST"
tar -C "$SRC" --sort=name --owner=0 --group=0 --numeric-owner --mtime='2026-01-01 00:00Z' \
    -cf - -T "$LIST" | xz -T0 -9 > "$WORK/payload.tar.xz"

echo "[5/5] Writing manifest"
python3 - "$SRC" "$WORK/payload.tar.xz" "$WORK/manifest.json" "$ZIP" "$VERSION" "$BUILD" "$RUST_IMAGE" "$NODE_IMAGE" "$SETUP_ROOT/evejs-linux-lu-compat.py" "${CRATES[@]}" <<'PY'
import hashlib, importlib.util, json, pathlib, sys
src, payload, out, zip_path, version, build, rust_image, node_image, compat_path, *crates = sys.argv[1:]
src = pathlib.Path(src)
spec = importlib.util.spec_from_file_location("compat", compat_path)
compat = importlib.util.module_from_spec(spec); spec.loader.exec_module(compat)

def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

def text(name):
    return (src / name).read_text().strip()

artifacts = []
for crate in crates:
    binary = src / crate / "target/release" / pathlib.Path(crate).name
    records = compat.build_input_records(src / crate)
    artifacts.append({
        "path": binary.relative_to(src).as_posix(), "size": binary.stat().st_size, "sha256": sha(binary),
        "linkage": "static-pie-musl", "crate": crate, "sourceDigest": compat.build_source_digest(records),
    })
sqlite = src / "server/node_modules/better-sqlite3/build/Release/better_sqlite3.node"
artifacts.append({"path": sqlite.relative_to(src).as_posix(), "size": sqlite.stat().st_size, "sha256": sha(sqlite),
                  "linkage": "official-linux-x64-prebuild"})

manifest = {
    "schema": "evejs-steamos-portable-payload/2",
    "builtFromEvejsVersion": version,
    "eveBuild": int(build),
    "platform": "linux-x86_64",
    "sourceArchiveSha256": sha(zip_path),
    "payload": {"file": "payload.tar.xz", "size": pathlib.Path(payload).stat().st_size, "sha256": sha(payload)},
    "buildEnvironment": {"rustImage": rust_image, "rustc": text(".rustc-version"), "nodeImage": node_image,
                         "node": text(".node-version"), "npm": text(".npm-version")},
    "dependencyFingerprints": {name: compat.dependency_fingerprint(src / name)
                               for name in ("package-lock.json", "server/package-lock.json")},
    "artifacts": artifacts,
}
pathlib.Path(out).write_text(json.dumps(manifest, indent=2) + "\n")
PY
mv -f "$WORK/payload.tar.xz" "$OUT/payload.tar.xz"
mv -f "$WORK/manifest.json" "$OUT/manifest.json"
echo "Done: $OUT/payload.tar.xz ($(du -h "$OUT/payload.tar.xz" | cut -f1)) and manifest.json"
