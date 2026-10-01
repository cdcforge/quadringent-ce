from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .contract import JournalPosition


@dataclass(frozen=True)
class JournaledObject:
    journal_library: str
    journal_name: str
    object_library: str
    object_name: str
    object_type: str
    journal_images: str | None


@dataclass(frozen=True)
class JournalInfo:
    journal_library: str
    journal_name: str
    state: str | None
    receiver_count: int | None
    receiver_total_size: int | None
    remote_journal_count: int | None


@dataclass(frozen=True)
class JournalReceiver:
    journal_receiver_library: str
    journal_receiver_name: str
    status: str | None
    attach_timestamp: Any
    first_sequence_number: int | None
    last_sequence_number: int | None
    entry_count: int | None


@dataclass(frozen=True)
class JournalReceiverChain:
    """Explicit, contiguous receiver order supplied by IBM i metadata.

    Receiver names are opaque. The caller must provide the order returned by
    an approved metadata snapshot; this class only verifies contiguous sequence
    boundaries and permits a transition after the current receiver has been
    drained to its declared last sequence.
    """

    receivers: tuple[JournalReceiver, ...]

    def __post_init__(self) -> None:
        if not self.receivers:
            raise ValueError("receiver chain must not be empty")
        identities = [
            (receiver.journal_receiver_library, receiver.journal_receiver_name)
            for receiver in self.receivers
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("receiver chain contains a duplicate receiver")
        for previous, following in zip(self.receivers, self.receivers[1:]):
            if previous.last_sequence_number is None or following.first_sequence_number is None:
                raise ValueError("receiver chain requires sequence bounds")
            if following.first_sequence_number != previous.last_sequence_number + 1:
                raise ValueError("receiver chain contains a sequence gap")

    def next_position(self, current: JournalPosition) -> JournalPosition | None:
        """Return the next receiver start only after the current one is drained."""

        current_index = next(
            (
                index
                for index, receiver in enumerate(self.receivers)
                if receiver.journal_receiver_name == current.receiver
            ),
            None,
        )
        if current_index is None:
            raise ValueError(f"receiver is not in the explicit chain: {current.receiver}")
        current_receiver = self.receivers[current_index]
        if current_receiver.last_sequence_number is None:
            raise ValueError("current receiver has no declared last sequence")
        if current.sequence != current_receiver.last_sequence_number:
            raise ValueError("cannot rotate before the current receiver is drained")
        if current_index == len(self.receivers) - 1:
            return None
        following = self.receivers[current_index + 1]
        if following.first_sequence_number is None:
            raise ValueError("next receiver has no declared first sequence")
        return JournalPosition(following.journal_receiver_name, following.first_sequence_number)


class IbmiJournalReader:
    """Small, read-only adapter around IBM i journal SQL services.

    The adapter deliberately keeps the IBM i connection outside the module:
    callers create it in their approved runtime (for example with pyodbc in
    the Popsink LAN pod), while tests use a fake DB-API connection. It only
    reads metadata and bounded journal windows. It never creates a data journal
    reader, changes journaling, or mutates an AS400 table.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def discover_object(self, object_library: str, object_name: str) -> JournaledObject:
        rows = self._fetch_all(
            """
            SELECT *
              FROM QSYS2.JOURNALED_OBJECTS
             WHERE OBJECT_LIBRARY = {object_library}
               AND OBJECT_NAME = {object_name}
             FETCH FIRST 2 ROWS ONLY
            """.format(
                object_library=_sql_literal(object_library),
                object_name=_sql_literal(object_name),
            )
        )
        if not rows:
            raise LookupError(f"journaled object not found: {object_library}.{object_name}")
        if len(rows) > 1:
            raise ValueError(f"journaled object is not unique: {object_library}.{object_name}")
        row = rows[0]
        return JournaledObject(
            journal_library=_required_text(row, "JOURNAL_LIBRARY"),
            journal_name=_required_text(row, "JOURNAL_NAME"),
            object_library=_required_text(row, "OBJECT_LIBRARY"),
            object_name=_required_text(row, "OBJECT_NAME"),
            object_type=_required_text(row, "OBJECT_TYPE"),
            journal_images=_optional_text(row, "JOURNAL_IMAGES"),
        )

    def journal_info(self, journal_library: str, journal_name: str) -> JournalInfo:
        rows = self._fetch_all(
            """
            SELECT *
              FROM QSYS2.JOURNAL_INFO
             WHERE JOURNAL_LIBRARY = {journal_library}
               AND JOURNAL_NAME = {journal_name}
             FETCH FIRST 2 ROWS ONLY
            """.format(
                journal_library=_sql_literal(journal_library),
                journal_name=_sql_literal(journal_name),
            )
        )
        if not rows:
            raise LookupError(f"journal not found: {journal_library}/{journal_name}")
        if len(rows) > 1:
            raise ValueError(f"journal is not unique: {journal_library}/{journal_name}")
        row = rows[0]
        return JournalInfo(
            journal_library=_required_text(row, "JOURNAL_LIBRARY"),
            journal_name=_required_text(row, "JOURNAL_NAME"),
            state=_optional_text(row, "JOURNAL_STATE", "STATE"),
            receiver_count=_optional_int(
                row,
                "RECEIVER_COUNT",
                "NUMBER_OF_RECEIVERS",
                "NUMBER_JOURNAL_RECEIVERS",
                "RECEIVERS",
            ),
            receiver_total_size=_optional_int(
                row,
                "RECEIVER_TOTAL_SIZE",
                "TOTAL_RECEIVER_SIZE",
                "TOTAL_SIZE_JOURNAL_RECEIVERS",
                "JOURNAL_SIZE",
            ),
            remote_journal_count=_optional_int(
                row,
                "REMOTE_JOURNAL_COUNT",
                "NUMBER_OF_REMOTE_JOURNALS",
                "NUMBER_REMOTE_JOURNALS",
            ),
        )

    def latest_receivers(
        self,
        journal_library: str,
        journal_name: str,
        *,
        limit: int = 10,
    ) -> list[JournalReceiver]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        rows = self._fetch_all(
            """
            SELECT *
              FROM QSYS2.JOURNAL_RECEIVER_INFO
             WHERE JOURNAL_LIBRARY = {journal_library}
               AND JOURNAL_NAME = {journal_name}
             ORDER BY ATTACH_TIMESTAMP DESC
             FETCH FIRST {limit} ROWS ONLY
            """.format(
                journal_library=_sql_literal(journal_library),
                journal_name=_sql_literal(journal_name),
                limit=limit,
            )
        )
        return [
            JournalReceiver(
                journal_receiver_library=_required_text(
                    row,
                    "JOURNAL_RECEIVER_LIBRARY",
                    "RECEIVER_LIBRARY",
                ),
                journal_receiver_name=_required_text(
                    row,
                    "JOURNAL_RECEIVER_NAME",
                    "RECEIVER_NAME",
                ),
                status=_optional_text(row, "STATUS"),
                attach_timestamp=_value(row, "ATTACH_TIMESTAMP", "ATTACHED"),
                first_sequence_number=_optional_int(
                    row,
                    "FIRST_SEQUENCE_NUMBER",
                    "FIRST_SEQ",
                ),
                last_sequence_number=_optional_int(
                    row,
                    "LAST_SEQUENCE_NUMBER",
                    "LAST_SEQ",
                ),
                entry_count=_optional_int(
                    row,
                    "ENTRY_COUNT",
                    "NUMBER_OF_JOURNAL_ENTRIES",
                    "ENTRIES",
                ),
            )
            for row in rows
        ]

    def read_entries(
        self,
        journal_library: str,
        journal_name: str,
        *,
        receiver_library: str,
        starting: JournalPosition,
        ending: JournalPosition | None = None,
        object_library: str,
        object_name: str,
        journal_codes: str = "R",
        max_rows: int = 1000,
        include_entry_data: bool = True,
    ) -> list[dict[str, Any]]:
        """Read a strictly bounded, object-filtered journal window.

        Receiver rotation is not implicit: ``ending`` must use the same
        receiver as ``starting``. A later production reader must obtain an
        explicit receiver chain from IBM i before crossing that boundary.
        """

        if ending is None:
            ending = starting
        if ending.receiver != starting.receiver:
            raise ValueError("journal window cannot cross receivers implicitly")
        if ending.sequence < starting.sequence:
            raise ValueError("journal window end must not precede its start")
        if not 1 <= max_rows <= 10000:
            raise ValueError("max_rows must be between 1 and 10000")
        if not journal_codes or any(code not in "CDFPR" for code in journal_codes):
            raise ValueError("journal_codes must contain only IBM i journal codes")

        selected_columns = [
            "ENTRY_TIMESTAMP",
            "SEQUENCE_NUMBER",
            "JOURNAL_CODE",
            "JOURNAL_ENTRY_TYPE",
            "COUNT_OR_RRN",
            "NULL_VALUE_INDICATORS",
            "OBJECT",
            "OBJECT_TYPE",
            "INDICATOR_FLAG",
            "RECEIVER_NAME",
            "RECEIVER_LIBRARY",
        ]
        if include_entry_data:
            # Fetching the BLOB directly through IBM i Access ODBC can make
            # the driver attempt a huge SQLGetData allocation even for a
            # small record. HEX keeps the same bytes while returning a
            # character value that pyodbc can bind predictably.
            selected_columns.append("HEX(ENTRY_DATA) AS ENTRY_DATA_HEX")
        query = """
            SELECT {selected_columns}
              FROM TABLE(QSYS2.DISPLAY_JOURNAL(
                    JOURNAL_LIBRARY => {journal_library},
                    JOURNAL_NAME => {journal_name},
                    STARTING_RECEIVER_LIBRARY => {receiver_library},
                    STARTING_RECEIVER_NAME => {receiver_name},
                    STARTING_SEQUENCE => {starting_sequence},
                    ENDING_RECEIVER_LIBRARY => {receiver_library},
                    ENDING_RECEIVER_NAME => {receiver_name},
                    ENDING_SEQUENCE => {ending_sequence},
                    JOURNAL_CODES => {journal_codes},
                    OBJECT_LIBRARY => {object_library},
                    OBJECT_NAME => {object_name},
                    OBJECT_OBJTYPE => '*FILE',
                    OBJECT_MEMBER => '*ALL'
              ))
             FETCH FIRST {max_rows} ROWS ONLY
        """.format(
            journal_library=_sql_literal(journal_library),
            journal_name=_sql_literal(journal_name),
            receiver_library=_sql_literal(receiver_library),
            receiver_name=_sql_literal(starting.receiver),
            starting_sequence=starting.sequence,
            ending_sequence=ending.sequence,
            journal_codes=_sql_literal(journal_codes),
            object_library=_sql_literal(object_library),
            object_name=_sql_literal(object_name),
            max_rows=max_rows,
            selected_columns=", ".join(selected_columns),
        )
        return self._fetch_all(query)

    def _fetch_all(self, query: str) -> list[dict[str, Any]]:
        cursor = self._connection.cursor()
        try:
            cursor.execute(query)
            description = getattr(cursor, "description", None) or []
            columns = [str(item[0]).upper() for item in description]
            rows = cursor.fetchall()
            return [_row_mapping(columns, row) for row in rows]
        finally:
            close = getattr(cursor, "close", None)
            if close is not None:
                close()


def _sql_literal(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("SQL literal must be a non-empty string")
    return "'" + value.replace("'", "''") + "'"


def _row_mapping(columns: list[str], row: Iterable[Any]) -> dict[str, Any]:
    values = list(row)
    if len(columns) != len(values):
        raise ValueError("DB-API cursor description does not match row width")
    return dict(zip(columns, values))


def _value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def _required_text(row: Mapping[str, Any], *names: str) -> str:
    value = _value(row, *names)
    if value is None or not str(value).strip():
        raise ValueError(f"required IBM i column missing: {'/'.join(names)}")
    return str(value).strip()


def _optional_text(row: Mapping[str, Any], *names: str) -> str | None:
    value = _value(row, *names)
    if value is None:
        return None
    return str(value).strip()


def _optional_int(row: Mapping[str, Any], *names: str) -> int | None:
    value = _value(row, *names)
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value)
    return int(value)
