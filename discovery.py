"""Read official Master data and normalize external update signals."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

from exporters import parallel
from storage import digest


def github(path: str, payload: dict | None = None):
    args = ["gh", "api", path]
    if payload is not None:
        args += ["--method", "POST", "--input", "-"]
    output = subprocess.check_output(args, input=None if payload is None else json.dumps(payload).encode())
    return json.loads(output) if output else None


def upstream_signal(server: str) -> dict:
    url = "https://raw.githubusercontent.com/StarMoe-org/moenotes-masterdata/main/current_version.json"
    with urlopen(Request(url, headers={"User-Agent": "Haneoka-Data-Pipeline"}), timeout=30) as response:
        regions = json.load(response)["regions"]
    names = ["jp"] if server == "jp" else ["en", "hk-tw-mo", "kr"]
    return {
        name: {
            key: regions[name].get(key)
            for key in ("version", "resource_version", "resource_hash", "manifest_sha256")
        }
        for name in names
        if name in regions
    }


def official_master(config, workspace: Path, previous: str | None) -> tuple[Path | None, str, str]:
    from extract.master import extract_master, validate_master_manifest
    from ingest.master import discover_master_version
    from ingest.version_api import proxy_from_env

    version, generation = discover_master_version(
        config.master_version_endpoint,
        skip_resolution_check=config.skip_public_resolution_check,
        proxy=proxy_from_env(config.version_proxy_env),
    )
    if version == previous:
        return None, version, generation
    root = workspace / "official-master"
    folder = root / "master"
    folder.mkdir(parents=True, exist_ok=True)
    remote = f"{config.master_remote_root}/{version}"

    def download(name: str) -> bytes:
        # Master objects are public; service endpoints and crypto stay environment scoped.
        return subprocess.check_output(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--retry",
                "3",
                "--retry-all-errors",
                "--max-time",
                "30",
                "--max-filesize",
                "67108864",
                f"{remote}/{name}",
            ],
            stderr=subprocess.PIPE,
        )

    manifest_raw = download("MasterManifest.json")
    manifest = validate_master_manifest(json.loads(manifest_raw), expected_version=version)
    (folder / "MasterManifest.json").write_bytes(manifest_raw)

    def one(entry):
        raw = download(entry["name"])
        if len(raw) != entry["size"] or digest(raw) != entry["hash"]:
            raise ValueError(f"Master hash mismatch: {entry['name']}")
        (folder / entry["name"]).write_bytes(raw)

    parallel(one, manifest["files"])
    output = workspace / "decoded-master"
    extract_master(Path("unused"), output, config, snapshot_root=root)
    return output, version, generation
