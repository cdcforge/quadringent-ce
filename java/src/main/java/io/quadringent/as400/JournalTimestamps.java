package io.quadringent.as400;

import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.Instant;
import java.time.DayOfWeek;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.Month;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.time.temporal.TemporalAdjusters;
import java.util.List;

/**
 * Journal timestamps are source-local, not UTC. Debezium's RJNE0200 decoder
 * (3.6.1.Final) decodes DTS with AS400Timestamp's default GMT calendar. Its
 * Instant therefore contains local clock fields incorrectly labelled UTC.
 * Keep this adapter at the journal boundary, never on business date columns.
 */
public final class JournalTimestamps {
    private static final String IBM_QP0100CET = "IBM:QP0100CET";
    private static final ZoneOffset CET = ZoneOffset.ofHours(1);
    private static final ZoneOffset CEST = ZoneOffset.ofHours(2);

    private final String sourceName;
    private final ZoneId sourceZone;

    private JournalTimestamps(String sourceName, ZoneId sourceZone) {
        this.sourceName = sourceName;
        this.sourceZone = sourceZone;
    }

    public static JournalTimestamps fromZoneName(String name) {
        if (name == null || name.isBlank()) {
            throw new IllegalArgumentException("AS400_SOURCE_TIME_ZONE is required; no assumed UTC");
        }
        // IBM's QP0100CET ends DST on the last Sunday of September. Modern
        // Europe/Paris/Zurich end it in October, despite IBM's alternate-name
        // column. Keep this one IBM rule explicit instead of silently using
        // the wrong IANA offset for a month of journal entries.
        if (IBM_QP0100CET.equals(name)) {
            return new JournalTimestamps(name, null);
        }
        return new JournalTimestamps(name, ZoneId.of(name));
    }

    public String fromHeader(Instant decodedLocalClock) {
        return resolve(LocalDateTime.ofInstant(decodedLocalClock, ZoneOffset.UTC)).toString();
    }

    public String fromSql(String sourceLocalTimestamp) {
        return resolve(parseLocal(sourceLocalTimestamp)).toString();
    }

    private static LocalDateTime parseLocal(String value) {
        return LocalDateTime.parse(value.trim().replace(' ', 'T'));
    }

    private Instant resolve(LocalDateTime local) {
        List<ZoneOffset> offsets = sourceZone == null
                ? qp0100CetOffsets(local)
                : sourceZone.getRules().getValidOffsets(local);
        if (offsets.size() != 1) {
            // A journal-local timestamp alone cannot distinguish the repeated
            // autumn hour. Do not invent an offset or advance the checkpoint.
            throw new IllegalStateException("ambiguous or nonexistent journal timestamp in "
                    + sourceName + "; checkpoint must not advance");
        }
        return local.toInstant(offsets.get(0));
    }

    private static LocalDate lastSunday(int year, Month month) {
        return LocalDate.of(year, month, 1).with(TemporalAdjusters.lastInMonth(DayOfWeek.SUNDAY));
    }

    private static List<ZoneOffset> qp0100CetOffsets(LocalDateTime local) {
        int year = local.getYear();
        LocalDateTime start = lastSunday(year, Month.MARCH).atTime(2, 0);
        LocalDateTime end = lastSunday(year, Month.SEPTEMBER).atTime(2, 0);
        if (local.isBefore(start) || !local.isBefore(end)) return List.of(CET);
        if (local.isBefore(start.plusHours(1))) return List.of();
        if (!local.isBefore(end.minusHours(1))) return List.of(CEST, CET);
        return List.of(CEST);
    }

    private static ZoneOffset qp0100CetOffsetAt(Instant utc) {
        int year = utc.atOffset(ZoneOffset.UTC).getYear();
        Instant start = lastSunday(year, Month.MARCH).atTime(2, 0).toInstant(CET);
        Instant end = lastSunday(year, Month.SEPTEMBER).atTime(2, 0).toInstant(CEST);
        return !utc.isBefore(start) && utc.isBefore(end) ? CEST : CET;
    }

    public void verifySourceClock(Connection jdbc) throws SQLException {
        Instant before = Instant.now();
        try (Statement statement = jdbc.createStatement()) {
            statement.setQueryTimeout(8);
            try (ResultSet rows = statement.executeQuery(
                    "SELECT CURRENT_TIMESTAMP, CURRENT_TIMEZONE FROM SYSIBM.SYSDUMMY1")) {
                if (!rows.next()) throw new IllegalStateException("source clock is unavailable");
                String local = rows.getString(1);
                int offset = rows.getInt(2);
                if (local == null || rows.wasNull()) throw new IllegalStateException("source clock is null");
                verifyClock(local, offset, before, Instant.now());
            }
        }
    }

    void verifyClock(String sourceLocal, int offsetHhmmss, Instant before, Instant after) {
        int absolute = Math.abs(offsetHhmmss);
        int hours = absolute / 10000;
        int minutes = absolute / 100 % 100;
        int seconds = absolute % 100;
        if (hours > 18 || minutes > 59 || seconds > 59) {
            throw new IllegalStateException("source UTC offset is invalid");
        }
        int sourceOffset = Integer.signum(offsetHhmmss) * (hours * 3600 + minutes * 60 + seconds);
        LocalDateTime local = parseLocal(sourceLocal);
        Instant utc = resolve(local);
        ZoneOffset declaredOffset = sourceZone == null
                ? qp0100CetOffsetAt(utc)
                : sourceZone.getRules().getOffset(utc);
        if (declaredOffset.getTotalSeconds() != sourceOffset) {
            throw new SourceClockMismatchException("AS400_SOURCE_TIME_ZONE does not match source UTC offset");
        }
        if (utc.isBefore(before.minusSeconds(30)) || utc.isAfter(after.plusSeconds(30))) {
            throw new IllegalStateException("source clock differs from capture clock by more than 30 seconds");
        }
    }
}
