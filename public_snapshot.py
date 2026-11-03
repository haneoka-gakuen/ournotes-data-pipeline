"""Consume sealed public feeds and verify all public resource bytes."""

from __future__ import annotations

import base64
import gzip
import json
import time
import hashlib
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from storage import digest


def read_http(url: str) -> bytes:
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "Haneoka-Data-Pipeline/0.1.0"})
            with urlopen(request, timeout=45) as response:
                return response.read()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("HTTP transfer failed")


def feed_for(server: str) -> dict:
    return json.loads(
        read_http(f"https://raw.githubusercontent.com/haneoka-gakuen/haneoka/companion-feeds/{server}.json")
    )


def feed_asset(feed: dict, name: str) -> bytes:
    entry = feed["assets"][name]
    raw = read_http(entry["url"])
    if len(raw) != entry["bytes"] or digest(raw) != entry["sha256"]:
        raise ValueError(f"Feed asset hash mismatch: {name}")
    return raw


def download_asset(feed: dict, name: str, output: Path) -> None:
    entry = feed["assets"][name]
    for attempt in range(3):
        try:
            request = Request(entry["url"], headers={"User-Agent": "Haneoka-Data-Pipeline/0.1.0"})
            hasher, size = hashlib.sha256(), 0
            with urlopen(request, timeout=60) as response, output.open("wb") as target:
                while chunk := response.read(1024 * 1024):
                    target.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
            if size != entry["bytes"] or hasher.hexdigest() != entry["sha256"]:
                raise ValueError(f"Feed asset hash mismatch: {name}")
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2**attempt)


class PublicSnapshot:
    def __init__(self, feed: dict):
        self.server = feed["server"]
        self.manifest = json.loads(gzip.decompress(feed_asset(feed, "resources.json.gz")))
        self.pointer = {"releaseId": self.manifest["releaseId"]}
        if self.manifest["server"] != self.server or self.manifest["releaseId"] != feed["releaseId"]:
            raise ValueError("Feed resource identity mismatch")
        self.entries = {entry["path"]: entry for entry in self.manifest["entries"]}
        self.tables = json.loads(gzip.decompress(feed_asset(feed, "tables.json.gz")))
        self.spine = gzip.decompress(feed_asset(feed, "spine.json.gz"))

    def read(self, path: str) -> bytes:
        if path in self.tables:
            raw = base64.b64decode(self.tables[path])
        elif path == "metadata/spine.json":
            raw = self.spine
        else:
            tree, relative = path.split("/", 1)
            if tree not in {"assets", "runtime"}:
                raise ValueError(f"Resource is not exposed by this feed: {path}")
            url = f"https://haneoka.org/{tree}/{self.server}/{quote(relative, safe='/')}?release={self.pointer['releaseId']}&download=1"
            raw = read_http(url)
        expected = self.entries[path]
        if len(raw) != expected["bytes"] or digest(raw) != expected["sha256"]:
            raise ValueError(f"Resource hash mismatch: {path}")
        return raw

    def json(self, path: str):
        return json.loads(self.read(path))
