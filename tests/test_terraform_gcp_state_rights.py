"""Droits GCS du runtime : les objets d'état sont remplacés, les lots bruts jamais.

Sur GCS, remplacer un objet existant (écriture conditionnée à sa génération)
exige ``storage.objects.delete``. Checkpoints et garde de sign-on sont des
objets remplacés (``GcsCheckpointStore``, ``GcsSourceGate``) : sans ce droit,
la capture échoue au deuxième checkpoint. Les lots bruts restent en écriture
unique : le droit de remplacement est borné par condition IAM aux préfixes
d'état.
"""
from __future__ import annotations

from pathlib import Path
import re

MAIN = Path(__file__).resolve().parents[1] / "deploy/terraform/gcp/base/main.tf"


def test_state_prefixes_are_replaceable_and_nothing_else() -> None:
    text = MAIN.read_text()
    block = re.search(r'resource "google_storage_bucket_iam_member" "runtime_state_writer" \{(.*?)\n\}', text, re.S)
    assert block, "droit de remplacement des objets d'état absent"
    body = block.group(1)
    assert 'role   = "roles/storage.objectUser"' in body
    assert "condition {" in body
    assert "/objects/checkpoints/" in body and "/objects/source-gates/" in body
    # Les autres liaisons ne donnent jamais la suppression sur tout le bucket.
    unconditioned = re.findall(r'role\s*=\s*"roles/storage\.(objectAdmin|objectUser|admin)"', text)
    assert unconditioned == ["objectUser"], unconditioned
