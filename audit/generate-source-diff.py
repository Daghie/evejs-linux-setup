#!/usr/bin/env python3
"""Generate the complete EveJS release-tree comparison shipped with Setup v1.14."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


STATUSES = ("ADDED", "REMOVED", "MODIFIED", "UNCHANGED")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def inventory(root: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        stat_result = path.lstat()
        record: dict[str, object] = {
            "path": relative,
            "type": "symlink" if path.is_symlink() else "dir" if path.is_dir() else "file",
        }
        if path.is_symlink():
            record["target"] = path.readlink().as_posix()
        elif path.is_file():
            record["size"] = stat_result.st_size
            record["sha256"] = sha256(path)
        record["mode"] = oct(stat_result.st_mode & 0o7777)
        rows[relative] = record
    return rows


def compare(old_root: Path, new_root: Path) -> tuple[list[dict], dict[str, int]]:
    old = inventory(old_root)
    new = inventory(new_root)
    rows = []
    counts = {status: 0 for status in STATUSES}
    for path in sorted(set(old) | set(new)):
        old_record = old.get(path)
        new_record = new.get(path)
        if old_record is None:
            status = "ADDED"
        elif new_record is None:
            status = "REMOVED"
        elif old_record == new_record:
            status = "UNCHANGED"
        else:
            status = "MODIFIED"
        counts[status] += 1
        rows.append({"status": status, "path": path, "old": old_record, "new": new_record})
    return rows, counts


def write_outputs(rows: list[dict], counts: dict[str, int], output_prefix: Path):
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    json_path = Path(str(output_prefix) + ".json")
    json_path.write_text(json.dumps({"counts": counts, "rows": rows}, indent=2) + "\n")

    csv_path = Path(str(output_prefix) + ".csv")
    with csv_path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("status", "path", "old_size", "old_sha256", "new_size", "new_sha256"))
        for row in rows:
            old = row["old"] or {}
            new = row["new"] or {}
            writer.writerow((
                row["status"], row["path"], old.get("size", ""), old.get("sha256", ""),
                new.get("size", ""), new.get("sha256", ""),
            ))

    text_path = Path(str(output_prefix) + ".txt")
    lines = [
        "EveJS v0.12.8 -> v0.12.9 complete tree comparison",
        json.dumps(counts, indent=2),
        "",
    ]
    lines.extend(
        f"{row['status']} {row['path']}" for row in rows if row["status"] != "UNCHANGED"
    )
    text_path.write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("old_root", type=Path)
    parser.add_argument("new_root", type=Path)
    parser.add_argument("output_prefix", type=Path)
    args = parser.parse_args()
    rows, counts = compare(args.old_root.resolve(), args.new_root.resolve())
    write_outputs(rows, counts, args.output_prefix.resolve())
    print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
