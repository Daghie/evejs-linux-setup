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
import socket
import subprocess
import sys
import tempfile

TARGET_VERSION = "0.12.6"
TARGET_BUILD = 3396210
XPATCH_VERSION = "0.3.2"
MOD_LU_PATH = Path("mods/x-eve-living-universe/loader.js")
SCRIPT_DIR = Path(__file__).absolute().parent
COMPAT_DIR = SCRIPT_DIR / "compatibility"
STOCK_MANIFEST = COMPAT_DIR / "market-stock-v0.12.6.json"
XPATCH_MANIFEST = COMPAT_DIR / "market-xpatch-lu-0.3.2-v0.12.6.json"
XPATCH_INSTALL = Path("_local/x-eve-patch/install.json")
LOADER_LU_ROOT = Path("mods/x-eve-living-universe")
XPATCH_SIGNATURES = (
    Path("config/xeve.json"),
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


def safe_repo_path(repo: Path, rel: str) -> Path:
    relpath = Path(rel)
    if relpath.is_absolute() or ".." in relpath.parts:
        raise CompatError(f"Unsafe path in X-Patch install record: {rel}")
    return repo / relpath


def verify_xpatch_install(repo: Path) -> dict:
    state_path = repo / XPATCH_INSTALL
    state = load_json(state_path)
    if str(state.get("pluginVersion") or "") != XPATCH_VERSION:
        raise CompatError(
            f"Unsupported X-Patch Living Universe version in {state_path}: "
            f"{state.get('pluginVersion')!r}. This build is audited only for {XPATCH_VERSION}."
        )
    if str(state.get("evejsVersion") or "") != TARGET_VERSION:
        raise CompatError(
            f"X-Patch install state targets EveJS {state.get('evejsVersion')!r}, "
            f"not {TARGET_VERSION}."
        )
    files = state.get("files")
    if not isinstance(files, list) or len(files) != 210:
        raise CompatError(
            f"X-Patch install state does not contain the audited 210 tracked files "
            f"(found {len(files) if isinstance(files, list) else 'invalid'})."
        )
    failures = []
    for entry in files:
        rel = str(entry.get("path") or "")
        path = safe_repo_path(repo, rel)
        if not path.is_file() or path.is_symlink():
            failures.append(f"{rel}: missing/not a regular file")
            continue
        actual_size = path.stat().st_size
        actual_hash = sha256_file(path)
        expected_size = int(entry.get("size", -1))
        expected_hash = str(entry.get("sha256") or "").upper()
        if actual_size != expected_size or actual_hash != expected_hash:
            failures.append(f"{rel}: expected {expected_hash}/{expected_size}, got {actual_hash}/{actual_size}")
    if failures:
        shown = "\n".join("  - " + x for x in failures[:20])
        more = "" if len(failures) <= 20 else f"\n  ... and {len(failures)-20} more"
        raise CompatError(
            "X-Patch Living Universe installed-state verification failed.\n"
            "The compatibility launcher will not repair or overwrite it.\n" + shown + more
        )
    installed_rows = [
        [str(e.get("path") or ""), int(e.get("size", -1)), str(e.get("sha256") or "").upper()]
        for e in files
    ]
    state = dict(state)
    state["_verifiedTrackedDigest"] = hashlib.sha256(
        json.dumps(installed_rows, separators=(",", ":")).encode()
    ).hexdigest().upper()
    return state


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
        raise CompatError(f"This compatibility build requires EveJS {TARGET_VERSION}: {repo}")
    build = read_supported_build(repo)
    if build != TARGET_BUILD:
        raise CompatError(f"Expected EVE client build contract {TARGET_BUILD}; found {build!r}.")

    mod_lu_active = (repo / MOD_LU_PATH).is_file()
    xpatch_state_present = (repo / XPATCH_INSTALL).is_file()
    if mod_lu_active and xpatch_state_present:
        raise CompatError(
            "Incompatible Living Universe variants are both active.\n"
            f"Loader LU: {repo / MOD_LU_PATH}\n"
            f"X-Patch LU: {repo / XPATCH_INSTALL}\n\n"
            "Nothing was changed. Disable/remove one variant using its own supported method before launching."
        )

    market_root = repo / "externalservices/market-server"
    actual_records = build_input_records(market_root)

    xpatch_state = None
    loader_digest = tree_digest(repo / LOADER_LU_ROOT) if mod_lu_active else ""
    xpatch_tracked_digest = ""
    if xpatch_state_present:
        xpatch_state = verify_xpatch_install(repo)
        xpatch_tracked_digest = str(xpatch_state.get("_verifiedTrackedDigest") or "")
        digest, expected = verify_build_manifest(actual_records, XPATCH_MANIFEST, "X-Patch LU 0.3.2")
        mode = "xpatch-lu"
        market_profile = "xpatch-lu-0.3.2-market"
        label = "X-Patch Living Universe 0.3.2"
    else:
        partial = detect_partial_xpatch(repo)
        if partial:
            raise CompatError(
                "X-Patch-like files were found but no valid _local/x-eve-patch/install.json is present.\n"
                "The launcher cannot factually classify this tree and will not guess.\n"
                "Detected:\n  - " + "\n  - ".join(partial)
            )
        digest, expected = verify_build_manifest(actual_records, STOCK_MANIFEST, "Stock EveJS 0.12.6")
        market_profile = "stock-v0.12.6-market"
        if mod_lu_active:
            mode = "loader-lu"
            label = "Loader Living Universe (mods/x-eve-living-universe)"
        else:
            mode = "stock"
            label = "Stock EveJS 0.12.6"

    return {
        "repo": str(repo),
        "mode": mode,
        "marketProfile": market_profile,
        "label": label,
        "sourceDigest": digest,
        "marketInputCount": len(actual_records),
        "modLivingUniverseActive": mod_lu_active,
        "loaderLivingUniverseDigest": loader_digest,
        "xpatchLivingUniverseActive": xpatch_state_present,
        "xpatchVersion": str(xpatch_state.get("pluginVersion")) if xpatch_state else "",
        "xpatchTrackedDigest": xpatch_tracked_digest,
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


def rpc_call(port: int, method: str, params: dict, timeout: float = 4.0) -> dict:
    payload = json.dumps({"id": "evejs-linux-lu-compat", "method": method, "params": params}, separators=(",", ":")) + "\n"
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload.encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    line = buf.split(b"\n", 1)[0]
    if not line:
        raise CompatError(f"No RPC response for {method}")
    try:
        return json.loads(line.decode())
    except Exception as exc:
        raise CompatError(f"Invalid RPC response for {method}: {line[:500]!r}") from exc


def probe_xpatch_rpc(port: int = 40111) -> dict:
    results = {}
    for method, params in (
        ("GetSeedStocks", {"keys": []}),
        ("AdjustSeedStocks", {"adjustments": []}),
    ):
        response = rpc_call(port, method, params)
        if response.get("ok") is not True:
            raise CompatError(
                f"X-Patch Living Universe Market RPC probe failed for {method}:\n"
                f"{json.dumps(response, indent=2)}"
            )
        results[method] = response
    return results


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
    assert load_json(STOCK_MANIFEST)["sourceDigest"] == "E9164F59BA3140A4C1AA792D9BF82896D10ED266A43D1BCD062166F77DA9025A"
    assert load_json(XPATCH_MANIFEST)["sourceDigest"] == "CA8F89FB0175B9207014E9EEC1461376C195DE0A9F34E7E4490B92C230435A8C"
    print(json.dumps({"status": "PASS", "stockDigest": load_json(STOCK_MANIFEST)["sourceDigest"], "xpatchDigest": load_json(XPATCH_MANIFEST)["sourceDigest"]}, indent=2))


def main():
    ap = argparse.ArgumentParser(description="Read-only EveJS/Living Universe runtime compatibility authority")
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

    p = sub.add_parser("probe-xpatch-rpc")
    p.add_argument("--port", type=int, default=40111)
    p.add_argument("--json", action="store_true")

    sub.add_parser("self-test")
    args = ap.parse_args()

    try:
        if args.command == "self-test":
            self_test()
            return
        if args.command == "probe-xpatch-rpc":
            result = probe_xpatch_rpc(args.port)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print("GetSeedStocks: PASS")
                print("AdjustSeedStocks: PASS")
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
