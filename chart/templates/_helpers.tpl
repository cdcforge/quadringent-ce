{{- define "quadringent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "quadringent.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "quadringent.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "quadringent.labels" -}}
app.kubernetes.io/name: {{ include "quadringent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{- define "quadringent.image" -}}
{{- if not .Values.image.digest -}}
{{- fail "image.digest est obligatoire : jamais de tag mouvant en production" -}}
{{- end -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- end -}}

{{- /*
  Gardes de périmètre, déclenchées avant tout rendu de charge utile : la
  release doit rester un environnement non productif, dans le namespace
  déclaré par le site, sans chemin de promotion PROD.
*/ -}}
{{- define "quadringent.siteGuards" -}}
{{- if and (ne .Values.storage.backend "gcs") (eq .Values.gcpIdentityMode "vm-metadata") -}}
{{- fail "gcpIdentityMode=vm-metadata exige storage.backend=gcs" -}}
{{- end -}}
{{- if not (has (required "deployment.environment est obligatoire" .Values.deployment.environment) (list "dev" "int" "test" "staging")) -}}
{{- fail "deployment.environment doit rester un environnement non productif (dev, int, test, staging) : aucun chemin de promotion PROD" -}}
{{- end -}}
{{- if .Values.deployment.productionPromotionAllowed -}}
{{- fail "deployment.productionPromotionAllowed doit rester false" -}}
{{- end -}}
{{- if ne .Release.Namespace (required "site.namespace est obligatoire" .Values.site.namespace) -}}
{{- fail "le namespace de release doit être exactement site.namespace" -}}
{{- end -}}
{{- end -}}

{{/* Le CA est déclaré par le site, jamais embarqué dans une image. */}}
{{- define "quadringent.caVolume" -}}
- name: ibmi-ca
  secret:
    secretName: {{ required "as400.tlsCaSecret.name est obligatoire" .Values.as400.tlsCaSecret.name | quote }}
    items:
      - key: {{ required "as400.tlsCaSecret.key est obligatoire" .Values.as400.tlsCaSecret.key | quote }}
        path: {{ base .Values.as400.tlsCaFile | quote }}
{{- end }}
{{- define "quadringent.caMount" -}}
- name: ibmi-ca
  mountPath: {{ dir .Values.as400.tlsCaFile | quote }}
  readOnly: true
{{- end }}

{{- /* Noms partagés entre postgres.yaml (StatefulSet/Secret/Service) et le
  câblage du conteneur control-plane v2 (DATABASE_URL) — une seule source de
  vérité pour éviter toute divergence entre les deux fichiers. */ -}}
{{- define "quadringent.postgresServiceName" -}}
{{- printf "%s-postgres" (include "quadringent.fullname" .) -}}
{{- end -}}
{{- define "quadringent.postgresSecretName" -}}
{{- printf "%s-credentials" (include "quadringent.postgresServiceName" .) -}}
{{- end -}}
