package io.quadringent.as400;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.sql.CallableStatement;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * Dedicated qualification source driver. Identifiers and mode are arguments;
 * host, user, password and generated DML arrive on stdin. TLS is mandatory.
 * No JDBC message, stack trace, credential or SQL text is printed on failure.
 */
public final class QualificationSourceDriver {
    private static final Pattern IDENTIFIER = Pattern.compile("[A-Za-z_][A-Za-z0-9_]{0,127}");
    private static final Pattern RECEIVER = Pattern.compile("[A-Za-z0-9_$#@]{1,128}");
    private static final Pattern HOST = Pattern.compile("[A-Za-z0-9][A-Za-z0-9.-]{0,252}");
    private static final Pattern USER = Pattern.compile("[A-Za-z0-9_$#@]{1,128}");
    private static final int MAX_RECEIVERS = 500;
    private static final int MAX_POSITIONS = 10_000;
    private static final int MAX_ROWS = 5_000;
    private static final int MAX_STATEMENTS = 500;

    private QualificationSourceDriver() { }

    public record Scope(String library, String table, String journalLibrary, String journalName,
                        String primaryKey, List<String> columns) {
        public Scope {
            for (String value : List.of(library, table, journalLibrary, journalName, primaryKey)) {
                if (value == null || !IDENTIFIER.matcher(value).matches()) {
                    throw new IllegalArgumentException("invalid qualification source identifier");
                }
            }
            if (columns == null || columns.isEmpty() || columns.size() > 32) {
                throw new IllegalArgumentException("invalid qualification columns");
            }
            columns = List.copyOf(columns);
            Set<String> unique = new HashSet<>();
            for (String column : columns) {
                if (column == null || !IDENTIFIER.matcher(column).matches() || !unique.add(column)) {
                    throw new IllegalArgumentException("invalid qualification column");
                }
            }
            if (!unique.contains(primaryKey)) {
                throw new IllegalArgumentException("qualification primary key is absent");
            }
        }

        String qualifiedTable() { return library + "." + table; }
    }

    private record Position(String receiver, long sequence) { }
    private record Journal(String library, String name) { }
    record ReceiverRef(String library, String name) {
        ReceiverRef {
            if (library == null || !IDENTIFIER.matcher(library).matches()
                    || name == null || !RECEIVER.matcher(name).matches()) {
                throw new IllegalArgumentException("invalid qualification receiver");
            }
        }
    }

    static String receiverLibraryForStart(List<ReceiverRef> chain, String receiverName) {
        String library = null;
        for (ReceiverRef receiver : chain) {
            if (receiver.name().equals(receiverName)) {
                if (library != null) throw new IllegalArgumentException("ambiguous starting receiver");
                library = receiver.library();
            }
        }
        if (library == null) throw new IllegalArgumentException("starting receiver is unavailable");
        return library;
    }

    public static void main(String[] args) {
        try {
            if (args.length != 7 && args.length != 9) {
                throw new IllegalArgumentException("invalid qualification driver arguments");
            }
            String mode = args[0];
            if (!Set.of("exec", "tail", "dump", "rowpos", "rotate", "inspect").contains(mode)
                    || (mode.equals("rowpos") != (args.length == 9))) {
                throw new IllegalArgumentException("invalid qualification driver mode");
            }
            Scope scope = new Scope(args[1], args[2], args[3], args[4], args[5],
                    List.of(args[6].split(",", -1)));
            BufferedReader input = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
            String host = input.readLine();
            String user = input.readLine();
            String password = input.readLine();
            if (host == null || !HOST.matcher(host).matches()
                    || user == null || !USER.matcher(user).matches()
                    || password == null || password.isBlank()) {
                throw new IllegalArgumentException("invalid qualification connection input");
            }
            List<String> statements = mode.equals("exec") ? readStatements(input, scope) : List.of();
            TlsTrust.install(true);
            Class.forName("com.ibm.as400.access.AS400JDBCDriver");
            Properties properties = new Properties();
            properties.setProperty("user", user);
            properties.setProperty("password", password);
            properties.setProperty("secure", "true");
            properties.setProperty("prompt", "false");
            DriverManager.setLoginTimeout(15);
            try (Connection connection = DriverManager.getConnection("jdbc:as400://" + host, properties)) {
                connection.setAutoCommit(true);
                if (!mode.equals("exec") && !mode.equals("rotate")) connection.setReadOnly(true);
                Journal journal = tableJournal(connection, scope);
                if (!mode.equals("inspect") && !journalMatches(scope, journal.library(), journal.name())) {
                    throw new SQLException("configured journal does not belong to qualification table");
                }
                switch (mode) {
                    case "exec" -> execute(connection, statements);
                    case "tail" -> tail(connection, scope);
                    case "dump" -> dump(connection, scope);
                    case "rowpos" -> rowPositions(connection, scope, args[7], Long.parseLong(args[8]));
                    case "rotate" -> rotate(connection, scope);
                    case "inspect" -> System.out.println("SRC_JOURNAL={\"JOURNAL_LIBRARY\":"
                            + jsonString(journal.library()) + ",\"JOURNAL_NAME\":"
                            + jsonString(journal.name()) + "}");
                    default -> throw new IllegalArgumentException("invalid qualification driver mode");
                }
            }
        } catch (IllegalArgumentException failure) {
            System.out.println("SRC_FAILED=invalid_input");
            System.exit(2);
        } catch (SQLException failure) {
            System.out.println("SRC_FAILED=source_sql");
            System.exit(3);
        } catch (Exception failure) {
            System.out.println("SRC_FAILED=source_unavailable");
            System.exit(4);
        }
    }

    static boolean journalMatches(Scope scope, String actualLibrary, String actualName) {
        return scope != null && actualLibrary != null && actualName != null
                && scope.journalLibrary().equalsIgnoreCase(actualLibrary.trim())
                && scope.journalName().equalsIgnoreCase(actualName.trim());
    }

    private static Journal tableJournal(Connection connection, Scope scope) throws SQLException {
        String sql = "SELECT JOURNAL_LIBRARY, JOURNAL_NAME FROM QSYS2.JOURNALED_OBJECTS "
                + "WHERE OBJECT_LIBRARY = ? AND OBJECT_NAME = ? AND OBJECT_TYPE = '*FILE' "
                + "FETCH FIRST 2 ROWS ONLY";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setString(1, scope.library());
            statement.setString(2, scope.table());
            statement.setQueryTimeout(30);
            try (ResultSet rows = statement.executeQuery()) {
                if (!rows.next()) throw new SQLException("qualification table is not journaled");
                String library = rows.getString(1);
                String name = rows.getString(2);
                if (rows.next() || library == null || name == null
                        || !IDENTIFIER.matcher(library.trim()).matches()
                        || !IDENTIFIER.matcher(name.trim()).matches()) {
                    throw new SQLException("qualification table journal is ambiguous");
                }
                return new Journal(library.trim(), name.trim());
            }
        }
    }

    private static List<String> readStatements(BufferedReader input, Scope scope) throws java.io.IOException {
        List<String> statements = new ArrayList<>();
        String line;
        while ((line = input.readLine()) != null) {
            if (!allowedDml(scope, line) || statements.size() >= MAX_STATEMENTS) {
                throw new IllegalArgumentException("DML outside qualification table");
            }
            statements.add(line);
        }
        if (statements.isEmpty()) throw new IllegalArgumentException("no qualification DML");
        return statements;
    }

    /** Secondary guard: Python checks the exact deterministic statement set. */
    static boolean allowedDml(Scope scope, String sql) {
        if (scope == null || sql == null || sql.length() > 8192 || sql.isBlank()
                || !sql.equals(sql.trim()) || sql.contains(";") || sql.contains("--")
                || sql.contains("/*") || sql.contains("*/") || sql.contains("\n") || sql.contains("\r")) {
            return false;
        }
        String table = Pattern.quote(scope.qualifiedTable());
        String key = Pattern.quote(scope.primaryKey());
        return sql.matches("INSERT INTO " + table + " \\([A-Za-z0-9_, ]+\\) VALUES \\(.+\\)")
                || sql.matches("UPDATE " + table + " SET .+ WHERE " + key + " = [0-9]+")
                || sql.matches("DELETE FROM " + table + " WHERE " + key + " = [0-9]+");
    }

    private static void execute(Connection connection, List<String> statements) throws SQLException {
        for (String sql : statements) {
            try (Statement statement = connection.createStatement()) {
                statement.setQueryTimeout(30);
                statement.executeUpdate(sql);
            }
        }
        System.out.println("SRC_EXEC_TOTAL=" + statements.size());
    }

    private static void tail(Connection connection, Scope scope) throws SQLException {
        String sql = "SELECT JOURNAL_RECEIVER_LIBRARY, JOURNAL_RECEIVER_NAME, "
                + "COALESCE(LAST_SEQUENCE_NUMBER, 0) AS LAST_SEQUENCE_NUMBER "
                + "FROM QSYS2.JOURNAL_RECEIVER_INFO "
                + "WHERE JOURNAL_LIBRARY = ? AND JOURNAL_NAME = ? "
                + "ORDER BY ATTACH_TIMESTAMP, JOURNAL_RECEIVER_NAME";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setString(1, scope.journalLibrary());
            statement.setString(2, scope.journalName());
            statement.setQueryTimeout(30);
            try (ResultSet rows = statement.executeQuery()) {
                emitRows(rows, "SRC_TAIL", MAX_RECEIVERS);
            }
        }
    }

    private static void dump(Connection connection, Scope scope) throws SQLException {
        String sql = "SELECT " + String.join(", ", scope.columns()) + " FROM "
                + scope.qualifiedTable() + " ORDER BY " + scope.primaryKey();
        try (Statement statement = connection.createStatement()) {
            statement.setQueryTimeout(60);
            try (ResultSet rows = statement.executeQuery(sql)) {
                emitRows(rows, "SRC_ROW", MAX_ROWS);
            }
        }
    }

    private static List<ReceiverRef> receiverNames(Connection connection, Scope scope) throws SQLException {
        List<ReceiverRef> names = new ArrayList<>();
        String sql = "SELECT JOURNAL_RECEIVER_LIBRARY, JOURNAL_RECEIVER_NAME "
                + "FROM QSYS2.JOURNAL_RECEIVER_INFO "
                + "WHERE JOURNAL_LIBRARY = ? AND JOURNAL_NAME = ? "
                + "ORDER BY ATTACH_TIMESTAMP, JOURNAL_RECEIVER_NAME";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setString(1, scope.journalLibrary());
            statement.setString(2, scope.journalName());
            statement.setQueryTimeout(30);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    if (names.size() >= MAX_RECEIVERS) throw new SQLException("receiver budget exceeded");
                    String library = rows.getString(1);
                    String name = rows.getString(2);
                    if (library == null || name == null
                            || !IDENTIFIER.matcher(library.trim()).matches()
                            || !RECEIVER.matcher(name.trim()).matches()
                            || names.stream().anyMatch(item -> item.name().equals(name.trim()))) {
                        throw new SQLException("invalid receiver chain");
                    }
                    names.add(new ReceiverRef(library.trim(), name.trim()));
                }
            }
        }
        return names;
    }

    private static void rowPositions(Connection connection, Scope scope, String startReceiver,
                                     long startSequence) throws SQLException {
        if (startReceiver == null || !RECEIVER.matcher(startReceiver).matches() || startSequence < 0) {
            throw new IllegalArgumentException("invalid journal starting position");
        }
        List<ReceiverRef> allReceivers = receiverNames(connection, scope);
        String startingLibrary = receiverLibraryForStart(allReceivers, startReceiver);
        int startIndex = 0;
        while (!allReceivers.get(startIndex).name().equals(startReceiver)) startIndex++;
        List<ReceiverRef> chain = allReceivers.subList(startIndex, allReceivers.size());
        Map<String, Integer> rank = new HashMap<>();
        for (int index = 0; index < chain.size(); index++) {
            ReceiverRef receiver = chain.get(index);
            rank.put(receiver.name(), index);
            System.out.println("SRC_RECEIVER={\"JOURNAL_RECEIVER_LIBRARY\":"
                    + jsonString(receiver.library()) + ",\"JOURNAL_RECEIVER_NAME\":"
                    + jsonString(receiver.name()) + "}");
        }
        String sql = "SELECT RECEIVER_NAME, SEQUENCE_NUMBER FROM TABLE(QSYS2.DISPLAY_JOURNAL("
                + "JOURNAL_LIBRARY => '" + scope.journalLibrary() + "', "
                + "JOURNAL_NAME => '" + scope.journalName() + "', "
                + "STARTING_RECEIVER_LIBRARY => ?, "
                + "STARTING_RECEIVER_NAME => ?, STARTING_SEQUENCE => ?, JOURNAL_CODES => 'R', "
                + "OBJECT_LIBRARY => '" + scope.library() + "', "
                + "OBJECT_NAME => '" + scope.table() + "', "
                + "OBJECT_OBJTYPE => '*FILE', OBJECT_MEMBER => '*ALL')) "
                + "WHERE JOURNAL_ENTRY_TYPE IN ('PT','PX','UB','UP','DL')";
        List<Position> positions = new ArrayList<>();
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setString(1, startingLibrary);
            statement.setString(2, startReceiver);
            statement.setLong(3, Math.max(1, startSequence));
            statement.setQueryTimeout(60);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    if (positions.size() >= MAX_POSITIONS) throw new SQLException("position budget exceeded");
                    String receiver = rows.getString(1);
                    long sequence = rows.getLong(2);
                    if (receiver == null || !rank.containsKey(receiver.trim()) || sequence < 0) {
                        throw new SQLException("position outside receiver chain");
                    }
                    positions.add(new Position(receiver.trim(), sequence));
                }
            }
        }
        positions.sort(Comparator.comparingInt((Position item) -> rank.get(item.receiver()))
                .thenComparingLong(Position::sequence));
        for (Position position : positions) {
            System.out.println("SRC_ROWPOS={\"JOURNAL_RECEIVER_NAME\":" + jsonString(position.receiver())
                    + ",\"SEQUENCE_NUMBER\":" + position.sequence() + "}");
        }
        System.out.println("SRC_ROWPOS_COUNT=" + positions.size());
    }

    private static void rotate(Connection connection, Scope scope) throws SQLException {
        String command = "CHGJRN JRN(" + scope.journalLibrary() + "/" + scope.journalName()
                + ") JRNRCV(*GEN)";
        try (CallableStatement call = connection.prepareCall("CALL QSYS2.QCMDEXC(?)")) {
            call.setString(1, command);
            call.setQueryTimeout(60);
            call.execute();
        }
        System.out.println("SRC_ROTATED=true");
    }

    private static void emitRows(ResultSet rows, String prefix, int maxRows) throws SQLException {
        ResultSetMetaData metadata = rows.getMetaData();
        int count = 0;
        while (rows.next()) {
            if (++count > maxRows) throw new SQLException("qualification row budget exceeded");
            StringBuilder json = new StringBuilder("{");
            for (int index = 1; index <= metadata.getColumnCount(); index++) {
                if (index > 1) json.append(',');
                json.append(jsonString(metadata.getColumnLabel(index))).append(':');
                String value = rows.getString(index);
                json.append(value == null ? "null" : jsonString(value));
            }
            System.out.println(prefix + "=" + json.append('}'));
        }
        System.out.println(prefix + "_COUNT=" + count);
    }

    static String jsonString(String value) {
        StringBuilder result = new StringBuilder("\"");
        for (int index = 0; index < value.length(); index++) {
            char character = value.charAt(index);
            switch (character) {
                case '"' -> result.append("\\\"");
                case '\\' -> result.append("\\\\");
                case '\n' -> result.append("\\n");
                case '\r' -> result.append("\\r");
                case '\t' -> result.append("\\t");
                default -> {
                    if (character < 0x20) result.append(String.format("\\u%04x", (int) character));
                    else result.append(character);
                }
            }
        }
        return result.append('"').toString();
    }
}
