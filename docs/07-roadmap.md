# Roadmap — Fases del proyecto

## Estado de las fases

| Fase | Descripción | Estado |
|---|---|---|
| Fase 1 | Agente + Ollama + extracción de params + generación .tf | Completa |
| Fase 2 | Integración Slack + GitHub PRs automáticas | Pendiente |
| Fase 3 | Agente de validación + score de confianza | Pendiente |
| Fase 4 | CI/CD con terraform plan/apply via GitHub Actions | Pendiente |

---

## Fase 1 — Completada

- [x] Agente FastAPI con extracción de parámetros via LLM
- [x] Ollama desplegado en K8s con PVC
- [x] Generador de Terraform (generate_tf.py)
- [x] Tests unitarios y de integración (40 tests)
- [x] Cloud Build con versionado ($COMMIT_SHA + :latest)
- [x] Probes separadas (liveness /healthz + readiness /readyz)
- [x] Cliente httpx compartido (no uno nuevo por request)
- [x] PodDisruptionBudget para Ollama
- [x] 2 réplicas del agente
- [x] Delimitadores en prompt (<user_message>) contra prompt injection

---

## Fase 2 — Slack + GitHub PRs (PRÓXIMA)

### Slack Integration
- [ ] Endpoint `POST /slack/events` para recibir webhooks de Slack
- [ ] Verificación de firma Slack (`signing_secret`)
- [ ] Parseo de mensajes del canal/thread
- [ ] Respuesta en thread con preview de parámetros extraídos
- [ ] Worker asíncrono (Slack tiene timeout de 3s en webhooks)
- [ ] Considerar Slack Bolt for Python vs API directa

### GitHub PR Automation
- [ ] GitHub client (PyGithub o API REST directa)
- [ ] Crear rama desde main con nombre descriptivo
- [ ] Commit del archivo .tf generado
- [ ] Abrir PR con descripción detallada (link a mensaje de Slack)
- [ ] Labels automáticos (ej: `aiops-generated`, `terraform`)

### Requisitos previos
- [ ] Gestión de secrets: `GITHUB_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`
- [ ] Decidir: GCP Secret Manager + ExternalSecrets vs K8s Secrets directos
- [ ] Crear Slack App en workspace de MasOrange
- [ ] Personal Access Token o GitHub App para la API

### Estructura propuesta
```
agent/
├── main.py              # FastAPI app (ya existe)
├── slack_handler.py     # Recepción y verificación de eventos Slack
├── github_client.py     # Crear rama, commit .tf, abrir PR
├── tf_generator.py      # Mover lógica de generate_tf.py aquí
├── config.py            # Settings centralizados (pydantic-settings)
└── tests/
    ├── test_main.py     # Ya existe
    ├── test_slack.py
    ├── test_github.py
    └── test_tf_generator.py
```

---

## Fase 3 — Validación + Confianza

- [ ] Agente de validación que inspecciona PRs generadas
- [ ] Compara código .tf vs petición original
- [ ] Verifica cumplimiento de políticas internas MasOrange:
  - Nomenclatura de recursos
  - Regiones permitidas
  - Tipos de instancia autorizados
  - Labels obligatorios
- [ ] Score de confianza (0-100%)
- [ ] Umbral configurable para automatización total
- [ ] Fallback humano: alerta en Slack cuando confianza < umbral
- [ ] Comentario automático en la PR con resultado de validación

---

## Fase 4 — CI/CD Terraform

- [ ] GitHub Actions workflow disparado por label/aprobación del agente
- [ ] Paso 1: `terraform init + plan` (automático)
- [ ] Paso 2: comentario en PR con output del plan
- [ ] Paso 3: `terraform apply` (requiere aprobación si confianza < 100%)
- [ ] Gestión segura de credenciales GCP para terraform
- [ ] State backend (GCS bucket)
- [ ] Notificación en Slack del resultado del apply

---

## Mejoras técnicas pendientes (transversales)

- [ ] Retry con backoff exponencial hacia Ollama
- [ ] Circuit breaker para evitar saturar Ollama
- [ ] Rate limiting en el agente
- [ ] NetworkPolicy para aislar tráfico entre pods
- [ ] Observabilidad: métricas Prometheus, tracing OpenTelemetry
- [ ] HPA (Horizontal Pod Autoscaler) para el agente
- [ ] Evaluar modelo más capaz (phi3:mini, llama3) vs precisión
- [ ] Considerar migrar modelos a GCS + init container (para escalar Ollama)
