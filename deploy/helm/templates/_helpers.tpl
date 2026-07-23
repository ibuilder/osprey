{{- define "osprey.name" -}}osprey{{- end -}}
{{- define "osprey.fullname" -}}{{ .Release.Name }}-osprey{{- end -}}
{{- define "osprey.labels" -}}
app.kubernetes.io/name: {{ include "osprey.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
{{- define "osprey.env" -}}
- name: OSPREY_ENV
  value: {{ .Values.config.env | quote }}
- name: OSPREY_LOG_LEVEL
  value: {{ .Values.config.logLevel | quote }}
- name: OSPREY_AI_PROVIDER
  value: {{ .Values.config.aiProvider | quote }}
- name: OSPREY_HOTLIST_TOP_N
  value: {{ .Values.config.hotlistTopN | quote }}
- name: OSPREY_FEATURE_AI_SIFT
  value: {{ .Values.config.featureAiSift | quote }}
- name: OSPREY_FEATURE_SCRIPTS
  value: {{ .Values.config.featureScripts | quote }}
- name: OSPREY_SECRET_KEY
  valueFrom: { secretKeyRef: { name: {{ include "osprey.fullname" . }}, key: secretKey } }
- name: OSPREY_ENCRYPTION_KEY
  valueFrom: { secretKeyRef: { name: {{ include "osprey.fullname" . }}, key: encryptionKey } }
- name: OSPREY_WEBHOOK_HMAC_SECRET
  valueFrom: { secretKeyRef: { name: {{ include "osprey.fullname" . }}, key: webhookHmacSecret } }
- name: OSPREY_DATABASE_URL
  valueFrom: { secretKeyRef: { name: {{ include "osprey.fullname" . }}, key: databaseUrl } }
- name: OSPREY_REDIS_URL
  valueFrom: { secretKeyRef: { name: {{ include "osprey.fullname" . }}, key: redisUrl } }
- name: OSPREY_ANTHROPIC_API_KEY
  valueFrom: { secretKeyRef: { name: {{ include "osprey.fullname" . }}, key: anthropicApiKey } }
{{- end -}}
