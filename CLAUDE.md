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

Skills del método (`.claude/skills/`, locales/gitignored). **3 de ciclo de sesión**:
- **`/start`** al empezar — reconstruye contexto (método + roadmap `docs/07` + última bitácora) y propone el micro-objetivo. Sin que tengas que dar contexto.
- **`/log`** a media sesión / **antes de un compact** / al cerrar — captura en `docs_sesion/` (`Objetivo · Hecho · Encontrado · Decisiones+porqué · Siguiente`, frontmatter `promoted: false`). Capa cruda, append-only.
- **`/promote`** al consolidar (fin de sesión o de fase) — destila la bitácora (`promoted: false`) a los docs canónicos (07, CLAUDE.md, 01-06, 11...) + vault de Obsidian, y marca `promoted: true`.

**6 operativas** (creadas 2026-07-07, cada una nace de una lección que se pagaba a mano): **`/ship`** (build+deploy con la disciplina de tags como Paso 0), **`/chaos-run`** (runbook humano del arco chaos: pre-vuelo, port-forwards, qué observar, screenshots Gate 8, teardown verificado como último gate), **`/eval`** (los 3 scripts de evaluación + modo R4), **`/ensayo`** (entrevistador hostil calibrado a chapter), **`/quality`** (docs/08 hecho invocable), **`/review-senior`** (auditoría arquitecto externo, serie F-xx). Principio: *las lecciones se convierten en gates, no en párrafos*.

Reglas no negociables:
- Microtasks (~20 min), flujo **Proposal → Validation → Execution**. Recomendar con postura, no enumerar opciones sin más.
- Comandos shell **en una línea** (el Cloud Shell de empresa rompe multilínea/heredocs).
- **NUNCA** `git add`/`commit`/`push` ni `pytest` — los corre Jay. Provee el comando como texto, **sin `Co-Authored-By`**.
- Docs/planificación en español, código en inglés. "Construir despacio y bien". "Docs reflect reality, not ambition". Repo limpio (sin binarios/caché/duplicados).

## Estado actual

Fuente única de estado y planificación: **`docs/07-roadmap.md`** (léelo al empezar, o usa `/start`).

- **TFM ya evaluado.** Objetivo actual: **presentación a un chapter** (MasOrange/Telecable), **15 julio 2026**. Foco: production-readiness + features de valor real.
- Sistema **completo y desplegado**; en cluster corre **`aiops-agent:2ac3c5d`** (S7, 2026-07-13). Fases 0-3 + Mini-Fase 4 + RAG + F2 (cola) + F3 (CPU + re-sourcing) + **v2 completa** (Ejes A+B) + **F4 R1/R2/R2·3/R3** (retrieval outcome-aware, p@1 73%→100%) + **F-03** (chroma no bloqueante) + **gap R2-humano cerrado** (`ca159be`) + **R4 corrido** (2026-07-09: Capa A retrieval 0→46.7% p@1; Capa B null pre-registrado — el 1.5b ignora la etiqueta de outcome) + **R5 bucle observacional** (validado en cluster 2026-07-13: `aiops_incident_resolution_seconds{OOMKilled}`=92.47s) + **F-17** (logs+events al prompt) + **C-07** (pre-flight `auth can-i` free-text) + **C-08** (doble botón model/×2, HMAC por acción, **validado E2E 2026-07-13**) + **audit trail** (`7e40837`: log `AUDIT human decision` en approve/reject) + **fix C-08** (`2ac3c5d`: `sanitize_action_id` — MM valida `action_id` alfanumérico, el `_` daba 404 en MM). **Tests: 695 funciones en 17 ficheros; pytest colecta ~700 (verde confirmado 07-11 con 691; los ~4 de S7 pendientes de re-confirmar).** **Deck v3 "Observz"** (`demo/demo_v3.html` + `guion_v3.html`, 15+4 slides offline con replay embebido del run real + 23 QA tras la review hostil del 07-13) listo. Capturas S7: `mattermost_c08_double_button.png` + `mattermost_approved_patch_ok.png`.
- **S3·b CERRADO (validado en cluster 2026-07-06)**: el arco `cured` completo E2E → `aiops_feedback_verdict_total{outcome="cured"}=1.0` + doc ChromaDB re-marcado `cured`. Con `cured`+`rolled_back` reales → **R4 desbloqueado**. Lecciones del run: **horno nocturno** (un `cured` con `stress` infinito quema CPU para siempre → teardown = último gate del run, codificado en `scripts/chaos_arc.sh` + `/chaos-run`); **factibilidad ≠ seguridad** (`kubectl top` Forbidden por least-privilege → cerrado con C-07: pre-flight `auth can-i`); **tag build/manifiesto** (`0914611` = hijo solo-manifiesto de `ca159be` → NO rebuildear para cuadrar el tag).
- **v2 (auditoría 5.9/10 → 3 P0) CERRADA en código**: **Eje B** (P0·2 auth fail-closed, P0·3 rollback durable Redis) y **Eje A grounding** (`agent/enrichment.py`: snapshot + identidad por ownerReferences + seal + cluster facts al prompt + confidence grounded + regla 4.7 target fantasma + paridad humano/auto). Tesis hecha código: "el cluster informa, el modelo razona, el motor dispone".
- **Review senior post-hardening (7.1/10, F-01..F-18)**: cerrados guard anti-placeholder + F-05 (dedup compensación) + F-02 (gate kind=Deployment) + **F-01 (cooldown por workload, SETNX 600s fail-closed)** + **F-04 (mattermost_timeout=10s)** + **F-03 (chroma no bloqueante, `asyncio.to_thread`)** + **F-17 (logs+events, 2026-07-10)** + **F-06 (vía docs 2026-07-12: claim degradado; AOF+PVC en v2.1)**; queda F-11. **CI GitHub Actions** (`.github/workflows/ci.yml`: tests espejo de Cloud Build + docker build), estrenado en el push de `8a40fdc`.
- **Validado EN CLUSTER 2026-07-04** (el payoff): grounding Eje A real (`current_value=32Mi` del snapshot, no del LLM; `grounded=1.0`); safety cap 4.6 (LLM 512Mi=16× → escala, no clampa); human-in-the-loop E2E (approve HMAC OK → patch persiste); rollback durable + R2. **Hallazgos** (todos con causa raíz, ninguno bloqueante de diseño): falso-rollback por restarts benignos del manifiesto (cerrado con C-01: `lastState.reason`); LLM = techo de hardware (147-213s warm en CPU); approve humano no siembra cooldown (cerrado con C-02); `ESCALATION_TTL` (subido a 120min); `scripts/chaos.sh` no observa el arco (cerrado con `chaos_arc.sh`).
- **S7 ✅ (2026-07-13)**: deploy `2ac3c5d` + 2 arcos chaos. El arco #1 destapó el **bug C-08 underscore→404** (cazado desde logs de dos componentes: 0 POSTs en el agente + 404 en MM); fix + regresión + redeploy + arco #2 validado E2E (`approve_engine` → patch → `rolled_back` a +300s: el ×2=64Mi no basta contra stress de 100M — solo el botón modelo 512Mi cura con este manifiesto; el operador determina el veredicto). Gotchas operativos: limpiar Redis pre-arco (el smoke test siembra cooldown+índice e intenta remediar), ollama sin `curl` (warm vía port-forward), `set image` explícito tras bump del manifiesto.
- **En curso / siguiente (ruta a chapter)**: **pendiente hoy** — screenshot Grafana R5 (Gate 8; la serie está en el TSDB de Prometheus, no se pierde con reinicios del agente) + commit de consolidación de docs (07-12 + los de hoy) + capturas nuevas. **14-jul (S8)** — `/ensayo` hostil (23 QA del guion v3 + munición nueva: bug C-08 cazado en vivo, dos veredictos por fingerprint, `rolled_back` como safety-net del fix humano insuficiente, defensa N=15 con IC/Fisher, posicionamiento vs K8sGPT/HolmesGPT) + timing ~15 min + plan B físico (PDF, copia móvil/USB) + vault end-session. **15-jul** — checklist mañana (fichero abre offline, N/F probados, respaldos R1-R4 localizados). R4 ✅; E1-E6 cerrada honesta en `docs/14`; F-06 resuelto por vía docs (claim degradado). Post-chapter: decidir orden v2.1 (F-15 → structured outputs → F-11 + AOF+PVC → KPIs) + apuestas ITBench / hardening adversarial.

## Stack

Python 3.11 | FastAPI | httpx | Pydantic v2 | Ollama (qwen2.5:1.5b en K8s, tinyllama default en config.py) | GKE | Cloud Build

## Archivos clave

```
agent/main.py           → FastAPI app (endpoints, retry, metrics, webhook)
agent/config.py         → Settings (pydantic-settings) + JSON logging. Incluye ollama_temperature=0.0 (greedy determinista), remediation_auto_cpu_enabled=False (gate auto-CPU), remediation_auto_namespace_prefix="arturo-" (guardrail blast-radius), enrichment_enabled=True + enrichment_timeout=10 (kill-switch grounding), remediation_auto_min_restarts=1 (gate confidence grounded), remediation_cooldown_seconds=600 (F-01), mattermost_timeout=10.0 (F-04), enrichment_log_tail_lines=20 + enrichment_log_max_chars=2000 + enrichment_events_limit=5 (F-17), incident_correlation_ttl_seconds=3600 (R5)
agent/schemas.py        → Modelos Pydantic v2 (AlertmanagerPayload, AlertItem, ProposedAction, ExtractResponse...)
agent/extraction.py     → 3 estrategias de extracción JSON
agent/validation.py     → Validación de parámetros GCP
agent/tf_generator.py   → Generación de template Terraform (Fase 0 legacy)
agent/mattermost.py     → Cliente HTTP async Mattermost con retry/backoff. C-08: send_escalation_with_buttons(..., approve_variants=None) renderiza un botón por variante y cada botón firma su propia action vía make_hmac_token (la elección model↔engine va HMAC-protegida); helper interno _button. Fix S7: sanitize_action_id() reduce el `id` del botón a [A-Za-z0-9] (MM valida action_id alfanumérico — `approve_engine` con `_` daba 404 EN Mattermost sin llegar al agente); context.action conserva el `_` (es lo que el handler lee y el HMAC firma)
agent/rag.py            → Cliente ChromaDB, ingesta, query, embeddings via Ollama. R1 retrieval guiado por metadata: ALERTNAME_TO_ERROR_CLASS (traduce prefijo KubePod de Prometheus) + error_class_for_alertname (identidad como default) + runbook_filter_for_alert; retrieve_context two-stage (filtro por error_class + fallback semántico si 0 matches). main.py pasa el filtro; eval con toggle EVAL_NO_FILTER. F-03: TODAS las llamadas HTTP síncronas de chroma envueltas en asyncio.to_thread (retrieve_context inner _blocking, ingest_* vía _upsert) — no congelan el event loop
agent/diagnosis.py      → Prompt AIOps contextual, generate_diagnosis(..., snapshot=None), JSON estructurado. Genera a temperature=settings.ollama_temperature (0.0). Sección "CLUSTER FACTS" en el prompt vía format_cluster_facts(snapshot) (fail-soft: "" sin snapshot). Prompt anti-alucinación
agent/enrichment.py     → Grounding v2 (Eje A): gather_incident_context(labels) → IncidentSnapshot (container/limits/phase/restart_count/last_state_reason + workload_kind/name/match_labels vía ownerReferences pod→RS→Deployment, get final = gate de existencia). F-17: + logs_tail (`_gather_logs`: `kubectl logs --tail`, `--previous` si el contenedor murió/reinició con fallback a current, cap líneas+chars) + recent_events (`_gather_events`: `--field-selector involvedObject.name`, newest-last, límite) → los consume `format_cluster_facts` (bloques RECENT LOGS/EVENTS). `_kubectl_json`/`_kubectl_text` argv sin shell, timeout corto, nunca raisea, proc.kill() en timeout. Fail-soft: nunca bloquea el pipeline (cada gather por separado). OJO: extra={"args":...} en logging revienta (reservado LogRecord) — usar cmd_args
agent/remediation.py    → Motor de remediación: validation layer + 9 reglas en cascada + re-sourcing + sellado. seal_proposed_action() (sella identidad/current_value/match_labels desde el snapshot; kind≠Deployment o workload sin confirmar → anula PA + marker target_unresolved → regla 4.7 escala pre-regla 5; síntesis new_value=2×current si el del LLM es inusable), derive/ground_confidence() (confidence desde señales del cluster, model_confidence preservada), is_structured_remediation() (fuente única elegibilidad auto), build_set_resources_command() (síntesis determinista, fuente única humano/auto), acquire_workload_cooldown() (F-01: SETNX aiops:cooldown:{ns}/{name} TTL 600s, gate fail-closed en process_remediation solo rama auto estructurada), bypass regla 5 si estructurado. Field-agnostic cpu+memoria; regla 4.6 cap ≤2×. C-07 (factibilidad ≠ seguridad): auth_can_i_args()/check_command_executable()/partition_by_permission() — pre-flight `kubectl auth can-i` de comandos free-text (top pod→pods.metrics.k8s.io Forbidden bajo least-privilege), fail-open salvo "no" explícito; main.py separa aprobables (botones) de denegados (sugeridos sin permiso) en la escalación free-text, solo ESCALATE, el comando estructurado no pasa por el check. C-08: structured_command_variants(diagnosis) — 2 variantes {engine ×2 determinista, model valor LLM} cuando difieren (ambas vía build_set_resources_command), 1 con acción legacy approve si coinciden o current no doblable; handler acepta (approve, approve_engine, approve_model) con fallback a safe_commands; safety-net value-agnostic
agent/utils.py          → backoff_delay() helper (exponential backoff compartido)
agent/escalation_store.py → store/get/delete/count async de escalaciones en Redis (fail-open)
agent/rollback_store.py → store/delete/list async de RollbackContext en Redis (fail-open, clave rollback:{id}, TTL nativo). Backstop de durabilidad del rollback (v2 P0·3): main.py persiste al programar, borra al evaluar, y _recover_rollbacks() re-arma al arranque tras un reinicio a mitad de ventana
agent/incident_index.py → R5 (bucle observacional): índice fail-open fingerprint→incidente activo en Redis (clave incident:active:{fp}, TTL incident_correlation_ttl_seconds=3600). record_active_incident / pop_active_incident (GET+DEL). main.py: al ingerir indexa {doc_id, error_class, started_at, awaits_verdict, text, metadata}; la rama resolved del webhook lanza _correlate_resolution → métrica aiops_incident_resolution_seconds{error_class} + (si awaits_verdict=False) re-upsert a resolved_observed. _alert_fingerprint = fuente única del fingerprint (dedup + R5). El veredicto del rollback (cured/rolled_back, fuerte) es dueño del outcome; resolved_observed (débil) nunca lo pisa (el approve suelta la entrada al programar rollback)
agent/streams.py        → Cola Redis Streams (F2): enqueue_alert (dedup SETNX + XADD, fail-closed), ensure_group(start_id), consume_loop (XREADGROUP 1-a-1 in-process; self-healing ante NOGROUP → recrea grupo con id=$ + backoff, mata busy-spin), reclaim_pending (XPENDING+XCLAIM+dead-letter, fail-soft), métricas aiops_queue_*
agent/tests/            → 695 funciones de test en 17 ficheros (pytest colecta ~700; verde 07-11 con 691, los ~4 de S7 pendientes de re-confirmar). Nuevos: S7 (audit ×2 en test_endpoints, C-08 render/sanitize en test_mattermost — lección: el mock del envío no cubría el payload real del botón), test_incident_index.py (R5 ×15), test_eval_feedback.py (R4 ×16); C-08: TestStructuredCommandVariants + TestCommandVariantEscalation/Callback; C-07: TestAuthCanIArgs/TestCheckCommandExecutable/TestPartitionByPermission; F-17: TestGatherLogs/TestGatherEvents; paridad R2-humano: test_approve_feeds_verdict_loop_with_doc_id; F4 R1: TestErrorClassForAlertname + TestMetadataFilteredRetrieval en test_rag.py; v2: TestSealProposedAction, TestDeriveConfidence, TestGroundConfidence; F-01: TestWorkloadCooldown; F2: test_streams.py; más TestChaosMetrics, TestRollbackScheduling/TestEvaluateRollback, escalation_store ×15. Gotcha: helpers.FakeRedis necesita métodos espejo (set(nx=,ex=)) para código SETNX nuevo
.claude/skills/         → Skills del método de trabajo (gitignored, locales): 3 de ciclo (start | log | promote) + 6 operativas (ship | chaos-run | eval | ensayo | quality | review-senior)
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
scripts/build_demo_v3.py → Genera el deck v3 "Observz" demo/demo_v3.html + guion_v3.html (fichero único offline: fuentes OFL de demo/assets/fonts/ + capturas como data URIs; editar el .py y regenerar, NUNCA el HTML de 2.7MB)
cloudbuild.yaml         → Pipeline: tests (gate) + build + push (lanzado a mano por Jay)
.github/workflows/ci.yml → CI en push/PR: tests (espejo del gate de Cloud Build) + docker build sin push
demo/                   → Deck para el chapter: demo_v3.html + guion_v3.html (v3 "Observz", el que se presenta: 15+4 slides, replay embebido del run real, 23 QA) · capturas .png. Reveal.js descartado (defensa.md §6). (Deck v1 demo.html/guion.html + build_demo.py eliminados en la limpieza pre-handoff 2026-07-15)
memoria/                → Memoria del TFM en LaTeX (main.tex, capitulos/, demos/)
```

## Entorno

- **Cluster**: ai-infra-agent (europe-southwest1-a, e2-standard-2 spot + 2 nodos guaranteed con label `guaranteed=true` desde 2026-05-06)
- **Namespaces**: `arturo-llm-test` (core), `arturo-monitoring` (alertmanager), `arturo-mattermost` (chatops), `arturo-chaos` (chaos experiments)
- **Registry**: europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent
- **Imagen actual**: `aiops-agent:2ac3c5d` (desplegada S7 2026-07-13; nota: el nombre de imagen es `aiops-agent`, no `agent`). Recordar: el commit del manifiesto NO despliega — hace falta `kubectl set image` explícito. El tag de deploy es SIEMPRE el short SHA del commit que BUILDEÓ, nunca el build ID de Cloud Build (los tags del registry son short SHAs de 7 chars; usar el build ID da ImagePullBackOff). **Gotcha del commit solo-manifiesto**: un commit que solo bumpea `deployment-agent.yaml` mueve HEAD → un rebuild saldría con un tag nuevo (`0914611`, hijo de `ca159be`) que invalida su propio manifiesto. Salida: NO rebuildear para "cuadrar" el tag — `0914611` = mismo código que `ca159be`, se despliega y el bump de manifiesto se commitea sin rebuild
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
