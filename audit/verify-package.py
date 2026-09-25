#!/usr/bin/env python3
"""Reproduce the fast archive, source-identity, contract, and package checks."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import py_compile
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

sys.dont_write_bytecode = True


TARGET_VERSION = "0.12.9"
SOURCE_VERSION = "0.12.8"
TARGET_BUILD = 3396210
TARGET_ZIP_SHA256 = "4a1204c733847f91b6d1a32128896a0e42a6c3c8c4413173b4855ad3af14e07c"
SOURCE_ZIP_SHA256 = "8b0893a44fd1a880c183e6fa00fa3d0e4e6ae4dab6661f9a013157fd97e0b7dd"
MARKET_DIGEST = "643633B3CE290ADBC5A85D936C3977512D4C3CB0CF4414BF86300F830D08F746"
DIFF_COUNTS = {"ADDED": 578, "REMOVED": 36, "MODIFIED": 1496, "UNCHANGED": 237}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_package_sums(package_root: Path):
    checksum_file = package_root / "PACKAGE-SHA256SUMS.txt"
    expected = {}
    for line in checksum_file.read_text().splitlines():
        digest, relative = line.split("  ", 1)
        expected[relative] = digest
    actual_files = {
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*")
        if path.is_file() and path != checksum_file and "__pycache__" not in path.parts
    }
    assert set(expected) == actual_files, (sorted(set(expected) - actual_files), sorted(actual_files - set(expected)))
    for relative, digest in expected.items():
        assert sha256(package_root / relative) == digest, relative


def assert_package_zip(package_zip: Path, package_root: Path):
    expected_root = package_root.name
    with zipfile.ZipFile(package_zip) as archive:
        assert archive.testzip() is None
        files = []
        seen = set()
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            parts = Path(name).parts
            assert parts and parts[0] == expected_root, name
            assert not name.startswith("/") and ".." not in parts, name
            if not info.is_dir():
                relative = "/".join(parts[1:])
                assert relative and relative not in seen, name
                seen.add(relative)
                mode = (info.external_attr >> 16) & 0o177777
                assert not stat.S_ISLNK(mode), name
                source = package_root / relative
                assert source.is_file(), relative
                assert hashlib.sha256(archive.read(info)).hexdigest() == sha256(source), relative
                if mode:
                    assert stat.S_IMODE(mode) == stat.S_IMODE(source.stat().st_mode), relative
                files.append(relative)
        expected_files = {
            path.relative_to(package_root).as_posix()
            for path in package_root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        assert set(files) == expected_files


def assert_portable_payload(package_root: Path, source_root: Path, temporary: Path):
    portable = package_root / "portable/steamos-x86_64"
    manifest = json.loads((portable / "manifest.json").read_text())
    payload = portable / manifest["payload"]["file"]
    assert payload.stat().st_size == manifest["payload"]["size"]
    assert sha256(payload) == manifest["payload"]["sha256"]
    for relative, digest in manifest["lockfiles"].items():
        assert sha256(source_root / relative) == digest, relative

    extract = temporary / "portable-payload"
    extract.mkdir()
    allowed = {"node_modules", "server", "externalservices", "tools"}
    with tarfile.open(payload, "r:xz") as archive:
        for member in archive.getmembers():
            name = member.name.removeprefix("./")
            parts = Path(name).parts
            if name in ("", ".") and member.isdir():
                continue
            assert name and not Path(name).is_absolute() and ".." not in parts
            assert parts and parts[0] in allowed
        archive.extractall(extract)

    for artifact in manifest["artifacts"]:
        path = extract / artifact["path"]
        assert path.is_file() and path.stat().st_size == artifact["size"], artifact["path"]
        assert sha256(path) == artifact["sha256"], artifact["path"]

    executables = [extract / item["path"] for item in manifest["artifacts"] if item["linkage"] == "static-pie-musl"]
    for executable in executables:
        assert os.access(executable, os.X_OK), executable
        output = subprocess.run(["file", str(executable)], check=True, text=True, capture_output=True).stdout
        assert "static-pie linked" in output, output
        subprocess.run([str(executable), "--help"], check=True, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-zip", type=Path, required=True)
    parser.add_argument("--previous-zip", type=Path, required=True)
    parser.add_argument("--package-zip", type=Path)
    args = parser.parse_args()

    package_root = Path(__file__).resolve().parent.parent
    maintenance = load_module("evejs_linux_maintenance_verify", package_root / "evejs-linux-maintenance.py")
    compat = load_module("evejs_linux_compat_verify", package_root / "evejs-linux-lu-compat.py")
    diff_tool = load_module("evejs_source_diff_verify", package_root / "audit/generate-source-diff.py")

    assert sha256(args.source_zip) == TARGET_ZIP_SHA256
    assert sha256(args.previous_zip) == SOURCE_ZIP_SHA256

    with tempfile.TemporaryDirectory(prefix="evejs-v0129-fast-verify-") as temporary:
        temporary = Path(temporary)
        old_extract = temporary / "old"
        new_extract = temporary / "new"
        old_extract.mkdir()
        new_extract.mkdir()
        maintenance._safe_extract_release_zip(args.previous_zip.resolve(), old_extract)
        maintenance._safe_extract_release_zip(args.source_zip.resolve(), new_extract)
        old_root = maintenance.resolve_archive_root(old_extract)
        new_root = maintenance.resolve_archive_root(new_extract)
        assert old_root is not None and maintenance.read_version(old_root) == SOURCE_VERSION
        assert new_root is not None and maintenance.read_version(new_root) == TARGET_VERSION
        assert maintenance.read_supported_build(new_root) == TARGET_BUILD

        old_defaults = package_root / "upgrade-contracts/0.12.8-to-0.12.9/old-defaults/config"
        for old_config in sorted((old_root / "config").glob("*.json")):
            assert old_config.read_bytes() == (old_defaults / old_config.name).read_bytes(), old_config.name

        old_server = json.loads((old_defaults / "server.json").read_text())
        new_server = json.loads((new_root / "config/server.json").read_text())
        merge_report = {"target": [], "user": [], "conflicts": []}
        assert maintenance.json_merge(old_server, old_server, new_server, "server.json", merge_report) == new_server

        classification = compat.classify(new_root)
        assert classification["mode"] == "stock"
        assert classification["sourceDigest"] == MARKET_DIGEST

        marker = new_root / "_local/evejs-linux-integrated-lu/install.json"
        marker.parent.mkdir(parents=True)
        marker.write_text("{}\n")
        try:
            compat.classify(new_root)
        except compat.CompatError:
            pass
        else:
            raise AssertionError("Stock-only compatibility did not reject integrated source state")
        shutil.rmtree(new_root / "_local")

        rows, counts = diff_tool.compare(old_root, new_root)
        assert counts == DIFF_COUNTS, counts
        audited = json.loads((package_root / "audit/full-tree-diff-0.12.8-to-0.12.9.json").read_text())
        assert audited == {"counts": counts, "rows": rows}

        subprocess.run([
            str(package_root / "evejs-linux-client-preflight.py"),
            "validate-recipes", "--repo", str(new_root), "--build", str(TARGET_BUILD),
        ], check=True)
        subprocess.run([
            str(package_root / "evejs-linux-client-preflight.py"),
            "validate-recipes", "--repo", str(new_root), "--build", str(TARGET_BUILD),
            "--cdn-port", "8443",
        ], check=True)
        subprocess.run([
            str(package_root / "evejs-linux-client-preflight.py"), "self-test",
        ], check=True)

        for script_name in ("install-evejs-linux.sh", "evejs-linux-launcher.sh"):
            script = (package_root / script_name).read_text()
            assert "_local/certs" in script, script_name
            assert "evejs-linux-client-preflight.py" in script, script_name
            for required_name in ("gateway-dev-cert.pem", "gateway-dns-cert.pem", "xmpp-dev-cert.pem"):
                assert required_name in script, (script_name, required_name)

        installer_text = (package_root / "install-evejs-linux.sh").read_text()
        launcher_text = (package_root / "evejs-linux-launcher.sh").read_text()
        assert "SteamOS package repair is disabled in v1.15 portable mode" in installer_text
        assert "no compiler, Cargo, sudo, Pacman, or read-only change is required" in installer_text
        assert 'CDN_PORT="8443"' in launcher_text
        assert 'required_commands+=(sysctl sudo)' in launcher_text
        assert 'if (( CDN_PORT < 1024 )); then' in launcher_text

        assert not (package_root / "integrated-lu").exists()
        assert_portable_payload(package_root, new_root, temporary)

        compile_root = temporary / "pyc"
        compile_root.mkdir()
        for source in (
            package_root / "evejs-linux-maintenance.py",
            package_root / "evejs-linux-lu-compat.py",
            package_root / "evejs-linux-client-preflight.py",
            package_root / "audit/generate-source-diff.py",
            package_root / "audit/verify-package.py",
        ):
            py_compile.compile(str(source), cfile=str(compile_root / f"{source.stem}.pyc"), doraise=True)

    for shell_script in (
        package_root / "install-evejs-linux.sh",
        package_root / "evejs-linux-launcher.sh",
        package_root / "evejs-v0.12.9-stale-processes.sh",
    ):
        subprocess.run(["bash", "-n", str(shell_script)], check=True)

    maintenance.self_test()
    compat.self_test()
    assert_package_sums(package_root)
    if args.package_zip:
        assert_package_zip(args.package_zip.resolve(), package_root)

    print(json.dumps({
        "status": "PASS",
        "targetVersion": TARGET_VERSION,
        "sourceVersion": SOURCE_VERSION,
        "targetBuild": TARGET_BUILD,
        "marketSourceDigest": MARKET_DIGEST,
        "diffCounts": DIFF_COUNTS,
        "steamOSPortablePayloadChecked": True,
        "packageZipChecked": bool(args.package_zip),
    }, indent=2))


if __name__ == "__main__":
    main()
