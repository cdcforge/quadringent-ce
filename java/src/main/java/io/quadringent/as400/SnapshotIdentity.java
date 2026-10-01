package io.quadringent.as400;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.UUID;

/** One full snapshot attempt; it is not a resumable ordinal cursor. */
public record SnapshotIdentity(String schema, String table, String runId) {
    public SnapshotIdentity {
        if (schema == null || !schema.matches("[A-Z#$@][A-Z0-9_#$@]{0,127}")
                || table == null || !table.matches("[A-Z#$@][A-Z0-9_#$@]{0,127}")) {
            throw new IllegalArgumentException("snapshot requires safe uppercase source identifiers");
        }
        if (runId == null || !UUID.fromString(runId).toString().equals(runId)) {
            throw new IllegalArgumentException("AS400_SNAPSHOT_RUN_ID must be a canonical UUID for this attempt");
        }
    }

    public String journal() {
        return "SNAPSHOT:" + schema + "." + table;
    }

    public String receiver() {
        return "SNAPSHOT:" + runId;
    }

    public String eventId(long rowNumber) {
        if (rowNumber < 1) throw new IllegalArgumentException("snapshot row number must be positive");
        // Keep exactly the ChangeEvent identity formula. Put table + attempt
        // in the namespace instead of inventing an incompatible hash contract.
        String identity = "ibmi|" + journal() + "|" + receiver() + "|" + rowNumber;
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(identity.getBytes(StandardCharsets.UTF_8)));
        }
        catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException("SHA-256 is required", error);
        }
    }
}
