from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .contract import JournalPosition


@dataclass(frozen=True)
class JsonCheckpointStore:
    """Durable single-receiver checkpoint used by the local POC.

    The production implementation can replace the file with a transactional
    DynamoDB/S3 or Kafka Connect offset store. The ordering rule remains the
    same: the caller commits here only after the raw batch is durable.
    """

    path: Path

    def load(self) -> JournalPosition | None:
        if not self.path.exists():
            return None
        record = json.loads(self.path.read_text(encoding="utf-8"))
        return JournalPosition(
            receiver=str(record["receiver"]),
            sequence=int(record["sequence"]),
        )

    def commit(self, position: JournalPosition) -> None:
        previous = self.load()
        if previous is not None:
            if position.receiver != previous.receiver:
                raise ValueError("receiver rotation requires explicit ordering")
            if position < previous:
                raise ValueError("checkpoint moved backwards")
        self.compare_and_set(previous, position)

    def transition(self, previous: JournalPosition, position: JournalPosition) -> None:
        """Commit a receiver change only when its exact predecessor is known."""

        current = self.load()
        if current != previous:
            raise ValueError("checkpoint transition predecessor does not match")
        if position.receiver == previous.receiver:
            raise ValueError("receiver transition must change receiver")
        self.compare_and_set(previous, position)

    def compare_and_set(self, previous: JournalPosition | None, position: JournalPosition) -> None:
        """Serialize local checkpoint writers and preserve the exact predecessor."""
        import fcntl

        if previous is not None and previous.receiver == position.receiver and position.sequence < previous.sequence:
            raise ValueError('checkpoint moved backwards')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_name(self.path.name + '.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if self.load() != previous:
                    raise RuntimeError('checkpoint compare-and-set conflict')
                self._write(position)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write(self, position: JournalPosition) -> None:
        payload = json.dumps(
            {
                "format_version": "as400-checkpoint-v1",
                "receiver": position.receiver,
                "sequence": position.sequence,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            dir=self.path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with open(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(self.path)
            directory_descriptor = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()


class DynamoDbCheckpointStore:
    """Transactional single-stream checkpoint backed by one DynamoDB item.

    The item is updated with compare-and-set on the exact previously observed
    receiver and sequence. A concurrent worker therefore cannot overwrite a
    newer offset, and receiver rotation remains an explicit application-level
    decision. The AWS client is lazy so the offline POC does not require
    boto3.
    """

    FORMAT_VERSION = "as400-checkpoint-v1"

    def __init__(self, table_name: str, stream_key: str, *, client: Any | None = None) -> None:
        if not table_name.strip() or table_name.startswith("-"):
            raise ValueError("invalid DynamoDB table name")
        if not stream_key.strip():
            raise ValueError("checkpoint stream key must not be empty")
        if client is None:
            import boto3

            client = boto3.client("dynamodb")
        self.table_name = table_name
        self.stream_key = stream_key
        self.client = client

    def load(self) -> JournalPosition | None:
        response = self.client.get_item(
            TableName=self.table_name,
            Key={"stream_id": {"S": self.stream_key}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        try:
            receiver = str(item["receiver"]["S"])
            sequence = int(item["sequence"]["N"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid DynamoDB checkpoint item") from error
        return JournalPosition(receiver=receiver, sequence=sequence)

    def commit(self, position: JournalPosition) -> None:
        previous = self.load()
        if previous is not None:
            if position.receiver != previous.receiver:
                raise ValueError("receiver rotation requires explicit ordering")
            if position < previous:
                raise ValueError("checkpoint moved backwards")
        self._put(position, expected=previous)

    def transition(self, previous: JournalPosition, position: JournalPosition) -> None:
        """CAS a receiver change against the exact predecessor position."""

        current = self.load()
        if current != previous:
            raise ValueError("checkpoint transition predecessor does not match")
        if position.receiver == previous.receiver:
            raise ValueError("receiver transition must change receiver")
        self._put(position, expected=previous)

    def compare_and_set(self, previous: JournalPosition | None, position: JournalPosition) -> None:
        """Do not reload and silently replace the caller's expected predecessor."""
        if previous is not None and previous.receiver == position.receiver and position.sequence < previous.sequence:
            raise ValueError('checkpoint moved backwards')
        self._put(position, expected=previous)

    def _put(self, position: JournalPosition, *, expected: JournalPosition | None) -> None:

        item = {
            "stream_id": {"S": self.stream_key},
            "format_version": {"S": self.FORMAT_VERSION},
            "receiver": {"S": position.receiver},
            "sequence": {"N": str(position.sequence)},
        }
        if expected is None:
            condition_expression = "attribute_not_exists(#stream)"
            expression_names = {"#stream": "stream_id"}
            expression_values: dict[str, dict[str, str]] = {}
        else:
            condition_expression = "#receiver = :receiver AND #sequence = :sequence"
            expression_names = {
                "#receiver": "receiver",
                "#sequence": "sequence",
            }
            expression_values = {
                ":receiver": {"S": expected.receiver},
                ":sequence": {"N": str(expected.sequence)},
            }

        put_arguments: dict[str, Any] = {
            "TableName": self.table_name,
            "Item": item,
            "ConditionExpression": condition_expression,
            "ExpressionAttributeNames": expression_names,
        }
        if expression_values:
            put_arguments["ExpressionAttributeValues"] = expression_values
        try:
            self.client.put_item(**put_arguments)
        except Exception as error:
            if _is_conditional_failure(error):
                raise RuntimeError("checkpoint compare-and-set conflict") from error
            raise


def _is_conditional_failure(error: Exception) -> bool:
    code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
    return code == "ConditionalCheckFailedException"
