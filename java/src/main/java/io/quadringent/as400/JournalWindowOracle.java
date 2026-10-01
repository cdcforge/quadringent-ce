package io.quadringent.as400;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

/**
 * Independent evaluation of journal metadata, not decoded business records.
 * A caller must prove the supplied window was read completely before using
 * this result as a source oracle. This class alone makes no runtime claim.
 *
 * <p>Diagnostic/verification tool: the target is declared by the caller via
 * the environment — host, user, journal name, schema and table are required,
 * validated identifiers; no installation default exists in code. TLS is
 * mandatory and plaintext is never negotiated.
 */
public final class JournalWindowOracle {
    private JournalWindowOracle() { }

    public record Result(long count, String identityDigest, Map<String, Long> types) { }

    /**
     * Journal et objet évalués, déclarés par l'appelant. Chaque identifiant
     * est exigé puis validé — aucun périmètre d'installation n'est figé.
     * La bibliothèque des récepteurs est celle du journal déclaré.
     */
    public record Scope(String journalName, String journalLibrary, String objectLibrary, String objectName) {
        static Scope fromEnvironment(Map<String, String> env) {
            return new Scope(
                    requiredIdentifier(env, "AS400_JOURNAL_NAME"),
                    requiredIdentifier(env, "AS400_JOURNAL_LIBRARY"),
                    requiredIdentifier(env, "ISERIES_SCHEMA"),
                    requiredIdentifier(env, "ISERIES_TABLE"));
        }
    }

    private static final java.util.regex.Pattern IDENTIFIER =
            java.util.regex.Pattern.compile("[A-Za-z0-9_$#@]{1,128}");
    private static final java.util.regex.Pattern HOST =
            java.util.regex.Pattern.compile("[A-Za-z0-9][A-Za-z0-9.-]{0,252}");

    public static void main(String[] args) {
        try {
            Map<String, String> env = System.getenv();
            if (args.length != 0) {
                throw new IllegalArgumentException("Oracle takes configuration from the environment only");
            }
            Scope scope = Scope.fromEnvironment(env);
            String host = required(env, "ISERIES_HOST");
            if (!HOST.matcher(host.trim()).matches()) {
                throw new IllegalArgumentException("ISERIES_HOST is not a safe endpoint");
            }
            String user = required(env, "ISERIES_USER");
            String password = required(env, "ISERIES_PASSWORD");
            if (!"true".equalsIgnoreCase(required(env, "AS400_TLS").trim())) {
                throw new IllegalArgumentException("Oracle requires AS400_TLS=true");
            }
            if (!"false".equalsIgnoreCase(required(env, "AS400_ALLOW_PLAINTEXT").trim())) {
                throw new IllegalArgumentException("Oracle never negotiates plaintext");
            }
            String receiver = required(env, "AS400_ORACLE_RECEIVER");
            long start = Long.parseLong(required(env, "AS400_ORACLE_START"));
            long end = Long.parseLong(required(env, "AS400_ORACLE_END"));
            evaluate(scope, receiver, start, end, List.of());
            TlsTrust.install(true);
            Class.forName("com.ibm.as400.access.AS400JDBCDriver");
            java.util.Properties properties = new java.util.Properties();
            properties.setProperty("user", user);
            properties.setProperty("password", password);
            properties.setProperty("secure", "true");
            properties.setProperty("prompt", "false");
            java.sql.DriverManager.setLoginTimeout(15);
            java.time.Instant started = java.time.Instant.now();
            Result result;
            try (java.sql.Connection connection = java.sql.DriverManager.getConnection(
                    "jdbc:as400://" + host.trim(), properties)) {
                connection.setReadOnly(true);
                result = scan(connection, scope, receiver, start, end);
            }
            String counts = new TreeMap<>(result.types()).entrySet().stream()
                    .map(entry -> "\"" + entry.getKey() + "\":" + entry.getValue())
                    .collect(java.util.stream.Collectors.joining(","));
            System.out.printf(java.util.Locale.ROOT,
                    "{\"format_version\":\"quadringent-journal-oracle-v1\",\"status\":\"observed\","
                    + "\"source\":\"%s.%s\",\"receiver\":\"%s\",\"start\":%d,\"end\":%d,"
                    + "\"count\":%d,\"identity_digest\":\"%s\",\"types\":{%s},"
                    + "\"started_at\":\"%s\",\"observed_at\":\"%s\",\"reconciled\":false}%n",
                    scope.objectLibrary(), scope.objectName(), receiver, start, end,
                    result.count(), result.identityDigest(), counts,
                    started, java.time.Instant.now());
        }
        catch (Exception failure) {
            // Never print JDBC messages, connection strings, environment or stacks.
            String kind = failure instanceof IllegalArgumentException ? "IllegalArgumentException"
                    : failure instanceof java.sql.SQLException ? "SQLException" : "OracleUnavailable";
            System.out.println("{\"status\":\"unverified\",\"error_type\":\"" + kind + "\"}");
            System.exit(2);
        }
    }

    /** Read every metadata row in the declared window or return no result. */
    public static Result scan(
            java.sql.Connection connection, Scope scope, String receiver, long start, long end)
            throws java.sql.SQLException {
        evaluate(scope, receiver, start, end, List.of()); // validate before any SQL
        String sql = """
                SELECT SEQUENCE_NUMBER, JOURNAL_ENTRY_TYPE
                FROM TABLE(QSYS2.DISPLAY_JOURNAL(
                    JOURNAL_LIBRARY => '%s', JOURNAL_NAME => '%s',
                    STARTING_RECEIVER_LIBRARY => '%s', STARTING_RECEIVER_NAME => ?,
                    STARTING_SEQUENCE => ?,
                    ENDING_RECEIVER_LIBRARY => '%s', ENDING_RECEIVER_NAME => ?,
                    ENDING_SEQUENCE => ?, JOURNAL_CODES => 'R',
                    OBJECT_LIBRARY => '%s', OBJECT_NAME => '%s',
                    OBJECT_OBJTYPE => '*FILE', OBJECT_MEMBER => '*ALL'))
                """.formatted(scope.journalLibrary(), scope.journalName(),
                        scope.journalLibrary(), scope.journalLibrary(),
                        scope.objectLibrary(), scope.objectName());
        List<String[]> metadata = new ArrayList<>();
        try (java.sql.PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setString(1, receiver);
            statement.setLong(2, start);
            statement.setString(3, receiver);
            statement.setLong(4, end);
            statement.setQueryTimeout(25);
            try (java.sql.ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    if (metadata.size() >= 10_000) {
                        throw new IllegalArgumentException("Journal metadata exceeds the event budget");
                    }
                    String type = rows.getString(2);
                    metadata.add(new String[]{rows.getString(1), type == null ? null : type.trim()});
                }
            }
        }
        return evaluate(scope, receiver, start, end, metadata);
    }

    public static Result evaluate(Scope scope, String receiver, long start, long end, List<String[]> rows) {
        if (scope == null) {
            throw new IllegalArgumentException("Journal scope is required");
        }
        if (receiver == null || !receiver.startsWith(scope.journalName())
                || !receiver.substring(scope.journalName().length()).matches("[0-9]{1,4}")) {
            throw new IllegalArgumentException("Invalid journal receiver for the declared journal");
        }
        if (start < 1 || end < start || end - start >= 1_000_000L) {
            throw new IllegalArgumentException("Journal window must contain at most one million positions");
        }
        if (rows == null || rows.size() > 10_000) {
            throw new IllegalArgumentException("Journal metadata exceeds the event budget");
        }
        Set<Long> positions = new HashSet<>();
        List<String> identities = new ArrayList<>();
        Map<String, Long> types = new TreeMap<>();
        for (String[] row : rows) {
            if (row == null || row.length != 2 || row[0] == null || row[1] == null) {
                throw new IllegalArgumentException("Invalid journal metadata row");
            }
            long sequence;
            try { sequence = Long.parseLong(row[0]); }
            catch (NumberFormatException invalid) {
                throw new IllegalArgumentException("Invalid journal position");
            }
            if (sequence < start || sequence > end || !positions.add(sequence)) {
                throw new IllegalArgumentException("Duplicate or out-of-window journal position");
            }
            // Intentionally independent from JournalSession's decoder enum.
            if (!Set.of("PT", "PX", "UB", "UP", "DL").contains(row[1])) {
                throw new IllegalArgumentException("Journal operation requires separate verification");
            }
            identities.add(sha256("ibmi|" + scope.journalName() + "|" + receiver + "|" + sequence));
            types.merge(row[1], 1L, Long::sum);
        }
        identities.sort(String::compareTo);
        return new Result(rows.size(), sha256(String.join("\n", identities)), Map.copyOf(types));
    }

    private static String required(Map<String, String> env, String name) {
        String value = env.get(name);
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(name + " is required");
        }
        return value;
    }

    private static String requiredIdentifier(Map<String, String> env, String name) {
        String value = required(env, name).trim();
        if (!IDENTIFIER.matcher(value).matches()) {
            throw new IllegalArgumentException(name + " is not a safe identifier");
        }
        return value;
    }

    private static String sha256(String input) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(input.getBytes(StandardCharsets.UTF_8)));
        }
        catch (java.security.NoSuchAlgorithmException unavailable) {
            throw new IllegalStateException("SHA-256 unavailable");
        }
    }
}
