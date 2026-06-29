{{/*
Expand the chart name.
*/}}
{{- define "realtime-translation.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a release-scoped fullname.
*/}}
{{- define "realtime-translation.fullname" -}}
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

{{- define "realtime-translation.databaseUrl" -}}
{{- if .Values.env.DATABASE_URL -}}
{{- .Values.env.DATABASE_URL -}}
{{- else if and .Values.postgresql.enabled .Values.postgresql.auth.username .Values.postgresql.auth.password .Values.postgresql.auth.database -}}
{{- printf "postgresql+asyncpg://%s:%s@%s-postgresql:5432/%s" .Values.postgresql.auth.username .Values.postgresql.auth.password (include "realtime-translation.fullname" .) .Values.postgresql.auth.database -}}
{{- end -}}
{{- end -}}
