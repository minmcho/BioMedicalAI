{{- define "opencare.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "opencare.fullname" -}}
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

{{- define "opencare.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "opencare.labels" -}}
helm.sh/chart: {{ include "opencare.chart" . }}
{{ include "opencare.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "opencare.selectorLabels" -}}
app.kubernetes.io/name: {{ include "opencare.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "opencare.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "opencare.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "opencare.image" -}}
{{- $registry := .Values.global.imageRegistry -}}
{{- if $registry }}
{{- printf "%s/%s:%s" $registry .image.repository (default $.Chart.AppVersion .image.tag) }}
{{- else }}
{{- printf "%s:%s" .image.repository (default $.Chart.AppVersion .image.tag) }}
{{- end }}
{{- end }}

{{/* Backend DB host: explicit backend.database.host > externalDatabase.host > bundled pg service */}}
{{- define "opencare.dbHost" -}}
{{- if .Values.backend.database.host }}
{{- .Values.backend.database.host }}
{{- else if .Values.externalDatabase.host }}
{{- .Values.externalDatabase.host }}
{{- else }}
{{- printf "%s-postgresql" (include "opencare.fullname" .) }}
{{- end }}
{{- end }}

{{- define "opencare.backendServiceName" -}}
{{- printf "%s-backend" (include "opencare.fullname" .) }}
{{- end }}

{{- define "opencare.adkUrl" -}}
{{- if .Values.config.adkRuntimeUrl }}
{{- .Values.config.adkRuntimeUrl }}
{{- else }}
{{- printf "http://%s-adk:%v" (include "opencare.fullname" .) .Values.adk.service.port }}
{{- end }}
{{- end }}

{{- define "opencare.apiBase" -}}
{{- if .Values.config.openCareApiBase }}
{{- .Values.config.openCareApiBase }}
{{- else }}
{{- printf "http://%s:%v" (include "opencare.backendServiceName" .) .Values.backend.service.port }}
{{- end }}
{{- end }}

{{- define "opencare.valkeyUrl" -}}
{{- if .Values.valkey.externalUrl }}
{{- .Values.valkey.externalUrl }}
{{- else if .Values.valkey.enabled }}
{{- printf "redis://%s-valkey:%v/0" (include "opencare.fullname" .) .Values.valkey.service.port }}
{{- else }}
{{- printf "redis://valkey:6379/0" }}
{{- end }}
{{- end }}
