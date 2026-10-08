{{/*
SPDX-License-Identifier: AGPL-3.0-only
*/}}

{{/*
Chart name, truncated and suffixed per the usual `helm create` scaffold -
kept here rather than re-derived in every template.
*/}}
{{- define "memory-manager.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Release-qualified name used for every object this chart renders - matches
deploy/'s bare "memory-manager" when the release is also named that.
*/}}
{{- define "memory-manager.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if eq $name .Release.Name }}
{{- $name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "memory-manager.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Standard app.kubernetes.io/* labels plus helm.sh/chart (CLAUDE.md's "Mirror
deploy/" guideline - deploy/ only ever sets app.kubernetes.io/name, so this
chart's selector label stays just that one, see selectorLabels below).
*/}}
{{- define "memory-manager.labels" -}}
{{ include "memory-manager.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ include "memory-manager.chart" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels: app.kubernetes.io/name only (deploy/deployment.yaml's own
selector), kept stable across releases/upgrades - instance/version must
never be part of a Deployment's immutable selector.
*/}}
{{- define "memory-manager.selectorLabels" -}}
app.kubernetes.io/name: {{ include "memory-manager.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
The ServiceAccount name a pod runs as - either the one the chart creates
(serviceAccount's own create flag, default false - deploy/'s own
Deployment sets automountServiceAccountToken: false and needs no
ServiceAccount at all) or an operator-supplied existing one.
*/}}
{{- define "memory-manager.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "memory-manager.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
The Secret name every env var pulling a credential reads from: an
operator-supplied existingSecret, or the chart-managed one this chart
renders from the secrets block's own values when its create flag is true
(templates/secret.yaml) - same key names either way (deploy/secrets.yaml).
*/}}
{{- define "memory-manager.secretName" -}}
{{- default (printf "%s-secrets" (include "memory-manager.fullname" .)) .Values.secrets.existingSecret }}
{{- end }}

{{/*
The CNPG Cluster name and the Secret CNPG derives from it
("<cluster-name>-app", CNPG's own convention - deploy/database.yaml +
deployment.yaml rely on exactly this).
*/}}
{{- define "memory-manager.cnpgClusterName" -}}
{{- printf "%s-db" (include "memory-manager.fullname" .) }}
{{- end }}

{{/*
Fails fast when storage's own backend is "git" and combined with more
than one replica or any autoscaler (ADR-0007, ADR-0009 §6: only a
"postgres" backend may scale) - a template-side guard alongside
values.schema.json's own "if"/"then", since `helm template
--skip-schema-validation` exists. Included by templates/deployment.yaml;
renders no output of its own.
*/}}
{{- define "memory-manager.validate" -}}
{{- if eq .Values.storage.backend "git" }}
{{- if gt (int .Values.replicaCount) 1 }}
{{- fail (printf "storage.backend \"git\" allows at most 1 replica, got %d - only storage.backend \"postgres\" may scale (ADR-0007, ADR-0009 §6)" (int .Values.replicaCount)) }}
{{- end }}
{{- $autoscaling := .Values.autoscaling }}
{{- if and $autoscaling (or $autoscaling.enabled (and $autoscaling.keda $autoscaling.keda.enabled)) }}
{{- fail "storage.backend \"git\" does not allow an autoscaler (HPA or KEDA) - only storage.backend \"postgres\" may scale (ADR-0007, ADR-0009 §6)" }}
{{- end }}
{{- end }}
{{- end }}
