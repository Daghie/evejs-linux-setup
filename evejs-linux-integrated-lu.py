#!/usr/bin/env python3
"""Transactional Integrated Living Universe manager for EveJS v0.12.7.1.

This does NOT invoke xeve-patch.  It installs the exact audited delta derived from
living-universe-dac-dev.zip over a clean EveJS v0.12.7.1 source tree, with backups and rollback.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

TARGET_VERSION = "0.12.7.1"
TARGET_BUILD = 3396210
SCRIPT_DIR = Path(__file__).absolute().parent
DATA_ROOT = SCRIPT_DIR / "integrated-lu"
MANIFEST_PATH = DATA_ROOT / "manifest.json"
PAYLOAD_ROOT = DATA_ROOT / "files"
STATE_ROOT_REL = Path("_local/evejs-linux-integrated-lu")
INSTALL_RECORD_REL = STATE_ROOT_REL / "install.json"
OLD_LOADER_REL = Path("mods/x-eve-living-universe/loader.js")
OLD_XPATCH_REL = Path("_local/x-eve-patch/install.json")


class LUError(RuntimeError):
    pass


MISSING = object()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest().upper()


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        raise LUError(f"Could not parse JSON: {path}\n{exc}") from exc


def atomic_write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".new-", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_copy(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=dst.name + ".new-", dir=dst.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(src, tmp, follow_symlinks=False)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def validate_relpath(text: str) -> Path:
    p = Path(text)
    if not text or p.is_absolute() or ".." in p.parts:
        raise LUError(f"Unsafe payload path in manifest: {text!r}")
    return p


def read_version(repo: Path) -> str:
    d = load_json(repo / "config/version.json")
    return str(d.get("evejsVersion") or d.get("version") or "")


def read_build(repo: Path) -> int | None:
    try:
        return int(load_json(repo / "tools/ClientSETUP/blue_patch_recipe.json")["supportedBuild"])
    except Exception:
        return None


def manifest() -> dict:
    d = load_json(MANIFEST_PATH)
    if d.get("schema") != "evejs-linux-integrated-lu/1":
        raise LUError(f"Unsupported Integrated LU manifest schema: {d.get('schema')!r}")
    return d


def authority_id(m: dict) -> str:
    return str(m["authority"]["sourceArchiveSha256"]).upper()


def verify_release_integrity(m: dict):
    seen: set[str] = set()
    for entry in m.get("files", []):
        rel_text = str(entry.get("path") or "")
        validate_relpath(rel_text)
        if rel_text in seen:
            raise LUError(f"Duplicate payload path in manifest: {rel_text}")
        seen.add(rel_text)
        src = PAYLOAD_ROOT / rel_text
        if not src.is_file() or src.is_symlink():
            raise LUError(f"Integrated LU payload file missing/not regular: {src}")
        actual_size = src.stat().st_size
        actual_hash = sha256_file(src)
        expected = entry.get("installed") or {}
        if actual_size != int(expected.get("size", -1)) or actual_hash != str(expected.get("sha256", "")).upper():
            raise LUError(
                f"Integrated LU payload integrity failure: {rel_text}\n"
                f"Expected: {expected.get('sha256')} / {expected.get('size')}\n"
                f"Actual:   {actual_hash} / {actual_size}"
            )


def validate_repo(repo: Path):
    repo = repo.expanduser().absolute()
    if not repo.is_dir() or repo.is_symlink():
        raise LUError(f"EveJS directory not found or is a symlink: {repo}")
    if read_version(repo) != TARGET_VERSION:
        raise LUError(f"Integrated LU requires EveJS {TARGET_VERSION}: {repo}")
    if read_build(repo) != TARGET_BUILD:
        raise LUError(f"Expected EVE client build contract {TARGET_BUILD}.")
    if not (repo / "server/index.js").is_file():
        raise LUError(f"Not a recognizable EveJS source tree: {repo}")
    return repo


def conflict_paths(repo: Path) -> list[str]:
    out = []
    if (repo / OLD_LOADER_REL).is_file():
        out.append(OLD_LOADER_REL.as_posix())
    if (repo / OLD_XPATCH_REL).is_file():
        out.append(OLD_XPATCH_REL.as_posix())
    return out


def exact_installed_matches(repo: Path, m: dict) -> tuple[bool, list[str]]:
    bad = []
    for e in m["files"]:
        if e["kind"] in {"seeded-json", "merged-json"}:
            continue
        p = repo / e["path"]
        want = str(e["installed"]["sha256"]).upper()
        if not p.is_file() or sha256_file(p) != want:
            bad.append(e["path"])
    return not bad, bad


def clean_installable(repo: Path, m: dict) -> tuple[bool, list[str]]:
    bad = []
    for e in m["files"]:
        rel = e["path"]
        p = repo / rel
        kind = e["kind"]
        if kind == "seeded-json":
            if p.exists() and not p.is_file():
                bad.append(f"{rel}: exists but is not a regular file")
            elif p.is_file():
                try:
                    load_json(p)
                except LUError:
                    bad.append(f"{rel}: existing user settings are not valid JSON")
            continue
        if kind == "merged-json":
            baseline = e.get("baseline") or {}
            if not p.is_file():
                bad.append(f"{rel}: missing baseline config")
            else:
                try:
                    load_json(p)
                except LUError:
                    bad.append(f"{rel}: current config is not valid JSON")
            # User edits are allowed on merged JSON. The clean EveJS version/build gate
            # plus the rest of the exact baseline source protects against wrong releases.
            continue
        if kind == "modified":
            baseline = e.get("baseline") or {}
            want = str(baseline.get("sha256") or "").upper()
            if not p.is_file() or sha256_file(p) != want:
                bad.append(f"{rel}: not the audited clean v0.12.7.1 baseline")
        elif kind == "added":
            if p.exists():
                bad.append(f"{rel}: LU-added path already exists")
        else:
            bad.append(f"{rel}: unknown manifest kind {kind!r}")
    return not bad, bad


def status(repo: Path) -> dict:
    m = manifest()
    verify_release_integrity(m)
    repo = validate_repo(repo)
    conflicts = conflict_paths(repo)
    record_path = repo / INSTALL_RECORD_REL
    if conflicts:
        return {
            "mode": "conflict",
            "repo": str(repo),
            "label": "Unsupported older Living Universe/X-Patch state",
            "conflicts": conflicts,
            "authoritySha256": authority_id(m),
        }
    if record_path.is_file():
        record = load_json(record_path)
        if str(record.get("authoritySha256", "")).upper() != authority_id(m):
            return {
                "mode": "managed-other-authority",
                "repo": str(repo),
                "label": "Installer-managed Integrated LU from another authority",
                "recordedAuthoritySha256": record.get("authoritySha256", ""),
                "authoritySha256": authority_id(m),
            }
        ok, bad = verify_managed(repo, m, record, raise_on_drift=False)
        return {
            "mode": "integrated-current" if ok else "integrated-drift",
            "repo": str(repo),
            "label": "Integrated Living Universe for EveJS v0.12.7.1" if ok else "Integrated Living Universe with managed-file drift",
            "authoritySha256": authority_id(m),
            "drift": bad,
        }
    exact, exact_bad = exact_installed_matches(repo, m)
    if exact:
        return {
            "mode": "integrated-current-unmanaged",
            "repo": str(repo),
            "label": "Exact current Integrated LU source detected without Universal Setup install record",
            "authoritySha256": authority_id(m),
        }
    installable, bad = clean_installable(repo, m)
    if installable:
        return {
            "mode": "clean-installable",
            "repo": str(repo),
            "label": "Clean/custom-config EveJS v0.12.7.1 ready for Integrated LU",
            "authoritySha256": authority_id(m),
        }
    return {
        "mode": "unknown-or-partial",
        "repo": str(repo),
        "label": "Unknown or partially modified EveJS/LU source state",
        "authoritySha256": authority_id(m),
        "problems": bad[:50],
        "exactIntegratedMismatches": exact_bad[:50],
    }


def json_equal(a, b) -> bool:
    return a == b


def merge_three_way(old, current, new, path=""):
    # Dicts are merged recursively so stock->LU additions arrive while user values win.
    if isinstance(old, dict) and isinstance(current, dict) and isinstance(new, dict):
        out = {}
        keys = set(old) | set(current) | set(new)
        for key in sorted(keys):
            o = old.get(key, MISSING); c = current.get(key, MISSING); n = new.get(key, MISSING)
            child = f"{path}.{key}" if path else key
            value = merge_three_way(o, c, n, child)
            if value is not MISSING:
                out[key] = value
        return out
    if current is MISSING:
        if old is MISSING:
            return copy.deepcopy(new)
        if new is MISSING:
            return MISSING
        # User removed an old key: preserve the deletion unless LU introduces a key that did not exist.
        return MISSING
    if old is MISSING:
        if new is MISSING:
            return copy.deepcopy(current)
        # New LU key: adopt it only when the user has not already authored one.
        return copy.deepcopy(current)
    if new is MISSING:
        # LU removed a stock key. Remove it only if the user left the stock value untouched.
        return MISSING if json_equal(current, old) else copy.deepcopy(current)
    if json_equal(current, old):
        return copy.deepcopy(new)
    # User changed it. Their value wins even when LU also changed the stock default.
    return copy.deepcopy(current)


def write_json_atomic(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".new-", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def verify_managed(repo: Path, m: dict, record: dict, raise_on_drift=True) -> tuple[bool, list[str]]:
    by_path = {r["path"]: r for r in record.get("files", [])}
    bad = []
    for e in m["files"]:
        rel = e["path"]; kind = e["kind"]; p = repo / rel
        rr = by_path.get(rel, {})
        if kind in {"modified", "added"}:
            want = str(e["installed"]["sha256"]).upper()
            if not p.is_file() or sha256_file(p) != want:
                bad.append(rel)
        elif kind == "merged-json":
            # Config is user-editable after installation; require valid JSON and presence,
            # but do not make Launcher activation depend on a user's chosen values.
            try:
                load_json(p)
            except Exception:
                bad.append(rel)
        elif kind == "seeded-json":
            if p.exists():
                try:
                    load_json(p)
                except Exception:
                    bad.append(rel)
    if bad and raise_on_drift:
        raise LUError("Integrated LU verification failed. Managed/runtime files with drift:\n  - " + "\n  - ".join(bad))
    return not bad, bad


def install(repo: Path) -> dict:
    m = manifest(); verify_release_integrity(m); repo = validate_repo(repo)
    st = status(repo)
    if st["mode"] == "integrated-current":
        record = load_json(repo / INSTALL_RECORD_REL)
        verify_managed(repo, m, record)
        return {**st, "changed": False, "message": "Integrated LU is already installed and verified."}
    if st["mode"] == "integrated-current-unmanaged":
        raise LUError(
            "This tree already matches the current Integrated LU source but has no Universal Setup backup/install record.\n"
            "It can be run after compatibility preparation, but Setup will not claim ownership or invent uninstall backups."
        )
    if st["mode"] != "clean-installable":
        detail = st.get("problems") or st.get("conflicts") or st.get("drift") or []
        raise LUError(f"Integrated LU install refused: {st['label']}\n" + "\n".join(f"  - {x}" for x in detail))

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    state_root = repo / STATE_ROOT_REL
    backup_root = state_root / "backups" / stamp
    backup_root.mkdir(parents=True, exist_ok=False)
    changed_paths: list[tuple[str, str]] = []
    record_files = []
    try:
        # Back up every existing file this install can change before writing anything.
        for e in m["files"]:
            rel = e["path"]; kind = e["kind"]; dst = repo / rel
            existed = dst.is_file()
            backup_rel = None
            if existed and kind in {"modified", "merged-json", "seeded-json"}:
                backup = backup_root / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, backup, follow_symlinks=False)
                backup_rel = backup.relative_to(state_root).as_posix()
            record_files.append({"path": rel, "kind": kind, "existedBefore": existed, "backup": backup_rel})

        for e, rr in zip(m["files"], record_files):
            rel=e["path"]; kind=e["kind"]; src=PAYLOAD_ROOT/rel; dst=repo/rel
            if kind in {"modified", "added"}:
                atomic_copy(src, dst); changed_paths.append((rel, kind))
            elif kind == "merged-json":
                baseline_path = Path('/nonexistent')
                # The clean stock bytes aren't separately shipped. Reconstruct old JSON from the
                # backup, but only if it is byte-identical to stock; when user-edited, derive OLD
                # from a compact baseline snapshot stored in the manifest below.
                old = e.get("baselineJson")
                if old is None:
                    # baselineJson is mandatory for merged files in v1.12 manifests.
                    raise LUError(f"Manifest lacks baselineJson for merged config: {rel}")
                current = load_json(dst)
                new = load_json(src)
                merged = merge_three_way(old, current, new)
                write_json_atomic(dst, merged); changed_paths.append((rel, kind))
                rr["installedResultSha256"] = sha256_file(dst)
            elif kind == "seeded-json":
                if not dst.exists():
                    atomic_copy(src, dst); changed_paths.append((rel, kind))
                    rr["createdByInstall"] = True
                else:
                    rr["createdByInstall"] = False
            else:
                raise LUError(f"Unknown Integrated LU file kind: {kind}")

        record = {
            "schema": "evejs-linux-integrated-lu-install/1",
            "installedAtUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "authorityLabel": m["authority"]["label"],
            "authoritySha256": authority_id(m),
            "evejsVersion": TARGET_VERSION,
            "eveClientBuild": TARGET_BUILD,
            "backupRoot": backup_root.relative_to(state_root).as_posix(),
            "files": record_files,
            "worldStatePolicy": "source install only; gamestore.sqlite and market.sqlite are not modified",
        }
        verify_managed(repo, m, record)
        atomic_write_json(repo / INSTALL_RECORD_REL, record)
        return {
            "mode": "integrated-current",
            "repo": str(repo),
            "label": "Integrated Living Universe for EveJS v0.12.7.1",
            "authoritySha256": authority_id(m),
            "changed": True,
            "backup": str(backup_root),
            "filesInstalled": len(m["files"]),
        }
    except Exception:
        # Roll back using the pre-write backups and the record of what existed.
        for rr in reversed(record_files):
            rel=rr["path"]; dst=repo/rel
            if rr.get("backup"):
                backup=state_root/rr["backup"]
                if backup.is_file(): atomic_copy(backup,dst)
            elif not rr.get("existedBefore"):
                if dst.is_file() or dst.is_symlink(): dst.unlink(missing_ok=True)
        (repo / INSTALL_RECORD_REL).unlink(missing_ok=True)
        raise


def verify(repo: Path) -> dict:
    m=manifest(); verify_release_integrity(m); repo=validate_repo(repo)
    st=status(repo)
    if st["mode"] == "integrated-current-unmanaged":
        return {**st,"verified":True,"managed":False}
    if st["mode"] != "integrated-current":
        raise LUError(f"Integrated LU is not in a verified current state: {st['label']}")
    record=load_json(repo/INSTALL_RECORD_REL)
    verify_managed(repo,m,record)
    return {**st,"verified":True,"managed":True}


def uninstall(repo: Path) -> dict:
    m=manifest(); verify_release_integrity(m); repo=validate_repo(repo)
    record_path=repo/INSTALL_RECORD_REL
    if not record_path.is_file():
        raise LUError("No Universal Setup Integrated LU install record exists; uninstall refused.")
    record=load_json(record_path)
    if str(record.get("authoritySha256","")).upper()!=authority_id(m):
        raise LUError("Installed LU authority differs from this Setup package; uninstall with the matching authority.")
    state_root=repo/STATE_ROOT_REL
    by_path={r["path"]:r for r in record.get("files",[])}
    # Refuse to overwrite post-install edits to owned/merged source.
    drift=[]
    for e in m["files"]:
        rel=e["path"]; kind=e["kind"]; p=repo/rel; rr=by_path.get(rel,{})
        if kind in {"modified","added"}:
            if not p.is_file() or sha256_file(p)!=str(e["installed"]["sha256"]).upper(): drift.append(rel)
        elif kind=="merged-json":
            want=rr.get("installedResultSha256")
            if not p.is_file() or (want and sha256_file(p)!=str(want).upper()): drift.append(rel)
    if drift:
        raise LUError(
            "Integrated LU uninstall refused because files changed after installation.\n"
            "No files were changed. Preserve/reconcile these edits first:\n  - " + "\n  - ".join(drift)
        )
    for e in reversed(m["files"]):
        rel=e["path"]; kind=e["kind"]; dst=repo/rel; rr=by_path.get(rel,{})
        backup_rel=rr.get("backup")
        if kind in {"modified","merged-json"}:
            if not backup_rel: raise LUError(f"Missing uninstall backup record for {rel}")
            backup=state_root/backup_rel
            if not backup.is_file(): raise LUError(f"Missing uninstall backup file: {backup}")
            atomic_copy(backup,dst)
        elif kind=="added":
            dst.unlink(missing_ok=True)
        elif kind=="seeded-json":
            if rr.get("existedBefore"):
                # This was user-owned before LU; leave the current user file untouched.
                pass
            elif rr.get("createdByInstall") and dst.is_file():
                # Remove only if it still equals the seeded authority; otherwise it became user-owned.
                if sha256_file(dst)==str(e["installed"]["sha256"]).upper(): dst.unlink()
    record_path.unlink(missing_ok=True)
    return {"mode":"clean-restored","repo":str(repo),"label":"Integrated LU source removed; clean EveJS source restored","backupPreserved":str(state_root/record.get('backupRoot',''))}


def emit(value: dict, as_json: bool):
    if as_json:
        print(json.dumps(value, indent=2))
    else:
        print(value.get("label") or value.get("mode"))
        print(f"Mode: {value.get('mode')}")
        if value.get("authoritySha256"): print(f"Authority: {value['authoritySha256']}")
        if value.get("backup"): print(f"Backup: {value['backup']}")
        for key in ("conflicts","problems","drift"):
            if value.get(key):
                print(key.capitalize()+":")
                for row in value[key]: print(f"  - {row}")


def self_test():
    old={"a":1,"nest":{"x":1,"keep":2}}
    new={"a":2,"nest":{"x":3,"keep":2,"add":9}}
    cur={"a":7,"nest":{"x":1,"keep":8}}
    got=merge_three_way(old,cur,new)
    assert got=={"a":7,"nest":{"x":3,"keep":8,"add":9}},got
    m=manifest(); verify_release_integrity(m)
    assert m["authority"]["evejsVersion"]==TARGET_VERSION
    assert len(m["files"])>500
    print("PASS integrated-LU manager self-test")


def main():
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest="cmd",required=True)
    for cmd in ("status","install","verify","uninstall"):
        s=sub.add_parser(cmd); s.add_argument("--repo",required=True); s.add_argument("--json",action="store_true")
    sub.add_parser("self-test")
    args=p.parse_args()
    try:
        if args.cmd=="self-test": self_test(); return 0
        repo=Path(args.repo)
        result={
            "status":status,
            "install":install,
            "verify":verify,
            "uninstall":uninstall,
        }[args.cmd](repo)
        emit(result,args.json)
        return 0
    except LUError as exc:
        print(f"[INTEGRATED-LU ERROR] {exc}",file=sys.stderr)
        return 2

if __name__=="__main__":
    raise SystemExit(main())
