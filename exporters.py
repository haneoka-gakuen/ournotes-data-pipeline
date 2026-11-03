"""Build standalone, complete data trees from one immutable resource snapshot."""

from __future__ import annotations

import json
import os
import posixpath
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlsplit

from storage import Snapshot, digest, encode, safe_path


def parallel(function, values) -> None:
    with ThreadPoolExecutor(max_workers=int(os.environ.get("HANEOKA_EXPORT_WORKERS", "12"))) as pool:
        try:
            list(pool.map(function, values))
        except BaseException:
            pool.shutdown(wait=True, cancel_futures=True)
            raise


class Dataset:
    def __init__(self, root: Path, server: str):
        self.root, self.server = root, server
        self.files: dict[str, dict] = {}

    def put(self, path: str, raw: bytes) -> None:
        path = safe_path(path)
        target = self.root / self.server / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        self.files[path] = {"path": path, "sha256": digest(raw), "bytes": len(raw)}

    def json(self, path: str, value: object) -> None:
        self.put(path, encode(value))

    def adopt(self, snapshot: Snapshot, source: str, target: str) -> None:
        file = self.root / self.server / safe_path(target)
        entry = snapshot.entries[source]
        if file.is_file() and file.stat().st_size == entry["bytes"]:
            raw = file.read_bytes()
            if digest(raw) == entry["sha256"]:
                self.files[target] = {"path": target, "sha256": entry["sha256"], "bytes": entry["bytes"]}
                return
        self.put(target, snapshot.read(source))

    def copy(self, snapshot: Snapshot, pairs: list[tuple[str, str]]) -> None:
        def one(pair):
            source, target = pair
            self.adopt(snapshot, source, target)

        parallel(one, pairs)

    def finish(self, **info) -> dict:
        directory = self.root / self.server
        for file in directory.rglob("*"):
            if file.is_file() and file.relative_to(directory).as_posix() not in self.files:
                file.unlink()
        entries = [self.files[path] for path in sorted(self.files)]
        return {"sha256": digest(encode(entries)), "files": len(entries), **info}


def masterdata(root: Path, server: str, tables: Path, version: str) -> dict:
    data = Dataset(root, server)
    files = sorted(tables.glob("Master*.json"))
    if not files:
        raise ValueError("Master snapshot has no tables")
    for file in files:
        value = json.loads(file.read_bytes())
        if not isinstance(value.get("_allData"), list):
            raise ValueError(f"Invalid Master table: {file.name}")
        data.json(file.name, value)
    return data.finish(version=version)


def stories(root: Path, snapshot: Snapshot) -> dict:
    from story_text import LANGUAGES, transcript

    data = Dataset(root, snapshot.server)
    paths = [
        path
        for path in snapshot.entries
        if path.startswith("api/v1/catalog/stories/entities/") and path.endswith(".json")
    ]

    def one(path):
        for identity, story in snapshot.json(path).items():
            for index, locale in enumerate(LANGUAGES):
                text = transcript(story, index)
                if text:
                    data.put(f"{locale}/{identity}.txt", text.encode("utf-8"))

    parallel(one, paths)
    return data.finish()


def charts(root: Path, snapshot: Snapshot) -> dict:
    data = Dataset(root, snapshot.server)
    prefix = "assets/Assets/AddressableResources/Live/MusicScore/"
    paths = [p for p in snapshot.entries if p.startswith(prefix) and p.endswith(".bytes")]

    def one(path):
        raw = snapshot.read(path)
        name = path.removeprefix(prefix)
        data.put("original/" + name, raw)
        if raw.lstrip().startswith(b"#"):
            data.put("sus/" + name.removesuffix(".bytes") + ".sus", raw)
        else:
            data.json("scores/" + name.removesuffix(".bytes") + ".json", json.loads(raw))

    parallel(one, paths)
    for name in ("MasterLiveMusic", "MasterLiveMusicScore"):
        data.put(f"tables/{name}.json", snapshot.read(f"objects/master/{name}.json"))
    data.json(
        "index.json",
        {
            "scores": sorted(path for path in data.files if path.startswith("scores/")),
            "sus": sorted(path for path in data.files if path.startswith("sus/")),
            "originals": sorted(path for path in data.files if path.startswith("original/")),
        },
    )
    return data.finish()


def live2d(root: Path, snapshot: Snapshot) -> dict:
    data = Dataset(root, snapshot.server)
    prefix = "runtime/live2d/"
    paths = [p for p in snapshot.entries if p.startswith(prefix) and not p.endswith(".gz")]
    data.copy(snapshot, [(p, p.removeprefix(prefix)) for p in paths if not p.endswith("/model3.json")])
    models = []
    for path in paths:
        if not path.endswith("/model3.json"):
            continue
        key = path.removeprefix(prefix).split("/")[0]
        model = snapshot.json(path)
        references = model["FileReferences"]

        # Website descriptors can reference shared textures outside their model folder.
        # Localize every reference; Cubism descriptor semantics and MOC bytes stay intact.
        def localize(reference: str, folder="") -> str:
            parsed = urlsplit(reference)
            source = unquote(parsed.path)
            if source.startswith(f"/runtime/{snapshot.server}/"):
                source = "runtime/" + source.split("/", 3)[3]
            elif source.startswith(f"/assets/{snapshot.server}/"):
                source = "assets/" + source.split("/", 3)[3]
            elif not source.startswith("/") and not parsed.scheme:
                source = posixpath.normpath(posixpath.join(posixpath.dirname(path), source))
            else:
                raise ValueError(f"External Live2D dependency: {key}")
            if source.startswith(f"{prefix}{key}/"):
                target = source.removeprefix(f"{prefix}{key}/")
            else:
                target = f"{folder or 'resources'}/{posixpath.basename(source)}"
            output = f"{key}/{target}"
            if data.files.get(output, {}).get("sha256") != snapshot.entries[source]["sha256"]:
                data.adopt(snapshot, source, output)
            return target

        for field in ("Moc", "Physics", "Pose", "UserData", "DisplayInfo"):
            if references.get(field):
                references[field] = localize(references[field])
        references["Textures"] = [localize(p, "textures") for p in references["Textures"]]
        for expression in references.get("Expressions", []):
            expression["File"] = localize(expression["File"], "expressions")
        for motions in references.get("Motions", {}).values():
            for motion in motions:
                motion["File"] = localize(motion["File"], "motions")
                if motion.get("Sound"):
                    motion["Sound"] = localize(motion["Sound"], "sounds")
        groups = references.get("Motions", {})
        aliases = dict(groups)
        idle = []
        for group, motions in groups.items():
            for motion in motions:
                stem = posixpath.basename(motion["File"]).removesuffix(".motion3.json")
                name, suffix = stem, 2
                while name in aliases:
                    name, suffix = f"{stem}-{suffix}", suffix + 1
                aliases[name] = [motion]
                if "idle" in stem.lower() or group.lower() == "idle":
                    idle.append(motion)
        if idle and "Idle" not in aliases:
            aliases["Idle"] = idle
        references["Motions"] = aliases
        sync = references.get("MotionSync")
        if isinstance(sync, str):
            references["MotionSync"] = localize(sync)
        descriptor = f"{key}/{key}.model3.json"
        data.json(descriptor, model)
        models.append({"id": key, "model": descriptor})
    if not models:
        raise ValueError("Published snapshot has no Live2D models")
    data.json("index.json", {"models": models})
    return data.finish()


def spine(root: Path, snapshot: Snapshot) -> dict:
    data = Dataset(root, snapshot.server)
    metadata = snapshot.json("metadata/spine.json")
    selected: set[str] = set()

    def visit(value):
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            candidate = value.removeprefix("/")
            if candidate in snapshot.entries and candidate.startswith(("assets/", "runtime/")):
                selected.add(candidate)

    visit(metadata)
    selected.update(p for p in snapshot.entries if p.startswith("assets/") and "/Spine/" in p)
    if not selected:
        raise ValueError("Published snapshot has no Spine dependencies")
    data.copy(snapshot, [(p, p) for p in sorted(selected)])
    index = {"schema": "haneoka-spine-index-v1", "server": snapshot.server}
    for field in [
        "models",
        "modelOrder",
        "renderRecipes",
        "renderRecipeOrder",
        "browserRuntime",
        "anonTokyo",
    ]:
        if field in metadata:
            index[field] = metadata[field]
    data.json("index.json", index)
    return data.finish()


def assets(root: Path, snapshot: Snapshot) -> dict:
    data = Dataset(root, snapshot.server)
    groups: dict[str, list] = {}
    for path, entry in sorted(snapshot.entries.items()):
        groups.setdefault(path.split("/")[0], []).append(entry)
    for group, entries in groups.items():
        data.json(f"{group}.json", entries)
    data.json("index.json", {"release": snapshot.pointer["releaseId"], "groups": sorted(groups)})
    return data.finish()


EXPORTERS = {"stories": stories, "charts": charts, "live2d": live2d, "spine": spine, "assets": assets}


def export_to(kind: str, root: Path, snapshot: Snapshot) -> dict:
    if kind not in EXPORTERS:
        raise ValueError("Unknown dataset")
    return EXPORTERS[kind](root, snapshot)
