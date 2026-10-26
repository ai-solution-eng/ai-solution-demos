{{/*
Expand the chart name.
*/}}
{{- define "agentic-finetune-v2.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this.
*/}}
{{- define "agentic-finetune-v2.fullname" -}}
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
{{- define "agentic-finetune-v2.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels applied to every resource.
*/}}
{{- define "agentic-finetune-v2.labels" -}}
helm.sh/chart: {{ include "agentic-finetune-v2.chart" . }}
{{ include "agentic-finetune-v2.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: agentic-finetune-v2
{{- end -}}

{{/*
Selector labels (subset of labels that must remain stable).
*/}}
{{- define "agentic-finetune-v2.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agentic-finetune-v2.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
ServiceAccount name.
*/}}
{{- define "agentic-finetune-v2.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "agentic-finetune-v2.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
PVC name for the shared workspace.
*/}}
{{- define "agentic-finetune-v2.workspacePvcName" -}}
{{- default (printf "%s-workspace" (include "agentic-finetune-v2.fullname" .)) .Values.workspace.pvc.name -}}
{{- end -}}

{{/*
ConfigMap name for the Job template.
*/}}
{{- define "agentic-finetune-v2.jobTemplateConfigMapName" -}}
{{- printf "%s-job-template" (include "agentic-finetune-v2.fullname" .) -}}
{{- end -}}

{{- define "agentic-finetune-v2.customEvalTemplateConfigMapName" -}}
{{- printf "%s-custom-eval-job-template" (include "agentic-finetune-v2.fullname" .) -}}
{{- end -}}

{{- define "agentic-finetune-v2.customEvalScriptConfigMapName" -}}
{{- printf "%s-custom-eval-script" (include "agentic-finetune-v2.fullname" .) -}}
{{- end -}}

{{- define "agentic-finetune-v2.mergeExportTemplateConfigMapName" -}}
{{- printf "%s-merge-export-job-template" (include "agentic-finetune-v2.fullname" .) -}}
{{- end -}}

{{- define "agentic-finetune-v2.mergeExportScriptConfigMapName" -}}
{{- printf "%s-merge-export-script" (include "agentic-finetune-v2.fullname" .) -}}
{{- end -}}

{{- define "agentic-finetune-v2.batchInferenceAdapterTemplateConfigMapName" -}}
{{- printf "%s-batch-inference-adapter-job-template" (include "agentic-finetune-v2.fullname" .) -}}
{{- end -}}

{{- define "agentic-finetune-v2.batchInferenceAdapterScriptConfigMapName" -}}
{{- printf "%s-batch-inference-adapter-script" (include "agentic-finetune-v2.fullname" .) -}}
{{- end -}}

{{- define "agentic-finetune-v2.batchInferenceEndpointTemplateConfigMapName" -}}
{{- printf "%s-batch-inference-endpoint-job-template" (include "agentic-finetune-v2.fullname" .) -}}
{{- end -}}

{{- define "agentic-finetune-v2.batchInferenceEndpointScriptConfigMapName" -}}
{{- printf "%s-batch-inference-endpoint-script" (include "agentic-finetune-v2.fullname" .) -}}
{{- end -}}

{{/*
Fully-qualified image refs (handles registries with or without a tag).
*/}}
{{- define "agentic-finetune-v2.image.orchestrator" -}}
{{- printf "%s:%s" .Values.images.orchestrator.repository .Values.images.orchestrator.tag -}}
{{- end -}}

{{- define "agentic-finetune-v2.image.mcpServer" -}}
{{- printf "%s:%s" .Values.images.mcpServer.repository .Values.images.mcpServer.tag -}}
{{- end -}}

{{- define "agentic-finetune-v2.image.llamaFactory" -}}
{{- printf "%s:%s" .Values.images.llamaFactory.repository .Values.images.llamaFactory.tag -}}
{{- end -}}
