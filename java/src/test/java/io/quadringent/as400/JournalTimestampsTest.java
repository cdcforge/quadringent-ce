package io.quadringent.as400;

import java.time.Instant;
import java.util.TimeZone;

/** Executable tests without a test-framework dependency or source connection. */
public final class JournalTimestampsTest {
    private static int checks;

    public static void main(String[] args) {
        JournalTimestamps zurich = JournalTimestamps.fromZoneName("Europe/Zurich");
        equal("2026-09-07T20:11:42.504176Z", zurich.fromHeader(
                Instant.parse("2026-09-07T22:11:42.504176Z")));
        equal("2026-01-07T21:11:42.504176Z", zurich.fromHeader(
                Instant.parse("2026-01-07T22:11:42.504176Z")));
        equal("2026-09-07T20:11:42.504176Z", zurich.fromSql("2026-09-07 22:11:42.504176"));
        equal("2026-09-07T20:11:42.504176Z", JournalTimestamps.fromZoneName("UTC")
                .fromHeader(Instant.parse("2026-09-07T20:11:42.504176Z")));
        equal("2026-09-07T20:11:42.504176Z", JournalTimestamps.fromZoneName("Asia/Kolkata")
                .fromSql("2026-09-08 01:41:42.504176"));
        rejects(() -> zurich.fromSql("2026-03-29 02:30:00"));
        rejects(() -> zurich.fromSql("2026-10-25 02:30:00"));
        rejects(() -> JournalTimestamps.fromZoneName("Not/AZone"));
        rejects(() -> JournalTimestamps.fromZoneName(""));
        rejects(() -> JournalTimestamps.fromZoneName(null));
        rejects(() -> zurich.fromSql("not a timestamp"));
        rejects(() -> zurich.fromSql("2026-09-07T22:11:42Z"));
        Instant before = Instant.parse("2026-09-07T20:24:03Z");
        Instant after = Instant.parse("2026-09-07T20:24:05Z");
        zurich.verifyClock("2026-09-07 22:24:04", 20000, before, after);
        checks++;
        rejectsWithClockMismatch(() -> zurich.verifyClock("2026-09-07 22:24:04", 10000, before, after));
        rejects(() -> zurich.verifyClock("2026-09-07 23:24:04", 20000, before, after));
        rejects(() -> zurich.verifyClock("2026-09-07 21:24:04", 20000, before, after));
        rejects(() -> zurich.verifyClock("2026-09-07 22:24:04", 26000, before, after));
        JournalTimestamps.fromZoneName("America/New_York").verifyClock(
                "2026-09-07 16:24:04", -40000, before, after);
        checks++;
        // QP0100CET switches back on the last Sunday of September, one month
        // before modern Europe/Paris. IBM i reported +01:00 on 2026-09-28.
        JournalTimestamps ibmCet = JournalTimestamps.fromZoneName("IBM:QP0100CET");
        equal("2026-09-26T10:00:00Z", ibmCet.fromSql("2026-09-26 12:00:00"));
        equal("2026-09-28T10:00:00Z", ibmCet.fromSql("2026-09-28 11:00:00"));
        rejects(() -> ibmCet.fromSql("2026-03-29 02:30:00"));
        rejects(() -> ibmCet.fromSql("2026-09-27 01:30:00"));
        ibmCet.verifyClock("2026-09-28 11:00:00", 10000,
                Instant.parse("2026-09-28T09:59:59Z"), Instant.parse("2026-09-28T10:00:01Z"));
        checks++;
        rejectsWithClockMismatch(() -> ibmCet.verifyClock("2026-09-28 11:00:00", 20000,
                Instant.parse("2026-09-28T09:59:59Z"), Instant.parse("2026-09-28T10:00:01Z")));
        System.out.println("journal_timestamp_tests=PASS checks="+checks+" jvm_zone="+TimeZone.getDefault().getID());
    }

    private static void equal(String expected, String actual) {
        if (!expected.equals(actual)) throw new AssertionError(expected+" != "+actual);
        checks++;
    }

    private static void rejects(Runnable action) {
        try { action.run(); }
        catch (RuntimeException expected) { checks++; return; }
        throw new AssertionError("expected fail-closed rejection");
    }

    /** A UTC-offset mismatch is a configuration error, never a generic fail-closed rejection. */
    private static void rejectsWithClockMismatch(Runnable action) {
        try { action.run(); }
        catch (SourceClockMismatchException expected) { checks++; return; }
        catch (RuntimeException other) {
            throw new AssertionError("expected SourceClockMismatchException, got " + other.getClass(), other);
        }
        throw new AssertionError("expected fail-closed rejection");
    }
}
