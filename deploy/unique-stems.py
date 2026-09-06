#!/usr/bin/env python3
# Margin — self-hosted read-later server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Give every saved item a stem no other item shares.

A stem — "2026-07-19-a-title" — is how the whole app names an item: the URL
index keys on it, the archive and delete forms post it, the service worker is
told to forget it. None of those carry a folder alongside it. Margin used to
allocate stems per folder, so the inbox and `archive/` could each hold a
different item called "2026-07-19-a-title", and then archiving one rewrote the
other's index entry: its URL resolved to a document it had nothing to do with.

Allocation now spans both folders, which stops new collisions. This repairs
the ones an older version already made, by renaming the archived family and
following it in the index.

    python3 deploy/unique-stems.py --output-dir ~/ReadLater/inbox
    python3 deploy/unique-stems.py --output-dir ~/ReadLater/inbox --apply

Reports by default and changes nothing; --apply does the renames. Run it with
the server stopped, so a save cannot allocate a name underneath it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

SERVE_EXTS = (".pdf", ".md", ".tex", ".org")
ARCHIVE_SUBDIR = "archive"
INDEX_NAME = ".saved-urls.json"


def stems_in(folder: Path) -> dict[str, list[Path]]:
    """Stem → its files, for the files this app serves."""
    found: dict[str, list[Path]] = {}
    if not folder.is_dir():
        return found
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.suffix.lower() in SERVE_EXTS:
            found.setdefault(path.stem, []).append(path)
    return found


def free_stem(stem: str, inbox: dict, archived: dict) -> str:
    """The first "<stem>-N" no file in either folder is using."""
    i = 2
    while f"{stem}-{i}" in inbox or f"{stem}-{i}" in archived:
        i += 1
    return f"{stem}-{i}"


def source_url_of(files: list[Path]) -> str | None:
    """The source_url recorded in the family's Markdown, if it has any.

    Front matter is the only thing that says which URL an archived family
    belongs to; a PDF-only capture carries nothing, and for those the index
    cannot be repaired automatically.
    """
    for path in files:
        if path.suffix.lower() != ".md":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        for line in text[3:end if end > 0 else len(text)].splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "source_url":
                return value.strip().strip('"').strip("'") or None
    return None


def write_index(path: Path, index: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true",
                        help="perform the renames (default: report only)")
    args = parser.parse_args()

    root: Path = args.output_dir.expanduser()
    if not root.is_dir():
        print(f"{root} is not a directory", file=sys.stderr)
        return 2
    archive = root / ARCHIVE_SUBDIR

    inbox, archived = stems_in(root), stems_in(archive)
    clashes = sorted(set(inbox) & set(archived))
    if not clashes:
        print(f"{len(inbox)} in the inbox, {len(archived)} archived, "
              "no stem shared between them — nothing to do.")
        return 0

    index_path = root / INDEX_NAME
    index: dict[str, list[str]] = {}
    if index_path.is_file():
        try:
            loaded = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                index = {k: list(v) for k, v in loaded.items()
                         if isinstance(v, list)}
        except (OSError, ValueError) as exc:
            print(f"could not read {INDEX_NAME} ({exc}); "
                  "renaming files but leaving the index alone", file=sys.stderr)

    unresolved = []
    for stem in clashes:
        new = free_stem(stem, inbox, archived)
        files = archived[stem]
        print(f"\n{stem}")
        print(f"  inbox:    {', '.join(p.name for p in inbox[stem])}")
        print(f"  archived: {', '.join(p.name for p in files)}  ->  {new}")

        url = source_url_of(files)
        holders = [u for u, stems in index.items() if stem in stems]
        if url and url in index:
            print(f"  index:    {url} -> {new}")
        elif len(holders) == 1 and not inbox.get(stem):
            url = holders[0]
            print(f"  index:    {url} -> {new}  (sole holder)")
        else:
            url = None
            unresolved.append(stem)
            print("  index:    cannot tell which URL the archived copy came "
                  "from — left pointing at the inbox item")

        if not args.apply:
            continue

        for path in files:
            path.rename(path.with_name(f"{new}{path.suffix}"))
        archived[new] = [p.with_name(f"{new}{p.suffix}") for p in files]
        if url:
            index[url] = [new if s == stem else s for s in index[url]]

    if args.apply and index_path.is_file():
        write_index(index_path, index)

    print()
    if args.apply:
        print(f"renamed {len(clashes)} archived families.")
    else:
        print(f"{len(clashes)} collisions found. Re-run with --apply to fix "
              "them.")
    if unresolved:
        print(f"{len(unresolved)} of them have no recorded source URL to "
              "follow: " + ", ".join(unresolved))
        print("Their files are renamed either way; only the URL mapping is "
              "lost, and re-saving the page rebuilds it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
