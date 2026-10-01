package io.quadringent.as400;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * Offline tests for {@link DiagnosticWorker}: command protocol dispatch and a
 * source-inspection guard proving it never runs the capture-only checks.
 * No IBM i connection is opened here.
 */
public final class DiagnosticWorkerTest {
    private DiagnosticWorkerTest() {
    }

    public static void main(String[] args) throws Exception {
        testShutdownDetection();
        testDiscoverDetection();
        testProbeDetection();
        testSafeTokenStripsControlCharacters();
        testNeverCallsCaptureOnlyChecks();
        System.out.println("diagnostic_worker_tests=PASS");
    }

    private static void testShutdownDetection() {
        expect("bare shutdown", DiagnosticWorker.isShutdown("shutdown"));
        expect("case-insensitive shutdown", DiagnosticWorker.isShutdown("SHUTDOWN"));
        expect("json shutdown", DiagnosticWorker.isShutdown("{\"cmd\":\"shutdown\"}"));
        expect("probe is not shutdown", !DiagnosticWorker.isShutdown("probe"));
        expect("garbage is not shutdown", !DiagnosticWorker.isShutdown("not json"));
    }

    private static void testDiscoverDetection() {
        expect("json discover", DiagnosticWorker.isDiscover("{\"cmd\":\"discover\"}"));
        expect("bare discover word is not enough", !DiagnosticWorker.isDiscover("discover"));
        expect("probe is not discover", !DiagnosticWorker.isDiscover("{\"cmd\":\"probe\"}"));
        expect("garbage is not discover", !DiagnosticWorker.isDiscover("not json"));
    }

    private static void testProbeDetection() {
        expect("bare probe", DiagnosticWorker.isProbe("probe"));
        expect("case-insensitive probe", DiagnosticWorker.isProbe("PROBE"));
        expect("json probe", DiagnosticWorker.isProbe("{\"cmd\":\"probe\"}"));
        expect("discover is not probe", !DiagnosticWorker.isProbe("{\"cmd\":\"discover\"}"));
        expect("garbage is not probe", !DiagnosticWorker.isProbe("not json"));
    }

    private static void testSafeTokenStripsControlCharacters() {
        expect("newline stripped", !DiagnosticWorker.safeToken("a\nb").contains("\n"));
        expect("tab stripped", !DiagnosticWorker.safeToken("a\tb").contains("\t"));
        expect(
                "known-safe token round-trips",
                "process_window".equals(DiagnosticWorker.safeToken("process_window")));
    }

    /**
     * Inspection de la construction (voir la docstring de la classe) : la
     * connexion de {@link DiagnosticWorker} ne doit jamais dépendre du
     * fuseau source ni du journal capturé — impossible à exercer sans IBM i
     * réel, donc vérifié sur le texte source lui-même.
     */
    private static void testNeverCallsCaptureOnlyChecks() throws IOException {
        String sourcePath = System.getenv("DIAGNOSTIC_WORKER_SOURCE");
        if (sourcePath == null || sourcePath.isBlank()) {
            throw new AssertionError("DIAGNOSTIC_WORKER_SOURCE is required");
        }
        String text = Files.readString(Path.of(sourcePath), StandardCharsets.UTF_8);
        // Call-site patterns, not the bare method names: the class Javadoc
        // legitimately names these methods in prose to say they are *not*
        // invoked (see the class-level doc comment above).
        expect("never calls verifySourceClock", !text.contains(".verifySourceClock("));
        expect("never calls verifyCapturedJournal", !text.contains(".verifyCapturedJournal("));
        expect("never reads a captured table window", !text.contains(".processWindow("));
        expect("never reads a captured receiver catalog", !text.contains(".emitReceiverCatalog("));
    }

    private static void expect(String name, boolean condition) {
        if (!condition) {
            throw new AssertionError(name);
        }
    }
}
