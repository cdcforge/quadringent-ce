package io.quadringent.as400;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Properties;

/**
 * Read-only DISPLAY_JOURNAL DL/DR counters. Metadata only, no payloads.
 */
public final class JournalDlScan {
    private JournalDlScan() {
    }

    public static void main(String[] args) throws Exception {
        String host = required("ISERIES_HOST");
        String user = required("ISERIES_USER");
        String password = required("ISERIES_PASSWORD");
        String receiver = ident(required("AS400_SCAN_RECEIVER"));
        String table = required("ISERIES_TABLE");
        String schema = ident(required("ISERIES_SCHEMA"));
        String journalName = ident(required("AS400_JOURNAL_NAME"));
        String journalLibrary = ident(required("AS400_JOURNAL_LIBRARY"));
        String types = entryTypes(getenv("AS400_SCAN_TYPES", "DLDR"));
        long start = Long.parseLong(required("AS400_SCAN_START"));
        long end = Long.parseLong(required("AS400_SCAN_END"));
        int fetch = Integer.parseInt(getenv("AS400_SCAN_FETCH", "20"));
        if (fetch < 1 || fetch > 1000) {
            throw new IllegalArgumentException("AS400_SCAN_FETCH out of range");
        }

        TlsTrust.install(true);
        Class.forName("com.ibm.as400.access.AS400JDBCDriver");
        Properties properties = new Properties();
        properties.setProperty("user", user);
        properties.setProperty("password", password);
        properties.setProperty("secure", "true");
        properties.setProperty("prompt", "false");
        properties.setProperty("date format", "iso");
        System.out.println("as400-dl-scan-v1");
        try (Connection connection = DriverManager.getConnection("jdbc:as400://" + host, properties)) {
            scan(connection, receiver, start, end, types, journalName, journalLibrary, schema, table, fetch);
        }
        System.out.println("scan_done");
    }

    private static void scan(
            Connection connection,
            String receiver,
            long start,
            long end,
            String types,
            String journalName,
            String journalLibrary,
            String schema,
            String table,
            int fetch) {
        String objectClause;
        if ("*ALL".equalsIgnoreCase(table) || "ALL".equalsIgnoreCase(table)) {
            objectClause = "";
        }
        else {
            objectClause = """
                    , OBJECT_LIBRARY => '%s'
                    , OBJECT_NAME => '%s'
                    , OBJECT_OBJTYPE => '*FILE'
                    , OBJECT_MEMBER => '*ALL'
                    """.formatted(schema, ident(table));
        }
        String sql = """
                SELECT JOURNAL_ENTRY_TYPE, COUNT(*) AS N
                  FROM TABLE(QSYS2.DISPLAY_JOURNAL(
                        JOURNAL_LIBRARY => '%s',
                        JOURNAL_NAME => '%s',
                        STARTING_RECEIVER_LIBRARY => '%s',
                        STARTING_RECEIVER_NAME => '%s',
                        STARTING_SEQUENCE => %d,
                        ENDING_RECEIVER_LIBRARY => '%s',
                        ENDING_RECEIVER_NAME => '%s',
                        ENDING_SEQUENCE => %d,
                        JOURNAL_CODES => 'R',
                        JOURNAL_ENTRY_TYPES => '%s'
                        %s
                  ))
                 GROUP BY JOURNAL_ENTRY_TYPE
                 FETCH FIRST %d ROWS ONLY
                """.formatted(journalLibrary, journalName, journalLibrary, receiver, start,
                        journalLibrary, receiver, end, types, objectClause, fetch);
        long t0 = System.nanoTime();
        Map<String, Long> counts = new LinkedHashMap<>();
        try (Statement statement = connection.createStatement()) {
            statement.setQueryTimeout(90);
            try (ResultSet result = statement.executeQuery(sql)) {
                while (result.next()) {
                    String type = result.getString(1);
                    long n = result.getLong(2);
                    counts.put(type == null ? "-" : type.trim(), n);
                }
            }
            long ms = (System.nanoTime() - t0) / 1_000_000L;
            System.out.printf(
                    "window table=%s receiver=%s start=%d end=%s types=%s counts=%s ms=%d%n",
                    table, receiver, start, end, types, counts, ms);
        }
        catch (Exception error) {
            String message = String.valueOf(error.getMessage());
            if (message.length() > 180) {
                message = message.substring(0, 180);
            }
            message = message.replace('\n', ' ').replace('\r', ' ');
            System.out.printf("window error=%s msg=%s%n", error.getClass().getSimpleName(), message);
        }
    }

    private static String required(String name) {
        String value = System.getenv(name);
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(name + " is required");
        }
        return value;
    }

    private static String ident(String value) {
        if (value == null || !value.matches("[A-Za-z0-9_$#@]{1,128}")) {
            throw new IllegalArgumentException("unsafe IBM i identifier");
        }
        return value;
    }

    private static String entryTypes(String value) {
        if (value == null || !value.matches("[A-Z]{1,16}")) {
            throw new IllegalArgumentException("AS400_SCAN_TYPES is not a safe entry type list");
        }
        return value;
    }

    private static String getenv(String name, String fallback) {
        String value = System.getenv(name);
        if (value == null || value.isBlank()) {
            return fallback;
        }
        return value;
    }
}
