"""Reconcile one data repository using only its own GitHub Actions token."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import tarfile
import tempfile
import shutil
from pathlib import Path

from exporters import export_to, masterdata
from git_store import REPOSITORIES, Repository, git
from public_snapshot import PublicSnapshot, download_asset, feed_asset, feed_for
from release import validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=list(REPOSITORIES), required=True)
    parser.add_argument("--force", action="store_true", help="Rebuild the projection from unchanged input")
    parser.add_argument("--no-release", action="store_true")
    args = parser.parse_args()
    root = Path.cwd()
    if os.environ["GITHUB_REPOSITORY"] != f"haneoka-gakuen/{REPOSITORIES[args.kind]}":
        raise ValueError("Consumer repository does not match the requested product")
    repo = Repository(REPOSITORIES[args.kind], root, local_root=root)
    for server in ["jp", "intl"]:
        feed = feed_for(server)
        if not feed.get("assets"):
            print(f"{server}: awaiting initial feed", flush=True)
            continue
        if args.kind in {"live2d", "stories"} and not feed.get(
            "models" if args.kind == "live2d" else "stories"
        ):
            print(f"{server}: awaiting complete model transfer archive", flush=True)
            continue
        if not feed.get("masterHash") or not feed.get("publishedMaster"):
            print(f"{server}: awaiting corrected Master version metadata", flush=True)
            continue
        manifest = json.loads((root / "manifest.json").read_text())
        previous = manifest["servers"].get(server, {})
        identity = feed["masterSha256"] if args.kind == "masterdata" else feed["releaseId"]
        if args.kind == "live2d" and feed.get("models"):
            identity = feed["models"]["input"]
        if args.kind == "stories":
            identity = feed["stories"]["input"]
        version = (
            {"version": feed["masterVersion"], "hash": feed["masterHash"]}
            if args.kind == "masterdata"
            else feed["publishedMaster"]
        )
        if (
            previous.get("input") == identity
            and all(previous.get(key) == value for key, value in version.items())
            and not args.force
        ):
            print(f"{server}: unchanged input", flush=True)
            continue
        print(f"Exporting complete {server} {args.kind}", flush=True)
        staged = f"refs/heads/ingest/{server}"
        if git(root, "ls-remote", "origin", staged):
            git(root, "fetch", "--quiet", "--depth", "1", "origin", staged)
            git(root, "restore", "--source", "FETCH_HEAD", "--worktree", "--", server)
            print(f"Restored interrupted {server} transfer for hash-verified reuse", flush=True)
        if args.kind in {"live2d", "stories"}:
            product = feed["models" if args.kind == "live2d" else "stories"]
            metadata = product["metadata"].copy()
            if previous.get("sha256") == metadata["sha256"] and (root / server).is_dir():
                validate(root, server, metadata)
            else:
                with tempfile.TemporaryDirectory(prefix="model-feed-") as directory:
                    temporary = Path(directory)
                    combined = temporary / "models.tar.gz"
                    with combined.open("wb") as target:
                        for index, name in enumerate(product["parts"]):
                            part = temporary / f"part-{index}"
                            download_asset(feed, name, part)
                            with part.open("rb") as source:
                                shutil.copyfileobj(source, target)
                            part.unlink()
                    with tarfile.open(combined, "r:gz") as archive:
                        archive.extractall(temporary / "data", filter="data")
                    combined.unlink()
                    metadata = product["metadata"].copy()
                    files = validate(temporary / "data", server, metadata)
                    selected = set()
                    for file in files:
                        relative = file.relative_to(temporary / "data" / server)
                        selected.add(relative.as_posix())
                        destination = root / server / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        file.replace(destination)
                    for file in (root / server).rglob("*"):
                        if file.is_file() and file.relative_to(root / server).as_posix() not in selected:
                            file.unlink()
        elif args.kind == "masterdata":
            raw = feed_asset(feed, "masterdata.tar.gz")
            with tempfile.TemporaryDirectory(prefix="master-feed-") as directory:
                with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
                    archive.extractall(directory, filter="data")
                metadata = masterdata(root, server, Path(directory), feed["masterVersion"])
        else:
            metadata = export_to(args.kind, root, PublicSnapshot(feed))
        # The single root version record also pins the producer's exact input.
        metadata["input"] = identity
        metadata.update(version)
        if repo.publish(server, metadata):
            tag = repo.published_tag
            if not args.no_release:
                subprocess.run(["python", "tools/release.py", "--tag", tag], check=True)
        else:
            print(f"{server}: unchanged content", flush=True)


if __name__ == "__main__":
    main()
