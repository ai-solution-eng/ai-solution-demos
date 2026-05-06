{{/*
Expand the name of the chart.
*/}}
{{- define "lp-number-detection.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- define "lp-number-detection-backend.name" -}}
{{- printf "%s-%s" (include "lp-number-detection.name" .) .Values.backend.name | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "lp-number-detection-isvc.name" -}}
{{- if .Values.inferenceService.enabled }}
{{- printf "%s-%s" (include "lp-number-detection.name" .) .Values.inferenceService.modelFormat | trunc 63 | trimSuffix "-" -}}
{{- end }}
{{- end }}


{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "lp-number-detection.fullname" -}}
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

{{- define "lp-number-detection-backend.fullname" -}}
{{- printf "%s-%s" (include "lp-number-detection.fullname" .) .Values.backend.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "lp-number-detection-isvc.fullname" -}}
{{- if .Values.inferenceService.enabled }}
{{- printf "%s-%s" (include "lp-number-detection.fullname" .) .Values.inferenceService.modelFormat | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "lp-number-detection.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "lp-number-detection.labels" -}}
helm.sh/chart: {{ include "lp-number-detection.chart" . }}
{{ include "lp-number-detection.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "lp-number-detection.selectorLabels" -}}
app.kubernetes.io/name: {{ include "lp-number-detection.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
backend labels
*/}}
{{- define "lp-number-detection-backend.labels" -}}
helm.sh/chart: {{ include "lp-number-detection.chart" . }}
{{ include "lp-number-detection-backend.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
inferenceService labels
*/}}
{{- define "lp-number-detection-isvc.labels" -}}
helm.sh/chart: {{ include "lp-number-detection.chart" . }}
{{ include "lp-number-detection-backend.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}



{{/*
Selector labels for backend
*/}}
{{- define "lp-number-detection-backend.selectorLabels" -}}
app.kubernetes.io/name: {{ include "lp-number-detection-backend.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account for frontend
*/}}
{{- define "lp-number-detection.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "lp-number-detection.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Create the name of the service account for backend
*/}}
{{- define "lp-number-detection-backend.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "lp-number-detection-backend.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Create the name of the service account for inferenceService
*/}}
{{- define "lp-number-detection-isvc.serviceAccountName" }}
{{- include "lp-number-detection-isvc.fullname" . }}
{{- end }}
