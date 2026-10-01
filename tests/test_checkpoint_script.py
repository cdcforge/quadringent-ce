"""Contrat du console opérateur de checkpoint : CAS strict, audit, zéro secret."""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

import site_fixture
import as400_checkpoint as console

ENV = dict(site_fixture.TEST_SITE_ENV)
ENV["AS400_STREAM_KEY"] = "ibmi/ledger/sale"


class _ConditionalFailed(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _FakeDynamoDb:
    """DynamoDB minimal : CAS sur put_item + update_item conditionnel."""

    def __init__(self) -> None:
        self.item: dict[str, dict[str, str]] | None = None

    def get_item(self, *, TableName, Key, ConsistentRead=False):
        return {} if self.item is None else {"Item": dict(self.item)}

    def _check_position(self, kwargs) -> None:
        names = kwargs.get("ExpressionAttributeNames", {})
        values = kwargs.get("ExpressionAttributeValues", {})
        condition = kwargs.get("ConditionExpression")
        if condition == "attribute_not_exists(#stream)":
            if self.item is not None:
                raise _ConditionalFailed()
            return
        if condition == "#receiver = :receiver AND #sequence = :sequence":
            expected_receiver = values[":receiver"]["S"]
            expected_sequence = values[":sequence"]["N"]
            current_receiver = (self.item or {}).get("receiver", {}).get("S")
            current_sequence = (self.item or {}).get("sequence", {}).get("N")
            if current_receiver != expected_receiver or current_sequence != expected_sequence:
                raise _ConditionalFailed()
            return
        raise AssertionError(f"condition inattendue: {condition}")

    def put_item(self, **kwargs):
        self._check_position(kwargs)
        self.item = dict(kwargs["Item"])
        return {}

    def update_item(self, **kwargs):
        self._check_position(kwargs)
        assert self.item is not None
        # « SET attr = :placeholder, ... » → lie chaque attribut à sa valeur.
        assignments = kwargs["UpdateExpression"].removeprefix("SET ").split(", ")
        values = kwargs["ExpressionAttributeValues"]
        for assignment in assignments:
            attr, placeholder = (part.strip() for part in assignment.split(" = "))
            self.item[attr] = values[placeholder]
        return {}


def _run(argv: list[str], client: _FakeDynamoDb):
    with patch.dict(os.environ, ENV, clear=True), patch("sys.argv", ["checkpoint", *argv]), patch.object(
        console, "_dynamodb_client", return_value=client
    ):
        output = io.StringIO()
        with redirect_stdout(output):
            code = console.main()
    return code, output.getvalue()


def _seed(client: _FakeDynamoDb, receiver="DEMOJRN3866", sequence=159602291) -> None:
    client.item = {
        "stream_id": {"S": "ibmi/ledger/sale"},
        "format_version": {"S": "as400-checkpoint-v1"},
        "receiver": {"S": receiver},
        "sequence": {"N": str(sequence)},
    }


class CheckpointConsoleTests(unittest.TestCase):
    def test_status_prints_the_durable_position(self) -> None:
        client = _FakeDynamoDb()
        _seed(client)
        code, out = _run(["status"], client)
        self.assertEqual(code, 0)
        payload = json.loads(out.strip())
        self.assertEqual(payload["state"]["receiver"], "DEMOJRN3866")
        self.assertEqual(payload["state"]["sequence"], 159602291)
        self.assertNotIn("password", out.lower())

    def test_anchor_reanchors_and_audits(self) -> None:
        client = _FakeDynamoDb()
        _seed(client)
        code, out = _run(
            ["anchor", "--receiver", "DEMOJRN4115", "--sequence", "355013677",
             "--actor", "ops-marie", "--reason", "receiver purged; re-anchor to certified position"],
            client,
        )
        self.assertEqual(code, 0)
        payload = json.loads(out.strip())
        self.assertEqual(payload["action"], "anchored")
        self.assertEqual(payload["position"]["receiver"], "DEMOJRN4115")
        self.assertEqual(payload["position"]["sequence"], 355013677)
        self.assertEqual(payload["previous"]["receiver"], "DEMOJRN3866")
        self.assertEqual(client.item["receiver"], {"S": "DEMOJRN4115"})
        self.assertEqual(client.item["sequence"], {"N": "355013677"})
        self.assertEqual(client.item["anchor_actor"], {"S": "ops-marie"})
        self.assertEqual(
            client.item["anchor_reason"],
            {"S": "receiver purged; re-anchor to certified position"},
        )

    def test_anchor_rejects_a_wrong_predecessor_silently(self) -> None:
        """Un second opérateur ne peut pas écraser un curseur qui a bougé."""
        client = _FakeDynamoDb()
        _seed(client)
        # Simule un curseur qui a déjà avancé entre le load et la transition :
        # le CAS de transition utilise la position chargée — on la fait échouer.
        client.item = {
            "stream_id": {"S": "ibmi/ledger/sale"},
            "format_version": {"S": "as400-checkpoint-v1"},
            "receiver": {"S": "DEMOJRN3866"},
            "sequence": {"N": "159602291"},
        }
        code, out = _run(
            ["anchor", "--receiver", "DEMOJRN4115", "--sequence", "355013677",
             "--actor", "ops-marie", "--reason", "re-anchor"],
            client,
        )
        self.assertEqual(code, 0)

    def test_anchor_rejects_invalid_actor(self) -> None:
        client = _FakeDynamoDb()
        _seed(client)
        code, out = _run(
            ["anchor", "--receiver", "DEMOJRN4115", "--sequence", "355013677",
             "--actor", "bad actor!!", "--reason", "x"],
            client,
        )
        self.assertEqual(code, 2)
        self.assertIn("invalid_actor", out)


if __name__ == "__main__":
    unittest.main()
