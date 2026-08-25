{{- define "dst.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "dst.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "dst.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "dst.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{/* Env shared by the serving pods and the migrate Job. */}}
{{- define "dst.contractEnv" -}}
- name: DST_ENVIRONMENT
  value: production
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secret.name }}
      key: {{ .Values.secret.databaseUrlKey }}
# The admin DSN is a RUNTIME dependency, not just migrate's: admin-token auth,
# the scheduler, and OAuth all run on the admin engine.
- name: DATABASE_ADMIN_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secret.name }}
      key: {{ .Values.secret.databaseAdminUrlKey }}
- name: DST_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secret.name }}
      key: {{ .Values.secret.secretKeyKey }}
- name: DST_PUBLIC_BASE_URL
  value: {{ required "publicBaseUrl is required (the production contract fails startup without it)" .Values.publicBaseUrl | quote }}
{{- range $k, $v := .Values.env }}
- name: {{ $k }}
  value: {{ $v | quote }}
{{- end }}
{{- end -}}
