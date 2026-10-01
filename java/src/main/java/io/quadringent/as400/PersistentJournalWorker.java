package io.quadringent.as400;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.Map;

/**
 * Long-lived JVM that keeps AS400, JDBC and RetrieveJournal open.
 *
 * Each stdin JSON line is one bounded window. stdout emits summary then raw
 * status, then {@code window_done}. Fail-closed windows emit
 * {@code window_error} without {@code scan_complete=true}.
 */
public final class PersistentJournalWorker {
    private PersistentJournalWorker() {
    }

    public static void main(String[] args) throws Exception {
        JournalSession.ConnectionSettings settings = JournalSession.ConnectionSettings.fromEnvironment();
        JournalSession session;
        try {
            session = JournalSession.connect(settings);
        }
        catch (Exception error) {
            // Le pilote décide de sa politique de reconnexion sur ce code :
            // USER_DISABLED et AUTHENTICATION_FAILED sont définitifs (rejouer
            // le sign-on verrouillerait le profil), CONNECTION_FAILED couvre
            // une source coupée ou en maintenance — pause bornée, jamais de
            // rafale. Émis sur stdout, le canal de contrôle, avant la sortie.
            System.out.println("connect_error=" + FleetCatalogProbe.classifyConnectFailure(error));
            System.out.flush();
            System.exit(2);
            return;
        }
        try (JournalSession active = session;
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
                if (isCatalog(trimmed)) {
                    try {
                        CatalogRequest request = catalogRequest(trimmed);
                        active.emitReceiverCatalog(
                                request.limit(), request.requiredReceiver(), System.out);
                        System.out.println("catalog_done");
                    }
                    catch (Exception error) {
                        System.out.println("catalog_error=" + error.getClass().getSimpleName());
                    }
                    System.out.flush();
                    continue;
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
                if (isTail(trimmed)) {
                    try {
                        active.emitTail(System.out);
                        System.out.println("tail_done");
                    }
                    catch (Exception error) {
                        System.out.println("tail_error=" + error.getClass().getSimpleName());
                    }
                    System.out.flush();
                    continue;
                }
                if (isSqlWindow(trimmed)) {
                    try {
                        active.processSqlWindow(JournalSession.WindowRequest.fromJson(trimmed), System.out);
                        System.out.println("window_done");
                    }
                    catch (Exception error) {
                        System.out.println("window_error=" + error.getClass().getSimpleName());
                    }
                    System.out.flush();
                    continue;
                }
                try {
                    active.processWindow(JournalSession.WindowRequest.fromJson(trimmed), System.out);
                    System.out.println("process_window_returned");
                    System.out.flush();
                    System.out.println("window_done");
                }
                catch (Exception error) {
                    System.out.println("window_error=" + error.getClass().getSimpleName());
                }
                System.out.flush();
            }
        }
    }

    private static boolean isShutdown(String line) {
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

    private static boolean isSqlWindow(String line) {
        try {
            Map<String, String> fields = JournalSession.parseFlatJson(line);
            return "sql_window".equalsIgnoreCase(fields.get("cmd"));
        }
        catch (RuntimeException ignored) {
            return false;
        }
    }

    private static boolean isCatalog(String line) {
        if ("catalog".equalsIgnoreCase(line)) {
            return true;
        }
        try {
            Map<String, String> fields = JournalSession.parseFlatJson(line);
            return "catalog".equalsIgnoreCase(fields.get("cmd"));
        }
        catch (RuntimeException ignored) {
            return false;
        }
    }

    private static boolean isDiscover(String line) {
        try {
            Map<String, String> fields = JournalSession.parseFlatJson(line);
            return "discover".equalsIgnoreCase(fields.get("cmd"));
        }
        catch (RuntimeException ignored) {
            return false;
        }
    }

    private static boolean isProbe(String line) {
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

    private static boolean isTail(String line) {
        if ("tail".equalsIgnoreCase(line)) {
            return true;
        }
        try {
            Map<String, String> fields = JournalSession.parseFlatJson(line);
            return "tail".equalsIgnoreCase(fields.get("cmd"));
        }
        catch (RuntimeException ignored) {
            return false;
        }
    }

    private record CatalogRequest(int limit, String requiredReceiver) {
    }

    private static CatalogRequest catalogRequest(String line) {
        if ("catalog".equalsIgnoreCase(line)) {
            return new CatalogRequest(20, null);
        }
        Map<String, String> fields = JournalSession.parseFlatJson(line);
        String value = fields.get("limit");
        int parsed = 20;
        if (value != null && !value.isBlank()) {
            parsed = Integer.parseInt(value);
        }
        if (parsed < 1 || parsed > 100) {
            throw new IllegalArgumentException("receiver metadata limit must be between 1 and 100");
        }
        String required = fields.get("requires_receiver");
        if (required != null && required.isBlank()) {
            required = null;
        }
        return new CatalogRequest(parsed, required);
    }
}
