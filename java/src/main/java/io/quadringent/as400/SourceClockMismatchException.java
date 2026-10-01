package io.quadringent.as400;

/**
 * Le fuseau déclaré ({@code AS400_SOURCE_TIME_ZONE}) ne correspond pas au
 * décalage UTC réel de la source IBM i (contrôle {@link
 * JournalTimestamps#verifyClock}).
 *
 * <p>Distincte des autres {@link IllegalStateException} de la capture : c'est
 * une erreur de configuration, jamais une coupure réseau ou une source en
 * maintenance — {@link FleetCatalogProbe#classifyConnectFailure} la classe en
 * {@code SOURCE_CLOCK_MISMATCH}, avant le repli générique {@code
 * CONNECTION_FAILED}, pour que le pilote Python (voir {@code
 * quadringent.source_gate.ConnectFailureClass}) la traite comme non
 * rejouable en l'état plutôt que comme une pause bornée.
 */
public final class SourceClockMismatchException extends IllegalStateException {
    private static final long serialVersionUID = 1L;

    public SourceClockMismatchException(String message) {
        super(message);
    }
}
