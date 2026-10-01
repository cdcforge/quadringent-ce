package io.quadringent.as400;

import com.ibm.as400.access.AS400DataType;
import com.ibm.as400.access.AS400Time;
import java.sql.Time;
import java.util.ArrayList;
import java.util.List;

/**
 * Une heure de journal dont le format appartient a la colonne, pas au produit.
 *
 * <p>Meme defaut que pour les dates, observe en production : le
 * produit supposait un separateur '.' alors que la donnee ecrit 14:00:06, et le
 * decodage echouait des la premiere ligne de cette forme. La capture
 * s'arretait, puis le disjoncteur coupait le service.</p>
 *
 * <p>La longueur du champ restreint les formats possibles. Le decodage essaie
 * les candidats dans un ordre stable et rend le premier qui accepte la valeur ;
 * une valeur qu'aucun candidat n'accepte leve toujours une erreur, elle n'est
 * jamais remplacee en silence.</p>
 */
public final class MultiFormatJournalTime implements AS400DataType {

    private final int byteLength;
    private final List<AS400Time> candidates;

    public MultiFormatJournalTime(int byteLength, Character preferredSeparator) {
        this.byteLength = byteLength;
        this.candidates = List.copyOf(candidatesFor(byteLength, preferredSeparator));
    }

    private static List<AS400Time> candidatesFor(int byteLength, Character preferredSeparator) {
        List<AS400Time> ordered = new ArrayList<>();
        if (byteLength == 8) {
            ordered.add(new AS400Time(AS400Time.FORMAT_ISO, ':'));
            ordered.add(new AS400Time(AS400Time.FORMAT_EUR, '.'));
            ordered.add(new AS400Time(AS400Time.FORMAT_ISO, '.'));
            ordered.add(new AS400Time(AS400Time.FORMAT_EUR, ':'));
            ordered.add(new AS400Time(AS400Time.FORMAT_HMS, ':'));
            ordered.add(new AS400Time(AS400Time.FORMAT_HMS, '.'));
            ordered.add(new AS400Time(AS400Time.FORMAT_JIS, ':'));
        }
        else if (byteLength == 6 || byteLength == 5) {
            ordered.add(new AS400Time(AS400Time.FORMAT_HMS, ':'));
            ordered.add(new AS400Time(AS400Time.FORMAT_HMS, '.'));
        }
        if (preferredSeparator != null) {
            ordered.add(new AS400Time(AS400Time.FORMAT_ISO, preferredSeparator));
            ordered.add(new AS400Time(AS400Time.FORMAT_HMS, preferredSeparator));
        }
        if (ordered.isEmpty()) {
            ordered.add(new AS400Time(AS400Time.FORMAT_ISO, ':'));
        }
        return ordered;
    }

    @Override
    public Object toObject(byte[] serverValue) {
        RuntimeException last = null;
        for (AS400Time candidate : candidates) {
            try {
                return candidate.toObject(serverValue);
            }
            catch (RuntimeException error) {
                last = error;
            }
        }
        throw last == null ? new IllegalArgumentException("journal time is undecodable") : last;
    }

    @Override
    public Object toObject(byte[] serverValue, int offset) {
        RuntimeException last = null;
        for (AS400Time candidate : candidates) {
            try {
                return candidate.toObject(serverValue, offset);
            }
            catch (RuntimeException error) {
                last = error;
            }
        }
        throw last == null ? new IllegalArgumentException("journal time is undecodable") : last;
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
    public Class<Time> getJavaType() {
        return Time.class;
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
