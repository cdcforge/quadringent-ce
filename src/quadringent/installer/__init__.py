"""Installateur « mode par défaut » de Quadringent (chantier 6).

``quadringent install --cloud aws|gcp --target vm|cluster`` orchestre les
modules Terraform de ``deploy/terraform/`` puis la chart Helm ``chart/``
pour créer un site Quadringent minimal : stockage, état, identité bornée,
VM k3s (si demandé), puis installation de la chart.

Toute commande externe (terraform, helm, aws, gcloud, kubectl) passe par un
``CommandRunner`` injectable (voir :mod:`quadringent.installer.runner`), pour
que les tests s'exécutent hors ligne, sans jamais toucher un cloud ou un
cluster réels.
"""

from __future__ import annotations
