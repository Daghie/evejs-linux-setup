#!/usr/bin/env python3
"""
EveJS Linux Installer Maintenance Engine v1.12
Target release: EveJS v0.12.7.1 / EVE client build 3396210

This file is an internal companion to install-evejs-linux.sh.
The Installer is the user-facing entry point for:
  - updating EveJS;
  - restoring an upgrade backup;
  - inspecting/deleting upgrade backups.

The normal EveJS launcher never invokes this updater.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
import zipfile
import stat

TARGET_VERSION = "0.12.7.1"
TARGET_BUILD = 3396210
SUPPORTED_UPDATE_SOURCES = {"0.12.7"}
REQUIRED_PORTS = (443, 5222, 26000, 26001, 26002, 26003, 26400, 40110, 40111)

SCRIPT_DIR = Path(__file__).resolve().parent
CONTRACT_ROOT = SCRIPT_DIR / "upgrade-contracts" / "0.12.7-to-0.12.7.1"
LAUNCHER_SOURCE = SCRIPT_DIR / "evejs-linux-launcher.sh"
CLEANUP_SOURCE = SCRIPT_DIR / "evejs-v0.12.7.1-stale-processes.sh"
COMPAT_SOURCE = SCRIPT_DIR / "evejs-linux-lu-compat.py"
COMPAT_DATA_SOURCE = SCRIPT_DIR / "compatibility"
INTEGRATED_LU_MANAGER_SOURCE = SCRIPT_DIR / "evejs-linux-integrated-lu.py"
INTEGRATED_LU_DATA_SOURCE = SCRIPT_DIR / "integrated-lu"


class UI:
    def __init__(self, force_text: bool = False):
        self.kind = "text"
        graphical = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        if not force_text and graphical:
            if shutil.which("zenity"):
                self.kind = "zenity"
            elif shutil.which("kdialog"):
                self.kind = "kdialog"

    def info(self, title: str, msg: str):
        if self.kind == "zenity":
            subprocess.run(["zenity", "--info", f"--title={title}", "--width=720", f"--text={msg}"])
        elif self.kind == "kdialog":
            subprocess.run(["kdialog", "--title", title, "--msgbox", msg])
        else:
            print(f"\n=== {title} ===\n{msg}\n")

    def warn(self, title: str, msg: str):
        if self.kind == "zenity":
            subprocess.run(["zenity", "--warning", f"--title={title}", "--width=760", f"--text={msg}"])
        elif self.kind == "kdialog":
            subprocess.run(["kdialog", "--title", title, "--sorry", msg])
        else:
            print(f"\n[WARNING] {title}\n{msg}\n", file=sys.stderr)

    def error(self, title: str, msg: str):
        if self.kind == "zenity":
            subprocess.run(["zenity", "--error", f"--title={title}", "--width=780", f"--text={msg}"])
        elif self.kind == "kdialog":
            subprocess.run(["kdialog", "--title", title, "--error", msg])
        else:
            print(f"\n[ERROR] {title}\n{msg}\n", file=sys.stderr)

    def yesno(self, title: str, msg: str) -> bool:
        if self.kind == "zenity":
            return subprocess.run(
                ["zenity", "--question", f"--title={title}", "--width=760", f"--text={msg}"]
            ).returncode == 0
        if self.kind == "kdialog":
            return subprocess.run(["kdialog", "--title", title, "--yesno", msg]).returncode == 0
        print(f"\n{title}\n{msg}")
        ans = input("Continue? [y/N]: ").strip().lower()
        return ans in {"y", "yes"}

    def choose_dir(self, title: str, start: str | None = None) -> Path | None:
        start = start or str(Path.home())
        if self.kind == "zenity":
            cp = subprocess.run(
                ["zenity", "--file-selection", "--directory", f"--title={title}", f"--filename={start}/"],
                text=True, capture_output=True,
            )
            return Path(cp.stdout.strip()).resolve() if cp.returncode == 0 and cp.stdout.strip() else None
        if self.kind == "kdialog":
            cp = subprocess.run(
                ["kdialog", "--title", title, "--getexistingdirectory", start],
                text=True, capture_output=True,
            )
            return Path(cp.stdout.strip()).resolve() if cp.returncode == 0 and cp.stdout.strip() else None
        print(f"\n{title}")
        value = input("Directory path: ").strip()
        return Path(value).expanduser().resolve() if value else None

    def choose_file(self, title: str, start: str | None = None) -> Path | None:
        start = start or str(Path.home())
        if self.kind == "zenity":
            cp = subprocess.run(
                ["zenity", "--file-selection", f"--title={title}", f"--filename={start}/"],
                text=True, capture_output=True,
            )
            return Path(cp.stdout.strip()).resolve() if cp.returncode == 0 and cp.stdout.strip() else None
        if self.kind == "kdialog":
            cp = subprocess.run(
                ["kdialog", "--title", title, "--getopenfilename", start],
                text=True, capture_output=True,
            )
            return Path(cp.stdout.strip()).resolve() if cp.returncode == 0 and cp.stdout.strip() else None
        print(f"\n{title}")
        value = input("File path: ").strip()
        return Path(value).expanduser().resolve() if value else None

    def choose_backup(self, title: str, records: list[dict]) -> dict | None:
        if not records:
            return None
        if self.kind == "zenity":
            rows = []
            for i, rec in enumerate(records, 1):
                rows += [str(i), rec["label"], rec["path"]]
            cp = subprocess.run(
                [
                    "zenity", "--list", f"--title={title}", "--width=1050", "--height=500",
                    "--text=Select an EveJS upgrade backup.",
                    "--column=#", "--column=Backup", "--column=Path",
                    *rows, "--print-column=1",
                ],
                text=True, capture_output=True,
            )
            if cp.returncode != 0 or not cp.stdout.strip().isdigit():
                return None
            idx = int(cp.stdout.strip()) - 1
            return records[idx] if 0 <= idx < len(records) else None

        if self.kind == "kdialog":
            args = []
            for i, rec in enumerate(records, 1):
                args += [str(i), f'{rec["label"]} — {rec["path"]}']
            cp = subprocess.run(
                ["kdialog", "--title", title, "--menu", "Select an EveJS upgrade backup:", *args],
                text=True, capture_output=True,
            )
            if cp.returncode != 0 or not cp.stdout.strip().isdigit():
                return None
            idx = int(cp.stdout.strip()) - 1
            return records[idx] if 0 <= idx < len(records) else None

        print(f"\n{title}")
        for i, rec in enumerate(records, 1):
            print(f"  [{i}] {rec['label']}\n      {rec['path']}")
        value = input(f"Choose [1-{len(records)}]: ").strip()
        if not value.isdigit():
            return None
        idx = int(value) - 1
        return records[idx] if 0 <= idx < len(records) else None

    def choose_backup_action(self) -> str:
        if self.kind == "zenity":
            cp = subprocess.run(
                [
                    "zenity", "--list", "--radiolist",
                    "--title=Manage EveJS upgrade backup", "--width=700", "--height=330",
                    "--text=Choose what to do with the selected backup.",
                    "--column=Use", "--column=Action", "--column=Description",
                    "TRUE", "inspect", "Show backup details",
                    "FALSE", "delete", "Delete this backup permanently",
                    "FALSE", "back", "Return without changing anything",
                    "--print-column=2",
                ],
                text=True, capture_output=True,
            )
            return cp.stdout.strip() if cp.returncode == 0 else "back"
        if self.kind == "kdialog":
            cp = subprocess.run(
                [
                    "kdialog", "--title", "Manage EveJS upgrade backup", "--menu",
                    "Choose an action:",
                    "inspect", "Show backup details",
                    "delete", "Delete this backup permanently",
                    "back", "Return without changing anything",
                ],
                text=True, capture_output=True,
            )
            return cp.stdout.strip() if cp.returncode == 0 else "back"
        print("\n[1] Inspect backup\n[2] Delete backup\n[3] Back")
        value = input("Choose [1-3]: ").strip()
        return {"1": "inspect", "2": "delete", "3": "back"}.get(value, "back")


def die(ui: UI, msg: str, code: int = 1):
    ui.error("EveJS Linux Setup", msg)
    raise SystemExit(code)


def run(cmd, *, cwd: Path | None = None, env: dict | None = None, check: bool = True,
        capture: bool = False) -> subprocess.CompletedProcess:
    print("+", " ".join(shlex.quote(str(x)) for x in cmd))
    return subprocess.run(
        [str(x) for x in cmd],
        cwd=str(cwd) if cwd else None,
        env=env,
        check=check,
        text=True,
        capture_output=capture,
    )


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_file_count(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(1 for p in root.rglob("*") if p.is_file())


def tree_manifest(root: Path) -> dict[str, dict]:
    """Content+metadata manifest for a preserved mod tree.

    Symlinks are recorded as symlinks rather than followed.  Directories are
    included so an empty mod-owned directory is also conserved.
    """
    manifest: dict[str, dict] = {}
    if not root.exists():
        return manifest

    for p in sorted(root.rglob("*"), key=lambda x: x.as_posix()):
        rel = p.relative_to(root).as_posix()
        st = p.lstat()
        mode = oct(st.st_mode & 0o7777)
        if p.is_symlink():
            manifest[rel] = {"type": "symlink", "target": os.readlink(p), "mode": mode}
        elif p.is_dir():
            manifest[rel] = {"type": "dir", "mode": mode}
        elif p.is_file():
            manifest[rel] = {
                "type": "file",
                "size": st.st_size,
                "sha256": sha256(p),
                "mode": mode,
            }
        else:
            manifest[rel] = {"type": "other", "mode": mode}
    return manifest


def manifest_digest(manifest: dict[str, dict]) -> str:
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def read_version(root: Path) -> str:
    try:
        data = json.loads((root / "config" / "version.json").read_text())
        return str(data.get("evejsVersion") or data.get("version") or "")
    except Exception:
        return ""


def read_supported_build(root: Path) -> int | None:
    try:
        data = json.loads((root / "tools" / "ClientSETUP" / "blue_patch_recipe.json").read_text())
        return int(data["supportedBuild"])
    except Exception:
        return None


def resolve_archive_root(root: Path) -> Path | None:
    if (root / "config/version.json").is_file() and (root / "package.json").is_file() and (root / "server").is_dir():
        return root
    candidates = []
    for vf in root.glob("**/config/version.json"):
        try:
            rel = vf.relative_to(root)
        except Exception:
            continue
        if len(rel.parts) < 3 or len(rel.parts) > 6:
            continue
        candidate = vf.parent.parent
        if (candidate / "package.json").is_file() and (candidate / "server").is_dir() and (candidate / "tools").is_dir():
            candidates.append(candidate.resolve())
    unique = []
    for c in sorted(candidates):
        if c not in unique:
            unique.append(c)
    return unique[0] if len(unique) == 1 else None


def _safe_extract_release_zip(zip_path: Path, dest: Path):
    seen=set()
    with zipfile.ZipFile(zip_path) as z:
        bad_crc=z.testzip()
        if bad_crc:
            raise RuntimeError(f"ZIP CRC failed at {bad_crc}")
        for info in z.infolist():
            raw=info.filename
            name=raw.replace("\\", "/")
            if not name or name.startswith("/") or name.startswith("\\") or re.match(r"^[A-Za-z]:", name):
                raise RuntimeError(f"unsafe ZIP path: {raw}")
            parts=Path(name).parts
            if any(part in ("..", "") for part in parts):
                raise RuntimeError(f"unsafe ZIP path: {raw}")
            key="/".join(parts).rstrip("/")
            if key in seen and not info.is_dir():
                raise RuntimeError(f"duplicate ZIP destination: {raw}")
            if not info.is_dir(): seen.add(key)
            mode=(info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise RuntimeError(f"symlink ZIP member is not accepted: {raw}")
            out=dest.joinpath(*parts)
            if info.is_dir() or name.endswith("/"):
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, out.open("wb") as dst:
                shutil.copyfileobj(src,dst)
            perm=mode & 0o777
            if perm:
                out.chmod(perm)


def extract_target_zip(ui: UI, zip_path: Path) -> tuple[Path, tempfile.TemporaryDirectory]:
    if not zip_path.is_file():
        die(ui, f"EveJS release ZIP not found:\n{zip_path}")
    temp = tempfile.TemporaryDirectory(prefix="evejs-update-source-")
    try:
        _safe_extract_release_zip(zip_path, Path(temp.name))
    except Exception as exc:
        temp.cleanup()
        die(ui, f"The selected file could not be safely extracted as an EveJS ZIP:\n{zip_path}\n\n{exc}")
    root = resolve_archive_root(Path(temp.name))
    if root is None:
        temp.cleanup()
        die(ui, "Could not locate a single EveJS repository root inside the selected ZIP.")
    version = read_version(root)
    build = read_supported_build(root)
    if version != TARGET_VERSION:
        temp.cleanup()
        die(ui, f"The selected archive is EveJS v{version or 'unknown'}.\n\nThis setup build targets EveJS v{TARGET_VERSION}.")
    if build != TARGET_BUILD:
        temp.cleanup()
        die(ui, f"The selected EveJS archive targets EVE build {build}.\n\nThis audited updater targets build {TARGET_BUILD}.")
    return root, temp


def load_linux_conf(ui: UI, root: Path) -> dict[str, str]:
    conf = root / ".evejs-linux.conf"
    if not conf.is_file():
        die(ui, f"This updater only accepts installations prepared by the Universal Linux Installer.\n\nMissing:\n{conf}")
    keys = [
        "EVEJS_REPO", "EVEJS_VERSION", "SUPPORTED_BUILD", "EVE_CLIENT_SELECTED",
        "TQ_PATH", "CACHE_ROOT", "RESFILES", "STEAM_ROOT", "PROTON_DIR",
        "PROTON_BIN", "PROTON_PREFIX", "NODE_BIN", "NODE_BIN_DIR", "CARGO_BIN",
        "MARKET_PROFILE", "MARKET_LABEL",
    ]
    script = 'set -a; source "$1"; shift; for k in "$@"; do printf "%s\\0" "${!k-}"; done'
    cp = subprocess.run(
        ["bash", "-c", script, "bash", str(conf), *keys],
        capture_output=True,
    )
    if cp.returncode != 0:
        die(ui, f"Could not safely read the existing Linux launcher configuration:\n{conf}")
    values = cp.stdout.decode(errors="replace").split("\0")
    return dict(zip(keys, values))


def write_linux_conf(path: Path, values: dict[str, str]):
    order = [
        "EVEJS_REPO", "EVEJS_VERSION", "SUPPORTED_BUILD", "EVE_CLIENT_SELECTED",
        "TQ_PATH", "CACHE_ROOT", "RESFILES", "STEAM_ROOT", "PROTON_DIR",
        "PROTON_BIN", "PROTON_PREFIX", "NODE_BIN", "NODE_BIN_DIR", "CARGO_BIN",
        "MARKET_PROFILE", "MARKET_LABEL",
    ]
    lines = []
    for key in order:
        if key in values:
            lines.append(f"{key}={shlex.quote(str(values[key]))}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def read_client_build(tq: Path) -> str:
    ini = tq / "start.ini"
    if not ini.is_file():
        return ""
    for line in ini.read_text(errors="replace").splitlines():
        m = re.match(r"^\s*build\s*=\s*([0-9]+)", line, re.I)
        if m:
            return m.group(1)
    return ""


def port_listeners() -> list[str]:
    cp = subprocess.run(["ss", "-H", "-ltnp"], text=True, capture_output=True)
    lines = []
    for line in cp.stdout.splitlines():
        if any(re.search(rf":{p}(?:\s|$)", line) for p in REQUIRED_PORTS):
            lines.append(line)
    return lines


def ensure_services_stopped(ui: UI, root: Path):
    listeners = port_listeners()
    if not listeners:
        return
    cleanup = root / "evejs-stale-process-cleanup.sh"
    text = "\n".join(listeners)
    if cleanup.is_file() and os.access(cleanup, os.X_OK):
        if ui.yesno(
            "Running EveJS services detected",
            "The update/restore cannot modify EveJS while its backend is running.\n\n"
            f"Detected listeners:\n{text}\n\n"
            "Run this installation's safe stale-process cleanup tool now?\n\n"
            "Unknown/non-EveJS processes will be refused by that tool.",
        ):
            run([str(cleanup)], check=False)
            if not port_listeners():
                return
    die(
        ui,
        "EveJS services are still using required ports.\n\n"
        "Close the EVE client and launcher, stop Market/Game, then retry.\n\n"
        f"Remaining listeners:\n{chr(10).join(port_listeners())}",
    )



def ensure_no_xpatch_overlay_for_update(ui: UI, root: Path):
    # A v1.11 installer-managed Integrated LU is an explicitly supported migration source.
    if (root / "_local/evejs-linux-integrated-lu/install.json").is_file():
        return
    state = root / "_local/x-eve-patch/install.json"
    signatures = [root/"config/xeve.json", root/"evejs.config.x-eve.json", root/"server/src/services/xEve/xEveRuntime.js"]
    present=[str(x.relative_to(root)) for x in signatures if x.exists()]
    if state.is_file():
        try: payload=json.loads(state.read_text())
        except Exception: payload={}
        die(ui, "Older X-Patch Living Universe state is present and is not a supported migration source.\n\n"
                f"X-Patch: {payload.get('pluginVersion','unknown')} / EveJS {payload.get('evejsVersion','unknown')}\n"
                "Remove it with its own supported method first.")
    if present:
        die(ui, "Living-Universe-like source files were found without the installer-managed Integrated LU authority.\n\n"
                "The updater will not guess or adopt them.\n  - " + "\n  - ".join(present))

def sqlite_checkpoint_integrity(ui: UI, path: Path, label: str) -> str | None:
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(str(path), timeout=10)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            row = con.execute("PRAGMA integrity_check").fetchone()
            if not row or str(row[0]).lower() != "ok":
                die(ui, f"{label} failed SQLite integrity_check:\n{row}")
        finally:
            con.close()
    except sqlite3.Error as exc:
        die(ui, f"Could not safely checkpoint/validate {label}:\n{path}\n\n{exc}")
    return sha256(path)


def copy_reflink(src: Path, dst: Path):
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    cp = subprocess.run(["cp", "-a", "--reflink=auto", str(src), str(dst)])
    if cp.returncode != 0:
        run(["cp", "-a", str(src), str(dst)])


def overlay_dir(src: Path, dst: Path):
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    cp = subprocess.run(["cp", "-a", "--reflink=auto", f"{src}/.", f"{dst}/"])
    if cp.returncode != 0:
        run(["cp", "-a", f"{src}/.", f"{dst}/"])


def copy_preserved_update_state(existing: Path, stage: Path):
    """Preserve user/world state named by EveJS v0.12.7.1's upstream updater policy."""
    if (existing / "_local").is_dir():
        overlay_dir(existing / "_local", stage / "_local")

    existing_mod_manifest = tree_manifest(existing / "mods")
    if (existing / "mods").is_dir():
        if (stage / "mods").exists(): shutil.rmtree(stage / "mods")
        copy_reflink(existing / "mods", stage / "mods")
    else:
        (stage / "mods").mkdir(exist_ok=True)
    if tree_manifest(stage / "mods") != existing_mod_manifest:
        raise RuntimeError("mod conservation failure")

    for rel in ("logs", "server/logs", "server/src/logs", "server/src/_secondary/data/chat",
                "server/src/_secondary/image/generated/Alliance", "server/src/_secondary/image/generated/Character",
                "server/src/gameStore/images", "tools/ConfigEditor/assets"):
        src=existing/rel
        if src.is_dir(): overlay_dir(src, stage/rel)

    # Preserve all Market runtime data, never only the main sqlite file.
    market_data=existing/"externalservices/market-server/data"
    if market_data.is_dir():
        if (stage/"externalservices/market-server/data").exists(): shutil.rmtree(stage/"externalservices/market-server/data")
        copy_reflink(market_data, stage/"externalservices/market-server/data")

    for rel in ("server/certs", "server/src/_secondary/express/certs"):
        src=existing/rel
        if src.is_dir(): overlay_dir(src, stage/rel)

    # User-local root/config files explicitly kept by upstream update-policy.json.
    for rel in (".env", ".env.local", "evejs.config.json", "evejs.config.local.json",
                "evejs.config.x-eve.local.json", "tools/ClientSETUP/scripts/EvEJSConfig.bat"):
        src=existing/rel
        if src.is_file():
            dst=stage/rel; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dst)

    # Generated source-tree game-store data.json files can be operator-owned.
    src_data=existing/"server/src/gameStore/data"
    if src_data.is_dir():
        for p in src_data.glob("*/data.json"):
            rel=p.relative_to(existing); dst=stage/rel; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(p,dst)

    return existing_mod_manifest

MISSING = object()


def json_merge(old, user, new, path="", report=None):
    """Three-way config merge.

    - unchanged user value -> target default
    - unchanged target default -> user's value
    - newly added target key -> target value
    - user-only key -> user value
    - both changed differently -> user wins, conflict is reported
    """
    report = report if report is not None else {"target": [], "user": [], "conflicts": []}

    if user is not MISSING and old is not MISSING and user == old:
        if new is MISSING:
            report["target"].append(f"{path}: removed by target")
            return MISSING
        if new != old:
            report["target"].append(f"{path}: adopted target change")
        return new

    if new is not MISSING and old is not MISSING and new == old:
        if user is MISSING:
            report["user"].append(f"{path}: user deletion preserved")
            return MISSING
        if user != old:
            report["user"].append(f"{path}: user value preserved")
        return user

    if old is MISSING:
        if user is MISSING:
            if new is not MISSING:
                report["target"].append(f"{path}: new target setting added")
            return new
        if new is MISSING:
            report["user"].append(f"{path}: user-only setting preserved")
            return user
        if user == new:
            return user

    if isinstance(old, dict) or isinstance(user, dict) or isinstance(new, dict):
        old_d = old if isinstance(old, dict) else {}
        user_d = user if isinstance(user, dict) else {}
        new_d = new if isinstance(new, dict) else {}
        result = {}
        for key in sorted(set(old_d) | set(user_d) | set(new_d)):
            child_path = f"{path}.{key}" if path else key
            value = json_merge(
                old_d.get(key, MISSING),
                user_d.get(key, MISSING),
                new_d.get(key, MISSING),
                child_path,
                report,
            )
            if value is not MISSING:
                result[key] = value
        return result

    if user is MISSING:
        if new is MISSING:
            return MISSING
        report["conflicts"].append(f"{path}: user removed it while target also changed; user deletion preserved")
        return MISSING

    if new is MISSING:
        report["conflicts"].append(f"{path}: target removed it while user changed it; user value preserved")
        return user

    if user != new:
        report["conflicts"].append(f"{path}: both user and target changed it; user value preserved")
    return user


def merge_configs(ui: UI, existing: Path, stage: Path) -> dict:
    old_defaults = CONTRACT_ROOT / "old-defaults" / "config"
    if not old_defaults.is_dir():
        die(ui, f"Updater contract is incomplete:\n{old_defaults}")

    report = {"target": [], "user": [], "conflicts": []}
    config_dir = stage / "config"
    for new_file in sorted(config_dir.glob("*.json")):
        if new_file.name == "version.json":
            continue
        old_file = old_defaults / new_file.name
        user_file = existing / "config" / new_file.name

        # Files absent in the old release are simply target-owned additions.
        if not old_file.is_file():
            if user_file.is_file():
                try:
                    user_data = json.loads(user_file.read_text())
                    new_data = json.loads(new_file.read_text())
                except Exception as exc:
                    die(ui, f"Could not parse configuration during update:\n{new_file.name}\n\n{exc}")
                merged = json_merge(MISSING, user_data, new_data, new_file.name, report)
                new_file.write_text(json.dumps(merged, indent=2) + "\n")
            continue

        if not user_file.is_file():
            report["target"].append(f"{new_file.name}: missing user config restored from target release")
            continue

        try:
            old_data = json.loads(old_file.read_text())
            user_data = json.loads(user_file.read_text())
            new_data = json.loads(new_file.read_text())
        except Exception as exc:
            die(ui, f"Could not parse configuration during update:\n{new_file.name}\n\n{exc}")

        merged = json_merge(old_data, user_data, new_data, new_file.name, report)
        if merged is MISSING:
            # Config files themselves are required; if user removed one, use target.
            merged = new_data
            report["target"].append(f"{new_file.name}: required target config restored")
        new_file.write_text(json.dumps(merged, indent=2) + "\n")

    report_path = stage / "CONFIG-MERGE-REPORT.txt"
    lines = [
        "EveJS update configuration merge",
        "=================================",
        "",
        "Rules:",
        "- unchanged old defaults adopt the new EveJS default;",
        "- user changes are preserved;",
        "- new target settings are added;",
        "- simultaneous user+target changes preserve the user value and are reported.",
        "",
        "Target changes adopted:",
    ]
    lines += [f"  - {x}" for x in report["target"]] or ["  (none)"]
    lines += ["", "User changes preserved:"]
    lines += [f"  - {x}" for x in report["user"]] or ["  (none)"]
    lines += ["", "Merge conflicts (user value preserved):"]
    lines += [f"  - {x}" for x in report["conflicts"]] or ["  (none)"]
    report_path.write_text("\n".join(lines) + "\n")
    return report


def validate_json_configs(ui: UI, root: Path):
    for p in sorted((root / "config").glob("*.json")):
        try:
            json.loads(p.read_text())
        except Exception as exc:
            die(ui, f"Target configuration is invalid after merge:\n{p}\n\n{exc}")


def ensure_sde(ui: UI, stage: Path, build: int):
    sde_url = f"https://developers.eveonline.com/static-data/tranquility/eve-online-static-data-{build}-jsonl.zip"
    sde_zip = stage / "_local" / "downloads" / "sde" / f"eve-online-static-data-{build}-jsonl.zip"
    sde_dir = stage / "_local" / "sde" / f"eve-online-static-data-{build}-jsonl"
    sde_zip.parent.mkdir(parents=True, exist_ok=True)
    sde_dir.mkdir(parents=True, exist_ok=True)

    if not sde_zip.is_file() or sde_zip.stat().st_size == 0:
        run(["curl", "-fL", "--retry", "3", "--retry-delay", "2", sde_url, "-o", str(sde_zip)])
    if not (sde_dir / "_sde.jsonl").is_file():
        if sde_dir.exists():
            shutil.rmtree(sde_dir)
        sde_dir.mkdir(parents=True)
        run(["unzip", "-q", str(sde_zip), "-d", str(sde_dir)])
    return sde_url, sde_zip, sde_dir


def rebuild_static_database(ui: UI, stage: Path, node: Path, build: int):
    game_root = stage / "_local" / "gameStore"
    data = game_root / "data"
    manifest = game_root / "manifest.json"
    if data.exists():
        shutil.rmtree(data)
    manifest.unlink(missing_ok=True)

    sde_url, _zip, sde_dir = ensure_sde(ui, stage, build)
    run([
        str(node), "--max-old-space-size=8192",
        str(stage / "tools" / "DatabaseCreator" / "database-creator.js"),
        "--sde-dir", str(sde_dir),
        "--out", str(data),
        "--build", str(build),
        "--sde-url", sde_url,
        "--force",
    ])
    if not manifest.is_file():
        die(ui, "The target-version static database manifest was not created.")


def current_tool_path(args, conf: dict, key: str, fallback_name: str) -> Path:
    """Return the executable path WITHOUT dereferencing symlink/proxy launchers.

    This is essential for rustup-managed Cargo.  ~/.cargo/bin/cargo is normally
    a rustup proxy/symlink; Path.resolve() turns it into ~/.cargo/bin/rustup,
    which changes `cargo build` into the invalid command `rustup build`.
    """
    env_name = {
        "NODE_BIN": "EVEJS_SETUP_NODE_BIN",
        "CARGO_BIN": "EVEJS_SETUP_CARGO_BIN",
    }[key]
    value = os.environ.get(env_name) or conf.get(key) or shutil.which(fallback_name) or ""
    if not value:
        return Path("")
    value = os.path.expandvars(os.path.expanduser(value))
    if not os.path.isabs(value):
        found = shutil.which(value)
        if found:
            value = found
    # abspath normalizes . and .. but deliberately does NOT follow symlinks.
    return Path(os.path.abspath(value))


def validate_tool_identity(ui: UI, path: Path, kind: str):
    if not path.is_file():
        die(ui, f"{kind} executable not found:\n{path}")

    try:
        cp = subprocess.run([str(path), "--version"], text=True, capture_output=True, timeout=10)
    except Exception as exc:
        die(ui, f"Could not execute {kind}:\n{path}\n\n{exc}")

    output = (cp.stdout + "\n" + cp.stderr).strip()
    if cp.returncode != 0:
        die(ui, f"{kind} --version failed:\n{path}\n\n{output}")

    if kind == "Cargo" and not re.search(r"(^|\n)cargo\s+\d", output, re.I):
        die(
            ui,
            "The updater's Cargo path does not execute Cargo.\n\n"
            f"Path:\n{path}\n\n"
            f"Reported version output:\n{output}\n\n"
            "The update was stopped before any existing EveJS files were replaced.",
        )

    if kind == "Node.js" and not re.search(r"(^|\n)v\d+\.\d+\.\d+", output):
        die(ui, f"The selected Node.js path did not report a Node version:\n{path}\n\n{output}")

    return output


def validate_stage_runtime(ui: UI, stage: Path, node: Path, cargo: Path,
                           expected_game_hash: str | None, expected_market_hash: str | None):
    if read_version(stage) != TARGET_VERSION:
        die(ui, "Staged EveJS version validation failed.")
    if read_supported_build(stage) != TARGET_BUILD:
        die(ui, "Staged EVE client-build contract validation failed.")

    validate_json_configs(ui, stage)

    cp = subprocess.run(["bash", "-n", str(stage / "evejs-linux-launcher.sh")])
    if cp.returncode != 0:
        die(ui, "The staged Linux launcher failed Bash syntax validation.")
    cp = subprocess.run(["bash", "-n", str(stage / "evejs-stale-process-cleanup.sh")])
    if cp.returncode != 0:
        die(ui, "The staged stale-process cleanup tool failed Bash syntax validation.")

    game_db = stage / "_local" / "gameStore" / "gamestore.sqlite"
    market_db = stage / "externalservices" / "market-server" / "data" / "generated" / "market.sqlite"

    game_hash = sqlite_checkpoint_integrity(ui, game_db, "staged player/world database")
    market_hash = sqlite_checkpoint_integrity(ui, market_db, "staged Market database")

    if expected_game_hash and game_hash != expected_game_hash:
        die(
            ui,
            "Player/world database conservation check failed.\n\n"
            f"Before update: {expected_game_hash}\n"
            f"Staged target: {game_hash}\n\n"
            "The updater will not continue.",
        )
    if expected_market_hash and market_hash != expected_market_hash:
        die(
            ui,
            "Market database conservation check failed.\n\n"
            f"Before update: {expected_market_hash}\n"
            f"Staged target: {market_hash}\n\n"
            "The updater will not reseed or continue.",
        )

    market_dir = stage / "externalservices" / "market-server"
    market_bin = market_dir / "target" / "release" / "market-server"
    if not market_bin.is_file():
        die(ui, "The v0.12.7.1 Market server binary was not built.")
    run([str(market_bin), "--config", "config/market-server.local.toml", "doctor"], cwd=market_dir)


def backup_base_for(root: Path) -> Path:
    return root.parent / ".evejs-upgrade-backups" / root.name


def write_metadata(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def backup_records(root: Path) -> list[dict]:
    base = backup_base_for(root)
    result = []
    if not base.is_dir():
        return result
    for record_dir in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
        meta_path = record_dir / "metadata.json"
        install = record_dir / "installation"
        if not meta_path.is_file() or not install.is_dir():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        label = (
            f'{meta.get("source_version","?")} → {meta.get("target_version","?")} '
            f'| {meta.get("timestamp","?")} | {meta.get("status","unknown")}'
        )
        result.append({"path": str(record_dir), "record_dir": record_dir, "installation": install, "meta": meta, "label": label})
    return result


def update_desktop_entry(original_root: Path, old_version: str, new_version: str):
    digest = hashlib.sha256(str(original_root).encode()).hexdigest()[:12]
    appdir = Path.home() / ".local" / "share" / "applications"
    old = appdir / f"evejs-{old_version}-{digest}.desktop"
    new = appdir / f"evejs-{new_version}-{digest}.desktop"
    if not old.is_file():
        return
    text = old.read_text(errors="replace")
    text = re.sub(r"^Name=.*$", f"Name=EveJS {new_version}", text, flags=re.M)
    text = re.sub(r"^Exec=.*$", f'Exec=/bin/bash "{original_root}/evejs-linux-launcher.sh"', text, flags=re.M)
    new.write_text(text)
    new.chmod(0o755)
    old.unlink(missing_ok=True)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(appdir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def choose_existing_install(ui: UI, explicit: str | None) -> Path:
    root = Path(explicit).expanduser().resolve() if explicit else None
    while root is None:
        root = ui.choose_dir("Select the existing EveJS installation to maintain")
        if root is None:
            raise SystemExit(0)
    if not root.is_dir() or not (root / "server/index.js").is_file() or not (root / "config/version.json").is_file():
        die(ui, f"The selected folder is not a recognizable EveJS installation:\n{root}")
    return root


OLD_V111_LU_AUTHORITY = "58B1FEA6365E81051DAA4024AFE84BEC04B8A0D9B5C9C5F256FAD9FFE09061CA"

def verify_old_integrated_lu_for_migration(ui: UI, existing: Path) -> bool:
    record_path=existing/"_local/evejs-linux-integrated-lu/install.json"
    if not record_path.is_file(): return False
    try: record=json.loads(record_path.read_text())
    except Exception as exc: die(ui,f"Could not parse prior Integrated LU install record: {exc}")
    if str(record.get("authoritySha256","")).upper()!=OLD_V111_LU_AUTHORITY or str(record.get("evejsVersion",""))!="0.12.7":
        die(ui,"An installer-managed Integrated LU is present, but it is not the audited v1.11/v0.12.7 authority supported by this migration. Nothing was changed.")
    mgr=existing/".evejs-linux-tools/evejs-linux-integrated-lu.py"
    manifest=existing/".evejs-linux-tools/integrated-lu/manifest.json"
    if not mgr.is_file() or not manifest.is_file():
        die(ui,"The prior v1.11 LU verification authority is missing from .evejs-linux-tools; migration cannot be proven safely.")
    cp=subprocess.run([sys.executable,str(mgr),"verify","--repo",str(existing)],text=True,capture_output=True)
    if cp.returncode!=0:
        die(ui,"Prior Integrated LU verification failed. The update was stopped before staging.\n\n"+(cp.stdout+cp.stderr)[-5000:])
    print("    ✓ Prior v1.11 Integrated LU source verified for transactional migration")
    return True

def prepare_stage(ui: UI, existing: Path, source_root: Path, conf: dict,
                  node: Path, cargo: Path, game_hash: str | None, market_hash: str | None) -> tuple[Path, dict]:
    parent = existing.parent
    stage = parent / f".evejs-update-stage-{existing.name}-{uuid.uuid4().hex[:10]}"
    stage.mkdir(parents=False, exist_ok=False)
    print(f"\n==> Staging EveJS v{TARGET_VERSION}\n    {stage}")
    overlay_dir(source_root, stage)

    existing_mod_manifest = copy_preserved_update_state(existing, stage)
    if tree_manifest(stage / "mods") != existing_mod_manifest:
        die(ui, "Mod conservation check failed while staging the update. The live installation was not replaced.")
    print(f"    ✓ Preserved mods exactly: {tree_file_count(existing / 'mods')} files")

    merge_report = merge_configs(ui, existing, stage)
    npm = node.parent / "npm"
    if not npm.exists():
        found = shutil.which("npm")
        if not found: die(ui, "npm could not be found for the selected Node.js runtime.")
        npm = Path(found)
    env = os.environ.copy(); env["PATH"] = f"{node.parent}:{cargo.parent}:{env.get('PATH','')}"

    print("\n==> Installing v0.12.7.1 root dependencies in the staging tree")
    run([str(npm), "ci", "--no-audit", "--no-fund"], cwd=stage, env=env)
    print("\n==> Installing v0.12.7.1 server dependencies in the staging tree")
    run([str(npm), "ci", "--no-audit", "--no-fund"], cwd=stage / "server", env=env)
    run([str(node), "-e", "const DB=require('better-sqlite3');const db=new DB(':memory:');db.exec('CREATE TABLE t(x INTEGER)');db.close();require('protobufjs');console.log('EveJS native Node dependency smoke test PASS');"], cwd=stage / "server", env=env)

    if (stage / "_local/newDatabase").exists():
        run([str(npm), "run", "db:migrate-legacy"], cwd=stage / "server", env=env)
    legacy_image_roots=[stage/"server/src/_secondary/image/generated/characters",stage/"server/src/_secondary/image/generated/alliances"]
    if any(p.exists() for p in legacy_image_roots):
        run([str(npm), "run", "images:migrate", "--", "--quiet"], cwd=stage / "server", env=env)

    migrating_lu = (existing / "_local/evejs-linux-integrated-lu/install.json").is_file()
    if migrating_lu:
        # Keep user-owned LU configuration, but archive the old manager record/backups in the staged world.
        for rel in ("config/xeve.json", "evejs.config.x-eve.local.json"):
            src=existing/rel
            if src.is_file():
                dst=stage/rel; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dst)
        old_state=stage/"_local/evejs-linux-integrated-lu"
        if old_state.exists():
            legacy=stage/"_local/evejs-linux-integrated-lu-legacy-v1.11"
            if legacy.exists(): shutil.rmtree(legacy)
            old_state.rename(legacy)
        print("\n==> Applying the new v0.12.7.1 Integrated Living Universe authority inside the staging tree")
        run([sys.executable, str(INTEGRATED_LU_MANAGER_SOURCE), "install", "--repo", str(stage)])
        run([sys.executable, str(INTEGRATED_LU_MANAGER_SOURCE), "verify", "--repo", str(stage)])

    print("\n==> Building the v0.12.7.1 Market daemon without reseeding")
    market_dir = stage / "externalservices/market-server"
    run([str(cargo), "build", "--release"], cwd=market_dir, env=env)

    shutil.copy2(LAUNCHER_SOURCE, stage / "evejs-linux-launcher.sh")
    shutil.copy2(CLEANUP_SOURCE, stage / "evejs-stale-process-cleanup.sh")
    tools_dir = stage / ".evejs-linux-tools"; (tools_dir / "compatibility").mkdir(parents=True, exist_ok=True)
    shutil.copy2(COMPAT_SOURCE, tools_dir / "evejs-linux-lu-compat.py")
    for compat_file in COMPAT_DATA_SOURCE.iterdir():
        if compat_file.is_file(): shutil.copy2(compat_file, tools_dir / "compatibility" / compat_file.name)
    shutil.copy2(INTEGRATED_LU_MANAGER_SOURCE, tools_dir / "evejs-linux-integrated-lu.py")
    if (tools_dir / "integrated-lu").exists(): shutil.rmtree(tools_dir / "integrated-lu")
    shutil.copytree(INTEGRATED_LU_DATA_SOURCE, tools_dir / "integrated-lu")
    (tools_dir / "evejs-linux-integrated-lu.py").chmod(0o755)

    (stage / "evejs-linux-launcher.sh").chmod(0o755); (stage / "evejs-stale-process-cleanup.sh").chmod(0o755); (tools_dir / "evejs-linux-lu-compat.py").chmod(0o755)

    updated_conf=dict(conf); updated_conf.update({"EVEJS_REPO":str(existing),"EVEJS_VERSION":TARGET_VERSION,"SUPPORTED_BUILD":str(TARGET_BUILD),"NODE_BIN":str(node),"NODE_BIN_DIR":str(node.parent),"CARGO_BIN":str(cargo)})
    write_linux_conf(stage / ".evejs-linux.conf", updated_conf)
    (stage / "UPDATE-LINUX-SUMMARY.txt").write_text(
        "EveJS Linux transactional update\n================================\n"
        f"From: {conf.get('EVEJS_VERSION','unknown')}\nTo:   {TARGET_VERSION}\n"
        "Market reseeded: NO\nPlayer/world database preserved: YES (entire _local tree)\n"
        "Market data preserved: YES\nMods preserved: YES\nCertificates/local config preserved: YES\n"
        "Generated static game data rebuilt during update: NO\n"
        "First v0.12.7.1 server start seeds only missing safe shipped static tables.\n"
        "Configuration: three-way merged\n" + ("Living Universe migration: " + ("v1.11 v0.12.7 -> current v0.12.7.1 authority\n" if migrating_lu else "not installed during base update; available through Installer option 3\n"))
    )
    validate_stage_runtime(ui, stage, node, cargo, game_hash, market_hash)
    return stage, merge_report

def do_update(ui: UI, args):
    existing = choose_existing_install(ui, args.existing)
    old_version = read_version(existing)

    if old_version == TARGET_VERSION:
        ui.info(
            "EveJS is already current",
            f"This installation is already EveJS v{TARGET_VERSION}:\n\n{existing}\n\nNo update was performed.",
        )
        return
    if old_version not in SUPPORTED_UPDATE_SOURCES:
        die(
            ui,
            "This update path has not been audited.\n\n"
            f"Existing installation: EveJS v{old_version or 'unknown'}\n"
            f"Target installer:      EveJS v{TARGET_VERSION}\n\n"
            "v1.12 currently supports only:\n"
            "  v0.12.7 → v0.12.7.1\n\n"
            "The updater will not guess across unaudited versions.",
        )

    if not CONTRACT_ROOT.is_dir():
        die(ui, f"Required update contract is missing:\n{CONTRACT_ROOT}")

    conf = load_linux_conf(ui, existing)
    configured_build = str(conf.get("SUPPORTED_BUILD", ""))
    if configured_build != str(TARGET_BUILD):
        die(
            ui,
            f"The existing Linux setup is configured for EVE build {configured_build or 'unknown'}, "
            f"but this update contract requires build {TARGET_BUILD}.",
        )

    tq = Path(conf.get("TQ_PATH", "")).expanduser()
    if not tq.is_dir() or read_client_build(tq) != str(TARGET_BUILD):
        die(
            ui,
            f"The existing copied EVE client is missing or no longer build {TARGET_BUILD}:\n{tq}\n\n"
            "This audited update does not replace the client because v0.12.7 and v0.12.7.1 use the same directly verified build 3396210 contract.",
        )

    zip_path = Path(args.zip).expanduser().resolve() if args.zip else None
    while zip_path is None:
        zip_path = ui.choose_file(f"Select the official EveJS v{TARGET_VERSION} release ZIP", str(SCRIPT_DIR))
        if zip_path is None:
            raise SystemExit(0)

    source_root, source_temp = extract_target_zip(ui, zip_path)
    try:
        verify_old_integrated_lu_for_migration(ui, existing)
        ensure_no_xpatch_overlay_for_update(ui, existing)
        ensure_services_stopped(ui, existing)

        game_db = existing / "_local" / "gameStore" / "gamestore.sqlite"
        market_db = existing / "externalservices" / "market-server" / "data" / "generated" / "market.sqlite"

        print("\n==> Checkpointing and validating existing player databases")
        game_hash = sqlite_checkpoint_integrity(ui, game_db, "existing player/world database")
        market_hash = sqlite_checkpoint_integrity(ui, market_db, "existing Market database")
        if not market_hash:
            die(ui, f"Existing Market database is missing:\n{market_db}\n\nThe updater will not create/reseed one during an update.")

        node = current_tool_path(args, conf, "NODE_BIN", "node")
        cargo = current_tool_path(args, conf, "CARGO_BIN", "cargo")
        if not node.is_file():
            die(ui, f"Node.js runtime not found:\n{node}\n\nRun the update through install-evejs-linux.sh so prerequisites can be prepared.")
        if not cargo.is_file():
            die(ui, f"Cargo runtime not found:\n{cargo}\n\nRun the update through install-evejs-linux.sh so prerequisites can be prepared.")

        node_version = validate_tool_identity(ui, node, "Node.js")
        cargo_version = validate_tool_identity(ui, cargo, "Cargo")
        print(f"    ✓ Updater Node executable: {node} ({node_version.splitlines()[0]})")
        print(f"    ✓ Updater Cargo executable: {cargo} ({cargo_version.splitlines()[0]})")

        stage = None
        stage_pattern = f".evejs-update-stage-{existing.name}-*"
        stages_before = {p.resolve() for p in existing.parent.glob(stage_pattern) if p.is_dir()}
        try:
            try:
                stage, merge_report = prepare_stage(ui, existing, source_root, conf, node, cargo, game_hash, market_hash)
            except BaseException:
                # prepare_stage creates its sibling stage before doing any build
                # work. If it fails before returning, `stage` is still None in
                # this caller, so explicitly remove only stages created by this
                # attempt. Pre-existing evidence folders are left untouched.
                stages_after = {p.resolve() for p in existing.parent.glob(stage_pattern) if p.is_dir()}
                for orphan in stages_after - stages_before:
                    shutil.rmtree(orphan, ignore_errors=True)
                raise

            backup_base = backup_base_for(existing)
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            record_dir = backup_base / f"{stamp}-{old_version}-to-{TARGET_VERSION}"
            backup_install = record_dir / "installation"

            mods_count = tree_file_count(existing / "mods")
            mods_manifest_sha256 = manifest_digest(tree_manifest(existing / "mods"))
            summary = (
                f"Existing EveJS:\n  v{old_version}\n  {existing}\n\n"
                f"Target EveJS:\n  v{TARGET_VERSION}\n  {zip_path}\n\n"
                "Protected state:\n"
                f"  Player/world DB: {'present' if game_hash else 'not yet created'}\n"
                f"  Market DB:       preserved exactly ({market_hash[:16]}…)\n"
                f"  Mod files:       {mods_count} (exact manifest {mods_manifest_sha256[:16]}…)\n"
                "  _local runtime/mod state: preserved (including Content Packs)\n"
                "  Living Universe handling follows this package edition and is fail-closed\n"
                "  Certificates: preserved\n"
                "  EVE client + Proton prefix: retained\n"
                "  Market reseed: NO\n\n"
                f"Configuration merge conflicts: {len(merge_report['conflicts'])}\n"
                "  (when both EveJS and the user changed the same setting, the user value wins)\n\n"
                "The complete old EveJS installation will become an atomic rollback backup at:\n"
                f"  {record_dir}\n\n"
                "The installation path itself will remain unchanged."
            )
            if not ui.yesno("Confirm EveJS update", summary + "\n\nCommit this update?"):
                shutil.rmtree(stage, ignore_errors=True)
                return

            record_dir.mkdir(parents=True, exist_ok=False)
            metadata = {
                "schema": 1,
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                "source_version": old_version,
                "target_version": TARGET_VERSION,
                "supported_build": TARGET_BUILD,
                "original_path": str(existing),
                "source_zip": str(zip_path),
                "source_zip_sha256": sha256(zip_path),
                "player_db_sha256": game_hash,
                "market_db_sha256": market_hash,
                "mods_file_count": mods_count,
                "mods_manifest_sha256": mods_manifest_sha256,
                "config_merge_conflicts": merge_report["conflicts"],
                "status": "committing",
            }
            write_metadata(record_dir / "metadata.json", metadata)

            print("\n==> Committing transactional update")
            try:
                os.rename(existing, backup_install)
                os.rename(stage, existing)
                stage = None
            except Exception as exc:
                if not existing.exists() and backup_install.exists():
                    os.rename(backup_install, existing)
                metadata["status"] = "commit-failed"
                metadata["error"] = str(exc)
                write_metadata(record_dir / "metadata.json", metadata)
                die(ui, f"The filesystem commit failed and the old installation was restored.\n\n{exc}")

            # Post-commit validation. ANY failure (our own validation,
            # Market doctor subprocess failure, filesystem error, etc.) rolls
            # back before the updater can return control to the user.
            try:
                validate_stage_runtime(ui, existing, node, cargo, game_hash, market_hash)
                compat_helper = existing / ".evejs-linux-tools" / "evejs-linux-lu-compat.py"
                candidate_market = existing / "externalservices" / "market-server" / "target" / "release" / "market-server"
                run([
                    sys.executable, str(compat_helper), "prepare",
                    "--repo", str(existing),
                    "--cargo", str(cargo),
                    "--candidate-binary", str(candidate_market),
                ])
            except (Exception, SystemExit) as exc:
                failed_target = record_dir / "failed-target"
                rollback_error = None
                try:
                    if existing.exists():
                        os.rename(existing, failed_target)
                    os.rename(backup_install, existing)
                    metadata["status"] = "rolled-back"
                    metadata["validation_error"] = str(exc)
                    write_metadata(record_dir / "metadata.json", metadata)
                except Exception as rb_exc:
                    rollback_error = rb_exc

                if rollback_error is not None:
                    die(
                        ui,
                        "Post-update validation failed AND automatic rollback could not complete.\n\n"
                        f"Validation error:\n{exc}\n\n"
                        f"Rollback error:\n{rollback_error}\n\n"
                        f"Backup record:\n{record_dir}",
                    )

                ui.error(
                    "Update rolled back",
                    "Post-update validation failed.\n\n"
                    f"EveJS v{old_version} has been restored to:\n{existing}\n\n"
                    f"The failed target tree was retained at:\n{failed_target}\n\n"
                    f"Validation error:\n{exc}",
                )
                raise SystemExit(1)

            metadata["status"] = "verified"
            write_metadata(record_dir / "metadata.json", metadata)
            update_desktop_entry(existing, old_version, TARGET_VERSION)

            history = existing / "UPDATE-HISTORY.jsonl"
            with history.open("a") as f:
                f.write(json.dumps({
                    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "from": old_version,
                    "to": TARGET_VERSION,
                    "backup": str(record_dir),
                    "player_db_sha256": game_hash,
                    "market_db_sha256": market_hash,
                    "config_conflicts": len(merge_report["conflicts"]),
                }, sort_keys=True) + "\n")

            ui.info(
                "EveJS update complete",
                f"EveJS was updated successfully:\n\n"
                f"  v{old_version} → v{TARGET_VERSION}\n\n"
                f"Installation path retained:\n{existing}\n\n"
                "Preserved:\n"
                "• player/world database\n"
                "• Market database and player orders/history\n"
                "• mods and mod-owned _local state (including Content Packs)\n"
                "• certificates\n"
                "• EVE client and Proton prefix\n• source-matched external Market runtime\n\n"
                f"Rollback backup:\n{record_dir}\n\n"
                "Use this Installer's Restore/Manage Backup options if that backup is ever needed.\n\n"
                "Launch EveJS normally with the existing evejs-linux-launcher.sh.",
            )
        finally:
            if stage and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
    finally:
        source_temp.cleanup()


def validate_backup_copy(ui: UI, path: Path, meta: dict):
    if not path.is_dir() or not (path / "config/version.json").is_file():
        die(ui, f"Backup installation is incomplete:\n{path}")
    expected = str(meta.get("source_version") or "")
    actual = read_version(path)
    if expected and actual != expected:
        die(ui, f"Backup version mismatch.\n\nMetadata: {expected}\nBackup files: {actual}")
    sqlite_checkpoint_integrity(ui, path / "_local/gameStore/gamestore.sqlite", "backup player/world database")
    sqlite_checkpoint_integrity(
        ui,
        path / "externalservices/market-server/data/generated/market.sqlite",
        "backup Market database",
    )


def do_restore(ui: UI, args):
    existing = choose_existing_install(ui, args.existing)
    ensure_services_stopped(ui, existing)
    records = backup_records(existing)
    if not records:
        die(ui, f"No upgrade backups were found for:\n{existing}\n\nExpected under:\n{backup_base_for(existing)}")

    selected = None
    if args.backup:
        p = Path(args.backup).expanduser().resolve()
        selected = next((r for r in records if r["record_dir"].resolve() == p), None)
        if selected is None:
            die(ui, f"The requested backup does not belong to this EveJS installation:\n{p}")
    else:
        selected = ui.choose_backup("Restore EveJS upgrade backup", records)
        if selected is None:
            return

    validate_backup_copy(ui, selected["installation"], selected["meta"])
    current_version = read_version(existing)
    backup_version = read_version(selected["installation"])

    if not ui.yesno(
        "Confirm EveJS backup restore",
        f"Current installation:\n  v{current_version}\n  {existing}\n\n"
        f"Restore backup:\n  v{backup_version}\n  {selected['record_dir']}\n\n"
        "The current installation will first be preserved as a new safety backup.\n"
        "The selected backup itself will remain available after restore.",
    ):
        return

    parent = existing.parent
    stage = parent / f".evejs-restore-stage-{existing.name}-{uuid.uuid4().hex[:10]}"
    copy_reflink(selected["installation"], stage)
    validate_backup_copy(ui, stage, selected["meta"])

    base = backup_base_for(existing)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safety = base / f"{stamp}-pre-restore-{current_version}"
    safety.mkdir(parents=True, exist_ok=False)
    safety_install = safety / "installation"
    safety_meta = {
        "schema": 1,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_version": current_version,
        "target_version": f"restore-to-{backup_version}",
        "supported_build": read_supported_build(existing),
        "original_path": str(existing),
        "status": "pre-restore-safety",
    }
    write_metadata(safety / "metadata.json", safety_meta)

    try:
        os.rename(existing, safety_install)
        os.rename(stage, existing)
    except Exception as exc:
        if not existing.exists() and safety_install.exists():
            os.rename(safety_install, existing)
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        die(ui, f"Restore commit failed; the previous installation was put back.\n\n{exc}")

    try:
        validate_backup_copy(ui, existing, selected["meta"])
    except (Exception, SystemExit) as exc:
        failed = safety / "failed-restore-target"
        rollback_error = None
        try:
            if existing.exists():
                os.rename(existing, failed)
            os.rename(safety_install, existing)
            safety_meta["status"] = "restore-rolled-back"
            safety_meta["validation_error"] = str(exc)
            write_metadata(safety / "metadata.json", safety_meta)
        except Exception as rb_exc:
            rollback_error = rb_exc

        if rollback_error is not None:
            die(
                ui,
                "Restore validation failed AND the pre-restore installation could not be returned automatically.\n\n"
                f"Validation error:\n{exc}\n\nRollback error:\n{rollback_error}\n\nSafety record:\n{safety}",
            )

        ui.error(
            "Restore rolled back",
            f"The restore failed validation. EveJS v{current_version} was restored.\n\nValidation error:\n{exc}",
        )
        raise SystemExit(1)

    safety_meta["status"] = "verified-pre-restore-safety"
    write_metadata(safety / "metadata.json", safety_meta)
    ui.info(
        "EveJS backup restored",
        f"Restored EveJS v{backup_version} to:\n{existing}\n\n"
        f"The installation that was present before restore is safely backed up at:\n{safety}\n\n"
        "The selected original upgrade backup was not consumed.",
    )


def do_backups(ui: UI, args):
    existing = choose_existing_install(ui, args.existing)
    records = backup_records(existing)
    if not records:
        ui.info("EveJS upgrade backups", f"No upgrade backups were found for:\n{existing}\n\nExpected under:\n{backup_base_for(existing)}")
        return
    selected = ui.choose_backup("Manage EveJS upgrade backups", records)
    if selected is None:
        return
    action = ui.choose_backup_action()
    if action == "back":
        return
    if action == "inspect":
        meta = selected["meta"]
        size = subprocess.run(["du", "-sh", str(selected["record_dir"])], text=True, capture_output=True).stdout.split("\t")[0]
        ui.info(
            "EveJS upgrade backup",
            f"Path:\n{selected['record_dir']}\n\n"
            f"Source version: {meta.get('source_version','?')}\n"
            f"Target version: {meta.get('target_version','?')}\n"
            f"Status: {meta.get('status','?')}\n"
            f"Created: {meta.get('timestamp','?')}\n"
            f"Size: {size or 'unknown'}\n"
            f"Player DB SHA-256: {meta.get('player_db_sha256') or 'not recorded'}\n"
            f"Market DB SHA-256: {meta.get('market_db_sha256') or 'not recorded'}\n"
            f"Mod files at backup: {meta.get('mods_file_count','not recorded')}",
        )
        return
    if action == "delete":
        if ui.yesno(
            "Delete EveJS upgrade backup?",
            f"This permanently deletes:\n\n{selected['record_dir']}\n\n"
            "It does not modify the current EveJS installation.\n\n"
            "Delete this backup?",
        ):
            shutil.rmtree(selected["record_dir"])
            ui.info("Backup deleted", f"Deleted:\n{selected['record_dir']}")


def self_test():
    report = {"tests": []}

    old = {"characters": {"x": 1}, "combat": MISSING}
    # Direct focused merge tests.
    r = {"target": [], "user": [], "conflicts": []}
    merged = json_merge(
        {"characters": {"x": 1}},
        {"characters": {"x": 1}},
        {"characters": {"x": 1}, "combat": {"turretHitChanceAdditive": 0.5}},
        "gameplay.json",
        r,
    )
    assert merged["combat"]["turretHitChanceAdditive"] == 0.5
    report["tests"].append("new target setting added")

    r = {"target": [], "user": [], "conflicts": []}
    merged = json_merge(
        {"development": {"playerConnectToken": "test"}},
        {"development": {"playerConnectToken": "my-private-token"}},
        {"development": {"playerConnectToken": ""}},
        "server.json",
        r,
    )
    assert merged["development"]["playerConnectToken"] == "my-private-token"
    assert r["conflicts"]
    report["tests"].append("user setting wins true three-way conflict")

    r = {"target": [], "user": [], "conflicts": []}
    merged = json_merge(
        {"development": {"playerConnectToken": "test"}},
        {"development": {"playerConnectToken": "test"}},
        {"development": {"playerConnectToken": ""}},
        "server.json",
        r,
    )
    assert merged["development"]["playerConnectToken"] == ""
    report["tests"].append("unchanged user setting adopts target default")

    dummy = Path("/tmp/example/EveJS")
    assert backup_base_for(dummy) == Path("/tmp/example/.evejs-upgrade-backups/EveJS")
    report["tests"].append("backup path stays sibling to installation")

    print(json.dumps({"status": "PASS", **report}, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["update", "restore", "backups"])
    ap.add_argument("--text", action="store_true")
    ap.add_argument("--zip")
    ap.add_argument("--existing")
    ap.add_argument("--backup")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return

    if not args.mode:
        raise SystemExit("--mode is required when invoked by the Installer.")

    ui = UI(args.text)

    if args.mode == "update":
        do_update(ui, args)
    elif args.mode == "restore":
        do_restore(ui, args)
    elif args.mode == "backups":
        do_backups(ui, args)


if __name__ == "__main__":
    main()
