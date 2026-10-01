package io.quadringent.as400;

import java.io.PrintStream;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;

/**
 * Version IBM i et {@code QTIMZON} — mesures pures pour la sonde de source
 * (chantier « prod-wiring », remplace un appel pyodbc/ODBC absent de
 * l'image de capture par les mêmes vues catalogue interrogées via la
 * connexion JDBC JTOpen déjà ouverte par {@link JournalSession}, comme
 * {@link TableDiscovery}).
 *
 * <p>Deux requêtes catalogue standard, jamais de ligne métier :
 * {@code SYSIBMADM.ENV_SYS_INFO} (version/release de l'OS ; {@code QSYS2.SYSTEM_STATUS_INFO} n'a pas ces colonnes, vérifié sur IBM i 7.5) et
 * {@code QSYS2.SYSTEM_VALUE_INFO} ({@code QTIMZON}).
 */
public final class SourceInfo {
    private SourceInfo() {
    }

    static final String VERSION_SQL = "SELECT OS_VERSION, OS_RELEASE FROM SYSIBMADM.ENV_SYS_INFO";
    static final String QTIMZON_SQL =
            "SELECT CURRENT_CHARACTER_VALUE FROM QSYS2.SYSTEM_VALUE_INFO WHERE SYSTEM_VALUE_NAME = 'QTIMZON'";
    private static final int QUERY_TIMEOUT_SECONDS = 10;

    /** Écrit ``version\t<V.R>`` puis ``qtimzon\t<valeur>`` — champ vide (jamais absent) si indisponible. */
    public static void write(Connection jdbc, PrintStream out) throws Exception {
        out.println(formatLine("version", queryVersion(jdbc)));
        out.println(formatLine("qtimzon", queryQtimzon(jdbc)));
    }

    static String queryVersion(Connection jdbc) throws Exception {
        try (PreparedStatement statement = jdbc.prepareStatement(VERSION_SQL)) {
            statement.setQueryTimeout(QUERY_TIMEOUT_SECONDS);
            try (ResultSet result = statement.executeQuery()) {
                if (!result.next()) {
                    return null;
                }
                String major = result.getString("OS_VERSION");
                String release = result.getString("OS_RELEASE");
                if (major == null || release == null) {
                    return null;
                }
                return major + "." + release;
            }
        }
    }

    static String queryQtimzon(Connection jdbc) throws Exception {
        try (PreparedStatement statement = jdbc.prepareStatement(QTIMZON_SQL)) {
            statement.setQueryTimeout(QUERY_TIMEOUT_SECONDS);
            try (ResultSet result = statement.executeQuery()) {
                if (!result.next()) {
                    return null;
                }
                String value = result.getString("CURRENT_CHARACTER_VALUE");
                return value == null ? null : value.trim();
            }
        }
    }

    /** Package-private pour {@code SourceInfoTest} — jamais de tabulation/retour ligne dans un champ. */
    static String formatLine(String key, String value) {
        if (value != null && (value.indexOf('\t') >= 0 || value.indexOf('\n') >= 0 || value.indexOf('\r') >= 0)) {
            throw new IllegalStateException(key + " must not contain control characters");
        }
        return key + "\t" + (value == null ? "" : value);
    }
}
