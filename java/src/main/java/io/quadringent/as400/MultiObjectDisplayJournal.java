package io.quadringent.as400;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.SQLTimeoutException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Properties;

/**
 * One DISPLAY_JOURNAL call covering several journaled files (no OBJECT_NAME
 * filter; WHERE keeps the whitelist). Does not replace PersistentJournalWorker.
 */
public final class MultiObjectDisplayJournal {
    private MultiObjectDisplayJournal() {
    }

    public static void main(String[] args) throws Exception {
        String host = required("ISERIES_HOST");
        String user = required("ISERIES_USER");
        String password = required("ISERIES_PASSWORD");
        String journalLibrary = ident(required("AS400_JOURNAL_LIBRARY"));
        String journalName = ident(required("AS400_JOURNAL_NAME"));
        String receiverLibrary = ident(envOr("AS400_RECEIVER_LIBRARY", journalLibrary));
        String receiver = ident(required("AS400_BOOTSTRAP_RECEIVER"));
        long start = Long.parseLong(required("AS400_BOOTSTRAP_SEQUENCE"));
        int batch = Integer.parseInt(envOr("AS400_BATCH_ENTRIES", "5000"));
        if (batch < 1 || batch > 10000) {
            throw new IllegalArgumentException("AS400_BATCH_ENTRIES out of range");
        }
        long end = start + batch - 1L;
        int timeout = Integer.parseInt(envOr("AS400_SQL_TIMEOUT_SECONDS", "25"));
        if (timeout < 1 || timeout >= 30) {
            throw new IllegalArgumentException("timeout_seconds must be in [1, 29]");
        }
        List<String> tables = parseTables(required("AS400_MULTI_TABLES"));
        String objectLibrary = ident(required("ISERIES_SCHEMA"));
        String sql = unionDisplayJournalSql(
                tables,
                journalLibrary,
                journalName,
                receiverLibrary,
                receiver,
                start,
                end,
                objectLibrary,
                batch);

        emit(Map.of(
                "event", "retrieve_start",
                "object_names", String.join(",", tables),
                "receiver", receiver,
                "start_sequence", Long.toString(start),
                "end_sequence", Long.toString(end),
                "timeout_seconds", Integer.toString(timeout)));

        TlsTrust.install(true);
        Class.forName("com.ibm.as400.access.AS400JDBCDriver");
        Properties properties = new Properties();
        properties.setProperty("user", user);
        properties.setProperty("password", password);
        properties.setProperty("date format", "iso");
        properties.setProperty("secure", "true");

        Map<String, Integer> decodedByTable = new LinkedHashMap<>();
        for (String table : tables) {
            decodedByTable.put(table, 0);
        }
        int decoded = 0;
        long startedNanos = System.nanoTime();
        try (Connection connection = DriverManager.getConnection("jdbc:as400://" + host, properties);
                Statement statement = connection.createStatement()) {
            statement.setQueryTimeout(timeout);
            try (ResultSet rows = statement.executeQuery(sql)) {
                while (rows.next()) {
                    String object = trim(rows.getString("OBJECT"));
                    String matched = matchTable(object, tables);
                    if (matched == null) {
                        continue;
                    }
                    String type = trim(rows.getString("JOURNAL_ENTRY_TYPE")).toUpperCase(Locale.ROOT);
                    if (!isRowType(type)) {
                        continue;
                    }
                    String sequence = trim(rows.getString("SEQUENCE_NUMBER"));
                    if (sequence.isEmpty()) {
                        continue;
                    }
                    decoded++;
                    decodedByTable.put(matched, decodedByTable.get(matched) + 1);
                    emit(Map.of(
                            "event", "sql_row",
                            "table", matched,
                            "object", object,
                            "sequence", sequence,
                            "type", type,
                            "timestamp", trim(rows.getString("ENTRY_TIMESTAMP")),
                            "hex_sha256", sha256Hex(trim(rows.getString("ENTRY_DATA_HEX")))));
                }
            }
        }
        catch (SQLTimeoutException timeoutError) {
            emit(Map.of(
                    "event", "window_timeout",
                    "error_type", "SqlWindowTimeout",
                    "timeout_seconds", Integer.toString(timeout)));
            return;
        }
        catch (Exception error) {
            String detail = error.getClass().getSimpleName();
            if (error.getMessage() != null && error.getMessage().contains("SQL0443")) {
                detail = "SQL0443";
            }
            emit(Map.of("event", "window_error", "error_type", detail));
            return;
        }
        long elapsedMs = Math.max(0L, (System.nanoTime() - startedNanos) / 1_000_000L);
        emit(summary("retrieve_summary", decoded, elapsedMs, String.join(",", tables), ""));
        for (String table : tables) {
            emit(summary("retrieve_summary", decodedByTable.get(table), elapsedMs, table, table));
        }
        emit(Map.of(
                "event", "window_done",
                "status", decoded > 0 ? "published" : "empty_scan",
                "decoded", Integer.toString(decoded)));
    }

    static Map<String, String> summary(
            String event, int decoded, long elapsedMs, String tables, String table) {
        Map<String, String> payload = new LinkedHashMap<>();
        payload.put("event", event);
        payload.put("decoded", Integer.toString(decoded));
        payload.put("elapsed_ms", Long.toString(elapsedMs));
        payload.put(
                "events_per_sec",
                elapsedMs > 0 ? String.format(Locale.US, "%.3f", decoded * 1000.0 / elapsedMs) : "0.000");
        if (!tables.isEmpty() && table.isEmpty()) {
            payload.put("object_names", tables);
        }
        if (!table.isEmpty()) {
            payload.put("table", table);
        }
        return payload;
    }

    static String unionDisplayJournalSql(
            List<String> tables,
            String journalLibrary,
            String journalName,
            String receiverLibrary,
            String receiver,
            long start,
            long end,
            String objectLibrary,
            int batch) {
        List<String> branches = new ArrayList<>();
        for (String table : tables) {
            branches.add("""
                    SELECT SEQUENCE_NUMBER, JOURNAL_ENTRY_TYPE, ENTRY_TIMESTAMP, '%s' AS OBJECT, HEX(ENTRY_DATA) AS ENTRY_DATA_HEX
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
                            OBJECT_LIBRARY => '%s',
                            OBJECT_NAME => '%s',
                            OBJECT_OBJTYPE => '*FILE',
                            OBJECT_MEMBER => '*ALL'
                      ))
                    """.formatted(
                    table,
                    journalLibrary,
                    journalName,
                    receiverLibrary,
                    receiver,
                    start,
                    receiverLibrary,
                    receiver,
                    end,
                    objectLibrary,
                    table));
        }
        return String.join(" UNION ALL ", branches) + " FETCH FIRST " + batch + " ROWS ONLY";
    }

    static List<String> parseTables(String csv) {
        List<String> tables = new ArrayList<>();
        for (String item : csv.split(",")) {
            String table = ident(item.trim().toUpperCase(Locale.ROOT));
            if (!tables.contains(table)) {
                tables.add(table);
            }
        }
        if (tables.size() < 2) {
            throw new IllegalArgumentException("AS400_MULTI_TABLES needs >=2 distinct tables");
        }
        return tables;
    }

    static String matchTable(String object, List<String> tables) {
        String compact = object.replace(" ", "").toUpperCase(Locale.ROOT);
        for (String table : tables) {
            if (compact.equals(table)
                    || compact.endsWith("/" + table)
                    || compact.endsWith("." + table)
                    || compact.endsWith(table)) {
                return table;
            }
        }
        return null;
    }

    static boolean isRowType(String type) {
        return "PT".equals(type)
                || "PX".equals(type)
                || "UB".equals(type)
                || "UP".equals(type)
                || "DL".equals(type)
                || "DR".equals(type);
    }

    static String ident(String value) {
        if (value == null || !value.matches("[A-Za-z0-9_$#@]{1,128}")) {
            throw new IllegalArgumentException("unsafe IBM i identifier");
        }
        return value;
    }

    static String required(String name) {
        String value = System.getenv(name);
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(name + " is required");
        }
        return value;
    }

    static String envOr(String name, String fallback) {
        String value = System.getenv(name);
        if (value == null || value.isBlank()) {
            return fallback;
        }
        return value;
    }

    static String trim(String value) {
        return value == null ? "" : value.trim();
    }

    static String sha256Hex(String hex) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] hashed = digest.digest(hex.getBytes(StandardCharsets.US_ASCII));
            StringBuilder builder = new StringBuilder(hashed.length * 2);
            for (byte item : hashed) {
                builder.append(String.format("%02x", item));
            }
            return builder.toString();
        }
        catch (Exception error) {
            return "";
        }
    }

    static void emit(Map<String, String> fields) {
        StringBuilder builder = new StringBuilder();
        builder.append('{');
        boolean first = true;
        for (Map.Entry<String, String> entry : fields.entrySet()) {
            if (!first) {
                builder.append(',');
            }
            first = false;
            builder.append(quote(entry.getKey())).append(':').append(quote(entry.getValue()));
        }
        builder.append('}');
        System.out.println(builder);
    }

    static String quote(String value) {
        StringBuilder builder = new StringBuilder();
        builder.append('"');
        for (int index = 0; index < value.length(); index++) {
            char character = value.charAt(index);
            if (character == '"' || character == '\\') {
                builder.append('\\').append(character);
            }
            else if (character == '\n') {
                builder.append("\\n");
            }
            else if (character < 0x20) {
                builder.append(' ');
            }
            else {
                builder.append(character);
            }
        }
        builder.append('"');
        return builder.toString();
    }
}
