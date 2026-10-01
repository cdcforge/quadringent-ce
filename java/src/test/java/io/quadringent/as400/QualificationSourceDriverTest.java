package io.quadringent.as400;

import java.util.List;

/** Offline safety checks for the dedicated IBM i qualification source CLI. */
public final class QualificationSourceDriverTest {
    private static int checks;

    public static void main(String[] args) {
        var scope = new QualificationSourceDriver.Scope(
                "QUALTEST", "QUALIF_ORDERS", "QUALTEST", "QUALJRN", "ORDER_ID",
                List.of("ORDER_ID", "LABEL"));
        accepts(scope, "INSERT INTO QUALTEST.QUALIF_ORDERS (ORDER_ID, LABEL) VALUES (1, 'Café')");
        accepts(scope, "UPDATE QUALTEST.QUALIF_ORDERS SET LABEL = 'Après' WHERE ORDER_ID = 1");
        accepts(scope, "DELETE FROM QUALTEST.QUALIF_ORDERS WHERE ORDER_ID = 1");
        refuses(scope, "DELETE FROM QUALTEST.BUSINESS_ORDERS WHERE ORDER_ID = 1");
        refuses(scope, "DELETE FROM QUALTEST.QUALIF_ORDERS WHERE ORDER_ID = 1; DROP TABLE X");
        refuses(scope, "UPDATE QUALTEST.QUALIF_ORDERS SET LABEL = 'x' -- comment WHERE ORDER_ID = 1");
        refuses(scope, "SELECT * FROM QUALTEST.QUALIF_ORDERS");
        if (!QualificationSourceDriver.journalMatches(scope, "QUALTEST", "QUALJRN")) {
            throw new AssertionError("configured journal should match the source table");
        }
        checks++;
        if (QualificationSourceDriver.journalMatches(scope, "QUALTEST", "OTHERJRN")
                || QualificationSourceDriver.journalMatches(scope, "OTHERLIB", "QUALJRN")) {
            throw new AssertionError("journal outside source table must be refused");
        }
        checks++;
        equal("RCVLIB", QualificationSourceDriver.receiverLibraryForStart(
                List.of(new QualificationSourceDriver.ReceiverRef("RCVLIB", "RCV0001")), "RCV0001"));
        rejects(() -> QualificationSourceDriver.receiverLibraryForStart(
                List.of(new QualificationSourceDriver.ReceiverRef("RCVLIB", "RCV0001")), "OTHER"));
        rejects(() -> QualificationSourceDriver.receiverLibraryForStart(
                List.of(new QualificationSourceDriver.ReceiverRef("RCVLIB", "RCV0001"),
                        new QualificationSourceDriver.ReceiverRef("OTHERLIB", "RCV0001")), "RCV0001"));
        equal("\"line\\nquote\\\"slash\\\\\"", QualificationSourceDriver.jsonString("line\nquote\"slash\\"));
        rejects(() -> new QualificationSourceDriver.Scope(
                "QUALTEST;DROP", "QUALIF_ORDERS", "QUALTEST", "QUALJRN", "ORDER_ID",
                List.of("ORDER_ID")));
        rejects(() -> new QualificationSourceDriver.Scope(
                "QUALTEST", "QUALIF_ORDERS", "QUALTEST", "QUALJRN", "ORDER_ID",
                List.of("LABEL")));
        System.out.println("qualification_source_driver=PASS checks=" + checks);
    }

    private static void accepts(QualificationSourceDriver.Scope scope, String sql) {
        if (!QualificationSourceDriver.allowedDml(scope, sql)) throw new AssertionError(sql);
        checks++;
    }

    private static void refuses(QualificationSourceDriver.Scope scope, String sql) {
        if (QualificationSourceDriver.allowedDml(scope, sql)) throw new AssertionError(sql);
        checks++;
    }

    private static void equal(String expected, String actual) {
        if (!expected.equals(actual)) throw new AssertionError("JSON escape mismatch");
        checks++;
    }

    private static void rejects(Runnable action) {
        try { action.run(); }
        catch (IllegalArgumentException expected) { checks++; return; }
        throw new AssertionError("expected IllegalArgumentException");
    }
}
