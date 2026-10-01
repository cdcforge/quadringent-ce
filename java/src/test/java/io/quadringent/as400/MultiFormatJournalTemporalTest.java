package io.quadringent.as400;

import java.nio.charset.StandardCharsets;
import java.util.Objects;

import com.ibm.as400.access.AS400Date;
import com.ibm.as400.access.AS400Time;

/**
 * Contrat du decodage des dates et heures du journal.
 *
 * <p>Le format appartient a la colonne : une table peut ecrire 2025-05-26 et sa
 * voisine 26.05.2025. Ce test fixe les trois proprietes qui comptent : les deux
 * formes se decodent, une valeur impossible echoue toujours, et le format
 * configure reste un candidat.</p>
 */
public final class MultiFormatJournalTemporalTest {

    private static int checks = 0;

    private MultiFormatJournalTemporalTest() {
    }

    public static void main(String[] args) throws Exception {
        // Deux formes reelles, mesurees le 16/09 sur SALES.EXPENS.
        Object iso = new MultiFormatJournalDate(10, '-').toObject(bytes("2025-05-26"));
        Object european = new MultiFormatJournalDate(10, '-').toObject(bytes("26.05.2025"));
        equal(iso, european);

        // Une heure ecrite avec deux points, alors que le produit supposait '.',
        // doit aussi se decoder.
        Object time = new MultiFormatJournalTime(8, '.').toObject(bytes("14:00:06"));
        equal(true, time.toString().startsWith("14:00:06"));

        // Le format configure reste un candidat : rien n'est retire.
        Object configured = new MultiFormatJournalTime(8, ':').toObject(bytes("14:00:06"));
        equal(time, configured);

        // Une valeur qu'aucun candidat n'accepte echoue toujours : elle n'est
        // jamais remplacee en silence.
        // Une valeur qui n'est une date ou une heure dans aucun format connu
        // doit echouer : la tolerance ne remplace jamais une valeur illisible.
        refused(bytes("XXXXXXXXXX"));

        // Le format d'origine reste la reference, pour prouver que le defaut
        // corrige existait bien : ISO n'accepte pas la forme europeenne.
        boolean isoRejectsEuropean;
        try {
            new AS400Date(AS400Date.FORMAT_ISO, '-').toObject(bytes("26.05.2025"));
            isoRejectsEuropean = false;
        }
        catch (RuntimeException expected) {
            isoRejectsEuropean = true;
        }
        equal(true, isoRejectsEuropean);

        boolean dotRejectsColon;
        try {
            new AS400Time(AS400Time.FORMAT_EUR, '.').toObject(bytes("14:00:06"));
            dotRejectsColon = false;
        }
        catch (RuntimeException expected) {
            dotRejectsColon = true;
        }
        equal(true, dotRejectsColon);

        // La longueur annoncee est celle du champ, jamais une valeur inventee.
        equal(10, new MultiFormatJournalDate(10, '-').getByteLength());
        equal(8, new MultiFormatJournalTime(8, '.').getByteLength());

        System.out.println("multi_format_journal_temporal_tests=PASS checks=" + checks);
    }

    /**
     * Les octets d'un champ date ou heure sont ceux du serveur, donc EBCDIC :
     * les passer en UTF-8 ferait echouer la conversion avant meme le decodage.
     */
    private static byte[] bytes(String value) {
        try {
            return value.getBytes("IBM1047");
        }
        catch (java.io.UnsupportedEncodingException error) {
            throw new IllegalStateException("EBCDIC encoding is required", error);
        }
    }

    private static void refused(byte[] value) {
        try {
            new MultiFormatJournalDate(10, '-').toObject(value);
            throw new AssertionError("an impossible date must be refused");
        }
        catch (AssertionError error) {
            throw error;
        }
        catch (RuntimeException expected) {
            checks++;
        }
        try {
            new MultiFormatJournalTime(8, ':').toObject(value);
            throw new AssertionError("an impossible time must be refused");
        }
        catch (AssertionError error) {
            throw error;
        }
        catch (RuntimeException expected) {
            checks++;
        }
    }

    private static void equal(Object expected, Object actual) {
        checks++;
        if (!Objects.equals(expected, actual)) {
            throw new AssertionError(
                    "multi format temporal contract failed at check " + checks
                            + ": expected " + expected + " but was " + actual);
        }
    }
}
