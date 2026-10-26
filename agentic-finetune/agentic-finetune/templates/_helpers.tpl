{{/*
Expand the chart name.
*/}}
{{- define "agentic-finetune.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this.
*/}}
{{- define "agentic-finetune.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Chart name and version for labels.
*/}}
{{- define "agentic-finetune.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels applied to every resource.
*/}}
{{- define "agentic-finetune.labels" -}}
helm.sh/chart: {{ include "agentic-finetune.chart" . }}
{{ include "agentic-finetune.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: agentic-finetune
{{- end -}}

{{/*
Selector labels (subset of labels that must remain stable).
*/}}
{{- define "agentic-finetune.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agentic-finetune.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
ServiceAccount name.
*/}}
{{- define "agentic-finetune.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "agentic-finetune.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
PVC name for the shared workspace.
*/}}
{{- define "agentic-finetune.workspacePvcName" -}}
{{- default (printf "%s-workspace" (include "agentic-finetune.fullname" .)) .Values.workspace.pvc.name -}}
{{- end -}}

{{/*
ConfigMap name for the Job template.
*/}}
{{- define "agentic-finetune.jobTemplateConfigMapName" -}}
{{- printf "%s-job-template" (include "agentic-finetune.fullname" .) -}}
{{- end -}}

{{- define "agentic-finetune.customEvalTemplateConfigMapName" -}}
{{- printf "%s-custom-eval-job-template" (include "agentic-finetune.fullname" .) -}}
{{- end -}}

{{- define "agentic-finetune.customEvalScriptConfigMapName" -}}
{{- printf "%s-custom-eval-script" (include "agentic-finetune.fullname" .) -}}
{{- end -}}

{{- define "agentic-finetune.mergeExportTemplateConfigMapName" -}}
{{- printf "%s-merge-export-job-template" (include "agentic-finetune.fullname" .) -}}
{{- end -}}

{{- define "agentic-finetune.mergeExportScriptConfigMapName" -}}
{{- printf "%s-merge-export-script" (include "agentic-finetune.fullname" .) -}}
{{- end -}}

{{- define "agentic-finetune.batchInferenceAdapterTemplateConfigMapName" -}}
{{- printf "%s-batch-inference-adapter-job-template" (include "agentic-finetune.fullname" .) -}}
{{- end -}}

{{- define "agentic-finetune.batchInferenceAdapterScriptConfigMapName" -}}
{{- printf "%s-batch-inference-adapter-script" (include "agentic-finetune.fullname" .) -}}
{{- end -}}

{{- define "agentic-finetune.batchInferenceEndpointTemplateConfigMapName" -}}
{{- printf "%s-batch-inference-endpoint-job-template" (include "agentic-finetune.fullname" .) -}}
{{- end -}}

{{- define "agentic-finetune.batchInferenceEndpointScriptConfigMapName" -}}
{{- printf "%s-batch-inference-endpoint-script" (include "agentic-finetune.fullname" .) -}}
{{- end -}}

{{/*
Fully-qualified image refs (handles registries with or without a tag).
*/}}
{{- define "agentic-finetune.image.orchestrator" -}}
{{- printf "%s:%s" .Values.images.orchestrator.repository .Values.images.orchestrator.tag -}}
{{- end -}}

{{- define "agentic-finetune.image.mcpServer" -}}
{{- printf "%s:%s" .Values.images.mcpServer.repository .Values.images.mcpServer.tag -}}
{{- end -}}

{{- define "agentic-finetune.image.llamaFactory" -}}
{{- printf "%s:%s" .Values.images.llamaFactory.repository .Values.images.llamaFactory.tag -}}
{{- end -}}
