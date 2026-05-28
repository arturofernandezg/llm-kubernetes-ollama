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
| `docs/08-code-quality-playbook.md` | Workflow de sesiones de calidad, 8 dimensiones de scan, prompt prehecho, sesiones tentativas |
| `docs/11-quality-backlog.md` | Backlog vivo de findings de calidad agrupados por módulo (43 findings, sesiones #1-#8 completadas) |
| `docs/13-pitch-chapter.md` | Pitch one-pager (audiencia: chapter principal — no defensa TFG) |

**Lee el archivo relevante antes de hacer cambios en esa parte del proyecto.**

## Estado actual (2026-05-27)

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
- **Fase 3 (Remediación Autónoma)**: Completa (2026-05-06).
  - ✅ `remediation.py` — validation layer + motor de decisión (9 reglas) + executor dual-mode. ~70 tests.
  - ✅ `process_remediation()` integrado en pipeline `main.py`. Counter `aiops_remediation_total`.
  - ✅ RBAC aplicado (`k8s/rbac.yaml`): Role `patch deployments/get pods/limitranges` en `arturo-llm-test`.
  - ✅ `REMEDIATION_ENABLED=true`, `REMEDIATION_DRY_RUN=false` en `deployment-agent.yaml`.
  - ✅ E2E verificado (2026-04-24): KubePodOOMKilled → RAG → LLM (211s, confidence=0.90, risk=high) → **escalate** → Mattermost. Motor activo.
  - ✅ Condición del tutor implementada (2026-04-25):
    - Regla 4.5: bloquea TODO comando MUTATING que implique reinicio de pod (`set resources`, `scale`, `rollout restart`, `patch deployment`...) → `reason_code: pod_restart_blocked`. Bloqueo conservador hasta confirmación del tutor.
    - Regla 4.6: si `proposed_action.field == resources.limits.memory`, bloquea si `new > 2 × current` → `reason_code: memory_exceeds_2x`.
    - Schema LLM extendido: campo opcional `proposed_action` con `current_value/new_value/field`.
    - `parse_memory_to_bytes()` + `implies_pod_restart()` como helpers reutilizables.
  - ✅ E2E cluster verificado (2026-04-24, imagen c3b0975): regla 4.5 → `reason_code: set_resources_triggers_rollout`. Regla 4.6 → `memory_exceeds_2x` / `auto_remediate` / `unparseable_memory`. Webhook E2E: `action=escalate` por regla 5, `outcome=escalate` persistido en ChromaDB + Mattermost.
  - ✅ Botones interactivos Mattermost (2026-05-06): `send_escalation_with_buttons()` + POST `/webhook/action` + `PENDING_ESCALATIONS` dict (TTL 60 min). Imagen `aiops-agent:1033c9f`.
  - ✅ HMAC-SHA256 en callbacks de botones (2026-05-11): `make_hmac_token(incident_id, action, secret)` + `_verify_hmac_token()` + `WEBHOOK_SECRET` en config + K8s Secret injection (`optional: true`). 263 tests.
  - ✅ E2E botones verificado (2026-05-06): KubePodOOMKilled → escalate (78s, confidence=0.90) → botones ✅/❌ en Mattermost → callback → mensaje actualizado in-place.
  - ✅ Config Mattermost requerida: `MM_PLUGINSETTINGS_ENABLE=true` + `MM_SERVICEALLOWEDUNTRUSTEDINTERNALCONNECTIONS=agent-svc.arturo-llm-test.svc.cluster.local` en `k8s/mattermost.yaml`.
  - ✅ Calidad código sesión #7 (2026-05-12): S2 guard ValueError en `generate_embedding()`, S3 `m.get()` seguro en list comprehensions Ollama, S4 warning startup `WEBHOOK_SECRET`, M10 try/except fallback Mattermost, X2 `backoff_delay()` extraído a `utils.py`. 272 tests.
  - ✅ Calidad código sesión #8 (2026-05-12): `nodeSelector: guaranteed="true"` + tolerations en `deployment-agent`, `deployment-ollama`, `chromadb`. Workloads críticos fijados a nodos guaranteed. Docs sync cierre Fase 3. Sesiones #1-#8 completadas.
  - ✅ [tutor-gate] Implementado y desplegado (2026-05-25): excepción regla 4.5 (`_set_resources_memory_exception`, conf≥0.9, risk≤medium, field=resources.limits.memory, 10 tests) + `REMEDIATION_DRY_RUN=false` en `k8s/deployment-agent.yaml`. Imagen `a4421f4` en cluster.
  - ✅ Defensa prep (2026-05-15): `defensa.md §3` completada (12 preguntas difíciles + respuestas). Presentación: **Reveal.js** (decisión bloqueada en `defensa.md §6`).
- **Mini-Fase 4 (Production Readiness)**: **Completa** (2026-05-27) — solo Gate 8 (screenshots Grafana) pendiente.
  - ✅ Sesión #1 — Chaos Engineering #1: manifests `k8s/chaos/`, script `scripts/chaos.sh`, métricas `aiops_chaos_*` en agente (Counter + Histogram), doc `docs/12-chaos-engineering.md`.
  - ✅ Sesión #2 — Chaos Engineering #2: `k8s/chaos/chaos-bad-image.yaml` + `chaos-cpu-stress.yaml`, regla Prometheus `KubePodImagePullBackOff` (6ª regla en `k8s/prometheus.yaml`), 2 casos nuevos en `scripts/chaos.sh` (`bad-image`, `cpu`), 279 tests.
  - ✅ Sesión #3 — Dashboard Grafana "AIOps — Chaos": key `chaos.json` añadida al ConfigMap `grafana-dashboard-aiops` en `k8s/grafana.yaml`. 7 paneles: MTTD/MTTR p95 por experimento, MTTD p50/p95 global, pie outcome, stat total, ALERTS correlation, tabla histórico.
  - ✅ Sesión #4 — Slash command Mattermost `/aiops` (2026-05-19): `POST /webhook/command`, schema `MattermostCommandPayload`, config `MM_COMMAND_TOKEN` (fail-open + warning startup), helpers `_query_recent_incidents` (recencia desc, clamp 1–20) + `_format_status_response` + `_format_incidents_response`. 294 tests. K8s: `MM_COMMAND_TOKEN` en `deployment-agent.yaml` desde `agent-secrets` (`optional: true`).
  - ✅ Sesión #5 — Rollback automático (2026-05-19): `ExecuteResult` dataclass + refactor `execute_commands() → list[ExecuteResult]` + `results_to_log()`. Helpers `capture_pre_patch_value()` + `check_pod_health()` + `revert_patch()` en `remediation.py`. `IN_FLIGHT_ROLLBACKS` registry + `_schedule_rollback_evaluation()` + `_evaluate_rollback()` en `main.py`. Counter `aiops_remediation_rollback_total{outcome}`. ~310 tests. K8s: `REMEDIATION_ROLLBACK_ENABLED/TIMEOUT/GRACE` en `deployment-agent.yaml`.
  - ✅ Sesión #6 — Hardening pre-prod (2026-05-26): bugs fijados en `chaos.sh` (C1/B2/Q1), manifests chaos corregidos (stress-ng→stress), `main.py` (WEBHOOK_COUNTER + pre-init labeled counters), `k8s/prometheus.yaml` (`metric_relabel_configs`: fix label collision KSM/cAdvisor — exported_namespace→namespace + exported_pod→pod). `docs/12`: hipótesis+criterios de éxito (Principles of Chaos).
  - ✅ **Sesión de pruebas + FASE 2 — Completa (2026-05-27)**:
    - Gates 0-9 ✅ (Gate 8 screenshots Grafana pendiente de captura manual).
    - Prometheus fix aplicado al cluster ✅. RAG limpieza ✅: 92 incidents contaminados borrados (133 limpios + 16 runbooks). Backup: `chromadb-backup-clean-20260527.tar.gz` ✅.
    - 4 experimentos chaos re-corridos con MTTD/MTTR verificados (is_chaos=true): OOM (MTTD=5.0s, MTTR=205.4s), CrashLoop (5.0s/205.7s), BadImage (5.1s/252.1s), CPU (10.1s/206.7s). Tabla en `docs/12-chaos-engineering.md` ✅.
    - FASE 2 desplegada (Gates A0-A9 ✅, imagen `fd37a5d`):
      - ✅ **Redis persistence**: `agent/escalation_store.py` + `k8s/redis.yaml`. `PENDING_ESCALATIONS` dict eliminado; botones sobreviven restart de pod (TTL 60 min). Fail-open.
      - ✅ **Dedup in-flight**: `IN_FLIGHT_ALERTS` + counter `aiops_dedup_skipped_total{alertname}`. Alertas idénticas en vuelo se saltan.
      - ✅ **Timeout explícito**: `_format_diagnosis_message(llm_timeout=True)` → mensaje Mattermost diferenciado ("⚠️ LLM agotó el tiempo").
    - Tests: 392 passed (FakeRedis, TestInFlightDedup ×4, TestDiagnosisTimeout ×4, test_escalation_store ×15).

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
agent/utils.py          → backoff_delay() helper (exponential backoff compartido)
agent/escalation_store.py → store/get/delete/count async de escalaciones en Redis (fail-open)
agent/tests/            → ~394 tests en 12 ficheros (incluye TestChaosMetrics, TestSlashCommandEndpoint, TestRollbackScheduling, TestEvaluateRollback, TestDecideActionTutorRule, TestInFlightDedup, TestDiagnosisTimeout, escalation_store ×15)
generate_tf.py          → CLI generador de .tf (importa de agent/tf_generator.py)
k8s/                    → Manifiestos K8s (agent, ollama, chromadb, redis, networkpolicy)
k8s/redis.yaml          → Deployment redis:7-alpine + Service redis-svc:6379 (estado escalaciones)
k8s/prometheus.yaml     → Prometheus + kube-state-metrics + ClusterRoles + 6 reglas + metric_relabel_configs (fix label collision KSM/cAdvisor aplicado 2026-05-26)
k8s/alertmanager.yaml   → Alertmanager standalone en arturo-monitoring
k8s/mattermost.yaml     → Mattermost + PostgreSQL en arturo-mattermost
k8s/rbac.yaml           → Role + RoleBinding para remediación autónoma en arturo-llm-test y arturo-chaos (cross-namespace RoleBinding añadido 2026-05-28)
k8s/chaos/              → Manifests chaos engineering (arturo-chaos namespace)
scripts/chaos.sh        → Runner bash: oom | crashloop | bad-image | cpu | status | cleanup
cloudbuild.yaml         → Pipeline: tests (gate) + build + push
```

## Entorno

- **Cluster**: ai-infra-agent (europe-southwest1-a, e2-standard-2 spot + 2 nodos guaranteed con label `guaranteed=true` desde 2026-05-06)
- **Namespaces**: `arturo-llm-test` (core), `arturo-monitoring` (alertmanager), `arturo-mattermost` (chatops), `arturo-chaos` (chaos experiments)
- **Registry**: europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent
- **Imagen actual**: `aiops-agent:fd37a5d` (nota: el nombre de imagen es `aiops-agent`, no `agent`)
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
