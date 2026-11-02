"""The web app's signed bundle (docs/webchat.md, Code integrity).

The bot hashes every file under `webui/` when it starts and signs the
list with its Ed25519 key (relay_crypto.sign_manifest). It serves the
signed document at `<base>/manifest.sig.json` and at `/api/bundle`; the
document is never a file in the repository. The phone's service worker
checks each file it serves against it, so a copy of the app served from
somewhere else (the relay's) runs only when it is the code the bot signed.

`Bundle` is one signing: the manifest, the served document, its bundle
hash, and the check of the files on disk against it. `built_at` is the
newest modification time among the files, so the hash changes only when a
file does, not at every start.

The signing also covers the security headers the app's page is served
with (its policy among them): the bot writes them as one more file of the
app, `headers.json` ({path: {header: value}}), which the manifest names by
its sha256 like the others. A phone that keeps a release it took through
the relay then keeps the release's own policy with it. A file, not a field
of the manifest: a phone that runs an earlier release checks the manifest's
shape exactly, and takes a file it does not know as one more file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from . import relay_crypto as rc

MANIFEST_NAME = "manifest.sig.json"
# The security headers of the app's pages, a file the bot writes and signs.
HEADERS_NAME = "headers.json"
# The code tree this module runs from: an install's `.version` (the release
# tag the updater wrote) and `.deployed-commit` lie at its root.
TREE = Path(__file__).resolve().parents[3]
_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hashes(root: Path) -> tuple[dict[str, str], int, list[str]]:
    """(each file's path under `root` -> its sha256 hex, the newest
    modification time, the paths left out). A dotfile, a path the
    manifest cannot name, a link that leaves `root` and the manifest's
    own name are left out; a missing `root` holds no files."""
    root = Path(root)
    files: dict[str, str] = {}
    skipped: list[str] = []
    newest = 0
    if not root.is_dir():
        return files, newest, skipped
    top = root.resolve()
    for here, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            path = Path(here) / name
            rel = path.relative_to(root).as_posix()
            if name.startswith(".") or rel == MANIFEST_NAME:
                continue
            try:
                real = path.resolve()
                if not rc.MANIFEST_PATH_RE.match(rel) or not real.is_relative_to(top) \
                        or not real.is_file():
                    skipped.append(rel)
                    continue
                files[rel] = sha256_hex(real.read_bytes())
                newest = max(newest, int(real.stat().st_mtime))
            except OSError:
                skipped.append(rel)
    return files, newest, skipped


def release(tree: Path = TREE) -> str:
    """The release the bot runs, as a device record keeps it: the tag in
    `.version` ("v1.2.3"), else "commit <short>" from `.deployed-commit`,
    else "" (a checkout the updater never wrote)."""
    def read(name: str) -> str:
        try:
            return (Path(tree) / name).read_text().strip()
        except (OSError, UnicodeDecodeError):
            return ""
    tag, commit = read(".version"), read(".deployed-commit")
    if _TAG_RE.match(tag):
        return tag
    return f"commit {commit[:12]}" if _COMMIT_RE.match(commit) else ""


def headers_file(headers: dict[str, dict[str, str]]) -> bytes:
    """The bytes of `headers.json`: header names lowercased, as canonical
    JSON (keys sorted, no spaces)."""
    block = {path: {k.lower(): str(v) for k, v in named.items()}
             for path, named in headers.items()}
    return json.dumps(block, sort_keys=True, separators=(",", ":")).encode()


def diff(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """The paths whose hash differs between two file maps, or that only
    one of them names."""
    return sorted(p for p in set(old) | set(new) if old.get(p) != new.get(p))


class Bundle:
    """One signing of the app's files: `manifest`, the served document
    (`doc`, as bytes in `body`), and `hash`, the bundle hash. With
    `headers` ({path: {header: value}}, for files it names) the bot's own
    `headers.json` is one of the files (`written`); a file of that name on
    disk is left out."""

    def __init__(self, root: str | os.PathLike, private: bytes,
                 headers: dict[str, dict[str, str]] | None = None) -> None:
        self.root = Path(root)
        files, newest, self.skipped = file_hashes(self.root)
        self.written: dict[str, bytes] = {}
        if headers:
            self.written[HEADERS_NAME] = headers_file(headers)
            if files.pop(HEADERS_NAME, None):
                self.skipped.append(HEADERS_NAME)
            files.update(self._written_hashes())
        _priv, public = rc.signing_keypair(private)
        self.manifest = {"v": rc.MANIFEST_VERSION, "files": files, "built_at": newest,
                         "key": rc.b64e(public)}
        self.doc = rc.sign_manifest(self.manifest, private)
        self.body = json.dumps(self.doc, sort_keys=True, separators=(",", ":")).encode()
        self.hash = rc.bundle_hash(self.manifest)

    @property
    def files(self) -> dict[str, str]:
        return self.manifest["files"]

    @property
    def public(self) -> str:
        return self.manifest["key"]

    def _written_hashes(self) -> dict[str, str]:
        return {name: sha256_hex(data) for name, data in self.written.items()}

    def changes(self) -> list[str]:
        """The files on disk that no longer match this signing: changed,
        added or gone."""
        now = file_hashes(self.root)[0]
        if self.written:
            now = {**{k: v for k, v in now.items() if k not in self.written},
                   **self._written_hashes()}
        return diff(self.files, now)

    def matches(self, rel: str, data: bytes) -> bool:
        """Whether `data`, served as `rel`, is the file this signing names."""
        return self.files.get(rel) == sha256_hex(data)
