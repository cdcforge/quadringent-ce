package io.quadringent.as400;

import java.io.IOException;
import java.io.PrintStream;
import java.math.BigInteger;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.SQLTimeoutException;
import java.sql.Statement;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.OptionalInt;
import java.util.Properties;
import java.util.Set;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import com.ibm.as400.access.AS400;
import com.ibm.as400.access.AS400DataType;
import com.ibm.as400.access.AS400Date;
import com.ibm.as400.access.AS400JDBCDataSource;
import com.ibm.as400.access.AS400Structure;
import com.ibm.as400.access.AS400Text;
import com.ibm.as400.access.AS400Time;
import com.ibm.as400.access.SecureAS400;

import io.debezium.ibmi.db2.journal.retrieve.Connect;
import io.debezium.ibmi.db2.journal.retrieve.FileFilter;
import io.debezium.ibmi.db2.journal.retrieve.JdbcFileDecoder;
import io.debezium.ibmi.db2.journal.retrieve.JournalEntryType;
import io.debezium.ibmi.db2.journal.retrieve.JournalInfo;
import io.debezium.ibmi.db2.journal.retrieve.JournalInfoRetrieval;
import io.debezium.ibmi.db2.journal.retrieve.JournalPosition;
import io.debezium.ibmi.db2.journal.retrieve.JournalProcessedPosition;
import io.debezium.ibmi.db2.journal.retrieve.JournalReceiver;
import io.debezium.ibmi.db2.journal.retrieve.PositionRange;
import io.debezium.ibmi.db2.journal.retrieve.RetrieveConfig;
import io.debezium.ibmi.db2.journal.retrieve.RetrieveConfigBuilder;
import io.debezium.ibmi.db2.journal.retrieve.RetrieveJournal;
import io.debezium.ibmi.db2.journal.retrieve.RetrievalState;
import io.debezium.ibmi.db2.journal.retrieve.SchemaCacheHash;
import io.debezium.ibmi.db2.journal.retrieve.SchemaCacheIF.TableInfo;
import io.debezium.ibmi.db2.journal.retrieve.rjne0200.EntryHeader;
import io.debezium.ibmi.db2.journal.retrieve.rjne0200.FirstHeader;
import io.debezium.ibmi.db2.journal.retrieve.rjne0200.OffsetStatus;

/**
 * Persistent IBM i journal session: one AS400, JDBC and RetrieveJournal for
 * many bounded windows. Fail-closed before raw and checkpoint.
 */
public final class JournalSession implements AutoCloseable {
    static final int DEFAULT_JOURNAL_BUFFER_SIZE = 16_000_000;
    static final int MIN_JOURNAL_BUFFER_SIZE = 131_072;
    static final int MAX_JOURNAL_BUFFER_SIZE = 16_000_000;
    static final int DEFAULT_MAX_SERVER_SIDE_ENTRIES = 1_000_000;
    static final int MAX_CAPTURE_TABLES = 32;

    private final ConnectionSettings settings;
    private final AS400 as400;
    private final Connection jdbc;
    private final RetrieveConfig retrieveConfig;
    private final JournalInfoRetrieval journalRetrieval;
    private final RrnAwareFileDecoder decoder;
    private final JournalInfo journal;

    private JournalSession(
            ConnectionSettings settings,
            AS400 as400,
            Connection jdbc,
            RetrieveConfig retrieveConfig,
            JournalInfoRetrieval journalRetrieval,
            RrnAwareFileDecoder decoder,
            JournalInfo journal) {
        this.settings = settings;
        this.as400 = as400;
        this.jdbc = jdbc;
        this.retrieveConfig = retrieveConfig;
        this.journalRetrieval = journalRetrieval;
        this.decoder = decoder;
        this.journal = journal;
    }

    /**
     * Minimal IBM i endpoint contract shared by {@link ConnectionSettings} (full
     * capture settings) and {@link DiagnosticWorker}'s lighter settings — the
     * connection plumbing ({@link #newAs400}, {@link #configureServicePorts},
     * {@link #openJdbcConnection}) needs only these fields, never the captured
     * schema/table or the journal timestamp policy.
     */
    interface AS400Endpoint {
        String host();

        String user();

        String password();

        boolean tls();

        OptionalInt databasePort();

        OptionalInt signonPort();

        OptionalInt commandPort();

        int socketTimeoutMs();
    }

    public static JournalSession connect(ConnectionSettings settings) throws Exception {
        TlsTrust.install(settings.tls());
        Class.forName("com.ibm.as400.access.AS400JDBCDriver");
        AS400 as400 = newAs400(settings);
        Connection jdbc = openJdbcConnection(settings, as400);
        try {
            settings.timestamps().verifySourceClock(jdbc);
            Connect<AS400, IOException> as400Connection = () -> as400;
            Connect<Connection, SQLException> jdbcConnection = () -> jdbc;
            JournalInfoRetrieval journalRetrieval = new JournalInfoRetrieval(0, 0, 2000);
            List<FileFilter> includeFiles = JournalSession.includeFiles(settings.schema(), settings.tables());
            JournalInfo journal = verifyCapturedJournal(journalRetrieval, as400, includeFiles);
            RetrieveConfig config = new RetrieveConfigBuilder()
                    .withAs400(as400Connection)
                    .withJournalInfo(journal)
                    .withServerFiltering(true)
                    .withIncludeFiles(includeFiles)
                    .withMaxServerSideEntries(settings.maxServerSideEntries())
                    .withJournalBufferSize(settings.journalBufferSize())
                    .build();
            SchemaCacheHash schemaCache = new SchemaCacheHash();
            String database = JdbcFileDecoder.getDatabaseName(jdbc);
            RrnAwareFileDecoder decoder = new RrnAwareFileDecoder(
                    jdbcConnection, database, schemaCache, settings);
            return new JournalSession(settings, as400, jdbc, config, journalRetrieval, decoder, journal);
        }
        catch (Exception error) {
            jdbc.close();
            as400.disconnectAllServices();
            throw error;
        }
    }

    public void emitReceiverCatalog(int limit, String requiredReceiver, PrintStream out)
            throws Exception {
        ReadOnlyReceiverCatalog.writeReceivers(
                jdbc,
                journal.journalLibrary(),
                journal.journalName(),
                limit,
                requiredReceiver,
                out);
    }

    /** Bibliothèque courante de la connexion — utilisée par {@code discover} sans bibliothèque explicite. */
    public String currentLibrary() {
        return settings.schema();
    }

    public void emitTail(PrintStream out) throws Exception {
        ReadOnlyReceiverCatalog.writeTail(jdbc, journal.journalLibrary(), journal.journalName(), out);
    }

    /**
     * Version IBM i et {@code QTIMZON} pour la sonde de source (chantier
     * « prod-wiring ») — la connexion JDBC JTOpen déjà ouverte ici prouve à
     * elle seule l'authentification (échouée, {@code connect_error} a déjà
     * arrêté le worker avant d'atteindre ce point).
     */
    public void emitSourceInfo(PrintStream out) throws Exception {
        SourceInfo.write(jdbc, out);
    }

    /**
     * Découverte de tables : catalogue seulement, borné par {@link TableDiscovery#discoverySql}
     * et le timeout SQL déjà appliqué aux fenêtres de journal. Une ligne {@code table\t...}
     * par table trouvée, jamais de ligne pour une table en échec de lecture individuelle
     * (elle est simplement absente du résultat — pas de fuite d'erreur par table).
     */
    public void emitDiscover(TableDiscovery.DiscoverRequest request, PrintStream out) throws Exception {
        TableDiscovery.emit(jdbc, request, sqlQueryTimeoutSeconds(), out);
    }

    public void processSqlWindow(WindowRequest window, PrintStream out) throws Exception {
        refuseSqlMultiTable(settings.tables());
        BigInteger startSequence = new BigInteger(window.startSequence());
        BigInteger endSequence = new BigInteger(window.endSequence());
        int span = window.boundedRange(startSequence, endSequence);
        int timeoutSeconds = sqlQueryTimeoutSeconds();
        out.printf(
                "retrieve_start receiver=%s start_sequence=%s end_sequence=%s timeout_ms=%d%n",
                window.receiver(),
                window.startSequence(),
                window.endSequence(),
                timeoutSeconds * 1000L);
        out.flush();
        String sql = displayJournalSql(window, span);
        Optional<TableInfo> tableInfo = decoder.getRecordFormat(settings.table(), settings.schema());
        RawCaptureWriter rawWriter = window.rawDirectory()
                .map(path -> {
                    try {
                        return new RawCaptureWriter(path);
                    }
                    catch (IOException e) {
                        throw new IllegalStateException("cannot initialize raw directory", e);
                    }
                })
                .orElse(null);
        List<RawCaptureWriter.RawEvent> rawEvents = new ArrayList<>();
        long startedNanos = System.nanoTime();
        int seen = 0;
        int decoded = 0;
        try (Statement statement = jdbc.createStatement()) {
            statement.setQueryTimeout(timeoutSeconds);
            try (ResultSet rows = statement.executeQuery(sql)) {
                while (rows.next()) {
                    seen++;
                    if (seen > window.maxDecodedEntries()) {
                        throw new IllegalStateException(
                                "bounded DISPLAY_JOURNAL window exceeded max decoded entries; checkpoint must not advance");
                    }
                    String typeCode = sqlToken(rows.getString("JOURNAL_ENTRY_TYPE"));
                    JournalEntryType type = sqlEntryType(typeCode);
                    if (type == null || !isRowEntry(type)) {
                        continue;
                    }
                    if (isUnsupportedRollbackEntry(type)) {
                        throw new IllegalStateException(
                                "unsupported rollback journal row entry sequence="
                                        + rows.getString("SEQUENCE_NUMBER")
                                        + "; checkpoint must not advance");
                    }
                    if (tableInfo.isEmpty()) {
                        throw new IllegalStateException(
                                "DISPLAY_JOURNAL table structure is missing; checkpoint must not advance");
                    }
                    long entryRrn = sqlRrn(rows.getString("COUNT_OR_RRN"));
                    boolean rrnOnlyDelete = type == JournalEntryType.DELETE_ROW;
                    byte[] image = hexToBytes(rows.getString("ENTRY_DATA_HEX"), rrnOnlyDelete);
                    Object[] fields;
                    if (image.length == 0 && rrnOnlyDelete) {
                        // IMAGES(*AFTER) : le delete n'a pas d'image ; le RRN
                        // est la seule identite durable de la ligne supprimee.
                        if (entryRrn < 0) {
                            throw new IllegalStateException(
                                    "DISPLAY_JOURNAL delete entry carries no row image and no usable RRN sequence="
                                            + rows.getString("SEQUENCE_NUMBER")
                                            + "; checkpoint must not advance");
                        }
                        fields = new Object[0];
                    }
                    else {
                        fields = applySqlNullIndicators(
                                decodeSqlImage(tableInfo.get(), image),
                                rows.getString("NULL_VALUE_INDICATORS"));
                        int expected = tableInfo.get().getStructure().size();
                        if (fields.length != expected) {
                            throw new IllegalStateException(String.format(
                                    "journal row image incomplete sequence=%s type=%s expected_fields=%d actual_fields=%d; checkpoint must not advance",
                                    rows.getString("SEQUENCE_NUMBER"),
                                    typeCode,
                                    expected,
                                    fields.length));
                        }
                    }
                    decoded++;
                    out.printf(
                            "sql_event sequence=%s type=%s timestamp=%s fields=%d rrn=%d%n",
                            sqlToken(rows.getString("SEQUENCE_NUMBER")),
                            typeCode,
                            sqlToken(rows.getString("ENTRY_TIMESTAMP")),
                            fields.length,
                            entryRrn);
                    if (rawWriter != null) {
                        rawEvents.add(rawWriter.eventFromSql(
                                "ibmi",
                                journal,
                                settings.schema(),
                                settings.table(),
                                type,
                                tableInfo.get(),
                                fields,
                                window.receiver(),
                                window.receiverLibrary(),
                                sqlToken(rows.getString("SEQUENCE_NUMBER")),
                                settings.timestamps().fromSql(rows.getString("ENTRY_TIMESTAMP")),
                                entryRrn));
                    }
                }
            }
        }
        catch (SQLTimeoutException timeout) {
            throw new IllegalStateException("bounded DISPLAY_JOURNAL timed out");
        }
        if (decoded > 0 && rawWriter == null) {
            throw new IllegalStateException(
                    "DISPLAY_JOURNAL decoded events without raw directory; checkpoint must not advance");
        }
        if (rawWriter != null && !rawEvents.isEmpty()) {
            RawCaptureWriter.RawPosition watermark = new RawCaptureWriter.RawPosition(
                    window.receiver(),
                    window.endSequence());
            rawWriter.writeBatch(rawEvents, watermark);
        }
        long elapsedMillis = (System.nanoTime() - startedNanos) / 1_000_000L;
        out.printf(
                "summary seen=%d decoded=%d elapsed_ms=%d scan_complete=true%n",
                seen,
                decoded,
                elapsedMillis);
        out.flush();
    }

    public void processWindow(WindowRequest window, PrintStream out) throws Exception {
        // Une session de lecture par fenetre. Le cache de receivers de
        // JournalInfoRetrieval est le seul etat mutable partage entre deux
        // fenetres : mesure du 16/09, un rattrapage reel echouait en
        // ExtendedIllegalArgumentException apres environ 6 700 entrees
        // decodees, et le meme couple de fenetres rejoue dans un JVM neuf
        // reussissait. La session partagee est donc remplacee a chaque
        // fenetre, sans toucher au reste de la chaine de lecture.
        JournalInfoRetrieval perWindowRetrieval = new JournalInfoRetrieval(0, 0, 2000);
        RetrieveJournal retrieve = new RetrieveJournal(retrieveConfig, perWindowRetrieval);
        JournalReceiver receiver = new JournalReceiver(window.receiver(), window.receiverLibrary());
        BigInteger startSequence = new BigInteger(window.startSequence());
        BigInteger endSequence = new BigInteger(window.endSequence());
        window.boundedRange(startSequence, endSequence);
        JournalPosition start = new JournalPosition(startSequence, receiver);
        JournalPosition end = new JournalPosition(endSequence, receiver);
        JournalProcessedPosition position = new JournalProcessedPosition(start, Instant.EPOCH, false);
        RawCaptureWriter rawWriter = window.rawDirectory()
                .map(path -> {
                    try {
                        return new RawCaptureWriter(path);
                    }
                    catch (IOException e) {
                        throw new IllegalStateException("cannot initialize raw directory", e);
                    }
                })
                .orElse(null);
        List<RawCaptureWriter.RawEvent> rawEvents = new ArrayList<>();
        boolean verbose = window.verbose().orElse(settings.verbose());

        out.printf(
                "retrieve_start receiver=%s start_sequence=%s end_sequence=%s timeout_ms=%d%n",
                window.receiver(),
                window.startSequence(),
                window.endSequence(),
                settings.retrieveTimeoutMs());
        out.flush();

        long startedNanos = System.nanoTime();
        long lastProgressNanos = startedNanos;
        int seen = 0;
        int decoded = 0;
        boolean scanComplete = false;
        boolean partialScan = false;
        BigInteger lastSeenSequence = null;
        int pageIndex = 0;
        while (!scanComplete) {
            PositionRange range = new PositionRange(false, position, end);
            long pageStartedNanos = System.nanoTime();
            pageIndex++;
            // One line per pagination page. The scan loop calls retrieveWithTimeout
            // repeatedly, so a window can exceed the client deadline without any single
            // retrieve timing out: that is why no stalled stack was ever captured.
            out.printf("page_start index=%d position=%s%n", pageIndex, position.getOffset());
            out.flush();
            RetrievalState state = retrieveWithTimeout(retrieve, position, range);
            long pageMillis = (System.nanoTime() - pageStartedNanos) / 1_000_000L;
            out.printf(
                    "page_done index=%d elapsed_ms=%d has_data=%s seen=%d decoded=%d%n",
                    pageIndex,
                    pageMillis,
                    state.hasData(),
                    seen,
                    decoded);
            out.flush();
            if (verbose) {
                System.err.printf("state=%s has_data=%s receiver=%s start_sequence=%s%n",
                        state, state.hasData(), window.receiver(), window.startSequence());
            }
            failIfBufferTooSmall(retrieve);
            out.printf("page_buffer_ok index=%d%n", pageIndex);
            out.flush();
            if (!state.hasData()) {
                scanComplete = true;
                break;
            }
            long decodeStartedNanos = System.nanoTime();
            int entriesThisPage = 0;
            out.printf("decode_start index=%d%n", pageIndex);
            out.flush();
            while (retrieve.nextEntry()) {
                entriesThisPage++;
                EntryHeader header = retrieve.getEntryHeader();
                if (header.getSequenceNumber().compareTo(endSequence) > 0) {
                    scanComplete = true;
                    break;
                }
                if (seen >= window.maxDecodedEntries()) {
                    throw new IllegalStateException(
                            "bounded journal window exceeded max decoded entries; checkpoint must not advance");
                }
                seen++;
                lastSeenSequence = header.getSequenceNumber();
                if (System.nanoTime() - lastProgressNanos >= 5_000_000_000L) {
                    out.printf("window_progress seen=%d decoded=%d last_sequence=%s%n",
                            seen, decoded, lastSeenSequence);
                    out.flush();
                    lastProgressNanos = System.nanoTime();
                }
                if (verbose) {
                    System.err.printf("entry code=%s type=%s schema=%s table=%s sequence=%s timestamp=%s%n",
                            header.getJournalCode(), header.getEntryType(), header.getLibrary(), header.getFile(),
                            header.getSequenceNumber(), header.getTime());
                }

                JournalEntryType type = header.getJournalEntryType();
                if (isUnsupportedRollbackEntry(type)) {
                    throw new IllegalStateException(
                            "unsupported rollback journal row entry receiver="
                                    + window.receiver() + " sequence=" + header.getSequenceNumber()
                                    + " type=" + type + "; checkpoint must not advance");
                }
                if (!isRowEntry(type)) {
                    // Une operation niveau fichier (reorganisation, vidage,
                    // changement de format) reecrit les positions physiques
                    // sans produire d'entrees ligne : les identites RRN
                    // deviennent muettes. On refuse explicitement plutot que
                    // de laisser la destination se corrompre en silence.
                    // Le refus vaut aussi pour les types non reconnus (type
                    // null) : une entree inconnue sur une table suivie ne doit
                    // jamais etre ignoree silencieusement.
                    if (!isRoutineNoiseEntry(type)
                            && settings.schema().equalsIgnoreCase(header.getLibrary() == null
                                    ? "" : header.getLibrary().trim())
                            && containsTable(settings.tables(), header.getFile())) {
                        throw new IllegalStateException(String.format(
                                "file-level or unrecognized journal entry receiver=%s sequence=%s type=%s object=%s.%s;"
                                        + " physical row identity may have been rewritten — resynchronization required;"
                                        + " checkpoint must not advance",
                                window.receiver(), header.getSequenceNumber(), type,
                                header.getLibrary(), header.getFile()));
                    }
                    continue;
                }
                requireKnownTable(settings.schema(), settings.tables(), header.getLibrary(), header.getFile());
                Optional<TableInfo> tableInfo = decoder.getRecordFormat(header.getFile(), header.getLibrary());
                if (tableInfo.isEmpty()) {
                    throw new IllegalStateException(
                            "journal table structure is missing; checkpoint must not advance");
                }
                Object[] fields = retrieve.decode(decoder);
                long entryRrn = decoder.lastEntryRrn();
                int expectedFields = tableInfo.get().getStructure().size();
                if (fields.length != expectedFields) {
                    // IMAGES(*AFTER) : un delete n'a pas d'image ligne — le
                    // RRN de l'en-tete est alors la seule identite durable de
                    // la ligne supprimee. Une entree marquee incomplete ou un
                    // RRN absent restent refuseres : impossible de distinguer
                    // un delete *AFTER legitime d'une entree tronquee.
                    boolean rrnOnlyDelete = type == JournalEntryType.DELETE_ROW
                            && fields.length == 0
                            && !decoder.lastEntryIncomplete()
                            && entryRrn >= 0;
                    if (!rrnOnlyDelete) {
                        throw new IllegalStateException(String.format(
                                "journal row image incomplete receiver=%s sequence=%s type=%s expected_fields=%d actual_fields=%d; checkpoint must not advance",
                                window.receiver(), header.getSequenceNumber(), type, expectedFields, fields.length));
                    }
                }
                decoded++;
                if (verbose) {
                    System.err.printf("decoded_count=%d decoded_types=%s%n",
                            fields.length, typeCounts(fields));
                }
                if (rawWriter != null) {
                    if (header.hasReceiver() && !window.receiver().equalsIgnoreCase(header.getReceiver())) {
                        throw new IllegalStateException("journal receiver changed inside bounded batch expected="
                                + window.receiver() + " observed=" + header.getReceiver());
                    }
                    rawEvents.add(rawWriter.event(
                            "ibmi",
                            journal,
                            header,
                            type,
                            tableInfo.get(),
                            fields,
                            window.receiver(),
                            window.receiverLibrary(),
                            settings.timestamps().fromHeader(header.getTime()),
                            entryRrn));
                }
            }
            out.printf(
                    "decode_done index=%d entries=%d seen=%d decoded=%d elapsed_ms=%d%n",
                    pageIndex,
                    entriesThisPage,
                    seen,
                    decoded,
                    (System.nanoTime() - decodeStartedNanos) / 1_000_000L);
            out.flush();
            if (scanComplete) {
                break;
            }
            if (lastSeenSequence != null && lastSeenSequence.compareTo(endSequence) >= 0) {
                scanComplete = true;
                break;
            }
            try {
                if (!moreDataWithTimeout(retrieve)) {
                    scanComplete = true;
                    break;
                }
            }
            catch (MoreDataUnknown unknown) {
                // Contiguously read up to lastSeenSequence, so that position is a
                // safe watermark: nothing is skipped and nothing is duplicated.
                // The window simply ends earlier than requested.
                out.printf("more_data_unknown index=%d last_sequence=%s%n",
                        pageIndex, lastSeenSequence);
                out.flush();
                partialScan = true;
                scanComplete = true;
                break;
            }
            if (seen >= window.maxDecodedEntries()) {
                throw new IllegalStateException(
                        "bounded journal window exceeded max decoded entries; checkpoint must not advance");
            }
            position = new JournalProcessedPosition(retrieve.getPosition());
        }
        long elapsedMillis = (System.nanoTime() - startedNanos) / 1_000_000L;
        if (!scanComplete) {
            throw new IllegalStateException("bounded journal window did not reach its end");
        }

        String rawStatus;
        if (rawWriter == null) {
            rawStatus = null;
        }
        else if (rawEvents.isEmpty()) {
            rawStatus = "raw_status=empty_no_checkpoint";
        }
        else {
            RawCaptureWriter.RawEvent last = rawEvents.get(rawEvents.size() - 1);
            RawCaptureWriter.RawPosition watermark;
            if (partialScan && lastSeenSequence != null) {
                // The window stopped short, so the watermark is the last sequence
                // actually read rather than the requested end. Contiguous read up
                // to that point means nothing is skipped and nothing repeats.
                watermark = new RawCaptureWriter.RawPosition(
                        window.receiver(), lastSeenSequence.toString());
            }
            else {
                watermark = window.rawHighWatermarkSequence()
                        .map(sequence -> new RawCaptureWriter.RawPosition(window.receiver(), sequence))
                        .orElseGet(() -> new RawCaptureWriter.RawPosition(last.receiver(), last.sequence()));
            }
            BigInteger lastEventSequence = new BigInteger(last.sequence());
            BigInteger watermarkSequence = new BigInteger(watermark.sequence());
            if (watermarkSequence.compareTo(lastEventSequence) < 0) {
                throw new IllegalArgumentException("raw high watermark must cover every decoded event");
            }
            if (watermarkSequence.compareTo(endSequence) > 0) {
                throw new IllegalArgumentException("raw high watermark must not exceed the scan end");
            }
            RawCaptureWriter.RawCaptureResult result = rawWriter.writeBatch(rawEvents, watermark);
            boolean checkpointCommitted = false;
            if (window.checkpointFile().isPresent()) {
                checkpointCommitted = rawWriter.commitCheckpoint(window.checkpointFile().get(), watermark);
            }
            rawStatus = String.format(
                    "raw_status=durable event_count=%d batch_id=%s payload_sha256=%s high_watermark_receiver=%s high_watermark_sequence=%s payload_existed=%s manifest_existed=%s checkpoint_committed=%s payload_path=%s manifest_path=%s",
                    result.eventCount(), result.batchId(), result.payloadSha256(), watermark.receiver(),
                    watermark.sequence(), result.payloadExisted(), result.manifestExisted(), checkpointCommitted,
                    result.payloadPath(), result.manifestPath());
        }

        // retrieve.getPosition() is an IBM i round trip and it was the LAST call
        // before the summary was emitted, so a stall here discarded a window that
        // had already been retrieved, decoded and written. Measured 2026-08-27:
        // retrieve under 2.4 s, decode about 3 s, moreData never timing out, then
        // nothing until the Python deadline. Bounded, with the sequence we already
        // hold as the fallback.
        out.printf("final_position_start%n");
        out.flush();
        String finalPosition = finalPositionWithTimeout(retrieve, lastSeenSequence);
        out.printf("final_position_done value_len=%d raw_status_len=%d%n",
                finalPosition.length(), rawStatus == null ? 0 : rawStatus.length());
        out.flush();
        out.printf("summary seen=%d decoded=%d elapsed_ms=%d final_position=%s scan_complete=true%n",
                seen, decoded, elapsedMillis, finalPosition);
        out.printf("summary_written%n");
        out.flush();
        if (rawStatus != null) {
            out.println(rawStatus);
        }
        out.flush();
        out.printf("window_flushed%n");
        out.flush();
    }

    @Override
    public void close() {
        try {
            jdbc.close();
        }
        catch (SQLException ignored) {
            // Connection teardown must not hide a window error.
        }
        as400.disconnectAllServices();
    }

    private int sqlQueryTimeoutSeconds() {
        int seconds = settings.retrieveTimeoutMs() / 1000;
        if (seconds < 1) {
            return 1;
        }
        if (seconds > 29) {
            return 29;
        }
        return seconds;
    }

    private String displayJournalSql(WindowRequest window, int maxRows) {
        String journalLibrary = sqlIdentifier(journal.journalLibrary());
        String journalName = sqlIdentifier(journal.journalName());
        String receiverLibrary = sqlIdentifier(window.receiverLibrary());
        String receiver = sqlIdentifier(window.receiver());
        String schema = sqlIdentifier(settings.schema());
        String table = sqlIdentifier(settings.table());
        return """
                SELECT SEQUENCE_NUMBER, JOURNAL_ENTRY_TYPE, ENTRY_TIMESTAMP, COUNT_OR_RRN,
                       NULL_VALUE_INDICATORS, HEX(ENTRY_DATA) AS ENTRY_DATA_HEX
                  FROM TABLE(QSYS2.DISPLAY_JOURNAL(
                        JOURNAL_LIBRARY => '%s',
                        JOURNAL_NAME => '%s',
                        STARTING_RECEIVER_LIBRARY => '%s',
                        STARTING_RECEIVER_NAME => '%s',
                        STARTING_SEQUENCE => %s,
                        ENDING_RECEIVER_LIBRARY => '%s',
                        ENDING_RECEIVER_NAME => '%s',
                        ENDING_SEQUENCE => %s,
                        JOURNAL_CODES => 'R',
                        OBJECT_LIBRARY => '%s',
                        OBJECT_NAME => '%s',
                        OBJECT_OBJTYPE => '*FILE',
                        OBJECT_MEMBER => '*ALL'
                  ))
                 FETCH FIRST %d ROWS ONLY
                """.formatted(
                journalLibrary,
                journalName,
                receiverLibrary,
                receiver,
                window.startSequence(),
                receiverLibrary,
                receiver,
                window.endSequence(),
                schema,
                table,
                maxRows);
    }

    /** Nom IBM i interpolé dans DISPLAY_JOURNAL : jeu des noms système (_ et @ compris), jamais de guillemet. */
    static String sqlIdentifier(String value) {
        if (value == null || !value.matches("[A-Za-z0-9_$#@]{1,128}")) {
            throw new IllegalArgumentException("unsafe IBM i identifier");
        }
        return value;
    }

    static List<String> parseTableList(String csv, String fallbackTable) {
        if (fallbackTable == null || fallbackTable.isBlank()) {
            throw new IllegalArgumentException("ISERIES_TABLE is required");
        }
        if (csv == null || csv.isBlank()) {
            return List.of(fallbackTable);
        }
        List<String> tables = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (String item : csv.split(",", -1)) {
            String table = item.trim();
            if (table.isEmpty()) {
                throw new IllegalArgumentException("ISERIES_TABLES entries must be non-empty");
            }
            table = sqlIdentifier(table);
            String key = table.toUpperCase(Locale.ROOT);
            if (!seen.add(key)) {
                throw new IllegalArgumentException("ISERIES_TABLES entries must be unique");
            }
            tables.add(table);
            if (tables.size() > MAX_CAPTURE_TABLES) {
                throw new IllegalArgumentException("ISERIES_TABLES must have at most 32 tables");
            }
        }
        if (tables.isEmpty()) {
            throw new IllegalArgumentException("ISERIES_TABLES entries must be non-empty");
        }
        if (!containsTable(tables, fallbackTable)) {
            throw new IllegalArgumentException("ISERIES_TABLE must be included in ISERIES_TABLES");
        }
        return List.copyOf(tables);
    }

    static List<FileFilter> includeFiles(String schema, List<String> tables) {
        if (schema == null || schema.isBlank()) {
            throw new IllegalArgumentException("ISERIES_SCHEMA is required");
        }
        if (tables == null || tables.isEmpty()) {
            throw new IllegalArgumentException("ISERIES_TABLES entries must be non-empty");
        }
        List<FileFilter> files = new ArrayList<>(tables.size());
        for (String table : tables) {
            files.add(new FileFilter(schema, table));
        }
        return List.copyOf(files);
    }

    static JournalInfo verifyCapturedJournal(
            JournalInfoRetrieval journalRetrieval,
            AS400 as400,
            List<FileFilter> includeFiles) throws Exception {
        if (includeFiles == null || includeFiles.isEmpty()) {
            throw new IllegalStateException("captured table list is empty; checkpoint must not advance");
        }
        JournalInfo journal = null;
        for (FileFilter file : includeFiles) {
            JournalInfo candidate = journalRetrieval.getJournal(as400, file.schema(), file.table());
            if (journal == null) {
                journal = candidate;
            }
            else {
                requireSameJournal(journal, candidate, file.table());
            }
        }
        return journal;
    }

    static void requireSameJournal(JournalInfo expected, JournalInfo observed, String table) {
        if (expected == null || observed == null
                || !expected.journalName().equalsIgnoreCase(observed.journalName())
                || !expected.journalLibrary().equalsIgnoreCase(observed.journalLibrary())) {
            throw new IllegalStateException(
                    "table " + table + " is not journaled to the verified journal; checkpoint must not advance");
        }
    }

    static String requireKnownTable(String schema, List<String> tables, String library, String file) {
        String capturedLibrary = library == null ? "" : library.trim();
        String capturedFile = file == null ? "" : file.trim();
        if (schema == null || !schema.equalsIgnoreCase(capturedLibrary) || !containsTable(tables, capturedFile)) {
            throw new IllegalStateException(
                    "server filter returned an unexpected object; checkpoint must not advance");
        }
        return capturedFile;
    }

    static void refuseSqlMultiTable(List<String> tables) {
        if (tables != null && tables.size() > 1) {
            throw new IllegalStateException(
                    "DISPLAY_JOURNAL refuses multi-table capture; checkpoint must not advance");
        }
    }

    static boolean containsTable(List<String> tables, String candidate) {
        if (tables == null || candidate == null || candidate.isBlank()) {
            return false;
        }
        String key = candidate.trim().toUpperCase(Locale.ROOT);
        for (String table : tables) {
            if (table != null && key.equals(table.toUpperCase(Locale.ROOT))) {
                return true;
            }
        }
        return false;
    }

    static String sqlToken(String value) {
        if (value == null || value.isBlank()) {
            return "-";
        }
        return value.trim().replace(' ', 'T').replace('\t', 'T').replace('\n', 'T');
    }

    static JournalEntryType sqlEntryType(String type) {
        if (type == null) {
            return null;
        }
        return switch (type.trim().toUpperCase()) {
            case "BR", "UR", "DR" -> throw new IllegalStateException(
                    "unsupported rollback journal row entry; checkpoint must not advance");
            case "PT" -> JournalEntryType.ADD_ROW1;
            case "PX" -> JournalEntryType.ADD_ROW2;
            case "UB" -> JournalEntryType.BEFORE_IMAGE;
            case "UP" -> JournalEntryType.AFTER_IMAGE;
            case "DL" -> JournalEntryType.DELETE_ROW;
            default -> null;
        };
    }

    static byte[] hexToBytes(String hex) {
        return hexToBytes(hex, false);
    }

    /**
     * COUNT_OR_RRN de DISPLAY_JOURNAL : absent sur les entrees qui ne portent
     * pas de ligne (-1), position physique sinon.
     */
    static long sqlRrn(String value) {
        if (value == null || value.isBlank() || "-".equals(value.trim())) {
            return -1;
        }
        try {
            return new BigInteger(value.trim()).longValueExact();
        }
        catch (NumberFormatException | ArithmeticException e) {
            return -1;
        }
    }

    /**
     * {@code allowMissingImage} sert les deletes sous {@code IMAGES(*AFTER)} :
     * l'image absente y est un etat legitime porte par le RRN, pas une
     * anomalie. Un hex tronque ou invalide reste refuse quoi qu'il arrive.
     */
    static byte[] hexToBytes(String hex, boolean allowMissingImage) {
        if (hex == null) {
            if (allowMissingImage) {
                return new byte[0];
            }
            throw new IllegalStateException(
                    "DISPLAY_JOURNAL row image is missing; checkpoint must not advance");
        }
        String cleaned = hex.trim();
        if (cleaned.isEmpty() || "-".equals(cleaned)) {
            if (allowMissingImage) {
                return new byte[0];
            }
            throw new IllegalStateException(
                    "DISPLAY_JOURNAL row image is empty; checkpoint must not advance");
        }
        if ((cleaned.length() & 1) != 0) {
            throw new IllegalStateException(
                    "DISPLAY_JOURNAL row image hex is truncated; checkpoint must not advance");
        }
        byte[] data = new byte[cleaned.length() / 2];
        for (int i = 0; i < data.length; i++) {
            int high = Character.digit(cleaned.charAt(i * 2), 16);
            int low = Character.digit(cleaned.charAt(i * 2 + 1), 16);
            if (high < 0 || low < 0) {
                throw new IllegalStateException(
                        "DISPLAY_JOURNAL row image hex is invalid; checkpoint must not advance");
            }
            data[i] = (byte) ((high << 4) + low);
        }
        return data;
    }

    static Object[] decodeSqlImage(TableInfo tableInfo, byte[] data) {
        AS400Structure structure = tableInfo.getAs400Structure();
        if (data.length >= 16) {
            try {
                String lengthStr = ((String) new AS400Text(5).toObject(data, 0)).trim();
                int length = Integer.parseInt(lengthStr);
                if (length > 0) {
                    return (Object[]) structure.toObject(data, 16);
                }
            }
            catch (RuntimeException ignored) {
                // DISPLAY_JOURNAL ENTRY_DATA may already be the row image.
            }
        }
        return (Object[]) structure.toObject(data, 0);
    }

    static Object[] applySqlNullIndicators(Object[] values, String indicators) {
        // DISPLAY_JOURNAL exposes NULL_VALUE_INDICATORS separately from
        // ENTRY_DATA. AS400Structure decodes an absent VARCHAR as "", so a
        // raw image without these indicators silently changes NULL into an
        // empty string. IBM documents one 0/1 indicator per physical field
        // for a complete record image; refuse unknown/minimized shapes.
        if (indicators == null || indicators.isBlank()) {
            return values;
        }
        String flags = indicators.stripTrailing();
        if (flags.length() != values.length) {
            throw new IllegalStateException(
                    "DISPLAY_JOURNAL null indicator count differs from field count; checkpoint must not advance");
        }
        Object[] corrected = values.clone();
        for (int index = 0; index < flags.length(); index++) {
            char flag = flags.charAt(index);
            if (flag == '1') {
                corrected[index] = null;
            }
            else if (flag != '0') {
                throw new IllegalStateException(
                        "DISPLAY_JOURNAL null indicator is unsupported; checkpoint must not advance");
            }
        }
        return corrected;
    }

    private RetrievalState retrieveWithTimeout(
            RetrieveJournal retrieve,
            JournalProcessedPosition position,
            PositionRange range) throws Exception {
        // Keep the worker thread so a timeout can report where it is parked.
        // The retrieve either returns in ~4 s or never returns at all, and the
        // rate is invariant to the deadline, so the stack is the only thing that
        // says whether it waits on IBM i or spins inside the client.
        final Thread[] holder = new Thread[1];
        ExecutorService executor = Executors.newSingleThreadExecutor(thread -> {
            Thread worker = new Thread(thread, "as400-retrieve");
            worker.setDaemon(true);
            holder[0] = worker;
            return worker;
        });
        Future<RetrievalState> future = executor.submit(() -> retrieve.retrieveJournal(position, range));
        try {
            return future.get(settings.retrieveTimeoutMs(), TimeUnit.MILLISECONDS);
        }
        catch (TimeoutException timeout) {
            emitStalledStack(holder[0]);
            retrieve.cancelJob();
            future.cancel(true);
            throw new IllegalStateException("bounded journal retrieve timed out");
        }
        catch (ExecutionException error) {
            Throwable cause = error.getCause();
            if (cause instanceof Exception exception) {
                throw exception;
            }
            if (cause instanceof Error panic) {
                throw panic;
            }
            throw error;
        }
        finally {
            executor.shutdownNow();
        }
    }

    /**
     * Print where the retrieve thread is parked when the deadline expires.
     *
     * Class and method names only, no arguments and no field values, so no IBM i
     * credential or payload can reach the log. One line per frame, prefixed so the
     * Python side can pick them out of the worker stream.
     */
    private static void emitStalledStack(Thread worker) {
        if (worker == null) {
            System.out.printf("stalled_stack thread=absent%n");
            System.out.flush();
            return;
        }
        StackTraceElement[] frames = worker.getStackTrace();
        System.out.printf(
                "stalled_stack thread=%s state=%s frames=%d%n",
                worker.getName(),
                worker.getState(),
                frames.length);
        int limit = Math.min(frames.length, 24);
        for (int index = 0; index < limit; index++) {
            StackTraceElement frame = frames[index];
            System.out.printf(
                    "stalled_frame %d %s.%s%n",
                    index,
                    frame.getClassName(),
                    frame.getMethodName());
        }
        System.out.flush();
    }

    private static void failIfBufferTooSmall(RetrieveJournal retrieve) {
        FirstHeader header = retrieve.getFirstHeader();
        if (header != null
                && header.status() == OffsetStatus.MORE_DATA_NEW_OFFSET
                && header.offset() == 0) {
            throw new IllegalStateException(
                    "journal buffer too small for a journal entry; checkpoint must not advance");
        }
    }

    private static boolean moreData(RetrieveJournal retrieve) {
        FirstHeader header = retrieve.getFirstHeader();
        return header != null && retrieve.futureDataAvailable();
    }

    /**
     * Ask whether more data follows, under a deadline.
     *
     * {@code futureDataAvailable()} is an IBM i round trip with no client-side
     * bound. Measured 2026-08-27: the retrieve itself returned in under 2.4 s and
     * the decode finished in about 3 s, then this call hung until the Python
     * reader deadline expired, which discarded a fully decoded window. Roughly
     * half of all polls died here.
     *
     * A timeout is reported as {@link MoreDataUnknown} so the caller can publish
     * what it actually read instead of throwing the window away.
     */
    private boolean moreDataWithTimeout(RetrieveJournal retrieve) throws MoreDataUnknown {
        ExecutorService executor = Executors.newSingleThreadExecutor(thread -> {
            Thread worker = new Thread(thread, "as400-more-data");
            worker.setDaemon(true);
            return worker;
        });
        Future<Boolean> future = executor.submit(() -> moreData(retrieve));
        try {
            return future.get(moreDataTimeoutMs(), TimeUnit.MILLISECONDS);
        }
        catch (TimeoutException timeout) {
            future.cancel(true);
            throw new MoreDataUnknown();
        }
        catch (Exception error) {
            throw new MoreDataUnknown();
        }
        finally {
            executor.shutdownNow();
        }
    }

    /**
     * Final journal position, under a deadline.
     *
     * Falls back to the last sequence actually read when IBM i does not answer in
     * time. The value is informational in the summary, so a fallback is safe and
     * far better than losing a window that is already durable on disk.
     */
    private String finalPositionWithTimeout(RetrieveJournal retrieve, BigInteger lastSeenSequence) {
        ExecutorService executor = Executors.newSingleThreadExecutor(thread -> {
            Thread worker = new Thread(thread, "as400-final-position");
            worker.setDaemon(true);
            return worker;
        });
        Future<String> future = executor.submit(() -> String.valueOf(retrieve.getPosition()));
        try {
            return future.get(finalPositionTimeoutMs(), TimeUnit.MILLISECONDS);
        }
        catch (Exception error) {
            future.cancel(true);
            return lastSeenSequence == null ? "unknown" : "fallback:" + lastSeenSequence;
        }
        finally {
            executor.shutdownNow();
        }
    }

    static long finalPositionTimeoutMs() {
        String raw = System.getenv("AS400_FINAL_POSITION_TIMEOUT_MS");
        long millis = raw == null || raw.isBlank() ? 2000L : Long.parseLong(raw.trim());
        if (millis < 100L || millis > 30_000L) {
            throw new IllegalArgumentException("AS400_FINAL_POSITION_TIMEOUT_MS must be in [100, 30000]");
        }
        return millis;
    }

    static long moreDataTimeoutMs() {
        String raw = System.getenv("AS400_MORE_DATA_TIMEOUT_MS");
        long millis = raw == null || raw.isBlank() ? 3000L : Long.parseLong(raw.trim());
        if (millis < 100L || millis > 30_000L) {
            throw new IllegalArgumentException("AS400_MORE_DATA_TIMEOUT_MS must be in [100, 30000]");
        }
        return millis;
    }

    /** Raised when it cannot be established whether more data follows. */
    static final class MoreDataUnknown extends Exception {
        private static final long serialVersionUID = 1L;
    }

    /**
     * Fresh {@link AS400} for {@code settings}: TLS variant chosen, socket timeout
     * applied when the JTOpen build supports it, service ports configured. Shared
     * by {@link #connect} and {@link DiagnosticWorker#connect} — no capture-specific
     * check runs here.
     */
    static AS400 newAs400(AS400Endpoint settings) throws Exception {
        AS400 as400 = settings.tls()
                ? new SecureAS400(settings.host(), settings.user(), settings.password().toCharArray())
                : new AS400(settings.host(), settings.user(), settings.password().toCharArray());
        try {
            as400.getClass().getMethod("setSocketTimeout", int.class)
                    .invoke(as400, settings.socketTimeoutMs());
        }
        catch (ReflectiveOperationException ignored) {
            // JTOpen 21.0.7 AS400 has no setSocketTimeout; keep the call site
            // for the contract test and newer JTOpen builds.
        }
        configureServicePorts(settings, as400);
        return as400;
    }

    static void configureServicePorts(AS400Endpoint settings, AS400 as400) {
        settings.databasePort().ifPresent(port -> as400.setServicePort(AS400.DATABASE, port));
        settings.signonPort().ifPresent(port -> as400.setServicePort(AS400.SIGNON, port));
        settings.commandPort().ifPresent(port -> as400.setServicePort(AS400.COMMAND, port));
    }

    static Connection openJdbcConnection(AS400Endpoint settings, AS400 as400) throws SQLException {
        if (settings.databasePort().isEmpty() && settings.signonPort().isEmpty()) {
            Properties jdbcProperties = new Properties();
            jdbcProperties.setProperty("user", settings.user());
            jdbcProperties.setProperty("password", settings.password());
            jdbcProperties.setProperty("date format", "iso");
            jdbcProperties.setProperty("secure", Boolean.toString(settings.tls()));
            return DriverManager.getConnection("jdbc:as400://" + settings.host(), jdbcProperties);
        }

        AS400JDBCDataSource dataSource = new AS400JDBCDataSource(as400);
        dataSource.setPrompt(false);
        dataSource.setSecure(settings.tls());
        dataSource.setDateFormat("iso");
        return dataSource.getConnection();
    }

    static boolean isRowEntry(JournalEntryType type) {
        return type == JournalEntryType.ADD_ROW1
                || type == JournalEntryType.ADD_ROW2
                || type == JournalEntryType.AFTER_IMAGE
                || type == JournalEntryType.BEFORE_IMAGE
                || type == JournalEntryType.DELETE_ROW;
    }

    static boolean isFileLevelEntry(JournalEntryType type) {
        return type == JournalEntryType.FILE_CREATED
                || type == JournalEntryType.FILE_CHANGE;
    }

    static boolean isRoutineNoiseEntry(JournalEntryType type) {
        return type == JournalEntryType.OPEN
                || type == JournalEntryType.CLOSE
                || type == JournalEntryType.START_COMMIT
                || type == JournalEntryType.END_COMMIT;
    }

    static boolean isUnsupportedRollbackEntry(JournalEntryType type) {
        return type == JournalEntryType.ROLLBACK_AFTER_IMAGE
                || type == JournalEntryType.ROLLBACK_BEFORE_IMAGE
                || type == JournalEntryType.ROLLBACK_DELETE_ROW;
    }

    private static Map<String, Integer> typeCounts(Object[] fields) {
        Map<String, Integer> counts = new LinkedHashMap<>();
        for (Object field : fields) {
            String name = field == null ? "null" : field.getClass().getSimpleName();
            counts.merge(name, 1, Integer::sum);
        }
        return counts;
    }

    static Map<String, String> parseFlatJson(String json) {
        String trimmed = json.trim();
        if (trimmed.length() < 2 || trimmed.charAt(0) != '{' || trimmed.charAt(trimmed.length() - 1) != '}') {
            throw new IllegalArgumentException("window request must be a JSON object");
        }
        Map<String, String> result = new LinkedHashMap<>();
        int index = 1;
        int end = trimmed.length() - 1;
        while (index < end) {
            while (index < end && Character.isWhitespace(trimmed.charAt(index))) {
                index++;
            }
            if (index >= end) {
                break;
            }
            if (trimmed.charAt(index) != '"') {
                throw new IllegalArgumentException("window request JSON is invalid");
            }
            index++;
            StringBuilder key = new StringBuilder();
            index = readJsonString(trimmed, index, end, key);
            while (index < end && Character.isWhitespace(trimmed.charAt(index))) {
                index++;
            }
            if (index >= end || trimmed.charAt(index) != ':') {
                throw new IllegalArgumentException("window request JSON is invalid");
            }
            index++;
            while (index < end && Character.isWhitespace(trimmed.charAt(index))) {
                index++;
            }
            if (index >= end) {
                throw new IllegalArgumentException("window request JSON is invalid");
            }
            String value;
            char first = trimmed.charAt(index);
            if (first == '"') {
                index++;
                StringBuilder text = new StringBuilder();
                index = readJsonString(trimmed, index, end, text);
                value = text.toString();
            }
            else if (trimmed.startsWith("null", index) && isJsonTokenEnd(trimmed, index + 4, end)) {
                value = null;
                index += 4;
            }
            else if (trimmed.startsWith("true", index) && isJsonTokenEnd(trimmed, index + 4, end)) {
                value = "true";
                index += 4;
            }
            else if (trimmed.startsWith("false", index) && isJsonTokenEnd(trimmed, index + 5, end)) {
                value = "false";
                index += 5;
            }
            else if (first == '{' || first == '[') {
                throw new IllegalArgumentException("window request JSON is invalid");
            }
            else {
                int start = index;
                if (first == '-' || first == '+') {
                    index++;
                }
                boolean seenDigit = false;
                while (index < end && Character.isDigit(trimmed.charAt(index))) {
                    seenDigit = true;
                    index++;
                }
                if (!seenDigit) {
                    throw new IllegalArgumentException("window request JSON is invalid");
                }
                value = trimmed.substring(start, index);
            }
            result.put(key.toString(), value);
            while (index < end && Character.isWhitespace(trimmed.charAt(index))) {
                index++;
            }
            if (index < end && trimmed.charAt(index) == ',') {
                index++;
            }
        }
        return result;
    }

    private static int readJsonString(String json, int index, int end, StringBuilder output) {
        while (index < end) {
            char character = json.charAt(index++);
            if (character == '"') {
                return index;
            }
            if (character == '\\') {
                if (index >= end) {
                    throw new IllegalArgumentException("window request JSON is invalid");
                }
                output.append(json.charAt(index++));
            }
            else {
                output.append(character);
            }
        }
        throw new IllegalArgumentException("window request JSON is invalid");
    }

    private static boolean isJsonTokenEnd(String json, int index, int end) {
        if (index >= end) {
            return true;
        }
        char character = json.charAt(index);
        return character == ',' || Character.isWhitespace(character);
    }

    public record ConnectionSettings(
            String host,
            String user,
            String password,
            String schema,
            String table,
            List<String> tables,
            boolean verbose,
            boolean tls,
            int journalBufferSize,
            int maxServerSideEntries,
            OptionalInt databasePort,
            OptionalInt signonPort,
            OptionalInt commandPort,
            int journalDateFormat,
            char journalDateSeparator,
            int journalTimeFormat,
            char journalTimeSeparator,
            int socketTimeoutMs,
            int retrieveTimeoutMs,
            JournalTimestamps timestamps) implements AS400Endpoint {

        static ConnectionSettings fromEnvironment() {
            String table = required("ISERIES_TABLE");
            List<String> tables = parseTableList(
                    optionalValue("ISERIES_TABLES").orElse(null),
                    table);
            return new ConnectionSettings(
                    required("ISERIES_HOST"),
                    required("ISERIES_USER"),
                    required("ISERIES_PASSWORD"),
                    required("ISERIES_SCHEMA"),
                    table,
                    tables,
                    booleanValue("AS400_VERBOSE", false),
                    tlsValue("AS400_TLS", true),
                    journalBufferSize("ISERIES_JOURNAL_BUFFER_SIZE", DEFAULT_JOURNAL_BUFFER_SIZE),
                    maxServerSideEntries("ISERIES_MAX_SERVER_ENTRIES", DEFAULT_MAX_SERVER_SIDE_ENTRIES),
                    optionalPort("AS400_DATABASE_PORT"),
                    optionalPort("AS400_SIGNON_PORT"),
                    optionalPort("AS400_COMMAND_PORT"),
                    dateFormat("AS400_JOURNAL_DATE_FORMAT", "ISO"),
                    dateSeparator("AS400_JOURNAL_DATE_SEPARATOR", '-'),
                    timeFormat("AS400_JOURNAL_TIME_FORMAT", "ISO"),
                    timeSeparator("AS400_JOURNAL_TIME_SEPARATOR", '.'),
                    socketTimeoutMs("AS400_SOCKET_TIMEOUT_MS", 270_000),
                    retrieveTimeoutMs("AS400_RETRIEVE_TIMEOUT_MS", 60_000),
                    JournalTimestamps.fromZoneName(required("AS400_SOURCE_TIME_ZONE")));
        }

        /**
         * Settings for {@link DiagnosticWorker}: connection plumbing only — no
         * {@code AS400_SOURCE_TIME_ZONE}, no captured schema/table/journal. The
         * unused capture-only fields carry {@code null}/empty placeholders,
         * never a fabricated schema or table name: {@link DiagnosticWorker}
         * never reads them (it uses {@code jdbc.getSchema()} for its current
         * library and never decodes a journal entry).
         */
        static ConnectionSettings diagnosticFromEnvironment() {
            return new ConnectionSettings(
                    required("ISERIES_HOST"),
                    required("ISERIES_USER"),
                    required("ISERIES_PASSWORD"),
                    null,
                    null,
                    List.of(),
                    booleanValue("AS400_VERBOSE", false),
                    tlsValue("AS400_TLS", true),
                    DEFAULT_JOURNAL_BUFFER_SIZE,
                    DEFAULT_MAX_SERVER_SIDE_ENTRIES,
                    optionalPort("AS400_DATABASE_PORT"),
                    optionalPort("AS400_SIGNON_PORT"),
                    optionalPort("AS400_COMMAND_PORT"),
                    0,
                    '-',
                    0,
                    '.',
                    socketTimeoutMs("AS400_SOCKET_TIMEOUT_MS", 270_000),
                    retrieveTimeoutMs("AS400_RETRIEVE_TIMEOUT_MS", 60_000),
                    null);
        }

        static String required(String name) {
            String value = System.getenv(name);
            if (value == null || value.isBlank()) {
                throw new IllegalArgumentException(name + " is required");
            }
            return value;
        }

        static Optional<String> optionalValue(String name) {
            String value = System.getenv(name);
            return value == null || value.isBlank() ? Optional.empty() : Optional.of(value);
        }

        static Optional<Path> optionalPath(String name) {
            String value = System.getenv(name);
            return value == null || value.isBlank() ? Optional.empty() : Optional.of(Path.of(value));
        }

        static int positiveInteger(String name, int defaultValue) {
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

        static int journalBufferSize(String name, int defaultValue) {
            String value = System.getenv(name);
            int parsed = defaultValue;
            if (value != null && !value.isBlank()) {
                parsed = Integer.parseInt(value);
            }
            if (parsed < MIN_JOURNAL_BUFFER_SIZE || parsed > MAX_JOURNAL_BUFFER_SIZE) {
                throw new IllegalArgumentException(
                        name + " must be between " + MIN_JOURNAL_BUFFER_SIZE + " and " + MAX_JOURNAL_BUFFER_SIZE);
            }
            return parsed;
        }

        static int maxServerSideEntries(String name, int defaultValue) {
            int parsed = positiveInteger(name, defaultValue);
            if (parsed > DEFAULT_MAX_SERVER_SIDE_ENTRIES) {
                throw new IllegalArgumentException(
                        name + " must be at most " + DEFAULT_MAX_SERVER_SIDE_ENTRIES);
            }
            return parsed;
        }

        static boolean booleanValue(String name, boolean defaultValue) {
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

        static boolean tlsValue(String name, boolean defaultValue) {
            boolean enabled = booleanValue(name, defaultValue);
            if (enabled) {
                return true;
            }
            if (!booleanValue("AS400_ALLOW_PLAINTEXT", false)) {
                throw new IllegalArgumentException(
                        "AS400_TLS=false requires AS400_ALLOW_PLAINTEXT=true");
            }
            return false;
        }

        static int socketTimeoutMs(String name, int defaultValue) {
            int parsed = positiveInteger(name, defaultValue);
            if (parsed < 1) {
                throw new IllegalArgumentException(name + " must be positive");
            }
            return parsed;
        }

        static int retrieveTimeoutMs(String name, int defaultValue) {
            int parsed = positiveInteger(name, defaultValue);
            if (parsed < 1_000) {
                throw new IllegalArgumentException(name + " must be at least 1000");
            }
            return parsed;
        }

        static OptionalInt optionalPort(String name) {
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

        static int dateFormat(String name, String defaultValue) {
            String value = System.getenv(name);
            String normalized = value == null || value.isBlank() ? defaultValue : value.trim().toUpperCase();
            return switch (normalized) {
                case "MDY" -> AS400Date.FORMAT_MDY;
                case "DMY" -> AS400Date.FORMAT_DMY;
                case "YMD" -> AS400Date.FORMAT_YMD;
                case "JUL" -> AS400Date.FORMAT_JUL;
                case "ISO" -> AS400Date.FORMAT_ISO;
                case "USA" -> AS400Date.FORMAT_USA;
                case "EUR" -> AS400Date.FORMAT_EUR;
                case "JIS" -> AS400Date.FORMAT_JIS;
                default -> throw new IllegalArgumentException(
                        name + " must be one of MDY, DMY, YMD, JUL, ISO, USA, EUR or JIS");
            };
        }

        static char dateSeparator(String name, char defaultValue) {
            String value = System.getenv(name);
            if (value == null || value.isBlank()) {
                return defaultValue;
            }
            String trimmed = value.trim();
            if (trimmed.length() != 1) {
                throw new IllegalArgumentException(name + " must contain exactly one character");
            }
            return trimmed.charAt(0);
        }

        static int timeFormat(String name, String defaultValue) {
            String value = System.getenv(name);
            String normalized = value == null || value.isBlank() ? defaultValue : value.trim().toUpperCase();
            return switch (normalized) {
                case "HMS" -> AS400Time.FORMAT_HMS;
                case "ISO" -> AS400Time.FORMAT_ISO;
                case "USA" -> AS400Time.FORMAT_USA;
                case "EUR" -> AS400Time.FORMAT_EUR;
                case "JIS" -> AS400Time.FORMAT_JIS;
                default -> throw new IllegalArgumentException(
                        name + " must be one of HMS, ISO, USA, EUR or JIS");
            };
        }

        static char timeSeparator(String name, char defaultValue) {
            String value = System.getenv(name);
            if (value == null || value.isBlank()) {
                return defaultValue;
            }
            String trimmed = value.trim();
            if (trimmed.length() != 1) {
                throw new IllegalArgumentException(name + " must contain exactly one character");
            }
            return trimmed.charAt(0);
        }
    }

    public record WindowRequest(
            String receiver,
            String receiverLibrary,
            String startSequence,
            String endSequence,
            int maxServerEntries,
            int maxDecodedEntries,
            Optional<Boolean> verbose,
            Optional<String> rawHighWatermarkSequence,
            Optional<Path> rawDirectory,
            Optional<Path> checkpointFile) {

        static WindowRequest fromEnvironment() {
            int maxServerEntries = ConnectionSettings.positiveInteger("ISERIES_MAX_SERVER_ENTRIES", 1000);
            int maxDecodedEntries = ConnectionSettings.positiveInteger("ISERIES_MAX_DECODED_ENTRIES", 100);
            return new WindowRequest(
                    ConnectionSettings.required("ISERIES_RECEIVER"),
                    ConnectionSettings.required("ISERIES_RECEIVER_LIBRARY"),
                    ConnectionSettings.required("ISERIES_START_SEQUENCE"),
                    ConnectionSettings.required("ISERIES_END_SEQUENCE"),
                    maxServerEntries,
                    maxDecodedEntries,
                    Optional.empty(),
                    ConnectionSettings.optionalValue("AS400_RAW_HIGH_WATERMARK_SEQUENCE"),
                    ConnectionSettings.optionalPath("AS400_RAW_DIRECTORY"),
                    ConnectionSettings.optionalPath("AS400_CHECKPOINT_FILE"));
        }

        static WindowRequest fromJson(String json) {
            Map<String, String> fields = parseFlatJson(json);
            if ("shutdown".equalsIgnoreCase(fields.get("cmd"))) {
                throw new IllegalArgumentException("shutdown is not a window");
            }
            String startSequence = requiredField(fields, "start_sequence");
            String endSequence = requiredField(fields, "end_sequence");
            int maxServerEntries = integerField(fields, "max_server_entries")
                    .orElseGet(() -> defaultBoundedRange(startSequence, endSequence));
            int maxDecodedEntries = integerField(fields, "max_decoded_entries").orElse(maxServerEntries);
            if (maxServerEntries < 1 || maxDecodedEntries < 1) {
                throw new IllegalArgumentException("window entry limits must be positive");
            }
            return new WindowRequest(
                    requiredField(fields, "receiver"),
                    requiredField(fields, "receiver_library"),
                    startSequence,
                    endSequence,
                    maxServerEntries,
                    maxDecodedEntries,
                    booleanField(fields, "verbose"),
                    optionalField(fields, "high_watermark_sequence"),
                    optionalPathField(fields, "raw_directory"),
                    optionalPathField(fields, "checkpoint_file"));
        }

        int boundedRange(BigInteger start, BigInteger end) {
            BigInteger distance = end.subtract(start);
            if (distance.signum() < 0) {
                throw new IllegalArgumentException("ISERIES_END_SEQUENCE must not precede start");
            }
            BigInteger configured = BigInteger.valueOf(maxServerEntries);
            BigInteger bounded = distance.add(BigInteger.ONE).min(configured);
            try {
                return bounded.intValueExact();
            }
            catch (ArithmeticException e) {
                throw new IllegalArgumentException("bounded journal window is too large", e);
            }
        }

        private static String requiredField(Map<String, String> fields, String name) {
            String value = fields.get(name);
            if (value == null || value.isBlank()) {
                throw new IllegalArgumentException(name + " is required");
            }
            return value;
        }

        private static Optional<String> optionalField(Map<String, String> fields, String name) {
            String value = fields.get(name);
            return value == null || value.isBlank() ? Optional.empty() : Optional.of(value);
        }

        private static Optional<Path> optionalPathField(Map<String, String> fields, String name) {
            return optionalField(fields, name).map(Path::of);
        }

        private static OptionalInt integerField(Map<String, String> fields, String name) {
            String value = fields.get(name);
            if (value == null || value.isBlank()) {
                return OptionalInt.empty();
            }
            return OptionalInt.of(Integer.parseInt(value));
        }

        private static Optional<Boolean> booleanField(Map<String, String> fields, String name) {
            String value = fields.get(name);
            if (value == null || value.isBlank()) {
                return Optional.empty();
            }
            if ("true".equalsIgnoreCase(value)) {
                return Optional.of(true);
            }
            if ("false".equalsIgnoreCase(value)) {
                return Optional.of(false);
            }
            throw new IllegalArgumentException(name + " must be true or false");
        }

        private static int defaultBoundedRange(String startSequence, String endSequence) {
            BigInteger start = new BigInteger(startSequence);
            BigInteger end = new BigInteger(endSequence);
            BigInteger distance = end.subtract(start);
            if (distance.signum() < 0) {
                throw new IllegalArgumentException("end sequence must not precede start");
            }
            try {
                return distance.add(BigInteger.ONE).intValueExact();
            }
            catch (ArithmeticException e) {
                throw new IllegalArgumentException("bounded journal window is too large", e);
            }
        }
    }

    /**
     * Decodeur de lignes qui conserve le RRN porte par l'en-tete rjne0200.
     *
     * <p>Debezium lit le champ 10 de l'en-tete (« count/relative record
     * number », Bin8 non signe a +56) mais ne l'expose pas dans {@link
     * EntryHeader}. Sous {@code IMAGES(*AFTER)} ce RRN est la seule identite
     * durable d'une ligne supprimee : il doit donc survivre jusqu'a
     * l'evenement brut. Le fanion « incomplete data » (+218, bit 0x20) est
     * conserve aussi : une entree tronquee ne doit jamais passer pour un
     * delete {@code *AFTER} legitime.</p>
     */
    static final class RrnAwareFileDecoder extends JdbcFileDecoder {
        private static final int RRN_FIELD_OFFSET = 56;
        private static final int FLAGS_FIELD_OFFSET = 218;
        private static final int INCOMPLETE_DATA_FLAG = 0x20;

        private final ConnectionSettings settings;
        private long lastEntryRrn = -1;
        private boolean lastEntryIncomplete;

        RrnAwareFileDecoder(
                Connect<Connection, SQLException> jdbcConnection,
                String database,
                SchemaCacheHash schemaCache,
                ConnectionSettings settings) {
            super(jdbcConnection, database, schemaCache, -1, -1);
            this.settings = settings;
        }

        @Override
        public AS400DataType toDataType(
                String schema,
                String table,
                String column,
                String sqlType,
                int length,
                Integer scale) {
            // Le format d'une date appartient a la colonne : une table
            // peut ecrire 2025-05-26 et sa voisine 26.05.2025. Le type
            // ci-dessous essaie les formats compatibles avec la
            // longueur reelle du champ, et echoue si aucun ne convient.
            if ("DATE".equalsIgnoreCase(sqlType)) {
                return new MultiFormatJournalDate(length, settings.journalDateSeparator());
            }
            if ("TIME".equalsIgnoreCase(sqlType)) {
                return new MultiFormatJournalTime(length, settings.journalTimeSeparator());
            }
            return super.toDataType(schema, table, column, sqlType, length, scale);
        }

        @Override
        public Object[] decodeFile(EntryHeader entryHeader, byte[] data, int offset) throws Exception {
            lastEntryRrn = readUnsignedLong(data, offset + RRN_FIELD_OFFSET);
            lastEntryIncomplete = readFlag(data, offset + FLAGS_FIELD_OFFSET, INCOMPLETE_DATA_FLAG);
            return super.decodeFile(entryHeader, data, offset);
        }

        long lastEntryRrn() {
            return lastEntryRrn;
        }

        boolean lastEntryIncomplete() {
            return lastEntryIncomplete;
        }

        static long readUnsignedLong(byte[] data, int position) {
            if (position < 0 || position + 8 > data.length) {
                return -1;
            }
            long value = 0;
            for (int index = 0; index < 8; index++) {
                value = (value << 8) | (data[position + index] & 0xffL);
            }
            return value;
        }

        static boolean readFlag(byte[] data, int position, int mask) {
            return position >= 0 && position < data.length && (data[position] & mask) != 0;
        }
    }
}
