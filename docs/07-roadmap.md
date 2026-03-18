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
- [x] Generador de Terraform (generate_tf.py + agent/tf_generator.py)
- [x] 64 tests unitarios y de integración (4 ficheros: test_endpoints, test_extraction, test_tf_generator, test_validation)
- [x] Cloud Build con versionado ($COMMIT_SHA + :latest) y tests como gate
- [x] Probes separadas (liveness /healthz + readiness /readyz)
- [x] Cliente httpx compartido (no uno nuevo por request)
- [x] PodDisruptionBudget para Ollama
- [x] 1 réplica del agente (optimizado para caber en 1 nodo spot e2-standard-2)
- [x] Delimitadores en prompt (<user_message>) contra prompt injection
- [x] Código modularizado: config.py, schemas.py, extraction.py, validation.py, tf_generator.py
- [x] Modelo qwen2.5:1.5b como modelo principal (mejor extracción que tinyllama)

### Mejoras técnicas completadas (post Fase 1)

- [x] Retry con backoff exponencial hacia Ollama — solo en errores transitorios (timeout, connection), no en errores HTTP determinísticos (commit 07ad2e3)
- [x] NetworkPolicy para aislar tráfico entre pods — Ollama solo acepta del agent, agent solo acepta de Apache (commit 5ec78f5)
- [x] Métricas Prometheus: endpoint /metrics auto-instrumentado + contadores custom `aiops_ollama_retries_total` y `aiops_extraction_total` (commit 5ec78f5)
- [x] SecurityContext hardening: runAsNonRoot, readOnlyRootFilesystem, drop ALL capabilities, allowPrivilegeEscalation: false (commit 5ec78f5)
- [x] Logging JSON estructurado compatible con Cloud Logging / ELK (config.py, python-json-logger)
- [x] Fix Dockerfile: `COPY *.py ./` con trailing slash para múltiples ficheros fuente (commit 326cdc5)

### Verificación end-to-end (2026-03-18)

Sesión de pruebas completa contra el cluster en producción:
- Cluster: 2 nodos spot Ready, K8s 1.35.1-gke
- PDB: `ALLOWED DISRUPTIONS: 0` — protección activa
- `/healthz`: 200 OK (~1.8ms)
- `/readyz`: 200 OK con `model_loaded: true` (~62ms)
- `/extract`: extracción correcta 4/4 parámetros, validación funcional
- `/metrics`: Prometheus con contadores reales (232 readyz, 168 healthz, 23 readyz 5xx durante arranque)
- Cloud Build: 64/64 tests + Docker build + push en ~1 min
- `generate_tf.py` end-to-end: .tf generado desde lenguaje natural (7.8s)

**Gaps identificados**:
- Validación de regiones acepta us-east1 sin warning (la convención es solo europe-\*)
- Buckets del histograma Prometheus no cubren latencias LLM (7-45s cae en +Inf)
- Primera inferencia tras restart de nodo: ~45s (modelo cargándose en memoria)

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
├── config.py            # Settings centralizados (ya existe)
├── schemas.py           # Pydantic models (ya existe)
├── extraction.py        # Extracción JSON del LLM (ya existe)
├── validation.py        # Validación de params GCP (ya existe)
├── tf_generator.py      # Generación de .tf (ya existe)
├── slack_handler.py     # NUEVO: recepción y verificación de eventos Slack
├── github_client.py     # NUEVO: crear rama, commit .tf, abrir PR
└── tests/
    ├── conftest.py      # Fixtures (ya existe)
    ├── helpers.py        # Mocks compartidos (ya existe)
    ├── test_endpoints.py # 26 tests (ya existe)
    ├── test_extraction.py # 11 tests (ya existe)
    ├── test_tf_generator.py # 16 tests (ya existe)
    ├── test_validation.py # 11 tests (ya existe)
    ├── test_slack.py     # NUEVO
    └── test_github.py    # NUEVO
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

- [ ] Restringir `VALID_REGIONS` a solo europe-\* (alinear validación con convención MasOrange)
- [ ] Configurar buckets personalizados en prometheus-fastapi-instrumentator para latencias LLM (5s, 10s, 30s, 60s, 120s)
- [ ] Circuit breaker para evitar saturar Ollama
- [ ] Rate limiting en el agente
- [ ] HPA (Horizontal Pod Autoscaler) para el agente
- [ ] Tracing OpenTelemetry
- [ ] Evaluar modelo más capaz (phi3:mini, llama3) vs precisión
- [ ] Considerar migrar modelos a GCS + init container (para escalar Ollama)
- [ ] Solicitar rol `roles/logging.logWriter` para service account de Cloud Build (warning actual no afecta builds)
