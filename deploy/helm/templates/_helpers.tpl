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
- name: OSPREY_RLS_ENABLED
  value: {{ .Values.config.rlsEnabled | quote }}
- name: OSPREY_TRUST_PROXY_HEADERS
  value: {{ .Values.config.trustProxyHeaders | quote }}
- name: OSPREY_CORS_ALLOW_ORIGINS
  value: {{ toJson .Values.config.corsAllowOrigins | quote }}
- name: OSPREY_PUBLIC_BASE_URL
  value: {{ .Values.config.publicBaseUrl | quote }}
- name: OSPREY_RATE_LIMIT_ENABLED
  value: {{ .Values.config.rateLimitEnabled | quote }}
- name: OSPREY_RATE_LIMIT_BACKEND
  value: {{ .Values.config.rateLimitBackend | quote }}
- name: OSPREY_RETENTION_SIGNAL_DAYS
  value: {{ .Values.config.retentionSignalDays | quote }}
- name: OSPREY_RETENTION_ITEM_DAYS
  value: {{ .Values.config.retentionItemDays | quote }}
- name: OSPREY_METRICS_ENABLED
  value: {{ .Values.config.metricsEnabled | quote }}
- name: OSPREY_OIDC_ENABLED
  value: {{ .Values.sso.enabled | quote }}
{{- if .Values.sso.enabled }}
- name: OSPREY_OIDC_ISSUER
  value: {{ .Values.sso.issuer | quote }}
- name: OSPREY_OIDC_CLIENT_ID
  value: {{ .Values.sso.clientId | quote }}
- name: OSPREY_OIDC_REDIRECT_URL
  value: {{ .Values.sso.redirectUrl | quote }}
- name: OSPREY_OIDC_AUTO_PROVISION
  value: {{ .Values.sso.autoProvision | quote }}
- name: OSPREY_OIDC_DEFAULT_ORG_ID
  value: {{ .Values.sso.defaultOrgId | quote }}
- name: OSPREY_OIDC_DEFAULT_ROLE
  value: {{ .Values.sso.defaultRole | quote }}
- name: OSPREY_OIDC_ALLOWED_EMAIL_DOMAINS
  value: {{ toJson .Values.sso.allowedEmailDomains | quote }}
- name: OSPREY_OIDC_CLIENT_SECRET
  valueFrom: { secretKeyRef: { name: {{ include "osprey.fullname" . }}, key: oidcClientSecret } }
{{- end }}
- name: OSPREY_SCIM_ENABLED
  value: {{ .Values.scim.enabled | quote }}
{{- if .Values.config.metricsEnabled }}
- name: OSPREY_METRICS_TOKEN
  valueFrom: { secretKeyRef: { name: {{ include "osprey.fullname" . }}, key: metricsToken } }
{{- end }}
- name: OSPREY_SECRET_KEY
  valueFrom: { secretKeyRef: { name: {{ include "osprey.fullname" . }}, key: secretKey } }
- name: OSPREY_ENCRYPTION_KEY
  valueFrom: { secretKeyRef: { name: {{ include "osprey.fullname" . }}, key: encryptionKey } }
- name: OSPREY_WEBHOOK_HMAC_SECRET
  valueFrom: { secretKeyRef: { name: {{ include "osprey.fullname" . }}, key: webhookHmacSecret } }
- name: OSPREY_REDIS_URL
  valueFrom: { secretKeyRef: { name: {{ include "osprey.fullname" . }}, key: redisUrl } }
- name: OSPREY_ANTHROPIC_API_KEY
  valueFrom: { secretKeyRef: { name: {{ include "osprey.fullname" . }}, key: anthropicApiKey } }
{{- end -}}

{{/*
The database URL is deliberately NOT part of "osprey.env": the runtime workloads
connect as an ordinary role (so Postgres row-level security actually enforces
tenant isolation — superusers and BYPASSRLS roles skip it), while the migration
job connects as the schema owner because it runs DDL.
*/}}
{{- define "osprey.appDbEnv" -}}
- name: OSPREY_DATABASE_URL
  valueFrom: { secretKeyRef: { name: {{ include "osprey.fullname" . }}, key: databaseUrl } }
{{- end -}}

{{- define "osprey.migrationDbEnv" -}}
- name: OSPREY_DATABASE_URL
  valueFrom: { secretKeyRef: { name: {{ include "osprey.fullname" . }}, key: migrationDatabaseUrl } }
{{- end -}}

{{/*
Container-level hardening. The pod-level securityContext handles identity
(runAsNonRoot/runAsUser); this handles the container's own capabilities. Both are
needed: a pod that runs as non-root can still write to its image layer and gain
privileges through a setuid binary without these.
*/}}
{{- define "osprey.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
runAsUser: {{ .Values.podSecurityContext.runAsUser }}
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/*
readOnlyRootFilesystem means the process cannot write anywhere by default.
Python needs a writable temp directory (multipart uploads, reportlab, matplotlib-
style caches), so mount an emptyDir over /tmp rather than relaxing the whole
filesystem back to writable.
*/}}
{{- define "osprey.tmpVolume" -}}
- name: tmp
  emptyDir:
    sizeLimit: {{ .Values.tmpVolumeSize | default "256Mi" }}
{{- end -}}

{{- define "osprey.tmpMount" -}}
- name: tmp
  mountPath: /tmp
{{- end -}}

{{/*
Shared egress rules. Osprey must reach DNS, its database, Redis, and the provider
APIs it polls (Microsoft Graph, Google, Procore) over HTTPS. The provider set is
not knowable in advance -- Graph alone resolves to a wide, changing address range --
so egress is restricted by *port* rather than by destination. Narrow it to your own
CIDRs if you front the providers with an egress proxy.
*/}}
{{- define "osprey.egressRules" -}}
- to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: kube-system
      podSelector:
        matchLabels:
          k8s-app: kube-dns
  ports:
    - protocol: UDP
      port: 53
    - protocol: TCP
      port: 53
- to:
    - podSelector: {}
- to:
    - ipBlock:
        cidr: 0.0.0.0/0
        except:
          # Keep a compromised pod off the cloud metadata endpoint and off the
          # rest of the private network. 169.254.169.254 is the credential-theft
          # target that turns an SSRF into cloud account access.
          - 169.254.0.0/16
          - 10.0.0.0/8
          - 172.16.0.0/12
          - 192.168.0.0/16
  ports:
    {{- range .Values.networkPolicy.egressPorts }}
    {{- if ne (int .) 53 }}
    - protocol: TCP
      port: {{ . }}
    {{- end }}
    {{- end }}
{{- end -}}
