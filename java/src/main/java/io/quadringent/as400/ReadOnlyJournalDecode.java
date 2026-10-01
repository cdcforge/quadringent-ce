package io.quadringent.as400;

/**
 * One-shot bounded IBM i journal decoder for probes.
 *
 * Continuous capture uses {@link PersistentJournalWorker} so AS400, JDBC and
 * RetrieveJournal stay alive across windows. This entry point still runs a
 * single env-configured window and exits.
 */
public final class ReadOnlyJournalDecode {
    private ReadOnlyJournalDecode() {
    }

    public static void main(String[] args) throws Exception {
        try (JournalSession session = JournalSession.connect(JournalSession.ConnectionSettings.fromEnvironment())) {
            session.processWindow(JournalSession.WindowRequest.fromEnvironment(), System.out);
        }
    }
}
