#!/usr/bin/env python3
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
import subprocess
import sys
import tempfile

TARGET_VERSION = "0.12.9"
TARGET_BUILD = 3396210
XPATCH_VERSION = "0.3.2"
SETUP_VERSION = "v1.14"
MOD_LU_PATH = Path("mods/x-eve-living-universe/loader.js")
SCRIPT_DIR = Path(__file__).absolute().parent
COMPAT_DIR = SCRIPT_DIR / "compatibility"
STOCK_MANIFEST = COMPAT_DIR / "market-stock-v0.12.9.json"
INTEGRATED_MARKET_MANIFEST = COMPAT_DIR / "market-integrated-lu-v0.12.9.json"
INTEGRATED_LU_DIR = SCRIPT_DIR / "integrated-lu"
INTEGRATED_LU_MANIFEST = INTEGRATED_LU_DIR / "manifest.json"
INTEGRATED_LU_INSTALL = Path("_local/evejs-linux-integrated-lu/install.json")
XPATCH_INSTALL = Path("_local/x-eve-patch/install.json")
LOADER_LU_ROOT = Path("mods/x-eve-living-universe")
XPATCH_SIGNATURES = (
    Path("evejs.config.x-eve.json"),
    Path("server/src/services/xEve/xEveRuntime.js"),
)


class CompatError(RuntimeError):
    pass


def eprint(*args):
    print(*args, file=sys.stderr)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        raise CompatError(f"Could not parse JSON: {path}\n{exc}") from exc


def tree_digest(root: Path) -> str:
    """Read-only content/metadata digest for an installed loader-style mod tree."""
    if not root.exists():
        return ""
    rows = []
    for p in sorted(root.rglob("*"), key=lambda x: x.as_posix()):
        rel = p.relative_to(root).as_posix()
        st = p.lstat()
        mode = oct(st.st_mode & 0o7777)
        if p.is_symlink():
            rows.append([rel, "symlink", os.readlink(p), mode])
        elif p.is_dir():
            rows.append([rel, "dir", mode])
        elif p.is_file():
            rows.append([rel, "file", p.stat().st_size, sha256_file(p), mode])
        else:
            rows.append([rel, "other", mode])
    payload = json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest().upper()


def read_evejs_version(repo: Path) -> str:
    data = load_json(repo / "config/version.json")
    return str(data.get("evejsVersion") or data.get("version") or "")


def read_supported_build(repo: Path) -> int | None:
    try:
        return int(load_json(repo / "tools/ClientSETUP/blue_patch_recipe.json")["supportedBuild"])
    except Exception:
        return None


def build_input_records(market_root: Path) -> list[dict]:
    paths: list[Path] = []
    for rel in ("Cargo.toml", "Cargo.lock", "build.rs"):
        p = market_root / rel
        if p.is_file():
            paths.append(p)
    for relroot in ("src", "crates"):
        base = market_root / relroot
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_file() and (p.suffix == ".rs" or p.name in {"Cargo.toml", "build.rs"}):
                paths.append(p)
    records = []
    for p in sorted(set(paths), key=lambda x: x.as_posix()):
        records.append({
            "path": p.relative_to(market_root).as_posix(),
            "size": p.stat().st_size,
            "sha256": sha256_file(p),
        })
    if not records:
        raise CompatError(f"No Rust market build inputs found under {market_root}")
    return records


def build_source_digest(records: list[dict]) -> str:
    payload = "\n".join(f"{r['path']} {r['sha256']}" for r in records).encode()
    return hashlib.sha256(payload).hexdigest().upper()


def manifest_by_path(records: list[dict]) -> dict[str, dict]:
    return {r["path"]: r for r in records}


def verify_build_manifest(actual: list[dict], manifest_path: Path, label: str) -> tuple[str, dict]:
    expected = load_json(manifest_path)
    expected_records = expected.get("files") or []
    a = manifest_by_path(actual)
    e = manifest_by_path(expected_records)
    missing = sorted(set(e) - set(a))
    extra = sorted(set(a) - set(e))
    changed = sorted(
        p for p in set(a) & set(e)
        if str(a[p].get("sha256", "")).upper() != str(e[p].get("sha256", "")).upper()
        or int(a[p].get("size", -1)) != int(e[p].get("size", -2))
    )
    if missing or extra or changed:
        lines = [f"{label} Rust market source does not match its audited build-input manifest."]
        if missing:
            lines.append("Missing: " + ", ".join(missing))
        if extra:
            lines.append("Unexpected: " + ", ".join(extra))
        if changed:
            lines.append("Changed: " + ", ".join(changed))
        raise CompatError("\n".join(lines))
    digest = build_source_digest(actual)
    expected_digest = str(expected.get("sourceDigest") or "").upper()
    if digest != expected_digest:
        raise CompatError(
            f"{label} source digest mismatch after per-file verification.\n"
            f"Expected: {expected_digest}\nActual:   {digest}"
        )
    return digest, expected



def detect_partial_xpatch(repo: Path) -> list[str]:
    signatures = []
    for rel in XPATCH_SIGNATURES:
        if (repo / rel).exists():
            signatures.append(rel.as_posix())
    rpc = repo / "externalservices/market-server/src/rpc.rs"
    if rpc.is_file() and sha256_file(rpc) == "F4892EC8D94D40B3BA07B52A55B663B700ADFE4FBBF856B093A35F5CF6D8A096":
        signatures.append("externalservices/market-server/src/rpc.rs (X-Patch 0.3.2 hash)")
    return signatures


def classify(repo: Path) -> dict:
    repo = repo.expanduser().absolute()
    if not repo.is_dir():
        raise CompatError(f"EveJS directory not found: {repo}")
    if read_evejs_version(repo) != TARGET_VERSION:
        raise CompatError(f"This Stock-only compatibility build requires EveJS {TARGET_VERSION}: {repo}")
    build = read_supported_build(repo)
    if build != TARGET_BUILD:
        raise CompatError(f"Expected EVE client build contract {TARGET_BUILD}; found {build!r}.")

    mod_lu_present = (repo / MOD_LU_PATH).is_file()
    xpatch_state_present = (repo / XPATCH_INSTALL).is_file()
    integrated_present = (repo / INTEGRATED_LU_INSTALL).is_file()
    if integrated_present:
        raise CompatError(
            "Installer-managed Integrated Living Universe state is present, but this is the Stock-only Setup.\n"
            "Nothing was changed. Use a Living-Universe edition compatible with EveJS v0.12.9 to verify/migrate that installation, "
            "or uninstall LU with the matching authority before using Stock-only."
        )
    if xpatch_state_present:
        state = load_json(repo / XPATCH_INSTALL)
        raise CompatError(
            "Older X-Patch Living Universe state is present and is unsupported by the Stock-only Setup.\n"
            f"Installed X-Patch version: {state.get('pluginVersion')!r}\n"
            f"Recorded EveJS baseline: {state.get('evejsVersion')!r}"
        )
    partial = detect_partial_xpatch(repo)
    if partial:
        raise CompatError(
            "Living-Universe/X-Patch-like source files are present in a Stock-only installation.\n"
            "Launcher activation is blocked rather than guessing about source ownership.\nDetected:\n  - "
            + "\n  - ".join(partial)
        )

    market_root = repo / "externalservices/market-server"
    actual_records = build_input_records(market_root)
    digest, expected = verify_build_manifest(actual_records, STOCK_MANIFEST, "Stock EveJS 0.12.9")
    loader_digest = tree_digest(repo / LOADER_LU_ROOT) if mod_lu_present else ""
    if mod_lu_present:
        mode = "stock-loader-lu-disabled"
        label = "Stock EveJS 0.12.9 (older loader Living Universe preserved but disabled)"
    else:
        mode = "stock"
        label = "Stock EveJS 0.12.9"
    return {
        "repo": str(repo), "mode": mode, "marketProfile": "stock-v0.12.9-market",
        "label": label, "sourceDigest": digest, "marketInputCount": len(actual_records),
        "modLivingUniverseActive": False, "modLivingUniversePresent": mod_lu_present,
        "loaderLivingUniverseEnabled": False, "loaderLivingUniverseDigest": loader_digest,
        "integratedLivingUniverseActive": False, "integratedAuthoritySha256": "",
        "xpatchLivingUniverseActive": False, "xpatchVersion": "", "xpatchTrackedDigest": "",
    }

def runtime_root_for(repo: Path) -> Path:
    repo_abs = repo.expanduser().absolute()
    identity = hashlib.sha256(str(repo_abs).encode()).hexdigest()[:16]
    return repo_abs.parent / ".evejs-linux-runtime" / identity


def cache_dir_for(info: dict) -> Path:
    repo = Path(info["repo"])
    return runtime_root_for(repo) / "market-builds" / info["sourceDigest"].lower()


def cache_manifest_path(info: dict) -> Path:
    return cache_dir_for(info) / "build.json"


def validate_cached_build(info: dict) -> dict:
    manifest_path = cache_manifest_path(info)
    if not manifest_path.is_file():
        raise CompatError(
            "No verified Market runtime build exists for the currently installed source.\n"
            f"Mode: {info['label']}\nSource digest: {info['sourceDigest']}\n\n"
            "Run the EveJS Universal Linux Installer and choose 'Prepare/Verify Runtime Compatibility'."
        )
    manifest = load_json(manifest_path)
    if str(manifest.get("repo")) != info["repo"]:
        raise CompatError("Cached Market build belongs to a different EveJS path.")
    if str(manifest.get("sourceDigest", "")).upper() != info["sourceDigest"]:
        raise CompatError("Cached Market build source digest does not match the installed source.")
    if str(manifest.get("marketProfile")) != info["marketProfile"]:
        raise CompatError("Cached Market build profile does not match the detected Market source profile.")
    binary = Path(str(manifest.get("binary") or ""))
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise CompatError(f"Cached Market executable is missing/not executable: {binary}")
    actual_hash = sha256_file(binary)
    expected_hash = str(manifest.get("binarySha256") or "").upper()
    if actual_hash != expected_hash:
        raise CompatError(
            f"Cached Market executable hash mismatch.\nExpected: {expected_hash}\nActual:   {actual_hash}"
        )
    return {**info, "runtimeRoot": str(runtime_root_for(Path(info["repo"]))), "marketBinary": str(binary), "marketBinarySha256": actual_hash, "buildManifest": str(manifest_path)}


def preserve_executable_path(value: str) -> Path:
    value = os.path.expandvars(os.path.expanduser(value))
    if not os.path.isabs(value):
        found = shutil.which(value)
        if found:
            value = found
    return Path(os.path.abspath(value))


def validate_cargo(path: Path) -> str:
    if not path.is_file():
        raise CompatError(f"Cargo executable not found: {path}")
    cp = subprocess.run([str(path), "--version"], text=True, capture_output=True)
    output = (cp.stdout + "\n" + cp.stderr).strip()
    if cp.returncode != 0 or not re.search(r"(^|\n)cargo\s+\d", output, re.I):
        raise CompatError(f"Configured Cargo path does not execute Cargo: {path}\n{output}")
    return output.splitlines()[0]


def run_doctor(binary: Path, repo: Path):
    market_root = repo / "externalservices/market-server"
    db = market_root / "data/generated/market.sqlite"
    cfg = market_root / "config/market-server.local.toml"
    if not db.is_file():
        raise CompatError(
            f"Market database is missing: {db}\n"
            "Runtime compatibility preparation never seeds/replaces market.sqlite."
        )
    cp = subprocess.run(
        [str(binary), "--config", "config/market-server.local.toml", "doctor"],
        cwd=market_root,
        text=True,
    )
    if cp.returncode != 0:
        raise CompatError(f"Market doctor failed for candidate executable: {binary}")


def copy_build_inputs(repo: Path, destination: Path, records: list[dict]):
    source_root = repo / "externalservices/market-server"
    destination.mkdir(parents=True, exist_ok=True)
    for record in records:
        rel = Path(record["path"])
        src = source_root / rel
        dst = destination / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst, follow_symlinks=False)


def prepare(repo: Path, cargo_arg: str, candidate_binary: str | None = None) -> dict:
    before = classify(repo)
    repo = Path(before["repo"])
    cache_dir = cache_dir_for(before)
    existing_manifest = cache_manifest_path(before)
    if existing_manifest.is_file():
        try:
            result = validate_cached_build(before)
            print(f"[LU-COMPAT] Reusing verified Market build: {result['marketBinary']}", file=sys.stderr)
            return result
        except CompatError:
            pass

    cargo = preserve_executable_path(cargo_arg)
    cargo_version = validate_cargo(cargo)
    records = build_input_records(repo / "externalservices/market-server")

    runtime_root = runtime_root_for(repo)
    runtime_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="market-build-", dir=runtime_root))
    try:
        stage_source = stage / "source"
        stage_target = stage / "target"
        copy_build_inputs(repo, stage_source, records)

        if candidate_binary:
            candidate = Path(candidate_binary).expanduser().absolute()
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                raise CompatError(f"Candidate Market binary not found/executable: {candidate}")
            built_binary = stage / "market-server"
            shutil.copy2(candidate, built_binary)
            built_binary.chmod(0o755)
        else:
            env = os.environ.copy()
            env["CARGO_TARGET_DIR"] = str(stage_target)
            env["PATH"] = f"{cargo.parent}:{env.get('PATH','')}"
            print(f"[LU-COMPAT] Building {before['label']} Market daemon from an external read-only source copy...", file=sys.stderr)
            cp = subprocess.run([str(cargo), "build", "--release", "--locked"], cwd=stage_source, env=env)
            if cp.returncode != 0:
                raise CompatError("Cargo failed while building the external compatibility Market binary.")
            built_binary = stage_target / "release/market-server"
            if not built_binary.is_file():
                raise CompatError(f"Cargo completed without creating {built_binary}")

        run_doctor(built_binary, repo)

        # Prove the preparation operation did not alter the classified source.
        after = classify(repo)
        conserved = (
            after["mode"] == before["mode"]
            and after["marketProfile"] == before["marketProfile"]
            and after["sourceDigest"] == before["sourceDigest"]
            and after.get("loaderLivingUniverseDigest", "") == before.get("loaderLivingUniverseDigest", "")
            and after.get("xpatchTrackedDigest", "") == before.get("xpatchTrackedDigest", "")
            and after.get("integratedAuthoritySha256", "") == before.get("integratedAuthoritySha256", "")
        )
        if not conserved:
            raise CompatError(
                "Installed EveJS/Living Universe files changed during compatibility preparation.\n"
                "The prepared binary will not be accepted."
            )

        final_parent = cache_dir.parent
        final_parent.mkdir(parents=True, exist_ok=True)
        final_stage = final_parent / (cache_dir.name + ".new-" + os.urandom(4).hex())
        final_stage.mkdir(parents=False, exist_ok=False)
        final_bin = final_stage / "market-server"
        shutil.copy2(built_binary, final_bin)
        final_bin.chmod(0o755)
        binary_hash = sha256_file(final_bin)
        record = {
            "schema": "evejs-linux-market-runtime/1",
            "createdAtUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "repo": before["repo"],
            "evejsVersion": TARGET_VERSION,
            "mode": before["mode"],
            "marketProfile": before["marketProfile"],
            "label": before["label"],
            "sourceDigest": before["sourceDigest"],
            "binary": str(cache_dir / "market-server"),
            "binarySha256": binary_hash,
            "cargo": str(cargo),
            "cargoVersion": cargo_version,
            "sourcePolicy": "external-copy-build; installed source files not written",
        }
        (final_stage / "build.json").write_text(json.dumps(record, indent=2) + "\n")
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        os.rename(final_stage, cache_dir)
        result = validate_cached_build(before)
        print(f"[LU-COMPAT] Prepared Market binary: {result['marketBinary']}", file=sys.stderr)
        return result
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def emit(result: dict, as_shell: bool, as_json: bool):
    if as_shell:
        pairs = {
            "EVEJS_RUNTIME_MODE": result.get("mode", ""),
            "EVEJS_RUNTIME_LABEL": result.get("label", ""),
            "EVEJS_MARKET_PROFILE": result.get("marketProfile", ""),
            "EVEJS_MARKET_SOURCE_DIGEST": result.get("sourceDigest", ""),
            "EVEJS_MARKET_RUNTIME_BIN": result.get("marketBinary", ""),
            "EVEJS_MARKET_RUNTIME_SHA256": result.get("marketBinarySha256", ""),
            "EVEJS_RUNTIME_ROOT": result.get("runtimeRoot", ""),
            "EVEJS_XPATCH_VERSION": result.get("xpatchVersion", ""),
            "EVEJS_INTEGRATED_LU_ACTIVE": "1" if result.get("integratedLivingUniverseActive") else "0",
            "EVEJS_INTEGRATED_LU_AUTHORITY": result.get("integratedAuthoritySha256", ""),
        }
        for k, v in pairs.items():
            print(f"{k}={shell_quote(str(v))}")
    elif as_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Mode:          {result.get('label')}")
        print(f"Source digest: {result.get('sourceDigest')}")
        if result.get("marketBinary"):
            print(f"Market binary: {result.get('marketBinary')}")
            print(f"Binary SHA256: {result.get('marketBinarySha256')}")


def self_test():
    stock = load_json(STOCK_MANIFEST)
    assert stock["evejsVersion"] == TARGET_VERSION
    assert stock["kind"] == "stock"
    assert len(stock.get("files") or []) >= 8
    preserve_executable_path("~/.cargo/bin/cargo")
    print("PASS stock-only compatibility helper self-test")

def main():
    ap = argparse.ArgumentParser(description="EveJS v0.12.9 Stock-only runtime compatibility authority")
    sub = ap.add_subparsers(dest="command", required=True)

    for name in ("detect", "verify-runtime"):
        p = sub.add_parser(name)
        p.add_argument("--repo", required=True)
        p.add_argument("--json", action="store_true")
        p.add_argument("--shell", action="store_true")

    p = sub.add_parser("prepare")
    p.add_argument("--repo", required=True)
    p.add_argument("--cargo", required=True)
    p.add_argument("--candidate-binary")
    p.add_argument("--json", action="store_true")
    p.add_argument("--shell", action="store_true")


    sub.add_parser("self-test")
    args = ap.parse_args()

    try:
        if args.command == "self-test":
            self_test()
            return

        repo = Path(args.repo)
        if args.command == "detect":
            result = classify(repo)
        elif args.command == "verify-runtime":
            result = validate_cached_build(classify(repo))
        elif args.command == "prepare":
            result = prepare(repo, args.cargo, args.candidate_binary)
        else:
            raise CompatError("Unknown command")
        emit(result, args.shell, args.json)
    except CompatError as exc:
        eprint(f"[LU-COMPAT ERROR] {exc}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
