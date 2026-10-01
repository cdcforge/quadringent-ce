package io.quadringent.as400;

import java.io.PrintStream;
import java.math.BigDecimal;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLTimeoutException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.OptionalInt;
import java.util.Properties;

import com.ibm.as400.access.AS400;
import com.ibm.as400.access.AS400JDBCDataSource;
import com.ibm.as400.access.SecureAS400;

/**
 * Read-only receiver metadata catalog for the continuous source runner.
 *
 * IBM i returns the newest receivers first. The helper reverses that result
 * explicitly before emitting it; receiver names are opaque and are never
 * sorted lexically. Output contains only receiver metadata and no row data.
 */
public final class ReadOnlyReceiverCatalog {
    private static final String HEADER = "as400-receiver-catalog-v1";
    private static final String OBJECT_HEADER = "as400-object-catalog-v1";

    private ReadOnlyReceiverCatalog() {
    }

    public static void main(String[] args) {
        try {
            run(Settings.fromEnvironment());
        }
        catch (Exception error) {
            // Do not print JDBC messages: they can contain endpoint details.
            System.err.println("receiver_catalog_error=" + error.getClass().getSimpleName());
            System.exit(1);
        }
    }

    private static void run(Settings settings) throws Exception {
        Class.forName("com.ibm.as400.access.AS400JDBCDriver");
        try (Connection connection = openConnection(settings)) {
            writeReceivers(
                    connection,
                    settings.journalLibrary(),
                    settings.journalName(),
                    settings.limit(),
                    settings.requiredReceiver().orElse(null),
                    System.out);
        }
        if (settings.objectLibrary().isPresent()) {
            emitObjectCatalog(settings, settings.objectLibrary().get());
        }
    }

    /**
     * Deadline for the receiver-catalogue query, in seconds.
     *
     * Bounded to [1, 29] like the journal window queries so a slow system view
     * cannot stall the worker the way it did before 2026-08-27.
     */
    static int catalogQueryTimeoutSeconds() {
        String raw = System.getenv("AS400_CATALOG_QUERY_TIMEOUT_SECONDS");
        int seconds = raw == null || raw.isBlank() ? 10 : Integer.parseInt(raw.trim());
        if (seconds < 1 || seconds >= 30) {
            throw new IllegalArgumentException(
                    "AS400_CATALOG_QUERY_TIMEOUT_SECONDS must be in [1, 29]");
        }
        return seconds;
    }

    static void writeReceivers(
            Connection connection,
            String journalLibrary,
            String journalName,
            int limit,
            PrintStream out) throws Exception {
        writeReceivers(connection, journalLibrary, journalName, limit, null, out);
    }

    /**
     * Receivers du journal, du plus récent au plus ancien, couvrant le
     * checkpoint.
     *
     * <p>Sans {@code requiredReceiver}, la vue reste les {@code limit}
     * receivers les plus récents — le cas courant d'un lecteur au tail. Avec
     * un receiver requis (le porteur du checkpoint durable), la requête est
     * ancrée sur son ATTACH_TIMESTAMP : le résultat couvre le span
     * checkpoint→tail quel que soit le nombre de rotations depuis la
     * dernière lecture — un week-end de maintenance ne doit pas rendre un
     * checkpoint vivant invisible. Un receiver requis absent de la vue
     * (supprimé côté source) produit une liste vide : le planificateur reste
     * fail-closed, la reprise passe alors par un nouvel ancrage explicite.
     */
    static void writeReceivers(
            Connection connection,
            String journalLibrary,
            String journalName,
            int limit,
            String requiredReceiver,
            PrintStream out) throws Exception {
        if (limit < 1 || limit > 100) {
            throw new IllegalArgumentException("receiver metadata limit must be between 1 and 100");
        }
        String sql;
        if (requiredReceiver == null || requiredReceiver.isBlank()) {
            sql = """
                    SELECT JOURNAL_RECEIVER_LIBRARY,
                           JOURNAL_RECEIVER_NAME,
                           STATUS,
                           FIRST_SEQUENCE_NUMBER,
                           LAST_SEQUENCE_NUMBER
                      FROM QSYS2.JOURNAL_RECEIVER_INFO
                     WHERE JOURNAL_LIBRARY = ?
                       AND JOURNAL_NAME = ?
                     ORDER BY ATTACH_TIMESTAMP DESC
                     FETCH FIRST %d ROWS ONLY
            """.formatted(limit);
        }
        else {
            sql = """
                    SELECT JOURNAL_RECEIVER_LIBRARY,
                           JOURNAL_RECEIVER_NAME,
                           STATUS,
                           FIRST_SEQUENCE_NUMBER,
                           LAST_SEQUENCE_NUMBER
                      FROM QSYS2.JOURNAL_RECEIVER_INFO
                     WHERE JOURNAL_LIBRARY = ?
                       AND JOURNAL_NAME = ?
                       AND ATTACH_TIMESTAMP >= (
                           SELECT MIN(ATTACH_TIMESTAMP)
                             FROM QSYS2.JOURNAL_RECEIVER_INFO
                            WHERE JOURNAL_LIBRARY = ?
                              AND JOURNAL_NAME = ?
                              AND JOURNAL_RECEIVER_NAME = ?)
                     ORDER BY ATTACH_TIMESTAMP DESC
                     FETCH FIRST 1000 ROWS ONLY
            """;
        }

        List<Receiver> newestFirst = new ArrayList<>();
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            // QSYS2.JOURNAL_RECEIVER_INFO is ordered by ATTACH_TIMESTAMP over every
            // receiver of the journal. Without a bound this call can block with no
            // client-side deadline, which is what made the RetrieveJournal polls fail:
            // the failures were on this query, not on the retrieve, and only the
            // Python reader deadline ever cut them.
            statement.setQueryTimeout(catalogQueryTimeoutSeconds());
            statement.setString(1, journalLibrary);
            statement.setString(2, journalName);
            if (requiredReceiver != null && !requiredReceiver.isBlank()) {
                statement.setString(3, journalLibrary);
                statement.setString(4, journalName);
                statement.setString(5, requiredReceiver.trim());
            }
            try (ResultSet result = statement.executeQuery()) {
                while (result.next()) {
                    newestFirst.add(new Receiver(
                            required(result.getString("JOURNAL_RECEIVER_LIBRARY")),
                            required(result.getString("JOURNAL_RECEIVER_NAME")),
                            optional(result.getString("STATUS")),
                            optional(result.getBigDecimal("FIRST_SEQUENCE_NUMBER")),
                            optional(result.getBigDecimal("LAST_SEQUENCE_NUMBER"))));
                }
            }
        }

        out.println(HEADER);
        for (int index = newestFirst.size() - 1; index >= 0; index--) {
            Receiver receiver = newestFirst.get(index);
            out.printf("receiver\t%s\t%s\t%s\t%s\t%s%n",
                    token(receiver.library()),
                    token(receiver.name()),
                    token(receiver.status()),
                    token(receiver.firstSequence()),
                    token(receiver.lastSequence()));
        }
    }

    /**
     * Sonde bornee : uniquement le receiver ATTACHED du journal.
     *
     * <p>Bien moins couteuse qu'un catalogue complet (une seule ligne, filtree
     * cote serveur sur {@code STATUS = 'ATTACHED'}), appelee a chaque poll pour
     * detecter une rotation ou une progression de {@code LAST_SEQUENCE_NUMBER}
     * sans refaire une lecture complete de {@code QSYS2.JOURNAL_RECEIVER_INFO}.
     * N'emet rien si aucun receiver n'est ATTACHED (journal detache) : le
     * cote Python traite l'absence de ligne comme un echec de sonde benin.
     */
    static void writeTail(
            Connection connection,
            String journalLibrary,
            String journalName,
            PrintStream out) throws Exception {
        String sql = """
                SELECT JOURNAL_RECEIVER_LIBRARY,
                       JOURNAL_RECEIVER_NAME,
                       STATUS,
                       FIRST_SEQUENCE_NUMBER,
                       LAST_SEQUENCE_NUMBER
                  FROM QSYS2.JOURNAL_RECEIVER_INFO
                 WHERE JOURNAL_LIBRARY = ?
                   AND JOURNAL_NAME = ?
                   AND STATUS = 'ATTACHED'
                 FETCH FIRST 1 ROWS ONLY
        """;
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            // Meme deadline bornee que le catalogue complet : une vue lente ne
            // doit jamais faire depasser un poll au-dela du raisonnable.
            statement.setQueryTimeout(catalogQueryTimeoutSeconds());
            statement.setString(1, journalLibrary);
            statement.setString(2, journalName);
            try (ResultSet result = statement.executeQuery()) {
                if (result.next()) {
                    out.println(formatTailLine(
                            required(result.getString("JOURNAL_RECEIVER_LIBRARY")),
                            required(result.getString("JOURNAL_RECEIVER_NAME")),
                            optional(result.getString("STATUS")),
                            optional(result.getBigDecimal("FIRST_SEQUENCE_NUMBER")),
                            optional(result.getBigDecimal("LAST_SEQUENCE_NUMBER"))));
                }
            }
        }
    }

    /** Formatte la ligne unique du protocole tail ; pur, testable hors ligne. */
    static String formatTailLine(
            String library,
            String name,
            String status,
            String firstSequence,
            String lastSequence) {
        return "tail receiver=%s library=%s first_sequence=%s last_sequence=%s status=%s".formatted(
                token(name),
                token(library),
                token(firstSequence),
                token(lastSequence),
                token(status));
    }

    private static void emitObjectCatalog(Settings settings, String objectLibrary) throws Exception {
        Map<String, String> rowCounts = new HashMap<>();
        String statsSql = """
                SELECT TABLE_NAME, NUMBER_ROWS
                  FROM QSYS2.SYSTABLESTAT
                 WHERE TABLE_SCHEMA = ?
                """;
        String objectsSql = """
                SELECT OBJECT_LIBRARY,
                       OBJECT_NAME,
                       OBJECT_TYPE,
                       JOURNAL_IMAGES,
                       JOURNAL_LIBRARY,
                       JOURNAL_NAME
                  FROM QSYS2.JOURNALED_OBJECTS
                 WHERE OBJECT_LIBRARY = ?
                   AND OBJECT_TYPE = '*FILE'
                """;
        try (Connection connection = openConnection(settings)) {
            try (PreparedStatement statement = connection.prepareStatement(statsSql)) {
                statement.setString(1, objectLibrary);
                try (ResultSet result = statement.executeQuery()) {
                    while (result.next()) {
                        String table = result.getString("TABLE_NAME");
                        if (table != null && !table.isBlank()) {
                            rowCounts.put(table, optional(result.getBigDecimal("NUMBER_ROWS")));
                        }
                    }
                }
            }
            System.out.println(OBJECT_HEADER);
            try (PreparedStatement statement = connection.prepareStatement(objectsSql)) {
                statement.setString(1, objectLibrary);
                try (ResultSet result = statement.executeQuery()) {
                    while (result.next()) {
                        String name = required(result.getString("OBJECT_NAME"));
                        System.out.printf("object\t%s\t%s\t%s\t%s\t%s\t%s\t%s%n",
                                token(result.getString("OBJECT_LIBRARY")),
                                token(name),
                                token(result.getString("OBJECT_TYPE")),
                                token(result.getString("JOURNAL_IMAGES")),
                                token(result.getString("JOURNAL_LIBRARY")),
                                token(result.getString("JOURNAL_NAME")),
                                token(rowCounts.get(name)));
                    }
                }
            }
        }
    }

    private static Connection openConnection(Settings settings) throws Exception {
        TlsTrust.install(settings.tls());
        if (settings.databasePort().isEmpty() && settings.signonPort().isEmpty()) {
            Properties properties = new Properties();
            properties.setProperty("user", settings.user());
            properties.setProperty("password", settings.password());
            properties.setProperty("date format", "iso");
            properties.setProperty("secure", Boolean.toString(settings.tls()));
            return DriverManager.getConnection("jdbc:as400://" + settings.host(), properties);
        }

        AS400 system = settings.tls()
                ? new SecureAS400(settings.host(), settings.user(), settings.password().toCharArray())
                : new AS400(settings.host(), settings.user(), settings.password().toCharArray());
        settings.databasePort().ifPresent(port -> system.setServicePort(AS400.DATABASE, port));
        settings.signonPort().ifPresent(port -> system.setServicePort(AS400.SIGNON, port));
        settings.commandPort().ifPresent(port -> system.setServicePort(AS400.COMMAND, port));
        AS400JDBCDataSource dataSource = new AS400JDBCDataSource(system);
        dataSource.setPrompt(false);
        dataSource.setSecure(settings.tls());
        dataSource.setDateFormat("iso");
        return dataSource.getConnection();
    }

    private static String required(String value) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("receiver identity is missing");
        }
        return value;
    }

    private static String optional(String value) {
        return value == null || value.isBlank() ? null : value;
    }

    private static String optional(BigDecimal value) {
        return value == null ? null : value.toBigIntegerExact().toString();
    }

    private static String token(Object value) {
        if (value == null) {
            return "-";
        }
        return value.toString().replace('\t', '_').replace('\r', '_').replace('\n', '_');
    }

    private record Receiver(
            String library,
            String name,
            String status,
            String firstSequence,
            String lastSequence) {
    }

    private record Settings(
            String host,
            String user,
            String password,
            String journalLibrary,
            String journalName,
            int limit,
            OptionalInt databasePort,
            OptionalInt signonPort,
            OptionalInt commandPort,
            boolean tls,
            Optional<String> objectLibrary,
            Optional<String> requiredReceiver) {

        static Settings fromEnvironment() {
            return new Settings(
                    requiredEnvironment("ISERIES_HOST"),
                    requiredEnvironment("ISERIES_USER"),
                    requiredEnvironment("ISERIES_PASSWORD"),
                    requiredEnvironment("AS400_JOURNAL_LIBRARY"),
                    requiredEnvironment("AS400_JOURNAL_NAME"),
                    positiveInteger("AS400_RECEIVER_METADATA_LIMIT", 20),
                    optionalPort("AS400_DATABASE_PORT"),
                    optionalPort("AS400_SIGNON_PORT"),
                    optionalPort("AS400_COMMAND_PORT"),
                    tlsValue("AS400_TLS", true),
                    optionalEnvironment("AS400_OBJECT_LIBRARY"),
                    optionalEnvironment("AS400_CATALOG_REQUIRES_RECEIVER"));
        }

        private static Optional<String> optionalEnvironment(String name) {
            String value = System.getenv(name);
            if (value == null || value.isBlank()) {
                return Optional.empty();
            }
            return Optional.of(value);
        }

        private static String requiredEnvironment(String name) {
            String value = System.getenv(name);
            if (value == null || value.isBlank()) {
                throw new IllegalArgumentException(name + " is required");
            }
            return value;
        }

        private static int positiveInteger(String name, int defaultValue) {
            String value = System.getenv(name);
            if (value == null || value.isBlank()) {
                return defaultValue;
            }
            int parsed = Integer.parseInt(value);
            if (parsed < 1 || parsed > 100) {
                throw new IllegalArgumentException(name + " must be between 1 and 100");
            }
            return parsed;
        }

        private static OptionalInt optionalPort(String name) {
            String value = System.getenv(name);
            if (value == null || value.isBlank()) {
                return OptionalInt.empty();
            }
            int parsed = Integer.parseInt(value);
            if (parsed < 1 || parsed > 65535) {
                throw new IllegalArgumentException(name + " must be between 1 and 65535");
            }
            return OptionalInt.of(parsed);
        }

        private static boolean tlsValue(String name, boolean defaultValue) {
            boolean enabled = booleanValue(name, defaultValue);
            if (enabled) {
                return true;
            }
            if (!booleanValue("AS400_ALLOW_PLAINTEXT", false)) {
                throw new IllegalArgumentException(
                        "AS400_TLS=false requires AS400_ALLOW_PLAINTEXT=true");
            }
            return false;
        }

        private static boolean booleanValue(String name, boolean defaultValue) {
            String value = System.getenv(name);
            if (value == null || value.isBlank()) {
                return defaultValue;
            }
            if ("true".equalsIgnoreCase(value)) {
                return true;
            }
            if ("false".equalsIgnoreCase(value)) {
                return false;
            }
            throw new IllegalArgumentException(name + " must be true or false");
        }
    }
}
