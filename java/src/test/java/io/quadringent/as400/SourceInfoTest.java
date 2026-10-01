package io.quadringent.as400;

/**
 * Tests offline (aucune connexion JDBC) du formatage pur de {@link SourceInfo}
 * — chantier « prod-wiring » (sonde de source via JTOpen, jamais ODBC).
 */
public final class SourceInfoTest {
    private static int checks = 0;

    private SourceInfoTest() {
    }

    public static void main(String[] args) {
        testFormatLineWithValue();
        testFormatLineWithNullValue();
        testFormatLineRejectsControlCharacters();
        System.out.println("SourceInfoTest: " + checks + " checks passed");
    }

    private static void check(boolean condition, String message) {
        checks++;
        if (!condition) {
            throw new AssertionError(message);
        }
    }

    private static void testFormatLineWithValue() {
        check("version\tV7R5M0".equals(SourceInfo.formatLine("version", "V7R5M0")), "value line");
    }

    private static void testFormatLineWithNullValue() {
        check("qtimzon\t".equals(SourceInfo.formatLine("qtimzon", null)), "null value stays an empty field, never absent");
    }

    private static void testFormatLineRejectsControlCharacters() {
        try {
            SourceInfo.formatLine("version", "bad\tvalue");
            check(false, "must reject a tab inside the value");
        }
        catch (IllegalStateException expected) {
            check(true, "rejected");
        }
    }
}
