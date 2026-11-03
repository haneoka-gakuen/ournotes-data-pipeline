"""Package one standard Cubism model for a local player or WebGAL."""

import argparse
import hashlib
import json
import posixpath
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


def fetch_model(server: str, model: str, target: Path):
    """Materialize a complete model from one publicly accessible Haneoka release."""
    origin = "https://haneoka.org"

    def download(url):
        return subprocess.check_output(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--http2",
                "--retry",
                "3",
                "--retry-all-errors",
                "--max-time",
                "45",
                "--user-agent",
                "Haneoka-Data-Pipeline/0.1.0",
                url,
            ]
        )

    manifest = json.loads(download(f"{origin}/api/v1/servers/{server}/release"))
    entries = {item["path"]: item for item in manifest["entries"]}
    release = manifest["releaseId"]
    base = f"runtime/live2d/{model}/"

    def read(path):
        tree, relative = path.split("/", 1)
        url = f"{origin}/{tree}/{server}/{quote(relative, safe='/')}?release={release}&download=1"
        raw = download(url)
        expected = entries[path]
        if len(raw) != expected["bytes"] or hashlib.sha256(raw).hexdigest() != expected["sha256"]:
            raise ValueError(f"Source hash mismatch: {path}")
        return raw

    descriptor = json.loads(read(base + "model3.json"))
    refs = descriptor["FileReferences"]
    resources = {}

    def reference(value, folder="resources"):
        url = urlsplit(value)
        path = unquote(url.path)
        if path.startswith(f"/runtime/{server}/"):
            path = "runtime/" + path.split("/", 3)[3]
        elif path.startswith(f"/assets/{server}/"):
            path = "assets/" + path.split("/", 3)[3]
        elif not url.scheme and not path.startswith("/"):
            path = posixpath.normpath(base + path)
        else:
            raise ValueError("Model contains an external dependency")
        local = path.removeprefix(base) if path.startswith(base) else f"{folder}/{posixpath.basename(path)}"
        if local.startswith("/") or ".." in Path(local).parts or "\\" in local:
            raise ValueError("Unsafe model path")
        if local in resources and resources[local] != path:
            raise ValueError("Conflicting model resource names")
        resources[local] = path
        return local

    for field in ["Moc", "Physics", "Pose", "UserData", "DisplayInfo", "MotionSync"]:
        if isinstance(refs.get(field), str):
            refs[field] = reference(refs[field])
    refs["Textures"] = [reference(value, "textures") for value in refs["Textures"]]
    for item in refs.get("Expressions", []):
        item["File"] = reference(item["File"], "expressions")
    groups = refs.get("Motions", {})
    aliases = dict(groups)
    idle = []
    for group, motions in groups.items():
        for item in motions:
            item["File"] = reference(item["File"], "motions")
            if item.get("Sound"):
                item["Sound"] = reference(item["Sound"], "sounds")
            stem = posixpath.basename(item["File"]).removesuffix(".motion3.json")
            name, suffix = stem, 2
            while name in aliases:
                name, suffix = f"{stem}-{suffix}", suffix + 1
            aliases[name] = [item]
            if "idle" in stem.lower() or group.lower() == "idle":
                idle.append(item)
    if idle and "Idle" not in aliases:
        aliases["Idle"] = idle
    refs["Motions"] = aliases
    target.mkdir(parents=True, exist_ok=True)

    def materialize(pair):
        local, path = pair
        file = target / local
        file.parent.mkdir(parents=True, exist_ok=True)
        if file.is_file():
            existing = file.read_bytes()
            if (
                len(existing) == entries[path]["bytes"]
                and hashlib.sha256(existing).hexdigest() == entries[path]["sha256"]
            ):
                return
        file.write_bytes(read(path))

    with ThreadPoolExecutor(max_workers=8) as pool:
        try:
            list(pool.map(materialize, resources.items()))
        except BaseException:
            pool.shutdown(wait=True, cancel_futures=True)
            raise
    (target / f"{model}.model3.json").write_text(json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("server", choices=["jp", "intl"])
    parser.add_argument("model")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch a hash-verified model from a pinned public Haneoka release",
    )
    parser.add_argument("--resume", action="store_true", help="Resume an interrupted verified fetch")
    args = parser.parse_args()
    root = Path(args.server) / args.model
    descriptor = root / f"{args.model}.model3.json"
    if args.fetch:
        if root.exists() and not args.resume:
            parser.error("Use an empty directory for --fetch to preserve an existing model")
        fetch_model(args.server, args.model, root)
    if not descriptor.is_file():
        parser.error("Model not found")
    model = json.loads(descriptor.read_text())
    refs = model["FileReferences"]
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
    files = {descriptor.resolve()}
    for path in paths:
        file = (root / path).resolve()
        if not file.is_relative_to(root.resolve()) or not file.is_file():
            parser.error(f"Missing local model resource: {path}")
        files.add(file)
    output = args.output or Path(f"{args.model}.zip")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(files):
            archive.write(file, Path(args.model) / file.relative_to(root.resolve()))
    print(output)


if __name__ == "__main__":
    main()
