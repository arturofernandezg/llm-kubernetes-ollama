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
| `docs/06-testing.md` | Tests por fichero (422 en 14), mocking, errores comunes y soluciones |
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
- Sistema **completo**; en cluster corre `aiops-agent:da7aafb`; la imagen **`cb2d1db` está buildeada pero NO desplegada** (bloqueada en recuperación de secrets — un `secrets-setup.sh` con placeholders pisó el webhook real; runbook de recuperación en la bitácora 2026-07-02). Fases 0-3 + Mini-Fase 4 + RAG + F2 (cola) + F3 (CPU + re-sourcing) + **v2 completa en código** (Ejes A+B) + **F4 R1** (retrieval por metadata, p@1 73%→100%). **573 tests en 15 ficheros.**
- **v2 (auditoría 5.9/10 → 3 P0) CERRADA en código**: **Eje B** (P0·2 auth fail-closed, P0·3 rollback durable Redis) y **Eje A grounding** (`agent/enrichment.py`: snapshot + identidad por ownerReferences + seal + cluster facts al prompt + confidence grounded + regla 4.7 target fantasma + paridad humano/auto). Tesis hecha código: "el cluster informa, el modelo razona, el motor dispone".
- **Review senior post-hardening (7.1/10, F-01..F-18)**: cerrados guard anti-placeholder + F-05 (dedup compensación) + F-02 (gate kind=Deployment) + **F-01 (cooldown por workload, SETNX 600s fail-closed)** + **F-04 (mattermost_timeout=10s)**; quedan F-03/F-06/F-11/F-17. **CI GitHub Actions** nuevo (`.github/workflows/ci.yml`: tests espejo de Cloud Build + docker build). *Todo esto en working tree, commit pendiente de Jay.*
- **En curso / siguiente**: (1) Jay: pytest + commit + push (activa CI) + recuperación secrets + deploy `set image` + **chaos OOM del arco completo** (validar: seal desde snapshot, `aiops_enrichment_total{gathered}`, sin `NotFound`, confidence grounded en MM); (2) mejoras RAG (**R1 filtro metadata ✅ hecho 2026-07-03**, p@1 73%→100%; siguen R3 higiene query → R2 bucle aprendizaje real → R4 métrica gain — detalle en `docs/07`/`docs/10`); (3) H1 restante de la review; (4) F1 matriz E1–E6 (`docs/14`); Gate 8 screenshots.

## Stack

Python 3.11 | FastAPI | httpx | Pydantic v2 | Ollama (qwen2.5:1.5b en K8s, tinyllama default en config.py) | GKE | Cloud Build

## Archivos clave

```
agent/main.py           → FastAPI app (endpoints, retry, metrics, webhook)
agent/config.py         → Settings (pydantic-settings) + JSON logging. Incluye ollama_temperature=0.0 (greedy determinista), remediation_auto_cpu_enabled=False (gate auto-CPU), remediation_auto_namespace_prefix="arturo-" (guardrail blast-radius), enrichment_enabled=True + enrichment_timeout=10 (kill-switch grounding), remediation_auto_min_restarts=1 (gate confidence grounded), remediation_cooldown_seconds=600 (F-01), mattermost_timeout=10.0 (F-04)
agent/schemas.py        → Modelos Pydantic v2 (AlertmanagerPayload, AlertItem, ProposedAction, ExtractResponse...)
agent/extraction.py     → 3 estrategias de extracción JSON
agent/validation.py     → Validación de parámetros GCP
agent/tf_generator.py   → Generación de template Terraform (Fase 0 legacy)
agent/mattermost.py     → Cliente HTTP async Mattermost con retry/backoff
agent/rag.py            → Cliente ChromaDB, ingesta, query, embeddings via Ollama. R1 retrieval guiado por metadata: ALERTNAME_TO_ERROR_CLASS (traduce prefijo KubePod de Prometheus) + error_class_for_alertname (identidad como default) + runbook_filter_for_alert; retrieve_context two-stage (filtro por error_class + fallback semántico si 0 matches). main.py pasa el filtro; eval con toggle EVAL_NO_FILTER
agent/diagnosis.py      → Prompt AIOps contextual, generate_diagnosis(..., snapshot=None), JSON estructurado. Genera a temperature=settings.ollama_temperature (0.0). Sección "CLUSTER FACTS" en el prompt vía format_cluster_facts(snapshot) (fail-soft: "" sin snapshot). Prompt anti-alucinación
agent/enrichment.py     → Grounding v2 (Eje A): gather_incident_context(labels) → IncidentSnapshot (container/limits/phase/restart_count/last_state_reason + workload_kind/name/match_labels vía ownerReferences pod→RS→Deployment, get final = gate de existencia). _kubectl_json argv sin shell, timeout corto, nunca raisea, proc.kill() en timeout. Fail-soft: nunca bloquea el pipeline. OJO: extra={"args":...} en logging revienta (reservado LogRecord) — usar cmd_args
agent/remediation.py    → Motor de remediación: validation layer + 9 reglas en cascada + re-sourcing + sellado. seal_proposed_action() (sella identidad/current_value/match_labels desde el snapshot; kind≠Deployment o workload sin confirmar → anula PA + marker target_unresolved → regla 4.7 escala pre-regla 5; síntesis new_value=2×current si el del LLM es inusable), derive/ground_confidence() (confidence desde señales del cluster, model_confidence preservada), is_structured_remediation() (fuente única elegibilidad auto), build_set_resources_command() (síntesis determinista, fuente única humano/auto), acquire_workload_cooldown() (F-01: SETNX aiops:cooldown:{ns}/{name} TTL 600s, gate fail-closed en process_remediation solo rama auto estructurada), bypass regla 5 si estructurado. Field-agnostic cpu+memoria; regla 4.6 cap ≤2×
agent/utils.py          → backoff_delay() helper (exponential backoff compartido)
agent/escalation_store.py → store/get/delete/count async de escalaciones en Redis (fail-open)
agent/rollback_store.py → store/delete/list async de RollbackContext en Redis (fail-open, clave rollback:{id}, TTL nativo). Backstop de durabilidad del rollback (v2 P0·3): main.py persiste al programar, borra al evaluar, y _recover_rollbacks() re-arma al arranque tras un reinicio a mitad de ventana
agent/streams.py        → Cola Redis Streams (F2): enqueue_alert (dedup SETNX + XADD, fail-closed), ensure_group(start_id), consume_loop (XREADGROUP 1-a-1 in-process; self-healing ante NOGROUP → recrea grupo con id=$ + backoff, mata busy-spin), reclaim_pending (XPENDING+XCLAIM+dead-letter, fail-soft), métricas aiops_queue_*
agent/tests/            → 573 funciones de test en 15 ficheros (F4 R1: TestErrorClassForAlertname + TestMetadataFilteredRetrieval ×8 en test_rag.py; test_enrichment.py nuevo ×25; v2: TestSealProposedAction, TestDeriveConfidence, TestGroundConfidence, TestFormatClusterFacts; F-01: TestWorkloadCooldown; F3: TestStructuredAutoRemediation, TestProcessRemediationCpuAuto; F2: test_streams.py; más TestChaosMetrics, TestSlashCommandEndpoint, TestRollbackScheduling/TestEvaluateRollback, TestRagReconnect, escalation_store ×15)
.claude/skills/         → Skills del método de trabajo: start | log | promote (gitignored, locales)
docs_sesion/            → Bitácora cruda por sesión (skill /log); se promueve a docs canónicos con /promote
generate_tf.py          → CLI generador de .tf (importa de agent/tf_generator.py)
k8s/                    → Manifiestos K8s (agent, ollama, chromadb, redis, networkpolicy)
k8s/redis.yaml          → Deployment redis:7-alpine + Service redis-svc:6379 (escalaciones + cola Streams F2: stream aiops:alerts + PEL + dead-letter aiops:alerts:dead). limits 128Mi/150m CPU (mem subida de 64Mi por la cola; CPU 50m→150m como defensa en profundidad — la raíz del HighCPU se curó en código, no aquí)
k8s/prometheus.yaml     → Prometheus + kube-state-metrics + ClusterRoles + 6 reglas + metric_relabel_configs (fix label collision KSM/cAdvisor 2026-05-26). Tenancy cluster compartido: 6 reglas acotadas a arturo-.*; KSM --namespaces=arturo-*; cadvisor metric_relabel keep namespace=~"arturo-.*"; TargetDown solo job=kubernetes-endpoints
k8s/grafana.yaml        → Grafana stateless + 2 dashboards (Overview con fila Cola aiops_queue_* + Chaos); panel scrape-targets acotado a servicios propios (cluster compartido)
k8s/alertmanager.yaml   → Alertmanager standalone en arturo-monitoring
k8s/mattermost.yaml     → Mattermost + PostgreSQL en arturo-mattermost
k8s/rbac.yaml           → Role + RoleBinding para remediación autónoma en arturo-llm-test y arturo-chaos (cross-namespace RoleBinding añadido 2026-05-28)
k8s/chaos/              → Manifests chaos engineering (arturo-chaos namespace)
scripts/chaos.sh        → Runner bash: oom | crashloop | bad-image | cpu | status | cleanup
scripts/build_demo.py   → Genera el deck único demo/demo.html + guion.html (CHAOS_RESULTS embebidos)
cloudbuild.yaml         → Pipeline: tests (gate) + build + push (lanzado a mano por Jay)
.github/workflows/ci.yml → CI en push/PR: tests (espejo del gate de Cloud Build) + docker build sin push
demo/                   → Deck de presentación único (demo.html + guion.html). Reveal.js descartado (defensa.md §6)
memoria/                → Memoria del TFM en LaTeX (main.tex, capitulos/, demos/)
```

## Entorno

- **Cluster**: ai-infra-agent (europe-southwest1-a, e2-standard-2 spot + 2 nodos guaranteed con label `guaranteed=true` desde 2026-05-06)
- **Namespaces**: `arturo-llm-test` (core), `arturo-monitoring` (alertmanager), `arturo-mattermost` (chatops), `arturo-chaos` (chaos experiments)
- **Registry**: europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent
- **Imagen actual**: `aiops-agent:da7aafb` (nota: el nombre de imagen es `aiops-agent`, no `agent`). El tag de deploy es SIEMPRE el short SHA del commit, nunca el build ID de Cloud Build (los tags del registry son short SHAs de 7 chars; usar el build ID da ImagePullBackOff)
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
