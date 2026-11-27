#!/usr/bin/env python3
"""Inputs for EveJS's generated game database (EveJS 261126.2 and later).

The release builds its static tables from two things the Linux Setup has to supply:

  * the CCP SDE archive. CCP does not publish an SDE for every client build, so a release
    names the archive it downloads (SDE_ARCHIVE_BUILD) and the build it is stamped as
    (SDE_BUILD) in tools/DatabaseCreator/CreateDatabase.bat. This module does what
    EnsureSde.ps1 does: download, check, extract, stamp.
  * "client records" read out of the EVE client with its own Windows Python 2.7 runtime, which
    only runs on Windows. They depend only on the client build, so they are extracted once
    (here, through Proton and the official Windows Python 3.10) and kept in
    ~/.local/share/evejs-linux/client-records/<build> for every install of that build.

Used by install-evejs-linux.sh (fresh installs) and evejs-linux-update.py (updates).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

SHARE = Path(os.environ.get("EVEJS_LINUX_SHARE", Path.home() / ".local/share/evejs-linux"))
WINDOWS_PYTHON_URL = "https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip"
WINDOWS_PYTHON_SHA256 = "608619f8619075629c9c69f361352a0da6ed7e62f83a0e19c63e0ea32eb7629d"
SDE_URL = "https://developers.eveonline.com/static-data/tranquility/eve-online-static-data-{build}-jsonl.zip"
HERE = Path(__file__).resolve().parent


class StaticDataError(RuntimeError):
    pass


def say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def creator_settings(repo: Path) -> dict:
    """SDE_BUILD, SDE_ARCHIVE_BUILD and CLIENT_BUILD as pinned by the release."""
    bat = repo / "tools/DatabaseCreator/CreateDatabase.bat"
    values = {}
    if bat.is_file():
        for key in ("SDE_BUILD", "SDE_ARCHIVE_BUILD", "CLIENT_BUILD"):
            m = re.search(rf'^set "{key}=(\d+)"\s*$', bat.read_text(encoding="utf-8", errors="replace"), re.M)
            if m:
                values[key] = int(m.group(1))
    return values


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, open(partial, "wb") as out:
            shutil.copyfileobj(response, out, 1 << 20)
    except Exception as exc:
        partial.unlink(missing_ok=True)
        raise StaticDataError(f"Could not download {url}: {exc}") from exc
    os.replace(partial, target)


def node_bin(explicit: str | None) -> str:
    found = explicit or os.environ.get("EVEJS_SETUP_NODE_BIN") or shutil.which("node")
    if not found:
        raise StaticDataError("Node.js was not found.")
    return found


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kwargs)


# ── SDE ───────────────────────────────────────────────────────────────────────
def prepare_sde(repo: Path, settings: dict, node: str, supported_build: int | None = None) -> tuple[Path, str]:
    build = settings.get("SDE_BUILD") or supported_build
    if not build:
        raise StaticDataError("The release does not say which SDE build it needs.")
    archive_build = settings.get("SDE_ARCHIVE_BUILD") or build
    url = SDE_URL.format(build=archive_build)
    local = repo / "_local"
    archive = local / f"downloads/sde/eve-online-static-data-{archive_build}-jsonl.zip"
    target = local / f"sde/eve-online-static-data-{build}-jsonl"
    marker = target / ".evejs-sde-complete"
    validator = repo / "tools/DatabaseCreator/validate-sde-directory.js"
    if marker.is_file() and marker.read_text().strip() == str(build):
        return target, url
    if not archive.is_file() or archive.stat().st_size == 0:
        say(f"Downloading CCP static data {archive_build} ...")
        download(url, archive)
    partial = target.with_name(target.name + ".partial")
    for leftover in (target, partial):
        if leftover.exists():
            shutil.rmtree(leftover)
    partial.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(partial)
    if archive_build != build:
        result = run([node, str(validator), "--stamp", str(partial), str(archive_build), str(build)])
        if result.returncode != 0:
            raise StaticDataError(f"Could not stamp CCP SDE archive {archive_build} as build {build}.")
        say(f"CCP SDE archive {archive_build} stamped as build {build}.")
    else:
        result = run([node, str(validator), str(partial), str(build)])
        if result.returncode != 0:
            raise StaticDataError(f"The CCP static data is not build {build}.")
    (partial / ".evejs-sde-complete").write_text(f"{build}\n")
    os.replace(partial, target)
    return target, url


# ── client records ────────────────────────────────────────────────────────────
def records_ok(folder: Path | None, build: int) -> bool:
    if not folder:
        return False
    try:
        manifest = json.loads((folder / "records.json").read_text(encoding="utf-8"))
    except Exception:
        return False
    return manifest.get("version") == 1 and manifest.get("build") == build


def records_candidates(repo: Path, build: int, explicit: str | None) -> list[Path]:
    root = os.environ.get("EVEJS_CLIENT_RECORDS_ROOT")
    items = [explicit, f"{root}/{build}" if root else None, str(HERE / "client-records" / str(build)),
             str(SHARE / "client-records" / str(build)), str(repo / "_local/client-records" / str(build))]
    return [Path(i) for i in items if i]


def windows_path(path: Path) -> str:
    return "Z:" + str(path).replace("/", "\\")


def extract_with_proton(repo: Path, build: int, proton: str, prefix: Path, steam_root: str, cache_root: Path) -> Path:
    runner = HERE / "winpy-runner.py"
    if not runner.is_file():
        runner = HERE / "tools/winpy-runner.py"
    if not runner.is_file():
        raise StaticDataError("tools/winpy-runner.py is missing beside the Setup.")
    script = repo / "tools/DatabaseCreator/extract-setup-datasets.py"
    if not script.is_file():
        raise StaticDataError("This release has no tools/DatabaseCreator/extract-setup-datasets.py.")
    pydir = SHARE / "windows-python"
    archive = pydir / "python-embed.zip"
    py = pydir / "py"
    pydir.mkdir(parents=True, exist_ok=True)
    if not archive.is_file() or sha256_file(archive) != WINDOWS_PYTHON_SHA256:
        archive.unlink(missing_ok=True)
        say("Downloading the official Windows Python 3.10 (embeddable) ...")
        download(WINDOWS_PYTHON_URL, archive)
        if sha256_file(archive) != WINDOWS_PYTHON_SHA256:
            archive.unlink(missing_ok=True)
            raise StaticDataError("The downloaded Windows Python failed its SHA-256 check.")
    if not (py / "pythonw.exe").is_file():
        if py.exists():
            shutil.rmtree(py)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(py)
    out_root = SHARE / "client-records"
    out_root.mkdir(parents=True, exist_ok=True)
    out = out_root / str(build)
    partial = out_root / f"{build}.partial"
    log = out_root / f"extract-{build}.log"
    status_file = Path(str(log) + ".exit")
    for leftover in (partial, status_file):
        if leftover.is_dir():
            shutil.rmtree(leftover)
        elif leftover.exists():
            leftover.unlink()
    prefix.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    env.update(STEAM_COMPAT_DATA_PATH=str(prefix), STEAM_COMPAT_CLIENT_INSTALL_PATH=steam_root, STEAM_COMPAT_APP_ID="0")
    say(f"Extracting the EVE client records for build {build} through Proton "
        "(several minutes, about 480 MB, done once and kept for later installs) ...")
    # pythonw.exe: the console python.exe never returns under Proton.
    try:
        subprocess.run([proton, "run", str(py / "pythonw.exe"), windows_path(runner), windows_path(log), windows_path(script),
                        "--client-root", windows_path(cache_root), "--build", str(build), "--output", windows_path(partial)],
                       env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3000)
    except subprocess.TimeoutExpired:
        pass
    status = status_file.read_text().strip() if status_file.is_file() else "missing"
    if status != "0" or not records_ok(partial, build):
        tail = ""
        if log.is_file():
            tail = "\n".join(log.read_text(errors="replace").splitlines()[-12:])
        raise StaticDataError(f"Client record extraction failed (status {status}). Log: {log}\n{tail}")
    if out.exists():
        shutil.rmtree(out)
    os.replace(partial, out)
    return out


def prepare_records(repo: Path, build: int, explicit: str | None, proton: str | None, prefix: str | None,
                    steam_root: str | None, cache_root: str | None) -> Path:
    for candidate in records_candidates(repo, build, explicit):
        if records_ok(candidate, build):
            return candidate.resolve()
    if proton and Path(proton).exists() and cache_root:
        return extract_with_proton(repo, build, proton, Path(prefix) if prefix else SHARE / "records-prefix",
                                   steam_root or str(Path.home() / ".local/share/Steam"), Path(cache_root))
    raise StaticDataError(
        f"No EVE client records for build {build} were found.\n"
        f"Pass --client-records <folder holding records.json>, or put the folder in {SHARE / 'client-records' / str(build)}.\n"
        'Create it once on a Windows PC with EveJS CreateDatabase.bat --export-client-records --client-root "<EVE cache>".')


def prepare(repo: Path, node: str | None, records: str | None, proton: str | None, prefix: str | None,
            steam_root: str | None, cache_root: str | None, supported_build: int | None = None) -> dict:
    repo = repo.resolve()
    settings = creator_settings(repo)
    node = node_bin(node)
    sde_dir, sde_url = prepare_sde(repo, settings, node, supported_build)
    result = {"sdeDir": str(sde_dir), "sdeUrl": sde_url, "sdeBuild": settings.get("SDE_BUILD") or supported_build,
              "clientBuild": settings.get("CLIENT_BUILD"), "recordsDir": ""}
    if settings.get("CLIENT_BUILD"):
        result["recordsDir"] = str(prepare_records(repo, settings["CLIENT_BUILD"], records, proton, prefix, steam_root, cache_root))
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--repo", required=True)
    p.add_argument("--node")
    p.add_argument("--client-records")
    p.add_argument("--proton")
    p.add_argument("--prefix")
    p.add_argument("--steam-root")
    p.add_argument("--cache-root")
    p.add_argument("--supported-build", type=int)
    p.add_argument("--shell", action="store_true")
    args = ap.parse_args()
    try:
        result = prepare(Path(args.repo), args.node, args.client_records, args.proton, args.prefix, args.steam_root,
                         args.cache_root, args.supported_build)
    except StaticDataError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if args.shell:
        import shlex
        for key, value in (("SDE_DIR", result["sdeDir"]), ("SDE_URL", result["sdeUrl"]), ("SDE_BUILD", result["sdeBuild"]),
                           ("CLIENT_RECORDS_BUILD", result["clientBuild"] or ""), ("CLIENT_RECORDS_DIR", result["recordsDir"])):
            print(f"{key}={shlex.quote(str(value))}")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
