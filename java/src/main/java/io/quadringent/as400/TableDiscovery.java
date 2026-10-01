package io.quadringent.as400;

import java.io.PrintStream;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Optional;
import java.util.stream.Collectors;

/**
 * Découverte de tables IBM i (catalogue seulement, jamais de lignes métier).
 *
 * <p>Réutilise les vues de catalogue déjà éprouvées par {@link FleetCatalogProbe}
 * ({@code QSYS2.SYSTABLES}, {@code QSYS2.JOURNALED_OBJECTS},
 * {@code QSYS2.SYSCST}/{@code QSYS2.SYSKEYCST}) et la validation d'identifiant
 * de {@link JournalSession#sqlIdentifier(String)}. Cette classe ne contient
 * aucun appel JDBC : elle construit le SQL borné et formate/parse le
 * protocole ligne du worker, pour rester testable hors connexion (voir
 * {@code TableDiscoveryTest}).
 */
public final class TableDiscovery {
    /** Tabulation — jamais dans un nom IBM i ni dans un texte de table borné. */
    static final char SEP = '\t';
    static final String HEADER =
            "table\tlibrary\tsystem_name\tsql_name\ttext\trow_count\tsize_bytes\thas_key\t"
                    + "key_columns\tjournaled\tjournal_library\tjournal_name\timages\tomitted\tcolumns";
    static final int DEFAULT_LIMIT = 500;
    static final int MAX_LIMIT = 5000;
    /** Borne le nombre de colonnes rapportées par table — une table à 301 colonnes existe déjà dans ce dépôt. */
    static final int MAX_COLUMNS_PER_TABLE = 2000;

    private TableDiscovery() {
    }

    /**
     * Une colonne rapportée par {@code QSYS2.SYSCOLUMNS} — nom, type SQL natif, longueur,
     * échelle et nullabilité. L'ordre est porté par la position dans la liste
     * ({@code ORDER BY ORDINAL_POSITION}), jamais un champ numérique séparé : c'est aussi le
     * contrat de {@code discovered_columns} côté control plane (v2/services/tables.py).
     */
    public record ColumnRow(String name, String type, Long length, Long scale, boolean nullable) {
    }

    /** Une ligne de sortie de {@code discover} — un enregistrement de catalogue par table trouvée. */
    public record DiscoveredTableRow(
            String library,
            String systemName,
            String sqlName,
            String text,
            Long rowCount,
            Long sizeBytes,
            boolean hasKey,
            List<String> keyColumns,
            boolean journaled,
            String journalLibrary,
            String journalName,
            String images,
            boolean omitted,
            List<ColumnRow> columns) {
    }

    /** Requête de découverte : bibliothèques optionnelles (défaut : bibliothèque courante), limite, filtre texte. */
    public record DiscoverRequest(List<String> libraries, int limit, String search) {

        public static DiscoverRequest fromFields(java.util.Map<String, String> fields, String currentLibrary) {
            List<String> libraries = parseLibraries(fields.get("libraries"), currentLibrary);
            int limit = parseLimit(fields.get("limit"));
            String search = normalizeSearch(fields.get("search"));
            return new DiscoverRequest(libraries, limit, search);
        }
    }

    static List<String> parseLibraries(String csv, String currentLibrary) {
        if (csv == null || csv.isBlank()) {
            if (currentLibrary == null || currentLibrary.isBlank()) {
                throw new IllegalArgumentException("discover requires libraries or a current library");
            }
            return List.of(JournalSession.sqlIdentifier(currentLibrary));
        }
        List<String> libraries = new ArrayList<>();
        java.util.Set<String> seen = new java.util.LinkedHashSet<>();
        for (String item : csv.split(",", -1)) {
            String library = item.trim();
            if (library.isEmpty()) {
                throw new IllegalArgumentException("discover libraries entries must be non-empty");
            }
            library = JournalSession.sqlIdentifier(library);
            String key = library.toUpperCase(Locale.ROOT);
            if (seen.add(key)) {
                libraries.add(library);
            }
        }
        if (libraries.isEmpty()) {
            throw new IllegalArgumentException("discover libraries must not be empty");
        }
        return libraries;
    }

    static int parseLimit(String raw) {
        if (raw == null || raw.isBlank()) {
            return DEFAULT_LIMIT;
        }
        int parsed = Integer.parseInt(raw.trim());
        if (parsed < 1 || parsed > MAX_LIMIT) {
            throw new IllegalArgumentException("discover limit must be in [1, " + MAX_LIMIT + "]");
        }
        return parsed;
    }

    /** Le filtre texte n'entre jamais tel quel dans le SQL : seul un motif LIKE échappé est construit ailleurs. */
    static String normalizeSearch(String raw) {
        if (raw == null || raw.isBlank()) {
            return null;
        }
        String trimmed = raw.trim();
        if (trimmed.length() > 128) {
            throw new IllegalArgumentException("discover search must be at most 128 characters");
        }
        return trimmed;
    }

    /**
     * SQL borné combinant SYSTABLES (nom/texte), SYSTABLESTAT (lignes/taille estimées),
     * JOURNALED_OBJECTS (journalisation + images) et une agrégation SYSKEYCST/SYSCST
     * (clé primaire ou index unique) — même trio de vues que {@code FleetCatalogProbe}.
     * Les bibliothèques sont interpolées après validation stricte d'identifiant : jamais
     * de paramètre utilisateur brut dans le texte SQL.
     */
    static String discoverySql(DiscoverRequest request) {
        String libList = request.libraries().stream()
                .map(lib -> "'" + JournalSession.sqlIdentifier(lib) + "'")
                .collect(Collectors.joining(", "));
        String searchClause = "";
        if (request.search() != null) {
            // Le motif LIKE est échappé (\, %, _) puis lié en littéral simple guillemet doublé.
            String escaped = escapeLikePattern(request.search());
            searchClause = " AND (UPPER(t.TABLE_NAME) LIKE UPPER('%" + escaped + "%') ESCAPE '\\' "
                    + "OR UPPER(COALESCE(t.TABLE_TEXT, '')) LIKE UPPER('%" + escaped + "%') ESCAPE '\\')";
        }
        return """
                SELECT
                    t.TABLE_SCHEMA AS LIBRARY,
                    t.SYSTEM_TABLE_NAME AS SYSTEM_NAME,
                    t.TABLE_NAME AS SQL_NAME,
                    t.TABLE_TEXT AS TABLE_TEXT,
                    s.NUMBER_ROWS AS ROW_COUNT,
                    s.DATA_SIZE AS SIZE_BYTES,
                    j.JOURNAL_LIBRARY AS JOURNAL_LIBRARY,
                    j.JOURNAL_NAME AS JOURNAL_NAME,
                    j.JOURNAL_IMAGES AS JOURNAL_IMAGES,
                    k.HAS_KEY AS HAS_KEY,
                    k.KEY_COLUMNS AS KEY_COLUMNS
                FROM QSYS2.SYSTABLES t
                LEFT JOIN QSYS2.SYSTABLESTAT s
                    ON s.TABLE_SCHEMA = t.TABLE_SCHEMA AND s.TABLE_NAME = t.TABLE_NAME
                LEFT JOIN QSYS2.JOURNALED_OBJECTS j
                    ON j.OBJECT_LIBRARY = t.TABLE_SCHEMA AND j.OBJECT_NAME = t.SYSTEM_TABLE_NAME
                    AND j.OBJECT_TYPE = '*FILE'
                LEFT JOIN (
                    SELECT
                        kc.TABLE_SCHEMA,
                        kc.TABLE_NAME,
                        MAX(1) AS HAS_KEY,
                        LISTAGG(kc.COLUMN_NAME, ',') WITHIN GROUP (ORDER BY kc.ORDINAL_POSITION) AS KEY_COLUMNS
                    FROM QSYS2.SYSCST c
                    JOIN QSYS2.SYSKEYCST kc
                        ON kc.CONSTRAINT_SCHEMA = c.CONSTRAINT_SCHEMA
                        AND kc.CONSTRAINT_NAME = c.CONSTRAINT_NAME
                    WHERE c.CONSTRAINT_TYPE IN ('PRIMARY KEY', 'UNIQUE')
                    GROUP BY kc.TABLE_SCHEMA, kc.TABLE_NAME
                ) k ON k.TABLE_SCHEMA = t.TABLE_SCHEMA AND k.TABLE_NAME = t.TABLE_NAME
                WHERE t.TABLE_SCHEMA IN (%s)
                    AND t.TABLE_TYPE IN ('T', 'P')
                    AND t.FILE_TYPE = 'D'%s
                ORDER BY t.TABLE_SCHEMA, t.SYSTEM_TABLE_NAME
                FETCH FIRST %d ROWS ONLY
                """.formatted(libList, searchClause, request.limit());
    }

    /**
     * SQL borné listant les colonnes des tables déjà trouvées par {@link #discoverySql} —
     * même trio de colonnes que {@code FleetCatalogProbe.loadColumns} ({@code DATA_TYPE},
     * {@code LENGTH}, {@code NUMERIC_SCALE}, {@code IS_NULLABLE}), filtré sur les bibliothèques
     * et les noms SQL de table déjà validés (jamais de valeur utilisateur interpolée ici : les
     * noms proviennent de {@link #discoverySql}'s propre résultat, déjà passés par
     * {@code JournalSession#sqlIdentifier} en amont côté bibliothèque).
     */
    static String columnsSql(List<String> libraries, int limit) {
        String libList = libraries.stream()
                .map(lib -> "'" + JournalSession.sqlIdentifier(lib) + "'")
                .collect(Collectors.joining(", "));
        return """
                SELECT
                    TABLE_SCHEMA,
                    TABLE_NAME,
                    COLUMN_NAME,
                    DATA_TYPE,
                    LENGTH,
                    NUMERIC_SCALE,
                    IS_NULLABLE,
                    ORDINAL_POSITION
                FROM QSYS2.SYSCOLUMNS
                WHERE TABLE_SCHEMA IN (%s)
                ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
                FETCH FIRST %d ROWS ONLY
                """.formatted(libList, limit);
    }

    /**
     * Runs {@link #discoverySql} and writes one {@code table\t...} line per row —
     * shared by {@link JournalSession#emitDiscover} (capture worker) and
     * {@link DiagnosticWorker#emitDiscover} (diagnostic-only worker): catalogue
     * only, never a row-in-error line (a table that fails its own read is simply
     * absent from the result).
     */
    static void emit(Connection jdbc, DiscoverRequest request, int timeoutSeconds, PrintStream out) throws Exception {
        String sql = discoverySql(request);
        List<DiscoveredTableRow> rows = new ArrayList<>();
        try (PreparedStatement statement = jdbc.prepareStatement(sql)) {
            statement.setQueryTimeout(timeoutSeconds);
            try (ResultSet result = statement.executeQuery()) {
                while (result.next()) {
                    String images = result.getString("JOURNAL_IMAGES");
                    boolean journaled = result.getString("JOURNAL_NAME") != null;
                    boolean hasKey = result.getInt("HAS_KEY") == 1;
                    String keyColumnsCsv = result.getString("KEY_COLUMNS");
                    List<String> keyColumns = keyColumnsCsv == null || keyColumnsCsv.isBlank()
                            ? List.of()
                            : List.of(keyColumnsCsv.replaceFirst("^,", "").split(",", -1));
                    Long rowCount = result.getObject("ROW_COUNT") == null ? null : result.getLong("ROW_COUNT");
                    Long sizeBytes = result.getObject("SIZE_BYTES") == null ? null : result.getLong("SIZE_BYTES");
                    rows.add(new DiscoveredTableRow(
                            result.getString("LIBRARY"),
                            result.getString("SYSTEM_NAME"),
                            result.getString("SQL_NAME"),
                            result.getString("TABLE_TEXT"),
                            rowCount,
                            sizeBytes,
                            hasKey,
                            keyColumns,
                            journaled,
                            result.getString("JOURNAL_LIBRARY"),
                            result.getString("JOURNAL_NAME"),
                            images,
                            false,
                            List.of()));
                }
            }
        }
        // Deuxième requête bornée : les colonnes des tables trouvées ci-dessus, jamais
        // interpolées par nom de table utilisateur (filtrée sur les bibliothèques déjà
        // validées, puis rapprochée en mémoire par (library, sql_name)).
        java.util.Map<String, List<ColumnRow>> columnsByTable = new java.util.LinkedHashMap<>();
        if (!rows.isEmpty()) {
            String columnsSql = columnsSql(request.libraries(), MAX_COLUMNS_PER_TABLE);
            try (PreparedStatement statement = jdbc.prepareStatement(columnsSql)) {
                statement.setQueryTimeout(timeoutSeconds);
                try (ResultSet result = statement.executeQuery()) {
                    while (result.next()) {
                        String key = result.getString("TABLE_SCHEMA").trim() + "/" + result.getString("TABLE_NAME").trim();
                        Long length = result.getObject("LENGTH") == null ? null : result.getLong("LENGTH");
                        Long scale = result.getObject("NUMERIC_SCALE") == null ? null : result.getLong("NUMERIC_SCALE");
                        boolean nullable = "Y".equalsIgnoreCase(result.getString("IS_NULLABLE"));
                        ColumnRow column = new ColumnRow(
                                result.getString("COLUMN_NAME").trim(),
                                result.getString("DATA_TYPE").trim(),
                                length,
                                scale,
                                nullable);
                        columnsByTable.computeIfAbsent(key, ignored -> new ArrayList<>()).add(column);
                    }
                }
            }
        }
        for (DiscoveredTableRow row : rows) {
            List<ColumnRow> columns = columnsByTable.getOrDefault(
                    row.library().trim() + "/" + row.sqlName().trim(), List.of());
            out.println(formatRow(new DiscoveredTableRow(
                    row.library(), row.systemName(), row.sqlName(), row.text(), row.rowCount(),
                    row.sizeBytes(), row.hasKey(), row.keyColumns(), row.journaled(),
                    row.journalLibrary(), row.journalName(), row.images(), row.omitted(), columns)));
        }
        out.flush();
    }

    static String escapeLikePattern(String value) {
        StringBuilder escaped = new StringBuilder(value.length());
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            if (c == '\\' || c == '%' || c == '_' || c == '\'') {
                escaped.append('\\');
            }
            escaped.append(c);
        }
        return escaped.toString();
    }

    /** Ligne de protocole tabulée, une par table — jamais de retour chariot dans un champ. */
    static String formatRow(DiscoveredTableRow row) {
        StringBuilder line = new StringBuilder("table");
        appendField(line, row.library());
        appendField(line, row.systemName());
        appendField(line, row.sqlName());
        appendField(line, row.text() == null ? "" : row.text());
        appendField(line, row.rowCount() == null ? "" : String.valueOf(row.rowCount()));
        appendField(line, row.sizeBytes() == null ? "" : String.valueOf(row.sizeBytes()));
        appendField(line, row.hasKey() ? "yes" : "no");
        appendField(line, row.keyColumns() == null ? "" : String.join(",", row.keyColumns()));
        appendField(line, row.journaled() ? "yes" : "no");
        appendField(line, row.journalLibrary() == null ? "" : row.journalLibrary());
        appendField(line, row.journalName() == null ? "" : row.journalName());
        appendField(line, row.images() == null ? "" : row.images());
        appendField(line, row.omitted() ? "yes" : "no");
        appendField(line, formatColumns(row.columns()));
        return line.toString();
    }

    /**
     * Sérialise les colonnes d'une table : {@code nom,type,longueur,échelle,nullable}
     * par colonne, colonnes séparées par {@code ;}. L'ordre de la liste porte l'ordre
     * (ordinal) — jamais un champ numérique séparé (même contrat que
     * {@code discovered_columns} côté control plane).
     */
    static String formatColumns(List<ColumnRow> columns) {
        if (columns == null || columns.isEmpty()) {
            return "";
        }
        return columns.stream()
                .map(column -> String.join(
                        ",",
                        column.name(),
                        column.type(),
                        column.length() == null ? "" : String.valueOf(column.length()),
                        column.scale() == null ? "" : String.valueOf(column.scale()),
                        column.nullable() ? "yes" : "no"))
                .collect(Collectors.joining(";"));
    }

    /** Reconstruit la liste de colonnes depuis le champ sérialisé par {@link #formatColumns}. */
    static List<ColumnRow> parseColumns(String field) {
        if (field == null || field.isEmpty()) {
            return List.of();
        }
        List<ColumnRow> columns = new ArrayList<>();
        for (String entry : field.split(";", -1)) {
            String[] parts = entry.split(",", -1);
            if (parts.length != 5) {
                throw new IllegalArgumentException("malformed discover column entry: " + entry);
            }
            columns.add(new ColumnRow(
                    parts[0],
                    parts[1],
                    parts[2].isEmpty() ? null : Long.valueOf(parts[2]),
                    parts[3].isEmpty() ? null : Long.valueOf(parts[3]),
                    "yes".equals(parts[4])));
        }
        return columns;
    }

    private static void appendField(StringBuilder line, String value) {
        if (value.indexOf('\t') >= 0 || value.indexOf('\n') >= 0 || value.indexOf('\r') >= 0) {
            throw new IllegalArgumentException("discover field must not contain control characters");
        }
        line.append(SEP).append(value);
    }

    /** Reconstruit une {@link DiscoveredTableRow} depuis une ligne de protocole — utilisé par les tests offline. */
    static DiscoveredTableRow parseRow(String line) {
        String[] parts = line.split("\t", -1);
        if (parts.length != 15 || !"table".equals(parts[0])) {
            throw new IllegalArgumentException("malformed discover row");
        }
        List<String> keyColumns = parts[8].isEmpty()
                ? List.of()
                : List.of(parts[8].split(",", -1));
        return new DiscoveredTableRow(
                parts[1],
                parts[2],
                parts[3],
                parts[4].isEmpty() ? null : parts[4],
                parts[5].isEmpty() ? null : Long.valueOf(parts[5]),
                parts[6].isEmpty() ? null : Long.valueOf(parts[6]),
                "yes".equals(parts[7]),
                keyColumns,
                "yes".equals(parts[9]),
                parts[10].isEmpty() ? null : parts[10],
                parts[11].isEmpty() ? null : parts[11],
                parts[12].isEmpty() ? null : parts[12],
                "yes".equals(parts[13]),
                parseColumns(parts[14]));
    }

    static Optional<String> images(String raw) {
        if (raw == null || raw.isBlank()) {
            return Optional.empty();
        }
        return Optional.of(raw.trim());
    }
}
