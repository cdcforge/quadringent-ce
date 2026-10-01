from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

import site_fixture
import as400_source_gate as console


ENV = dict(site_fixture.TEST_SITE_ENV)


class _StubGate:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def state(self):
        return {"state": "paused", "retry_after": "2026-09-21T08:15:00+00:00"}

    def pause_until(self, *, until, actor, now):
        self.calls.append(("pause_until", {"until": until, "actor": actor}))
        return {"state": "paused", "retry_after": until.isoformat()}

    def reset(self, *, actor, now):
        self.calls.append(("reset", {"actor": actor}))
        return {"state": "closed", "reset_by": actor}


class SourceGateConsoleTests(unittest.TestCase):
    def _run(self, argv: list[str], gate: _StubGate):
        output = io.StringIO()
        with patch.dict(os.environ, ENV, clear=True), patch("sys.argv", ["gate", *argv]), patch.object(
            console, "DynamoDbSourceGate", return_value=gate
        ) as gate_class, patch.object(console, "_dynamodb_client", return_value=object()):
            code = console.main()
        return code, output, gate_class

    def test_status_prints_the_durable_record_without_secrets(self) -> None:
        gate = _StubGate()
        with redirect_stdout(io.StringIO()) as output:
            code, _, gate_class = self._run(["status"], gate)
        self.assertEqual(code, 0)
        gate_class.assert_called_once()
        payload = json.loads(output.getvalue().strip())
        self.assertEqual(payload["state"]["state"], "paused")
        self.assertNotIn("password", output.getvalue().lower())

    def test_pause_declares_a_bounded_window(self) -> None:
        gate = _StubGate()
        with redirect_stdout(io.StringIO()) as output:
            code, _, _ = self._run(["pause", "--seconds", "3600"], gate)
        self.assertEqual(code, 0)
        self.assertEqual(gate.calls[0][0], "pause_until")
        self.assertEqual(json.loads(output.getvalue().strip())["action"], "paused")

    def test_pause_out_of_range_is_rejected_without_touching_the_gate(self) -> None:
        gate = _StubGate()
        with redirect_stdout(io.StringIO()):
            code, _, _ = self._run(["pause", "--seconds", "10"], gate)
        self.assertEqual(code, 2)
        self.assertEqual(gate.calls, [])

    def test_reset_requires_a_valid_actor_and_audits_it(self) -> None:
        gate = _StubGate()
        with redirect_stdout(io.StringIO()) as output:
            code, _, _ = self._run(["reset", "--actor", "oncall-paris"], gate)
        self.assertEqual(code, 0)
        self.assertEqual(gate.calls, [("reset", {"actor": "oncall-paris"})])
        self.assertEqual(json.loads(output.getvalue().strip())["state"]["reset_by"], "oncall-paris")

        gate = _StubGate()
        with redirect_stdout(io.StringIO()):
            code, _, _ = self._run(["reset", "--actor", "bad;actor"], gate)
        self.assertEqual(code, 2)
        self.assertEqual(gate.calls, [])


if __name__ == "__main__":
    unittest.main()
