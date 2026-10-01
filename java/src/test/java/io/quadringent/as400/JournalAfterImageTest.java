package io.quadringent.as400;

import java.math.BigInteger;
import java.nio.file.Files;
import java.time.Instant;
import java.util.List;
import java.util.Map;

import com.ibm.as400.access.AS400DataType;
import com.ibm.as400.access.AS400Structure;
import com.ibm.as400.access.AS400Text;

import io.debezium.ibmi.db2.journal.retrieve.JournalEntryType;
import io.debezium.ibmi.db2.journal.retrieve.JournalInfo;
import io.debezium.ibmi.db2.journal.retrieve.SchemaCacheIF.Structure;
import io.debezium.ibmi.db2.journal.retrieve.SchemaCacheIF.TableInfo;
import io.debezium.ibmi.db2.journal.retrieve.rjne0200.EntryHeader;

/**
 * Contrat IMAGES(*AFTER) : un delete n'a pas d'image ligne, seulement la
 * position physique (RRN) portee par l'en-tete du journal. L'evenement brut
 * doit la conserver au lieu de planter, sans jamais confondre une entree
 * marquee incomplete avec un delete legitime.
 */
public final class JournalAfterImageTest {
    public static void main(String[] args) throws Exception {
        checkUnsignedLongRead();
        checkIncompleteFlagRead();
        checkSqlRrn();
        checkHexToBytesAllowMissing();
        checkSqlNullIndicators();
        checkFileLevelEntry();
        checkRrnOnlyDelete();
        checkImageCarriesRrn();
        checkReservedCollision();
        System.out.println("journal_after_image_tests=PASS checks=34");
    }

    private static void checkUnsignedLongRead() {
        byte[] data = new byte[300];
        data[56 + 0] = 0;
        data[56 + 6] = 0x01;
        data[56 + 7] = 0x02;
        equal(0x0102L, JournalSession.RrnAwareFileDecoder.readUnsignedLong(data, 56));
        equal(-1L, JournalSession.RrnAwareFileDecoder.readUnsignedLong(data, 295));
        equal(-1L, JournalSession.RrnAwareFileDecoder.readUnsignedLong(data, -1));
        equal(0L, JournalSession.RrnAwareFileDecoder.readUnsignedLong(data, 100));
    }

    private static void checkIncompleteFlagRead() {
        byte[] data = new byte[300];
        if (JournalSession.RrnAwareFileDecoder.readFlag(data, 218, 0x20)) {
            throw new AssertionError("flag must not be set on a zero byte");
        }
        data[218] = 0x20;
        if (!JournalSession.RrnAwareFileDecoder.readFlag(data, 218, 0x20)) {
            throw new AssertionError("incomplete-data flag not detected");
        }
        data[218] = 0x10;
        if (JournalSession.RrnAwareFileDecoder.readFlag(data, 218, 0x20)) {
            throw new AssertionError("wrong flag bit detected");
        }
        if (JournalSession.RrnAwareFileDecoder.readFlag(data, 500, 0x20)) {
            throw new AssertionError("out of bounds flag read must be false");
        }
    }

    private static void checkSqlRrn() {
        equal(42L, JournalSession.sqlRrn("42"));
        equal(7L, JournalSession.sqlRrn(" 7 "));
        equal(-1L, JournalSession.sqlRrn(null));
        equal(-1L, JournalSession.sqlRrn(""));
        equal(-1L, JournalSession.sqlRrn("-"));
        equal(-1L, JournalSession.sqlRrn("notanumber"));
        // Valeur hors long, construite sans ressembler à un identifiant de site.
        equal(-1L, JournalSession.sqlRrn("9".repeat(24)));
    }

    private static void checkHexToBytesAllowMissing() {
        equal(0, JournalSession.hexToBytes(null, true).length);
        equal(0, JournalSession.hexToBytes("", true).length);
        equal(0, JournalSession.hexToBytes("  ", true).length);
        equal(0, JournalSession.hexToBytes("-", true).length);
        equal(2, JournalSession.hexToBytes("0A0B", true).length);
        rejectsState(() -> JournalSession.hexToBytes(null, false));
        rejectsState(() -> JournalSession.hexToBytes("", false));
        rejectsState(() -> JournalSession.hexToBytes("0A0", true));
        rejectsState(() -> JournalSession.hexToBytes("ZZ", true));
    }

    private static void checkSqlNullIndicators() {
        Object[] values = {"alpha", "", 42};
        Object[] corrected = JournalSession.applySqlNullIndicators(values, "010");
        equal("alpha", corrected[0]);
        if (corrected[1] != null) {
            throw new AssertionError("null indicator must clear the decoded field");
        }
        equal(42, corrected[2]);
        equal("", values[1]); // the original decoder result is not mutated
        equal("", JournalSession.applySqlNullIndicators(values, "000   ")[1]);
        equal("", JournalSession.applySqlNullIndicators(values, "   ")[1]);
        rejectsState(() -> JournalSession.applySqlNullIndicators(values, "01"));
        rejectsState(() -> JournalSession.applySqlNullIndicators(values, "019"));
    }

    private static void checkFileLevelEntry() {
        if (!JournalSession.isFileLevelEntry(JournalEntryType.FILE_CREATED)
                || !JournalSession.isFileLevelEntry(JournalEntryType.FILE_CHANGE)) {
            throw new AssertionError("file-level entries must be detected");
        }
        if (JournalSession.isFileLevelEntry(JournalEntryType.DELETE_ROW)
                || JournalSession.isFileLevelEntry(JournalEntryType.START_COMMIT)
                || JournalSession.isFileLevelEntry(JournalEntryType.END_COMMIT)
                || JournalSession.isFileLevelEntry(null)) {
            throw new AssertionError("row or commit entries are not file-level");
        }
        if (!JournalSession.isRoutineNoiseEntry(JournalEntryType.OPEN)
                || !JournalSession.isRoutineNoiseEntry(JournalEntryType.CLOSE)
                || !JournalSession.isRoutineNoiseEntry(JournalEntryType.START_COMMIT)
                || !JournalSession.isRoutineNoiseEntry(JournalEntryType.END_COMMIT)) {
            throw new AssertionError("open/close/commit entries must stay routine noise");
        }
        if (JournalSession.isRoutineNoiseEntry(JournalEntryType.FILE_CREATED)
                || JournalSession.isRoutineNoiseEntry(JournalEntryType.FILE_CHANGE)
                || JournalSession.isRoutineNoiseEntry(JournalEntryType.DELETE_ROW)
                || JournalSession.isRoutineNoiseEntry(null)) {
            throw new AssertionError("file-level and unrecognized entries must not be routine noise");
        }
    }

    private static void checkRrnOnlyDelete() throws Exception {
        RawCaptureWriter writer = new RawCaptureWriter(Files.createTempDirectory("after-delete"));
        RawCaptureWriter.RawEvent delete = writer.event(
                "ibmi",
                journal(),
                header("PLACE01", "DL"),
                JournalEntryType.DELETE_ROW,
                tableInfo(),
                new Object[0],
                "DEMOJRN4110",
                "JRNLIB",
                "2026-09-19T00:00:00Z",
                7L);
        equal("d", delete.operation());
        equal(Map.of("_rrn", 7L), delete.before());
        if (delete.after() != null) {
            throw new AssertionError("a delete must not carry an after image");
        }
        equal("DELETE_ROW", delete.journalEntryType());
    }

    private static void checkImageCarriesRrn() throws Exception {
        RawCaptureWriter writer = new RawCaptureWriter(Files.createTempDirectory("after-update"));
        RawCaptureWriter.RawEvent update = writer.event(
                "ibmi",
                journal(),
                header("PLACE01", "UP"),
                JournalEntryType.AFTER_IMAGE,
                tableInfo(),
                new Object[] {"abc"},
                "DEMOJRN4110",
                "JRNLIB",
                "2026-09-19T00:00:00Z",
                9L);
        equal("u_after", update.operation());
        Map<String, Object> after = update.after();
        if (after == null || !"abc".equals(after.get("COL1")) || !Long.valueOf(9L).equals(after.get("_rrn"))) {
            throw new AssertionError("after image must carry the row and the RRN: " + after);
        }
        RawCaptureWriter.RawEvent insert = writer.event(
                "ibmi",
                journal(),
                header("CNTR", "PT"),
                JournalEntryType.ADD_ROW1,
                tableInfo(),
                new Object[] {"zz"},
                "DEMOJRN4110",
                "JRNLIB",
                "2026-09-19T00:00:01Z",
                11L);
        if (!Long.valueOf(11L).equals(insert.after().get("_rrn"))) {
            throw new AssertionError("insert image must carry the RRN");
        }
    }

    private static void checkReservedCollision() throws Exception {
        RawCaptureWriter writer = new RawCaptureWriter(Files.createTempDirectory("after-collision"));
        TableInfo colliding = new TableInfo(
                List.of(new Structure("_rrn", "CHAR", 1, 10, 0, false, 1, false)),
                List.of(),
                new AS400Structure(new AS400DataType[] {new AS400Text(10)}));
        try {
            writer.event(
                    "ibmi",
                    journal(),
                    header("CNTR", "UP"),
                    JournalEntryType.AFTER_IMAGE,
                    colliding,
                    new Object[] {"abc"},
                    "DEMOJRN4110",
                    "JRNLIB",
                    "2026-09-19T00:00:00Z",
                    5L);
            throw new AssertionError("a real _rrn column must collide fail-closed");
        }
        catch (IllegalArgumentException expected) {
            if (!expected.getMessage().contains("_rrn")) {
                throw new AssertionError("collision must name the reserved field", expected);
            }
        }
    }

    private static EntryHeader header(String table, String entryType) {
        String objectName = String.format("%-10.10s%-10.10s%-10.10s", table, "SALES", "*FILE");
        return new EntryHeader(
                0,
                0,
                16L,
                BigInteger.valueOf(42),
                BigInteger.valueOf(42),
                Instant.parse("2026-09-19T00:00:00Z"),
                'R',
                entryType,
                objectName,
                BigInteger.ONE,
                200,
                0L,
                "DEMOJRN4110",
                "JRNLIB");
    }

    private static JournalInfo journal() {
        return new JournalInfo("DEMOJRN", "JRNLIB", true);
    }

    private static TableInfo tableInfo() {
        return new TableInfo(
                List.of(new Structure("COL1", "CHAR", 1, 10, 0, false, 1, false)),
                List.of(),
                new AS400Structure(new AS400DataType[] {new AS400Text(10)}));
    }

    private static void equal(long a, long b) {
        if (a != b) {
            throw new AssertionError(a + " != " + b);
        }
    }

    private static void equal(Object a, Object b) {
        if (!a.equals(b)) {
            throw new AssertionError(a + " != " + b);
        }
    }

    private static void rejectsState(Runnable action) {
        try {
            action.run();
        }
        catch (IllegalStateException expected) {
            return;
        }
        throw new AssertionError("expected rejection");
    }
}
