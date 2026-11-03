"""Publish sealed producer feeds and reconcile resource refresh requests."""

from __future__ import annotations

import argparse
import base64
import gzip
import os
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from git_store import Repository
from storage import Snapshot, Store, digest, encode, write_json
from discovery import github, official_master, upstream_signal
from exporters import export_to
from versions import master_version, dataset_tag
from release import package


class ControlState:
    def __init__(self, store: Store, server: str):
        self.store = store
        self.key = f"private/companion-data/{server}/state.json"

    def read(self):
        from botocore.exceptions import ClientError

        try:
            return self.store.json(self.key)
        except ClientError as error:
            if error.response["Error"]["Code"] in {"NoSuchKey", "404"}:
                return {}
            raise

    def write(self, state):
        self.store.put_json(self.key, state)


def reconcile(
    server: str, state: dict, control: ControlState, version: str | None, signal: dict, retry: bool
):
    key = None
    if version and version != state["publishedMaster"]:
        key = digest(encode({"master": version}))
    elif state.get("pendingSignal"):
        key = digest(encode(state["pendingSignal"]["signal"]))
    if key is None:
        return
    request = state.get("request", {})
    if request.get("key") != key or retry:
        request = {"key": key, "attempts": 0, "lastAttempt": 0}
    now = int(time.time())
    if request["attempts"] >= 3 or now - request["lastAttempt"] < 8 * 3600:
        return
    runs = github("repos/haneoka-gakuen/haneoka/actions/workflows/resource-pipeline.yml/runs?per_page=30")[
        "workflow_runs"
    ]
    if any(run["status"] in {"queued", "in_progress", "waiting", "pending", "requested"} for run in runs):
        return
    request.update(attempts=request["attempts"] + 1, lastAttempt=now, status="intent")
    state["request"] = request
    control.write(state)
    github(
        "repos/haneoka-gakuen/haneoka/actions/workflows/resource-pipeline.yml/dispatches",
        {
            "ref": "main",
            "inputs": {"server": server, "rebuild_site": True},
        },
    )
    request["status"] = "accepted"
    control.write(state)
    print(f"Requested {server} resource refresh ({request['attempts']}/3)", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", choices=["jp", "intl"], required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--no-dispatch", action="store_true")
    args = parser.parse_args()
    from core.config import load_server_config
    from core.private_config import redact_private_text

    config = load_server_config(args.server)
    args.workspace.mkdir(parents=True, exist_ok=True)
    store = Store(config.r2_bucket, args.workspace / "objects")
    control = ControlState(store, args.server)
    state = control.read()
    snapshot = Snapshot(store, args.server)
    model_input = digest(
        b"cubism-portable-v1"
        + encode(
            [
                entry
                for path, entry in sorted(snapshot.entries.items())
                if (path.startswith("runtime/live2d/") and not path.endswith(".gz"))
                or (path.startswith("runtime/unity/") and "/Live2D/" in path)
            ]
        )
    )
    published_source = store.json(
        f"servers/{args.server}/sources/{snapshot.manifest['sourceId']}/source.json"
    )
    published_master = published_source["master"]
    published_info = master_version(published_master["version"], published_master["resourceVersion"])
    if (
        state.get("versionSchema") != 2
        and state.get("feedRelease") == snapshot.pointer["releaseId"]
        and state.get("modelAssets")
    ):
        state["modelInput"] = model_input
    story_input = digest(
        Path(__file__).with_name("story_text.py").read_bytes()
        + encode(
            [
                entry
                for path, entry in sorted(snapshot.entries.items())
                if path.startswith("api/v1/catalog/stories/entities/")
            ]
        )
    )
    state["publishedRelease"] = snapshot.pointer["releaseId"]
    state["publishedMaster"] = snapshot.json("objects/master/master.json")["systemVersion"]
    try:
        signal = upstream_signal(args.server)
    except Exception as error:
        print(f"::warning::External signal unavailable: {type(error).__name__}")
        signal = state.get("resourceSignal", {})
    pending = state.get("pendingSignal")
    if pending and pending["baseRelease"] != state["publishedRelease"]:
        state.pop("pendingSignal")
    if signal and state.get("resourceSignal") != signal:
        if state.get("resourceSignal") is not None:
            state["pendingSignal"] = {"signal": signal, "baseRelease": state["publishedRelease"]}
        state["resourceSignal"] = signal
    version, tables = state.get("officialMaster"), None
    error = None
    if args.event != "workflow_run":
        print(f"Probe official {args.server} Master", flush=True)
        try:
            tables, version, generation = official_master(config, args.workspace, state.get("officialMaster"))
            state["officialGeneration"] = generation
            state["officialMaster"] = version
        except Exception as exception:
            error = redact_private_text(str(exception))
            print("::warning::Official Master probe: " + error, flush=True)
    # Retain a newer direct Master feed when a resource completion arrives later.
    if (
        tables is None
        and state.get("feedMasterVersion")
        and state.get("feedRelease") == state["publishedRelease"]
        and state.get("modelInput") == model_input
        and state.get("storyInput") == story_input
        and state.get("versionSchema") == 2
    ):
        control.write(state)
        if args.event != "workflow_run" and not args.no_dispatch:
            reconcile(args.server, state, control, version, signal, args.retry)
        return
    with tempfile.TemporaryDirectory(prefix="companion-feed-") as temporary:
        output = Path(temporary)
        if tables is None and state.get("feedMasterAsset"):
            from public_snapshot import read_http

            raw = read_http(state["feedMasterAsset"]["url"])
            if digest(raw) != state["feedMasterAsset"]["sha256"]:
                raise ValueError("Previous Master feed hash mismatch")
            (output / "masterdata.tar.gz").write_bytes(raw)
            version = state["feedMasterVersion"]
        else:
            if tables is None:
                # Bootstrap from the complete published generation if discovery is unavailable.
                tables = output / "master"
                tables.mkdir()
                for path in snapshot.entries:
                    if path.startswith("objects/master/Master") and path.endswith(".json"):
                        (tables / path.rsplit("/", 1)[1]).write_bytes(snapshot.read(path))
                version = state["publishedMaster"]
            with tarfile.open(output / "masterdata.tar.gz", "w:gz") as archive:
                for file in sorted(tables.glob("Master*.json")):
                    archive.add(file, arcname=file.name)
        generation = (
            state.get("officialGeneration")
            if version == state.get("officialMaster")
            else published_master["resourceVersion"]
        )
        if not generation and version == published_master["version"]:
            generation = published_master["resourceVersion"]
        info = master_version(version, generation)
        table_names = [
            "MasterAdv",
            "MasterAdvChat",
            "MasterAdvPlayTime",
            "MasterLiveMusic",
            "MasterLiveMusicScore",
        ]
        table_data = {
            f"objects/master/{name}.json": base64.b64encode(
                snapshot.read(f"objects/master/{name}.json")
            ).decode()
            for name in table_names
            if f"objects/master/{name}.json" in snapshot.entries
        }
        (output / "tables.json.gz").write_bytes(gzip.compress(encode(table_data), mtime=0))
        (output / "resources.json.gz").write_bytes(gzip.compress(encode(snapshot.manifest), mtime=0))
        (output / "spine.json.gz").write_bytes(gzip.compress(snapshot.read("metadata/spine.json"), mtime=0))
        if state.get("modelInput") != model_input or not state.get("modelAssets"):
            print(f"Build complete {args.server} Live2D transfer archive", flush=True)
            model_root = args.workspace / "model-product"
            os.environ["HANEOKA_EXPORT_WORKERS"] = "48"
            model_metadata = export_to("live2d", model_root, snapshot)
            write_json(
                model_root / "manifest.json",
                {"schema": "haneoka-live2d-v1", "servers": {args.server: model_metadata}},
            )
            parts = package(model_root, args.workspace / "model-archives", args.server)
            model_parts = []
            for part in parts:
                name = "live2d-" + part.name
                part.rename(output / name)
                model_parts.append(name)
            model_assets = {}
        else:
            model_assets = state["modelAssets"]
            model_parts = state["modelParts"]
            model_metadata = state["modelMetadata"]
        if state.get("storyInput") != story_input or not state.get("storyAssets"):
            story_root = args.workspace / "story-product"
            os.environ["HANEOKA_EXPORT_WORKERS"] = "48"
            story_metadata = export_to("stories", story_root, snapshot)
            write_json(
                story_root / "manifest.json",
                {"schema": "haneoka-stories-v2", "servers": {args.server: story_metadata}},
            )
            story_parts = []
            for part in package(story_root, args.workspace / "story-archives", args.server):
                name = "stories-" + part.name
                part.rename(output / name)
                story_parts.append(name)
            story_assets = {}
        else:
            story_parts, story_assets, story_metadata = (
                state["storyParts"],
                state["storyAssets"],
                state["storyMetadata"],
            )
        asset_records = {
            file.name: {"sha256": digest(file.read_bytes()), "bytes": file.stat().st_size}
            for file in output.iterdir()
            if file.is_file() and file.name != "feed.json"
        }
        asset_records.update(model_assets)
        asset_records.update(story_assets)
        identity = {
            "server": args.server,
            "releaseId": state["publishedRelease"],
            "masterVersion": version,
            "assets": asset_records,
        }
        tag = "feed-" + dataset_tag(args.server, info) + "-" + digest(encode(identity))[:8]
        feed = {
            **identity,
            "masterSha256": asset_records["masterdata.tar.gz"]["sha256"],
            "masterVersion": info["version"],
            "masterHash": info["hash"],
            "publishedMaster": published_info,
            "versionSchema": 2,
            "stories": {"parts": story_parts, "metadata": story_metadata, "input": story_input},
            "tag": tag,
            "models": {"parts": model_parts, "metadata": model_metadata, "input": model_input},
        }
        for name, record in asset_records.items():
            record.setdefault(
                "url", f"https://github.com/haneoka-gakuen/haneoka/releases/download/{tag}/{name}"
            )
        write_json(output / "feed.json", feed)
        exists = (
            subprocess.run(
                ["gh", "release", "view", tag, "--repo", "haneoka-gakuen/haneoka"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
        if not exists:
            notes = output / "notes.md"
            notes.write_text(
                "Sealed companion-data producer input. Domain repositories publish complete user-facing datasets.\n"
            )
            subprocess.run(
                [
                    "gh",
                    "release",
                    "create",
                    tag,
                    "--repo",
                    "haneoka-gakuen/haneoka",
                    "--target",
                    os.environ["GITHUB_SHA"],
                    "--prerelease",
                    "--latest=false",
                    "--title",
                    f"{args.server.upper()} companion data feed",
                    "--notes-file",
                    str(notes),
                ],
                check=True,
            )
        subprocess.run(
            [
                "gh",
                "release",
                "upload",
                tag,
                "--repo",
                "haneoka-gakuen/haneoka",
                "--clobber",
                *map(
                    str,
                    [
                        file
                        for file in output.iterdir()
                        if file.is_file() and file.name not in {"notes.md", "feed.json"}
                    ],
                ),
                str(output / "feed.json"),
            ],
            check=True,
        )
        pointer = Repository("haneoka", args.workspace, "companion-feeds")
        write_json(pointer.root / f"{args.server}.json", feed)
        pointer.save([f"{args.server}.json"], f"Publish sealed {args.server} companion feed")
        state.update(
            feedMasterVersion=version,
            feedMasterAsset=feed["assets"]["masterdata.tar.gz"],
            feedRelease=state["publishedRelease"],
            modelInput=model_input,
            modelAssets={name: feed["assets"][name] for name in model_parts},
            modelMetadata=model_metadata,
            modelParts=model_parts,
            versionSchema=2,
            storyInput=story_input,
            storyAssets={name: feed["assets"][name] for name in story_parts},
            storyParts=story_parts,
            storyMetadata=story_metadata,
        )
        state["probeError"] = error
        control.write(state)
        print(f"Published complete producer feed {tag}", flush=True)
    if args.event != "workflow_run" and not args.no_dispatch:
        reconcile(args.server, state, control, version, signal, args.retry)


if __name__ == "__main__":
    main()
