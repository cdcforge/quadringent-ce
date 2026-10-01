package io.quadringent.as400;

import java.util.List;

/** Offline tests of the independent metadata oracle, never a source connection. */
public final class JournalWindowOracleTest {
    private static int checks;

    public static void main(String[] args) throws Exception {
        Class<?> oracle;
        try { oracle = Class.forName("io.quadringent.as400.JournalWindowOracle"); }
        catch (ClassNotFoundException missing) {
            throw new AssertionError("Independent journal metadata oracle is not implemented");
        }
        Class<?> scopeType;
        try { scopeType = Class.forName("io.quadringent.as400.JournalWindowOracle$Scope"); }
        catch (ClassNotFoundException missing) {
            throw new AssertionError("Oracle scope is not implemented");
        }
        // Données d'entrée de test : le journal, le schéma et la table sont
        // déclarés par l'appelant — la fixture n'est pas un défaut du code.
        Object scope = scopeType
                .getDeclaredConstructor(String.class, String.class, String.class, String.class)
                .newInstance("DEMOJRN", "JRNLIB", "SALES", "SALE");
        var evaluate = oracle.getDeclaredMethod(
                "evaluate", scopeType, String.class, long.class, long.class, List.class);
        // Inputs do not contain row images. Source metadata is evaluated without
        // importing the production journal decoder or its filtering rules.
        Object result = evaluate.invoke(null, scope, "DEMOJRN3940", 100L, 200L,
                List.of(new String[]{"111", "PT"}, new String[]{"151", "UP"}, new String[]{"180", "DL"}));
        var get = result.getClass().getDeclaredMethod("count");
        equal(3L, get.invoke(result));
        var hash = result.getClass().getDeclaredMethod("identityDigest");
        equal("256f8b83f21325d004677a89a5844729127c558d27fe1358cd94a6c39727bc44", hash.invoke(result));
        Object reordered = evaluate.invoke(null, scope, "DEMOJRN3940", 100L, 200L,
                List.of(new String[]{"180", "DL"}, new String[]{"111", "PT"}, new String[]{"151", "UP"}));
        equal(hash.invoke(result), hash.invoke(reordered));
        var counts = result.getClass().getDeclaredMethod("types");
        equal(java.util.Map.of("PT", 1L, "UP", 1L, "DL", 1L), counts.invoke(result));
        Object otherJournal = scopeType
                .getDeclaredConstructor(String.class, String.class, String.class, String.class)
                .newInstance("TRNJRN", "JRNLIB", "SALES", "SALE");
        rejects(() -> evaluate.invoke(null, otherJournal, "DEMOJRN3940", 100L, 200L,
                List.of(new String[]{"111", "PT"})));
        rejects(() -> evaluate.invoke(null, scope, "DEMOJRN3940", 100L, 200L,
                List.<String[]>of(new String[]{"99", "PT"})));
        rejects(() -> evaluate.invoke(null, scope, "DEMOJRN3940", 100L, 200L,
                List.<String[]>of(new String[]{"201", "PT"})));
        rejects(() -> evaluate.invoke(null, scope, "DEMOJRN3940", 100L, 200L,
                List.of(new String[]{"111", "PT"}, new String[]{"111", "PT"})));
        for (String unsupported : List.of("NR", "UR", "DR", "ZZ", "")) {
            rejects(() -> evaluate.invoke(null, scope, "DEMOJRN3940", 100L, 200L,
                    List.<String[]>of(new String[]{"111", unsupported})));
        }
        for (String unsafe : List.of("../secret", "DEMOJRN3940'", "", "DEMOJRN3940\n", "TRNJRN3940")) {
            rejects(() -> evaluate.invoke(null, scope, unsafe, 100L, 200L, List.of()));
        }
        rejects(() -> evaluate.invoke(null, scope, "DEMOJRN3940", 0L, 200L, List.of()));
        rejects(() -> evaluate.invoke(null, scope, "DEMOJRN3940", 200L, 100L, List.of()));
        rejects(() -> evaluate.invoke(null, scope, "DEMOJRN3940", 1L, 1000001L, List.of()));
        rejects(() -> evaluate.invoke(null, scope, "DEMOJRN3940", 1L, Long.MAX_VALUE, List.of()));
        Object empty = evaluate.invoke(null, scope, "DEMOJRN3940", 100L, 200L, List.of());
        equal(0L, get.invoke(empty));
        equal("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", hash.invoke(empty));
        jdbcTests(oracle, scopeType, scope);
        entrypointTests();
        System.out.println("journal_window_oracle_tests=PASS checks=" + checks);
    }

    private static void entrypointTests() throws Exception {
        for (String mode : List.of("missing", "unsafe_host", "plaintext", "no_journal")) {
            ProcessBuilder builder = new ProcessBuilder(
                    System.getProperty("java.home") + "/bin/java", "-cp",
                    System.getProperty("java.class.path"), "io.quadringent.as400.JournalWindowOracle");
            builder.environment().clear();
            if (!mode.equals("missing")) {
                builder.environment().putAll(java.util.Map.ofEntries(
                    java.util.Map.entry("ISERIES_HOST", mode.equals("unsafe_host") ? "bad host/" : "127.0.0.1"),
                    java.util.Map.entry("ISERIES_USER", "CDCAPP"),
                    java.util.Map.entry("ISERIES_SCHEMA", "SALES"),
                    java.util.Map.entry("ISERIES_TABLE", "SALE"),
                    java.util.Map.entry("AS400_JOURNAL_LIBRARY", "JRNLIB"),
                    java.util.Map.entry("AS400_TLS", mode.equals("plaintext") ? "false" : "true"),
                    java.util.Map.entry("AS400_ALLOW_PLAINTEXT", "false"),
                    java.util.Map.entry("AS400_ORACLE_RECEIVER", "DEMOJRN3940"),
                    java.util.Map.entry("AS400_ORACLE_START", "100"),
                    java.util.Map.entry("AS400_ORACLE_END", "200"),
                    java.util.Map.entry("ISERIES_PASSWORD", "test-credential-must-not-appear")));
                if (!mode.equals("no_journal")) {
                    builder.environment().put("AS400_JOURNAL_NAME", "DEMOJRN");
                }
            }
            builder.redirectErrorStream(true);
            Process process = builder.start();
            if (!process.waitFor(5, java.util.concurrent.TimeUnit.SECONDS)) {
                process.destroyForcibly();
                throw new AssertionError("Invalid configuration did not fail before connection");
            }
            String output = new String(process.getInputStream().readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
            equal(2, process.exitValue());
            equal("{\"status\":\"unverified\",\"error_type\":\"IllegalArgumentException\"}\n", output);
        }
    }

    private static void jdbcTests(Class<?> oracle, Class<?> scopeType, Object scope) throws Exception {
        java.lang.reflect.Method scan;
        try {
            scan = oracle.getDeclaredMethod(
                    "scan", java.sql.Connection.class, scopeType, String.class, long.class, long.class);
        }
        catch (NoSuchMethodException missing) { throw new AssertionError("Bounded JDBC scan is missing"); }
        JdbcFake complete = new JdbcFake(3, false);
        Object result = scan.invoke(null, complete.connection(), scope, "DEMOJRN3940", 100L, 200L);
        equal(3L, result.getClass().getDeclaredMethod("count").invoke(result));
        equal(4, complete.nextCalls);
        equal(25, complete.timeout);
        equal(true, complete.resultClosed);
        equal(true, complete.statementClosed);
        equal(java.util.Map.of(1, "DEMOJRN3940", 2, 100L, 3, "DEMOJRN3940", 4, 200L), complete.params);
        if (complete.sql.contains("ENTRY_DATA") || complete.sql.contains("FETCH FIRST")) {
            throw new AssertionError("Oracle reads payloads or truncates source metadata");
        }
        if (!complete.sql.contains("JOURNAL_LIBRARY => 'JRNLIB'")
                || !complete.sql.contains("JOURNAL_NAME => 'DEMOJRN'")
                || !complete.sql.contains("OBJECT_LIBRARY => 'SALES'")
                || !complete.sql.contains("OBJECT_NAME => 'SALE'")) {
            throw new AssertionError("Oracle must scan only the declared journal scope");
        }
        JdbcFake overflow = new JdbcFake(10001, false);
        rejects(() -> scan.invoke(null, overflow.connection(), scope, "DEMOJRN3940", 1L, 20000L));
        equal(true, overflow.resultClosed);
        equal(true, overflow.statementClosed);
        JdbcFake failed = new JdbcFake(3, true);
        try { scan.invoke(null, failed.connection(), scope, "DEMOJRN3940", 100L, 200L); }
        catch (java.lang.reflect.InvocationTargetException error) {
            if (!(error.getCause() instanceof java.sql.SQLException)) throw error;
            equal(true, failed.resultClosed);
            equal(true, failed.statementClosed);
            return;
        }
        throw new AssertionError("Partial JDBC result incorrectly accepted");
    }

    private static final class JdbcFake {
        final int rows;
        final boolean fail;
        int nextCalls;
        int timeout;
        boolean resultClosed;
        boolean statementClosed;
        String sql;
        final java.util.Map<Integer, Object> params = new java.util.HashMap<>();
        JdbcFake(int rows, boolean fail) { this.rows = rows; this.fail = fail; }
        java.sql.Connection connection() {
            return proxy(java.sql.Connection.class, (p, method, args) -> {
                if (!method.getName().equals("prepareStatement")) throw new AssertionError(method.getName());
                sql = (String) args[0];
                return proxy(java.sql.PreparedStatement.class, (s, call, values) -> {
                    switch (call.getName()) {
                        case "setString", "setLong": params.put((Integer) values[0], values[1]); return null;
                        case "setQueryTimeout": timeout = (Integer) values[0]; return null;
                        case "close": statementClosed = true; return null;
                        case "executeQuery": return proxy(java.sql.ResultSet.class, (r, read, fields) -> {
                            switch (read.getName()) {
                                case "next":
                                    nextCalls++;
                                    if (fail && nextCalls == 2) throw new java.sql.SQLException("test only");
                                    return nextCalls <= rows;
                                case "getString": return ((Integer) fields[0]) == 1 ? Integer.toString(100 + nextCalls) : "PT";
                                case "close": resultClosed = true; return null;
                                default: throw new AssertionError(read.getName());
                            }
                        });
                        default: throw new AssertionError(call.getName());
                    }
                });
            });
        }
        private static <T> T proxy(Class<T> type, java.lang.reflect.InvocationHandler handler) {
            return type.cast(java.lang.reflect.Proxy.newProxyInstance(type.getClassLoader(), new Class<?>[]{type}, handler));
        }
    }

    private static void equal(Object expected, Object actual) {
        if (!expected.equals(actual)) throw new AssertionError(expected + " != " + actual);
        checks++;
    }

    private static void rejects(Checked action) throws Exception {
        try { action.run(); }
        catch (java.lang.reflect.InvocationTargetException error) {
            if (!(error.getCause() instanceof IllegalArgumentException)) throw error;
            checks++;
            return;
        }
        throw new AssertionError("Oracle accepted an invalid or unverifiable window");
    }

    private interface Checked { void run() throws Exception; }
}
