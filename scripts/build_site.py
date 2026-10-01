"""Assemble la présentation ; les docs pointent vers le dépôt réellement choisi."""
import argparse
from pathlib import Path
import re
import shutil
from urllib.parse import urlparse


def build(repository_url: str, output: Path) -> None:
    parsed = urlparse(repository_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("URL HTTPS du dépôt requise")
    if output.exists():
        raise ValueError("Le dossier de sortie doit être neuf")
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "site", output)
    page = output / "index.html"
    text = page.read_text()
    text = re.sub(r'data-repo-path="([A-Za-z0-9_./-]+)" href="[^"]+"',
                  lambda m: f'href="{repository_url.rstrip("/")}/blob/main/{m[1]}"', text)
    page.write_text(text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.repository_url, args.output)
