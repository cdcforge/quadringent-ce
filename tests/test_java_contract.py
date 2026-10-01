from __future__ import annotations

from pathlib import Path
import unittest


_ROOT = Path(__file__).resolve().parents[1]
_JAVA = (
    _ROOT
    / "java"
    / "src"
    / "main"
    / "java"
    / "io" / "quadringent"
    / "as400"
)
_READER = _JAVA / "JournalSession.java"
_ONESHOT = _JAVA / "ReadOnlyJournalDecode.java"
_WORKER = _JAVA / "PersistentJournalWorker.java"
_WRITER = _JAVA / "RawCaptureWriter.java"


class JavaJournalContractTests(unittest.TestCase):
    def test_both_journal_paths_normalize_source_clock_before_raw(self) -> None:
        session = _READER.read_text(encoding="utf-8")
        writer = _WRITER.read_text(encoding="utf-8")
        self.assertIn('required("AS400_SOURCE_TIME_ZONE")', session)
        self.assertIn('settings.timestamps().verifySourceClock(jdbc)', session)
        self.assertIn('settings.timestamps().fromSql(rows.getString("ENTRY_TIMESTAMP"))', session)
        self.assertIn('settings.timestamps().fromHeader(header.getTime())', session)
        self.assertNotIn('header.getTime().toString()', writer)

    def test_before_and_after_images_keep_explicit_technical_roles(self) -> None:
        source = _WRITER.read_text(encoding="utf-8")

        self.assertIn('case AFTER_IMAGE -> "u_after"', source)
        self.assertIn('case BEFORE_IMAGE -> "u_before"', source)

    def test_rollback_row_entries_fail_closed_before_checkpoint(self) -> None:
        source = _READER.read_text(encoding="utf-8")

        self.assertIn("ROLLBACK_AFTER_IMAGE", source)
        self.assertIn("ROLLBACK_BEFORE_IMAGE", source)
        self.assertIn("ROLLBACK_DELETE_ROW", source)
        self.assertIn("unsupported rollback journal row entry", source)
        self.assertIn("checkpoint must not advance", source)

    def test_java_writer_rejects_out_of_order_journal_events(self) -> None:
        source = _WRITER.read_text(encoding="utf-8")

        self.assertIn("previousSequence", source)
        self.assertIn("raw batch events must be ordered", source)

    def test_retrieve_connect_sends_ibm_i_sale_file_filter(self) -> None:
        source = _READER.read_text(encoding="utf-8")
        connect = source.split("public static JournalSession connect", 1)[1]
        connect = connect.split("public void emitReceiverCatalog", 1)[0]

        self.assertIn("withServerFiltering(true)", connect)
        self.assertNotIn("withServerFiltering(false)", connect)
        self.assertIn("withIncludeFiles", connect)
        self.assertIn("includeFiles(settings.schema(), settings.tables())", connect)
        self.assertIn("withIncludeFiles(includeFiles)", connect)
        self.assertIn("verifyCapturedJournal", connect)
        self.assertIn("server filter returned an unexpected object", source)
        self.assertIn('optionalValue("ISERIES_TABLES")', source)
        self.assertIn("parseTableList", source)

    def test_multi_table_session_is_fail_closed_and_keeps_mono_table_decode(self) -> None:
        source = _READER.read_text(encoding="utf-8")
        process_window = source.split("public void processWindow", 1)[1]
        process_window = process_window.split("public void close", 1)[0]
        process_sql = source.split("public void processSqlWindow", 1)[1]
        process_sql = process_sql.split("public void processWindow", 1)[0]

        self.assertIn("requireKnownTable(settings.schema(), settings.tables()", process_window)
        self.assertIn("decoder.getRecordFormat(header.getFile(), header.getLibrary())", process_window)
        self.assertIn("journal table structure is missing; checkpoint must not advance", process_window)
        self.assertNotIn("if (tableInfo.isEmpty()) {\n                    continue;", process_window)
        self.assertIn("refuseSqlMultiTable(settings.tables())", process_sql)
        self.assertLess(
            process_sql.index("refuseSqlMultiTable(settings.tables())"),
            process_sql.index("jdbc.createStatement()"),
        )
        self.assertIn("DISPLAY_JOURNAL refuses multi-table capture", source)
        self.assertIn("is not journaled to the verified journal", source)
        self.assertIn("new FileFilter(schema, table)", source)
        self.assertIn("MAX_CAPTURE_TABLES = 32", source)

    def test_java_reader_cannot_advance_an_empty_window_without_exhaustion(self) -> None:
        source = _READER.read_text(encoding="utf-8")

        self.assertIn("scan_complete=true", source)
        self.assertIn("window_progress", source)
        self.assertIn("setSocketTimeout", source)
        self.assertIn("maxDecodedEntries", source)
        self.assertIn("bounded journal window exceeded max decoded entries", source)
        self.assertIn("futureDataAvailable", source)
        self.assertIn("journal buffer too small", source)
        self.assertIn("16_000_000", source)
        self.assertIn("withJournalBufferSize", source)
        self.assertIn("lastSeenSequence", source)
        self.assertIn("ISERIES_MAX_SERVER_ENTRIES", source)
        self.assertIn("withMaxServerSideEntries(settings.maxServerSideEntries())", source)
        self.assertNotIn(
            "seen >= boundedRange && seen >= window.maxDecodedEntries()",
            source,
        )
        self.assertNotIn("while (state.hasData() && retrieve.nextEntry()\n                    && seen <", source)
        self.assertIn("writeBatch(rawEvents, watermark)", source)
        write_index = source.index("writeBatch(rawEvents, watermark)")
        summary_index = source.index("scan_complete=true", write_index)
        self.assertLess(write_index, summary_index)

    def test_persistent_worker_keeps_one_jvm_and_speaks_stdin_json(self) -> None:
        worker = _WORKER.read_text(encoding="utf-8")
        session = _READER.read_text(encoding="utf-8")
        oneshot = _ONESHOT.read_text(encoding="utf-8")

        self.assertIn("worker_ready", worker)
        self.assertIn("window_done", worker)
        self.assertIn("window_error=", worker)
        self.assertIn("System.in", worker)
        self.assertIn("fromJson", worker)
        self.assertIn("catalog_done", worker)
        self.assertIn("emitReceiverCatalog", worker)
        self.assertIn("RetrieveJournal", session)
        self.assertIn("cancelJob", session)
        self.assertIn("bounded journal retrieve timed out", session)
        self.assertIn("AS400_RETRIEVE_TIMEOUT_MS", session)
        self.assertIn("retrieve_start", session)
        self.assertIn("processSqlWindow", session)
        self.assertIn("QSYS2.DISPLAY_JOURNAL", session)
        self.assertIn("setQueryTimeout", session)
        self.assertIn("bounded DISPLAY_JOURNAL timed out", session)
        self.assertIn("sql_window", worker)
        self.assertIn("sql_event", session)
        self.assertIn("HEX(ENTRY_DATA)", session)
        self.assertIn("eventFromSql", _WRITER.read_text(encoding="utf-8"))
        self.assertIn("decodeSqlImage", session)
        self.assertIn("PersistentJournalWorker", oneshot)

    def test_snapshot_reader_writes_creates_without_printing_payloads(self) -> None:
        source = (
            _ROOT
            / "java"
            / "src"
            / "main"
            / "java"
            / "io" / "quadringent"
            / "as400"
            / "ReadOnlyTableSnapshot.java"
        ).read_text(encoding="utf-8")

        # Le snapshot porte la position physique : c'est l'identite de
        # correspondance avec les entrees de journal *AFTER (delete sans image).
        self.assertIn("SELECT T.*, RRN(T) AS", source)
        self.assertIn("_rrn", source)
        self.assertIn('"c"', source)
        self.assertIn("SNAPSHOT_ROW", source)
        self.assertIn("snapshot_summary", source)
        self.assertIn("AS400_RAW_DIRECTORY", source)
        self.assertNotIn("System.out.println(row", source)
        self.assertNotIn("System.out.print(value", source)


if __name__ == "__main__":
    unittest.main()
