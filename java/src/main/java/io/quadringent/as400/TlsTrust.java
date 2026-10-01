package io.quadringent.as400;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.security.cert.Certificate;
import java.security.cert.CertificateFactory;
import java.util.Collection;
import java.util.Locale;

import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;

/**
 * Installs the IBM i CA into the JVM default SSL context — or leaves the
 * JVM's own default trust store untouched when no private CA is declared.
 *
 * <p>Trust chain (objective A, 2026-09-24 TLS trust chain fix):
 * <ul>
 *   <li>{@code AS400_TLS_CA_FILE} unset/blank: no file required — a
 *       publicly-issued certificate (e.g. a public CA chaining to a
 *       well-known root) is already trusted by the JVM's default trust
 *       store, so this is a no-op. There used to be a hard-coded default
 *       path constant here; a Dockerfile that set {@code AS400_TLS_CA_FILE}
 *       to that same default unconditionally, with no file actually shipped
 *       in the image, made every probe against a publicly-trusted IBM i
 *       throw before producing any diagnostic — the bug this class now
 *       avoids by construction.</li>
 *   <li>{@code AS400_TLS_CA_FILE} set: the file must be a readable PEM CA —
 *       a variable pointing at a missing/unreadable file is an explicit,
 *       fail-closed error (never silently ignored, never a trust-all
 *       fallback), and a private key in that file is refused.</li>
 * </ul>
 * There is no trust-all path.
 */
public final class TlsTrust {
    static final String ENV_CA_FILE = "AS400_TLS_CA_FILE";

    private TlsTrust() {
    }

    public static void install(boolean tls) {
        installFrom(tls, System.getenv(ENV_CA_FILE));
    }

    /**
     * Package-private seam for tests: same logic as {@link #install(boolean)}
     * but with the raw {@code AS400_TLS_CA_FILE} value injected instead of
     * read from the real process environment (which the JVM does not let a
     * test override).
     */
    static void installFrom(boolean tls, String rawEnvValue) {
        if (!tls) {
            return;
        }
        Path caFile = caFile(rawEnvValue);
        if (caFile == null) {
            // Aucune autorité privée déclarée : on laisse le magasin de
            // confiance JVM/système par défaut en place — suffisant pour
            // une autorité publique (Sectigo, etc.), jamais un trust-all.
            return;
        }
        if (!Files.isRegularFile(caFile) || !Files.isReadable(caFile)) {
            throw new IllegalArgumentException(
                    ENV_CA_FILE + " must point to a readable PEM CA when set: " + caFile);
        }
        try {
            String pem = Files.readString(caFile, StandardCharsets.US_ASCII);
            if (pem.toUpperCase(Locale.ROOT).contains("PRIVATE KEY")) {
                throw new IllegalArgumentException(ENV_CA_FILE + " must not contain a PRIVATE KEY");
            }
            Collection<? extends Certificate> certificates = loadCertificates(pem);
            if (certificates.isEmpty()) {
                throw new IllegalArgumentException(ENV_CA_FILE + " does not contain an X.509 certificate");
            }
            KeyStore store = KeyStore.getInstance(KeyStore.getDefaultType());
            store.load(null, null);
            int index = 0;
            for (Certificate certificate : certificates) {
                store.setCertificateEntry("ibmi-ca-" + index, certificate);
                index += 1;
            }
            TrustManagerFactory factory = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
            factory.init(store);
            SSLContext context = SSLContext.getInstance("TLS");
            context.init(null, factory.getTrustManagers(), null);
            SSLContext.setDefault(context);
        }
        catch (IllegalArgumentException error) {
            throw error;
        }
        catch (Exception error) {
            throw new IllegalArgumentException("IBM i TLS CA could not be installed", error);
        }
    }

    static Path caFile() {
        return caFile(System.getenv(ENV_CA_FILE));
    }

    static Path caFile(String rawEnvValue) {
        if (rawEnvValue == null || rawEnvValue.isBlank()) {
            return null;
        }
        return Path.of(rawEnvValue);
    }

    private static Collection<? extends Certificate> loadCertificates(String pem) throws Exception {
        CertificateFactory factory = CertificateFactory.getInstance("X.509");
        try (InputStream stream = new ByteArrayInputStream(pem.getBytes(StandardCharsets.US_ASCII))) {
            return factory.generateCertificates(stream);
        }
        catch (IOException error) {
            throw new IllegalArgumentException(ENV_CA_FILE + " could not be parsed as PEM", error);
        }
    }
}
