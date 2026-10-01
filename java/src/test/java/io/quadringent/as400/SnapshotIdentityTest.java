package io.quadringent.as400;

public final class SnapshotIdentityTest {
    public static void main(String[] args) {
        String first = "438b9fdd-d15e-458a-8349-d11b8f6291a4";
        String second = "b9b206c2-ebde-42cb-9153-a507102960bd";
        SnapshotIdentity pays = new SnapshotIdentity("SALES", "CNTR", first);
        SnapshotIdentity sale = new SnapshotIdentity("SALES", "SALE", first);
        SnapshotIdentity otherRun = new SnapshotIdentity("SALES", "CNTR", second);
        SnapshotIdentity otherLibrary = new SnapshotIdentity("OTHERLIB", "CNTR", first);
        equal("66238d2bf6b365ad9fe41057bb3f2ee769c08c081d73731bb998fb4dec220a1d", pays.eventId(1));
        equal(pays.eventId(1), new SnapshotIdentity("SALES", "CNTR", first).eventId(1));
        different(pays.eventId(1), sale.eventId(1));
        different(pays.eventId(1), otherRun.eventId(1));
        different(pays.eventId(1), otherLibrary.eventId(1));
        different(pays.eventId(1), pays.eventId(2));
        rejects(() -> new SnapshotIdentity("SALES", "CNTR", ""));
        rejects(() -> new SnapshotIdentity("SALES", "CNTR", "1-1-1-1-1"));
        rejects(() -> new SnapshotIdentity("SALES", "CNTR;DROP", first));
        rejects(() -> pays.eventId(0));
        rejects(() -> pays.eventId(-1));
        System.out.println("snapshot_identity_tests=PASS checks=11");
    }
    private static void equal(String a, String b) {
        if (!a.equals(b)) throw new AssertionError("identity must be stable");
    }
    private static void different(String a, String b) {
        if (a.equals(b)) throw new AssertionError("snapshot identities collided");
    }
    private static void rejects(Runnable action) {
        try { action.run(); }
        catch (RuntimeException expected) { return; }
        throw new AssertionError("expected invalid identity rejection");
    }
}
