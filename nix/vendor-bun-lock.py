#!/usr/bin/env python3
"""Build a platform-independent node_modules tree from a Bun lockfile.

Bun lockfiles are JSON-with-trailing-commas. Every package tarball listed in
the lock (including TypeScript's per-OS optional binaries) is fetched from the
npm registry, checked against the lockfile sha512, and extracted under
node_modules/. The result does not depend on which OS runs the script, so one
Nix fixed-output hash covers aarch64-darwin, x86_64-linux, aarch64-linux, and
x86_64-darwin.

Both MCP packages declare the same dependency closure. The second lockfile is
checked against the first and then ignored.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def load_jsonc(path: Path) -> dict:
    text = _TRAILING_COMMA.sub(r"\1", path.read_text())
    return json.loads(text)


def split_ident(ident: str) -> tuple[str, str]:
    if ident.startswith("@"):
        name, version = ident[1:].rsplit("@", 1)
        return "@" + name, version
    name, version = ident.rsplit("@", 1)
    return name, version


def tarball_url(name: str, version: str) -> str:
    if name.startswith("@"):
        bare = name.split("/", 1)[1]
        return f"https://registry.npmjs.org/{name}/-/{bare}-{version}.tgz"
    return f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz"


def fetch(url: str, sri: str) -> bytes:
    algo, _, digest = sri.partition("-")
    if algo != "sha512" or not digest:
        raise SystemExit(f"unsupported integrity {sri} for {url}")
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    got = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if got != digest:
        raise SystemExit(f"sha512 mismatch for {url}")
    return data


def extract_npm_tarball(data: bytes, dest: Path) -> None:
    """Extract an npm tarball, dropping its single top-level directory.

    Registry tarballs usually wrap files in ``package/``. ``@types/node``
    publishes a wrapper named like ``node v22.20/`` instead. Either way the
    first path segment is not part of the installed package.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        members = tf.getmembers()
        roots = {m.name.split("/", 1)[0] for m in members if m.name}
        if len(roots) != 1:
            raise SystemExit(f"expected one tarball root, got {sorted(roots)}")
        for member in members:
            name = member.name
            rel = name.split("/", 1)[1] if "/" in name else ""
            if not rel or rel.endswith("/"):
                continue
            parts = Path(rel).parts
            if any(part == ".." for part in parts):
                raise SystemExit(f"unsafe tarball path {name}")
            target = dest.joinpath(*parts)
            if member.issym():
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or target.exists():
                    target.unlink()
                target.symlink_to(member.linkname)
                continue
            if not member.isfile():
                continue
            extracted = tf.extractfile(member)
            if extracted is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(extracted.read())
            mode = member.mode & 0o777
            target.chmod(0o755 if mode & 0o111 else 0o644)


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(f"usage: {sys.argv[0]} exa.bun.lock brave.bun.lock $out")
    exa_lock = load_jsonc(Path(sys.argv[1]))
    brave_lock = load_jsonc(Path(sys.argv[2]))
    if exa_lock.get("packages") != brave_lock.get("packages"):
        raise SystemExit("mcp/exa and mcp/brave bun.lock package closures differ")
    out = Path(sys.argv[3])
    node_modules = out / "node_modules"
    node_modules.mkdir(parents=True)
    for key in sorted(exa_lock["packages"]):
        spec = exa_lock["packages"][key]
        ident, sri = spec[0], spec[3]
        name, version = split_ident(ident)
        print(f"fetch {name}@{version}", flush=True)
        data = fetch(tarball_url(name, version), sri)
        extract_npm_tarball(data, node_modules / name)
    bindir = node_modules / ".bin"
    bindir.mkdir()
    (bindir / "tsc").symlink_to("../typescript/bin/tsc")


if __name__ == "__main__":
    main()
