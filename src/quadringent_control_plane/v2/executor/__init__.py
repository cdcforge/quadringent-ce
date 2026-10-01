"""Exécuteur Kubernetes v2 (chantier 4 — démarrage automatique et pilotage).

Sous-modules :

- ``boundary`` : protocole de bascule journal (lecture de position, copie
  initiale, bascule explicite jamais reculée) — fonctions pures, aucun I/O.
- ``manifests`` : construction pure des objets Kubernetes désirés (Deployment
  de capture, Job de copie initiale, Job de rejeu) à partir de l'état
  déclaré ; aucun appel réseau.
- ``evidence`` : lecture/écriture de la preuve durable de fin de copie
  initiale (objet JSON sur le stockage objet), qui autorise la transition
  ``copying -> live``.
- ``reconcile`` : boucle de réconciliation pure (désiré vs observé) —
  produit une liste d'actions Kubernetes à appliquer, jamais un effet direct.
- ``kubernetes`` : implémentation ``PipelineExecutorProtocol`` qui assemble
  les modules ci-dessus avec un client Kubernetes injecté (faux en tests).
"""
