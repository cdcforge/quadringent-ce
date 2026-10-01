#!/usr/bin/env python3
"""Retire uniquement la description documentaire de PyJWT dans l'image runtime."""

from __future__ import annotations

import base64
import csv
from email import policy
from email.parser import BytesParser
import hashlib
from importlib.metadata import PackageNotFoundError, distribution
import io
from pathlib import Path
import re


def strip_description(dist):
    """Conserve les octets des en-têtes et actualise leur empreinte dans RECORD."""
    files = dist.files or []
    candidates = [file for file in files if file.name == "METADATA" and file.parent.name.endswith(".dist-info")]
    if len(candidates) != 1:
        raise ValueError("PyJWT : METADATA absent ou ambigu dans RECORD")
    relative = candidates[0]
    metadata_path = Path(dist.locate_file(relative))
    original = metadata_path.read_bytes()
    separator = re.search(rb"\r?\n\r?\n", original)
    if separator is None:
        raise ValueError("PyJWT : séparateur entre en-têtes et description absent")
    headers = original[:separator.end()]
    parsed = BytesParser(policy=policy.default).parsebytes(headers)
    if parsed.defects or any(parsed[name].defects for name in parsed.keys()):
        raise ValueError("PyJWT : en-têtes Core Metadata invalides")
    for name in ("Metadata-Version", "Name", "Version"):
        values = parsed.get_all(name, [])
        if len(values) != 1 or not str(values[0]).strip():
            raise ValueError(f"PyJWT : en-tête {name} absent ou ambigu")
    if str(parsed["Name"]).lower() != "pyjwt":
        raise ValueError("PyJWT : nom de distribution inattendu")
    record_path = metadata_path.with_name("RECORD")
    if not record_path.is_file():
        raise ValueError("PyJWT : RECORD absent")
    rows = list(csv.reader(io.StringIO(record_path.read_text(encoding="utf-8"), newline="")))
    if any(len(row) != 3 for row in rows):
        raise ValueError("PyJWT : format RECORD invalide")
    selected = [row for row in rows if row[0] == str(relative)]
    if len(selected) != 1:
        raise ValueError("PyJWT : entrée METADATA absente ou ambiguë dans RECORD")
    digest = base64.urlsafe_b64encode(hashlib.sha256(headers).digest()).decode("ascii").rstrip("=")
    selected[0][1:] = [f"sha256={digest}", str(len(headers))]
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    metadata_path.write_bytes(headers)
    record_path.write_text(output.getvalue(), encoding="utf-8", newline="")


def main():
    try:
        dist = distribution("PyJWT")
    except PackageNotFoundError as exc:
        raise ValueError("PyJWT : distribution requise absente") from exc
    strip_description(dist)
    print(f"PyJWT {dist.version} : description documentaire retirée, en-têtes conservés")


if __name__ == "__main__":
    main()
