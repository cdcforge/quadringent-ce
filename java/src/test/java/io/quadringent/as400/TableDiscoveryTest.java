package io.quadringent.as400;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Tests offline (aucune connexion JDBC) des helpers purs de {@link TableDiscovery} :
 * parsing de requête, construction SQL bornée, et protocole ligne
 * (formatage/parsing round-trip).
 */
public final class TableDiscoveryTest {
    private static int checks = 0;

    private TableDiscoveryTest() {
    }

    public static void main(String[] args) {
        testLibrariesFallBackToCurrentLibrary();
        testLibrariesCsvDeduplicatesAndValidates();
        testUnsafeLibraryRejected();
        testLimitDefaultsAndBounds();
        testSearchNormalizedAndLengthBounded();
        testDiscoverySqlContainsBoundedLibrariesAndFetchFirst();
        testDiscoverySqlEscapesSearchPattern();
        testDiscoverySqlMatchesDb2ForICatalog();
        testFormatAndParseRoundTrip();
        testFormatRejectsControlCharacters();
        testMalformedRowRejected();
        testColumnsSqlIsBoundedAndUsesSyscolumns();
        testFormatAndParseColumnsRoundTrip();
        testColumnsFieldOmittedWhenEmpty();
        testMalformedColumnsFieldRejected();
        System.out.println("TableDiscoveryTest: " + checks + " checks passed");
    }

    private static void check(boolean condition, String message) {
        checks++;
        if (!condition) {
            throw new AssertionError(message);
        }
    }

    private static void testLibrariesFallBackToCurrentLibrary() {
        List<String> libs = TableDiscovery.parseLibraries(null, "SALES");
        check(libs.equals(List.of("SALES")), "should default to current library");
        try {
            TableDiscovery.parseLibraries(null, null);
            throw new AssertionError("should require a library");
        }
        catch (IllegalArgumentException expected) {
            check(true, "no library available");
        }
    }

    private static void testLibrariesCsvDeduplicatesAndValidates() {
        List<String> libs = TableDiscovery.parseLibraries("SALES, QGPL,sales", "IGNORED");
        check(libs.size() == 2, "duplicates (case-insensitive) collapse: " + libs);
        check(libs.get(0).equals("SALES") && libs.get(1).equals("QGPL"), "order preserved: " + libs);
    }

    private static void testUnsafeLibraryRejected() {
        try {
            TableDiscovery.parseLibraries("SALES; DROP TABLE X", "IGNORED");
            throw new AssertionError("unsafe identifier must fail");
        }
        catch (IllegalArgumentException expected) {
            check(true, "unsafe library rejected");
        }
    }

    private static void testLimitDefaultsAndBounds() {
        check(TableDiscovery.parseLimit(null) == TableDiscovery.DEFAULT_LIMIT, "default limit");
        check(TableDiscovery.parseLimit("10") == 10, "explicit limit");
        try {
            TableDiscovery.parseLimit("0");
            throw new AssertionError("limit below range must fail");
        }
        catch (IllegalArgumentException expected) {
            check(true, "limit lower bound enforced");
        }
        try {
            TableDiscovery.parseLimit(String.valueOf(TableDiscovery.MAX_LIMIT + 1));
            throw new AssertionError("limit above range must fail");
        }
        catch (IllegalArgumentException expected) {
            check(true, "limit upper bound enforced");
        }
    }

    private static void testSearchNormalizedAndLengthBounded() {
        check(TableDiscovery.normalizeSearch(null) == null, "blank search is null");
        check(TableDiscovery.normalizeSearch("  ") == null, "whitespace-only search treated as absent");
        check("CUST".equals(TableDiscovery.normalizeSearch("  CUST  ")), "search trimmed");
        try {
            TableDiscovery.normalizeSearch("x".repeat(129));
            throw new AssertionError("overlong search must fail");
        }
        catch (IllegalArgumentException expected) {
            check(true, "search length bounded");
        }
    }

    private static void testDiscoverySqlContainsBoundedLibrariesAndFetchFirst() {
        TableDiscovery.DiscoverRequest request =
                new TableDiscovery.DiscoverRequest(List.of("SALES", "QGPL"), 25, null);
        String sql = TableDiscovery.discoverySql(request);
        check(sql.contains("'SALES', 'QGPL'"), "libraries interpolated as literals: " + sql);
        check(sql.contains("FETCH FIRST 25 ROWS ONLY"), "bounded fetch: " + sql);
        check(sql.contains("QSYS2.SYSTABLES"), "uses SYSTABLES");
        check(sql.contains("QSYS2.JOURNALED_OBJECTS"), "uses JOURNALED_OBJECTS");
        check(sql.contains("QSYS2.SYSKEYCST"), "uses SYSKEYCST");
        check(!sql.toUpperCase().contains("DROP") && !sql.toUpperCase().contains("DELETE"),
                "read-only catalog query");
    }

    /**
     * Constaté sur un IBM i 7.5 réel : XMLCAST(XMLAGG(...)) est refusé (SQ20338),
     * JOURNALED_OBJECTS expose OBJECT_LIBRARY (pas OBJECT_SCHEMA), FILE_TYPE vaut
     * 'D' pour les données, et les fichiers physiques DDS ont TABLE_TYPE = 'P'.
     */
    private static void testDiscoverySqlMatchesDb2ForICatalog() {
        String sql = TableDiscovery.discoverySql(
                new TableDiscovery.DiscoverRequest(List.of("SALES"), 10, null));
        check(!sql.contains("XMLCAST"), "no XMLCAST on Db2 for i: " + sql);
        check(sql.contains("LISTAGG(kc.COLUMN_NAME, ',') WITHIN GROUP (ORDER BY kc.ORDINAL_POSITION)"),
                "key columns aggregated with LISTAGG: " + sql);
        check(sql.contains("j.OBJECT_LIBRARY = t.TABLE_SCHEMA"), "JOURNALED_OBJECTS joined on OBJECT_LIBRARY");
        check(!sql.contains("OBJECT_SCHEMA"), "no OBJECT_SCHEMA column");
        check(sql.contains("t.TABLE_TYPE IN ('T', 'P')"), "SQL tables and DDS physical files");
        check(sql.contains("t.FILE_TYPE = 'D'"), "data files only");
    }

    private static void testDiscoverySqlEscapesSearchPattern() {
        TableDiscovery.DiscoverRequest request =
                new TableDiscovery.DiscoverRequest(List.of("SALES"), 5, "100%_'x");
        String sql = TableDiscovery.discoverySql(request);
        check(sql.contains("100\\%\\_\\'x"), "LIKE pattern escaped: " + sql);
        check(sql.contains("ESCAPE '\\'"), "escape character declared");
    }

    private static void testFormatAndParseRoundTrip() {
        TableDiscovery.DiscoveredTableRow row = new TableDiscovery.DiscoveredTableRow(
                "SALES",
                "ORDHDR",
                "ORDER_HEADER",
                "En-tête de commande",
                1200L,
                65536L,
                true,
                List.of("ORDER_ID", "LINE_NO"),
                true,
                "SALES",
                "ORDJRN",
                "*BOTH",
                false,
                List.of(
                        new TableDiscovery.ColumnRow("ORDER_ID", "DECIMAL", 7L, 0L, false),
                        new TableDiscovery.ColumnRow("CUSTOMER_NAME", "VARCHAR", 60L, null, true)));
        String line = TableDiscovery.formatRow(row);
        check(line.startsWith("table\t"), "row line starts with tag: " + line);
        TableDiscovery.DiscoveredTableRow parsed = TableDiscovery.parseRow(line);
        check(parsed.equals(row), "round-trip preserves the row: " + parsed);
    }

    private static void testFormatRejectsControlCharacters() {
        TableDiscovery.DiscoveredTableRow row = new TableDiscovery.DiscoveredTableRow(
                "SALES", "ORDHDR", "ORDER_HEADER", "texte\tavec tab", null, null, false, List.of(),
                false, null, null, null, false, List.of());
        try {
            TableDiscovery.formatRow(row);
            throw new AssertionError("tab in field must fail");
        }
        catch (IllegalArgumentException expected) {
            check(true, "control character rejected");
        }
    }

    private static void testMalformedRowRejected() {
        try {
            TableDiscovery.parseRow("not-a-discover-row");
            throw new AssertionError("malformed row must fail");
        }
        catch (IllegalArgumentException expected) {
            check(true, "malformed row rejected");
        }
        Map<String, String> unused = new LinkedHashMap<>();
        check(unused.isEmpty(), "sanity");
    }

    private static void testColumnsSqlIsBoundedAndUsesSyscolumns() {
        String sql = TableDiscovery.columnsSql(List.of("SALES", "QGPL"), 2000);
        check(sql.contains("QSYS2.SYSCOLUMNS"), "uses SYSCOLUMNS: " + sql);
        check(sql.contains("'SALES', 'QGPL'"), "libraries interpolated as literals: " + sql);
        check(sql.contains("FETCH FIRST 2000 ROWS ONLY"), "bounded fetch: " + sql);
        check(sql.contains("ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"), "ordered by ordinal");
        check(!sql.toUpperCase().contains("DROP") && !sql.toUpperCase().contains("DELETE"),
                "read-only catalog query");
    }

    private static void testFormatAndParseColumnsRoundTrip() {
        List<TableDiscovery.ColumnRow> columns = List.of(
                new TableDiscovery.ColumnRow("ORDER_ID", "DECIMAL", 7L, 0L, false),
                new TableDiscovery.ColumnRow("CUSTOMER_NAME", "VARCHAR", 60L, null, true));
        String field = TableDiscovery.formatColumns(columns);
        check(field.equals("ORDER_ID,DECIMAL,7,0,no;CUSTOMER_NAME,VARCHAR,60,,yes"),
                "columns serialized name,type,length,scale,nullable: " + field);
        List<TableDiscovery.ColumnRow> parsed = TableDiscovery.parseColumns(field);
        check(parsed.equals(columns), "columns round-trip preserves order and values: " + parsed);
    }

    private static void testColumnsFieldOmittedWhenEmpty() {
        check(TableDiscovery.formatColumns(List.of()).isEmpty(), "no columns serializes to empty field");
        check(TableDiscovery.parseColumns("").isEmpty(), "empty field parses to no columns");
        check(TableDiscovery.parseColumns(null).isEmpty(), "null field parses to no columns");
    }

    private static void testMalformedColumnsFieldRejected() {
        try {
            TableDiscovery.parseColumns("ORDER_ID,DECIMAL,7");
            throw new AssertionError("malformed column entry must fail");
        }
        catch (IllegalArgumentException expected) {
            check(true, "malformed column entry rejected");
        }
    }
}
