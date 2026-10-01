package io.quadringent.as400;

import com.ibm.as400.access.AS400DataType;
import com.ibm.as400.access.AS400Date;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;

/**
 * Une date de journal dont le format appartient a la colonne, pas au produit.
 *
 * <p>Le format d'une date IBM i est une propriete de la colonne : une table
 * peut ecrire 2025-05-26 et sa voisine 26.05.2025. Imposer un seul format
 * faisait echouer le decodage des la premiere ligne de l'autre forme, et la
 * capture s'arretait sur une exception de parametre — defaut observe en
 * production sur une table voisine.</p>
 *
 * <p>La longueur du champ, fournie par la definition de la table, restreint
 * les formats possibles : dix octets pour ISO, EUR, USA et JIS, huit pour
 * DMY, MDY et YMD, six pour le jour julien. Le decodage essaie les formats
 * compatibles dans un ordre stable et rend le premier qui accepte la valeur.
 * Aucun format n'est devine : une valeur qu'aucun candidat n'accepte leve
 * toujours une erreur, elle n'est jamais remplacee en silence.</p>
 */
public final class MultiFormatJournalDate implements AS400DataType {

    private final int byteLength;
    private final List<AS400Date> candidates;

    public MultiFormatJournalDate(int byteLength, Character preferredSeparator) {
        this.byteLength = byteLength;
        this.candidates = List.copyOf(candidatesFor(byteLength, preferredSeparator));
    }

    private static List<AS400Date> candidatesFor(int byteLength, Character preferredSeparator) {
        List<AS400Date> ordered = new ArrayList<>();
        if (byteLength == 10) {
            ordered.add(new AS400Date(AS400Date.FORMAT_ISO, '-'));
            ordered.add(new AS400Date(AS400Date.FORMAT_EUR, '.'));
            ordered.add(new AS400Date(AS400Date.FORMAT_USA, '/'));
            ordered.add(new AS400Date(AS400Date.FORMAT_JIS, '-'));
        }
        else if (byteLength == 8) {
            if (preferredSeparator != null) {
                ordered.add(new AS400Date(AS400Date.FORMAT_DMY, preferredSeparator));
            }
            ordered.add(new AS400Date(AS400Date.FORMAT_DMY, '.'));
            ordered.add(new AS400Date(AS400Date.FORMAT_DMY, '/'));
            ordered.add(new AS400Date(AS400Date.FORMAT_MDY, '/'));
            ordered.add(new AS400Date(AS400Date.FORMAT_YMD, '/'));
            ordered.add(new AS400Date(AS400Date.FORMAT_YMD, '.'));
        }
        else if (byteLength == 6) {
            ordered.add(new AS400Date(AS400Date.FORMAT_JUL, preferredSeparator == null ? '/' : preferredSeparator));
            ordered.add(new AS400Date(AS400Date.FORMAT_JUL, '/'));
        }
        if (ordered.isEmpty()) {
            // Longueur inattendue : le format configure reste le seul candidat,
            // ce qui garde le comportement precedent plutot que d'inventer.
            ordered.add(new AS400Date(AS400Date.FORMAT_ISO, '-'));
        }
        return ordered;
    }

    @Override
    public Object toObject(byte[] serverValue) {
        RuntimeException last = null;
        for (AS400Date candidate : candidates) {
            try {
                return candidate.toObject(serverValue);
            }
            catch (RuntimeException error) {
                last = error;
            }
        }
        throw last == null ? new IllegalArgumentException("journal date is undecodable") : last;
    }

    @Override
    public Object toObject(byte[] serverValue, int offset) {
        RuntimeException last = null;
        for (AS400Date candidate : candidates) {
            try {
                return candidate.toObject(serverValue, offset);
            }
            catch (RuntimeException error) {
                last = error;
            }
        }
        throw last == null ? new IllegalArgumentException("journal date is undecodable") : last;
    }

    @Override
    public int getByteLength() {
        return byteLength;
    }

    @Override
    public int getInstanceType() {
        return candidates.get(0).getInstanceType();
    }

    @Override
    public Class<Date> getJavaType() {
        return Date.class;
    }

    @Override
    public Object getDefaultValue() {
        return candidates.get(0).getDefaultValue();
    }

    @Override
    public byte[] toBytes(Object value) {
        return candidates.get(0).toBytes(value);
    }

    @Override
    public int toBytes(Object value, byte[] buffer) {
        return candidates.get(0).toBytes(value, buffer);
    }

    @Override
    public int toBytes(Object value, byte[] buffer, int offset) {
        return candidates.get(0).toBytes(value, buffer, offset);
    }

    @Override
    public Object clone() {
        return this;
    }
}
