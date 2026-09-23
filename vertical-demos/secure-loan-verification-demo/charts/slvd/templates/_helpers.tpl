{{- define "slvd.labels" -}}
app.kubernetes.io/part-of: slvd
app.kubernetes.io/managed-by: Helm
{{- end -}}

{{- define "slvd.fullname" -}}
slvd
{{- end -}}

{{/*
HPE EZUA labels — required on workload pods so the EzAppConfig controller can
query pod health and register the app on the PCAI UI.
hpe-ezua/app must equal the EzAppConfig spec.name ("slvd").
*/}}
{{- define "hpe-ezua.labels" -}}
hpe-ezua/app: {{ .Values.ezua.appName }}
hpe-ezua/type: {{ .Values.ezua.type }}
hpe-ezua/component: app
{{- end -}}

{{/*
=============================================================================
Zero-input deploy: secret + endpoint auto-resolution
=============================================================================
The PCAI UI form may be left COMPLETELY EMPTY (zero-input deploy):
  - engine.env.SLVD_LLM_API_KEY    (used only when backend=direct_llm) —
       explicit value wins; empty -> Secret litellm-helm-masterkey
  - engine.env.SLVD_OPENCLAW_TOKEN (used only when backend=openclaw) —
       explicit value wins; empty -> NemoClaw's openclaw.json ConfigMap (the
       gateway was started with exactly this baked static token)
  - SLVD_JWT_SECRET / SLVD_HMAC_SECRET / SLVD_MCP_INTERNAL_TOKEN — GENERATED:
       deterministic sha256 of the release coordinates (stable across upgrades;
       identical across templates). Pin a custom value via engine.env if needed.
CRITICAL (verified on PCAI, helm 3.16): cluster-wide lookup (namespace "")
returns NIL for Service/Secret/ConfigMap under the import identity while
NAMESPACE-scoped lookup works. Every lookup below therefore iterates
candidate namespaces explicitly — NEVER "". Also: `lookup` returns nothing
during `helm template`/lint and during CLIENT dry-run — verify with a real
`helm install`.
=============================================================================
*/}}

{{- define "slvd.secretLookup" -}}
{{- /* args: (dict "root" $ "name" $secretName "key" $dataKey). Namespace-
       iterated Secret lookup; returns the decoded value or "". */ -}}
{{- $root := index . "root" -}}
{{- $name := index . "name" -}}
{{- $key := index . "key" -}}
{{- $found := "" -}}
{{- range $ns := concat (list $root.Release.Namespace) (default (list) $root.Values.detect.namespaces) (list "project-user-aieadmin" "nemoclaw" "default") -}}
  {{- if not $found -}}
    {{- $ss := lookup "v1" "Secret" $ns $name -}}
    {{- if $ss -}}
      {{- /* explicit-name lookup returns the object itself, NOT a List */ -}}
      {{- $data := default (dict) $ss.data -}}
      {{- if hasKey $data $key -}}
        {{- $found = b64dec (get $data $key) -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- $found -}}
{{- end -}}

{{- define "slvd.configmapRegexLookup" -}}
{{- /* args: (dict "root" $ "name" $cmName "key" $dataKey "re" $regex). First
       full regex match inside data[key] of a namespace-iterated ConfigMap. */ -}}
{{- $root := index . "root" -}}
{{- $name := index . "name" -}}
{{- $key := index . "key" -}}
{{- $re := index . "re" -}}
{{- $found := "" -}}
{{- range $ns := concat (list $root.Release.Namespace) (default (list) $root.Values.detect.namespaces) (list "project-user-aieadmin" "nemoclaw" "default") -}}
  {{- if not $found -}}
    {{- $cms := lookup "v1" "ConfigMap" $ns $name -}}
    {{- if $cms -}}
      {{- $data := default (dict) $cms.data -}}
      {{- if hasKey $data $key -}}
        {{- $ms := regexFindAll $re (get $data $key) -1 -}}
        {{- if $ms -}}{{- $found = index $ms 0 -}}{{- end -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- $found -}}
{{- end -}}

{{- define "slvd.detectSvc" -}}
{{- /* args: (dict "root" $ "name" $svcName) -> "<name>.<ns>.svc.cluster.local"
       or "" — namespace-iterated Service lookup. */ -}}
{{- $root := index . "root" -}}
{{- $name := index . "name" -}}
{{- $found := "" -}}
{{- range $ns := concat (list $root.Release.Namespace) (default (list) $root.Values.detect.namespaces) (list "project-user-aieadmin" "nemoclaw" "default") -}}
  {{- if not $found -}}
    {{- $svcs := lookup "v1" "Service" $ns $name -}}
    {{- if $svcs -}}
      {{- $found = printf "%s.%s.svc.cluster.local" $svcs.metadata.name $svcs.metadata.namespace -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- $found -}}
{{- end -}}

{{/*
Endpoint auto-detection: when the owning service exists in-cluster, resolve
its FQDN instead of trusting a possibly-stale form value. "" => keep values.yaml.
*/}}
{{- define "slvd.openclawUrl" -}}
{{- $host := include "slvd.detectSvc" (dict "root" . "name" "nemoclaw-openclaw") -}}
{{- if $host -}}{{- printf "ws://%s:80" $host -}}{{- end -}}
{{- end -}}

{{- define "slvd.llmBaseUrl" -}}
{{- $host := include "slvd.detectSvc" (dict "root" . "name" "litellm-helm") -}}
{{- if $host -}}{{- printf "http://%s:4000/v1" $host -}}{{- end -}}
{{- end -}}

{{- define "slvd.secret.litellmApiKey" -}}
{{- /* engine.env.SLVD_LLM_API_KEY is the operator override; empty = detect */ -}}
{{- $v := default "" (index .Values.engine.env "SLVD_LLM_API_KEY") -}}
{{- if $v -}}
  {{- $v -}}
{{- else -}}
  {{- include "slvd.secretLookup" (dict "root" . "name" "litellm-helm-masterkey" "key" "masterkey") -}}
{{- end -}}
{{- end -}}

{{- define "slvd.secret.openclawToken" -}}
{{- /* engine.env.SLVD_OPENCLAW_TOKEN is the operator override; empty = detect */ -}}
{{- $explicit := default "" (index .Values.engine.env "SLVD_OPENCLAW_TOKEN") -}}
{{- if $explicit -}}
  {{- $explicit -}}
{{- else -}}
  {{- /* 0.2.x bakes the static gateway token into openclaw.json
         ("token": "<hex>") — regexFindAll returns the full match; trim it. */ -}}
  {{- $m := include "slvd.configmapRegexLookup" (dict "root" . "name" "nemoclaw-openclaw-openclaw-config" "key" "openclaw.json" "re" "\"token\": \"[a-f0-9]{16,}\"") -}}
  {{- if $m -}}
    {{- $m | trimPrefix "\"token\": \"" | trimSuffix "\"" -}}
  {{- else -}}
    {{- include "slvd.secretLookup" (dict "root" . "name" "nemoclaw-openclaw-gateway-token" "key" "token") -}}
  {{- end -}}
{{- end -}}
{{- end -}}

{{- define "slvd.secret.jwtSecret" -}}
{{- /* Generated deterministically from the release coordinates — stable across
       upgrades (live JWTs survive) and identical across all templates. To pin a
       custom key, set engine.env.SLVD_JWT_SECRET (template skips generation
       when that key exists). */ -}}
{{- sha256sum (printf "slvd-jwt|%s|%s" .Release.Namespace .Release.Name) -}}
{{- end -}}

{{- define "slvd.secret.hmacSecret" -}}
{{- /* Generated deterministically (see jwtSecret). Shared by engine
       SLVD_HMAC_SECRET and mcp SLVD_MCP_INTERNAL_TOKEN — must match. Override:
       engine.env.SLVD_HMAC_SECRET. */ -}}
{{- sha256sum (printf "slvd-hmac|%s|%s" .Release.Namespace .Release.Name) -}}
{{- end -}}
