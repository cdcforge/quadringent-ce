package io.quadringent.as400;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * Tests hors ligne pour la sonde de tail (formattage pur, sans connexion
 * JDBC) et pour les garanties SQL du texte source de la requete bornee.
 */
public final class ReadOnlyReceiverCatalogTailTest {
    private ReadOnlyReceiverCatalogTailTest() {
    }

    public static void main(String[] args) throws Exception {
        testFormatsAllFields();
        testFormatsMissingBoundsAsDash();
        testFieldsAreOrderIndependentKeyValuePairs();
        testSqlIsBoundedToTheAttachedReceiver();
        System.out.println("ReadOnlyReceiverCatalogTailTest passed");
    }

    private static void testFormatsAllFields() {
        String line = ReadOnlyReceiverCatalog.formatTailLine("QGPL", "R10", "ATTACHED", "111", "140");
        expect(
                "line",
                "tail receiver=R10 library=QGPL first_sequence=111 last_sequence=140 status=ATTACHED"
                        .equals(line));
    }

    private static void testFormatsMissingBoundsAsDash() {
        String line = ReadOnlyReceiverCatalog.formatTailLine("QGPL", "R10", null, null, null);
        expect(
                "dash bounds",
                "tail receiver=R10 library=QGPL first_sequence=- last_sequence=- status=-".equals(line));
    }

    private static void testFieldsAreOrderIndependentKeyValuePairs() {
        // Le cote Python parse par decoupage sur les espaces puis "=" : l'ordre
        // des champs n'est pas contractuel, mais chaque cle doit apparaitre une
        // seule fois avec une valeur non vide.
        String line = ReadOnlyReceiverCatalog.formatTailLine("QGPL", "R10", "ATTACHED", "111", "140");
        String[] tokens = line.substring("tail ".length()).split(" ");
        expect("five fields", tokens.length == 5);
        for (String token : tokens) {
            expect("token has key and value " + token, token.contains("=") && token.split("=", 2)[1].length() > 0);
        }
    }

    private static void testSqlIsBoundedToTheAttachedReceiver() throws Exception {
        Path sourcePath = Path.of("java/src/main/java/io/quadringent/as400/ReadOnlyReceiverCatalog.java");
        if (!Files.isRegularFile(sourcePath)) {
            sourcePath = Path.of("src/main/java/io/quadringent/as400/ReadOnlyReceiverCatalog.java");
        }
        String source = Files.readString(sourcePath, StandardCharsets.UTF_8);
        expect("writeTail exists", source.contains("static void writeTail("));
        expect("bounded to journal library", source.contains("JOURNAL_LIBRARY = ?"));
        expect("bounded to journal name", source.contains("JOURNAL_NAME = ?"));
        expect("bounded to attached status", source.contains("STATUS = 'ATTACHED'"));
        expect("single row", source.contains("FETCH FIRST 1 ROWS ONLY"));
        expect("bounded query timeout", source.contains("statement.setQueryTimeout(catalogQueryTimeoutSeconds())"));
        // La requete tail ne doit jamais lire l'image des entrees : uniquement
        // les metadonnees du receiver.
        expect("no entry data column", !source.contains("ENTRY_DATA"));
    }

    private static void expect(String name, boolean condition) {
        if (!condition) {
            throw new AssertionError(name);
        }
    }
}
