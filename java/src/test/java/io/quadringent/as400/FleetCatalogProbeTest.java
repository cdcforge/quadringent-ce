package io.quadringent.as400;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.sql.SQLNonTransientConnectionException;
import java.sql.SQLTimeoutException;
import java.util.ArrayList;
import java.util.List;
import java.util.OptionalInt;

import javax.net.ssl.SSLHandshakeException;

import com.ibm.as400.access.AS400;
import com.ibm.as400.access.AS400JDBCDataSource;
import com.ibm.as400.access.AS400SecurityException;
import com.ibm.as400.access.SecureAS400;

/**
 * Offline tests for fleet catalog allowlists, fail-closed validation and
 * deterministic JSON. Does not open a JDBC connection.
 */
public final class FleetCatalogProbeTest {
    /** Périmètre fictif des tests — données d'entrée, jamais des défauts du code. */
    private static final List<String> TEST_TABLES = List.of(
            "ADDRS1",
            "CAL001",
            "COST1",
            "CUSTOM1",
            "ORDER",
            "EXPENS",
            "DATE01",
            "SALE",
            "PLACE01",
            "PLACES",
            "CNTR",
            "PRODUCT",
            "HOLIDAYS");
    private static final FleetCatalogProbe.CatalogScope SCOPE =
            new FleetCatalogProbe.CatalogScope("DEV", "SALES", TEST_TABLES);

    private FleetCatalogProbeTest() {
    }

    public static void main(String[] args) throws Exception {
        testScopeComesFromEnvironmentOnly();
        testAllowlistExactOrder();
        testMissingTableFails();
        testDuplicateTableFails();
        testDistinctJournalsAreValid();
        testReceiverChainUncertaintyAndPreviousMismatch();
        testUnsafeIdentifiersFail();
        testColumnNullsStayNull();
        testDeterministicJsonAndForbiddenFields();
        testCanonicalTlsTrustMissingCaIsTlsFailedWithoutLeak();
        testOpenConnectionSourcePinsTlsTrust();
        testPreviousColumnsAreOptionalInSql();
        testSqlFailureClassificationNeverLeaksSecrets();
        testClassifyConnectFailureRanksClockMismatchBeforeConnectionFailed();
        testLoginTimeoutBoundedDefault();
        testPortedConfiguration9471And9476DoesNotConnect();
        testConfigurationFailureIsSafeConnectionFailed();
        System.out.println("FleetCatalogProbeTest passed");
    }

    private static void testScopeComesFromEnvironmentOnly() {
        java.util.Map<String, String> env = new java.util.HashMap<>(java.util.Map.of(
                "QUADRINGENT_ENVIRONMENT", "dev",
                "QUADRINGENT_SOURCE_SCHEMA", "sales",
                "QUADRINGENT_FLEET_TABLES", String.join(",", TEST_TABLES)));
        FleetCatalogProbe.CatalogScope scope = FleetCatalogProbe.CatalogScope.fromEnvironment(env);
        expect("environment uppercased", "DEV".equals(scope.environment()));
        expect("schema uppercased", "SALES".equals(scope.sourceSchema()));
        expect("scope tables preserve declared order", TEST_TABLES.equals(scope.tables()));
        for (java.util.Map<String, String> invalid : List.of(
                java.util.Map.<String, String>of(), // tout absent
                java.util.Map.of(
                        "QUADRINGENT_ENVIRONMENT", "dev",
                        "QUADRINGENT_SOURCE_SCHEMA", "SALES"), // tables absentes
                java.util.Map.of(
                        "QUADRINGENT_ENVIRONMENT", "dev",
                        "QUADRINGENT_SOURCE_SCHEMA", "SALES",
                        "QUADRINGENT_FLEET_TABLES", "SALE,SALE"), // doublon
                java.util.Map.of(
                        "QUADRINGENT_ENVIRONMENT", "dev",
                        "QUADRINGENT_SOURCE_SCHEMA", "SALES",
                        "QUADRINGENT_FLEET_TABLES", "SALE;DROP"), // identifiant hostile
                java.util.Map.of(
                        "QUADRINGENT_ENVIRONMENT", "dev",
                        "QUADRINGENT_SOURCE_SCHEMA", "SALES",
                        "QUADRINGENT_FLEET_TABLES", "  "))) { // liste vide
            boolean refused = false;
            try {
                FleetCatalogProbe.CatalogScope.fromEnvironment(invalid);
            }
            catch (IllegalArgumentException error) {
                refused = true;
            }
            expect("scope refuse " + invalid, refused);
        }
    }

    private static void testAllowlistExactOrder() {
        List<String> expected = TEST_TABLES;
        expect("allowlist size", SCOPE.tables().size() == 13);
        expect("allowlist order", expected.equals(SCOPE.tables()));
        expect("schema", "SALES".equals(SCOPE.sourceSchema()));
        expect("environment", "DEV".equals(SCOPE.environment()));
        expect("format", "quadringent-fleet-catalog-v1".equals(FleetCatalogProbe.FORMAT_VERSION));
        FleetCatalogProbe.requireAllowlist(expected, SCOPE.tables());
        FleetCatalogProbe.requireAllowlistOrder(expected, SCOPE.tables());
        String json = FleetCatalogProbe.closedJson(sampleCatalog(), SCOPE);
        int cursor = 0;
        for (String table : expected) {
            String needle = "\"name\":\"" + table + "\"";
            int found = json.indexOf(needle, cursor);
            expect("table order " + table, found >= cursor);
            cursor = found + needle.length();
        }
    }

    private static void testMissingTableFails() {
        List<String> missing = new ArrayList<>(TEST_TABLES);
        missing.remove("SALE");
        expectCode("missing allowlist", "MISSING_TABLE",
                () -> FleetCatalogProbe.requireAllowlist(missing, SCOPE.tables()));

        List<FleetCatalogProbe.TableRecord> tables = new ArrayList<>(sampleTables("JRNLIB", "DEMOJRN"));
        tables.remove(7);
        expectCode("missing table document", "MISSING_TABLE", () -> FleetCatalogProbe.validateCatalog(
                new FleetCatalogProbe.CatalogDocument(
                        FleetCatalogProbe.FORMAT_VERSION,
                        "2026-09-13T00:00:00Z",
                        SCOPE.environment(),
                        SCOPE.sourceSchema(),
                        List.of(journalChain("JRNLIB", "DEMOJRN", sampleReceivers("JRNLIB"))),
                        tables),
                SCOPE));
    }

    private static void testDuplicateTableFails() {
        List<String> duplicates = new ArrayList<>(TEST_TABLES);
        duplicates.set(1, "ADDRS1");
        expectCode("duplicate allowlist", "DUPLICATE_METADATA",
                () -> FleetCatalogProbe.requireAllowlist(duplicates, SCOPE.tables()));

        List<FleetCatalogProbe.TableRecord> tables = new ArrayList<>(sampleTables("JRNLIB", "DEMOJRN"));
        tables.set(1, table("ADDRS1", "JRNLIB", "DEMOJRN"));
        expectCode("duplicate table document", "DUPLICATE_METADATA", () -> FleetCatalogProbe.validateCatalog(
                new FleetCatalogProbe.CatalogDocument(
                        FleetCatalogProbe.FORMAT_VERSION,
                        "2026-09-13T00:00:00Z",
                        SCOPE.environment(),
                        SCOPE.sourceSchema(),
                        List.of(journalChain("JRNLIB", "DEMOJRN", sampleReceivers("JRNLIB"))),
                        tables),
                SCOPE));
    }

    private static void testDistinctJournalsAreValid() {
        List<FleetCatalogProbe.TableRecord> tables = new ArrayList<>(sampleTables("JRNLIB", "DEMOJRN"));
        tables.set(4, table("ORDER", "OTHERLIB", "OTHERJRN"));
        FleetCatalogProbe.CatalogDocument document = new FleetCatalogProbe.CatalogDocument(
                FleetCatalogProbe.FORMAT_VERSION,
                "2026-09-13T00:00:00Z",
                SCOPE.environment(),
                SCOPE.sourceSchema(),
                List.of(
                        journalChain("JRNLIB", "DEMOJRN", sampleReceivers("JRNLIB")),
                        journalChain("OTHERLIB", "OTHERJRN", sampleReceivers("OTHERLIB"))),
                tables);
        String json = FleetCatalogProbe.closedJson(document, SCOPE);
        expect("no shared journal object", !json.contains("\"journal\":{"));
        expect("journals array", json.contains("\"journals\":["));
        expect("first journal order", json.indexOf("\"library\":\"JRNLIB\"") < json.indexOf("\"library\":\"OTHERLIB\""));
        expect("order keeps own journal", json.contains("\"name\":\"ORDER\"") && json.contains("\"journal_library\":\"OTHERLIB\""));
        List<FleetCatalogProbe.JournalIdentity> unique = FleetCatalogProbe.uniqueJournalsInTableOrder(tables);
        expect("two journals", unique.size() == 2);
        expect("allowlist journal order", "JRNLIB".equals(unique.get(0).library()) && "OTHERLIB".equals(unique.get(1).library()));
    }

    private static void testReceiverChainUncertaintyAndPreviousMismatch() {
        FleetCatalogProbe.ReceiverRecord first = receiver("JRNLIB", "R1", "1", "10", "2026-01-01T00:00:00", null, null);
        FleetCatalogProbe.ReceiverRecord gapNoPrevious = receiver("JRNLIB", "R2", "12", "20", "2026-01-02T00:00:00", null, null);
        expect(
                "sequence gap without previous is uncertain",
                FleetCatalogProbe.CONTINUITY_UNCERTAIN.equals(
                        FleetCatalogProbe.assessReceiverChain(List.of(first, gapNoPrevious))));

        FleetCatalogProbe.ReceiverRecord nullBounds = receiver("JRNLIB", "R2", null, null, "2026-01-02T00:00:00", "JRNLIB", "R1");
        expect(
                "null bounds stay observed and uncertain",
                FleetCatalogProbe.CONTINUITY_UNCERTAIN.equals(
                        FleetCatalogProbe.assessReceiverChain(List.of(first, nullBounds))));

        FleetCatalogProbe.ReceiverRecord overlap = receiver("JRNLIB", "R2", "8", "20", "2026-01-02T00:00:00", "JRNLIB", "R1");
        expect(
                "overlap with previous link is proven",
                FleetCatalogProbe.CONTINUITY_PROVEN.equals(
                        FleetCatalogProbe.assessReceiverChain(List.of(first, overlap))));

        FleetCatalogProbe.ReceiverRecord gapWithPrevious = receiver(
                "JRNLIB", "R2", "12", "20", "2026-01-02T00:00:00", "JRNLIB", "R1");
        expect(
                "sequence gap with previous stays proven, bounds observed",
                FleetCatalogProbe.CONTINUITY_PROVEN.equals(
                        FleetCatalogProbe.assessReceiverChain(List.of(first, gapWithPrevious))));

        FleetCatalogProbe.ReceiverRecord windowStart = receiver(
                "JRNLIB", "R1", "1", "10", "2026-01-01T00:00:00", "JRNLIB", "R0");
        expect(
                "oldest receiver pointing before the window stays proven",
                FleetCatalogProbe.CONTINUITY_PROVEN.equals(
                        FleetCatalogProbe.assessReceiverChain(List.of(windowStart, overlap))));

        FleetCatalogProbe.ReceiverRecord wrongPrevious = receiver("JRNLIB", "R2", "11", "20", "2026-01-02T00:00:00", "JRNLIB", "RX");
        expectCode(
                "previous mismatch",
                "RECEIVER_DISCONTINUITY",
                () -> FleetCatalogProbe.assessReceiverChain(List.of(first, wrongPrevious)));

        expectCode("empty receivers", "RECEIVER_DISCONTINUITY",
                () -> FleetCatalogProbe.assessReceiverChain(List.of()));
    }

    private static void testUnsafeIdentifiersFail() {
        expectCode("sql injection table", "UNSAFE_IDENTIFIER",
                () -> FleetCatalogProbe.requireIdentifier("SALE;DROP"));
        expectCode("dotted identifier", "UNSAFE_IDENTIFIER",
                () -> FleetCatalogProbe.requireIdentifier("SALES.SALE"));
        expectCode("space identifier", "UNSAFE_IDENTIFIER",
                () -> FleetCatalogProbe.requireIdentifier("EV NT"));
        expectCode("blank identifier", "UNSAFE_IDENTIFIER",
                () -> FleetCatalogProbe.requireIdentifier(" "));
        expectCode("null identifier", "UNSAFE_IDENTIFIER",
                () -> FleetCatalogProbe.requireIdentifier(null));
        expect("safe identifier", "ADDRS1".equals(FleetCatalogProbe.requireIdentifier("ADDRS1")));
        expect("safe dollar", "QGPL$1".equals(FleetCatalogProbe.requireIdentifier("QGPL$1")));
        expect("safe IBM i system library", "DEMOLIB".equals(FleetCatalogProbe.requireIdentifier("DEMOLIB")));
    }

    private static void testColumnNullsStayNull() {
        FleetCatalogProbe.ColumnRecord unknown = new FleetCatalogProbe.ColumnRecord(
                "COL1", "VARCHAR", null, null, null, null, null, 1);
        FleetCatalogProbe.TableRecord table = new FleetCatalogProbe.TableRecord(
                "ADDRS1",
                10L,
                100L,
                1L,
                "JRNLIB",
                "DEMOJRN",
                "*BOTH",
                List.of(unknown),
                List.of(),
                List.of(new FleetCatalogProbe.IndexRecord(
                        "SALES",
                        "ADRB2BIX",
                        true,
                        false,
                        null,
                        List.of(new FleetCatalogProbe.IndexColumnRecord("COL1", 1, "A")))));
        List<FleetCatalogProbe.TableRecord> tables = new ArrayList<>(sampleTables("JRNLIB", "DEMOJRN"));
        tables.set(0, table);
        String json = FleetCatalogProbe.closedJson(new FleetCatalogProbe.CatalogDocument(
                FleetCatalogProbe.FORMAT_VERSION,
                "2026-09-13T00:00:00Z",
                SCOPE.environment(),
                SCOPE.sourceSchema(),
                List.of(journalChain("JRNLIB", "DEMOJRN", sampleReceivers("JRNLIB"))),
                tables), SCOPE);
        expect("length null", json.contains("\"length\":null"));
        expect("precision null", json.contains("\"numeric_precision\":null"));
        expect("scale null", json.contains("\"numeric_scale\":null"));
        expect("ccsid null", json.contains("\"ccsid\":null"));
        expect("nullable null", json.contains("\"nullable\":null"));
        int addrs1 = json.indexOf("\"name\":\"ADDRS1\"");
        int nextTable = json.indexOf("\"name\":\"CAL001\"");
        String adrb2bJson = json.substring(addrs1, nextTable);
        expect("unknown not coerced to zero", !adrb2bJson.contains("\"numeric_scale\":0"));
        expect("unknown length not coerced to zero", !adrb2bJson.contains("\"length\":0"));
        expect("unknown select_omit stays null", adrb2bJson.contains("\"select_omit\":null"));
    }

    private static void testDeterministicJsonAndForbiddenFields() {
        FleetCatalogProbe.CatalogDocument document = sampleCatalog();
        String first = FleetCatalogProbe.closedJson(document, SCOPE);
        String second = FleetCatalogProbe.closedJson(document, SCOPE);
        expect("deterministic json", first.equals(second));
        expect("closed object", first.startsWith("{") && first.endsWith("}"));
        expect("format version", first.contains("\"format_version\":\"quadringent-fleet-catalog-v1\""));
        expect("schema", first.contains("\"source_schema\":\"SALES\""));
        expect("environment", first.contains("\"environment\":\"DEV\""));
        expect("journal chain", first.contains("\"journals\":[{\"library\":\"JRNLIB\",\"name\":\"DEMOJRN\""));
        expect("single line json", !first.contains("\n") && !first.contains("\r"));
        FleetCatalogProbe.assertSafeCatalogJson(first);
        for (String key : FleetCatalogProbe.FORBIDDEN_JSON_KEYS) {
            expect("forbidden key " + key, !first.toLowerCase().contains("\"" + key + "\""));
        }
        expect("no entry data", !first.toLowerCase().contains("entry_data"));
        expect("no jdbc url", !first.contains("jdbc:as400"));
        expect("no select star", !first.toLowerCase().contains("select *"));
        expect("no password value", !first.contains("ISERIES_PASSWORD"));
        expectCode("forbidden payload field", "UNSAFE_IDENTIFIER",
                () -> FleetCatalogProbe.assertSafeCatalogJson("{\"payload\":\"secret-row\"}"));
        expectCode("forbidden sql field", "UNSAFE_IDENTIFIER",
                () -> FleetCatalogProbe.assertSafeCatalogJson("{\"sql\":\"SELECT * FROM SALES.SALE\"}"));
        expectCode("forbidden password field", "UNSAFE_IDENTIFIER",
                () -> FleetCatalogProbe.assertSafeCatalogJson("{\"password\":\"x\"}"));
    }

    private static void testCanonicalTlsTrustMissingCaIsTlsFailedWithoutLeak() throws Exception {
        // Sans TLS demandé : jamais d'installation, quel que soit l'environnement.
        TlsTrust.install(false);

        // Objectif A (2026-09-24) : AS400_TLS_CA_FILE absent/vide -> aucune
        // exception, aucune installation — le magasin de confiance
        // système/JVM par défaut reste en place (suffisant pour une
        // autorité publique). C'est exactement le cas qui plantait avant ce
        // correctif (le Dockerfile posait un chemin par défaut sans fichier
        // livré dans l'image).
        TlsTrust.installFrom(true, null);
        TlsTrust.installFrom(true, "");
        TlsTrust.installFrom(true, "   ");
        expect("unset ca file resolves to null", TlsTrust.caFile(null) == null);
        expect("blank ca file resolves to null", TlsTrust.caFile("  ") == null);

        // AS400_TLS_CA_FILE déclaré mais introuvable : échec explicite,
        // jamais un trust-all, jamais une exception non typée.
        Path missingCa = Files.createTempDirectory("tls-trust-test").resolve("does-not-exist.pem");
        IllegalArgumentException missing = null;
        try {
            TlsTrust.installFrom(true, missingCa.toString());
            throw new AssertionError("missing declared CA must fail closed");
        }
        catch (IllegalArgumentException error) {
            missing = error;
        }
        String leaked = missing.getMessage() == null ? "" : missing.getMessage();
        String line = FleetCatalogProbe.formatFailure(missing);
        expect("missing ca is tls failed", line.equals("fleet_catalog_error=TLS_FAILED"));
        expect("missing ca line has no message", !line.contains(leaked) || leaked.isBlank());
        expect("missing ca line has no path", !line.contains(missingCa.toString()));
        expect("missing ca line has no env", !line.contains("AS400_TLS_CA_FILE"));

        // installTls(true) sans variable posée dans l'environnement réel de
        // ce process (jamais réglée pour ces tests) doit réussir sans lever.
        FleetCatalogProbe.installTls(true);

        String original = System.getProperty("com.ibm.as400.access.SSLTrustAll");
        System.setProperty("com.ibm.as400.access.SSLTrustAll", "true");
        try {
            boolean failed = false;
            try {
                FleetCatalogProbe.installTls(true);
            }
            catch (FleetCatalogProbe.ProbeFailure error) {
                failed = "TLS_FAILED".equals(error.code());
            }
            expect("trust-all rejected", failed);
        }
        finally {
            if (original == null) {
                System.clearProperty("com.ibm.as400.access.SSLTrustAll");
            }
            else {
                System.setProperty("com.ibm.as400.access.SSLTrustAll", original);
            }
        }
    }

    private static void testOpenConnectionSourcePinsTlsTrust() throws Exception {
        String sourcePath = System.getenv("FLEET_CATALOG_SOURCE");
        if (sourcePath == null || sourcePath.isBlank()) {
            throw new AssertionError("FLEET_CATALOG_SOURCE is required");
        }
        String source = Files.readString(Path.of(sourcePath), StandardCharsets.UTF_8);
        int installAt = source.indexOf("installTls(settings.tls())");
        int canonicalAt = source.indexOf("TlsTrust.install(tls)");
        int connectAt = source.indexOf("getConnection");
        expect("tls install present", installAt >= 0);
        expect("canonical tls called", canonicalAt >= 0);
        expect("tls install before jdbc", installAt < connectAt);
        expect("secure as400", source.contains("new SecureAS400("));
        expect("setSecure follows tls", source.contains("dataSource.setSecure(settings.tls())"));
        expect("plaintext gated", source.contains("AS400_ALLOW_PLAINTEXT"));
        expect("no trust-all installer", !source.contains("TrustAll") || source.contains("SSLTrustAll"));
        expect("does not disable hostname verification", !source.contains("setHostnameVerifier"));
        expect("does not hardcode setSecure false", !source.contains("setSecure(false)"));
        expect("metadata catalogs", source.contains("FROM QSYS2.SYSCOLUMNS")
                && source.contains("FROM QSYS2.JOURNALED_OBJECTS")
                && source.contains("FROM QSYS2.JOURNAL_RECEIVER_INFO"));
        expect("no entry_data column", !source.contains("SELECT ENTRY_DATA") && !source.contains("ENTRY_DATA,"));
        expect("no mixed journal fail-closed", !source.contains("MIXED_JOURNAL"));
        expect("previous columns probed via syscolumns", source.contains("receiverLinkColumns"));
        expect("previous sql is optional", source.contains("journalReceiverInfoSql(ReceiverLinkColumns"));
        expect("no syspartitionindexes", !source.contains("SYSPARTITIONINDEXES"));
        expect("sysindexes required", source.contains("FROM QSYS2.SYSINDEXES"));
        expect("syskeys required", source.contains("FROM QSYS2.SYSKEYS"));
        expect("stats views only if queried",
                !source.contains("SYSTABLEINDEXSTAT") && !source.contains("SYSPARTITIONINDEXSTAT"));
        expect("sql failures go through formatFailure", source.contains("System.err.println(formatFailure(error))"));
        expect("main does not print exception class", !source.contains("error.getClass().getSimpleName()"));
        expect("no nested tls trust", !source.contains("static final class TlsTrust"));
        expect("uses canonical tls trust", source.contains("TlsTrust.install(tls)"));
        expect("login timeout applied", source.contains("DriverManager.setLoginTimeout(seconds)"));
        expect("jdbc login timeout property", source.contains("properties.setProperty(\"login timeout\""));
        expect("no datasource login timeout", !source.contains("dataSource.setLoginTimeout"));
        expect("no socket properties", !source.contains("SocketProperties"));
        expect("no socket timeout api", !source.contains("setSocketTimeout"));
        expect("no so timeout api", !source.contains("setSoTimeout"));
        expect("no validate signon timeout", !source.contains("setvalidateSignonTimeOut"));
        // Le périmètre est déclaré par l'environnement, jamais figé : aucun
        // littéral d'installation ne doit subsister dans la source livrée.
        expect("no hardcoded source schema", !source.contains("\"SALES\""));
        expect("no hardcoded environment", !source.contains("\"DEV\""));
        expect("no hardcoded journal", !source.contains("DEMOJRN"));
        expect("no hardcoded allowlist", !source.contains("ALLOWED_TABLES"));
        expect("no hardcoded table name", !source.contains("\"ADDRS1\""));
        expect("scope from declared environment", source.contains("CatalogScope.fromEnvironment()"));
        expect("declared environment variable", source.contains("QUADRINGENT_ENVIRONMENT"));
        expect("declared source schema variable", source.contains("QUADRINGENT_SOURCE_SCHEMA"));
        expect("declared fleet tables variable", source.contains("QUADRINGENT_FLEET_TABLES"));
        Path trust = Path.of(sourcePath).resolveSibling("TlsTrust.java");
        expect("canonical tls trust source present", Files.isRegularFile(trust));
        String trustSource = Files.readString(trust, StandardCharsets.UTF_8);
        expect("canonical loads ca file", trustSource.contains("AS400_TLS_CA_FILE"));
        // Plus de chemin par défaut trompeur (objectif A, 2026-09-24) :
        // variable absente/vide -> magasin système/JVM par défaut, jamais un
        // fichier implicite qui n'existe pas forcément dans l'image.
        expect("no misleading default ca path", !trustSource.contains("/app/certs/ibmi-ca.pem"));
        expect("canonical refuses private key", trustSource.contains("PRIVATE KEY"));
    }

    private static void testLoginTimeoutBoundedDefault() throws Exception {
        expect("default login timeout", FleetCatalogProbe.parseLoginTimeoutSeconds(null) == 15);
        expect("blank login timeout", FleetCatalogProbe.parseLoginTimeoutSeconds("  ") == 15);
        expect("configured 10s", FleetCatalogProbe.parseLoginTimeoutSeconds("10") == 10);
        boolean rejectedLow = false;
        boolean rejectedHigh = false;
        try {
            FleetCatalogProbe.parseLoginTimeoutSeconds("0");
        }
        catch (IllegalArgumentException error) {
            rejectedLow = true;
        }
        try {
            FleetCatalogProbe.parseLoginTimeoutSeconds("80");
        }
        catch (IllegalArgumentException error) {
            rejectedHigh = true;
        }
        expect("reject 0s", rejectedLow);
        expect("reject 80s", rejectedHigh);
        int previous = DriverManager.getLoginTimeout();
        try {
            FleetCatalogProbe.applyLoginTimeout(15);
            expect("driver manager login timeout", DriverManager.getLoginTimeout() == 15);
            AS400JDBCDataSource dataSource = new AS400JDBCDataSource();
            expect("datasource login timeout stays default", dataSource.getLoginTimeout() == 0);
            expect("datasource socket timeout stays default", dataSource.getSocketTimeout() == 0);
        }
        finally {
            DriverManager.setLoginTimeout(previous);
        }
    }

    private static void testPortedConfiguration9471And9476DoesNotConnect() {
        FleetCatalogProbe.Settings settings = new FleetCatalogProbe.Settings(
                "127.0.0.1", "OFFLINE", "PASSWORD", true,
                OptionalInt.of(9471), OptionalInt.of(9476), OptionalInt.empty());
        FleetCatalogProbe.PortedJdbc ported = FleetCatalogProbe.configurePortedDataSource(settings);
        try {
            expect("ported uses secure as400", ported.system() instanceof SecureAS400);
            expect("database port 9471", ported.system().getServicePort(AS400.DATABASE) == 9471);
            expect("signon port 9476", ported.system().getServicePort(AS400.SIGNON) == 9476);
            expect("datasource secure", ported.dataSource().isSecure());
            expect("datasource login timeout not applied", ported.dataSource().getLoginTimeout() == 0);
            expect("datasource socket timeout not applied", ported.dataSource().getSocketTimeout() == 0);
        }
        finally {
            ported.system().disconnectAllServices();
        }
    }

    private static void testConfigurationFailureIsSafeConnectionFailed() {
        FleetCatalogProbe.ProbeFailure mapped = FleetCatalogProbe.configurationFailure(
                new IllegalStateException("sensitive configuration"));
        expect("config failure code", "CONNECTION_FAILED".equals(mapped.code()));
        expect("config failure has no cause", mapped.getCause() == null);
        expect("config failure stderr", mapped.stderrLine().equals("fleet_catalog_error=CONNECTION_FAILED"));
        FleetCatalogProbe.ProbeFailure preserved = FleetCatalogProbe.configurationFailure(
                new FleetCatalogProbe.ProbeFailure("TLS_FAILED"));
        expect("probe failure preserved", "TLS_FAILED".equals(preserved.code()));
    }

    private static void testSqlFailureClassificationNeverLeaksSecrets() {
        String secret = "password=hunter2-secret user=CDCUSER host=192.0.2.10 "
                + "jdbc:as400://192.0.2.10/SALES SELECT * FROM SALES.SALE";
        expectSafeSqlFailure("connection failed", new SQLNonTransientConnectionException(secret, "08001", -4499),
                "CONNECTION_FAILED", "08001", -4499, secret);
        SQLNonTransientConnectionException disabled = new SQLNonTransientConnectionException(
                "User ID is disabled.:CDCUSER " + secret, "08001", 0);
        disabled.initCause(new TestSecurityException(AS400SecurityException.USERID_DISABLE));
        expectSafeSqlFailure("user disabled", disabled, "USER_DISABLED", "08001", 31, secret);
        expectSafeSqlFailure("user disabled from known phrase", new SQLNonTransientConnectionException(
                "User ID is disabled.:CDCUSER " + secret, "08001", 0),
                "USER_DISABLED", "08001", null, secret);
        SQLException auth = new SQLException(secret, "28000", 0);
        auth.initCause(new TestSecurityException(AS400SecurityException.PASSWORD_INCORRECT));
        expectSafeSqlFailure("authentication failed", auth, "AUTHENTICATION_FAILED", "28000", 8, secret);
        SQLNonTransientConnectionException tls = new SQLNonTransientConnectionException(secret, "08S01", 0);
        tls.initCause(new SSLHandshakeException(secret));
        expectSafeSqlFailure("tls failed", tls, "TLS_FAILED", "08S01", null, secret);
        expectSafeSqlFailure("query timeout", new SQLTimeoutException(secret, "HYT00", 0),
                "QUERY_TIMEOUT", "HYT00", null, secret);
        expectSafeSqlFailure("query failed", new SQLException(secret, "42704", -204),
                "QUERY_FAILED", "42704", -204, secret);
        expectSafeSqlFailure("unknown sql", new SQLException(secret), "UNKNOWN_SQL", null, null, secret);

        SQLException poisonedState = new SQLNonTransientConnectionException(secret, "08001;DROP", Integer.MAX_VALUE);
        String poisoned = FleetCatalogProbe.formatFailure(poisonedState);
        expect("invalid sqlstate omitted", !poisoned.contains("sqlstate="));
        expect("out of range vendor omitted", !poisoned.contains("vendor="));
        expect("still connection", poisoned.equals("fleet_catalog_error=CONNECTION_FAILED"));
        expectNoSecretLeak("poisoned state", poisoned, secret);
        String tlsLine = FleetCatalogProbe.formatFailure(new SSLHandshakeException(secret));
        expect("ssl without sql", tlsLine.equals("fleet_catalog_error=TLS_FAILED"));
        expectNoSecretLeak("ssl without sql", tlsLine, secret);
    }

    private static void testClassifyConnectFailureRanksClockMismatchBeforeConnectionFailed() {
        expect(
                "clock mismatch classified as SOURCE_CLOCK_MISMATCH",
                "SOURCE_CLOCK_MISMATCH".equals(FleetCatalogProbe.classifyConnectFailure(
                        new SourceClockMismatchException("AS400_SOURCE_TIME_ZONE does not match source UTC offset"))));
        expect(
                "clock mismatch wrapped as a cause is still classified",
                "SOURCE_CLOCK_MISMATCH".equals(FleetCatalogProbe.classifyConnectFailure(
                        new RuntimeException("connect failed", new SourceClockMismatchException("mismatch")))));
        expect(
                "generic connection failure still falls back to CONNECTION_FAILED",
                "CONNECTION_FAILED".equals(FleetCatalogProbe.classifyConnectFailure(
                        new IllegalStateException("bounded journal window did not reach its end"))));
        expect(
                "user disabled outranks a clock mismatch raised alongside it",
                "USER_DISABLED".equals(FleetCatalogProbe.classifyConnectFailure(
                        new RuntimeException(
                                new TestSecurityException(AS400SecurityException.USERID_DISABLE)))));
    }

    private static void expectSafeSqlFailure(String name, SQLException error, String code,
            String sqlState, Integer vendor, String secret) {
        String line = FleetCatalogProbe.formatFailure(error);
        String expected = "fleet_catalog_error=" + code;
        if (sqlState != null) expected += " sqlstate=" + sqlState;
        if (vendor != null) expected += " vendor=" + vendor;
        expect(name + " line", line.equals(expected));
        expectNoSecretLeak(name, line, secret);
    }

    private static void expectNoSecretLeak(String name, String line, String secret) {
        String lowered = line.toLowerCase();
        expect(name + " closed prefix", line.startsWith("fleet_catalog_error="));
        expect(name + " no raw secret", !line.contains(secret));
        expect(name + " no password value", !lowered.contains("hunter2"));
        expect(name + " no user", !line.contains("CDCUSER"));
        expect(name + " no host", !line.contains("192.0.2.10"));
        expect(name + " no jdbc", !lowered.contains("jdbc:as400"));
        expect(name + " no sql text", !lowered.contains("select *"));
        expect(name + " no getMessage dump", !line.contains("password="));
    }

    private static final class TestSecurityException extends AS400SecurityException {
        private static final long serialVersionUID = 1L;
        private TestSecurityException(int returnCode) { super(returnCode); }
    }

    private static void testPreviousColumnsAreOptionalInSql() {
        FleetCatalogProbe.ReceiverLinkColumns columns = new FleetCatalogProbe.ReceiverLinkColumns(
                "PREVIOUS_JOURNAL_RECEIVER_LIBRARY", "PREVIOUS_JOURNAL_RECEIVER");
        String withPrevious = FleetCatalogProbe.journalReceiverInfoSql(columns);
        String withoutPrevious = FleetCatalogProbe.journalReceiverInfoSql(null);
        expect("optional previous library selected", withPrevious.contains("PREVIOUS_JOURNAL_RECEIVER_LIBRARY"));
        expect("optional previous name selected", withPrevious.contains("PREVIOUS_JOURNAL_RECEIVER\n"));
        expect("previous columns omitted when unavailable", !withoutPrevious.contains("PREVIOUS_"));
        expect("stable columns remain", withoutPrevious.contains("FIRST_SEQUENCE_NUMBER"));
        expect("attach order remains", withoutPrevious.contains("ORDER BY ATTACH_TIMESTAMP DESC"));
        String json = FleetCatalogProbe.closedJson(sampleCatalog(), SCOPE);
        expect("observed previous null stays null", json.contains("\"previous_library\":null"));
        expect("observed previous name null stays null", json.contains("\"previous_name\":null"));
        expect("observed first bound kept", json.contains("\"first_sequence\":\"1\""));
        expect("observed last bound kept", json.contains("\"last_sequence\":\"10\""));
    }

    private static FleetCatalogProbe.CatalogDocument sampleCatalog() {
        return new FleetCatalogProbe.CatalogDocument(
                FleetCatalogProbe.FORMAT_VERSION,
                "2026-09-13T00:00:00Z",
                SCOPE.environment(),
                SCOPE.sourceSchema(),
                List.of(journalChain("JRNLIB", "DEMOJRN", sampleReceivers("JRNLIB"))),
                sampleTables("JRNLIB", "DEMOJRN"));
    }

    private static FleetCatalogProbe.JournalChain journalChain(
            String library,
            String name,
            List<FleetCatalogProbe.ReceiverRecord> receivers) {
        return new FleetCatalogProbe.JournalChain(
                library,
                name,
                FleetCatalogProbe.assessReceiverChain(receivers),
                receivers);
    }

    private static List<FleetCatalogProbe.ReceiverRecord> sampleReceivers(String library) {
        return List.of(
                receiver(library, "R1", "1", "10", "2026-01-01T00:00:00", null, null),
                receiver(library, "R2", "11", "20", "2026-01-02T00:00:00", library, "R1"));
    }

    private static List<FleetCatalogProbe.TableRecord> sampleTables(String journalLibrary, String journalName) {
        List<FleetCatalogProbe.TableRecord> tables = new ArrayList<>();
        for (String name : TEST_TABLES) {
            tables.add(table(name, journalLibrary, journalName));
        }
        return tables;
    }

    private static FleetCatalogProbe.TableRecord table(String name, String journalLibrary, String journalName) {
        return new FleetCatalogProbe.TableRecord(
                name,
                10L,
                100L,
                1L,
                journalLibrary,
                journalName,
                "*BOTH",
                List.of(new FleetCatalogProbe.ColumnRecord("COL1", "INTEGER", 4L, 10L, 0L, 37L, Boolean.FALSE, 1)),
                List.of(new FleetCatalogProbe.ConstraintRecord(
                        "SALES",
                        name + "PK",
                        "PRIMARY KEY",
                        List.of("COL1"))),
                List.of(new FleetCatalogProbe.IndexRecord(
                        "SALES",
                        name + "IX",
                        true,
                        false,
                        false,
                        List.of(new FleetCatalogProbe.IndexColumnRecord("COL1", 1, "A")))));
    }

    private static FleetCatalogProbe.ReceiverRecord receiver(
            String library,
            String name,
            String first,
            String last,
            String attach,
            String previousLibrary,
            String previousName) {
        return new FleetCatalogProbe.ReceiverRecord(
                library,
                name,
                "ONLINE",
                first,
                last,
                attach,
                "R1".equals(name) ? "2026-01-02T00:00:00" : null,
                previousLibrary,
                previousName);
    }

    private static void expect(String name, boolean condition) {
        if (!condition) {
            throw new AssertionError(name);
        }
    }

    private static void expectCode(String name, String code, Runnable action) {
        try {
            action.run();
            throw new AssertionError(name + " expected " + code);
        }
        catch (FleetCatalogProbe.ProbeFailure failure) {
            if (!code.equals(failure.code())) {
                throw new AssertionError(name + " expected " + code + " got " + failure.code());
            }
        }
    }
}
