"""Read immutable published resources and verify every materialized object."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path, PurePosixPath


def encode(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode(value))


def safe_path(value: str) -> str:
    parts = PurePosixPath(value).parts
    if not parts or value.startswith("/") or "\\" in value or any(x in {".", ".."} for x in parts):
        raise ValueError("Unsafe resource path")
    return value


class Store:
    def __init__(self, bucket: str, cache: Path):
        import boto3
        from botocore.config import Config

        self.bucket, self.cache = bucket, cache
        cache.mkdir(parents=True, exist_ok=True)
        self.client = boto3.client(
            "s3",
            endpoint_url=os.environ.get("R2_ENDPOINT")
            or f"https://{os.environ['CLOUDFLARE_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            region_name="auto",
            config=Config(
                max_pool_connections=64, connect_timeout=10, read_timeout=30, retries={"max_attempts": 3}
            ),
        )

    def json(self, key: str) -> dict:
        return json.loads(self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read())

    def put_json(self, key: str, value: dict) -> None:
        if not key.startswith("private/companion-data/"):
            raise ValueError("Control state must stay in the companion namespace")
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=encode(value), ContentType="application/json"
        )

    def object(self, entry: dict) -> bytes:
        sha = entry["sha256"]
        path = self.cache / sha
        if path.exists():
            raw = path.read_bytes()
            if len(raw) == entry["bytes"] and digest(raw) == sha:
                return raw
        key = f"cas/v1/sha256/{sha[:2]}/{sha}"
        raw = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        if len(raw) != entry["bytes"] or digest(raw) != sha:
            raise ValueError(f"Resource hash mismatch: {entry['path']}")
        temp = path.with_suffix(f".{threading.get_ident()}.tmp")
        temp.write_bytes(raw)
        temp.replace(path)
        return raw


class Snapshot:
    def __init__(self, store: Store, server: str):
        self.store, self.server = store, server
        self.pointer = store.json(f"servers/{server}/current.json")
        self.manifest = store.json(self.pointer["releaseManifest"])
        if self.manifest["releaseId"] != self.pointer["releaseId"] or self.manifest["server"] != server:
            raise ValueError("Published resource identity mismatch")
        self.entries = {entry["path"]: entry for entry in self.manifest["entries"]}

    def read(self, path: str) -> bytes:
        return self.store.object(self.entries[path])

    def json(self, path: str) -> object:
        return json.loads(self.read(path))
