#!/usr/bin/env python3
"""Exact-hash offline client patch gate for the EveJS Linux launcher."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile


RECIPE_JOBS = (
    (
        "launchdarkly_patch_recipe.json",
        "launchdarkly_localhost_upgrade_recipe.json",
    ),
    ("crash_upload_patch_recipe.json", None),
)


class ClientPreflightError(RuntimeError):
    pass


def sha256_bytes(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest().lower()


def load_recipe(path: Path, expected_build: int) -> dict:
    try:
        recipe = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ClientPreflightError(f"Could not read client patch recipe {path}: {exc}") from exc
    if recipe.get("requireExactHash") is not True:
        raise ClientPreflightError(f"Client network recipe must require exact hashes: {path.name}")
    if int(recipe.get("supportedBuild", 0)) != expected_build:
        raise ClientPreflightError(
            f"Client network recipe {path.name} targets build "
            f"{recipe.get('supportedBuild')!r}, not {expected_build}."
        )
    source = recipe.get("source") or {}
    if not source.get("filename") or not source.get("sha256") or not source.get("size"):
        raise ClientPreflightError(f"Client network recipe has an incomplete source identity: {path.name}")
    for patch in recipe.get("patches") or []:
        before = bytes.fromhex(patch["beforeHex"])
        after = bytes.fromhex(patch["afterHex"])
        if not before or len(before) != len(after):
            raise ClientPreflightError(f"Client network recipe changes binary length: {path.name}")
    if not recipe.get("patches") or not recipe.get("knownPatchedVariants"):
        raise ClientPreflightError(f"Client network recipe is incomplete: {path.name}")
    return recipe


def identity_matches(data: bytes | bytearray, identity: dict) -> bool:
    return len(data) == int(identity["size"]) and sha256_bytes(data) == str(identity["sha256"]).lower()


def pe_offsets(data: bytes | bytearray) -> tuple[int, int, int, int]:
    if len(data) < 0x100 or data[:2] != b"MZ":
        raise ClientPreflightError("Client network patch target is not a valid MZ/PE image.")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ClientPreflightError("Client network patch target has an invalid PE signature.")
    optional = pe_offset + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic == 0x20B:
        directories = optional + 112
    elif magic == 0x10B:
        directories = optional + 96
    else:
        raise ClientPreflightError("Client network patch target has an unsupported PE header.")
    checksum_offset = optional + 64
    security_directory = directories + (4 * 8)
    certificate_offset, certificate_size = struct.unpack_from("<II", data, security_directory)
    return checksum_offset, security_directory, certificate_offset, certificate_size


def pe_checksum(data: bytes | bytearray, checksum_offset: int) -> int:
    total = 0
    index = 0
    while index < len(data):
        if checksum_offset <= index < checksum_offset + 4:
            index += 2
            continue
        word = data[index]
        if index + 1 < len(data):
            word |= data[index + 1] << 8
        total += word
        total = (total & 0xFFFFFFFF) + (total >> 32)
        index += 2
    total = (total & 0xFFFF) + (total >> 16)
    total += total >> 16
    return ((total & 0xFFFF) + len(data)) & 0xFFFFFFFF


def patched_variant(recipe: dict, data: bytes | bytearray) -> dict | None:
    return next(
        (variant for variant in recipe["knownPatchedVariants"] if identity_matches(data, variant)),
        None,
    )


def apply_exact_recipe(binary: Path, primary: dict, upgrade: dict | None, apply: bool) -> str:
    data = bytearray(binary.read_bytes())
    known = patched_variant(primary, data)
    if known:
        return f"{primary['name']}: exact patched SHA-256 verified"

    selected = None
    if identity_matches(data, primary["source"]):
        selected = primary
    elif upgrade and identity_matches(data, upgrade["source"]):
        selected = upgrade
    if selected is None:
        raise ClientPreflightError(
            f"Unsupported or changed {primary['name']}. Restore the original Eve build "
            f"{primary['supportedBuild']} file. SHA-256: {sha256_bytes(data)}"
        )
    if not apply:
        action = "localhost TLS upgrade" if selected is upgrade else "loopback patch"
        raise ClientPreflightError(f"{primary['name']} needs its exact-hash {action}.")

    for patch in selected["patches"]:
        offset = int(patch["offset"])
        before = bytes.fromhex(patch["beforeHex"])
        after = bytes.fromhex(patch["afterHex"])
        if bytes(data[offset : offset + len(before)]) != before:
            raise ClientPreflightError(
                f"Patch precondition failed for {primary['name']} at "
                f"{patch.get('offsetHex', hex(offset))}."
            )
        data[offset : offset + len(after)] = after

    checksum_offset, security_directory, certificate_offset, certificate_size = pe_offsets(data)
    if certificate_offset and certificate_size:
        if certificate_offset > len(data) or certificate_offset + certificate_size > len(data):
            raise ClientPreflightError(f"Invalid PE security directory in {primary['name']}.")
        data = data[:certificate_offset]
    checksum_offset, security_directory, _, _ = pe_offsets(data)
    struct.pack_into("<II", data, security_directory, 0, 0)
    struct.pack_into("<I", data, checksum_offset, 0)
    struct.pack_into("<I", data, checksum_offset, pe_checksum(data, checksum_offset))

    if not patched_variant(primary, data):
        raise ClientPreflightError(
            f"Patched {primary['name']} did not match the audited output identity; nothing was published."
        )

    backup = binary.with_name(f"{binary.name}.pre-evejs-linux-network")
    if not backup.exists():
        shutil.copy2(binary, backup)
    mode = binary.stat().st_mode
    with tempfile.NamedTemporaryFile(prefix=f".{binary.name}.", dir=binary.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.chmod(mode)
        os.replace(temporary, binary)
    finally:
        temporary.unlink(missing_ok=True)

    published = binary.read_bytes()
    if not patched_variant(primary, published):
        raise ClientPreflightError(f"Published {primary['name']} failed its exact patched hash gate.")
    return f"{primary['name']}: exact loopback patch applied and verified"


def validate_client_identity(tq: Path, expected_build: int) -> None:
    if not tq.is_dir() or tq.is_symlink():
        raise ClientPreflightError(f"Client identity gate requires an ordinary tq directory: {tq}")
    ini = tq / "start.ini"
    if not ini.is_file() or ini.is_symlink():
        raise ClientPreflightError(f"Client identity gate is missing an ordinary start.ini: {ini}")
    values: dict[str, str] = {}
    for line in ini.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        key = key.lower()
        if key in {"server", "serverip", "cryptopack", "build"}:
            canonical = "server" if key == "serverip" else key
            if canonical in values:
                raise ClientPreflightError(f"Client identity gate found duplicate {canonical} in {ini}.")
            values[canonical] = value
    if values.get("server") != "127.0.0.1" or values.get("cryptopack") != "Placebo":
        raise ClientPreflightError(
            "Client identity gate requires server = 127.0.0.1 and cryptoPack = Placebo."
        )
    if values.get("build") != str(expected_build):
        raise ClientPreflightError(
            f"Client identity gate found build {values.get('build')!r}; expected {expected_build}."
        )


def assert_client_stopped(tq: Path) -> None:
    executable = (tq / "bin64" / "exefile.exe").resolve()
    linux_name = str(executable).lower()
    windows_name = ("z:" + str(executable).replace("/", "\\")).lower()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            ).lower()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if linux_name in command or windows_name in command:
            raise ClientPreflightError(
                f"Close the copied EveJS client before applying/verifying network patches (PID {proc.name})."
            )


def run(repo: Path, tq: Path, expected_build: int, apply: bool) -> list[str]:
    validate_client_identity(tq, expected_build)
    assert_client_stopped(tq)
    setup = repo / "tools" / "ClientSETUP"
    messages = []
    for primary_name, upgrade_name in RECIPE_JOBS:
        primary = load_recipe(setup / primary_name, expected_build)
        upgrade = load_recipe(setup / upgrade_name, expected_build) if upgrade_name else None
        if upgrade and upgrade["source"]["filename"] != primary["source"]["filename"]:
            raise ClientPreflightError("LaunchDarkly upgrade recipe targets a different file.")
        binary = tq / "bin64" / primary["source"]["filename"]
        if not binary.is_file() or binary.is_symlink():
            raise ClientPreflightError(f"Client network patch gate is missing an ordinary file: {binary}")
        messages.append(apply_exact_recipe(binary, primary, upgrade, apply))
    return messages


def validate_recipes(repo: Path, expected_build: int) -> None:
    setup = repo / "tools" / "ClientSETUP"
    for primary_name, upgrade_name in RECIPE_JOBS:
        primary = load_recipe(setup / primary_name, expected_build)
        if upgrade_name:
            upgrade = load_recipe(setup / upgrade_name, expected_build)
            if upgrade["source"]["filename"] != primary["source"]["filename"]:
                raise ClientPreflightError("LaunchDarkly upgrade recipe targets a different file.")


def self_test() -> None:
    original = bytearray(1024)
    original[:2] = b"MZ"
    struct.pack_into("<I", original, 0x3C, 0x80)
    original[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", original, 0x98, 0x20B)
    _, security_directory, _, _ = pe_offsets(original)
    struct.pack_into("<II", original, security_directory, 900, 124)
    original[512:514] = b"AB"

    final = bytearray(original)
    final[512:514] = b"CD"
    checksum_offset, security_directory, certificate_offset, _ = pe_offsets(final)
    final = final[:certificate_offset]
    checksum_offset, security_directory, _, _ = pe_offsets(final)
    struct.pack_into("<II", final, security_directory, 0, 0)
    struct.pack_into("<I", final, checksum_offset, 0)
    struct.pack_into("<I", final, checksum_offset, pe_checksum(final, checksum_offset))

    primary = {
        "name": "synthetic-client.dll",
        "supportedBuild": 3396210,
        "source": {"size": len(original), "sha256": sha256_bytes(original)},
        "patches": [{"offset": 512, "beforeHex": "4142", "afterHex": "4344"}],
        "knownPatchedVariants": [{"size": len(final), "sha256": sha256_bytes(final)}],
    }

    with tempfile.TemporaryDirectory(prefix="evejs-client-preflight-test-") as temporary:
        binary = Path(temporary) / "synthetic-client.dll"
        binary.write_bytes(original)
        apply_exact_recipe(binary, primary, None, True)
        assert binary.read_bytes() == final
        assert binary.with_name(f"{binary.name}.pre-evejs-linux-network").read_bytes() == original
        assert "verified" in apply_exact_recipe(binary, primary, None, False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("apply", "check", "validate-recipes", "self-test"))
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--tq", type=Path)
    parser.add_argument("--build", type=int)
    args = parser.parse_args()
    try:
        if args.command == "self-test":
            self_test()
            print("EveJS client preflight patch-engine self-test PASS")
            return 0
        if args.repo is None:
            parser.error("--repo is required")
        if args.build is None:
            parser.error("--build is required")
        repo = args.repo.expanduser().resolve()
        if args.command == "validate-recipes":
            validate_recipes(repo, args.build)
            print("EveJS client network patch recipes PASS")
            return 0
        if args.tq is None:
            parser.error("--tq is required for apply/check")
        for message in run(repo, args.tq.expanduser().resolve(), args.build, args.command == "apply"):
            print(f"  ✓ {message}")
        return 0
    except ClientPreflightError as exc:
        print(f"[ERROR] {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
