#!/bin/sh
# Amorçage k3s pour le mode par défaut Quadringent (--target vm). Ce script
# installe k3s en single-node ; l'installation de la chart Quadringent elle-
# même reste pilotée par le CLI `quadringent install` depuis le poste
# opérateur, via kubeconfig récupéré par IAP TCP forwarding.
set -eu
curl -sfL https://get.k3s.io | INSTALL_K3S_CHANNEL=${k3s_channel} sh -s - server --write-kubeconfig-mode=600 --disable=traefik
mkdir -p /etc/quadringent
echo "site=${site_name}" > /etc/quadringent/site.env
