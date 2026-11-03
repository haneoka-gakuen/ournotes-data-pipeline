"""Publish complete datasets through bounded Git transfers."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from versions import dataset_tag

ORG = "haneoka-gakuen"
REPOSITORIES = {
    kind: f"ournotes-{kind}" for kind in ("masterdata", "stories", "charts", "live2d", "spine", "assets")
}
REPOSITORIES["assets"] = "ournotes-resource-catalog"


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


class Repository:
    def __init__(self, name: str, workspace: Path, branch="main", local_root: Path | None = None):
        if (
            os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("GITHUB_REPOSITORY") != f"{ORG}/{name}"
        ):
            raise ValueError("Publication token must stay inside its owning repository")
        self.name = name
        self.root = local_root or workspace / name / branch
        self.root.parent.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--depth",
                    "1",
                    "--branch",
                    branch,
                    f"https://github.com/{ORG}/{name}.git",
                    str(self.root),
                ],
                check=True,
            )
        git(self.root, "config", "user.name", "haneoka bot")
        git(self.root, "config", "user.email", "bot@haneoka.org")
        self.base_head = git(self.root, "rev-parse", "HEAD")
        self.branch = branch
        self.stage_heads: dict[str, str] = {}

    def push(self, *refs: str, lease: str | None = None) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "credential.helper=",
                "-c",
                "credential.helper=!gh auth git-credential",
                "push",
                "--quiet",
                *(["--force-with-lease=" + lease] if lease is not None else []),
                f"https://github.com/{ORG}/{self.name}.git",
                *refs,
            ],
            check=True,
        )

    def save(self, paths: list[str], message: str) -> bool:
        git(self.root, "add", "--", *paths)
        if not git(self.root, "diff", "--cached", "--name-only"):
            return False
        git(self.root, "commit", "--quiet", "-m", message)
        self.push(f"HEAD:refs/heads/{self.branch}")
        return True

    def publish(self, server: str, metadata: dict) -> bool:
        manifest_file = self.root / "manifest.json"
        manifest = json.loads(manifest_file.read_text())
        previous = manifest["servers"].get(server)
        if previous == metadata:
            self.published_tag = self.ensure_tag(server, metadata)
            return False
        manifest["servers"][server] = metadata
        # Transfer objects on a temporary branch, then expose the complete tree.
        changed = set(git(self.root, "ls-files", "--others", "--exclude-standard", "--", server).splitlines())
        changed.update(git(self.root, "diff", "--name-only", "--", server).splitlines())
        batch, size = [], 0
        stage = f"refs/heads/ingest/{server}"
        for path in sorted(changed):
            batch.append(path)
            file = self.root / path
            size += file.stat().st_size if file.exists() else 0
            if size >= 128 * 1024 * 1024 or len(batch) >= 400:
                self._batch(batch, server, stage)
                batch, size = [], 0
        if batch:
            self._batch(batch, server, stage)
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        git(self.root, "add", "--", "manifest.json")
        git(
            self.root,
            "commit",
            "--quiet",
            "-m",
            f"Publish complete {server} snapshot {metadata['sha256'][:12]}",
        )
        git(self.root, "fetch", "--quiet", "origin", self.branch)
        remote_head = git(self.root, "rev-parse", "FETCH_HEAD")
        if remote_head != self.base_head:
            git(self.root, "rebase", "--onto", remote_head, self.base_head)
        self.push(f"HEAD:refs/heads/{self.branch}")
        self.base_head = git(self.root, "rev-parse", "HEAD")
        self.published_tag = self.ensure_tag(server, metadata)
        if changed:
            self.push(f":{stage}", lease=f"{stage}:{self.stage_heads[stage]}")
        return True

    def ensure_tag(self, server: str, metadata: dict) -> str:
        tag = dataset_tag(server, metadata)
        base = tag
        revision = 1
        while git(self.root, "ls-remote", "--tags", "origin", f"refs/tags/{tag}"):
            git(self.root, "fetch", "--quiet", "--depth", "1", "origin", f"refs/tags/{tag}")
            previous = json.loads(git(self.root, "show", "FETCH_HEAD:manifest.json"))["servers"].get(
                server, {}
            )
            if previous.get("sha256") == metadata["sha256"] and previous.get("hash") == metadata["hash"]:
                return tag
            revision += 1
            tag = f"{base}-{metadata['hash'][:12]}" + (f"-r{revision}" if revision > 2 else "")
        git(self.root, "tag", "-a", tag, "-m", f"{server} Master {metadata['version']}")
        self.push(f"refs/tags/{tag}")
        return tag

    def _batch(self, paths: list[str], server: str, stage: str) -> None:
        if stage not in self.stage_heads:
            remote = git(self.root, "ls-remote", "origin", stage)
            self.stage_heads[stage] = remote.split()[0] if remote else ""
        git(self.root, "add", "--", *paths)
        git(self.root, "commit", "--quiet", "-m", f"Stage {server} dataset files")
        self.push(f"HEAD:{stage}", lease=f"{stage}:{self.stage_heads[stage]}")
        self.stage_heads[stage] = git(self.root, "rev-parse", "HEAD")
