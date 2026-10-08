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
The ObjectStore name the CNPG Cluster's own plugins block and the
ScheduledBackup both reference (#252) - derived from the Cluster name so
all three stay in lockstep without a separate values key.
*/}}
{{- define "memory-manager.cnpgObjectStoreName" -}}
{{- printf "%s-backup" (include "memory-manager.cnpgClusterName" .) }}
{{- end }}

{{/*
The Secret the ObjectStore's own s3Credentials read from: the backup
sub-block's own operator-supplied existingSecret, defaulting to
"<cnpg cluster name>-backup-credentials" - this chart never manages cloud
object-store credentials itself (#252's own "Not included": installing
the operator/plugin), so there is no chart-managed "create" alternative
here the way templates/secret.yaml has for the app's own secrets.
*/}}
{{- define "memory-manager.cnpgBackupSecretName" -}}
{{- default (printf "%s-backup-credentials" (include "memory-manager.cnpgClusterName" .)) .Values.database.cnpg.backup.existingSecret }}
{{- end }}

{{/*
Fails fast when more than one CNPG instance is requested while storage's
own backend is not "postgres" (ADR-0007, ADR-0009 §6: only a "postgres"
backend may scale) - a template-side guard alongside values.schema.json's
own "if"/"then", since `helm template --skip-schema-validation` exists.
Included by templates/cnpg-cluster.yaml; renders no output of its own.
*/}}
{{- define "memory-manager.validateCnpgInstances" -}}
{{- if and (ne .Values.storage.backend "postgres") (gt (int .Values.database.cnpg.instances) 1) }}
{{- fail (printf "database.cnpg.instances (%d) requires storage.backend \"postgres\" - storage.backend %q allows at most 1 (ADR-0007, ADR-0009 §6)" (int .Values.database.cnpg.instances) .Values.storage.backend) }}
{{- end }}
{{- end }}

{{/*
The Valkey Deployment/Service name this chart renders when valkey's own
enabled flag is true (templates/valkey.yaml) - "<fullname>-valkey",
matching the api/worker suffix convention above.
*/}}
{{- define "memory-manager.valkeyName" -}}
{{- printf "%s-valkey" (include "memory-manager.fullname" .) }}
{{- end }}

{{/*
VALKEY_URL for the chart's own Valkey Deployment (never for valkey's own
externalUrl, which is already a complete URL a caller supplies as-is) -
redis:// to the in-cluster Service's own DNS name on Valkey's
default port, with $(VALKEY_PASSWORD) spliced in when valkey's own
existingSecret is set. Kubernetes expands a $(VAR_NAME) reference
against env vars defined earlier in the same container ("dependent
environment variables") - templates/api-deployment.yaml and
templates/worker-deployment.yaml both define VALKEY_PASSWORD right
before the env entry that calls this helper, whenever existingSecret is
set.
*/}}
{{- define "memory-manager.valkeyUrl" -}}
{{- if .Values.valkey.existingSecret }}
{{- printf "redis://:$(VALKEY_PASSWORD)@%s:6379/0" (include "memory-manager.valkeyName" .) }}
{{- else }}
{{- printf "redis://%s:6379/0" (include "memory-manager.valkeyName" .) }}
{{- end }}
{{- end }}

{{/*
Fails fast when valkey's own enabled flag and externalUrl are both set
(#254): two different ways to point api/worker at Valkey, never both.
Included by templates/valkey.yaml; renders no output of its own.
*/}}
{{- define "memory-manager.validateValkey" -}}
{{- if and .Values.valkey.enabled .Values.valkey.externalUrl }}
{{- fail "valkey.enabled and valkey.externalUrl are mutually exclusive - pick one way to point api/worker at Valkey (#254)" }}
{{- end }}
{{- end }}

{{/*
Fails fast when storage's own backend is "git" and combined with more
than one replica or any autoscaler (ADR-0007, ADR-0009 §6: only a
"postgres" backend may scale) - a template-side guard alongside
values.schema.json's own "if"/"then", since `helm template
--skip-schema-validation` exists. Included by templates/deployment.yaml;
renders no output of its own. Checks both a generic top-level
"autoscaling" block (kept for callers that set one directly) and the
api and worker blocks' own component-scoped autoscaling (and, for api,
keda) sub-blocks the chart itself renders from (#251, ADR-0009 addendum
2026-10-08).
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
{{- if or .Values.api.autoscaling.enabled .Values.api.keda.enabled .Values.worker.autoscaling.enabled }}
{{- fail "storage.backend \"git\" does not allow an autoscaler (HPA or KEDA) - only storage.backend \"postgres\" may scale (ADR-0007, ADR-0009 §6)" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Fails fast when the api block's own autoscaling and keda sub-blocks are
both enabled (#251, ADR-0009 addendum 2026-10-08): a KEDA ScaledObject
creates its own HorizontalPodAutoscaler, so an HPA (templates/hpa.yaml)
and a ScaledObject (templates/keda-scaledobject.yaml) for the same api
Deployment would fight over the desired replica count - never both.
Included by templates/api-deployment.yaml; renders no output of its own.
*/}}
{{- define "memory-manager.validateApiAutoscaling" -}}
{{- if and .Values.api.autoscaling.enabled .Values.api.keda.enabled }}
{{- fail "api.autoscaling.enabled and api.keda.enabled are mutually exclusive - a KEDA ScaledObject creates its own HorizontalPodAutoscaler (ADR-0009 addendum 2026-10-08)" }}
{{- end }}
{{- end }}

{{/*
Component-qualified selector labels: the same base selector (the
selectorLabels helper above) plus app.kubernetes.io/component, so the
"postgres"-only api and worker Deployments (ADR-0009 §4) get distinct,
immutable selectors while the "git"-only Deployment above keeps its own,
unqualified one exactly as it was (golden render test, #249). Takes a
dict `(dict "context" $ "component" "api")`, since a named template
cannot otherwise take a second argument alongside the root context.
*/}}
{{- define "memory-manager.componentSelectorLabels" -}}
{{ include "memory-manager.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Component-qualified labels: the full label set (the labels helper above)
plus app.kubernetes.io/component. Same calling convention as
componentSelectorLabels above.
*/}}
{{- define "memory-manager.componentLabels" -}}
{{ include "memory-manager.labels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Shared egress rules for the api and worker NetworkPolicies, storage's
own backend "postgres" only (#255, templates/networkpolicy.yaml): DNS
(always, same shape as the git-mode policy's own egress), the CNPG
Cluster's own instance pods on 5432 (only while the database block's own
cnpg sub-block is enabled - an external Postgres is an operator-specific
host this chart cannot scope by podSelector, covered by the egress
block's own "rules" below instead), Valkey on 6379 (only while valkey's
own enabled flag is true, same reasoning), and finally 443 for
Entra/Graph/an embedding provider - unrestricted destination by default
(the egress block's own "allowAll", the same knob the git-mode policy's
own egress uses) since those hostnames are operator-specific; set it to
false and list its own "rules" instead to lock that down to your own
resolved ranges. Takes the root context directly, not a dict, since it
needs no per-component argument.
*/}}
{{- define "memory-manager.networkPolicyAppEgress" -}}
- to:
    - namespaceSelector: {}
  ports:
    - protocol: UDP
      port: 53
    - protocol: TCP
      port: 53
{{- if .Values.database.cnpg.enabled }}
- to:
    - podSelector:
        matchLabels:
          cnpg.io/cluster: {{ include "memory-manager.cnpgClusterName" . }}
          cnpg.io/podRole: instance
  ports:
    - protocol: TCP
      port: 5432
{{- end }}
{{- if .Values.valkey.enabled }}
- to:
    - podSelector:
        matchLabels:
          {{- include "memory-manager.componentSelectorLabels" (dict "context" . "component" "valkey") | nindent 10 }}
  ports:
    - protocol: TCP
      port: 6379
{{- end }}
{{- if .Values.networkPolicy.egress.allowAll }}
- ports:
    - protocol: TCP
      port: 443
{{- else }}
{{- range .Values.networkPolicy.egress.rules }}
- {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Fails fast when shutdown's own terminationGracePeriodSeconds does not
cover both the uvicorn drain (shutdown's own graceSeconds, ADR-0009
§1/§5) and the preStop sleep on top of it (shutdown's own
preStopSleepSeconds) - otherwise Kubernetes SIGKILLs the process before
uvicorn's own grace period even starts draining in-flight requests.
Included by templates/api-deployment.yaml and
templates/worker-deployment.yaml ("postgres" only - the "git" Deployment
has no comparable drain to protect); renders no output of its own.
*/}}
{{- define "memory-manager.validateShutdown" -}}
{{- $needed := add (int .Values.shutdown.graceSeconds) (int .Values.shutdown.preStopSleepSeconds) }}
{{- if lt (int .Values.shutdown.terminationGracePeriodSeconds) $needed }}
{{- fail (printf "shutdown.terminationGracePeriodSeconds (%d) must be at least shutdown.graceSeconds + shutdown.preStopSleepSeconds (%d) (ADR-0009 §5)" (int .Values.shutdown.terminationGracePeriodSeconds) $needed) }}
{{- end }}
{{- end }}
