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
| `docs/06-testing.md` | Tests por fichero (394 en 13), mocking, errores comunes y soluciones |
| `docs/07-roadmap.md` | **Fuente única**: estado actual + roadmap a entrega + changelog + modos de fallo + backlog |
| `docs/08-code-quality-playbook.md` | Workflow de sesiones de calidad, 8 dimensiones de scan, prompt prehecho, sesiones tentativas |
| `docs/10-evaluation.md` | Evaluación del retrieval (p@1/p@3) + RAG safety vs zero-shot |
| `docs/11-quality-backlog.md` | Backlog vivo de findings de calidad agrupados por módulo (43 findings + 7 PR-0x de F1) |
| `docs/12-chaos-engineering.md` | Chaos engineering: 4 experimentos, MTTD/MTTR, hipótesis, métricas `aiops_chaos_*` |
| `docs/13-pitch-chapter.md` | Pitch one-pager (audiencia: chapter principal — no defensa TFG) |
| `docs/14-production-readiness.md` | Entregable F1: protocolo de validación (matriz E1-E6) + 7 hallazgos PR-01..07 + plan de soluciones |

**Lee el archivo relevante antes de hacer cambios en esa parte del proyecto.**

## Método de trabajo

Ciclo de sesión con 3 skills (`.claude/skills/`):
- **`/start`** al empezar — reconstruye contexto (método + roadmap `docs/07` + última bitácora) y propone el micro-objetivo. Sin que tengas que dar contexto.
- **`/log`** a media sesión / **antes de un compact** / al cerrar — captura en `docs_sesion/` (`Objetivo · Hecho · Encontrado · Decisiones+porqué · Siguiente`, frontmatter `promoted: false`). Capa cruda, append-only.
- **`/promote`** al consolidar (fin de sesión o de fase) — destila la bitácora (`promoted: false`) a los docs canónicos (07, CLAUDE.md, 01-06, 11...) + vault de Obsidian, y marca `promoted: true`.

Reglas no negociables:
- Microtasks (~20 min), flujo **Proposal → Validation → Execution**. Recomendar con postura, no enumerar opciones sin más.
- Comandos shell **en una línea** (el Cloud Shell de empresa rompe multilínea/heredocs).
- **NUNCA** `git add`/`commit`/`push` ni `pytest` — los corre Jay. Provee el comando como texto, **sin `Co-Authored-By`**.
- Docs/planificación en español, código en inglés. "Construir despacio y bien". "Docs reflect reality, not ambition". Repo limpio (sin binarios/caché/duplicados).

## Estado actual

Fuente única de estado y planificación: **`docs/07-roadmap.md`** (léelo al empezar, o usa `/start`).

- **TFM ya evaluado.** Objetivo actual: **presentación a un chapter** (MasOrange/Telecable), fechas 8 o 14 julio 2026. Foco: production-readiness + features de valor real.
- Sistema **completo y desplegado** (imagen `aiops-agent:fd37a5d`): Fases 0-3 + Mini-Fase 4 + FASE 2. Pipeline E2E verificado (Prometheus→Alertmanager→agente→RAG+LLM→Mattermost→kubectl con rollback). 394 tests.
- **En curso**: F1 — validación en cluster + production-readiness (`docs/14`). **4 quick-wins de código hechos** (PR-01 drift timeout, PR-04 escalate sin grounding RAG, PR-05 reconexión lazy ChromaDB, PR-06 observabilidad). *Nota: los cambios de PR-01/04/05/06 viven en código, aún no horneados en imagen nueva.* Pendiente de cluster: matriz E1–E6 + PR-03 (réplicas). Siguiente: cola Redis Streams (F2), HPA/CPU (F3), bucle RAG (F4).
- Pendiente real (requiere cluster): Gate 8 — screenshots Grafana.

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
agent/tests/            → 394 funciones de test en 13 ficheros (incluye TestChaosMetrics, TestSlashCommandEndpoint, TestRollbackScheduling, TestEvaluateRollback, TestDecideActionTutorRule, TestInFlightDedup, TestDiagnosisTimeout, TestEscalationStoreMetric, TestRagReconnect, escalation_store ×15)
.claude/skills/         → Skills del método de trabajo: start | log | promote (gitignored, locales)
docs_sesion/            → Bitácora cruda por sesión (skill /log); se promueve a docs canónicos con /promote
generate_tf.py          → CLI generador de .tf (importa de agent/tf_generator.py)
k8s/                    → Manifiestos K8s (agent, ollama, chromadb, redis, networkpolicy)
k8s/redis.yaml          → Deployment redis:7-alpine + Service redis-svc:6379 (estado escalaciones)
k8s/prometheus.yaml     → Prometheus + kube-state-metrics + ClusterRoles + 6 reglas + metric_relabel_configs (fix label collision KSM/cAdvisor aplicado 2026-05-26)
k8s/alertmanager.yaml   → Alertmanager standalone en arturo-monitoring
k8s/mattermost.yaml     → Mattermost + PostgreSQL en arturo-mattermost
k8s/rbac.yaml           → Role + RoleBinding para remediación autónoma en arturo-llm-test y arturo-chaos (cross-namespace RoleBinding añadido 2026-05-28)
k8s/chaos/              → Manifests chaos engineering (arturo-chaos namespace)
scripts/chaos.sh        → Runner bash: oom | crashloop | bad-image | cpu | status | cleanup
scripts/build_demo.py   → Genera el deck único demo/demo.html + guion.html (CHAOS_RESULTS embebidos)
cloudbuild.yaml         → Pipeline: tests (gate) + build + push
demo/                   → Deck de presentación único (demo.html + guion.html). Reveal.js descartado (defensa.md §6)
memoria/                → Memoria del TFM en LaTeX (main.tex, capitulos/, demos/)
```

## Entorno

- **Cluster**: ai-infra-agent (europe-southwest1-a, e2-standard-2 spot + 2 nodos guaranteed con label `guaranteed=true` desde 2026-05-06)
- **Namespaces**: `arturo-llm-test` (core), `arturo-monitoring` (alertmanager), `arturo-mattermost` (chatops), `arturo-chaos` (chaos experiments)
- **Registry**: europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent
- **Imagen actual**: `aiops-agent:fd37a5d` (nota: el nombre de imagen es `aiops-agent`, no `agent`)
- **Ollama models**: qwen2.5:1.5b (generación), nomic-embed-text:latest (embeddings)
- Jay trabaja desde **Mac ARM (M4)** con acceso directo a GCP/GKE — git/tests/builds los lanza él a mano
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
