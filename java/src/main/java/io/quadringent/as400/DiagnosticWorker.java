package io.quadringent.as400;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.Map;

import com.ibm.as400.access.AS400;

/**
 * JVM de diagnostic : ouvre seulement la session IBM i (JTOpen + JDBC), sans
 * aucune des exigences propres à la capture.
 *
 * <p>Réutilise la même tuyauterie de connexion que {@link JournalSession}
 * ({@link TlsTrust}, {@link JournalSession#newAs400}, {@link
 * JournalSession#configureServicePorts}, {@link
 * JournalSession#openJdbcConnection}) mais n'exige ni {@code
 * AS400_SOURCE_TIME_ZONE}, ni bibliothèque/table capturée, ni journal :
 * {@link JournalTimestamps#verifySourceClock} et {@link
 * JournalSession#verifyCapturedJournal} ne sont jamais appelés ici. C'est
 * précisément ce qui distingue ce worker de {@link PersistentJournalWorker},
 * dont les contrôles de capture restent stricts et inchangés.
 *
 * <p>Sert le même protocole ligne que {@link PersistentJournalWorker} pour
 * {@code probe} ({@link SourceInfo#write}) et {@code discover} ({@link
 * TableDiscovery}), avec les mêmes sorties ({@code probe_done}/{@code
 * probe_error=}, lignes {@code table\t...} de découverte) — les Jobs de
 * diagnostic Kubernetes (sonde de source, découverte de tables) peuvent donc
 * s'exécuter contre une source qui n'est pas encore configurée pour la
 * capture.
 */
public final class DiagnosticWorker implements AutoCloseable {
    private static final int DISCOVER_QUERY_TIMEOUT_SECONDS = 10;

    private final AS400 as400;
    private final Connection jdbc;

    private DiagnosticWorker(AS400 as400, Connection jdbc) {
        this.as400 = as400;
        this.jdbc = jdbc;
    }

    public static DiagnosticWorker connect(JournalSession.ConnectionSettings settings) throws Exception {
        TlsTrust.install(settings.tls());
        Class.forName("com.ibm.as400.access.AS400JDBCDriver");
        AS400 as400 = JournalSession.newAs400(settings);
        try {
            Connection jdbc = JournalSession.openJdbcConnection(settings, as400);
            return new DiagnosticWorker(as400, jdbc);
        }
        catch (Exception error) {
            as400.disconnectAllServices();
            throw error;
        }
    }

    /** Même mesure que {@code JournalSession#emitSourceInfo} : aucune ligne métier. */
    public void emitSourceInfo(PrintStream out) throws Exception {
        SourceInfo.write(jdbc, out);
    }

    /** Découverte de tables, catalogue seulement — voir {@link TableDiscovery#emit}. */
    public void emitDiscover(TableDiscovery.DiscoverRequest request, PrintStream out) throws Exception {
        TableDiscovery.emit(jdbc, request, DISCOVER_QUERY_TIMEOUT_SECONDS, out);
    }

    /**
     * Bibliothèque courante de la connexion, pour {@code discover} sans
     * bibliothèque explicite — lue depuis la connexion JDBC elle-même
     * ({@code CURRENT_SCHEMA}/{@code CURRENT LIBRARY} du profil connecté)
     * puisqu'aucune bibliothèque n'est configurée pour ce worker.
     */
    public String currentLibrary() throws SQLException {
        return jdbc.getSchema();
    }

    @Override
    public void close() {
        try {
            jdbc.close();
        }
        catch (SQLException ignored) {
            // Connection teardown must not hide a diagnostic error.
        }
        as400.disconnectAllServices();
    }

    public static void main(String[] args) throws Exception {
        JournalSession.ConnectionSettings settings = JournalSession.ConnectionSettings.diagnosticFromEnvironment();
        DiagnosticWorker session;
        try {
            session = DiagnosticWorker.connect(settings);
        }
        catch (Exception error) {
            // Même classifieur, même protocole que PersistentJournalWorker : le
            // pilote Python décide de sa politique sur ce seul code.
            System.out.println("connect_error=" + FleetCatalogProbe.classifyConnectFailure(error));
            System.out.flush();
            System.exit(2);
            return;
        }
        try (DiagnosticWorker active = session;
                BufferedReader reader = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
            System.out.println("worker_ready");
            System.out.flush();
            String line;
            while ((line = reader.readLine()) != null) {
                String trimmed = line.trim();
                if (trimmed.isEmpty()) {
                    continue;
                }
                if (isShutdown(trimmed)) {
                    break;
                }
                if (isDiscover(trimmed)) {
                    try {
                        Map<String, String> fields = JournalSession.parseFlatJson(trimmed);
                        TableDiscovery.DiscoverRequest request =
                                TableDiscovery.DiscoverRequest.fromFields(fields, active.currentLibrary());
                        active.emitDiscover(request, System.out);
                        System.out.println("discover_done");
                    }
                    catch (Exception error) {
                        System.out.println("discover_error=" + error.getClass().getSimpleName());
                    }
                    System.out.flush();
                    continue;
                }
                if (isProbe(trimmed)) {
                    try {
                        active.emitSourceInfo(System.out);
                        System.out.println("probe_done");
                    }
                    catch (Exception error) {
                        System.out.println("probe_error=" + error.getClass().getSimpleName());
                    }
                    System.out.flush();
                    continue;
                }
                // Aucune fenêtre de journal, aucun catalogue de receivers ici :
                // ce worker ne connaît pas de table capturée.
                System.out.println("unsupported_command=" + safeToken(trimmed));
                System.out.flush();
            }
        }
    }

    static String safeToken(String line) {
        return line.replaceAll("[^A-Za-z0-9_=.:-]", "_");
    }

    static boolean isShutdown(String line) {
        if ("shutdown".equalsIgnoreCase(line)) {
            return true;
        }
        try {
            Map<String, String> fields = JournalSession.parseFlatJson(line);
            return "shutdown".equalsIgnoreCase(fields.get("cmd"));
        }
        catch (RuntimeException ignored) {
            return false;
        }
    }

    static boolean isDiscover(String line) {
        try {
            Map<String, String> fields = JournalSession.parseFlatJson(line);
            return "discover".equalsIgnoreCase(fields.get("cmd"));
        }
        catch (RuntimeException ignored) {
            return false;
        }
    }

    static boolean isProbe(String line) {
        if ("probe".equalsIgnoreCase(line)) {
            return true;
        }
        try {
            Map<String, String> fields = JournalSession.parseFlatJson(line);
            return "probe".equalsIgnoreCase(fields.get("cmd"));
        }
        catch (RuntimeException ignored) {
            return false;
        }
    }
}
