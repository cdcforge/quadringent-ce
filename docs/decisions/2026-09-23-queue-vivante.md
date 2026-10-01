# Décision — lecture de la dernière entrée d’un receiver attaché

Date : 23 septembre 2026. Statut : acceptée.

## Contexte

Le planificateur de fenêtres (`plan_next_window`) ne lisait jamais
`last_sequence` d’un receiver `ATTACHED` (« queue vivante »), en invoquant un
comportement de RetrieveJournal. Conséquence observée en qualification : une
modification isolée n’est capturée qu’à l’écriture suivante ou à la rotation du
receiver. Pour une table peu active, la dernière ligne modifiée peut donc rester
invisible indéfiniment.

## Mesures

IBM i 7.5 partagé de qualification, table de test journalisée `IMAGES(*BOTH)`,
écritures isolées sans contrôle de validation, lecture immédiate de la fenêtre
`[last_sequence, last_sequence]` du receiver attaché, puis relecture 5 s après.

| Chemin | Itérations | Entrée complète à la première lecture | Identique 5 s après |
|---|---:|---:|---:|
| `QSYS2.DISPLAY_JOURNAL` (forme de `displayJournalSql`) | 20 | 20 | 20 |
| RetrieveJournal (`PersistentJournalWorker.processWindow`, image de capture) | 10 | 10 (`u_after`, lot durable) | 10 (mêmes octets) |

Un premier essai RetrieveJournal semblait bloquer après `window_flushed` : c’était
un défaut du harnais de mesure (`select()` sur un flux déjà mis en tampon), pas
du lecteur.

## Décision

La règle de queue vivante est retirée pour les deux chemins : une fenêtre peut
se terminer à `last_sequence` d’un receiver attaché, y compris juste après une
transition de receiver.

## Limites

- Mesure sans contrôle de validation (pas de `COMMIT` différé) : les entrées
  d’une transaction non validée restent un sujet distinct, non traité ici.
- Mesure sur une seule version d’IBM i et un serveur partagé ; la qualification
  continue la répète.
