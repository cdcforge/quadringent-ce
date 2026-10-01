from __future__ import annotations

from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path

from quadringent.source_gate import (
    ConnectFailureClass,
    DynamoDbSourceGate,
    FileSourceGate,
    SourceAuthenticationBlockedError,
    SourceGatePolicy,
    SourceUnavailablePausedError,
)

T0 = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)


class ConditionalFailed(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeDynamoDb:
    """DynamoDB minimal : item par clé + ConditionExpression revision CAS."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def get_item(self, TableName, Key, ConsistentRead=False):
        item = self.items.get(Key["stream_id"]["S"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, TableName, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        key = Item["stream_id"]["S"]
        existing = self.items.get(key)
        if ConditionExpression and "revision" in ConditionExpression:
            expected = int(ExpressionAttributeValues[":expected"]["N"])
            current = int(existing["revision"]["N"]) if existing else None
            if current is not None and current != expected:
                raise ConditionalFailed()
            if current is None and expected != 0:
                raise ConditionalFailed()
        self.items[key] = dict(Item)
        return {}


def _file_gate(tmp: str, **policy) -> FileSourceGate:
    return FileSourceGate(Path(tmp) / "gate.json", policy=SourceGatePolicy(**policy))


def _ddb_gate(client: FakeDynamoDb, **policy) -> DynamoDbSourceGate:
    return DynamoDbSourceGate(
        "checkpoints", "source-gate#ibmi#USER", policy=SourceGatePolicy(**policy), client=client
    )


class SourceGateContract:
    """Le même scénario sur les deux implémentations : la loi ne dépend pas du store."""

    def make_gate(self, tmp: str, **policy):
        raise NotImplementedError

    def new_gate_on_same_store(self, tmp: str, **policy):
        raise NotImplementedError

    def test_three_grants_then_persisted_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.make_gate(tmp)
            for _ in range(3):
                gate.before_connect(now=T0)
                gate.record_connect_failure(ConnectFailureClass.UNAVAILABLE, now=T0)
            with self.assertRaises(SourceUnavailablePausedError) as caught:
                gate.before_connect(now=T0)
            self.assertEqual(caught.exception.reason_code, "RETRY_BUDGET_EXHAUSTED")
            record = gate.state()
            self.assertEqual(record["state"], "paused")
            self.assertEqual(record["attempts_used"], 3)
            # La pause est durable : un nouveau lecteur (pod redémarré) la voit.
            restarted = self.new_gate_on_same_store(tmp)
            with self.assertRaises(SourceUnavailablePausedError):
                restarted.before_connect(now=T0 + timedelta(seconds=1))

    def test_authentication_failure_blocks_immediately_and_forever(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.make_gate(tmp)
            gate.before_connect(now=T0)
            gate.record_connect_failure(ConnectFailureClass.AUTHENTICATION, now=T0)
            for step in (1, 3600, 86400):
                with self.assertRaises(SourceAuthenticationBlockedError):
                    gate.before_connect(now=T0 + timedelta(seconds=step))
            restarted = self.new_gate_on_same_store(tmp)
            with self.assertRaises(SourceAuthenticationBlockedError):
                restarted.before_connect(now=T0 + timedelta(days=30))
            self.assertEqual(gate.state()["reason_code"], "AUTHENTICATION_BLOCKED")

    def test_expired_pause_grants_one_probe_then_repause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.make_gate(tmp, pause_seconds=600)
            for _ in range(3):
                gate.before_connect(now=T0)
                gate.record_connect_failure(ConnectFailureClass.UNAVAILABLE, now=T0)
            with self.assertRaises(SourceUnavailablePausedError) as caught:
                gate.before_connect(now=T0)
            first_retry = caught.exception.retry_after
            self.assertEqual(first_retry, T0 + timedelta(seconds=600))
            # Pendant la pause : aucun sign-on.
            with self.assertRaises(SourceUnavailablePausedError):
                gate.before_connect(now=T0 + timedelta(seconds=599))
            # Après l'échéance : une sonde unique.
            gate.before_connect(now=first_retry)
            gate.record_connect_failure(ConnectFailureClass.UNAVAILABLE, now=first_retry)
            record = gate.state()
            self.assertEqual(record["state"], "paused")
            self.assertEqual(record["attempts_used"], 4)
            # Recul borné : la pause double (600 → 1200).
            self.assertEqual(
                record["retry_after"],
                (first_retry + timedelta(seconds=1200)).isoformat(timespec="milliseconds"),
            )

    def test_success_closes_the_gate_and_resets_the_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.make_gate(tmp)
            for _ in range(2):
                gate.before_connect(now=T0)
                gate.record_connect_failure(ConnectFailureClass.UNAVAILABLE, now=T0)
            gate.before_connect(now=T0)
            gate.record_connect_success(now=T0)
            record = gate.state()
            self.assertEqual(record["state"], "closed")
            self.assertEqual(record["attempts_used"], 0)
            # Le budget repart à zéro après un sign-on réussi.
            gate.before_connect(now=T0)

    def test_a_fresh_lease_blocks_a_second_pod(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = self.make_gate(tmp)
            first.before_connect(now=T0)
            second = self.new_gate_on_same_store(tmp)
            with self.assertRaises(SourceUnavailablePausedError) as caught:
                second.before_connect(now=T0 + timedelta(seconds=1))
            self.assertEqual(caught.exception.reason_code, "CONNECT_IN_FLIGHT")
            # Bail expiré : le second pod reprend la main sans double comptage.
            second.before_connect(now=T0 + timedelta(seconds=121))
            self.assertEqual(second.state()["attempts_used"], 2)

    def test_operator_pause_needs_no_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.make_gate(tmp)
            until = T0 + timedelta(hours=8)
            record = gate.pause_until(until=until, actor="oncall", now=T0)
            self.assertEqual(record["state"], "paused")
            self.assertEqual(record["reason_code"], "OPERATOR_PAUSE")
            with self.assertRaises(SourceUnavailablePausedError):
                gate.before_connect(now=T0 + timedelta(hours=1))
            gate.before_connect(now=until)

    def test_reset_rearms_blocked_gate_with_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.make_gate(tmp)
            gate.before_connect(now=T0)
            gate.record_connect_failure(ConnectFailureClass.AUTHENTICATION, now=T0)
            record = gate.reset(actor="oncall-marie", now=T0 + timedelta(hours=2))
            self.assertEqual(record["state"], "closed")
            self.assertEqual(record["attempts_used"], 0)
            self.assertEqual(record["reset_by"], "oncall-marie")
            gate.before_connect(now=T0 + timedelta(hours=2))


class FileSourceGateTests(SourceGateContract, unittest.TestCase):
    def make_gate(self, tmp: str, **policy) -> FileSourceGate:
        return _file_gate(tmp, **policy)

    def new_gate_on_same_store(self, tmp: str, **policy) -> FileSourceGate:
        return _file_gate(tmp, **policy)


class DynamoDbSourceGateTests(SourceGateContract, unittest.TestCase):
    _clients: dict[str, FakeDynamoDb]

    def make_gate(self, tmp: str, **policy) -> DynamoDbSourceGate:
        self._clients = getattr(self, "_clients", {})
        self._clients[tmp] = FakeDynamoDb()
        return _ddb_gate(self._clients[tmp], **policy)

    def new_gate_on_same_store(self, tmp: str, **policy) -> DynamoDbSourceGate:
        return _ddb_gate(self._clients[tmp], **policy)


class SourceGatePolicyTests(unittest.TestCase):
    def test_attempts_never_exceed_three(self) -> None:
        with self.assertRaises(ValueError):
            SourceGatePolicy(max_attempts=4)
        SourceGatePolicy(max_attempts=3)

    def test_pause_backoff_is_bounded(self) -> None:
        policy = SourceGatePolicy(pause_seconds=900, pause_max_seconds=3600)
        self.assertEqual(policy.pause_delay(attempts_used=3), timedelta(seconds=900))
        self.assertEqual(policy.pause_delay(attempts_used=4), timedelta(seconds=1800))
        self.assertEqual(policy.pause_delay(attempts_used=5), timedelta(seconds=3600))
        self.assertEqual(policy.pause_delay(attempts_used=20), timedelta(seconds=3600))

    def test_gate_key_scopes_by_host_and_user(self) -> None:
        self.assertEqual(
            DynamoDbSourceGate.key_for("192.0.2.10", "CDCUSER"),
            "source-gate#192.0.2.10#CDCUSER",
        )
        with self.assertRaises(ValueError):
            DynamoDbSourceGate.key_for("", "USER")


if __name__ == "__main__":
    unittest.main()
