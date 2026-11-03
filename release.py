"""Validate and package a complete data snapshot for GitHub Releases."""

from __future__ import annotations

import argparse
import hashlib
import gzip
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def model_files(model: Path) -> list[Path]:
    descriptor = json.loads(model.read_text())
    refs = descriptor["FileReferences"]
    paths = [refs["Moc"], *refs["Textures"]]
    paths.extend(refs[field] for field in ["Physics", "Pose", "UserData", "DisplayInfo"] if refs.get(field))
    paths.extend(item["File"] for item in refs.get("Expressions", []))
    paths.extend(
        item[field]
        for group in refs.get("Motions", {}).values()
        for item in group
        for field in ["File", "Sound"]
        if item.get(field)
    )
    if isinstance(refs.get("MotionSync"), str):
        paths.append(refs["MotionSync"])
    files = {model}
    for path in paths:
        target = (model.parent / path).resolve()
        if not target.is_relative_to(model.parent.resolve()) or not target.is_file():
            raise ValueError(f"Missing local model reference: {model.name}: {path}")
        files.add(target)
    return sorted(files)


def validate(root: Path, server: str, expected: dict) -> list[Path]:
    files = sorted(p for p in (root / server).rglob("*") if p.is_file())
    entries = [
        {
            "path": p.relative_to(root / server).as_posix(),
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "bytes": p.stat().st_size,
        }
        for p in files
    ]
    actual = hashlib.sha256(encoded(entries)).hexdigest()
    if actual != expected["sha256"] or len(files) != expected["files"]:
        raise ValueError(f"Incomplete {server} dataset")
    for model in (root / server).glob("*/*.model3.json"):
        model_files(model)
    return files


def package(root: Path, output: Path, server: str, *, native=False, include_manifest=True) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)

    class Parts:
        def __init__(self):
            self.paths = []
            self.file = None
            self.size = 0

        def write(self, data):
            original = len(data)
            while data:
                if self.file is None or self.size == 1024**3:
                    if self.file:
                        self.file.close()
                    path = output / f"{server}.tar.gz.part{len(self.paths):03}"
                    self.paths.append(path)
                    self.file = path.open("wb")
                    self.size = 0
                count = min(len(data), 1024**3 - self.size)
                self.file.write(data[:count])
                self.size += count
                data = data[count:]
            return original

        def flush(self):
            if self.file:
                self.file.flush()

        def close(self):
            if self.file:
                self.file.close()

    def normalized(info):
        info.uid = info.gid = info.mtime = 0
        info.uname = info.gname = ""
        info.pax_headers = {}
        info.mode = 0o755 if info.isdir() else 0o644
        return info

    parts = Parts()
    try:
        with gzip.GzipFile(filename="", mode="wb", fileobj=parts, compresslevel=4, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as bundle:
                if native:
                    for model in sorted((root / server).glob("*/*.model3.json")):
                        for file in model_files(model):
                            bundle.add(
                                file,
                                arcname=file.relative_to(root).as_posix(),
                                recursive=False,
                                filter=normalized,
                            )
                else:
                    bundle.add(root / server, arcname=server, filter=normalized)
                if include_manifest:
                    bundle.add(root / "manifest.json", arcname="manifest.json", filter=normalized)
    finally:
        parts.close()
    if len(parts.paths) == 1:
        archive = output / f"{server}.tar.gz"
        parts.paths[0].rename(archive)
        return [archive]
    return parts.paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    root = Path.cwd()
    manifest = json.loads((root / "manifest.json").read_text())
    server = args.tag.split("-", 1)[0]
    servers = [server] if server in {"jp", "intl"} else list(manifest["servers"])
    for server in servers:
        validate(root, server, manifest["servers"][server])
    if args.check_only:
        print("Complete dataset hashes and local model references verified")
        return
    with tempfile.TemporaryDirectory(prefix="data-release-") as temporary:
        output = Path(temporary)
        native = manifest["schema"].startswith("haneoka-live2d")
        pure = native or manifest["schema"].startswith("haneoka-stories")
        assets = [
            file
            for server in servers
            for file in package(root, output, server, native=native, include_manifest=not pure)
        ]
        checksums = output / "SHA256SUMS"

        def sha_file(path):
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()

        checksums.write_text("".join(f"{sha_file(p)}  {p.name}\n" for p in assets))
        notes = output / "notes.md"
        notes.write_text(
            "Complete JP/Intl data snapshot. Verify downloads with `sha256sum -c SHA256SUMS`.\n\n"
            + "\n".join(f"- {server}: `{manifest['servers'][server]['sha256']}`" for server in servers)
        )
        exists = (
            subprocess.run(
                ["gh", "release", "view", args.tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            ).returncode
            == 0
        )
        if not exists:
            subprocess.run(
                [
                    "gh",
                    "release",
                    "create",
                    args.tag,
                    "--verify-tag",
                    "--title",
                    args.tag,
                    "--notes-file",
                    str(notes),
                    "--latest=" + str(args.tag.startswith("v")).lower(),
                ],
                check=True,
            )
        subprocess.run(
            ["gh", "release", "upload", args.tag, "--clobber", *map(str, assets), str(checksums)], check=True
        )


if __name__ == "__main__":
    main()
