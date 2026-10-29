#!/usr/bin/env python3
"""EveJS updater for Linux.

A Linux twin of EveJS's own Windows updater (tools/Updater/Update-EvEJS.ps1). It reads
the same tools/Updater/update-policy.json, so each EveJS release decides what an update
keeps and replaces, and it writes the same records:

  * old program files are moved, not deleted, to _backup/pre-update-<version>-<stamp>/
  * .evejs-installed-files.json records what the release installed, so the next update
    removes exactly what a new release drops and leaves the operator's own files alone

On top of that, because an update can break a world as well as program files:

  * before anything moves, the world database, Market database, settings, certificates
    and launcher config are COPIED into the same backup folder (_linux-data/), with
    every copy verified byte for byte;
  * `restore` puts back the program files, the data, or both, and first moves whatever
    it replaces into a pre-restore backup, so a restore can be undone too;
  * after the copy it reinstalls Node packages when the release changed them, runs the
    release's one-time maintenance step, records the official release for the launcher,
    and prepares a Market server program that matches the new Market source.

Commands (see --help):
  update <release.zip>   install a release over this folder (--preview touches nothing)
  restore [backup]       put a backup back (default: the newest)
  list                   show backups
  prune --keep N         delete all but the newest N backups
  reseed-market [v1|v2|v3]  rebuild the Market database with an install-time seed profile
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import zipfile

SETUP_VERSION = "v1.18"
STAMP_FORMAT = "%Y%m%d-%H%M%S"
TOOLS_DIR = Path(__file__).absolute().parent
POLICY_REL = "tools/Updater/update-policy.json"
LINUX_DATA = "_linux-data"
AFTER_LIST = "installed-files-after.json"

# Files the Linux setup adds to an installation. A release never ships them, and the
# folder rule of a first update would otherwise sweep tools/update-evejs-linux.sh away.
LINUX_KEEP_TREES = [".evejs-linux-tools", "mods"]
LINUX_KEEP_FILES = [
    ".evejs-linux.conf", "evejs-linux-launcher.sh", "evejs-stale-process-cleanup.sh",
    "tools/update-evejs-linux.sh", "INSTALL-LINUX-SUMMARY.txt", "LINUX-HOST-TOOLCHAIN.txt",
    "STEAMOS-MINIMAL-REPAIR.txt",
]
# Used only when neither the installation nor the release carries a policy file.
FALLBACK_POLICY = {
    "installMarkers": ["server/index.js"],
    "releaseMarkers": ["server/index.js", "config/version.json", "package.json"],
    "refuseWhenPresent": [".git"],
    "keepTrees": ["_local", "_backup", "logs", "node_modules", "server/node_modules", "server/logs",
                  "externalservices/market-server/data", "externalservices/market-server/target",
                  "tools/market-seed/target", "tools/market-seederv2/target", "tools/market-seederv3/target",
                  "tools/market-seederv2/cache", "tools/market-seederv3/cache"],
    "keepFiles": [".env", ".env.local", "evejs.config.json", "evejs.config.local.json",
                  ".evejs-installed-files.json"],
    "keepGlobs": ["**/*.sqlite", "**/*.sqlite-wal", "**/*.sqlite-shm"],
    "keepMine": ["config/server.json", "config/gameplay.json", "config/world.json",
                 "config/mining.json", "config/npc.json", "config/economy.json"],
    "sourceTreeStoreGuards": ["server/src/gameStore/gamestore.sqlite"],
    "installedFilesManifest": ".evejs-installed-files.json",
    "dependencyTriggers": ["server/package.json", "server/package-lock.json", "package.json", "package-lock.json"],
    "backupRoot": "_backup",
}
# What a data snapshot copies (relative to the installation). Trees skip the listed
# large regenerable folders; missing entries are simply absent from the snapshot.
DATA_TREES = {
    "_local/gameStore": ["data"],          # world, portraits, installed packs; not the static tables
    "_local/certs": [],
    "_local/evejs-linux-setup": [],
    "externalservices/market-server/data/generated": [],
    "server/src/_secondary/data/chat": [],
}
DATA_GLOBS = ["config/*.json", "evejs.config*.json", ".env", ".env.local", ".evejs-linux.conf",
              ".evejs-installed-files.json", "tools/ClientSETUP/scripts/EvEJSConfig.bat"]
LAUNCHER_PORTS = [5222, 26001, 26003, 26400, 40110, 40111]
# Identifies the installed program, so it follows the program files, not the data.
RELEASE_RECEIPT = "_local/evejs-linux-setup/release.json"


class UpdateError(RuntimeError):
    pass


# ── output ────────────────────────────────────────────────────────────────────
LOG_LINES: list[str] = []


def say(message: str = "", kind: str = "") -> None:
    prefix = {"step": "==> ", "warn": " !  ", "error": "[ERROR] "}.get(kind, "    " if message else "")
    line = prefix + message
    LOG_LINES.append(line)
    print(line, file=sys.stderr if kind == "error" else sys.stdout, flush=True)


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        say(f"{question} -> no (not interactive; pass --yes)", "warn")
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


# ── helpers ───────────────────────────────────────────────────────────────────
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def comparable_version(text: str):
    """EveJS's ConvertTo-ComparableVersion: digits and dots only, four parts."""
    parts = [p for p in re.sub(r"[^0-9.]", "", text or "").split(".") if p]
    if not parts:
        return None
    parts = (parts + ["0"] * 4)[:4]
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def installed_version(root: Path) -> str:
    data = load_json(root / "config/version.json", {}) or {}
    if data.get("evejsVersion"):
        return str(data["evejsVersion"])
    package = load_json(root / "package.json", {}) or {}
    return str(package.get("version") or "unknown")


def load_compat():
    path = TOOLS_DIR / "evejs-linux-lu-compat.py"
    if not path.is_file():
        raise UpdateError(f"Runtime compatibility helper is missing: {path}")
    spec = importlib.util.spec_from_file_location("evejs_linux_compat", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_linux_conf(root: Path) -> dict:
    conf = root / ".evejs-linux.conf"
    if not conf.is_file():
        return {}
    out = subprocess.run(["bash", "-c", 'set -a; . "$1" >/dev/null 2>&1; env -0', "_", str(conf)],
                         capture_output=True, check=False)
    values = {}
    for item in out.stdout.split(b"\0"):
        if b"=" in item:
            key, _, value = item.partition(b"=")
            values[key.decode()] = value.decode(errors="replace")
    keys = ("EVEJS_REPO", "EVEJS_VERSION", "SUPPORTED_BUILD", "NODE_BIN", "NODE_BIN_DIR", "CARGO_BIN",
            "EVEJS_PLATFORM_MODE", "EVEJS_CDN_PORT", "MARKET_PROFILE", "MARKET_LABEL", "TQ_PATH")
    return {k: values[k] for k in keys if k in values}


def set_linux_conf(root: Path, updates: dict) -> None:
    conf = root / ".evejs-linux.conf"
    if not conf.is_file():
        return
    lines = [l for l in conf.read_text(encoding="utf-8").splitlines()
             if not any(l.startswith(k + "=") for k in updates)]
    lines += [f"{k}={shlex.quote(str(v))}" for k, v in updates.items()]
    mode = conf.stat().st_mode
    temporary = conf.with_name(conf.name + ".new")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.chmod(mode)
    os.replace(temporary, conf)


def tool_path(conf: dict, name: str) -> str | None:
    if name == "node" and conf.get("NODE_BIN") and Path(conf["NODE_BIN"]).is_file():
        return conf["NODE_BIN"]
    if name == "cargo" and conf.get("CARGO_BIN") and Path(conf["CARGO_BIN"]).is_file():
        return conf["CARGO_BIN"]
    if name == "npm" and conf.get("NODE_BIN_DIR") and (Path(conf["NODE_BIN_DIR"]) / "npm").is_file():
        return str(Path(conf["NODE_BIN_DIR"]) / "npm")
    found = shutil.which(name)
    if not found and name == "cargo" and (Path.home() / ".cargo/bin/cargo").is_file():
        found = str(Path.home() / ".cargo/bin/cargo")
    return found


def tool_env(conf: dict) -> dict:
    env = os.environ.copy()
    extra = [d for d in (conf.get("NODE_BIN_DIR"), str(Path.home() / ".cargo/bin")) if d]
    env["PATH"] = ":".join(extra + [env.get("PATH", "")])
    return env


def run(cmd: list[str], cwd: Path, conf: dict) -> int:
    say("$ " + " ".join(shlex.quote(c) for c in cmd) + f"   (in {cwd})")
    return subprocess.run(cmd, cwd=cwd, env=tool_env(conf)).returncode


def is_steamos_portable(conf: dict) -> bool:
    return conf.get("EVEJS_PLATFORM_MODE") == "steamos-portable"


# ── policy ────────────────────────────────────────────────────────────────────
def key(rel: str) -> str:
    return rel.replace("\\", "/").strip("/").lower()


class Policy:
    """update-policy.json matchers, exactly as Update-EvEJS.ps1 builds them.

    Built from the union of the installed and the incoming release's policy (keeping
    more is always the safe direction), plus the Linux setup's own files."""

    def __init__(self, raws: list[dict]):
        raws = [r for r in raws if r] or [FALLBACK_POLICY]
        merged: dict = {}
        for raw in raws:
            for name, value in raw.items():
                if isinstance(value, list):
                    merged.setdefault(name, [])
                    merged[name] += [v for v in value if v not in merged[name]]
                elif name not in merged:
                    merged[name] = value
        merged["keepTrees"] = merged.get("keepTrees", []) + LINUX_KEEP_TREES
        merged["keepFiles"] = merged.get("keepFiles", []) + LINUX_KEEP_FILES
        self.raw = merged
        self.anchored = [key(t) for t in merged["keepTrees"] if "/" in key(t)]
        self.named = [key(t) for t in merged["keepTrees"] if "/" not in key(t)]
        self.globs = []
        for glob in merged.get("keepGlobs", []):
            pattern = re.escape(key(glob)).replace(r"\*\*/", "(?:.*/)?").replace(r"\*", "[^/]*")
            self.globs.append(re.compile("^" + pattern + "$"))
        self.keep_files = {key(f) for f in merged.get("keepFiles", [])}
        self.keep_mine = {key(f) for f in merged.get("keepMine", [])}
        self.manifest_name = merged.get("installedFilesManifest", ".evejs-installed-files.json")
        self.backup_root = merged.get("backupRoot", "_backup")

    def kept_dir(self, rel: str) -> bool:
        k = key(rel)
        if not k:
            return False
        if any(k == t or k.startswith(t + "/") for t in self.anchored):
            return True
        leaf = k.split("/")[-1]
        return any(leaf == n or f"/{n}/" in f"/{k}/" for n in self.named)

    def kept_file(self, rel: str) -> bool:
        k = key(rel)
        if k in self.keep_files:
            return True
        if "/" in k and self.kept_dir(k.rsplit("/", 1)[0]):
            return True
        return any(p.match(k) for p in self.globs)

    def keep_mine_file(self, rel: str) -> bool:
        return key(rel) in self.keep_mine


def program_files(root: Path, policy: Policy) -> list[str]:
    """Program files under root, pruning kept folders (never walks the world or node_modules)."""
    results = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except FileNotFoundError:
            continue
        for entry in entries:
            rel = Path(entry.path).relative_to(root).as_posix()
            if entry.is_dir(follow_symlinks=False):
                if not policy.kept_dir(rel):
                    stack.append(Path(entry.path))
            elif not policy.kept_file(rel):
                results.append(rel)
    return results


# ── release zip ───────────────────────────────────────────────────────────────
def read_release(zip_path: Path, policy: Policy) -> dict:
    if not zip_path.is_file():
        raise UpdateError(f"That zip file does not exist: {zip_path}")
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"Not a readable zip file: {zip_path}") from exc
    with archive:
        names = [i.filename.replace("\\", "/") for i in archive.infolist()]
        tops = {n.split("/")[0] for n in names}
        prefix = ""
        if len(tops) == 1:
            only = next(iter(tops))
            if any(n.startswith(only + "/") for n in names):
                prefix = only + "/"
        relative = [n[len(prefix):] for n in names if not n.endswith("/") and n.startswith(prefix)]
        lookup = {r.lower() for r in relative}
        for marker in policy.raw.get("releaseMarkers", FALLBACK_POLICY["releaseMarkers"]):
            if marker.lower() not in lookup:
                raise UpdateError(f"That zip is not an EveJS release: it has no {marker}.")
        for rel in relative:
            pure = PurePosixPath(rel)
            if pure.is_absolute() or ".." in pure.parts:
                raise UpdateError(f"Unsafe path in the release zip: {rel}")
        version = "unknown"
        try:
            version = str(json.loads(archive.read(prefix + "config/version.json")).get("evejsVersion") or "unknown")
        except Exception:
            pass
    return {"path": zip_path, "prefix": prefix, "version": version, "fileCount": len(relative)}


def expand_release(release: dict, destination: Path) -> int:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    count = 0
    with zipfile.ZipFile(release["path"]) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if name.endswith("/") or not name.startswith(release["prefix"]):
                continue
            rel = name[len(release["prefix"]):]
            target = destination / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise UpdateError(f"The release zip contains a symbolic link: {rel}")
            with archive.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            perms = (info.external_attr >> 16) & 0o777
            if perms:
                target.chmod(perms | 0o600)
            elif rel.endswith((".sh", ".py")):
                target.chmod(0o755)
            count += 1
    return count


# ── checks ────────────────────────────────────────────────────────────────────
def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def server_ports(root: Path, conf: dict) -> list[int]:
    ports = set(LAUNCHER_PORTS)
    cfg = load_json(root / "config/server.json", {}) or {}
    ports.add(int((cfg.get("network") or {}).get("serverPort", 26000)))
    ports.add(int((cfg.get("gateway") or {}).get("microservicesPort", 26002)))
    if conf.get("EVEJS_CDN_PORT", "").isdigit():
        ports.add(int(conf["EVEJS_CDN_PORT"]))
    return sorted(ports)


def assert_stopped(root: Path, conf: dict) -> None:
    busy = [p for p in server_ports(root, conf) if port_open(p)]
    pid_dir = root / "logs/launcher-linux/.pids"
    live = []
    if pid_dir.is_dir():
        for pid_file in pid_dir.glob("*.pid"):
            value = pid_file.read_text(errors="ignore").strip()
            if value.isdigit() and Path(f"/proc/{value}").exists():
                live.append(f"{pid_file.stem}={value}")
    if busy or live:
        raise UpdateError(
            "The EveJS stack is still running"
            + (f" (listening: {', '.join(map(str, busy))})" if busy else "")
            + (f" (launcher processes: {', '.join(live)})" if live else "")
            + ".\nQuit the EVE client and stop the launcher session, then try again. Nothing was changed."
        )


def assert_install_root(root: Path, policy: Policy) -> None:
    for marker in policy.raw.get("installMarkers", FALLBACK_POLICY["installMarkers"]):
        if marker.lower().endswith(".bat"):
            continue  # Windows launchers are optional on Linux
        if not (root / marker).exists():
            raise UpdateError(f"That folder is not an EveJS installation (no {marker}): {root}")
    for refused in policy.raw.get("refuseWhenPresent", [".git"]):
        if (root / refused).exists():
            raise UpdateError(f"{root} contains {refused}: it is a source checkout. Update it with Git instead.")
    stranded = [g for g in policy.raw.get("sourceTreeStoreGuards", []) if (root / g).exists()]
    if stranded:
        raise UpdateError(
            "This installation keeps its game database in the source tree rather than in _local:\n  "
            + "\n  ".join(stranded)
            + "\nMove it (and any -wal/-shm beside it) into _local/gameStore/ first. Nothing was changed."
        )


def assert_not_living_universe(root: Path) -> None:
    markers = ["_local/evejs-linux-integrated-lu/install.json", "_local/x-eve-patch/install.json",
               "_local/living-universe-direct-deploy", "evejs.config.x-eve.json"]
    found = [m for m in markers if (root / m).exists()]
    if found:
        raise UpdateError(
            "This folder carries Living Universe / X-Patch state:\n  " + "\n  ".join(found)
            + "\nAn EveJS release would overwrite that source. Update it with its Living Universe tools instead."
        )


# ── plan ──────────────────────────────────────────────────────────────────────
def plan_update(root: Path, staging: Path, policy: Policy, compat) -> dict:
    install_files = program_files(root, policy)
    staged_files = program_files(staging, policy)
    install_lookup = {f.lower(): f for f in install_files}
    staged_lookup = {f.lower(): f for f in staged_files}

    added, changed, kept_mine = [], [], []
    unchanged = 0
    for k, rel in staged_lookup.items():
        if policy.keep_mine_file(rel) and k in install_lookup:
            kept_mine.append(rel)
            continue
        if k not in install_lookup:
            added.append(rel)
            continue
        a, b = root / install_lookup[k], staging / rel
        if a.stat().st_size != b.stat().st_size or sha256(a) != sha256(b):
            changed.append(rel)
        else:
            unchanged += 1

    dependency_changed = any(
        compat.dependency_fingerprint(root / t) != compat.dependency_fingerprint(staging / t)
        for t in policy.raw.get("dependencyTriggers", [])
    )

    previous = load_json(root / policy.manifest_name)
    previous_set = {str(f).lower() for f in (previous or {}).get("files", [])} or None
    release_dirs = {k.rsplit("/", 1)[0] for k in staged_lookup if "/" in k}
    to_move, left_alone = [], []
    for rel in install_files:
        if policy.keep_mine_file(rel):
            continue
        k = rel.lower()
        belongs = k in staged_lookup
        if not belongs:
            directory = k.rsplit("/", 1)[0] if "/" in k else ""
            belongs = (k in previous_set) if previous_set is not None else (directory in release_dirs)
        (to_move if belongs else left_alone).append(rel)
    removed = [r for r in to_move if r.lower() not in staged_lookup]
    quarantine = sum((root / r).stat().st_size for r in to_move if (root / r).is_file())
    return {"added": added, "changed": changed, "removed": removed, "unchanged": unchanged,
            "keptMine": kept_mine, "toMove": to_move, "leftAlone": left_alone, "staged": staged_files,
            "hadManifest": previous_set is not None, "dependencyChanged": dependency_changed,
            "quarantineBytes": quarantine}


def print_plan(plan: dict, current: str, incoming: str) -> None:
    say("")
    say("What this update would do", "step")
    say(f"From version : {current}")
    say(f"To version   : {incoming}")
    say(f"New files    : {len(plan['added'])}")
    say(f"Changed      : {len(plan['changed'])}")
    say(f"Unchanged    : {plan['unchanged']}")
    say(f"No longer shipped, so removed : {len(plan['removed'])}")
    say(f"Your settings files kept as they are : {len(plan['keptMine'])}")
    say(f"Moved to the backup folder : {len(plan['toMove'])} file(s), {human(plan['quarantineBytes'])}")
    if plan["leftAlone"]:
        say(f"Not part of a release, so left where they are : {len(plan['leftAlone'])} file(s)")
        folders = sorted({"/".join(r.split("/")[:2]) for r in plan["leftAlone"]})[:8]
        for folder in folders:
            say(f"  kept: {folder}")
        if not plan["hadManifest"]:
            say("  (first update here, so this is judged by folder; the next one uses an exact list)")
    say("Node packages changed, so they will be reinstalled." if plan["dependencyChanged"]
        else "Node packages are unchanged and will be left alone.")
    for rel in plan["removed"][:20]:
        say(f"  removed: {rel}")
    if len(plan["removed"]) > 20:
        say(f"  ... and {len(plan['removed']) - 20} more")
    say("")


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


# ── moving and copying ────────────────────────────────────────────────────────
def move_files(src_root: Path, dst_root: Path, rels: list[str]) -> tuple[int, list[str]]:
    moved, failed = 0, []
    for rel in rels:
        src, dst = src_root / rel, dst_root / rel
        if not src.is_file() and not src.is_symlink():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            os.replace(src, dst)
            moved += 1
        except OSError:
            try:
                shutil.move(str(src), str(dst))
                moved += 1
            except OSError:
                failed.append(rel)
    return moved, failed


def remove_empty_dirs(root: Path, policy: Policy) -> None:
    ordered = []
    stack = [root]
    while stack:
        directory = stack.pop()
        for entry in os.scandir(directory):
            if entry.is_dir(follow_symlinks=False):
                rel = Path(entry.path).relative_to(root).as_posix()
                if policy.kept_dir(rel):
                    continue
                ordered.append(Path(entry.path))
                stack.append(Path(entry.path))
    for directory in sorted(ordered, key=lambda p: len(str(p)), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def copy_tree_files(src_root: Path, dst_root: Path, rels: list[str]) -> list[str]:
    copied = []
    for rel in rels:
        src, dst = src_root / rel, dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)
    return copied


def all_files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink())


# ── data snapshot ─────────────────────────────────────────────────────────────
def data_files(root: Path) -> list[str]:
    rels = set()
    for tree, skip in DATA_TREES.items():
        base = root / tree
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            inner = path.relative_to(base).as_posix()
            if any(inner == s or inner.startswith(s + "/") for s in skip):
                continue
            rels.add(path.relative_to(root).as_posix())
    for pattern in DATA_GLOBS:
        for path in root.glob(pattern):
            if path.is_file():
                rels.add(path.relative_to(root).as_posix())
    return sorted(rels)


def snapshot_data(root: Path, destination: Path) -> dict:
    rels = data_files(root)
    total = sum((root / r).stat().st_size for r in rels)
    say(f"Copying {len(rels)} data file(s), {human(total)}, into {destination.name}/ ...")
    records = []
    for rel in rels:
        src, dst = root / rel, destination / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        digest = sha256(src)
        if sha256(dst) != digest:
            raise UpdateError(f"The backup copy of {rel} does not match the original.")
        records.append({"path": rel, "size": src.stat().st_size, "sha256": digest})
    manifest = {"schema": "evejs-linux-data-snapshot/1", "takenAt": dt.datetime.now().astimezone().isoformat(),
                "files": records, "trees": list(DATA_TREES), "globs": DATA_GLOBS}
    write_json(destination / "snapshot.json", manifest)
    say(f"Verified {len(records)} copies byte for byte.")
    return manifest


def verify_snapshot(snapshot: Path) -> dict:
    manifest = load_json(snapshot / "snapshot.json")
    if not manifest:
        raise UpdateError(f"No data snapshot manifest in {snapshot}")
    for record in manifest["files"]:
        path = snapshot / record["path"]
        if not path.is_file() or path.stat().st_size != record["size"] or sha256(path) != record["sha256"]:
            raise UpdateError(f"Backup file is missing or changed: {record['path']}")
    return manifest


# ── post-update steps ─────────────────────────────────────────────────────────
def reinstall_node_packages(root: Path, conf: dict, staging_fingerprints: dict, compat) -> None:
    if is_steamos_portable(conf):
        payload = find_portable_payload(conf)
        if payload and install_portable_payload(root, payload, compat, want_node=True, want_market=False):
            return
        say("The Node packages changed, and no matching SteamOS portable bundle was found.", "warn")
        say("Download the EveJS Universal Linux Setup that matches this release and run its Update.", "warn")
        return
    npm = tool_path(conf, "npm")
    if not npm:
        say("The Node packages changed but npm was not found. Install them with `npm ci` in the folder and in server/.", "warn")
        return
    for directory in (root, root / "server"):
        if (directory / "package-lock.json").is_file():
            if run([npm, "ci", "--no-audit", "--no-fund"], directory, conf) != 0:
                raise UpdateError(f"npm ci failed in {directory}.")


def run_sovereignty_reset(root: Path, conf: dict, policy: Policy) -> None:
    tool = policy.raw.get("sovereigntyResetTool")
    if not tool:
        return
    if not (root / tool).is_file():
        say(f"{tool} is missing from this release, so the sovereignty reset did not run.", "warn")
        return
    node = tool_path(conf, "node")
    if not node:
        say(f"Node.js was not found, so the one-time sovereignty reset did not run. Run: node {tool}", "warn")
        return
    say("Running the one-time sovereignty reset (it does nothing when it already ran) ...", "step")
    if run([node, tool], root, conf) != 0:
        say(f"The sovereignty reset failed. The server also runs it on its next start; or run: node {tool}", "warn")


def find_portable_payload(conf: dict) -> Path | None:
    candidates = [os.environ.get("EVEJS_SETUP_PORTABLE_MANIFEST", "")]
    candidates.append(str(TOOLS_DIR / "portable/steamos-x86_64/manifest.json"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and (Path(candidate).parent / "payload.tar.xz").is_file():
            return Path(candidate)
    return None


def install_portable_payload(root: Path, manifest_path: Path, compat, want_node: bool, want_market: bool) -> bool:
    """Install the parts of a SteamOS bundle that match this installation's sources."""
    import tarfile
    manifest = load_json(manifest_path, {}) or {}
    archive = manifest_path.parent / manifest.get("payload", {}).get("file", "payload.tar.xz")
    if sha256(archive) != manifest.get("payload", {}).get("sha256"):
        raise UpdateError("SteamOS portable bundle checksum does not match its manifest.")
    members: list[str] = []
    if want_node:
        fingerprints = manifest.get("dependencyFingerprints") or {}
        if not fingerprints or any(compat.dependency_fingerprint(root / n) != f for n, f in fingerprints.items()):
            return False
        members += ["node_modules", "server/node_modules"]
    if want_market:
        for artifact in manifest.get("artifacts", []):
            crate = artifact.get("crate")
            if crate and compat.build_source_digest(compat.build_input_records(root / crate)) == artifact.get("sourceDigest"):
                members.append(artifact["path"])
        if not any(m.endswith("market-server") for m in members):
            return False
    with tarfile.open(archive, "r:xz") as tar:
        chosen = [m for m in tar.getmembers()
                  if any(m.name.removeprefix("./") == p or m.name.removeprefix("./").startswith(p + "/") for p in members)]
        for m in chosen:
            pure = PurePosixPath(m.name.removeprefix("./"))
            if pure.is_absolute() or ".." in pure.parts:
                raise UpdateError(f"Unsafe SteamOS bundle member: {m.name}")
        if want_node:
            for d in ("node_modules", "server/node_modules"):
                if (root / d).exists():
                    shutil.rmtree(root / d)
        try:
            tar.extractall(root, members=chosen, filter="data")
        except TypeError:  # Python older than 3.11.4 has no extraction filters
            tar.extractall(root, members=chosen)
    for artifact in manifest.get("artifacts", []):
        path = root / artifact["path"]
        if artifact["path"] in members:
            if sha256(path) != artifact["sha256"]:
                raise UpdateError(f"SteamOS bundle file changed while installing: {artifact['path']}")
            path.chmod(0o755)
    say(f"Installed matching SteamOS bundle parts: {', '.join(sorted({m.split('/target/')[0] for m in members}))}")
    return True


def prepare_market_runtime(root: Path, conf: dict, compat) -> None:
    """Make sure a verified Market server program exists for the installed Market source."""
    try:
        info = compat.classify(root)
    except compat.CompatError as exc:
        raise UpdateError(str(exc)) from exc
    try:
        compat.validate_cached_build(info)
        say("The Market server program already matches this Market source.")
        return
    except compat.CompatError:
        pass
    say("Preparing a Market server program for the new Market source ...", "step")
    candidate = root / "externalservices/market-server/target/release/market-server"
    if is_steamos_portable(conf):
        payload = find_portable_payload(conf)
        if not payload or not install_portable_payload(root, payload, compat, want_node=False, want_market=True):
            raise UpdateError(
                "This release changed the Market server, and no SteamOS portable bundle built for it was found.\n"
                "Download the EveJS Universal Linux Setup that matches this release and run its Update or\n"
                "'Prepare/verify runtime compatibility'. Your world and Market data are untouched."
            )
        compat.prepare(root, None, str(candidate))
        return
    cargo = tool_path(conf, "cargo")
    if not cargo:
        raise UpdateError("Cargo (Rust) is needed to build the new Market server program and was not found.")
    compat.prepare(root, cargo, None)


# ── commands ──────────────────────────────────────────────────────────────────
def backups_dir(root: Path) -> Path:
    policy = Policy([load_json(root / POLICY_REL)])
    return root / policy.backup_root


def cmd_update(args) -> int:
    root = Path(args.repo).absolute()
    zip_path = Path(args.zip).expanduser().absolute()
    conf = read_linux_conf(root)
    compat = load_compat()
    installed_policy = load_json(root / POLICY_REL)

    say("Checking the installation ...", "step")
    policy = Policy([installed_policy])
    assert_install_root(root, policy)
    assert_not_living_universe(root)
    current = installed_version(root)
    say(f"Folder  : {root}")
    say(f"Version : {current}")

    say("Checking the release zip ...", "step")
    release = read_release(zip_path, policy)
    say(f"Zip     : {zip_path}")
    say(f"Version : {release['version']}  ({release['fileCount']} files)")
    cur, inc = comparable_version(current), comparable_version(release["version"])
    if cur and inc and inc < cur and not args.allow_downgrade:
        raise UpdateError(f"That release ({release['version']}) is older than what is installed ({current}). "
                          "Pass --allow-downgrade if that is really what you want.")
    if cur and inc and inc == cur:
        say("That is the version already installed. Continuing will reinstall the program files.", "warn")

    try:
        assert_stopped(root, conf)
    except UpdateError:
        if not args.preview:
            raise
        say("The server is running. It must be stopped before a real update.", "warn")

    backups = root / policy.backup_root
    backups.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime(STAMP_FORMAT)
    staging = backups / f".staging-{stamp}"
    say("Unpacking the release to a temporary folder ...", "step")
    say(f"Unpacked {expand_release(release, staging)} file(s).")
    try:
        policy = Policy([installed_policy, load_json(staging / POLICY_REL)])
        say("Working out what changes ...", "step")
        plan = plan_update(root, staging, policy, compat)
        print_plan(plan, current, release["version"])
        new_build = compat.read_supported_build(staging)
        old_build = conf.get("SUPPORTED_BUILD")
        if new_build and old_build and str(new_build) != str(old_build):
            say(f"This release needs EVE client build {new_build}; your client is set up for build {old_build}.", "warn")
            say("After the update the launcher will refuse the old client until it is replaced.", "warn")
        if args.preview:
            say("Preview only. Nothing was changed.", "step")
            return 0
        data_bytes = sum((root / r).stat().st_size for r in data_files(root))
        free = shutil.disk_usage(root).free
        if free < plan["quarantineBytes"] * 2 + data_bytes + (512 << 20):
            raise UpdateError(f"Not enough free space: the update needs about "
                              f"{human(plan['quarantineBytes'] * 2 + data_bytes)} and {human(free)} is free.")
        if not confirm(f"Update EveJS {current} -> {release['version']} in {root}?", args.yes):
            say("Nothing was changed.")
            return 1

        backup = backups / f"pre-update-{current}-{stamp}"
        backup.mkdir()
        say("Backing up your world, Market, settings and certificates ...", "step")
        snapshot_data(root, backup / LINUX_DATA)
        previous_manifest = root / policy.manifest_name
        if previous_manifest.is_file():
            shutil.copy2(previous_manifest, backup / LINUX_DATA / "installed-files-before.json")

        say("Moving the old program files to the backup folder ...", "step")
        say(str(backup))
        moved, failed = move_files(root, backup, plan["toMove"])
        if failed:
            move_files(backup, root, plan["toMove"])
            raise UpdateError(f"Could not move {len(failed)} file(s) out of the way (first: {failed[0]}). "
                              "The installation was put back as it was.")
        remove_empty_dirs(root, policy)
        say(f"Moved {moved} file(s).")

        kept = []
        for rel in policy.raw.get("keepMine", []):
            if (root / rel).is_file() and (staging / rel).is_file():
                (staging / rel).unlink()
                kept.append(rel)
        copy_list = all_files(staging)
        say("Copying the new version in ...", "step")
        copied: list[str] = []
        try:
            for rel in copy_list:
                if policy.kept_file(rel) and (root / rel).exists():
                    continue  # never overwrite a kept file that already exists here
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(staging / rel, root / rel)
                copied.append(rel)
        except OSError as exc:
            say(f"The copy failed: {exc}", "error")
            for rel in copied:
                try:
                    (root / rel).unlink()
                except OSError:
                    pass
            move_files(backup, root, plan["toMove"])
            remove_empty_dirs(root, policy)
            raise UpdateError("The update failed and the installation was put back as it was. Nothing was lost.")
        for rel in kept:
            say(f"kept your {rel}")

        staged_now = program_files(staging, policy)
        write_json(root / policy.manifest_name, {
            "schema": "evejs.installed-files.v1",
            "comment": "Written by the EveJS updater. It records what the last update installed, so the next one "
                       "removes exactly what a new release drops and leaves your own files alone.",
            "version": release["version"], "installedAt": dt.datetime.now().astimezone().isoformat(),
            "files": staged_now})
        write_json(backup / AFTER_LIST, {"version": release["version"], "files": copied})
        write_json(backup / "manifest.json", {
            "schema": "evejs.updater-backup.v1", "linuxSchema": "evejs-linux-update-backup/1",
            "movedAt": dt.datetime.now().astimezone().isoformat(), "fromVersion": current,
            "toVersion": release["version"], "installMode": "native", "platformMode": conf.get("EVEJS_PLATFORM_MODE", ""),
            "releaseZip": str(zip_path), "releaseZipSha256": sha256(zip_path),
            "movedFileCount": len(plan["toMove"]), "movedFiles": plan["toMove"],
            "removedByThisRelease": plan["removed"], "settingsKept": kept,
            "dataSnapshot": LINUX_DATA, "setupVersion": SETUP_VERSION})

        say("Finishing the update ...", "step")
        failures = []
        try:
            compat.record_release(root, staging, zip_path)
            say(f"Recorded the official EveJS {release['version']} release for the launcher.")
        except Exception as exc:
            failures.append(f"Could not record the release: {exc}")
        if plan["dependencyChanged"]:
            say("The Node packages changed. Reinstalling them (this can take a few minutes) ...", "step")
            try:
                reinstall_node_packages(root, conf, {}, compat)
            except UpdateError as exc:
                failures.append(str(exc))
        run_sovereignty_reset(root, conf, policy)
        try:
            prepare_market_runtime(root, conf, compat)
        except Exception as exc:
            failures.append(str(exc))
        updates = {"EVEJS_VERSION": release["version"]}
        if new_build:
            updates["SUPPORTED_BUILD"] = str(new_build)
        set_linux_conf(root, updates)
        say("Content packs are installed by the server on its next start.")

        say("")
        say(f"Update finished: {current} -> {release['version']}", "step")
        say("Your world, settings and certificates stayed in place; a verified copy is in the backup.")
        say(f"Backup: {backup}")
        say(f"Undo:   tools/update-evejs-linux.sh restore {backup.name}")
        for failure in failures:
            say(failure, "warn")
        if failures:
            say("The program files are updated, but the steps above need attention before playing.", "warn")
        (backup / "update.log").write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
        return 3 if failures else 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def list_backups(root: Path) -> list[Path]:
    base = backups_dir(root)
    if not base.is_dir():
        return []
    found = [p for p in base.iterdir() if p.is_dir() and re.match(r"pre-(update|restore|reseed)-", p.name)]
    return sorted(found, key=lambda p: p.stat().st_mtime)


def describe_backup(path: Path) -> str:
    manifest = load_json(path / "manifest.json", {}) or {}
    data = "data+program" if (path / LINUX_DATA / "snapshot.json").is_file() else "program only"
    if path.name.startswith("pre-reseed-"):
        data = "Market only"
    size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    span = f"{manifest.get('fromVersion', '?')} -> {manifest.get('toVersion', '?')}" if manifest else ""
    return f"{path.name}   {span}   [{data}, {human(size)}]"


def cmd_list(args) -> int:
    root = Path(args.repo).absolute()
    backups = list_backups(root)
    if not backups:
        say(f"No backups in {backups_dir(root)}")
        return 0
    for b in backups:
        say(describe_backup(b))
    return 0


def cmd_prune(args) -> int:
    root = Path(args.repo).absolute()
    backups = list_backups(root)
    doomed = backups[:-args.keep] if args.keep > 0 else backups
    if not doomed:
        say("Nothing to delete.")
        return 0
    for b in doomed:
        say(f"delete: {describe_backup(b)}")
    if not confirm(f"Delete {len(doomed)} backup(s) permanently?", args.yes):
        return 1
    for b in doomed:
        shutil.rmtree(b)
    say(f"Deleted {len(doomed)} backup(s).")
    return 0


def resolve_backup(root: Path, name: str | None) -> Path:
    backups = [b for b in list_backups(root) if not b.name.startswith("pre-reseed-")] if not name else list_backups(root)
    if not backups:
        raise UpdateError(f"No backups in {backups_dir(root)}")
    if not name or name == "latest":
        return backups[-1]
    for b in backups:
        if b.name == name or str(b) == name:
            return b
    raise UpdateError(f"No backup named {name}. Run `list` to see them.")


def cmd_restore(args) -> int:
    root = Path(args.repo).absolute()
    conf = read_linux_conf(root)
    compat = load_compat()
    backup = resolve_backup(root, args.backup)
    assert_stopped(root, conf)
    manifest = load_json(backup / "manifest.json", {}) or {}
    snapshot = backup / LINUX_DATA
    has_data = (snapshot / "snapshot.json").is_file()
    is_reseed = backup.name.startswith("pre-reseed-")
    do_program = not args.data_only and not is_reseed
    do_data = not args.program_only and (has_data or is_reseed)
    if args.data_only and not (has_data or is_reseed):
        raise UpdateError("That backup holds no data snapshot (it was made by EveJS's Windows updater or an older tool).")
    say("Restore", "step")
    say(f"Backup  : {describe_backup(backup)}")
    say(f"Program files : {'yes' if do_program else 'no'}    Data (world, Market, settings) : {'yes' if do_data else 'no'}")
    if do_data:
        say("Restoring data rewinds the world and Market to the moment of that backup: progress since then is lost.", "warn")
    if not confirm("Restore this backup now?", args.yes):
        say("Nothing was changed.")
        return 1

    policy = Policy([load_json(root / POLICY_REL), load_json(backup / POLICY_REL)])
    stamp = dt.datetime.now().strftime(STAMP_FORMAT)
    undo = root / policy.backup_root / f"pre-restore-{installed_version(root)}-{stamp}"
    undo.mkdir(parents=True)
    write_json(undo / "manifest.json", {"schema": "evejs.updater-backup.v1", "linuxSchema": "evejs-linux-update-backup/1",
                                        "movedAt": dt.datetime.now().astimezone().isoformat(),
                                        "fromVersion": installed_version(root), "toVersion": f"restore of {backup.name}"})
    if do_data:
        say("Verifying the backup's data copies ...", "step")
        verify_snapshot(snapshot) if has_data else None
        say("Keeping a copy of the current data so this restore can be undone ...", "step")
        snapshot_data(root, undo / LINUX_DATA)

    if do_program:
        say("Putting the old program files back ...", "step")
        after = load_json(backup / AFTER_LIST, {}) or {}
        installed_by_update = [f for f in after.get("files", []) if (root / f).is_file()]
        old_files = [f for f in all_files(backup) if not f.startswith(LINUX_DATA + "/")
                     and f not in ("manifest.json", AFTER_LIST, "update.log")]
        if not after:
            say("This backup has no record of what its update installed; files that update added stay in place.", "warn")
        current_to_move = sorted(set(installed_by_update) | {f for f in old_files if (root / f).is_file()})
        moved, failed = move_files(root, undo, current_to_move)
        if failed:
            move_files(undo, root, current_to_move)
            raise UpdateError(f"Could not move {len(failed)} current file(s) aside (first: {failed[0]}). Nothing was changed.")
        copy_tree_files(backup, root, old_files)
        remove_empty_dirs(root, policy)
        write_json(undo / AFTER_LIST, {"version": installed_version(root), "files": old_files})
        receipt_copy = snapshot / RELEASE_RECEIPT
        if receipt_copy.is_file():
            shutil.copy2(receipt_copy, root / RELEASE_RECEIPT)
        elif (root / RELEASE_RECEIPT).is_file():
            (root / RELEASE_RECEIPT).unlink()
        before = snapshot / "installed-files-before.json"
        if before.is_file():
            shutil.copy2(before, root / policy.manifest_name)
        elif (root / policy.manifest_name).is_file() and do_program and after:
            (root / policy.manifest_name).unlink()
        say(f"Restored {len(old_files)} program file(s); {moved} current file(s) kept in {undo.name}.")

    if do_data:
        say("Putting the data back ...", "step")
        records = verify_snapshot(snapshot)["files"]
        for record in records:
            rel = record["path"]
            if rel == RELEASE_RECEIPT:
                continue  # restored with the program files above, never on its own
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot / rel, root / rel)
        # A database restored without its old -wal/-shm must not pick up the current ones.
        restored = {r["path"] for r in records}
        for rel in list(restored):
            if rel.endswith(".sqlite"):
                for suffix in ("-wal", "-shm"):
                    if rel + suffix not in restored and (root / (rel + suffix)).exists():
                        (root / (rel + suffix)).unlink()
        say(f"Restored {len(records)} data file(s).")

    say("Checking the restored installation ...", "step")
    problems = []
    if do_program:
        npm_needed = any(compat.dependency_fingerprint(root / n) != compat.dependency_fingerprint(undo / n)
                         for n in ("package-lock.json", "server/package-lock.json") if (undo / n).exists())
        if npm_needed:
            try:
                reinstall_node_packages(root, conf, {}, compat)
            except UpdateError as exc:
                problems.append(str(exc))
        set_linux_conf(root, {"EVEJS_VERSION": installed_version(root)})
        try:
            prepare_market_runtime(root, conf, compat)
        except Exception as exc:
            problems.append(str(exc))
    (undo / "restore.log").write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
    say(f"Restore finished. Undo it with: tools/update-evejs-linux.sh restore {undo.name}", "step")
    for problem in problems:
        say(problem, "warn")
    return 3 if problems else 0


MARKET_PROFILES = {
    "v1": ("v1 — Synthetic Jita + New Caldari", "tools/market-seed", "market-seed",
           [["--config", "config/market-seed.local.toml", "build", "--force", "--preset", "jita_new_caldari"]]),
    "v2": ("v2 — Live TQ Jita market + NPC catalog", "tools/market-seederv2", "market-seederv2",
           [["--config", "config/market-seederv2.local.toml", "build", "--yes", "--reuse-download"]]),
    "v3": ("v3 — Cost-priced Jita 4-4 + authentic NPC catalog", "tools/market-seederv3", "market-seederv3",
           [["--config", "config/market-seederv3.local.toml", "doctor"],
            ["--config", "config/market-seederv3.local.toml", "snapshot-info", "--download", "--reuse-download"],
            ["--config", "config/market-seederv3.local.toml", "audit"],
            ["--config", "config/market-seederv3.local.toml", "build", "--yes"]]),
}
MARKET_DB_DIR = "externalservices/market-server/data/generated"


def cmd_reseed(args) -> int:
    root = Path(args.repo).absolute()
    conf = read_linux_conf(root)
    compat = load_compat()
    profile = (args.profile or "").lower()
    if profile not in MARKET_PROFILES:
        choices = "\n".join(f"  {k}  {v[0]}" for k, v in MARKET_PROFILES.items())
        raise UpdateError(f"Choose a Market profile:\n{choices}")
    label, crate, binary_name, steps = MARKET_PROFILES[profile]
    if not (root / crate / "Cargo.toml").is_file() and not (root / crate / "target/release" / binary_name).is_file():
        raise UpdateError(f"This EveJS release has no {crate}.")
    assert_stopped(root, conf)
    say("Reseed the Market", "step")
    say(f"Folder  : {root}")
    say(f"Current : {conf.get('MARKET_LABEL') or conf.get('MARKET_PROFILE') or 'unknown'}")
    say(f"New     : {label}")
    say("This replaces the whole Market: every buy and sell order, including your characters' own", "warn")
    say("orders and the items/ISK held in them, is replaced by the fresh seed. Your hangars, wallets", "warn")
    say("and the rest of the world are not touched. The current Market is backed up first.", "warn")
    if not confirm("Reseed the Market now?", args.yes):
        say("Nothing was changed.")
        return 1

    stamp = dt.datetime.now().strftime(STAMP_FORMAT)
    backup = backups_dir(root) / f"pre-reseed-{stamp}"
    snap = backup / LINUX_DATA
    db_dir = root / MARKET_DB_DIR
    db_dir.mkdir(parents=True, exist_ok=True)
    say("Backing up the current Market database ...", "step")
    records = []
    for path in sorted(db_dir.glob("market.sqlite*")):
        rel = path.relative_to(root).as_posix()
        (snap / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, snap / rel)
        if sha256(snap / rel) != sha256(path):
            raise UpdateError(f"Backup copy of {rel} does not match. Nothing was changed.")
        records.append({"path": rel, "size": path.stat().st_size, "sha256": sha256(path)})
    write_json(snap / "snapshot.json", {"schema": "evejs-linux-data-snapshot/1", "files": records,
                                        "takenAt": dt.datetime.now().astimezone().isoformat()})
    write_json(backup / "manifest.json", {"schema": "evejs-linux-reseed-backup/1", "fromVersion": conf.get("MARKET_PROFILE", "?"),
                                          "toVersion": profile, "evejsVersion": installed_version(root)})
    say(f"Backup: {backup}")

    try:
        say(f"Seeding: {label}", "step")
        for path in db_dir.glob("market.sqlite-*"):
            path.unlink()  # a stale -wal/-shm must not be applied to the new database
        for step in steps:
            binary = root / crate / "target/release" / binary_name
            if is_steamos_portable(conf) or (binary.is_file() and not tool_path(conf, "cargo")):
                if not binary.is_file():
                    raise UpdateError(f"The bundled {binary_name} program is missing: {binary}")
                cmd = [str(binary)] + step
            else:
                cargo = tool_path(conf, "cargo")
                if not cargo:
                    raise UpdateError("Cargo (Rust) is needed to run the Market seeder and was not found.")
                cmd = [cargo, "run", "--release", "--"] + step
            if run(cmd, root / crate, conf) != 0:
                raise UpdateError(f"The seeder step failed: {' '.join(step)}")
        if not (db_dir / "market.sqlite").is_file() or (db_dir / "market.sqlite").stat().st_size == 0:
            raise UpdateError("The seeder did not produce market.sqlite.")
        say("Checking the new Market with the Market server ...", "step")
        info = compat.validate_cached_build(compat.classify(root))
        if run([info["marketBinary"], "--config", "config/market-server.local.toml", "doctor"],
               root / "externalservices/market-server", conf) != 0:
            raise UpdateError("The Market server's doctor check rejected the new Market database.")
    except Exception as exc:
        say(f"Reseed failed: {exc}", "error")
        say("Putting the previous Market back ...", "step")
        for path in db_dir.glob("market.sqlite*"):
            path.unlink()
        for record in records:
            shutil.copy2(snap / record["path"], root / record["path"])
        say("The previous Market was restored. Nothing else was changed.")
        return 2
    set_linux_conf(root, {"MARKET_PROFILE": profile, "MARKET_LABEL": label})
    (backup / "reseed.log").write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
    say(f"Market reseeded: {label}", "step")
    say(f"Undo: tools/update-evejs-linux.sh restore {backup.name}")
    return 0


def cmd_record_install(args) -> int:
    """Called by the installer after a fresh install: record the release and what it installed."""
    root = Path(args.repo).absolute()
    release_dir = Path(args.release_dir).absolute()
    compat = load_compat()
    policy = Policy([load_json(release_dir / POLICY_REL)])
    receipt = compat.record_release(root, release_dir, Path(args.zip).absolute() if args.zip else None)
    write_json(root / policy.manifest_name, {
        "schema": "evejs.installed-files.v1",
        "comment": "Written by the EveJS Universal Linux Setup. It records what this installation received, so "
                   "the first update removes exactly what a new release drops and leaves your own files alone.",
        "version": receipt["evejsVersion"], "installedAt": dt.datetime.now().astimezone().isoformat(),
        "files": program_files(release_dir, policy)})
    say(f"Recorded EveJS {receipt['evejsVersion']} as installed from its official release.")
    return 0


def default_repo() -> str:
    # tools/update-evejs-linux.sh passes --repo; run from .evejs-linux-tools the parent is the install.
    if TOOLS_DIR.name == ".evejs-linux-tools":
        return str(TOOLS_DIR.parent)
    return os.getcwd()


def main() -> int:
    ap = argparse.ArgumentParser(prog="update-evejs-linux.sh", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=default_repo(), help="EveJS installation folder (default: this installation)")
    sub = ap.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p = sub.add_parser("update", parents=[common], help="install an EveJS release zip over this installation")
    p.add_argument("zip")
    p.add_argument("--preview", action="store_true", help="show what would change, touch nothing")
    p.add_argument("--allow-downgrade", action="store_true")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p = sub.add_parser("restore", parents=[common], help="put a backup back (default: newest update backup)")
    p.add_argument("backup", nargs="?")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--program-only", action="store_true")
    group.add_argument("--data-only", action="store_true")
    p.add_argument("--yes", action="store_true")
    sub.add_parser("list", parents=[common], help="list backups")
    p = sub.add_parser("prune", parents=[common], help="delete old backups")
    p.add_argument("--keep", type=int, default=3)
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("reseed-market", parents=[common], help="rebuild the Market with an install-time seed profile")
    p.add_argument("profile", nargs="?", choices=sorted(MARKET_PROFILES))
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("record-install", parents=[common], help=argparse.SUPPRESS)
    p.add_argument("--release-dir", required=True)
    p.add_argument("--zip")
    args = ap.parse_args()
    handlers = {"record-install": cmd_record_install, "update": cmd_update, "restore": cmd_restore, "list": cmd_list, "prune": cmd_prune,
                "reseed-market": cmd_reseed}
    try:
        return handlers[args.command](args)
    except UpdateError as exc:
        say(str(exc), "error")
        return 2
    except KeyboardInterrupt:
        say("Interrupted.", "error")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
