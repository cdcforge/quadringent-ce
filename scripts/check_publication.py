"""Refuse les identités privées sans les reproduire dans les diagnostics."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

SHA256_DIGEST = re.compile(r"[0-9a-f]{64}")
PRIVATE_IP = re.compile(r"(?<![\d.])(?:10\.(?:\d{1,3}\.){2}\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})(?!\d|\.\d)")
LOCAL_HOME = re.compile(r"/Users/[A-Za-z0-9._-]+/")
ACCOUNT = re.compile(r"(?<![A-Za-z0-9])\d{12}(?![A-Za-z0-9])")
LICENSE = re.compile(r"quadringent1[.][A-Za-z0-9_-]{40,}[.][A-Za-z0-9_-]{60,}")
MAX_HISTORY_OBJECT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str


def load_private_hashes(path: Path) -> frozenset[str]:
    """Load site-specific SHA256 digests from a file kept outside the repository."""
    lines = path.read_text(encoding="ascii").splitlines()
    if not lines or any(not SHA256_DIGEST.fullmatch(line) for line in lines):
        raise ValueError("private denylist must contain one SHA256 digest per line")
    return frozenset(lines)


def scan_text(text: str, path: str,
              denied_hashes: frozenset[str] = frozenset()) -> list[Finding]:
    findings = []
    for index, line in enumerate(text.splitlines(), 1):
        for rule, pattern in (("private_ip", PRIVATE_IP), ("commercial_license", LICENSE),
                              ("local_home_path", LOCAL_HOME)):
            if pattern.search(line):
                findings.append(Finding(path, index, rule))
        # Ces comptes synthétiques servent aux tests d'isolation entre sites.
        if any(m.group() not in {"000000000000", "000000000001"} for m in ACCOUNT.finditer(line)):
            findings.append(Finding(path, index, "aws_account"))
        tokens = re.findall(r"[A-Za-z$][A-Za-z0-9_$-]*", line)
        if any(hashlib.sha256(token.encode()).hexdigest() in denied_hashes
               for word in tokens for token in (word, word.lower(), re.sub(r"[0-9]+$", "", word.lower()))):
            findings.append(Finding(path, index, "private_identifier"))
    return findings


def publication_paths(root: Path) -> list[Path]:
    result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                            cwd=root, check=True, capture_output=True)
    return sorted({Path(p) for p in result.stdout.decode().split("\0") if p and (root / p).is_file()})


def scan_tree(root: Path, denied_hashes: frozenset[str] = frozenset()) -> list[Finding]:
    findings = []
    for index, path in enumerate(publication_paths(root), 1):
        label = f"file/{index:06d}"
        findings.extend(scan_text(str(path), label, denied_hashes))
        findings.extend(scan_text((root / path).read_bytes().decode("utf-8", errors="replace"),
                                  label, denied_hashes))
    return findings


def _scan_git_tree_names(content: bytes, oid: str,
                         denied_hashes: frozenset[str]) -> list[Finding]:
    """Read tree entry names, including old names of unchanged blobs."""
    offset = 0
    digest_bytes = len(oid) // 2
    label = f"tree/{oid[:12]}"
    findings: list[Finding] = []
    while offset < len(content):
        mode_end = content.find(b" ", offset)
        name_end = content.find(b"\0", mode_end + 1)
        if mode_end < offset or name_end < 0 or name_end + 1 + digest_bytes > len(content):
            raise RuntimeError("Git tree object is malformed")
        name = content[mode_end + 1:name_end].decode("utf-8", errors="replace")
        findings.extend(scan_text(name, label, denied_hashes))
        offset = name_end + 1 + digest_bytes
    return findings


def scan_history(root: Path, ref: str,
                 denied_hashes: frozenset[str] = frozenset()) -> list[Finding]:
    """Scan every object reachable from one intended public Git revision.

    This is separate from the working-tree gate: a removed fixture remains
    reachable after a normal commit. Findings expose object IDs and rules,
    never source content. A large object that cannot be inspected fails closed.
    """
    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", ref],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()
    listing = subprocess.run(
        ["git", "rev-list", "--objects", resolved],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    objects: list[tuple[str, str]] = []
    for line in listing:
        oid, _, name = line.partition(" ")
        objects.append((oid, name))

    findings: list[Finding] = []
    with subprocess.Popen(
        ["git", "cat-file", "--batch"], cwd=root,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        for oid, name in objects:
            process.stdin.write((oid + "\n").encode("ascii"))
            process.stdin.flush()
            header = process.stdout.readline().decode("ascii").strip().split(" ")
            if len(header) != 3 or header[0] != oid or not header[2].isdigit():
                raise RuntimeError("Git history object cannot be inspected")
            kind, size = header[1], int(header[2])
            label = f"{kind}/{oid[:12]}"
            if name:
                findings.extend(scan_text(name, label, denied_hashes))
            if size > MAX_HISTORY_OBJECT_BYTES:
                findings.append(Finding(label, 0, "oversized_git_object_unscanned"))
                remaining = size
                while remaining:
                    chunk = process.stdout.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError("Git history object is truncated")
                    remaining -= len(chunk)
            else:
                content = process.stdout.read(size)
                if len(content) != size:
                    raise RuntimeError("Git history object is truncated")
                if kind in {"blob", "commit", "tag"}:
                    findings.extend(scan_text(content.decode("utf-8", errors="replace"),
                                              label, denied_hashes))
                elif kind == "tree":
                    findings.extend(_scan_git_tree_names(content, oid, denied_hashes))
            if process.stdout.read(1) != b"\n":
                raise RuntimeError("Git history object framing is invalid")
        process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError("Git history scan failed")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-ref", help="scan all Git objects reachable from this public revision")
    parser.add_argument("--private-denylist-file", type=Path,
                        help="external file of site-specific SHA256 digests for private prepublication review")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    denied_hashes = frozenset()
    if args.private_denylist_file:
        if args.private_denylist_file.resolve().is_relative_to(root):
            parser.error("private denylist must live outside the source tree")
        try:
            denied_hashes = load_private_hashes(args.private_denylist_file)
        except (OSError, UnicodeError, ValueError) as exc:
            parser.error(f"private denylist cannot be loaded: {type(exc).__name__}")
    findings = scan_tree(root, denied_hashes)
    if args.history_ref:
        findings.extend(scan_history(root, args.history_ref, denied_hashes))
    print(json.dumps({"files": len(publication_paths(root)), "findings": len(findings),
                      "details": [asdict(finding) for finding in findings]}, indent=2))
    return int(bool(findings))


if __name__ == "__main__":
    sys.exit(main())
