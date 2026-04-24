# AIOps Infrastructure Agent — Contexto para Claude Code

## Resumen del proyecto

Sistema de Remediación Automática en Kubernetes. Detecta alertas de Prometheus, las procesa con un LLM (junto a una base RAG in-cluster con ChromaDB) y notifica a Mattermost, pudiendo auto-remediar fallos (ej. OOMs).
*(Nota: El proyecto original generaba Terraform, la Fase 1 original se conserva intacta sin interrupción).*
TFG/TFM en MasOrange/Telecable. Rol: ingeniero AIOps.

Flujo objetivo: `Prometheus → Alertmanager → FastAPI (Webhook) → LLM + ChromaDB (RAG) → Mattermost → K8s API`

## Documentación detallada

Cada parte del proyecto tiene su propio archivo en `docs/`:

| Archivo | Contenido |
|---|---|
| `docs/01-architecture.md` | Arquitectura, decisiones de diseño, componentes |
| `docs/02-agent-fastapi.md` | Módulos, endpoints, schemas, retry, métricas, logging |
| `docs/03-kubernetes.md` | Cluster GKE, manifiestos, probes, PDB, NetworkPolicy, SecurityContext |
| `docs/04-cicd-cloudbuild.md` | Cloud Build (tests + build), Artifact Registry, versionado |
| `docs/05-terraform-generator.md` | CLI generate_tf.py, módulo tf_generator.py, template, uso |
| `docs/06-testing.md` | 103 tests en 7 ficheros, mocking, errores comunes y soluciones |
| `docs/07-roadmap.md` | Fases del proyecto, TODOs por fase, mejoras completadas |

**Lee el archivo relevante antes de hacer cambios en esa parte del proyecto.**

## Estado actual (2026-04-23)

- **Fase 0 (Legado)**: Completa (agente modular + Ollama local + Terraform endpoints + K8s base). Se mantienen los archivos sin borrar.
- **Fase 1 (Observabilidad)**: En curso (~98%).
  - ✅ Webhook `/webhook/alert` operativo con schema Alertmanager — **verificado en cluster**.
  - ✅ Prometheus standalone + kube-state-metrics desplegados en `arturo-monitoring` (`k8s/prometheus.yaml`).
    - 5 reglas: KubePodOOMKilled, KubePodCrashLoopBackOff, HighMemory, HighCPU, TargetDown.
    - Agente scrapeado via `prometheus.io/scrape=true` en `service-agent.yaml`.
    - ClusterRole de lectura (tutor confirmó admin en 2026-04-20 — necesario para cAdvisor).
    - `scrape_timeout: 20s` en global config (antes 10s, timeout con agente ocupado).
    - KSM imagen mirroreada a AR (`crane copy --platform linux/amd64`) — `registry.k8s.io` inaccesible sin Cloud NAT.
    - `imagePullPolicy: Always` en KSM para forzar re-pull si se actualiza el mirror.
  - ✅ Alertmanager standalone en `arturo-monitoring` — receiver → `agent-svc:8000/webhook/alert`.
  - ✅ Mattermost + PostgreSQL en `arturo-mattermost`.
  - ✅ Módulo `mattermost.py` con retry/backoff.
  - ✅ Cloud Build exitoso: 124 tests + imagen `aiops-agent:5f64b61` desplegada.
  - ✅ E2E completo verificado (2026-04-20): alerta → webhook → RAG → LLM → Mattermost.
  - ✅ RBAC aplicado: Role + RoleBinding en `arturo-llm-test` (k8s/rbac.yaml).
  - ✅ NetworkPolicy scrape: regla ingress `app=prometheus` → puerto 8000 del agente añadida.
  - ✅ Targets Prometheus UP: kube-state-metrics UP; agent UP (NetworkPolicy scrape aplicada 2026-04-22).
  - ✅ Grafana desplegado en `arturo-monitoring` — stateless (emptyDir), datasource Prometheus + dashboard "AIOps Agent — Overview" (9 paneles: aiops_* counters, latencia p95 webhook, targets UP, pod phases, retries/extraction) + contact point `aiops-agent-webhook` → `agent-svc:8000/webhook/alert`. Secret `grafana-admin` externo. Manifiesto: `k8s/grafana.yaml`. NetworkPolicy actualizada (Grafana → agent port 8000).
  - ⏳ Pendiente: webhook entrante de Mattermost.
- **Fase 2 (RAG)**: **Completa** (2026-04-23).
  - ✅ `rag.py` y `diagnosis.py` escritos y testeados (26 tests).
  - ✅ ChromaDB StatefulSet desplegado — **Running** (imagen 0.6.3).
  - ✅ `nomic-embed-text:latest` cargado en Ollama.
  - ✅ Pipeline RAG integrado en `main.py` (`_process_alert_with_diagnosis`) — triple fail-open.
  - ✅ CLI `agent/ingest_runbooks.py` + K8s Job `k8s/job-ingest-runbooks.yaml` — 16 runbooks ingestados en ChromaDB (2026-04-23). `runAsUser: 1000` requerido en GKE.
  - ✅ E2E verificado (2026-04-23): KubePodOOMKilled → RAG (3 runbooks + 2 incidents) → LLM (187s, confidence=0.85, risk=high) → suggest_only → Mattermost. `HTTP_TIMEOUT=240` en deployment-agent.yaml.
- **Fase 3 (Remediación Autónoma)**: En curso (dry-run activo, 2026-04-25).
  - ✅ `remediation.py` — validation layer + motor de decisión (9 reglas) + executor dual-mode. ~70 tests.
  - ✅ `process_remediation()` integrado en pipeline `main.py`. Counter `aiops_remediation_total`.
  - ✅ RBAC aplicado (`k8s/rbac.yaml`): Role `patch deployments/get pods/limitranges` en `arturo-llm-test`.
  - ✅ `REMEDIATION_ENABLED=true`, `REMEDIATION_DRY_RUN=true` en `deployment-agent.yaml`.
  - ✅ E2E verificado (2026-04-24): KubePodOOMKilled → RAG → LLM (211s, confidence=0.90, risk=high) → **escalate** → Mattermost. Motor activo.
  - ✅ Condición del tutor implementada (2026-04-25):
    - Regla 4.5: bloquea TODO comando MUTATING que implique reinicio de pod (`set resources`, `scale`, `rollout restart`, `patch deployment`...) → `reason_code: pod_restart_blocked`. Bloqueo conservador hasta confirmación del tutor.
    - Regla 4.6: si `proposed_action.field == resources.limits.memory`, bloquea si `new > 2 × current` → `reason_code: memory_exceeds_2x`.
    - Schema LLM extendido: campo opcional `proposed_action` con `current_value/new_value/field`.
    - `parse_memory_to_bytes()` + `implies_pod_restart()` como helpers reutilizables.
  - ✅ E2E cluster verificado (2026-04-24, imagen c3b0975): regla 4.5 → `reason_code: set_resources_triggers_rollout` (kubectl exec sobre binario desplegado). Regla 4.6 → `memory_exceeds_2x` (256Mi→1Gi), `auto_remediate` (256Mi→512Mi, 2× exacto), `unparseable_memory` (fail-safe). Webhook E2E: `action=escalate` por regla 5 (risk=high), `outcome=escalate` persistido en ChromaDB + Mattermost.
  - ✅ Métricas verificadas: `aiops_remediation_total{action="escalate"} 2`, `aiops_feedback_total{outcome="persisted"} 2`.
  - ⏳ Pendiente: screenshot Grafana dashboard.
  - ⏳ Pendiente: confirmar con tutor excepción a regla 4.5 (in-place resize k8s 1.27+, rolling HA...). Pasar a `DRY_RUN=false` requiere acuerdo con tutor.

## Stack

Python 3.11 | FastAPI | httpx | Pydantic v2 | Ollama (qwen2.5:1.5b en K8s, tinyllama default en config.py) | GKE | Cloud Build

## Archivos clave

```
agent/main.py           → FastAPI app (endpoints, retry, metrics, webhook)
agent/config.py         → Settings (pydantic-settings) + JSON logging
agent/schemas.py        → Modelos Pydantic v2 (AlertmanagerPayload, AlertItem, ProposedAction, ExtractResponse...)
agent/extraction.py     → 3 estrategias de extracción JSON
agent/validation.py     → Validación de parámetros GCP
agent/tf_generator.py   → Generación de template Terraform (Fase 0 legacy)
agent/mattermost.py     → Cliente HTTP async Mattermost con retry/backoff
agent/rag.py            → Cliente ChromaDB, ingesta, query, embeddings via Ollama
agent/diagnosis.py      → Prompt AIOps contextual, generate_diagnosis(), JSON estructurado
agent/tests/            → 124 tests en 7 ficheros
generate_tf.py          → CLI generador de .tf (importa de agent/tf_generator.py)
k8s/                    → Manifiestos K8s (agent, ollama, chromadb, networkpolicy)
k8s/prometheus.yaml     → Prometheus + kube-state-metrics + ClusterRoles + 5 reglas
k8s/alertmanager.yaml   → Alertmanager standalone en arturo-monitoring
k8s/mattermost.yaml     → Mattermost + PostgreSQL en arturo-mattermost
k8s/rbac.yaml           → Role + RoleBinding para remediación autónoma (arturo-llm-test)
cloudbuild.yaml         → Pipeline: tests (gate) + build + push
```

## Entorno

- **Cluster**: ai-infra-agent (europe-southwest1-a, e2-standard-2 spot, 2 nodos)
- **Namespaces**: `arturo-llm-test` (core), `arturo-monitoring` (alertmanager), `arturo-mattermost` (chatops)
- **Registry**: europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent
- **Imagen actual**: `aiops-agent:5f64b61` (nota: el nombre de imagen es `aiops-agent`, no `agent`)
- **Ollama models**: qwen2.5:1.5b (generación), nomic-embed-text:latest (embeddings)
- **NO hay Python local en Windows** — tests se ejecutan en GCloud Shell
- **Sin Cloud NAT** — pods no tienen internet, modelos se cargan manualmente
- **Permisos de admin confirmados** (tutor, 2026-04-20) — se usan ClusterRoles de lectura para Prometheus (necesario para cAdvisor). Mantener convención de no ClusterRoles de escritura.

## Convenciones

- Regiones GCP permitidas (convención): europe-west1/2/3/4, europe-southwest1 (nota: validation.py acepta más regiones — gap conocido)
- Labels obligatorios: managed-by, project, environment, created-by
- Tests con mocking de Ollama (no requieren cluster ni LLM)
- Builds con `--substitutions=COMMIT_SHA=$(git rev-parse --short HEAD)`
- Helpers de test en `tests/helpers.py` (no importar desde conftest.py)

## Notas importantes

- NUNCA borrar el PVC `ollama-pvc` — perderías los modelos LLM cargados
- NUNCA usar recursos cluster-scoped (ClusterRole, ClusterRoleBinding) — el usuario no tiene permisos y podría afectar a otros
- Los docs/ se mantienen actualizados con errores encontrados y soluciones
- El guion original menciona Jira, pero el proyecto usa Mattermost (ChatOps) en su lugar
- Para type hints con clases factory de terceros (ej. `chromadb.HttpClient`), usar `from __future__ import annotations` en vez de `X | None`
- El campo `startsAt` es obligatorio en `AlertItem` — incluirlo siempre en payloads de test de Alertmanager
