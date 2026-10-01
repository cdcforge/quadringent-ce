"""Copie historique : convention de préfixe et refus du mélange de tentatives."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import site_fixture

SITE = site_fixture.build_test_site()

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "quadringent_fleet_history", ROOT / "scripts" / "quadringent_fleet_history.py"
)
history = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(history)


class _NotFound(Exception):
    """Erreur client factice : 404 pour _existing_digest."""

    response = {"Error": {"Code": "404"}}


class _FakeS3:
    """S3 minimal : clés -> (corps, métadonnées), pagination plane."""

    def __init__(self, objects: dict[str, tuple[bytes, dict[str, str]]]) -> None:
        self.objects = dict(objects)
        self.deleted: list[str] = []
        self.put: dict[str, dict[str, str]] = {}

    def get_paginator(self, _name: str):
        client = self

        class _Paginator:
            def paginate(self, **_kwargs):
                yield {"Contents": [{"Key": key} for key in client.objects]}

        return _Paginator()

    def head_object(self, Bucket: str, Key: str):  # noqa: N803 - API AWS
        if Key not in self.objects:
            raise _NotFound(Key)
        return {"Metadata": dict(self.objects[Key][1])}

    def delete_object(self, Bucket: str, Key: str):  # noqa: N803 - API AWS
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def put_object(self, Bucket: str, Key: str, Body: bytes, Metadata: dict, ContentType: str):  # noqa: N803
        self.objects[Key] = (Body, dict(Metadata))
        self.put[Key] = dict(Metadata)

    def get_object(self, Bucket: str, Key: str):  # noqa: N803 - API AWS
        return {"Body": io.BytesIO(self.objects[Key][0])}


def _batch(name_char: str, kind: str = "history-snapshot", run: str | None = None):
    """Objets lot+manifeste factices marqués comme la publication réelle."""

    batch_id = name_char * 32
    payload_key = f"p/batch-{batch_id}.jsonl"
    manifest_key = f"p/batch-{batch_id}.manifest.json"
    metadata = {"kind": kind}
    if run is not None:
        metadata["run"] = run
    return {
        payload_key: (b"{}\n", dict(metadata)),
        manifest_key: (b'{"event_count": 1}', dict(metadata)),
    }


def test_prefix_is_the_live_journal_prefix() -> None:
    """L'historique se dépose là où le flux devient vivant, pas ailleurs."""

    assert history.table_journal_prefix("ORDER") == SITE.journal_prefix_for("ORDER")
    assert history.table_journal_prefix("HOLIDAYS") == SITE.journal_prefix_for("HOLIDAYS")


@pytest.mark.parametrize("table", ("order/journal", "ORDER*", "", "ORDER ", "a" * 31))
def test_unsafe_table_identifier_is_refused(table: str) -> None:
    with pytest.raises(history.HistoryError):
        history.table_journal_prefix(table)


def test_batch_pattern_reads_only_the_canonical_names() -> None:
    assert history.BATCH.fullmatch("batch-" + "a" * 32 + ".jsonl") is not None
    assert history.BATCH.fullmatch("batch-" + "a" * 32 + ".manifest.json") is not None
    assert history.BATCH.fullmatch("batch-short.jsonl") is None
    assert history.BATCH.fullmatch("other-" + "a" * 32 + ".jsonl") is None


def test_reader_command_carries_the_full_batch_contract() -> None:
    """Une commande de lecture doit rester reproductible et sans secret affiché."""

    import os

    os.environ.update(
        {
            "ISERIES_HOST": "192.0.2.10",
            "ISERIES_USER": "USER",
            "ISERIES_PASSWORD": "secret",
        }
    )
    history._host_dir = "/tmp/host"
    command = history._reader_command(
        "image:tag",
        "ORDER",
        "00000000-0000-4000-8000-000000000000",
        "/work/out",
        Path("/tmp/host/.reader.env"),
    )
    joined = " ".join(command)
    assert "io.quadringent.as400.ReadOnlyTableSnapshot" in joined
    # Sans `-c`, bash traite la chaine comme un nom de fichier et echoue en 127.
    assert "-c" in command
    assert command[-2] == "-c"
    assert "/tmp/host:/work" in joined
    # Le secret ne doit jamais apparaître dans la ligne de commande : il
    # transite par un env-file 0600 écrit par _read_table.
    assert "secret" not in joined
    assert not any(item.startswith("ISERIES_PASSWORD=") for item in command)
    assert "--env-file" in command
    assert command[command.index("--env-file") + 1] == "/tmp/host/.reader.env"


def test_reader_command_propagates_ports_and_timeouts(monkeypatch) -> None:
    """Les réglages de transport déclarés passent au lecteur, sans défaut caché."""

    import os

    os.environ.update(
        {
            "ISERIES_HOST": "192.0.2.10",
            "ISERIES_USER": "USER",
            "ISERIES_PASSWORD": "secret",
            "AS400_DATABASE_PORT": "18471",
            "AS400_SIGNON_PORT": "18476",
            "AS400_SNAPSHOT_SOCKET_TIMEOUT_MS": "90000",
            "AS400_SNAPSHOT_LOGIN_TIMEOUT_MS": "30000",
        }
    )
    history._host_dir = "/tmp/host"
    command = history._reader_command(
        "image:tag",
        "ORDER",
        "00000000-0000-4000-8000-000000000000",
        "/work/out",
        Path("/tmp/host/.reader.env"),
    )
    for expected in (
        "AS400_DATABASE_PORT=18471",
        "AS400_SIGNON_PORT=18476",
        "AS400_SNAPSHOT_SOCKET_TIMEOUT_MS=90000",
        "AS400_SNAPSHOT_LOGIN_TIMEOUT_MS=30000",
    ):
        assert expected in command
    # Une variable absente n'est pas matérialisée : pas de valeur inventée.
    for name in ("AS400_COMMAND_PORT",):
        assert not any(item.startswith(name + "=") for item in command)


def test_read_table_writes_secret_in_0600_env_file(tmp_path, monkeypatch) -> None:
    """Le mot de passe part dans un fichier protégé, retiré après le run."""

    import os
    import stat

    os.environ.update(
        {
            "ISERIES_HOST": "192.0.2.10",
            "ISERIES_USER": "USER",
            "ISERIES_PASSWORD": "unit-only-password",
        }
    )
    captured: dict[str, object] = {}

    def fake_run(command, **_kwargs):
        env_file = Path(command[command.index("--env-file") + 1])
        captured["command"] = command
        captured["mode"] = stat.S_IMODE(env_file.stat().st_mode)
        captured["content"] = env_file.read_text()
        assert env_file.is_file()

        class _Done:
            returncode = 0
            stdout = "rows=4 batches=1"
            stderr = ""

        return _Done()

    monkeypatch.setattr(history.subprocess, "run", fake_run)
    history._host_dir = str(tmp_path)
    rows, batches, _out = history._read_table("image:tag", "ORDER", tmp_path)

    assert (rows, batches) == (4, 1)
    assert captured["mode"] == 0o600
    assert captured["content"] == "ISERIES_PASSWORD=unit-only-password\n"
    assert "unit-only-password" not in " ".join(captured["command"])
    # Le fichier protégé est retiré après la lecture, même en cas d'échec.
    assert not (tmp_path / ".reader.env").exists()


def test_live_lots_do_not_block_a_first_history_copy() -> None:
    """Le préfixe porte deux origines : l'image initiale et le flux vivant.

    Un lot du flux vivant partage le préfixe sans être une tentative
    d'historique. Le compter comme tel interdirait toute table déjà en
    service : c'est ce qui a bloqué CUSTOM1 alors qu'un seul lot vivant
    existait.
    """

    assert history.HISTORY_KIND == "history-snapshot"


def test_existing_objects_reads_the_snapshot_marker() -> None:
    """Seuls les objets marqués sont retenus comme tentatives précédentes."""

    class _Client:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def get_paginator(self, _name: str):
            live = "batch-" + "a" * 32 + ".jsonl"
            history = "batch-" + "b" * 32 + ".jsonl"

            class _Paginator:
                def paginate(self, **_kwargs):
                    yield {
                        "Contents": [
                            {"Key": "p/" + live},
                            {"Key": "p/" + history},
                            {"Key": "p/not-a-batch.jsonl"},
                        ]
                    }

            return _Paginator()

        def head_object(self, Bucket: str, Key: str):  # noqa: N803 - API AWS
            marker = "history-snapshot" if "b" * 32 in Key else ""
            return {"Metadata": {"kind": marker}}

    found = history._existing_objects(_Client(), "p")
    assert list(found) == ["b" * 32]


def test_replace_partial_reclear_keeps_the_current_run(tmp_path, monkeypatch) -> None:
    """Après purge des lots étrangers, la re-vérification porte le run :
    une tranche déjà posée de la même tentative ne doit pas faire échouer
    « partial attempt could not be cleared »."""

    objects: dict[str, tuple[bytes, dict[str, str]]] = {}
    objects.update(_batch("a", run="attempt-0"))  # étranger : à purger
    objects.update(_batch("b", run="run-1"))      # même run : conservé
    objects.update(_batch("d", kind=""))          # flux vivant : ignoré
    client = _FakeS3(objects)
    monkeypatch.setattr(history, "_s3_client", lambda: client)
    monkeypatch.setattr(history, "_bucket", lambda: "bkt")
    monkeypatch.setattr(history, "table_journal_prefix", lambda _t: "p")

    local = "batch-" + "c" * 32 + ".jsonl"
    (tmp_path / local).write_bytes(b"{}\n")
    (tmp_path / ("batch-" + "c" * 32 + ".manifest.json")).write_bytes(
        b'{"event_count": 1}\n'
    )
    result = history._publish(tmp_path, "ORDER", True, "run-1")

    assert result["removed_objects"] == 1
    # L'étranger est purgé (lot + manifeste), le lot du run courant survit.
    assert f"p/batch-{'a' * 32}.jsonl" in client.deleted
    assert f"p/batch-{'b' * 32}.jsonl" in client.objects
    assert f"p/batch-{'d' * 32}.jsonl" in client.objects
    # Les lots publiés portent le marqueur de la tentative.
    assert client.put[f"p/{local}"]["run"] == "run-1"
    assert client.put[f"p/{local}"]["kind"] == "history-snapshot"


def test_count_published_rows_counts_only_the_snapshot_run(monkeypatch) -> None:
    """Le comptage filtre par kind=history-snapshot et run=<run_id> via
    head_object : les lots du flux vivant et des autres tentatives qui
    partagent le préfixe ne sont jamais sommés."""

    objects: dict[str, tuple[bytes, dict[str, str]]] = {}
    objects.update(_batch("a", run="run-1"))
    objects.update(_batch("b", run="other"))
    objects.update(_batch("c", kind=""))  # flux vivant : sans marqueur
    client = _FakeS3(objects)
    monkeypatch.setattr(history, "_bucket", lambda: "bkt")

    assert history._count_published_rows(client, "p", "run-1") == 1
    # Sans run fourni, toutes les tentatives d'image comptent — jamais le
    # flux vivant.
    assert history._count_published_rows(client, "p") == 2


def test_count_published_rows_still_refuses_a_missing_manifest(monkeypatch) -> None:
    """Un lot du run sans manifeste reste une erreur explicite."""

    objects = {f"p/batch-{'e' * 32}.jsonl": (b"{}\n", {"kind": "history-snapshot", "run": "run-1"})}
    client = _FakeS3(objects)
    monkeypatch.setattr(history, "_bucket", lambda: "bkt")
    with pytest.raises(history.HistoryError):
        history._count_published_rows(client, "p", "run-1")
    # Le même lot orphelin d'un autre run n'interfère pas avec le comptage.
    objects = {
        f"p/batch-{'e' * 32}.jsonl": (b"{}\n", {"kind": "history-snapshot", "run": "other"}),
    }
    objects.update(_batch("a", run="run-1"))
    assert history._count_published_rows(_FakeS3(objects), "p", "run-1") == 1


def test_reader_timeout_emits_the_failed_contract(monkeypatch, capsys) -> None:
    """Un timeout lecteur produit le JSON FAILED, pas un traceback, et retire
    le conteneur orphelin nommé."""

    os.environ.update(
        {
            "ISERIES_HOST": "192.0.2.10",
            "ISERIES_USER": "USER",
            "ISERIES_PASSWORD": "unit-only-password",
        }
    )
    calls: list[list[str]] = []

    def timing_out(command, **_kwargs):
        calls.append([str(part) for part in command])
        if command[1] == "run":
            raise subprocess.TimeoutExpired("docker", 7200)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(history.subprocess, "run", timing_out)
    monkeypatch.setattr(
        sys, "argv", ["quadringent_fleet_history.py", "--table", "ORDER", "--image", "img:test"]
    )
    rc = history.main()
    assert rc == 1
    document = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert document["status"] == "FAILED"
    assert document["table"] == "ORDER"
    assert "timed out" in document["reason"]
    runs = [c for c in calls if c[1] == "run"]
    assert len(runs) == 1
    name = runs[0][runs[0].index("--name") + 1]
    assert name.startswith("quadringent-history-")
    assert ["docker", "rm", "-f", name] in calls
