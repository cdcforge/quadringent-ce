package io.quadringent.as400;

import java.math.BigInteger;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.SQLNonTransientConnectionException;
import java.sql.SQLTimeoutException;
import java.sql.SQLTransientConnectionException;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.OptionalInt;
import java.util.Properties;
import java.util.Set;
import java.util.regex.Pattern;

import com.ibm.as400.access.AS400;
import com.ibm.as400.access.AS400JDBCDataSource;
import com.ibm.as400.access.AS400SecurityException;
import com.ibm.as400.access.ReturnCodeException;
import com.ibm.as400.access.SecureAS400;

/**
 * Metadata-only IBM i fleet catalog probe for Quadringent full-history planning.
 *
 * <p>Stdout is one closed JSON document. Stderr on failure is a safe code only.
 * Credentials, business rows, ENTRY_DATA and SQL text never leave the process
 * on stdout. Tables may use distinct journals; each journal keeps its own
 * receiver chain.
 */
public final class FleetCatalogProbe {
    static final String FORMAT_VERSION = "quadringent-fleet-catalog-v1";
    static final String CONTINUITY_PROVEN = "proven";
    static final String CONTINUITY_UNCERTAIN = "uncertain";
    static final int MAX_SCOPE_TABLES = 64;
    static final Set<String> FORBIDDEN_JSON_KEYS = Set.of(
            "password",
            "secret",
            "secrets",
            "payload",
            "payloads",
            "entry_data",
            "sql",
            "error",
            "errors",
            "stack",
            "stack_trace",
            "host",
            "user",
            "endpoint",
            "endpoints",
            "jdbc",
            "credential",
            "credentials",
            "exception",
            "query",
            "rows");

    private static final Pattern IDENTIFIER = Pattern.compile("[A-Za-z$#@][A-Za-z0-9_$#@]{0,127}");
    private static final Pattern TYPE_NAME = Pattern.compile("[A-Za-z][A-Za-z0-9_ ]{0,127}");
    private static final Pattern JOURNAL_IMAGES = Pattern.compile("\\*[A-Z0-9]+");
    private static final Pattern CONSTRAINT_TYPE = Pattern.compile("[A-Z][A-Z ]{0,31}");
    private static final Pattern SQLSTATE = Pattern.compile("[A-Za-z0-9]{5}");
    private static final int RECEIVER_FETCH_LIMIT = 101;
    private static final int METADATA_FETCH_LIMIT = 5000;
    private static final int VENDOR_CODE_MIN = -999999;
    private static final int VENDOR_CODE_MAX = 999999;

    private FleetCatalogProbe() {
    }

    public static void main(String[] args) {
        try {
            System.out.print(probe(Settings.fromEnvironment(), CatalogScope.fromEnvironment()));
        }
        catch (Exception error) {
            System.err.println(formatFailure(error));
            System.exit(1);
        }
    }

    static String probe(Settings settings, CatalogScope scope) throws Exception {
        Class.forName("com.ibm.as400.access.AS400JDBCDriver");
        try (Connection connection = openConnection(settings)) {
            connection.setReadOnly(true);
            return closedJson(loadCatalog(connection, Instant.now(), scope), scope);
        }
        catch (SQLException sql) {
            throw sqlFailure(sql);
        }
    }

    static Connection openConnection(Settings settings) throws Exception {
        installTls(settings.tls());
        int loginSeconds = loginTimeoutSeconds();
        applyLoginTimeout(loginSeconds);
        Connection connection;
        if (settings.databasePort().isEmpty() && settings.signonPort().isEmpty()) {
            Properties properties = new Properties();
            properties.setProperty("user", settings.user());
            properties.setProperty("password", settings.password());
            properties.setProperty("date format", "iso");
            properties.setProperty("secure", Boolean.toString(settings.tls()));
            properties.setProperty("prompt", "false");
            properties.setProperty("access", "read only");
            properties.setProperty("login timeout", Integer.toString(loginSeconds));
            connection = DriverManager.getConnection("jdbc:as400://" + settings.host(), properties);
        }
        else {
            try {
                connection = configurePortedDataSource(settings).dataSource().getConnection();
            }
            catch (ProbeFailure failure) {
                throw failure;
            }
            catch (SQLException sql) {
                throw sql;
            }
            catch (Exception error) {
                throw configurationFailure(error);
            }
        }
        connection.setReadOnly(true);
        return connection;
    }

    /** Configure les ports de service sans ouvrir de connexion. */
    static PortedJdbc configurePortedDataSource(Settings settings) {
        try {
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
            dataSource.setAccess("read only");
            return new PortedJdbc(system, dataSource);
        }
        catch (ProbeFailure failure) {
            throw failure;
        }
        catch (Exception error) {
            throw configurationFailure(error);
        }
    }

    static ProbeFailure configurationFailure(Throwable error) {
        if (error instanceof ProbeFailure failure) {
            return failure;
        }
        return new ProbeFailure("CONNECTION_FAILED");
    }

    static void installTls(boolean tls) {
        if (tls && (permissiveTrustProperty("com.ibm.as400.access.SSLTrustAll")
                || permissiveTrustProperty("com.ibm.as400.access.SecureAS400.trustAll"))) {
            throw new ProbeFailure("TLS_FAILED");
        }
        try {
            TlsTrust.install(tls);
        }
        catch (IllegalArgumentException | IllegalStateException error) {
            throw new ProbeFailure("TLS_FAILED");
        }
    }

    static int loginTimeoutSeconds() {
        return parseLoginTimeoutSeconds(System.getenv("AS400_LOGIN_TIMEOUT_SECONDS"));
    }

    static int parseLoginTimeoutSeconds(String raw) {
        int seconds = raw == null || raw.isBlank() ? 15 : Integer.parseInt(raw.trim());
        if (seconds < 1 || seconds > 29) {
            throw new IllegalArgumentException("AS400_LOGIN_TIMEOUT_SECONDS must be in [1, 29]");
        }
        return seconds;
    }

    static void applyLoginTimeout(int seconds) {
        DriverManager.setLoginTimeout(seconds);
    }

    private static boolean permissiveTrustProperty(String name) {
        return "true".equalsIgnoreCase(System.getProperty(name));
    }

    /**
     * Closed stderr line: symbolic code, optional validated SQLSTATE and bounded
     * vendor code. Never copies {@code getMessage()}, host, user, SQL or secrets.
     */
    static String formatFailure(Throwable error) {
        if (error instanceof ProbeFailure failure) {
            return failure.stderrLine();
        }
        SQLException sql = firstSqlException(error);
        if (sql != null) {
            return sqlFailure(sql).stderrLine();
        }
        if (isTlsTrustFailure(error) || containsTls(error)) {
            return "fleet_catalog_error=TLS_FAILED";
        }
        return "fleet_catalog_error=UNKNOWN_SQL";
    }

    static ProbeFailure sqlFailure(SQLException error) {
        return new ProbeFailure(classifySqlException(error), firstSqlState(error), firstVendorCode(error));
    }

    /**
     * Classifie un échec d'établissement de session, SQL ou non.
     *
     * <p>Le pilote Python décide de sa politique de reconnexion sur ce seul
     * code : {@code USER_DISABLED} et {@code AUTHENTICATION_FAILED} sont des
     * verdicts définitifs (rejouer le sign-on verrouillerait le profil),
     * {@code CONNECTION_FAILED} couvre une source coupée ou en maintenance —
     * pause bornée, jamais de rafale. Émis par le worker persistent sous la
     * forme {@code connect_error=<code>} quand {@link JournalSession#connect}
     * échoue.
     */
    static String classifyConnectFailure(Throwable error) {
        SQLException sql = firstSqlException(error);
        if (sql != null) {
            return classifySqlException(sql);
        }
        if (isTlsTrustFailure(error) || containsTls(error)) {
            return "TLS_FAILED";
        }
        boolean userDisabled = false;
        boolean authentication = false;
        boolean clockMismatch = false;
        for (Throwable item : exceptionChain(error)) {
            if (isUserDisabled(item)) userDisabled = true;
            if (isAuthenticationFailure(item)) authentication = true;
            if (item instanceof SourceClockMismatchException) clockMismatch = true;
        }
        if (userDisabled) return "USER_DISABLED";
        if (authentication) return "AUTHENTICATION_FAILED";
        // Erreur de configuration (fuseau déclaré incohérent avec la source),
        // jamais une coupure : classée avant le repli CONNECTION_FAILED.
        if (clockMismatch) return "SOURCE_CLOCK_MISMATCH";
        return "CONNECTION_FAILED";
    }

    static String classifySqlException(SQLException error) {
        boolean userDisabled = false;
        boolean authentication = false;
        boolean tls = false;
        boolean timeout = false;
        boolean connection = false;
        boolean query = false;
        for (Throwable item : exceptionChain(error)) {
            if (isUserDisabled(item)) userDisabled = true;
            if (isAuthenticationFailure(item)) authentication = true;
            if (isTlsType(item)) tls = true;
            if (item instanceof SQLTimeoutException) timeout = true;
            if (item instanceof SQLNonTransientConnectionException
                    || item instanceof SQLTransientConnectionException) connection = true;
            if (item instanceof SQLException sql) {
                String state = sql.getSQLState();
                if (validSqlState(state)) {
                    String normalized = state.toUpperCase(Locale.ROOT);
                    if (normalized.startsWith("08")) connection = true;
                    if ("28000".equals(normalized)) authentication = true;
                    if ("HYT00".equals(normalized) || "HYT01".equals(normalized) || "57014".equals(normalized)) timeout = true;
                    if (normalized.startsWith("22") || normalized.startsWith("23")
                            || normalized.startsWith("42") || normalized.startsWith("54")) query = true;
                }
            }
        }
        if (userDisabled) return "USER_DISABLED";
        if (authentication) return "AUTHENTICATION_FAILED";
        if (tls) return "TLS_FAILED";
        if (timeout) return "QUERY_TIMEOUT";
        if (connection) return "CONNECTION_FAILED";
        if (query) return "QUERY_FAILED";
        return "UNKNOWN_SQL";
    }

    private static boolean isUserDisabled(Throwable error) {
        if (error instanceof ReturnCodeException security) {
            int code = security.getReturnCode();
            if (code == AS400SecurityException.USERID_DISABLE
                    || code == AS400SecurityException.PASSWORD_INCORRECT_USERID_DISABLE) return true;
        }
        String compact = compactDiagnostic(error);
        return compact.contains("useriddisabled") || compact.contains("useridisdisabled")
                || compact.contains("userid_disable") || compact.contains("passwordincorrectuseriddisable");
    }

    private static boolean isAuthenticationFailure(Throwable item) {
        if (item instanceof ReturnCodeException security) {
            int code = security.getReturnCode();
            if (code == AS400SecurityException.PASSWORD_INCORRECT
                    || code == AS400SecurityException.PASSWORD_ERROR
                    || code == AS400SecurityException.PASSWORD_EXPIRED
                    || code == AS400SecurityException.PASSWORD_NOT_SET
                    || code == AS400SecurityException.USERID_UNKNOWN
                    || code == AS400SecurityException.USERID_ERROR
                    || code == AS400SecurityException.USERID_NOT_SET) return true;
        }
        if (item.getClass().getSimpleName().contains("Authorization")) return true;
        String compact = compactDiagnostic(item);
        return compact.contains("passwordincorrect") || compact.contains("passwordisincorrect")
                || compact.contains("useridunknown");
    }

    private static boolean containsTls(Throwable error) {
        for (Throwable item : exceptionChain(error)) if (isTlsType(item)) return true;
        return false;
    }

    private static boolean isTlsTrustFailure(Throwable error) {
        for (Throwable item : exceptionChain(error)) {
            if (!(item instanceof IllegalArgumentException) && !(item instanceof IllegalStateException)) {
                continue;
            }
            for (StackTraceElement frame : item.getStackTrace()) {
                if ("io.quadringent.as400.TlsTrust".equals(frame.getClassName())) {
                    return true;
                }
            }
        }
        return false;
    }

    private static boolean isTlsType(Throwable item) {
        String name = item.getClass().getName();
        String simple = item.getClass().getSimpleName();
        return name.startsWith("javax.net.ssl.") || name.startsWith("sun.security.ssl.")
                || simple.contains("SSL") || simple.contains("Tls") || "CertificateException".equals(simple);
    }

    private static String compactDiagnostic(Throwable item) {
        StringBuilder builder = new StringBuilder(item.getClass().getSimpleName());
        if (item instanceof SQLException sql && sql.getSQLState() != null) builder.append(sql.getSQLState());
        String message = item.getMessage();
        if (message != null) builder.append(message);
        StringBuilder compact = new StringBuilder(builder.length());
        for (int index = 0; index < builder.length(); index++) {
            char character = builder.charAt(index);
            if (character >= 'A' && character <= 'Z') compact.append((char) (character + 32));
            else if ((character >= 'a' && character <= 'z') || (character >= '0' && character <= '9')) compact.append(character);
        }
        return compact.toString();
    }

    private static SQLException firstSqlException(Throwable error) {
        for (Throwable item : exceptionChain(error)) if (item instanceof SQLException sql) return sql;
        return null;
    }

    private static String firstSqlState(SQLException error) {
        for (Throwable item : exceptionChain(error)) {
            if (item instanceof SQLException sql && validSqlState(sql.getSQLState())) {
                return sql.getSQLState().toUpperCase(Locale.ROOT);
            }
        }
        return null;
    }

    private static Integer firstVendorCode(SQLException error) {
        for (Throwable item : exceptionChain(error)) {
            if (item instanceof SQLException sql) {
                Integer vendor = boundedVendorCode(sql.getErrorCode());
                if (vendor != null) return vendor;
            }
            if (item instanceof ReturnCodeException security) {
                Integer vendor = boundedVendorCode(security.getReturnCode());
                if (vendor != null) return vendor;
            }
        }
        return null;
    }

    private static boolean validSqlState(String state) {
        return state != null && SQLSTATE.matcher(state).matches();
    }

    private static Integer boundedVendorCode(int code) {
        if (code == 0 || code < VENDOR_CODE_MIN || code > VENDOR_CODE_MAX) return null;
        return code;
    }

    private static List<Throwable> exceptionChain(Throwable error) {
        ArrayDeque<Throwable> queue = new ArrayDeque<>();
        IdentityHashMap<Throwable, Boolean> seen = new IdentityHashMap<>();
        List<Throwable> chain = new ArrayList<>();
        if (error != null) queue.add(error);
        while (!queue.isEmpty()) {
            Throwable item = queue.removeFirst();
            if (seen.put(item, Boolean.TRUE) != null) continue;
            chain.add(item);
            if (item.getCause() != null) queue.add(item.getCause());
            for (Throwable suppressed : item.getSuppressed()) queue.add(suppressed);
            if (item instanceof SQLException sql && sql.getNextException() != null) queue.add(sql.getNextException());
        }
        return chain;
    }

    static int catalogQueryTimeoutSeconds() {
        String raw = System.getenv("AS400_CATALOG_QUERY_TIMEOUT_SECONDS");
        int seconds = raw == null || raw.isBlank() ? 10 : Integer.parseInt(raw.trim());
        if (seconds < 1 || seconds >= 30) {
            throw new IllegalArgumentException("AS400_CATALOG_QUERY_TIMEOUT_SECONDS must be in [1, 29]");
        }
        return seconds;
    }

    static CatalogDocument loadCatalog(Connection connection, Instant observedAt, CatalogScope scope)
            throws SQLException {
        Map<String, TableAcc> tables = loadTableStats(connection, scope);
        loadJournals(connection, tables, scope);
        loadColumns(connection, tables, scope);
        loadConstraints(connection, tables, scope);
        loadIndexes(connection, tables, scope);
        List<TableRecord> ordered = new ArrayList<>(scope.tables().size());
        for (String name : scope.tables()) {
            TableAcc acc = tables.get(name);
            if (acc == null) {
                throw new ProbeFailure("MISSING_TABLE");
            }
            ordered.add(acc.toRecord());
        }
        List<JournalChain> journals = loadJournalChains(connection, ordered);
        return new CatalogDocument(
                FORMAT_VERSION,
                utc(observedAt),
                scope.environment(),
                scope.sourceSchema(),
                journals,
                ordered);
    }

    private static Map<String, TableAcc> loadTableStats(Connection connection, CatalogScope scope)
            throws SQLException {
        String sql = """
                SELECT TABLE_NAME, NUMBER_ROWS, DATA_SIZE, NUMBER_PARTITIONS
                  FROM QSYS2.SYSTABLESTAT
                 WHERE TABLE_SCHEMA = ?
                   AND TABLE_NAME IN (%s)
                 FETCH FIRST %d ROWS ONLY
                """.formatted(inList(scope.tables().size()), METADATA_FETCH_LIMIT);
        Map<String, TableAcc> tables = new LinkedHashMap<>();
        query(connection, sql, schemaAndTables(scope), rs -> {
            String name = requireIdentifier(rs.getString("TABLE_NAME")).toUpperCase(Locale.ROOT);
            if (tables.containsKey(name)) {
                throw new ProbeFailure("DUPLICATE_METADATA");
            }
            TableAcc acc = new TableAcc();
            acc.name = name;
            acc.rowCount = requiredCount(rs, "NUMBER_ROWS");
            acc.dataSize = requiredCount(rs, "DATA_SIZE");
            acc.memberCount = requiredCount(rs, "NUMBER_PARTITIONS");
            tables.put(name, acc);
        });
        requireAllowlist(tables.keySet(), scope.tables());
        return tables;
    }

    private static void loadJournals(
            Connection connection, Map<String, TableAcc> tables, CatalogScope scope) throws SQLException {
        String sql = """
                SELECT OBJECT_NAME, JOURNAL_LIBRARY, JOURNAL_NAME, JOURNAL_IMAGES
                  FROM QSYS2.JOURNALED_OBJECTS
                 WHERE OBJECT_LIBRARY = ?
                   AND OBJECT_TYPE = '*FILE'
                   AND OBJECT_NAME IN (%s)
                 FETCH FIRST %d ROWS ONLY
                """.formatted(inList(scope.tables().size()), METADATA_FETCH_LIMIT);
        Set<String> seen = new LinkedHashSet<>();
        query(connection, sql, schemaAndTables(scope), rs -> {
            String name = requireIdentifier(rs.getString("OBJECT_NAME")).toUpperCase(Locale.ROOT);
            if (!scope.tables().contains(name)) {
                return;
            }
            if (!seen.add(name)) {
                throw new ProbeFailure("DUPLICATE_METADATA");
            }
            TableAcc acc = tables.get(name);
            if (acc == null) {
                throw new ProbeFailure("MISSING_TABLE");
            }
            acc.journalLibrary = requireIdentifier(rs.getString("JOURNAL_LIBRARY")).toUpperCase(Locale.ROOT);
            acc.journalName = requireIdentifier(rs.getString("JOURNAL_NAME")).toUpperCase(Locale.ROOT);
            acc.journalImages = requireJournalImages(rs.getString("JOURNAL_IMAGES"));
        });
        for (String name : scope.tables()) {
            TableAcc acc = tables.get(name);
            if (acc == null || acc.journalLibrary == null || acc.journalName == null) {
                throw new ProbeFailure("NOT_JOURNALED");
            }
        }
    }

    private static void loadColumns(
            Connection connection, Map<String, TableAcc> tables, CatalogScope scope) throws SQLException {
        String sql = """
                SELECT TABLE_NAME,
                       COLUMN_NAME,
                       DATA_TYPE,
                       LENGTH,
                       NUMERIC_PRECISION,
                       NUMERIC_SCALE,
                       CCSID,
                       IS_NULLABLE,
                       ORDINAL_POSITION
                  FROM QSYS2.SYSCOLUMNS
                 WHERE TABLE_SCHEMA = ?
                   AND TABLE_NAME IN (%s)
                 ORDER BY TABLE_NAME, ORDINAL_POSITION
                 FETCH FIRST %d ROWS ONLY
                """.formatted(inList(scope.tables().size()), METADATA_FETCH_LIMIT);
        query(connection, sql, schemaAndTables(scope), rs -> {
            TableAcc acc = requiredTable(tables, rs.getString("TABLE_NAME"));
            int ordinal = requiredInt(rs, "ORDINAL_POSITION");
            String column = requireIdentifier(rs.getString("COLUMN_NAME")).toUpperCase(Locale.ROOT);
            if (acc.columnNames.contains(column) || acc.columnOrdinals.contains(ordinal)) {
                throw new ProbeFailure("DUPLICATE_METADATA");
            }
            acc.columnNames.add(column);
            acc.columnOrdinals.add(ordinal);
            acc.columns.add(new ColumnRecord(
                    column,
                    requireTypeName(rs.getString("DATA_TYPE")),
                    optionalLong(rs, "LENGTH"),
                    optionalLong(rs, "NUMERIC_PRECISION"),
                    optionalLong(rs, "NUMERIC_SCALE"),
                    optionalLong(rs, "CCSID"),
                    optionalNullable(rs.getString("IS_NULLABLE")),
                    ordinal));
        });
        for (TableAcc acc : tables.values()) {
            if (acc.columns.isEmpty()) {
                throw new ProbeFailure("MISSING_TABLE");
            }
        }
    }

    private static void loadConstraints(
            Connection connection, Map<String, TableAcc> tables, CatalogScope scope) throws SQLException {
        String cstSql = """
                SELECT TABLE_NAME, CONSTRAINT_SCHEMA, CONSTRAINT_NAME, CONSTRAINT_TYPE
                  FROM QSYS2.SYSCST
                 WHERE TABLE_SCHEMA = ?
                   AND TABLE_NAME IN (%s)
                 FETCH FIRST %d ROWS ONLY
                """.formatted(inList(scope.tables().size()), METADATA_FETCH_LIMIT);
        query(connection, cstSql, schemaAndTables(scope), rs -> {
            TableAcc acc = requiredTable(tables, rs.getString("TABLE_NAME"));
            String schema = requireIdentifier(rs.getString("CONSTRAINT_SCHEMA")).toUpperCase(Locale.ROOT);
            String name = requireIdentifier(rs.getString("CONSTRAINT_NAME")).toUpperCase(Locale.ROOT);
            String key = schema + "." + name;
            if (acc.constraints.containsKey(key)) {
                throw new ProbeFailure("DUPLICATE_METADATA");
            }
            ConstraintAcc constraint = new ConstraintAcc();
            constraint.schema = schema;
            constraint.name = name;
            constraint.type = requireConstraintType(rs.getString("CONSTRAINT_TYPE"));
            acc.constraints.put(key, constraint);
        });
        String colSql = """
                SELECT TABLE_NAME, CONSTRAINT_SCHEMA, CONSTRAINT_NAME, COLUMN_NAME, ORDINAL_POSITION
                  FROM QSYS2.SYSKEYCST
                 WHERE TABLE_SCHEMA = ?
                   AND TABLE_NAME IN (%s)
                 ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION
                 FETCH FIRST %d ROWS ONLY
                """.formatted(inList(scope.tables().size()), METADATA_FETCH_LIMIT);
        query(connection, colSql, schemaAndTables(scope), rs -> {
            TableAcc acc = requiredTable(tables, rs.getString("TABLE_NAME"));
            String key = requireIdentifier(rs.getString("CONSTRAINT_SCHEMA")).toUpperCase(Locale.ROOT)
                    + "."
                    + requireIdentifier(rs.getString("CONSTRAINT_NAME")).toUpperCase(Locale.ROOT);
            ConstraintAcc constraint = acc.constraints.get(key);
            if (constraint == null) {
                return;
            }
            String column = requireIdentifier(rs.getString("COLUMN_NAME")).toUpperCase(Locale.ROOT);
            int ordinal = requiredInt(rs, "ORDINAL_POSITION");
            if (constraint.columnNames.contains(column) || constraint.ordinals.contains(ordinal)) {
                throw new ProbeFailure("DUPLICATE_METADATA");
            }
            constraint.columnNames.add(column);
            constraint.ordinals.add(ordinal);
            constraint.columns.add(column);
        });
    }

    private static void loadIndexes(
            Connection connection, Map<String, TableAcc> tables, CatalogScope scope) throws SQLException {
        String sqlIndexSql = """
                SELECT TABLE_NAME, INDEX_SCHEMA, INDEX_NAME, IS_UNIQUE, INDEX_HAS_SEARCH_CONDITION
                  FROM QSYS2.SYSINDEXES
                 WHERE TABLE_SCHEMA = ?
                   AND TABLE_NAME IN (%s)
                 FETCH FIRST %d ROWS ONLY
                """.formatted(inList(scope.tables().size()), METADATA_FETCH_LIMIT);
        query(connection, sqlIndexSql, schemaAndTables(scope), rs -> {
            TableAcc acc = requiredTable(tables, rs.getString("TABLE_NAME"));
            String schema = requireIdentifier(rs.getString("INDEX_SCHEMA")).toUpperCase(Locale.ROOT);
            String name = requireIdentifier(rs.getString("INDEX_NAME")).toUpperCase(Locale.ROOT);
            String key = schema + "." + name;
            boolean unique = sqlUnique(rs.getString("IS_UNIQUE"));
            boolean sparse = searchCondition(rs.getString("INDEX_HAS_SEARCH_CONDITION"));
            IndexAcc existing = acc.indexes.get(key);
            if (existing == null) {
                IndexAcc index = new IndexAcc();
                index.schema = schema;
                index.name = name;
                index.unique = unique;
                index.sparse = sparse;
                index.selectOmit = null;
                acc.indexes.put(key, index);
                return;
            }
            if (existing.unique != unique || existing.sparse != sparse) {
                throw new ProbeFailure("DUPLICATE_METADATA");
            }
        });
        String keysSql = """
                SELECT i.TABLE_NAME, k.INDEX_SCHEMA, k.INDEX_NAME, k.COLUMN_NAME, k.ORDINAL_POSITION, k.ORDERING
                  FROM QSYS2.SYSKEYS k
                  INNER JOIN QSYS2.SYSINDEXES i
                          ON i.INDEX_SCHEMA = k.INDEX_SCHEMA
                         AND i.INDEX_NAME = k.INDEX_NAME
                 WHERE i.TABLE_SCHEMA = ?
                   AND i.TABLE_NAME IN (%s)
                 ORDER BY i.TABLE_NAME, k.INDEX_SCHEMA, k.INDEX_NAME, k.ORDINAL_POSITION
                 FETCH FIRST %d ROWS ONLY
                """.formatted(inList(scope.tables().size()), METADATA_FETCH_LIMIT);
        query(connection, keysSql, schemaAndTables(scope), rs -> {
            TableAcc acc = requiredTable(tables, rs.getString("TABLE_NAME"));
            String key = requireIdentifier(rs.getString("INDEX_SCHEMA")).toUpperCase(Locale.ROOT)
                    + "."
                    + requireIdentifier(rs.getString("INDEX_NAME")).toUpperCase(Locale.ROOT);
            IndexAcc index = acc.indexes.get(key);
            if (index == null) {
                return;
            }
            String column = requireIdentifier(rs.getString("COLUMN_NAME")).toUpperCase(Locale.ROOT);
            int ordinal = requiredInt(rs, "ORDINAL_POSITION");
            if (index.columnNames.contains(column) || index.ordinals.contains(ordinal)) {
                throw new ProbeFailure("DUPLICATE_METADATA");
            }
            index.columnNames.add(column);
            index.ordinals.add(ordinal);
            index.columns.add(new IndexColumnRecord(column, ordinal, requireOrdering(rs.getString("ORDERING"))));
        });
    }

    private static List<JournalChain> loadJournalChains(Connection connection, List<TableRecord> tables)
            throws SQLException {
        ReceiverLinkColumns previousColumns = receiverLinkColumns(connection);
        List<JournalChain> chains = new ArrayList<>();
        for (JournalIdentity identity : uniqueJournalsInTableOrder(tables)) {
            chains.add(loadReceivers(connection, identity, previousColumns));
        }
        return chains;
    }

    /** Colonnes de chaînage receivers exposées par la vue, si disponibles. */
    record ReceiverLinkColumns(String library, String name) {}

    /**
     * IBM i 7.5 expose {@code PREVIOUS_JOURNAL_RECEIVER[_LIBRARY]} ; certaines
     * versions utilisent {@code PREVIOUS_RECEIVER_*}. Aucune paire n'est un
     * contrat stable ({@code ReadOnlyReceiverCatalog} et {@code ibmi_reader}
     * ne les sélectionnent pas) : on sonde SYSCOLUMNS et on renonce aux
     * colonnes quand la vue ne les expose pas.
     */
    static ReceiverLinkColumns receiverLinkColumns(Connection connection) throws SQLException {
        String sql = """
                SELECT COLUMN_NAME
                  FROM QSYS2.SYSCOLUMNS
                 WHERE TABLE_SCHEMA = ?
                   AND TABLE_NAME = ?
                   AND COLUMN_NAME IN (?, ?, ?, ?)
                 FETCH FIRST 10 ROWS ONLY
                """;
        Set<String> found = new LinkedHashSet<>();
        query(
                connection,
                sql,
                List.of(
                        "QSYS2",
                        "JOURNAL_RECEIVER_INFO",
                        "PREVIOUS_JOURNAL_RECEIVER_LIBRARY",
                        "PREVIOUS_JOURNAL_RECEIVER",
                        "PREVIOUS_RECEIVER_LIBRARY",
                        "PREVIOUS_RECEIVER_NAME"),
                rs -> found.add(requireIdentifier(rs.getString("COLUMN_NAME")).toUpperCase(Locale.ROOT)));
        if (found.contains("PREVIOUS_JOURNAL_RECEIVER_LIBRARY")
                && found.contains("PREVIOUS_JOURNAL_RECEIVER")) {
            return new ReceiverLinkColumns(
                    "PREVIOUS_JOURNAL_RECEIVER_LIBRARY", "PREVIOUS_JOURNAL_RECEIVER");
        }
        if (found.contains("PREVIOUS_RECEIVER_LIBRARY") && found.contains("PREVIOUS_RECEIVER_NAME")) {
            return new ReceiverLinkColumns("PREVIOUS_RECEIVER_LIBRARY", "PREVIOUS_RECEIVER_NAME");
        }
        return null;
    }

    static String journalReceiverInfoSql(ReceiverLinkColumns previousColumns) {
        StringBuilder sql = new StringBuilder();
        sql.append("""
                SELECT JOURNAL_RECEIVER_LIBRARY,
                       JOURNAL_RECEIVER_NAME,
                       STATUS,
                       FIRST_SEQUENCE_NUMBER,
                       LAST_SEQUENCE_NUMBER,
                       ATTACH_TIMESTAMP,
                       DETACH_TIMESTAMP
                """);
        if (previousColumns != null) {
            sql.append("""
                       ,
                       %s,
                       %s
                    """.formatted(previousColumns.library(), previousColumns.name()));
        }
        sql.append("""
                  FROM QSYS2.JOURNAL_RECEIVER_INFO
                 WHERE JOURNAL_LIBRARY = ?
                   AND JOURNAL_NAME = ?
                 ORDER BY ATTACH_TIMESTAMP DESC
                 FETCH FIRST %d ROWS ONLY
                """.formatted(RECEIVER_FETCH_LIMIT));
        return sql.toString();
    }

    private static JournalChain loadReceivers(
            Connection connection,
            JournalIdentity journal,
            ReceiverLinkColumns previousColumns) throws SQLException {
        List<ReceiverRecord> newestFirst = new ArrayList<>();
        boolean[] truncated = {false};
        query(connection, journalReceiverInfoSql(previousColumns), List.of(journal.library(), journal.name()), rs -> {
            if (newestFirst.size() >= RECEIVER_FETCH_LIMIT - 1) {
                truncated[0] = true;
                return;
            }
            newestFirst.add(new ReceiverRecord(
                    requireIdentifier(rs.getString("JOURNAL_RECEIVER_LIBRARY")).toUpperCase(Locale.ROOT),
                    requireIdentifier(rs.getString("JOURNAL_RECEIVER_NAME")).toUpperCase(Locale.ROOT),
                    optionalToken(rs.getString("STATUS")),
                    optionalSequence(rs, "FIRST_SEQUENCE_NUMBER"),
                    optionalSequence(rs, "LAST_SEQUENCE_NUMBER"),
                    optionalTimestamp(rs.getString("ATTACH_TIMESTAMP")),
                    optionalTimestamp(rs.getString("DETACH_TIMESTAMP")),
                    previousColumns != null ? optionalIdentifier(rs.getString(previousColumns.library())) : null,
                    previousColumns != null ? optionalIdentifier(rs.getString(previousColumns.name())) : null));
        });
        List<ReceiverRecord> oldestFirst = new ArrayList<>(newestFirst.size());
        for (int index = newestFirst.size() - 1; index >= 0; index--) {
            oldestFirst.add(newestFirst.get(index));
        }
        String continuity = assessReceiverChain(oldestFirst);
        if (truncated[0] && CONTINUITY_PROVEN.equals(continuity)) {
            continuity = CONTINUITY_UNCERTAIN;
        }
        return new JournalChain(journal.library(), journal.name(), continuity, oldestFirst);
    }

    static String closedJson(CatalogDocument document, CatalogScope scope) {
        validateCatalog(document, scope);
        String json = serializeCatalog(document);
        assertSafeCatalogJson(json);
        return json;
    }

    static void validateCatalog(CatalogDocument document, CatalogScope scope) {
        if (document == null) {
            throw new ProbeFailure("MISSING_TABLE");
        }
        if (!FORMAT_VERSION.equals(document.formatVersion())) {
            throw new ProbeFailure("UNSAFE_IDENTIFIER");
        }
        if (!scope.environment().equals(document.environment())) {
            throw new ProbeFailure("UNSAFE_IDENTIFIER");
        }
        if (!scope.sourceSchema().equals(requireIdentifier(document.sourceSchema()))) {
            throw new ProbeFailure("UNSAFE_IDENTIFIER");
        }
        requireAllowlist(tableNames(document.tables()), scope.tables());
        requireAllowlistOrder(tableNames(document.tables()), scope.tables());
        List<JournalIdentity> expectedJournals = uniqueJournalsInTableOrder(document.tables());
        if (document.journals() == null || document.journals().size() != expectedJournals.size()) {
            throw new ProbeFailure("NOT_JOURNALED");
        }
        Set<String> journalKeys = new LinkedHashSet<>();
        for (int index = 0; index < expectedJournals.size(); index++) {
            JournalChain chain = document.journals().get(index);
            JournalIdentity expected = expectedJournals.get(index);
            if (chain == null) {
                throw new ProbeFailure("NOT_JOURNALED");
            }
            JournalIdentity actual = new JournalIdentity(
                    requireIdentifier(chain.library()).toUpperCase(Locale.ROOT),
                    requireIdentifier(chain.name()).toUpperCase(Locale.ROOT));
            if (!expected.equals(actual)) {
                throw new ProbeFailure("NOT_JOURNALED");
            }
            String key = actual.library() + "." + actual.name();
            if (!journalKeys.add(key)) {
                throw new ProbeFailure("DUPLICATE_METADATA");
            }
            String continuity = assessReceiverChain(chain.receivers());
            if (!CONTINUITY_PROVEN.equals(chain.continuity())
                    && !CONTINUITY_UNCERTAIN.equals(chain.continuity())) {
                throw new ProbeFailure("UNSAFE_IDENTIFIER");
            }
            if (CONTINUITY_PROVEN.equals(chain.continuity()) && CONTINUITY_UNCERTAIN.equals(continuity)) {
                throw new ProbeFailure("RECEIVER_DISCONTINUITY");
            }
        }
        Set<String> tableSeen = new LinkedHashSet<>();
        for (TableRecord table : document.tables()) {
            if (!tableSeen.add(table.name())) {
                throw new ProbeFailure("DUPLICATE_METADATA");
            }
            requireIdentifier(table.name());
            requireIdentifier(table.journalLibrary());
            requireIdentifier(table.journalName());
            requireJournalImages(table.journalImages());
            if (table.rowCount() < 0 || table.dataSize() < 0 || table.memberCount() < 0) {
                throw new ProbeFailure("ROW_COUNT_UNAVAILABLE");
            }
            if (table.columns() == null || table.columns().isEmpty()) {
                throw new ProbeFailure("MISSING_TABLE");
            }
            Set<String> columns = new LinkedHashSet<>();
            Set<Integer> ordinals = new LinkedHashSet<>();
            for (ColumnRecord column : table.columns()) {
                requireIdentifier(column.name());
                requireTypeName(column.type());
                if (column.length() != null && column.length() < 0) {
                    throw new ProbeFailure("UNSAFE_IDENTIFIER");
                }
                if (column.ordinal() < 1) {
                    throw new ProbeFailure("UNSAFE_IDENTIFIER");
                }
                if (!columns.add(column.name()) || !ordinals.add(column.ordinal())) {
                    throw new ProbeFailure("DUPLICATE_METADATA");
                }
            }
            Set<String> constraints = new LinkedHashSet<>();
            if (table.constraints() != null) {
                for (ConstraintRecord constraint : table.constraints()) {
                    requireIdentifier(constraint.schema());
                    requireIdentifier(constraint.name());
                    requireConstraintType(constraint.type());
                    String key = constraint.schema() + "." + constraint.name();
                    if (!constraints.add(key)) {
                        throw new ProbeFailure("DUPLICATE_METADATA");
                    }
                    if (constraint.columns() != null) {
                        Set<String> constraintColumns = new LinkedHashSet<>();
                        for (String column : constraint.columns()) {
                            requireIdentifier(column);
                            if (!constraintColumns.add(column)) {
                                throw new ProbeFailure("DUPLICATE_METADATA");
                            }
                        }
                    }
                }
            }
            Set<String> indexes = new LinkedHashSet<>();
            if (table.indexes() != null) {
                for (IndexRecord index : table.indexes()) {
                    requireIdentifier(index.schema());
                    requireIdentifier(index.name());
                    String key = index.schema() + "." + index.name();
                    if (!indexes.add(key)) {
                        throw new ProbeFailure("DUPLICATE_METADATA");
                    }
                    Set<String> indexColumns = new LinkedHashSet<>();
                    Set<Integer> indexOrdinals = new LinkedHashSet<>();
                    if (index.columns() != null) {
                        for (IndexColumnRecord column : index.columns()) {
                            requireIdentifier(column.name());
                            requireOrdering(column.ordering());
                            if (column.ordinal() < 1) {
                                throw new ProbeFailure("UNSAFE_IDENTIFIER");
                            }
                            if (!indexColumns.add(column.name()) || !indexOrdinals.add(column.ordinal())) {
                                throw new ProbeFailure("DUPLICATE_METADATA");
                            }
                        }
                    }
                }
            }
        }
    }

    static List<String> requireAllowlistOrder(List<String> tables, List<String> allowed) {
        if (tables == null || tables.size() != allowed.size()) {
            throw new ProbeFailure("MISSING_TABLE");
        }
        for (int index = 0; index < allowed.size(); index++) {
            String name = requireIdentifier(tables.get(index)).toUpperCase(Locale.ROOT);
            if (!allowed.get(index).equals(name)) {
                throw new ProbeFailure("MISSING_TABLE");
            }
        }
        return List.copyOf(allowed);
    }

    static Set<String> requireAllowlist(Iterable<String> present, List<String> allowed) {
        LinkedHashSet<String> names = new LinkedHashSet<>();
        if (present != null) {
            for (String raw : present) {
                String name = requireIdentifier(raw).toUpperCase(Locale.ROOT);
                if (!allowed.contains(name)) {
                    throw new ProbeFailure("UNSAFE_IDENTIFIER");
                }
                if (!names.add(name)) {
                    throw new ProbeFailure("DUPLICATE_METADATA");
                }
            }
        }
        if (names.size() != allowed.size()) {
            throw new ProbeFailure("MISSING_TABLE");
        }
        return Set.copyOf(names);
    }

    static List<JournalIdentity> uniqueJournalsInTableOrder(List<TableRecord> tables) {
        LinkedHashMap<String, JournalIdentity> unique = new LinkedHashMap<>();
        if (tables == null) {
            throw new ProbeFailure("MISSING_TABLE");
        }
        for (TableRecord table : tables) {
            if (table == null || table.journalLibrary() == null || table.journalName() == null) {
                throw new ProbeFailure("NOT_JOURNALED");
            }
            JournalIdentity identity = new JournalIdentity(
                    requireIdentifier(table.journalLibrary()).toUpperCase(Locale.ROOT),
                    requireIdentifier(table.journalName()).toUpperCase(Locale.ROOT));
            unique.putIfAbsent(identity.library() + "." + identity.name(), identity);
        }
        return List.copyOf(unique.values());
    }

    /**
     * Validates one journal's receiver list in attach order (oldest first).
     *
     * <p>PREVIOUS_* is used only when IBM i actually returns it. Sequence bounds
     * may be null or overlap; that is uncertainty, not a fabricated gap. An empty
     * chain or a PREVIOUS identity that contradicts attach order is incoherent.
     */
    static String assessReceiverChain(List<ReceiverRecord> receivers) {
        if (receivers == null || receivers.isEmpty()) {
            throw new ProbeFailure("RECEIVER_DISCONTINUITY");
        }
        boolean linked = true;
        Set<String> identities = new LinkedHashSet<>();
        ReceiverRecord previous = null;
        for (ReceiverRecord receiver : receivers) {
            if (receiver == null) {
                throw new ProbeFailure("RECEIVER_DISCONTINUITY");
            }
            String library = requireIdentifier(receiver.library()).toUpperCase(Locale.ROOT);
            String name = requireIdentifier(receiver.name()).toUpperCase(Locale.ROOT);
            if (!identities.add(library + "." + name)) {
                throw new ProbeFailure("DUPLICATE_METADATA");
            }
            BigInteger first = parseOptionalSequence(receiver.firstSequence());
            BigInteger last = parseOptionalSequence(receiver.lastSequence());
            if (first == null || last == null || last.compareTo(first) < 0) {
                linked = false;
            }
            if (receiver.attachTimestamp() == null || receiver.attachTimestamp().isBlank()) {
                linked = false;
            }
            else {
                optionalTimestamp(receiver.attachTimestamp());
            }
            if (receiver.detachTimestamp() != null) {
                optionalTimestamp(receiver.detachTimestamp());
            }
            if (receiver.status() != null) {
                optionalToken(receiver.status());
            }
            if (previous != null) {
                if (receiver.previousName() == null) {
                    linked = false;
                }
                else {
                    if (!requireIdentifier(receiver.previousName()).equalsIgnoreCase(previous.name())) {
                        throw new ProbeFailure("RECEIVER_DISCONTINUITY");
                    }
                    if (receiver.previousLibrary() != null
                            && !requireIdentifier(receiver.previousLibrary()).equalsIgnoreCase(previous.library())) {
                        throw new ProbeFailure("RECEIVER_DISCONTINUITY");
                    }
                }
            }
            previous = receiver;
        }
        return linked ? CONTINUITY_PROVEN : CONTINUITY_UNCERTAIN;
    }

    static String requireIdentifier(String value) {
        if (value == null || value.isBlank()) {
            throw new ProbeFailure("UNSAFE_IDENTIFIER");
        }
        String trimmed = value.trim();
        if (!IDENTIFIER.matcher(trimmed).matches()) {
            throw new ProbeFailure("UNSAFE_IDENTIFIER");
        }
        return trimmed;
    }

    static String serializeCatalog(CatalogDocument document) {
        Map<String, Object> root = new LinkedHashMap<>();
        root.put("format_version", document.formatVersion());
        root.put("observed_at", document.observedAt());
        root.put("environment", document.environment());
        root.put("source_schema", document.sourceSchema());
        List<Object> journals = new ArrayList<>();
        for (JournalChain chain : document.journals()) {
            Map<String, Object> journal = new LinkedHashMap<>();
            journal.put("library", chain.library());
            journal.put("name", chain.name());
            journal.put("continuity", chain.continuity());
            List<Object> receivers = new ArrayList<>();
            for (ReceiverRecord receiver : chain.receivers()) {
                Map<String, Object> item = new LinkedHashMap<>();
                item.put("library", receiver.library());
                item.put("name", receiver.name());
                item.put("status", receiver.status());
                item.put("first_sequence", receiver.firstSequence());
                item.put("last_sequence", receiver.lastSequence());
                item.put("attach_timestamp", receiver.attachTimestamp());
                item.put("detach_timestamp", receiver.detachTimestamp());
                item.put("previous_library", receiver.previousLibrary());
                item.put("previous_name", receiver.previousName());
                receivers.add(item);
            }
            journal.put("receivers", receivers);
            journals.add(journal);
        }
        root.put("journals", journals);
        List<Object> tables = new ArrayList<>();
        for (TableRecord table : document.tables()) {
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("name", table.name());
            item.put("row_count", table.rowCount());
            item.put("data_size", table.dataSize());
            item.put("member_count", table.memberCount());
            item.put("journal_library", table.journalLibrary());
            item.put("journal_name", table.journalName());
            item.put("journal_images", table.journalImages());
            List<Object> columns = new ArrayList<>();
            for (ColumnRecord column : table.columns()) {
                Map<String, Object> columnJson = new LinkedHashMap<>();
                columnJson.put("name", column.name());
                columnJson.put("type", column.type());
                columnJson.put("length", column.length());
                columnJson.put("numeric_precision", column.numericPrecision());
                columnJson.put("numeric_scale", column.numericScale());
                columnJson.put("ccsid", column.ccsid());
                columnJson.put("nullable", column.nullable());
                columnJson.put("ordinal", column.ordinal());
                columns.add(columnJson);
            }
            item.put("columns", columns);
            List<Object> constraints = new ArrayList<>();
            if (table.constraints() != null) {
                for (ConstraintRecord constraint : table.constraints()) {
                    Map<String, Object> constraintJson = new LinkedHashMap<>();
                    constraintJson.put("schema", constraint.schema());
                    constraintJson.put("name", constraint.name());
                    constraintJson.put("type", constraint.type());
                    constraintJson.put("columns", List.copyOf(constraint.columns() == null ? List.of() : constraint.columns()));
                    constraints.add(constraintJson);
                }
            }
            item.put("constraints", constraints);
            List<Object> indexes = new ArrayList<>();
            if (table.indexes() != null) {
                for (IndexRecord index : table.indexes()) {
                    Map<String, Object> indexJson = new LinkedHashMap<>();
                    indexJson.put("schema", index.schema());
                    indexJson.put("name", index.name());
                    indexJson.put("unique", index.unique());
                    indexJson.put("sparse", index.sparse());
                    indexJson.put("select_omit", index.selectOmit());
                    List<Object> indexColumns = new ArrayList<>();
                    if (index.columns() != null) {
                        for (IndexColumnRecord column : index.columns()) {
                            Map<String, Object> columnJson = new LinkedHashMap<>();
                            columnJson.put("name", column.name());
                            columnJson.put("ordinal", column.ordinal());
                            columnJson.put("ordering", column.ordering());
                            indexColumns.add(columnJson);
                        }
                    }
                    indexJson.put("columns", indexColumns);
                    indexes.add(indexJson);
                }
            }
            item.put("indexes", indexes);
            tables.add(item);
        }
        root.put("tables", tables);
        return writeJson(root);
    }

    static void assertSafeCatalogJson(String json) {
        if (json == null || json.isBlank()) {
            throw new ProbeFailure("UNSAFE_IDENTIFIER");
        }
        String compact = json.toLowerCase(Locale.ROOT);
        for (String key : FORBIDDEN_JSON_KEYS) {
            if (compact.contains("\"" + key + "\"")) {
                throw new ProbeFailure("UNSAFE_IDENTIFIER");
            }
        }
        if (compact.contains("entry_data")
                || compact.contains("stacktrace")
                || compact.contains("jdbc:as400")
                || compact.contains("select *")) {
            throw new ProbeFailure("UNSAFE_IDENTIFIER");
        }
    }

    static String utc(Instant instant) {
        return instant.atOffset(ZoneOffset.UTC).truncatedTo(ChronoUnit.SECONDS).toString();
    }

    private static List<String> tableNames(List<TableRecord> tables) {
        List<String> names = new ArrayList<>();
        if (tables == null) {
            return names;
        }
        for (TableRecord table : tables) {
            names.add(table.name());
        }
        return names;
    }

    private static TableAcc requiredTable(Map<String, TableAcc> tables, String rawName) {
        String name = requireIdentifier(rawName).toUpperCase(Locale.ROOT);
        TableAcc acc = tables.get(name);
        if (acc == null) {
            throw new ProbeFailure("MISSING_TABLE");
        }
        return acc;
    }

    private static List<String> schemaAndTables(CatalogScope scope) {
        List<String> binds = new ArrayList<>(1 + scope.tables().size());
        binds.add(scope.sourceSchema());
        binds.addAll(scope.tables());
        return binds;
    }

    private static String inList(int count) {
        StringBuilder builder = new StringBuilder();
        for (int index = 0; index < count; index++) {
            if (index > 0) {
                builder.append(", ");
            }
            builder.append('?');
        }
        return builder.toString();
    }

    private static void query(Connection connection, String sql, List<String> binds, ResultRow consumer)
            throws SQLException {
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setQueryTimeout(catalogQueryTimeoutSeconds());
            for (int index = 0; index < binds.size(); index++) {
                statement.setString(index + 1, binds.get(index));
            }
            int rows = 0;
            try (ResultSet result = statement.executeQuery()) {
                while (result.next()) {
                    rows++;
                    if (rows > METADATA_FETCH_LIMIT) {
                        throw new ProbeFailure("DUPLICATE_METADATA");
                    }
                    consumer.accept(result);
                }
            }
        }
        catch (SQLTimeoutException timeout) {
            throw new ProbeFailure("QUERY_TIMEOUT");
        }
    }

    private static long requiredCount(ResultSet result, String column) throws SQLException {
        long value = result.getLong(column);
        if (result.wasNull() || value < 0) {
            throw new ProbeFailure("ROW_COUNT_UNAVAILABLE");
        }
        return value;
    }

    private static Long optionalLong(ResultSet result, String column) throws SQLException {
        long value = result.getLong(column);
        if (result.wasNull()) {
            return null;
        }
        return value;
    }

    private static int requiredInt(ResultSet result, String column) throws SQLException {
        int value = result.getInt(column);
        if (result.wasNull() || value < 1) {
            throw new ProbeFailure("DUPLICATE_METADATA");
        }
        return value;
    }

    private static String optionalSequence(ResultSet result, String column) throws SQLException {
        String value = result.getString(column);
        if (result.wasNull()) {
            return null;
        }
        BigInteger parsed = parseOptionalSequence(value);
        return parsed == null ? null : parsed.toString();
    }

    private static BigInteger parseOptionalSequence(String value) {
        if (value == null || value.isBlank()) {
            return null;
        }
        try {
            BigInteger parsed = new BigInteger(value.trim());
            if (parsed.signum() < 0) {
                return null;
            }
            return parsed;
        }
        catch (NumberFormatException error) {
            throw new ProbeFailure("UNSAFE_IDENTIFIER");
        }
    }

    private static Boolean optionalNullable(String value) {
        if (value == null || value.isBlank()) {
            return null;
        }
        String trimmed = value.trim().toUpperCase(Locale.ROOT);
        if ("Y".equals(trimmed) || "YES".equals(trimmed)) {
            return true;
        }
        if ("N".equals(trimmed) || "NO".equals(trimmed)) {
            return false;
        }
        throw new ProbeFailure("UNSAFE_IDENTIFIER");
    }

    private static boolean sqlUnique(String value) {
        String trimmed = trim(value).toUpperCase(Locale.ROOT);
        if ("U".equals(trimmed) || "V".equals(trimmed)) {
            return true;
        }
        if ("D".equals(trimmed) || "E".equals(trimmed)) {
            return false;
        }
        throw new ProbeFailure("UNSAFE_IDENTIFIER");
    }

    private static boolean searchCondition(String value) {
        String trimmed = trim(value).toUpperCase(Locale.ROOT);
        if ("Y".equals(trimmed) || "YES".equals(trimmed)) {
            return true;
        }
        if ("N".equals(trimmed) || "NO".equals(trimmed)) {
            return false;
        }
        throw new ProbeFailure("UNSAFE_IDENTIFIER");
    }

    private static String requireJournalImages(String value) {
        if (value == null || value.isBlank()) {
            throw new ProbeFailure("NOT_JOURNALED");
        }
        String trimmed = value.trim().toUpperCase(Locale.ROOT);
        if (!JOURNAL_IMAGES.matcher(trimmed).matches()) {
            throw new ProbeFailure("UNSAFE_IDENTIFIER");
        }
        return trimmed;
    }

    private static String requireTypeName(String value) {
        if (value == null || value.isBlank()) {
            throw new ProbeFailure("UNSAFE_IDENTIFIER");
        }
        String trimmed = value.trim().toUpperCase(Locale.ROOT);
        if (!TYPE_NAME.matcher(trimmed).matches()) {
            throw new ProbeFailure("UNSAFE_IDENTIFIER");
        }
        return trimmed;
    }

    private static String requireConstraintType(String value) {
        if (value == null || value.isBlank()) {
            throw new ProbeFailure("UNSAFE_IDENTIFIER");
        }
        String trimmed = value.trim().toUpperCase(Locale.ROOT);
        if (!CONSTRAINT_TYPE.matcher(trimmed).matches()) {
            throw new ProbeFailure("UNSAFE_IDENTIFIER");
        }
        return trimmed;
    }

    private static String requireOrdering(String value) {
        String trimmed = trim(value).toUpperCase(Locale.ROOT);
        if ("A".equals(trimmed) || "D".equals(trimmed) || "ASC".equals(trimmed) || "DESC".equals(trimmed)) {
            return trimmed.startsWith("D") ? "D" : "A";
        }
        if (trimmed.isEmpty()) {
            return "A";
        }
        throw new ProbeFailure("UNSAFE_IDENTIFIER");
    }

    private static String optionalTimestamp(String value) {
        if (value == null || value.isBlank()) {
            return null;
        }
        String trimmed = value.trim().replace(' ', 'T');
        if (trimmed.indexOf('\n') >= 0 || trimmed.indexOf('\r') >= 0 || trimmed.indexOf('\t') >= 0) {
            throw new ProbeFailure("UNSAFE_IDENTIFIER");
        }
        return trimmed;
    }

    private static String optionalToken(String value) {
        if (value == null || value.isBlank()) {
            return null;
        }
        String trimmed = value.trim().toUpperCase(Locale.ROOT);
        if (!trimmed.matches("[A-Z0-9_*$#@.-]{1,32}")) {
            throw new ProbeFailure("UNSAFE_IDENTIFIER");
        }
        return trimmed;
    }

    private static String optionalIdentifier(String value) {
        if (value == null || value.isBlank()) {
            return null;
        }
        return requireIdentifier(value).toUpperCase(Locale.ROOT);
    }

    private static String trim(String value) {
        return value == null ? "" : value.trim();
    }

    private static String writeJson(Object value) {
        StringBuilder builder = new StringBuilder();
        writeJson(builder, value);
        return builder.toString();
    }

    private static void writeJson(StringBuilder builder, Object value) {
        switch (value) {
            case null -> builder.append("null");
            case String text -> writeString(builder, text);
            case Boolean flag -> builder.append(flag ? "true" : "false");
            case Long number -> builder.append(number.toString());
            case Integer number -> builder.append(number.toString());
            case Map<?, ?> map -> {
                builder.append('{');
                boolean first = true;
                for (Map.Entry<?, ?> entry : map.entrySet()) {
                    if (!first) {
                        builder.append(',');
                    }
                    first = false;
                    writeString(builder, String.valueOf(entry.getKey()));
                    builder.append(':');
                    writeJson(builder, entry.getValue());
                }
                builder.append('}');
            }
            case List<?> list -> {
                builder.append('[');
                boolean first = true;
                for (Object item : list) {
                    if (!first) {
                        builder.append(',');
                    }
                    first = false;
                    writeJson(builder, item);
                }
                builder.append(']');
            }
            default -> throw new ProbeFailure("UNSAFE_IDENTIFIER");
        }
    }

    private static void writeString(StringBuilder builder, String value) {
        builder.append('"');
        for (int index = 0; index < value.length(); index++) {
            char character = value.charAt(index);
            switch (character) {
                case '"' -> builder.append("\\\"");
                case '\\' -> builder.append("\\\\");
                case '\n' -> builder.append("\\n");
                case '\r' -> builder.append("\\r");
                case '\t' -> builder.append("\\t");
                default -> {
                    if (character < 0x20) {
                        builder.append(String.format("\\u%04x", (int) character));
                    }
                    else {
                        builder.append(character);
                    }
                }
            }
        }
        builder.append('"');
    }

    @FunctionalInterface
    private interface ResultRow {
        void accept(ResultSet result) throws SQLException;
    }

    private static final class TableAcc {
        private String name;
        private long rowCount;
        private long dataSize;
        private long memberCount;
        private String journalLibrary;
        private String journalName;
        private String journalImages;
        private final List<ColumnRecord> columns = new ArrayList<>();
        private final Set<String> columnNames = new LinkedHashSet<>();
        private final Set<Integer> columnOrdinals = new LinkedHashSet<>();
        private final Map<String, ConstraintAcc> constraints = new LinkedHashMap<>();
        private final Map<String, IndexAcc> indexes = new LinkedHashMap<>();

        private TableRecord toRecord() {
            List<ConstraintRecord> constraintRecords = new ArrayList<>();
            for (ConstraintAcc constraint : constraints.values()) {
                constraintRecords.add(new ConstraintRecord(
                        constraint.schema,
                        constraint.name,
                        constraint.type,
                        List.copyOf(constraint.columns)));
            }
            List<IndexRecord> indexRecords = new ArrayList<>();
            for (IndexAcc index : indexes.values()) {
                indexRecords.add(new IndexRecord(
                        index.schema,
                        index.name,
                        index.unique,
                        index.sparse,
                        index.selectOmit,
                        List.copyOf(index.columns)));
            }
            return new TableRecord(
                    name,
                    rowCount,
                    dataSize,
                    memberCount,
                    journalLibrary,
                    journalName,
                    journalImages,
                    List.copyOf(columns),
                    List.copyOf(constraintRecords),
                    List.copyOf(indexRecords));
        }
    }

    private static final class ConstraintAcc {
        private String schema;
        private String name;
        private String type;
        private final List<String> columns = new ArrayList<>();
        private final Set<String> columnNames = new LinkedHashSet<>();
        private final Set<Integer> ordinals = new LinkedHashSet<>();
    }

    private static final class IndexAcc {
        private String schema;
        private String name;
        private boolean unique;
        private boolean sparse;
        private Boolean selectOmit;
        private final List<IndexColumnRecord> columns = new ArrayList<>();
        private final Set<String> columnNames = new LinkedHashSet<>();
        private final Set<Integer> ordinals = new LinkedHashSet<>();
    }

    record JournalIdentity(String library, String name) {
    }

    record JournalChain(
            String library,
            String name,
            String continuity,
            List<ReceiverRecord> receivers) {
    }

    record ReceiverRecord(
            String library,
            String name,
            String status,
            String firstSequence,
            String lastSequence,
            String attachTimestamp,
            String detachTimestamp,
            String previousLibrary,
            String previousName) {
    }

    record ColumnRecord(
            String name,
            String type,
            Long length,
            Long numericPrecision,
            Long numericScale,
            Long ccsid,
            Boolean nullable,
            int ordinal) {
    }

    record ConstraintRecord(String schema, String name, String type, List<String> columns) {
    }

    record IndexColumnRecord(String name, int ordinal, String ordering) {
    }

    record IndexRecord(
            String schema,
            String name,
            boolean unique,
            boolean sparse,
            Boolean selectOmit,
            List<IndexColumnRecord> columns) {
    }

    record TableRecord(
            String name,
            long rowCount,
            long dataSize,
            long memberCount,
            String journalLibrary,
            String journalName,
            String journalImages,
            List<ColumnRecord> columns,
            List<ConstraintRecord> constraints,
            List<IndexRecord> indexes) {
    }

    record CatalogDocument(
            String formatVersion,
            String observedAt,
            String environment,
            String sourceSchema,
            List<JournalChain> journals,
            List<TableRecord> tables) {
    }

    static final class ProbeFailure extends RuntimeException {
        private static final long serialVersionUID = 1L;
        private final String code;
        private final String sqlState;
        private final Integer vendorCode;

        ProbeFailure(String code) {
            this(code, null, null);
        }

        ProbeFailure(String code, String sqlState, Integer vendorCode) {
            super(code);
            this.code = code;
            this.sqlState = sqlState;
            this.vendorCode = vendorCode;
        }

        String code() {
            return code;
        }

        String stderrLine() {
            StringBuilder line = new StringBuilder("fleet_catalog_error=").append(code);
            if (sqlState != null) line.append(" sqlstate=").append(sqlState);
            if (vendorCode != null) line.append(" vendor=").append(vendorCode.intValue());
            return line.toString();
        }
    }

    record PortedJdbc(AS400 system, AS400JDBCDataSource dataSource) {
    }

    /**
     * Périmètre déclaré par l'appelant : environnement de flotte, schéma
     * source et liste ordonnée des tables autorisées. Aucune valeur
     * d'installation n'existe dans le code — chaque champ est exigé de
     * l'environnement, sans défaut.
     */
    record CatalogScope(String environment, String sourceSchema, List<String> tables) {

        static CatalogScope fromEnvironment() {
            return fromEnvironment(System.getenv());
        }

        static CatalogScope fromEnvironment(Map<String, String> env) {
            return new CatalogScope(
                    environmentValue(env.get("QUADRINGENT_ENVIRONMENT")),
                    requiredIdentifier(env.get("QUADRINGENT_SOURCE_SCHEMA"), "QUADRINGENT_SOURCE_SCHEMA"),
                    tableList(env.get("QUADRINGENT_FLEET_TABLES")));
        }

        private static String environmentValue(String raw) {
            if (raw == null || raw.isBlank()) {
                throw new IllegalArgumentException("QUADRINGENT_ENVIRONMENT is required");
            }
            String value = raw.trim().toUpperCase(Locale.ROOT);
            if (!IDENTIFIER.matcher(value).matches()) {
                throw new IllegalArgumentException("QUADRINGENT_ENVIRONMENT is not a safe identifier");
            }
            return value;
        }

        private static String requiredIdentifier(String raw, String name) {
            if (raw == null || raw.isBlank()) {
                throw new IllegalArgumentException(name + " is required");
            }
            String value = raw.trim().toUpperCase(Locale.ROOT);
            if (!IDENTIFIER.matcher(value).matches()) {
                throw new IllegalArgumentException(name + " is not a safe identifier");
            }
            return value;
        }

        private static List<String> tableList(String raw) {
            if (raw == null || raw.isBlank()) {
                throw new IllegalArgumentException("QUADRINGENT_FLEET_TABLES is required");
            }
            List<String> tables = new ArrayList<>();
            Set<String> seen = new LinkedHashSet<>();
            for (String item : raw.split(",")) {
                String table = requiredIdentifier(item, "QUADRINGENT_FLEET_TABLES");
                if (!seen.add(table)) {
                    throw new IllegalArgumentException("QUADRINGENT_FLEET_TABLES contains a duplicate");
                }
                tables.add(table);
            }
            if (tables.isEmpty() || tables.size() > MAX_SCOPE_TABLES) {
                throw new IllegalArgumentException("QUADRINGENT_FLEET_TABLES must hold 1.."
                        + MAX_SCOPE_TABLES + " tables");
            }
            return List.copyOf(tables);
        }
    }

    record Settings(
            String host,
            String user,
            String password,
            boolean tls,
            OptionalInt databasePort,
            OptionalInt signonPort,
            OptionalInt commandPort) {

        static Settings fromEnvironment() {
            return new Settings(
                    required("ISERIES_HOST"),
                    required("ISERIES_USER"),
                    required("ISERIES_PASSWORD"),
                    tlsValue("AS400_TLS", true),
                    optionalPort("AS400_DATABASE_PORT"),
                    optionalPort("AS400_SIGNON_PORT"),
                    optionalPort("AS400_COMMAND_PORT"));
        }

        private static String required(String name) {
            String value = System.getenv(name);
            if (value == null || value.isBlank()) {
                throw new IllegalArgumentException(name + " is required");
            }
            return value;
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
                throw new IllegalArgumentException("AS400_TLS=false requires AS400_ALLOW_PLAINTEXT=true");
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
