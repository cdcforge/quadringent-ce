#!/usr/bin/env python3
"""Conserve les avis des distributions réellement installées dans une image."""

from __future__ import annotations

import argparse
from importlib.metadata import distributions
import json
from pathlib import Path
import re
from urllib.parse import quote

_NOTICE = re.compile(r"^(LICENSE|LICENCE|COPYING|NOTICE|AUTHORS)(?:[._-]|$)", re.I)
_LICENSE = re.compile(r"^(LICENSE|LICENCE|COPYING)(?:[._-]|$)", re.I)


def collect(installed):
    manifest, sections = [], []
    for dist in sorted(installed, key=lambda item: item.metadata["Name"].lower()):
        name, version = dist.metadata["Name"], dist.version
        texts, has_license = [], False
        for file in sorted(dist.files or [], key=str):
            path = Path(str(file))
            if not _NOTICE.match(path.name) or path.suffix in {".py", ".pyc"}:
                continue
            text = Path(dist.locate_file(file)).read_text(encoding="utf-8").strip()
            if not text:
                continue
            has_license |= bool(_LICENSE.match(path.name))
            texts.append((str(file), text))
        if not has_license:
            raise ValueError(f"Licence absente : {name} {version}")
        source = f"https://pypi.org/project/{quote(name, safe='')}/{quote(version, safe='')}/#files"
        manifest.append({
            "name": name, "version": version,
            "license": dist.metadata.get("License-Expression") or dist.metadata.get("License"),
            "source_distributions": source,
            "notice_files": [file for file, _ in texts],
        })
        sections.append(f"{name} {version}\nSources : {source}")
        sections.extend(f"--- {file} ---\n\n{text}" for file, text in texts)
    return manifest, "\n\n".join(sections) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest, notices = collect(distributions())
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "distributions.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    (args.output / "third-party-notices.txt").write_text(notices)
    print(f"Avis Python : {len(manifest)} distributions")


if __name__ == "__main__":
    main()
