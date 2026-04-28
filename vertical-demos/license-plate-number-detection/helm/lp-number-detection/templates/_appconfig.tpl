{{/*
Define backend service name
*/}}
{{- define "lp-number-detection-backend.apihost" -}}
{{- printf "%s.%s.%s" (include "lp-number-detection-backend.fullname" .) .Release.Namespace "svc.cluster.local"| trimSuffix "-" -}}
{{- end }}

{{/*
Define Model Serving URL
Option 1 : Embedded Model
Option 2 : Kserve in this Helm
Option 3 : MLIS 
*/}}
{{- define "lp-number-detection-detectors.serverUrl" -}}
{{- if eq .Values.backend.appConfig.detectors.option "embedded" }}
{{- "./models" }}
{{- else if eq .Values.backend.appConfig.detectors.option "kserve" }}
{{- printf "%s%s-%s.%s.%s" "http://" (include "lp-number-detection-isvc.fullname" .) "predictor-00001" .Release.Namespace "svc.cluster.local"| trimSuffix "-" -}}
{{- else if eq .Values.backend.appConfig.detectors.option "mlis" }}
{{- printf "%s%s-%s.%s.%s" "http://" (.Values.backend.appConfig.detectors.mlis_deployment_name) "predictor-00001" (.Values.backend.appConfig.detectors.mlis_deployment_namespace) "svc.cluster.local"| trimSuffix "-" -}}
{{- end }}
{{- end }}