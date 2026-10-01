package io.quadringent.as400;

import java.util.List;

import io.debezium.ibmi.db2.journal.retrieve.FileFilter;
import io.debezium.ibmi.db2.journal.retrieve.JournalInfo;

/** Offline tests of multi-table journal session rules, never a source connection. */
public final class JournalSessionTablesTest {
    private static int checks;

    public static void main(String[] args) {
        parseTests();
        includeTests();
        dispatchTests();
        journalMismatchTests();
        sqlRefuseTests();
        identifierTests();
        System.out.println("journal_session_tables=PASS checks=" + checks);
    }

    private static void parseTests() {
        equal(List.of("SALE"), JournalSession.parseTableList(null, "SALE"));
        equal(List.of("SALE"), JournalSession.parseTableList("  ", "SALE"));
        equal(List.of("SALE", "CNTR"), JournalSession.parseTableList("SALE, CNTR", "SALE"));
        equal(List.of("CNTR", "SALE"), JournalSession.parseTableList("CNTR,SALE", "SALE"));
        rejects(() -> JournalSession.parseTableList("SALE,,CNTR", "SALE"));
        rejects(() -> JournalSession.parseTableList("SALE,SALE", "SALE"));
        rejects(() -> JournalSession.parseTableList("SALE,sale", "SALE"));
        rejects(() -> JournalSession.parseTableList("SALE,CNTR;DROP", "SALE"));
        rejects(() -> JournalSession.parseTableList("SALE,CNTR", "ORDER"));
        rejects(() -> JournalSession.parseTableList(null, ""));
        StringBuilder tooMany = new StringBuilder("SALE");
        for (int index = 2; index <= 33; index++) {
            tooMany.append(",T").append(index);
        }
        rejects(() -> JournalSession.parseTableList(tooMany.toString(), "SALE"));
        StringBuilder maxed = new StringBuilder("SALE");
        for (int index = 2; index <= 32; index++) {
            maxed.append(",T").append(index);
        }
        equal(32, JournalSession.parseTableList(maxed.toString(), "SALE").size());
    }

    private static void includeTests() {
        List<FileFilter> single = JournalSession.includeFiles("SALES", List.of("SALE"));
        equal(1, single.size());
        equal("SALES", single.get(0).schema());
        equal("SALE", single.get(0).table());
        List<FileFilter> multi = JournalSession.includeFiles("SALES", List.of("SALE", "CNTR"));
        equal(2, multi.size());
        equal("SALE", multi.get(0).table());
        equal("CNTR", multi.get(1).table());
        equal("SALES", multi.get(1).schema());
        rejects(() -> JournalSession.includeFiles("SALES", List.of()));
    }

    private static void dispatchTests() {
        List<String> tables = List.of("SALE", "CNTR");
        equal("CNTR", JournalSession.requireKnownTable("SALES", tables, "SALES", "CNTR"));
        equal("sale", JournalSession.requireKnownTable("SALES", tables, "sales", "sale"));
        equal("CNTR", JournalSession.requireKnownTable("SALES", tables, " SALES ", " CNTR "));
        rejectsState(() -> JournalSession.requireKnownTable("SALES", tables, "SALES", "ORDER"));
        rejectsState(() -> JournalSession.requireKnownTable("SALES", tables, "OTHER", "SALE"));
        rejectsState(() -> JournalSession.requireKnownTable("SALES", tables, "SALES", null));
    }

    private static void journalMismatchTests() {
        JournalInfo demojrn = new JournalInfo("DEMOJRN", "QGPL", false);
        JournalInfo same = new JournalInfo("demojrn", "qgpl", true);
        JournalInfo other = new JournalInfo("OTHER", "QGPL", false);
        JournalSession.requireSameJournal(demojrn, same, "CNTR");
        checks++;
        rejectsState(() -> JournalSession.requireSameJournal(demojrn, other, "CNTR"));
        rejectsState(() -> JournalSession.requireSameJournal(demojrn, new JournalInfo("DEMOJRN", "OTHER", false), "CNTR"));
        rejectsState(() -> JournalSession.requireSameJournal(demojrn, null, "CNTR"));
    }

    private static void identifierTests() {
        // Le soulignement et @ sont valides dans un nom système IBM i : la
        // lecture DISPLAY_JOURNAL doit accepter ce que le snapshot accepte.
        for (String valid : List.of("QDC_ORDERS", "TESTLIB", "TESTRCV001", "$LIB#1", "A@B")) {
            equal(valid, JournalSession.sqlIdentifier(valid));
            equal(valid, MultiObjectDisplayJournal.ident(valid));
        }
        equal(List.of("QDC_ORDERS", "SALE"), JournalSession.parseTableList("QDC_ORDERS,SALE", "SALE"));
        for (String unsafe : List.of("", "A'B", "A B", "A;B", "A-B", "A.B", "A/B", "A\"B")) {
            rejects(() -> JournalSession.sqlIdentifier(unsafe));
            rejects(() -> MultiObjectDisplayJournal.ident(unsafe));
        }
        rejects(() -> JournalSession.sqlIdentifier(null));
        rejects(() -> JournalSession.sqlIdentifier("A".repeat(129)));
    }

    private static void sqlRefuseTests() {
        JournalSession.refuseSqlMultiTable(List.of("SALE"));
        checks++;
        JournalSession.refuseSqlMultiTable(List.of());
        checks++;
        rejectsState(() -> JournalSession.refuseSqlMultiTable(List.of("SALE", "CNTR")));
    }

    private static void equal(Object expected, Object actual) {
        if (expected == null ? actual != null : !expected.equals(actual)) {
            throw new AssertionError(expected + " != " + actual);
        }
        checks++;
    }

    private static void rejects(Runnable action) {
        try {
            action.run();
        }
        catch (RuntimeException expected) {
            checks++;
            return;
        }
        throw new AssertionError("expected fail-closed rejection");
    }

    private static void rejectsState(Runnable action) {
        try {
            action.run();
        }
        catch (IllegalStateException expected) {
            if (expected.getMessage() == null || !expected.getMessage().contains("checkpoint must not advance")) {
                throw new AssertionError("missing fail-closed diagnostic", expected);
            }
            checks++;
            return;
        }
        throw new AssertionError("expected fail-closed IllegalStateException");
    }
}
