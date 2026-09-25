#!/usr/bin/env python3
"""Exact-hash offline client patch gate for the EveJS Linux launcher."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile


STANDARD_CDN_PORT = 443
PORTABLE_CDN_PORT = 8443
PORTABLE_LAUNCHDARKLY_VARIANT = {
    "name": "EveJS localhost:8443 TLS endpoints, signature removed, checksum updated",
    "size": 11079680,
    "sha256": "0e8e0cabb8dddf3e1cc0ce517f1732cfdd05dce63a40401b0413649de694cbb3",
}


class ClientPreflightError(RuntimeError):
    pass


def sha256_bytes(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest().lower()


def validate_recipe(recipe: dict, label: str, expected_build: int) -> dict:
    if recipe.get("requireExactHash") is not True:
        raise ClientPreflightError(f"Client network recipe must require exact hashes: {label}")
    if int(recipe.get("supportedBuild", 0)) != expected_build:
        raise ClientPreflightError(
            f"Client network recipe {label} targets build "
            f"{recipe.get('supportedBuild')!r}, not {expected_build}."
        )
    source = recipe.get("source") or {}
    if not source.get("filename") or not source.get("sha256") or not source.get("size"):
        raise ClientPreflightError(f"Client network recipe has an incomplete source identity: {label}")
    for patch in recipe.get("patches") or []:
        before = bytes.fromhex(patch["beforeHex"])
        after = bytes.fromhex(patch["afterHex"])
        if not before or len(before) != len(after):
            raise ClientPreflightError(f"Client network recipe changes binary length: {label}")
    if not recipe.get("patches") or not recipe.get("knownPatchedVariants"):
        raise ClientPreflightError(f"Client network recipe is incomplete: {label}")
    return recipe


def load_recipe(path: Path, expected_build: int) -> dict:
    try:
        recipe = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ClientPreflightError(f"Could not read client patch recipe {path}: {exc}") from exc
    return validate_recipe(recipe, path.name, expected_build)


def local_url_hex(length: int, port: int) -> str:
    base = f"https://localhost{'' if port == STANDARD_CDN_PORT else f':{port}'}".encode()
    if len(base) > length:
        raise ClientPreflightError(f"Local CDN URL does not fit the audited client field ({length} bytes).")
    return (base + (b"/" * (length - len(base)))).hex()


def retarget_recipe(template: dict, source: dict, before_hexes: list[str], port: int,
                    variant: dict, label: str, expected_build: int) -> dict:
    result = copy.deepcopy(template)
    result["description"] = label
    result["source"] = copy.deepcopy(source)
    result["source"].setdefault("filename", template["source"]["filename"])
    result["knownPatchedVariants"] = [copy.deepcopy(variant)]
    for patch, before_hex in zip(result["patches"], before_hexes, strict=True):
        before = bytes.fromhex(before_hex)
        patch["beforeHex"] = before_hex
        patch["afterHex"] = local_url_hex(len(before), port)
        patch["description"] = label
    return validate_recipe(result, label, expected_build)


def launchdarkly_recipes(setup: Path, expected_build: int, cdn_port: int) -> tuple[dict, list[dict]]:
    standard = load_recipe(setup / "launchdarkly_patch_recipe.json", expected_build)
    numeric = load_recipe(setup / "launchdarkly_localhost_upgrade_recipe.json", expected_build)
    if numeric["source"]["filename"] != standard["source"]["filename"]:
        raise ClientPreflightError("LaunchDarkly upgrade recipe targets a different file.")
    standard_variant = standard["knownPatchedVariants"][0]

    if cdn_port == STANDARD_CDN_PORT:
        portable_to_standard = retarget_recipe(
            standard,
            PORTABLE_LAUNCHDARKLY_VARIANT,
            [local_url_hex(len(bytes.fromhex(p["afterHex"])), PORTABLE_CDN_PORT)
             for p in standard["patches"]],
            STANDARD_CDN_PORT,
            standard_variant,
            "Convert the exact SteamOS localhost:8443 endpoints to standard localhost:443 endpoints.",
            expected_build,
        )
        return standard, [numeric, portable_to_standard]

    if cdn_port != PORTABLE_CDN_PORT:
        raise ClientPreflightError(f"Unsupported local CDN port profile: {cdn_port}")

    portable_primary = retarget_recipe(
        standard,
        standard["source"],
        [p["beforeHex"] for p in standard["patches"]],
        PORTABLE_CDN_PORT,
        PORTABLE_LAUNCHDARKLY_VARIANT,
        "Replace the audited external endpoints with localhost:8443 for unprivileged SteamOS use.",
        expected_build,
    )
    numeric_to_portable = retarget_recipe(
        standard,
        numeric["source"],
        [p["beforeHex"] for p in numeric["patches"]],
        PORTABLE_CDN_PORT,
        PORTABLE_LAUNCHDARKLY_VARIANT,
        "Convert the exact legacy numeric-loopback endpoints to localhost:8443.",
        expected_build,
    )
    standard_to_portable = retarget_recipe(
        standard,
        standard_variant,
        [p["afterHex"] for p in standard["patches"]],
        PORTABLE_CDN_PORT,
        PORTABLE_LAUNCHDARKLY_VARIANT,
        "Convert the exact localhost:443 endpoints to localhost:8443 for SteamOS.",
        expected_build,
    )
    return portable_primary, [numeric_to_portable, standard_to_portable]


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


def apply_exact_recipe(binary: Path, primary: dict, upgrades: list[dict], apply: bool) -> str:
    data = bytearray(binary.read_bytes())
    known = patched_variant(primary, data)
    if known:
        return f"{primary['name']}: exact patched SHA-256 verified"

    selected = None
    if identity_matches(data, primary["source"]):
        selected = primary
    else:
        selected = next((recipe for recipe in upgrades if identity_matches(data, recipe["source"])), None)
    if selected is None:
        raise ClientPreflightError(
            f"Unsupported or changed {primary['name']}. Restore the original Eve build "
            f"{primary['supportedBuild']} file. SHA-256: {sha256_bytes(data)}"
        )
    if not apply:
        raise ClientPreflightError(f"{primary['name']} needs its exact-hash local endpoint patch.")

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


def run(repo: Path, tq: Path, expected_build: int, apply: bool, cdn_port: int) -> list[str]:
    validate_client_identity(tq, expected_build)
    assert_client_stopped(tq)
    setup = repo / "tools" / "ClientSETUP"
    launch_primary, launch_upgrades = launchdarkly_recipes(setup, expected_build, cdn_port)
    jobs = [(launch_primary, launch_upgrades)]
    jobs.append((load_recipe(setup / "crash_upload_patch_recipe.json", expected_build), []))
    messages = []
    for primary, upgrades in jobs:
        binary = tq / "bin64" / primary["source"]["filename"]
        if not binary.is_file() or binary.is_symlink():
            raise ClientPreflightError(f"Client network patch gate is missing an ordinary file: {binary}")
        messages.append(apply_exact_recipe(binary, primary, upgrades, apply))
    return messages


def validate_recipes(repo: Path, expected_build: int, cdn_port: int) -> None:
    setup = repo / "tools" / "ClientSETUP"
    launchdarkly_recipes(setup, expected_build, cdn_port)
    load_recipe(setup / "crash_upload_patch_recipe.json", expected_build)


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
        apply_exact_recipe(binary, primary, [], True)
        assert binary.read_bytes() == final
        assert binary.with_name(f"{binary.name}.pre-evejs-linux-network").read_bytes() == original
        assert "verified" in apply_exact_recipe(binary, primary, [], False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("apply", "check", "validate-recipes", "self-test"))
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--tq", type=Path)
    parser.add_argument("--build", type=int)
    parser.add_argument("--cdn-port", type=int, choices=(STANDARD_CDN_PORT, PORTABLE_CDN_PORT), default=STANDARD_CDN_PORT)
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
            validate_recipes(repo, args.build, args.cdn_port)
            print("EveJS client network patch recipes PASS")
            return 0
        if args.tq is None:
            parser.error("--tq is required for apply/check")
        for message in run(repo, args.tq.expanduser().resolve(), args.build, args.command == "apply", args.cdn_port):
            print(f"  ✓ {message}")
        return 0
    except ClientPreflightError as exc:
        print(f"[ERROR] {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
