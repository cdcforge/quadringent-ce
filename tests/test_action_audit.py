"""Une action laisse une intention durable et son effet constaté."""
import json
from pathlib import Path

import pytest

from quadringent_control_plane.audit import ActionAuditLog


def test_journalise_l_auteur_l_action_et_le_recu_sans_corps_brut(tmp_path: Path):
    log = ActionAuditLog(tmp_path / "actions.jsonl")
    request = log.begin("resume", "example", "operator@example.invalid")
    log.finish(request, 200, {"id": "receipt-1", "state": "succeeded", "stages": {"observed_effect": {"state": "succeeded"}}, "secret": "never-log"})
    records = [json.loads(line) for line in log.path.read_text().splitlines() if "action_" in line]
    assert records[0]["actor"] == "operator@example.invalid"
    assert records[0]["action"] == "resume"
    assert records[1]["request_id"] == records[0]["request_id"]
    assert records[1]["receipt_id"] == "receipt-1"
    assert records[1]["observed_effect"] == "succeeded"
    assert "never-log" not in log.path.read_text()
    assert log.path.stat().st_mode & 0o777 == 0o600


def test_un_journal_indisponible_refuse_l_intention(tmp_path: Path):
    log = ActionAuditLog(tmp_path / "actions.jsonl")
    log.path.unlink()
    log.path.mkdir()
    with pytest.raises(OSError):
        log.begin("resume", "example", "local-port-forward")
    assert not log.healthy


def test_un_lien_symbolique_ne_peut_pas_detourner_le_journal(tmp_path: Path):
    target = tmp_path / "target"
    target.write_text("préserver")
    path = tmp_path / "audit"
    path.symlink_to(target)
    with pytest.raises(OSError):
        ActionAuditLog(path)
    assert target.read_text() == "préserver"
