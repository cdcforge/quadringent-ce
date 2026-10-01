package io.quadringent.as400;

import io.debezium.ibmi.db2.journal.retrieve.JournalEntryType;

/** Runs the real SQL type decoder without opening a source connection. */
public final class SqlJournalEntryTypesTest {
    public static void main(String[] args) {
        for (String code : new String[] {"BR", "UR", "DR", " dr "}) {
            try {
                JournalSession.sqlEntryType(code);
                throw new AssertionError("rollback entry silently accepted or ignored: " + code);
            } catch (IllegalStateException expected) {
                if (!expected.getMessage().contains("checkpoint must not advance")) {
                    throw new AssertionError("missing safe-stop diagnostic", expected);
                }
            }
        }
        check("PT", JournalEntryType.ADD_ROW1);
        check("PX", JournalEntryType.ADD_ROW2);
        check("UB", JournalEntryType.BEFORE_IMAGE);
        check("UP", JournalEntryType.AFTER_IMAGE);
        check("DL", JournalEntryType.DELETE_ROW);
        check(" dl ", JournalEntryType.DELETE_ROW);
        check("IL", null);
        check(null, null);
        System.out.println("sql_journal_entry_types=PASS");
    }

    private static void check(String code, JournalEntryType expected) {
        if (JournalSession.sqlEntryType(code) != expected) {
            throw new AssertionError("unexpected SQL journal type mapping");
        }
    }
}
