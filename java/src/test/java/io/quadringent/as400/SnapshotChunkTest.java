package io.quadringent.as400;

import java.nio.file.Path;
import java.util.OptionalInt;

/**
 * Contrat du découpage RRN de la copie historique : une tranche exige deux
 * bornes ordonnées, la sonde de borne est exclusive, l'offset ordinal reste
 * positif, et seule une lecture bornée peut être vide.
 */
public final class SnapshotChunkTest {
    public static void main(String[] args) {
        ReadOnlyTableSnapshot.Settings plain = settings(-1, -1, 0, false);
        equal("SELECT T.*, RRN(T) AS \"_rrn\" FROM SALES.SALE T", ReadOnlyTableSnapshot.snapshotSql(plain));
        ReadOnlyTableSnapshot.Settings ranged = settings(5, 10, 40, false);
        equal(
                "SELECT T.*, RRN(T) AS \"_rrn\" FROM SALES.SALE T WHERE RRN(T) BETWEEN ? AND ? ORDER BY RRN(T)",
                ReadOnlyTableSnapshot.snapshotSql(ranged));
        equal(
                "SELECT MAX(RRN(T)) FROM SALES.SALE T",
                ReadOnlyTableSnapshot.rrnBoundSql(plain));
        rejects(() -> settings(5, -1, 0, false));
        rejects(() -> settings(-1, 9, 0, false));
        rejects(() -> settings(0, 9, 0, false));
        rejects(() -> settings(9, 5, 0, false));
        rejects(() -> settings(5, 10, -1, false));
        rejects(() -> settings(5, 10, 0, true));
        ReadOnlyTableSnapshot.Settings.validateChunking(settings(-1, -1, 0, true));
        if (!ReadOnlyTableSnapshot.emptyReadAllowed(ranged)) {
            throw new AssertionError("a bounded read may be empty");
        }
        if (ReadOnlyTableSnapshot.emptyReadAllowed(plain)) {
            throw new AssertionError("an unbounded read must never be empty");
        }
        // Bornes socket/login : défauts quand l'env est absent, bornes refusées.
        checkTimeouts(plain);
        checkIdentifiersAndPorts();
        System.out.println("snapshot_chunk_tests=PASS checks=29");
    }

    /**
     * Schéma et table sont concaténés dans le SQL : seul un identifiant IBM i
     * borné passe. Les ports déclarés restent dans la borne TCP.
     */
    private static void checkIdentifiersAndPorts() {
        equal(
                "SALES",
                ReadOnlyTableSnapshot.Settings.requireIdentifier("ISERIES_SCHEMA", "SALES"));
        equal(
                "SALE$1",
                ReadOnlyTableSnapshot.Settings.requireIdentifier("ISERIES_TABLE", "SALE$1"));
        rejects(() -> ReadOnlyTableSnapshot.Settings.requireIdentifier(
                "ISERIES_TABLE", "SALE T WHERE 1=1"));
        rejects(() -> ReadOnlyTableSnapshot.Settings.requireIdentifier(
                "ISERIES_TABLE", "SALE;DROP"));
        rejects(() -> ReadOnlyTableSnapshot.Settings.requireIdentifier("ISERIES_SCHEMA", ""));
        rejects(() -> ReadOnlyTableSnapshot.Settings.requireIdentifier(
                "ISERIES_SCHEMA", "A".repeat(129)));
        equal(446, ReadOnlyTableSnapshot.Settings.parsePort("AS400_DATABASE_PORT", "446"));
        equal(1, ReadOnlyTableSnapshot.Settings.parsePort("AS400_DATABASE_PORT", "1"));
        rejects(() -> ReadOnlyTableSnapshot.Settings.parsePort("AS400_DATABASE_PORT", "0"));
        rejects(() -> ReadOnlyTableSnapshot.Settings.parsePort("AS400_DATABASE_PORT", "65536"));
        rejects(() -> ReadOnlyTableSnapshot.Settings.parsePort("AS400_DATABASE_PORT", "-1"));
    }

    private static void checkTimeouts(ReadOnlyTableSnapshot.Settings settings) {
        if (settings.socketTimeoutMs() != 120_000 || settings.loginTimeoutMs() != 60_000) {
            throw new AssertionError("timeout defaults changed");
        }
        if (ReadOnlyTableSnapshot.loginTimeoutSeconds(settings) != 60) {
            throw new AssertionError("login timeout must convert ms to seconds");
        }
        equal(
                1,
                ReadOnlyTableSnapshot.loginTimeoutSeconds(withTimeouts(1_000, 1_000)));
        if (ReadOnlyTableSnapshot.Settings.parseTimeoutMillis("T", null, 42) != 42
                || ReadOnlyTableSnapshot.Settings.parseTimeoutMillis("T", "  ", 42) != 42
                || ReadOnlyTableSnapshot.Settings.parseTimeoutMillis("T", "1500", 42) != 1500) {
            throw new AssertionError("timeout parsing contract broken");
        }
        rejects(() -> ReadOnlyTableSnapshot.Settings.parseTimeoutMillis("T", "0", 42));
        rejects(() -> ReadOnlyTableSnapshot.Settings.parseTimeoutMillis("T", "999", 42));
        rejects(() -> ReadOnlyTableSnapshot.Settings.parseTimeoutMillis("T", "3600001", 42));
    }

    private static ReadOnlyTableSnapshot.Settings withTimeouts(int socketMs, int loginMs) {
        return new ReadOnlyTableSnapshot.Settings(
                "host",
                "user",
                "pw",
                "SALES",
                "SALE",
                Path.of("/tmp"),
                "438b9fdd-d15e-458a-8349-d11b8f6291a4",
                5000,
                500,
                60,
                socketMs,
                loginMs,
                true,
                OptionalInt.empty(),
                OptionalInt.empty(),
                -1,
                -1,
                0,
                false);
    }

    private static void equal(int a, int b) {
        if (a != b) throw new AssertionError(a + " != " + b);
    }

    private static ReadOnlyTableSnapshot.Settings settings(
            long rrnStart, long rrnEnd, long rowOffset, boolean rrnProbe) {
        ReadOnlyTableSnapshot.Settings settings = new ReadOnlyTableSnapshot.Settings(
                "host",
                "user",
                "pw",
                "SALES",
                "SALE",
                Path.of("/tmp"),
                "438b9fdd-d15e-458a-8349-d11b8f6291a4",
                5000,
                500,
                60,
                120_000,
                60_000,
                true,
                OptionalInt.empty(),
                OptionalInt.empty(),
                rrnStart,
                rrnEnd,
                rowOffset,
                rrnProbe);
        ReadOnlyTableSnapshot.Settings.validateChunking(settings);
        return settings;
    }

    private static void equal(String a, String b) {
        if (!a.equals(b)) throw new AssertionError(a + " != " + b);
    }

    private static void rejects(Runnable action) {
        try {
            action.run();
        }
        catch (IllegalArgumentException expected) {
            return;
        }
        throw new AssertionError("expected rejection");
    }
}
