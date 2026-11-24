{{/*
Expand the name of the chart.
*/}}
{{- define "defence-ops.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "defence-ops.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "defence-ops.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "defence-ops.labels" -}}
helm.sh/chart: {{ include "defence-ops.chart" . }}
{{ include "defence-ops.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "defence-ops.selectorLabels" -}}
{{ include "hpe-ezua.labels" . }}
app.kubernetes.io/name: {{ include "defence-ops.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ServiceAccount name
*/}}
{{- define "defence-ops.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "defence-ops.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Environment shared by every container. Call with (dict "root" $ "service" .Values.<service>).
*/}}
{{- define "defence-ops.env" -}}
- name: ENV
  value: {{ .root.Values.global.appEnv | quote }}
- name: PORT
  value: {{ .service.port | quote }}
- name: CONFIG_PATH
  value: /app/data/config.json
# Defaults until an administrator saves System Config
- name: LLM_MODEL
  value: {{ .root.Values.llm.model | quote }}
- name: KAFKA_BROKER
  value: {{ .root.Values.kafka.broker | quote }}
- name: KAFKA_TOPIC
  value: {{ .root.Values.kafka.topic | quote }}
{{- with .root.Values.global.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Security context for every container: non-root (set on the pod), no privilege escalation, no
capabilities, and a read-only root filesystem outside the Tilt dev loop.
*/}}
{{- define "defence-ops.containerSecurityContext" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: {{ .Values.securityContext.readOnlyRootFilesystem }}
  capabilities:
    drop: ["ALL"]
{{- end }}

{{/*
Probes for one container. Call with the service's values; the startup probe gives Next.js and
uvicorn up to two minutes to come up, which dev mode needs for its first compile.
*/}}
{{- define "defence-ops.probes" -}}
startupProbe:
  httpGet:
    path: {{ .probePath }}
    port: {{ .port }}
  periodSeconds: 5
  timeoutSeconds: 5
  failureThreshold: 24
readinessProbe:
  httpGet:
    path: {{ .probePath }}
    port: {{ .port }}
  periodSeconds: 10
  timeoutSeconds: 5
livenessProbe:
  httpGet:
    path: {{ .probePath }}
    port: {{ .port }}
  periodSeconds: 20
  timeoutSeconds: 5
  failureThreshold: 3
{{- end }}

{{/*
Image reference for one container. Call with (dict "root" $ "service" .Values.<service>); the
service's own tag wins over global.image.tag.
*/}}
{{- define "defence-ops.image" -}}
{{- printf "%s:%s" .service.image.repository (.service.image.tag | default .root.Values.global.image.tag) }}
{{- end }}

{{/*
Secret holding the LiteLLM key: the one named in llm.existingSecret, else the chart's own when
llm.apiKey is set, else none.
*/}}
{{- define "defence-ops.llmSecretName" -}}
{{- if .Values.llm.existingSecret.name }}
{{- .Values.llm.existingSecret.name }}
{{- else if .Values.llm.apiKey }}
{{- printf "%s-llm" (include "defence-ops.fullname" .) }}
{{- end }}
{{- end }}

{{- define "defence-ops.llmSecretKey" -}}
{{- if .Values.llm.existingSecret.name }}
{{- .Values.llm.existingSecret.key | default "api-key" }}
{{- else }}
{{- "api-key" }}
{{- end }}
{{- end }}

{{/*
Secret holding the Kafka credentials (keys username and password), chosen the same way.
*/}}
{{- define "defence-ops.kafkaSecretName" -}}
{{- if .Values.kafka.existingSecret.name }}
{{- .Values.kafka.existingSecret.name }}
{{- else if or .Values.kafka.username .Values.kafka.password }}
{{- printf "%s-kafka-credentials" (include "defence-ops.fullname" .) }}
{{- end }}
{{- end }}

{{/*
PCAI's Keycloak issuer, which signs the ID tokens its sign-in forwards.
*/}}
{{- define "defence-ops.oidcIssuer" -}}
{{- printf "https://keycloak.%s/realms/UA" .Values.ezua.domainName }}
{{- end }}
