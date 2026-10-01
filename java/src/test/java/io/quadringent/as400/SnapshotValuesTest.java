package io.quadringent.as400;

import java.lang.reflect.InvocationTargetException;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.sql.Date;
import java.sql.Time;
import java.sql.Timestamp;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;

/** Synthetic values only: exercises the actual snapshot adapter and raw JSON. */
public final class SnapshotValuesTest {
    private static int checks;

    public static void main(String[] args) throws Exception {
        byte[] bytes = {0, 1, 127, (byte) 128, (byte) 255};
        Object binary = normalize(bytes);
        equal(Map.of("encoding", "base64", "type", "bytes", "value", "AAF/gP8="), binary);
        equal(true, java.util.Arrays.equals(bytes,
                Base64.getDecoder().decode((String) ((Map<?, ?>) binary).get("value"))));
        equal(Map.of("encoding", "base64", "type", "bytes", "value", ""), normalize(new byte[0]));
        equal(null, normalize(null));
        equal(" 001 é\u0000\n ", normalize(" 001 é\u0000\n "));
        equal(true, normalize(true));
        equal(new BigDecimal("12345678901234567890.123400"),
                normalize(new BigDecimal("12345678901234567890.123400")));
        equal(new BigInteger("123456789012345678901234567890"),
                normalize(new BigInteger("123456789012345678901234567890")));
        equal("2026-09-07", normalize(Date.valueOf("2026-09-07")));
        equal("22:11:42", normalize(Time.valueOf("22:11:42")));
        equal("2026-09-07 22:11:42.504176", normalize(Timestamp.valueOf("2026-09-07 22:11:42.504176")));
        for (Object number : new Object[] {(byte) 1, (short) 2, 3, 4L, 1.25f, 1.25d}) {
            equal(number, normalize(number));
        }
        rejects(Double.NaN);
        rejects(Double.POSITIVE_INFINITY);
        rejects(Float.NEGATIVE_INFINITY);
        rejects(new Object() {
            @Override public String toString() {
                throw new AssertionError("unsupported values must not be converted to text");
            }
        });
        rejects(new javax.sql.rowset.serial.SerialBlob(bytes));
        rejects(new javax.sql.rowset.serial.SerialClob("synthetic".toCharArray()));

        Map<String, Object> after = new LinkedHashMap<>();
        after.put("BIN", binary);
        after.put("NIL", normalize(null));
        after.put("DEC", normalize(new BigDecimal("12345678901234567890.123400")));
        after.put("TXT", normalize(" 001 é\u0000\n "));
        var row = new RawCaptureWriter.RawEvent("id", "ibmi", "SNAPSHOT:SALES.TEST", "SALES",
                "TEST", "c", "SNAPSHOT:attempt", "SNAPSHOT", "1", "2026-09-07T20:00:00Z",
                "snapshot-v1", null, after, "SNAPSHOT_ROW");
        String json = row.toJson();
        equal(true, json.contains("\"BIN\":{\"encoding\":\"base64\",\"type\":\"bytes\",\"value\":\"AAF/gP8=\"}"));
        equal(true, json.contains("\"DEC\":12345678901234567890.123400"));
        equal(true, json.contains("\"NIL\":null"));
        equal(true, json.contains("\"TXT\":\" 001 é\\u0000\\n \""));
        System.out.println("snapshot_values_tests=PASS checks=" + checks);
    }

    private static Object normalize(Object value) throws Exception {
        var method = ReadOnlyTableSnapshot.class.getDeclaredMethod("normalize", Object.class);
        method.setAccessible(true);
        try { return method.invoke(null, value); }
        catch (InvocationTargetException error) {
            if (error.getCause() instanceof Exception cause) throw cause;
            if (error.getCause() instanceof Error cause) throw cause;
            throw error;
        }
    }

    private static void equal(Object expected, Object actual) {
        checks++;
        if (!Objects.equals(expected, actual)) throw new AssertionError("snapshot value contract failed at check " + checks);
    }

    private static void rejects(Object value) throws Exception {
        checks++;
        try { normalize(value); }
        catch (IllegalArgumentException expected) { return; }
        throw new AssertionError("unsupported snapshot value was accepted");
    }
}
