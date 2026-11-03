"""Readable generations and exact content identities from the Master service."""

import re


def master_version(identity: str, generation: str) -> dict:
    prefix, separator, suffix = identity.rpartition("/")
    version = prefix if separator else generation
    readable = re.match(r"^(\d+\.\d+\.\d+\.\d+)(?:\.[A-Za-z0-9]+)?$", version or "")
    if not readable:
        raise ValueError("The Master service did not provide a readable generation")
    return {"version": readable.group(1), "hash": suffix if separator else identity}


def dataset_tag(server: str, metadata: dict) -> str:
    return f"{server}-{metadata['version']}"
