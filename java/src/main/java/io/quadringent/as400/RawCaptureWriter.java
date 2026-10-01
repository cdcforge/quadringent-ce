package io.quadringent.as400;

import java.io.IOException;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.Date;
import java.sql.Time;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.TreeMap;

import io.debezium.ibmi.db2.journal.retrieve.JournalEntryType;
import io.debezium.ibmi.db2.journal.retrieve.JournalInfo;
import io.debezium.ibmi.db2.journal.retrieve.JournalReceiver;
import io.debezium.ibmi.db2.journal.retrieve.SchemaCacheIF.Structure;
import io.debezium.ibmi.db2.journal.retrieve.SchemaCacheIF.TableInfo;
import io.debezium.ibmi.db2.journal.retrieve.rjne0200.EntryHeader;

/**
 * Small append-only raw adapter for the bounded IBM i proof.
 *
 * <p>The adapter deliberately has no cloud SDK dependency. The JSONL and
 * manifest semantics are the contract that an S3 adapter must preserve: the
 * payload is made durable before the caller writes the checkpoint.</p>
 */
public final class RawCaptureWriter {
    public static final String FORMAT_VERSION = "as400-raw-v1";
    /** Reserved image field carrying the physical row position (journal RRN). */
    public static final String RRN_FIELD = "_rrn";

    private final Path root;

    public RawCaptureWriter(Path root) throws IOException {
        this.root = root;
        Files.createDirectories(root);
    }

    public RawEvent event(
            String sourceSystem,
            JournalInfo journal,
            EntryHeader header,
            JournalEntryType type,
            TableInfo tableInfo,
            Object[] values,
            String fallbackReceiver,
            String fallbackReceiverLibrary,
            String commitTimestamp,
            long rrn) {
        Objects.requireNonNull(type, "journal entry type");
        String operation = operation(type);
        if (operation == null) {
            throw new IllegalArgumentException("not a row entry: " + type);
        }
        Map<String, Object> image = rowImage(tableInfo, type, values, rrn);
        Map<String, Object> before = type == JournalEntryType.DELETE_ROW || type == JournalEntryType.BEFORE_IMAGE
                ? image
                : null;
        Map<String, Object> after = type == JournalEntryType.ADD_ROW1
                || type == JournalEntryType.ADD_ROW2
                || type == JournalEntryType.AFTER_IMAGE
                ? image
                : null;
        String receiver = header.hasReceiver() ? header.getReceiver() : fallbackReceiver;
        String receiverLibrary = header.hasReceiver() ? header.getReceiverLibrary() : fallbackReceiverLibrary;
        if (receiver == null || receiver.isBlank() || receiverLibrary == null || receiverLibrary.isBlank()) {
            throw new IllegalArgumentException("decoded journal entry has no receiver");
        }
        String eventId = sha256(sourceSystem + "|" + journal.journalName() + "|" + receiver + "|"
                + header.getSequenceNumber());
        return new RawEvent(
                eventId,
                sourceSystem,
                journal.journalName(),
                header.getLibrary(),
                header.getFile(),
                operation,
                receiver,
                receiverLibrary,
                header.getSequenceNumber().toString(),
                commitTimestamp,
                schemaVersion(tableInfo),
                before,
                after,
                type.name());
    }

    public RawEvent eventFromSql(
            String sourceSystem,
            JournalInfo journal,
            String library,
            String table,
            JournalEntryType type,
            TableInfo tableInfo,
            Object[] values,
            String receiver,
            String receiverLibrary,
            String sequence,
            String commitTimestamp,
            long rrn) {
        Objects.requireNonNull(type, "journal entry type");
        String operation = operation(type);
        if (operation == null) {
            throw new IllegalArgumentException("not a row entry: " + type);
        }
        Map<String, Object> image = rowImage(tableInfo, type, values, rrn);
        if (image.isEmpty()) {
            throw new IllegalStateException(
                    "journal row image incomplete sequence=" + sequence + " type=" + type
                            + "; checkpoint must not advance");
        }
        Map<String, Object> before = type == JournalEntryType.DELETE_ROW || type == JournalEntryType.BEFORE_IMAGE
                ? image
                : null;
        Map<String, Object> after = type == JournalEntryType.ADD_ROW1
                || type == JournalEntryType.ADD_ROW2
                || type == JournalEntryType.AFTER_IMAGE
                ? image
                : null;
        String eventId = sha256(sourceSystem + "|" + journal.journalName() + "|" + receiver + "|" + sequence);
        return new RawEvent(
                eventId,
                sourceSystem,
                journal.journalName(),
                library,
                table,
                operation,
                receiver,
                receiverLibrary,
                sequence,
                commitTimestamp,
                schemaVersion(tableInfo),
                before,
                after,
                type.name());
    }

    public RawCaptureResult writeBatch(List<RawEvent> events, RawPosition highWatermark) throws IOException {
        if (events == null || events.isEmpty()) {
            throw new IllegalArgumentException("cannot write an empty raw batch");
        }
        List<RawEvent> uniqueEvents = deduplicate(events);
        BigInteger previousSequence = null;
        for (RawEvent event : uniqueEvents) {
            if (!event.receiver().equals(highWatermark.receiver())) {
                throw new IllegalArgumentException("raw batch cannot cross receivers implicitly");
            }
            BigInteger sequence = new BigInteger(event.sequence());
            if (previousSequence != null && sequence.compareTo(previousSequence) < 0) {
                throw new IllegalArgumentException("raw batch events must be ordered by journal sequence");
            }
            if (sequence.compareTo(new BigInteger(highWatermark.sequence())) > 0) {
                throw new IllegalArgumentException("high watermark must cover every event");
            }
            previousSequence = sequence;
        }

        StringBuilder payloadBuilder = new StringBuilder();
        for (RawEvent event : uniqueEvents) {
            payloadBuilder.append(event.toJson()).append('\n');
        }
        byte[] payload = payloadBuilder.toString().getBytes(StandardCharsets.UTF_8);
        String payloadSha256 = sha256(payload);
        Map<String, Object> identity = new LinkedHashMap<>();
        identity.put("format_version", FORMAT_VERSION);
        identity.put("event_ids", uniqueEvents.stream().map(RawEvent::eventId).toList());
        identity.put("high_watermark", Map.of(
                "receiver", highWatermark.receiver(),
                "sequence", new BigInteger(highWatermark.sequence())));
        identity.put("payload_sha256", payloadSha256);
        // Keep this JSON canonical and identical to poc/as400_ingestion/raw.py:
        // sorted object keys, compact separators, UTF-8, event order preserved.
        String batchId = sha256(toJson(identity)).substring(0, 32);
        Path payloadPath = root.resolve("batch-" + batchId + ".jsonl");
        Path manifestPath = root.resolve("batch-" + batchId + ".manifest.json");
        boolean payloadExisted = Files.exists(payloadPath);
        boolean manifestExisted = Files.exists(manifestPath);
        writeOnce(payloadPath, payload);

        Map<String, Object> manifest = new LinkedHashMap<>();
        manifest.put("batch_id", batchId);
        manifest.put("format_version", FORMAT_VERSION);
        manifest.put("event_count", uniqueEvents.size());
        manifest.put("event_ids", uniqueEvents.stream().map(RawEvent::eventId).toList());
        manifest.put("high_watermark", Map.of(
                "receiver", highWatermark.receiver(),
                "sequence", new BigInteger(highWatermark.sequence())));
        manifest.put("payload_sha256", payloadSha256);
        writeOnce(manifestPath, (toJson(manifest) + "\n").getBytes(StandardCharsets.UTF_8));
        return new RawCaptureResult(batchId, uniqueEvents.size(), highWatermark, payloadSha256,
                payloadExisted, manifestExisted, payloadPath, manifestPath);
    }

    private static List<RawEvent> deduplicate(List<RawEvent> events) {
        Map<String, RawEvent> unique = new LinkedHashMap<>();
        for (RawEvent event : events) {
            RawEvent previous = unique.putIfAbsent(event.eventId(), event);
            if (previous != null && !previous.toJson().equals(event.toJson())) {
                throw new IllegalArgumentException("journal position reused with different content: " + event.eventId());
            }
        }
        return new ArrayList<>(unique.values());
    }

    /** Commit only after {@link #writeBatch(List, RawPosition)} has returned. */
    public boolean commitCheckpoint(Path checkpoint, RawPosition position) throws IOException {
        Files.createDirectories(checkpoint.toAbsolutePath().getParent());
        if (Files.exists(checkpoint)) {
            String existing = Files.readString(checkpoint, StandardCharsets.UTF_8);
            String previousReceiver = jsonString(existing, "receiver");
            BigInteger previousSequence = new BigInteger(jsonString(existing, "sequence"));
            if (!position.receiver().equals(previousReceiver)) {
                throw new IllegalArgumentException("receiver rotation requires explicit ordering");
            }
            BigInteger currentSequence = new BigInteger(position.sequence());
            if (currentSequence.compareTo(previousSequence) < 0) {
                throw new IllegalArgumentException("checkpoint moved backwards");
            }
            if (currentSequence.equals(previousSequence)) {
                return false;
            }
        }
        Map<String, Object> record = new LinkedHashMap<>();
        record.put("format_version", "as400-checkpoint-v1");
        record.put("receiver", position.receiver());
        record.put("sequence", new BigInteger(position.sequence()));
        writeAtomic(checkpoint, (toJson(record) + "\n").getBytes(StandardCharsets.UTF_8));
        return true;
    }

    /**
     * Construit l'image ligne d'un événement et y attache le RRN du journal.
     *
     * <p>Sous {@code IMAGES(*AFTER)}, un delete n'a aucune image : l'entrée ne
     * porte plus que la position physique de la ligne (RRN) dans l'en-tête.
     * On émet alors {@code before = {"_rrn": n}} — l'identité du delete reste
     * adressable et la validation/merge aval n'ont pas besoin d'un nouveau
     * type d'opération.</p>
     */
    private static Map<String, Object> rowImage(TableInfo tableInfo, JournalEntryType type, Object[] values, long rrn) {
        if (values.length == 0) {
            if (type == JournalEntryType.DELETE_ROW && rrn >= 0) {
                Map<String, Object> image = new LinkedHashMap<>();
                image.put(RRN_FIELD, rrn);
                return image;
            }
            return valuesByColumn(tableInfo, values);
        }
        Map<String, Object> image = valuesByColumn(tableInfo, values);
        if (rrn >= 0) {
            if (image.containsKey(RRN_FIELD)) {
                throw new IllegalArgumentException(
                        "table column collides with reserved journal identity field " + RRN_FIELD);
            }
            image.put(RRN_FIELD, rrn);
        }
        return image;
    }

    private static Map<String, Object> valuesByColumn(TableInfo tableInfo, Object[] values) {
        List<Structure> structures = tableInfo.getStructure();
        if (structures.size() != values.length) {
            throw new IllegalArgumentException("decoded field count does not match record format");
        }
        Map<String, Object> result = new LinkedHashMap<>();
        for (int i = 0; i < structures.size(); i++) {
            result.put(structures.get(i).getName(), normalize(values[i]));
        }
        return result;
    }

    private static String operation(JournalEntryType type) {
        return switch (type) {
            case ADD_ROW1, ADD_ROW2 -> "c";
            case AFTER_IMAGE -> "u_after";
            case BEFORE_IMAGE -> "u_before";
            case DELETE_ROW -> "d";
            default -> null;
        };
    }

    private static String schemaVersion(TableInfo tableInfo) {
        StringBuilder material = new StringBuilder();
        for (Structure structure : tableInfo.getStructure()) {
            material.append(structure.getName()).append('|')
                    .append(structure.getType()).append('|')
                    .append(structure.getJdcbType()).append('|')
                    .append(structure.getLength()).append('|')
                    .append(structure.getPrecision()).append('|')
                    .append(structure.isOptional()).append('|')
                    .append(structure.getPosition()).append('|')
                    .append(structure.isAutoinc()).append(';');
        }
        material.append("keys=").append(tableInfo.getPrimaryKeys());
        return "sha256:" + sha256(material.toString());
    }

    // Shared by journal decoding and the JDBC snapshot: both paths must emit
    // identical raw-v1 representations (not JVM object identity strings).
    static Object normalize(Object value) {
        if (value == null || value instanceof String || value instanceof Number || value instanceof Boolean) {
            return value;
        }
        if (value instanceof byte[] bytes) {
            return Map.of("encoding", "base64", "type", "bytes", "value", Base64.getEncoder().encodeToString(bytes));
        }
        if (value instanceof Date || value instanceof Time || value instanceof Timestamp
                || value instanceof java.util.Date || value instanceof Instant) {
            return value.toString();
        }
        if (value instanceof Character character) {
            return character.toString();
        }
        return String.valueOf(value);
    }

    private static void writeOnce(Path path, byte[] content) throws IOException {
        if (Files.exists(path)) {
            byte[] existing = Files.readAllBytes(path);
            if (!MessageDigest.isEqual(existing, content)) {
                throw new IOException("raw artifact collision with different content: " + path);
            }
            return;
        }
        writeAtomic(path, content);
    }

    private static void writeAtomic(Path path, byte[] content) throws IOException {
        Path parent = path.toAbsolutePath().getParent();
        Files.createDirectories(parent);
        Path temporary = Files.createTempFile(parent, "." + path.getFileName() + ".", ".tmp");
        try {
            try (var channel = java.nio.channels.FileChannel.open(temporary, java.nio.file.StandardOpenOption.WRITE)) {
                channel.write(java.nio.ByteBuffer.wrap(content));
                channel.force(true);
            }
            try {
                Files.move(temporary, path, StandardCopyOption.ATOMIC_MOVE);
            }
            catch (AtomicMoveNotSupportedException e) {
                Files.move(temporary, path);
            }
            try (var directory = java.nio.channels.FileChannel.open(parent, java.nio.file.StandardOpenOption.READ)) {
                directory.force(true);
            }
            catch (IOException ignored) {
                // Some filesystems do not allow fsync on directories.
            }
        }
        finally {
            Files.deleteIfExists(temporary);
        }
    }

    private static String jsonString(String json, String key) {
        String marker = "\"" + key + "\":";
        int start = json.indexOf(marker);
        if (start < 0) {
            throw new IllegalArgumentException("checkpoint missing " + key);
        }
        start += marker.length();
        while (start < json.length() && Character.isWhitespace(json.charAt(start))) {
            start++;
        }
        if (json.charAt(start) == '\"') {
            int end = json.indexOf('\"', start + 1);
            if (end < 0) {
                throw new IllegalArgumentException("invalid checkpoint " + key);
            }
            return json.substring(start + 1, end);
        }
        int end = start;
        while (end < json.length() && Character.isDigit(json.charAt(end))) {
            end++;
        }
        return json.substring(start, end);
    }

    private static String toJson(Object value) {
        StringBuilder output = new StringBuilder();
        appendJson(output, value);
        return output.toString();
    }

    private static void appendJson(StringBuilder output, Object value) {
        if (value == null) {
            output.append("null");
        }
        else if (value instanceof String || value instanceof Character || value instanceof Enum<?>) {
            appendQuoted(output, value.toString());
        }
        else if (value instanceof Boolean || value instanceof BigInteger || value instanceof BigDecimal
                || value instanceof Byte || value instanceof Short || value instanceof Integer
                || value instanceof Long) {
            output.append(value);
        }
        else if (value instanceof Float || value instanceof Double) {
            double number = ((Number) value).doubleValue();
            if (!Double.isFinite(number)) {
                appendQuoted(output, value.toString());
            }
            else {
                output.append(value);
            }
        }
        else if (value instanceof Map<?, ?> map) {
            output.append('{');
            boolean first = true;
            Map<String, Object> sorted = new TreeMap<>();
            for (Map.Entry<?, ?> entry : map.entrySet()) {
                sorted.put(String.valueOf(entry.getKey()), entry.getValue());
            }
            for (Map.Entry<String, Object> entry : sorted.entrySet()) {
                if (!first) {
                    output.append(',');
                }
                first = false;
                appendQuoted(output, entry.getKey());
                output.append(':');
                appendJson(output, entry.getValue());
            }
            output.append('}');
        }
        else if (value instanceof Iterable<?> iterable) {
            output.append('[');
            boolean first = true;
            for (Object item : iterable) {
                if (!first) {
                    output.append(',');
                }
                first = false;
                appendJson(output, item);
            }
            output.append(']');
        }
        else {
            appendQuoted(output, value.toString());
        }
    }

    private static void appendQuoted(StringBuilder output, String value) {
        output.append('"');
        for (int i = 0; i < value.length(); i++) {
            char character = value.charAt(i);
            switch (character) {
                case '"' -> output.append("\\\"");
                case '\\' -> output.append("\\\\");
                case '\b' -> output.append("\\b");
                case '\f' -> output.append("\\f");
                case '\n' -> output.append("\\n");
                case '\r' -> output.append("\\r");
                case '\t' -> output.append("\\t");
                default -> {
                    if (character < 0x20) {
                        output.append(String.format("\\u%04x", (int) character));
                    }
                    else {
                        output.append(character);
                    }
                }
            }
        }
        output.append('"');
    }

    private static String sha256(String value) {
        return sha256(value.getBytes(StandardCharsets.UTF_8));
    }

    private static String sha256(byte[] value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(value);
            StringBuilder result = new StringBuilder(digest.length * 2);
            for (byte item : digest) {
                result.append(String.format("%02x", item));
            }
            return result.toString();
        }
        catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is required", e);
        }
    }

    public record RawPosition(String receiver, String sequence) {
        public RawPosition {
            if (receiver == null || receiver.isBlank() || sequence == null || sequence.isBlank()) {
                throw new IllegalArgumentException("raw position must be complete");
            }
            new BigInteger(sequence);
        }
    }

    public record RawEvent(
            String eventId,
            String sourceSystem,
            String journal,
            String library,
            String table,
            String operation,
            String receiver,
            String receiverLibrary,
            String sequence,
            String commitTimestamp,
            String schemaVersion,
            Map<String, Object> before,
            Map<String, Object> after,
            String journalEntryType) {

        String toJson() {
            Map<String, Object> record = new LinkedHashMap<>();
            record.put("event_id", eventId);
            record.put("source_system", sourceSystem);
            record.put("journal", journal);
            record.put("library", library);
            record.put("table", table);
            record.put("operation", operation);
            record.put("journal_receiver", receiver);
            record.put("journal_receiver_library", receiverLibrary);
            record.put("journal_sequence", new BigInteger(sequence));
            record.put("commit_timestamp", commitTimestamp);
            record.put("schema_version", schemaVersion);
            record.put("before", before);
            record.put("after", after);
            record.put("journal_entry_type", journalEntryType);
            return RawCaptureWriter.toJson(record);
        }
    }

    public record RawCaptureResult(
            String batchId,
            int eventCount,
            RawPosition highWatermark,
            String payloadSha256,
            boolean payloadExisted,
            boolean manifestExisted,
            Path payloadPath,
            Path manifestPath) {
    }
}
