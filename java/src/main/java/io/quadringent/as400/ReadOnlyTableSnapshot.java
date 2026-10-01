package io.quadringent.as400;

import java.nio.file.Path;
import java.nio.file.Files;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.OptionalInt;
import java.util.Properties;
import java.util.regex.Pattern;

import com.ibm.as400.access.AS400;
import com.ibm.as400.access.AS400JDBCDataSource;
import com.ibm.as400.access.SecureAS400;

/**
 * Read-only JDBC snapshot of one IBM i table into as400-raw-v1 batches.
 *
 * <p>It emits create events with after-images only. Stdout contains a single
 * summary line, never row payloads or credentials.</p>
 */
public final class ReadOnlyTableSnapshot {
    private ReadOnlyTableSnapshot() {
    }

    public static void main(String[] args) throws Exception {
        Settings settings = Settings.fromEnvironment();
        SnapshotIdentity identity = new SnapshotIdentity(settings.schema(), settings.table(), settings.runId());
        Files.createDirectories(settings.rawDirectory());
        // Ordinal JDBC snapshots are not resumable. Do not mix a new attempt
        // with partial output or silently reuse a directory on process restart.
        try (var existing = Files.list(settings.rawDirectory())) {
            if (existing.findAny().isPresent()) {
                throw new IllegalStateException("snapshot output directory must be empty for a new attempt");
            }
        }
        Files.createFile(settings.rawDirectory().resolve("snapshot-attempt.lock"));
        String capturedAt = Instant.now().toString();
        Class.forName("com.ibm.as400.access.AS400JDBCDriver");
        long started = System.nanoTime();
        long rows = 0;
        int batches = 0;
        int columns = 0;
        List<RawCaptureWriter.RawEvent> buffer = new ArrayList<>();
        RawCaptureWriter writer = new RawCaptureWriter(settings.rawDirectory());
        try (Connection connection = openConnection(settings)) {
            if (settings.rrnProbe()) {
                try (PreparedStatement probe = connection.prepareStatement(rrnBoundSql(settings));
                        ResultSet bound = probe.executeQuery()) {
                    bound.next();
                    System.out.printf(
                            "rrn_probe table=%s.%s max_rrn=%d%n",
                            settings.schema(), settings.table(), bound.getLong(1));
                }
                return;
            }
            try (PreparedStatement statement = connection.prepareStatement(
                    snapshotSql(settings), ResultSet.TYPE_FORWARD_ONLY,
                    ResultSet.CONCUR_READ_ONLY)) {
            statement.setFetchSize(settings.fetchSize());
            statement.setQueryTimeout(settings.queryTimeoutSeconds());
            connection.setReadOnly(true);
            if (settings.rrnStart() >= 0) {
                statement.setLong(1, settings.rrnStart());
                statement.setLong(2, settings.rrnEnd());
            }
            try (ResultSet result = statement.executeQuery()) {
                ResultSetMetaData meta = result.getMetaData();
                columns = meta.getColumnCount();
                // Les noms de colonnes sont lus une fois, pas par ligne : un appel
                // JDBC par colonne et par ligne domine le cout des que la table est
                // large (mesure sur une table a 301 colonnes : le lecteur saturait
                // huit coeurs et n'ecrivait plus rien).
                String[] columnNames = new String[columns];
                int rrnColumns = 0;
                for (int index = 1; index <= columns; index++) {
                    columnNames[index - 1] = meta.getColumnName(index);
                    if ("_rrn".equalsIgnoreCase(columnNames[index - 1])) {
                        rrnColumns++;
                    }
                }
                // La colonne _rrn ajoutee au SELECT est notre champ reserve :
                // une table qui en porterait deja un rendrait l'identite
                // physique ambigue. On refuse plutot que d'ecraser une donnee.
                if (rrnColumns != 1) {
                    throw new IllegalStateException(
                            "snapshot _rrn column is missing or collides with a real column");
                }
                int batchSize = settings.batchSize();
                while (result.next()) {
                    rows++;
                    Map<String, Object> after = new LinkedHashMap<>(columns * 2);
                    for (int index = 1; index <= columns; index++) {
                        // _rrn est notre champ reserve : sa cle JSON doit etre
                        // la forme canonique en minuscules, quelle que soit la
                        // casse que le pilote rend pour l'alias SQL.
                        String name = "_rrn".equalsIgnoreCase(columnNames[index - 1])
                                ? "_rrn"
                                : columnNames[index - 1];
                        after.put(name, normalize(result.getObject(index)));
                    }
                    long ordinal = settings.rowOffset() + rows;
                    String sequence = Long.toString(ordinal);
                    String eventId = identity.eventId(ordinal);
                    buffer.add(new RawCaptureWriter.RawEvent(
                            eventId,
                            "ibmi",
                            identity.journal(),
                            settings.schema(),
                            settings.table(),
                            "c",
                            identity.receiver(),
                            "SNAPSHOT",
                            sequence,
                            capturedAt,
                            "snapshot-v1",
                            null,
                            after,
                            "SNAPSHOT_ROW"));
                    if (buffer.size() >= batchSize) {
                        writer.writeBatch(buffer, new RawCaptureWriter.RawPosition(identity.receiver(), sequence));
                        batches++;
                        buffer.clear();
                    }
                }
            }
            }
        }
        if (!buffer.isEmpty()) {
            writer.writeBatch(buffer, new RawCaptureWriter.RawPosition(
                    identity.receiver(), Long.toString(settings.rowOffset() + rows)));
            batches++;
        }
        long elapsedMs = (System.nanoTime() - started) / 1_000_000L;
        System.out.printf(
                "snapshot_summary table=%s.%s rows=%d columns=%d batches=%d elapsed_ms=%d run_id=%s%n",
                settings.schema(),
                settings.table(),
                rows,
                columns,
                batches,
                elapsedMs,
                settings.runId());
        // Une plage RRN peut legitimement ne contenir aucune ligne (fin de
        // table atteinte, lignes supprimees) : elle rend alors rows=0 sans
        // erreur. Une lecture non bornee, elle, ne doit jamais etre vide.
        if (rows < 1 && !emptyReadAllowed(settings)) {
            throw new IllegalStateException("snapshot produced no rows");
        }
    }

    static boolean emptyReadAllowed(Settings settings) {
        return settings.rrnStart() >= 0;
    }

    /**
     * Requête de l'image initiale. Sans bornes : la table entière. Avec bornes
     * RRN : une tranche physique de la table, relisible indépendamment — une
     * coupure ne fait perdre que la tranche courante au lieu de toute la copie.
     */
    static String snapshotSql(Settings settings) {
        // RRN(T) est la position physique de la ligne : c'est l'identite que
        // le journal porte sur chaque entree (meme celles sans image sous
        // IMAGES(*AFTER)). La copie et le flux doivent donc la partager pour
        // que delete/upsert retrouvent la meme ligne.
        if (settings.rrnStart() >= 0) {
            return "SELECT T.*, RRN(T) AS \"_rrn\" FROM " + settings.schema() + "." + settings.table()
                    + " T WHERE RRN(T) BETWEEN ? AND ? ORDER BY RRN(T)";
        }
        return "SELECT T.*, RRN(T) AS \"_rrn\" FROM " + settings.schema() + "." + settings.table() + " T";
    }

    static String rrnBoundSql(Settings settings) {
        return "SELECT MAX(RRN(T)) FROM " + settings.schema() + "." + settings.table() + " T";
    }

    private static Connection openConnection(Settings settings) throws Exception {
        TlsTrust.install(settings.tls());
        int loginSeconds = loginTimeoutSeconds(settings);
        if (settings.databasePort().isEmpty() && settings.signonPort().isEmpty()) {
            Properties properties = new Properties();
            properties.setProperty("user", settings.user());
            properties.setProperty("password", settings.password());
            properties.setProperty("prompt", "false");
            properties.setProperty("date format", "iso");
            properties.setProperty("secure", Boolean.toString(settings.tls()));
            properties.setProperty("socket timeout", Integer.toString(settings.socketTimeoutMs()));
            properties.setProperty("login timeout", Integer.toString(loginSeconds));
            return DriverManager.getConnection("jdbc:as400://" + settings.host(), properties);
        }
        AS400 system = settings.tls()
                ? new SecureAS400(settings.host(), settings.user(), settings.password().toCharArray())
                : new AS400(settings.host(), settings.user(), settings.password().toCharArray());
        settings.databasePort().ifPresent(port -> system.setServicePort(AS400.DATABASE, port));
        settings.signonPort().ifPresent(port -> system.setServicePort(AS400.SIGNON, port));
        AS400JDBCDataSource dataSource = new AS400JDBCDataSource(system);
        dataSource.setPrompt(false);
        dataSource.setSecure(settings.tls());
        dataSource.setDateFormat("iso");
        dataSource.setSocketTimeout(settings.socketTimeoutMs());
        dataSource.setLoginTimeout(loginSeconds);
        return dataSource.getConnection();
    }

    /** JTOpen exprime le login timeout en secondes, la borne est en millisecondes. */
    static int loginTimeoutSeconds(Settings settings) {
        return (int) Math.max(1, (settings.loginTimeoutMs() + 999L) / 1000L);
    }

    private static Object normalize(Object value) {
        if ((value instanceof Double doubleValue && !Double.isFinite(doubleValue))
                || (value instanceof Float floatValue && !Float.isFinite(floatValue))) {
            throw new IllegalArgumentException("non-finite snapshot numeric value is unsupported");
        }
        if (value == null || value instanceof String || value instanceof Boolean
                || value instanceof Byte || value instanceof Short || value instanceof Integer
                || value instanceof Long || value instanceof Float || value instanceof Double
                || value instanceof java.math.BigInteger || value instanceof java.math.BigDecimal
                || value instanceof byte[] || value instanceof java.sql.Date
                || value instanceof java.sql.Time || value instanceof java.sql.Timestamp) {
            return RawCaptureWriter.normalize(value);
        }
        // LOBs and vendor-specific objects need explicit, bounded extraction.
        // Failing is safer than reporting a successful snapshot with lost data.
        // Never call toString(): an unsupported object may expose its payload.
        throw new IllegalArgumentException("unsupported snapshot JDBC value type: " + value.getClass().getName());
    }

    record Settings(
            String host,
            String user,
            String password,
            String schema,
            String table,
            Path rawDirectory,
            String runId,
            int batchSize,
            int fetchSize,
            int queryTimeoutSeconds,
            int socketTimeoutMs,
            int loginTimeoutMs,
            boolean tls,
            OptionalInt databasePort,
            OptionalInt signonPort,
            long rrnStart,
            long rrnEnd,
            long rowOffset,
            boolean rrnProbe) {

        static Settings fromEnvironment() {
            Settings settings = new Settings(
                    required("ISERIES_HOST"),
                    required("ISERIES_USER"),
                    required("ISERIES_PASSWORD"),
                    requireIdentifier("ISERIES_SCHEMA", required("ISERIES_SCHEMA")),
                    requireIdentifier("ISERIES_TABLE", required("ISERIES_TABLE")),
                    Path.of(required("AS400_RAW_DIRECTORY")),
                    required("AS400_SNAPSHOT_RUN_ID"),
                    positive("AS400_SNAPSHOT_BATCH_SIZE", 5000),
                    positive("AS400_SNAPSHOT_FETCH_SIZE", 500),
                    positive("AS400_SNAPSHOT_QUERY_TIMEOUT_SECONDS", 60),
                    timeoutMillis("AS400_SNAPSHOT_SOCKET_TIMEOUT_MS", 120_000),
                    timeoutMillis("AS400_SNAPSHOT_LOGIN_TIMEOUT_MS", 60_000),
                    tlsValue("AS400_TLS", true),
                    optionalPort("AS400_DATABASE_PORT"),
                    optionalPort("AS400_SIGNON_PORT"),
                    optionalLong("AS400_SNAPSHOT_RRN_START", -1),
                    optionalLong("AS400_SNAPSHOT_RRN_END", -1),
                    optionalLong("AS400_SNAPSHOT_ROW_OFFSET", 0),
                    booleanValue("AS400_SNAPSHOT_RRN_PROBE", false));
            validateChunking(settings);
            return settings;
        }

        /**
         * Une tranche exige ses deux bornes, ordonnées, à partir de 1 ; l'offset
         * ordinal est positif ou nul ; la sonde de borne est exclusive — elle
         * mesure la table sans rien écrire.
         */
        static void validateChunking(Settings settings) {
            boolean hasStart = settings.rrnStart() >= 0;
            boolean hasEnd = settings.rrnEnd() >= 0;
            if (hasStart != hasEnd) {
                throw new IllegalArgumentException(
                        "AS400_SNAPSHOT_RRN_START and AS400_SNAPSHOT_RRN_END must be set together");
            }
            if (hasStart && (settings.rrnStart() < 1 || settings.rrnEnd() < settings.rrnStart())) {
                throw new IllegalArgumentException(
                        "AS400_SNAPSHOT_RRN bounds must satisfy 1 <= start <= end");
            }
            if (settings.rrnProbe() && hasStart) {
                throw new IllegalArgumentException(
                        "AS400_SNAPSHOT_RRN_PROBE cannot be combined with a range");
            }
            if (settings.rowOffset() < 0) {
                throw new IllegalArgumentException("AS400_SNAPSHOT_ROW_OFFSET must be zero or positive");
            }
        }

        private static long optionalLong(String name, long defaultValue) {
            String value = System.getenv(name);
            if (value == null || value.isBlank()) {
                return defaultValue;
            }
            long parsed = Long.parseLong(value.trim());
            if (parsed < 0) {
                throw new IllegalArgumentException(name + " must be zero or positive");
            }
            return parsed;
        }

        private static String required(String name) {
            String value = System.getenv(name);
            if (value == null || value.isBlank()) {
                throw new IllegalArgumentException(name + " is required");
            }
            return value;
        }

        /**
         * Schéma et table sont concaténés dans le SQL de la tranche : seul un
         * identifiant IBM i borné ([A-Za-z0-9$#_]{1,128}) passe — jamais une
         * chaîne libre.
         */
        private static final Pattern IBM_IDENTIFIER =
                Pattern.compile("[A-Za-z0-9$#_]{1,128}");

        static String requireIdentifier(String name, String value) {
            String cleaned = value == null ? "" : value.trim();
            if (!IBM_IDENTIFIER.matcher(cleaned).matches()) {
                throw new IllegalArgumentException(
                        name + " must be an IBM i identifier ([A-Za-z0-9$#_]{1,128})");
            }
            return cleaned;
        }

        private static int positive(String name, int defaultValue) {
            String value = System.getenv(name);
            if (value == null || value.isBlank()) {
                return defaultValue;
            }
            int parsed = Integer.parseInt(value);
            if (parsed < 1) {
                throw new IllegalArgumentException(name + " must be positive");
            }
            return parsed;
        }

        private static int timeoutMillis(String name, int defaultValue) {
            return parseTimeoutMillis(name, System.getenv(name), defaultValue);
        }

        /**
         * Borne de timeout socket/login : une seconde minimum, une heure
         * maximum — au-delà, une lecture suspendue ne serait jamais détectée.
         */
        static int parseTimeoutMillis(String name, String raw, int defaultValue) {
            if (raw == null || raw.isBlank()) {
                return defaultValue;
            }
            int parsed = Integer.parseInt(raw.trim());
            if (parsed < 1_000 || parsed > 3_600_000) {
                throw new IllegalArgumentException(name + " must be in [1000, 3600000] ms");
            }
            return parsed;
        }

        private static OptionalInt optionalPort(String name) {
            String value = System.getenv(name);
            if (value == null || value.isBlank()) {
                return OptionalInt.empty();
            }
            return OptionalInt.of(parsePort(name, value));
        }

        /** Un port déclaré reste dans la borne TCP : 0 ou 65536+ refusés. */
        static int parsePort(String name, String raw) {
            int parsed = Integer.parseInt(raw.trim());
            if (parsed < 1 || parsed > 65_535) {
                throw new IllegalArgumentException(name + " must be in [1, 65535]");
            }
            return parsed;
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
