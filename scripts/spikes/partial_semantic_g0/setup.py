"""Installation-only, checksum-pinned source download; never imported by runtime.

No compiler/backend is executed. Archives are small official public sources; extraction
rejects links/devices, traversal, duplicate members and oversized inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024


def extract(data: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        roots = {PurePosixPath(m.name).parts[0] for m in members if m.name}
        if len(roots) != 1 or sum(m.size for m in members) > MAX_EXPANDED:
            raise ValueError("invalid archive root or expansion limit")
        seen = set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
                raise ValueError("unsafe archive member")
            relative = Path(*path.parts[1:])
            if relative == Path("."):
                continue
            if relative in seen:
                raise ValueError("duplicate archive member")
            seen.add(relative)
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                assert stream is not None
                target.write_bytes(stream.read())
                target.chmod(0o755 if member.mode & 0o111 else 0o644)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((HERE / "sources.json").read_text())
    for name, item in manifest.items():
        if not item["url"].startswith("https://codeload.github.com/"):
            raise ValueError("only official codeload archives are permitted")
        with urllib.request.urlopen(item["url"], timeout=90) as response:
            data = response.read(MAX_ARCHIVE + 1)
        if len(data) > MAX_ARCHIVE or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError(f"checksum/size mismatch: {name}")
        extract(data, destination / name)
    (destination / "sources.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
