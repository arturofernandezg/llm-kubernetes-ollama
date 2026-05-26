# Roadmap — Fases del proyecto (Evolución AIOps)

> Última actualización: 2026-05-23 (consolidación pre-reunión chapter)

## Estado de las fases

| Fase | Descripción | Estado |
|---|---|---|
| Fase 0 (Legado) | Agente + Ollama + extracción de params + generación .tf | Completa (En desuso activo) |
| Fase 1 (Observabilidad) | Prometheus standalone + Alertmanager, webhook, Mattermost ChatOps | En curso (~95%) |
| Fase 2 (RAG) | ChromaDB dual-collection, embeddings in-cluster, diagnóstico contextual | **Completa** (2026-04-23) |
| Fase 3 (Remediación) | Auto-patch K8s API, validation layer, feedback loop cerrado | **Completa** (2026-05-12) |
| Mini-Fase 4 (Production Readiness) | Chaos engineering, métricas reales, slash command MM, rollback, hardening | En curso (código S1-S6 listo, E2E pendiente) |

---

## Fase 0 (Legado) — Completada

- [x] Agente FastAPI con extracción de parámetros via LLM
- [x] Ollama desplegado en K8s con PVC
- [x] Generador de Terraform (generate_tf.py + agent/tf_generator.py)
- [x] 64 tests unitarios y de integración (4 ficheros)
- [x] Cloud Build con versionado ($COMMIT_SHA + :latest) y tests como gate
- [x] Cliente httpx compartido (no uno nuevo por request)
- [x] PodDisruptionBudget para Ollama
- [x] Código modularizado: config.py, schemas.py, extraction.py, validation.py, tf_generator.py

---

## Fase 1 — Observabilidad y ChatOps (EN CURSO)

### Enrutamiento de Alertas
- [x] Implementar endpoint `POST /webhook/alert` en FastAPI con Data Contract Alertmanager.
- [x] Schemas Pydantic: `AlertmanagerPayload`, `AlertItem`.
- [x] Módulo `mattermost.py` con retry y exponential backoff.
- [x] Alertmanager standalone desplegado en `arturo-monitoring` (manifiesto propio, sin helm).
  - ⚠️ kube-prometheus-stack descartado: requiere `ClusterRoles` → permisos IAM insuficientes.
  - Alertmanager ConfigMap con receiver apuntando a `agent-svc.arturo-llm-test.svc.cluster.local:8000/webhook/alert`.
- [x] Conectividad cross-namespace verificada: Alertmanager (`arturo-monitoring`) → Agent (`arturo-llm-test`).
- [x] Imagen Docker nueva (tag `5f64b61`) con todos los módulos Fase 1+2 buildeada y desplegada.
  - 103 tests pasando en Cloud Build (incluyendo tests RAG y diagnosis).
  - Fix aplicado: `from __future__ import annotations` en `rag.py` para compatibilidad Python 3.11 runtime con type hints de `chromadb.HttpClient`.
- [x] Prometheus standalone desplegado en `arturo-monitoring` (decisión tutor 2026-04-20: mínimo, no kube-prometheus-stack).
  - `k8s/prometheus.yaml`: Prometheus + kube-state-metrics + ClusterRole de lectura + 5 reglas AIOps.
  - 5 reglas: `KubePodOOMKilled`, `KubePodCrashLoopBackOff`, `HighMemory`, `HighCPU`, `TargetDown`.
  - Scrape del agente vía annotations `prometheus.io/scrape=true` en `service-agent.yaml`.
  - `prometheus-rules.yaml` (CRD obsoleto) y `prometheus-stack-values.yaml` eliminados.

### ChatOps (Mattermost)
- [x] Instalar Mattermost en el cluster (manifiesto propio en `arturo-mattermost`).
  - PostgreSQL StatefulSet + Mattermost Deployment operativos.
- [ ] Configurar webhook entrante + token bot para el agente.
- [ ] Desarrollar formateo enriquecido: diagnóstico del LLM renderizado como mensaje
  Mattermost con severity, commands sugeridos y botones de acción (Fase 3).

### Infraestructura de red
- [x] NetworkPolicy actualizada para cross-namespace (Alertmanager→Agent, Agent→Mattermost, Agent→ChromaDB).
- [x] 3 namespaces operativos: `arturo-llm-test`, `arturo-monitoring`, `arturo-mattermost`.

### Estado del cluster (2026-03-23)
| Namespace | Pod | Estado |
|---|---|---|
| `arturo-llm-test` | agent | ✅ Running (imagen 5f64b61) |
| `arturo-llm-test` | ollama | ✅ Running |
| `arturo-llm-test` | chromadb-0 | ⏳ Fix pendiente de aplicar (ver diagnóstico abajo) |
| `arturo-monitoring` | alertmanager | ✅ Running |
| `arturo-mattermost` | mattermost | ✅ Running |
| `arturo-mattermost` | postgres | ✅ Running |

#### Diagnóstico ChromaDB CrashLoopBackOff (investigado 2026-03-23)

**Error**: `ERROR: Error loading ASGI app. Could not import module "chromadb.app".`

**Causa raíz**: La imagen `chromadb/chroma:0.4.24` tiene un bug — su script de arranque ejecuta
`uvicorn chromadb.app:app` pero ese módulo no existe en esa versión del paquete Python interno.
No es un problema de permisos ni de recursos.

**Causa secundaria**: Incompatibilidad de versión. El cliente Python del agente usa
`chromadb-client==0.6.3` (ver `agent/requirements.txt`) pero el servidor era `0.4.24`.

**Fix aplicado en manifest**: Imagen actualizada a `chromadb/chroma:0.6.3` en
`k8s/chromadb.yaml`. También se añadieron liveness/readiness probes apuntando a
`GET /api/v1/heartbeat:8000`.

**Nota**: el pod desplegado antes de este fix tenía un ConfigMap huérfano (`chroma-log-config`)
montado que no está en nuestro manifest. Al hacer `kubectl apply` con el manifest actualizado,
si falla, borrar el StatefulSet con `--cascade=orphan` (preserva PVC) y re-aplicar.

**Pendiente**: aplicar el manifest en Cloud Shell con `kubectl apply -f k8s/chromadb.yaml`
y verificar que el pod arranca correctamente.

### Entregable Fase 1
Pipeline end-to-end: Alerta de Prometheus → Alertmanager → FastAPI webhook → notificación
formateada en Mattermost con datos de la alerta. Sin LLM/RAG aún — solo routing + formateo.

### Pendiente para completar Fase 1
1. **Configurar webhook entrante en Mattermost** (obtener URL del incoming webhook).
2. **Setear env `MATTERMOST_WEBHOOK_URL`** en el Deployment del agente.

### Completado en sesiones posteriores (2026-04-22)
- ✅ **NetworkPolicy de scrape** — regla `ingress` en `arturo-llm-test` desde `app=prometheus` en `arturo-monitoring` → puerto 8000 (`networkpolicy.yaml`).
- ✅ **KSM mirror a AR** — `registry.k8s.io` inaccesible sin Cloud NAT; imagen copiada con `crane copy --platform linux/amd64` a AR. `imagePullPolicy: Always` para re-pulls futuros.
- ✅ **scrape_timeout: 20s** — añadido al `global:` de Prometheus (default 10s era insuficiente con el agente procesando diagnósticos LLM).
- ✅ **Targets UP** — kube-state-metrics y agent-svc ambos UP en `/targets`.

### Completado en sesiones posteriores (cont.)
- ✅ **Grafana** (2026-04-22) — datasource Prometheus + dashboard "AIOps Agent — Overview" (9 paneles, 4 filas) + contact point `aiops-agent-webhook` provisionados vía ConfigMap. Stateless (`emptyDir`). Secret `grafana-admin` externo (patrón `secrets-setup.sh`). Acceso vía `kubectl port-forward svc/grafana-svc 3000:3000 -n arturo-monitoring`. NetworkPolicy actualizada: Grafana (`arturo-monitoring`) → agent webhook port 8000. Manifiesto: `k8s/grafana.yaml`.

### Próximas sesiones

→ Ver [§ TODO Consolidado](#todo-consolidado--siguientes-pasos) para la lista unificada de pendientes.

---

## Fase 2 — RAG y Diagnóstico Contextual

### Infraestructura
- [x] ChromaDB StatefulSet desplegado (manifiesto en `k8s/chromadb.yaml`).
  - ⚠️ Imagen corregida de `0.4.24` → `0.6.3` (bug de módulo ASGI + incompatibilidad con cliente `0.6.3`).
  - Se añadieron liveness/readiness probes (`/api/v1/heartbeat`).
  - **Pendiente**: `kubectl apply -f k8s/chromadb.yaml` en Cloud Shell.
- [ ] Cargar modelo de embeddings `nomic-embed-text` (274 MB) en Ollama (mismo flujo manual).
- [x] NetworkPolicy actualizada: tráfico agent → chromadb-svc:8000 permitido.

### Módulos nuevos del agente
- [x] **`rag.py`**: cliente ChromaDB (chromadb-client), funciones de ingesta y query,
  construcción de queries enriquecidas. 12 tests unitarios.
- [x] **`diagnosis.py`**: prompt template AIOps contextual (alerta + contexto RAG → JSON
  estructurado), parsing de respuesta del LLM con validación de schema. 14 tests unitarios.

### Knowledge Base
- [x] 16 runbooks semilla en `agent/runbooks/*.yaml` para alertas K8s comunes:
  OOMKilled, CrashLoopBackOff, ImagePullBackOff, HighCPU, HighMemory, PodEvicted,
  NodeNotReady, DiskPressure, NetworkUnavailable, TargetDown, KubeCPUOvercommit,
  KubeMemoryOvercommit, PodNotReady, ContainerWaiting, JobFailed, PersistentVolumeFillingUp.
- [x] Función batch `ingest_all_runbooks()` + `load_runbooks_from_dir()` en `rag.py`.
  11 tests nuevos (parsing, ingesta, resiliencia). Dependencia: PyYAML==6.0.2.
- [x] CLI `agent/ingest_runbooks.py` + K8s Job `k8s/job-ingest-runbooks.yaml` para ingesta idempotente en cluster (2026-04-23). 16 runbooks ingestados. `runAsUser: 1000` requerido en GKE (runAsNonRoot con UID simbólico rechazado por kubelet).
- [x] E2E RAG verificado (2026-04-23): KubePodOOMKilled → RAG 3 runbooks + 2 incidents → LLM 187s (confidence=0.85, risk=high, suggest_only) → Mattermost enriquecido. `HTTP_TIMEOUT=240` en deployment-agent.yaml.
- [ ] Crear colección `incidents` (vacía inicialmente, se llena con feedback loop).
- [x] Metadata schema definido: `error_class`, `service`, `severity`, `commands` (string).

### Pipeline RAG completo
- [x] Flujo: alerta → normalizar → embedding → query ChromaDB → construir prompt
  con contexto → LLM genera JSON estructurado → formatear para Mattermost.
  - Implementado en `main.py`: `_process_alert_with_diagnosis()` (BackgroundTask) + `_format_diagnosis_message()`.
  - Triple fail-open: ChromaDB down → contexto vacío; Ollama down → diagnosis=None; ambos → raw fallback.
  - Prometheus counter `aiops_diagnosis_total` con labels: `success`, `rag_ok`, `rag_failed`, `llm_failed`, `pipeline_failed`.
  - 124/124 tests pasando (9 tests nuevos para el pipeline + fix de conftest.py).
- [x] Output del LLM: `{ diagnosis, commands[], confidence, risk, explanation }`.

### Entregable Fase 2
Pipeline end-to-end con RAG: misma alerta ahora genera un diagnóstico contextualizado
con runbook relevante, comandos sugeridos y nivel de confianza. Notificación enriquecida
en Mattermost.

---

## Fase 3 — Remediación Autónoma y Feedback Loop ✅ COMPLETA (2026-05-12)

### Auto-remediación
- [x] **`remediation.py`**: validation layer (whitelist/blacklist de comandos via regex),
  motor de decisión con 7 reglas en cascada, executor con dual mode (dry-run / real). 54 tests unitarios.
  - Config-gated: `REMEDIATION_ENABLED=false` y `REMEDIATION_DRY_RUN=true` por defecto.
  - Ejecución real via `asyncio.create_subprocess_exec` con timeout configurable.
  - Solo ejecuta comandos `kubectl` — cualquier otra cosa se rechaza con [SKIP].
- [x] Integrar `process_remediation()` en el pipeline de `main.py`.
  - Prometheus counter `aiops_remediation_total` con labels: `auto_remediate`, `escalate`, `suggest_only`, `skipped`.
  - Formatter actualizado con bloques auto-remediation/escalation.
  - 5 tests de integración en `test_endpoints.py`.
- [x] RBAC least-privilege: Role + RoleBinding para el ServiceAccount del agente con
  permisos solo de `patch` y `get` sobre `deployments`, `pods`, `limitranges` en el namespace.
  Manifiesto en `k8s/rbac.yaml`. Aplicado en cluster (2026-04-24).
- [x] `REMEDIATION_ENABLED=true` + `REMEDIATION_DRY_RUN=true` en `deployment-agent.yaml` (2026-04-24).
  E2E verificado: KubePodOOMKilled → `action=escalate` (confidence=0.90, risk=high). Motor activo.
- [x] Umbrales de auto-ejecución — **condición tutor implementada (2026-04-25)**:
  - Regla 4.5: cualquier comando MUTATING que implique reinicio de pod → **ESCALATE** (`reason_code: pod_restart_blocked`). Aplica a: `rollout restart`, `set resources`, `scale`, `patch deployment/statefulset/daemonset`.
  - Regla 4.6: si `proposed_action.field == resources.limits.memory` y `new_value > 2 × current_value` → **ESCALATE** (`reason_code: memory_exceeds_2x`). Fail-safe en error de parseo (`reason_code: unparseable_memory`).
  - Backward-compatible: sin `proposed_action` → regla 4.6 se salta, legacy risk/confidence deciden.
  - Schema LLM extendido: campo opcional `proposed_action` con `kind/name/namespace/container/field/current_value/new_value`.
  - `REMEDIATION_DRY_RUN=true` sigue activo — paso a real requiere acuerdo con tutor.
  - Pendiente confirmar con tutor: ¿excepciones a regla 4.5? (in-place resize k8s 1.27+, rolling update en HA...).
- [x] **E2E cluster verificado (2026-04-24, imagen c3b0975)**: regla 4.5 (`set_resources_triggers_rollout`) y regla 4.6 (`memory_exceeds_2x`, `auto_remediate` en límite 2×, `unparseable_memory`) confirmados via `kubectl exec` sobre binario desplegado. Webhook E2E: escalate → Mattermost → ChromaDB persistence.
- ✅ **Evaluación inicial (2026-04-30)** — COMPLETA:
  - ✅ Módulo `agent/evaluation/` + dataset 10 alertas + 3 scripts offline. 15 tests pasando.
  - ✅ `eval_retrieval`: **precision@1=60% (6/10), precision@3=80% (8/10)**. `evaluation_results/retrieval_2026-04-30.json`.
  - ✅ `eval_actionability`: **RAG 100% (27/27 cmds), avg_confidence=0.86**; zero_shot 100% (12/12 cmds), avg_confidence=0.63. `evaluation_results/actionability_2026-04-30.json`.
  - ✅ `eval_safety`: **RAG 100% SAFE (27/27)**; zero_shot 25% SAFE + 67% UNKNOWN (alucinaciones) + 8.3% BLOCKED (true positive: `kubectl delete`). `evaluation_results/safety_2026-04-30.json`.
  - ✅ `docs/10-evaluation.md` — tabla completa + análisis de casos límite. Listo para memoria TFM.

### Feedback Loop (Memoria Semántica)
- [x] Tras cada remediación (aprobada o rechazada), persistir el incidente completo
  en la colección `incidents` de ChromaDB. Implementado en `main.py` con `FEEDBACK_COUNTER` Prometheus.
- [x] Estructura: alerta original + diagnóstico + fix propuesto + outcome (auto_remediate/escalate/suggest_only/no_remediation).
  Builder en `rag.py`: `build_incident_document()`. Fail-open: si ChromaDB falla, el pipeline continúa.
- ⏳ Monitorización de bucle cerrado — **post-MVP**: verificar en Prometheus que la alerta cesa
  tras aplicar el fix. Si cesa → `outcome: resolved`. Si persiste → `outcome: failed`, escalar.
  (Requiere `REMEDIATION_DRY_RUN=false` — bloqueado hasta acuerdo con tutor sobre regla 4.5.)

### Botones interactivos (Mattermost)
- [x] Mensajes con acciones: `[✅ Ejecutar remediación]` / `[❌ Rechazar]` (2026-05-06).
- [x] Endpoint `POST /webhook/action` — callback que recibe decisión del humano, ejecuta/aborta, persiste outcome en ChromaDB y actualiza el mensaje original limpiando los botones.
  - Estado en memoria `PENDING_ESCALATIONS` (dict con TTL 60 min, sin deps nuevas).
  - Solo `safe_commands` (no blacklist) son aprobables por el humano.
  - Contadores Prometheus: `human_approved` + `human_rejected`.
  - NetworkPolicy: Mattermost → agent puerto 8000 añadida.
  - Env var `AGENT_CALLBACK_URL` en deployment para configurar URL de callback.

### Sesiones de calidad #1-#8 (2026-05-11 → 2026-05-12)
- [x] **Sesiones #1-#7** (2026-05-11 → 2026-05-12): 43 findings corregidos en remediation.py, main.py, mattermost.py, rag.py. Nuevos módulos: `utils.py` (backoff_delay). 272 tests totales. Ver `docs/11-quality-backlog.md`.
- [x] **Sesión #8** (2026-05-12): `nodeSelector: guaranteed="true"` + tolerations en los 3 workloads críticos (`agent`, `ollama`, `chromadb`) para forzar scheduling en nodos guaranteed. Docs sync cierre Fase 3.

### Entregable Fase 3
Sistema autónomo: el agente auto-parchea OOMs simples, escala a humano los casos complejos,
y aprende de cada decisión para mejorar diagnósticos futuros.

---

## Cronograma de Ejecución (Roadmap 2 Meses - 8 Semanas)

Horizonte temporal: ciclo final del proyecto TFM/TFG. Prioridad: pipeline funcional
end-to-end > componentes sofisticados a medio implementar.

### MES 1: Observabilidad + RAG (Fases 1 y 2)

| Semana | Objetivo | Entregable concreto |
|---|---|---|
| 1-2 | Observabilidad base | kube-prometheus-stack desplegado, alerting rules definidas, Mattermost operativo, pipeline Alerta → Mattermost funcionando |
| 3 | ChromaDB + embeddings | ChromaDB StatefulSet activo, `nomic-embed-text` cargado en Ollama, módulo `rag.py` con ingesta y query básica |
| 4 | RAG end-to-end | 15 runbooks semilla cargados, módulo `diagnosis.py`, pipeline completo: alerta → RAG → diagnóstico → Mattermost |

### MES 2: Remediación + Evaluación (Fase 3 + Cierre)

| Semana | Objetivo | Entregable concreto |
|---|---|---|
| 5 | Structured output + validation | JSON output del LLM validado, whitelist de comandos, bloqueo de destructivos |
| 6 | Auto-remediación MVP | RBAC configurado, `remediation.py`, auto-patch de memory limits (caso OOMKilled) |
| 7 | Evaluación + feedback loop | Métricas medidas (MTTR, precision, actionability, safety), colección `incidents` acumulando datos |
| 8 | Tests + documentación + cierre | Tests nuevos para módulos RAG/diagnosis/remediation, memoria descriptiva del TFM |

---

## Estrategia de Evaluación (Métricas para la Tesis)

El valor diferencial del TFM está en medir el impacto real del sistema, no solo en construirlo.

| Métrica | Qué mide | Cómo se obtiene |
|---|---|---|
| **MTTR** (Mean Time To Resolve) | Tiempo desde alerta firing → fix aplicado | Timestamps de Prometheus (alert start) vs timestamp de patch aplicado |
| **Retrieval Precision** | ¿Los runbooks devueltos son relevantes? | Evaluación manual de 20-30 queries contra ground truth |
| **Actionability Rate** | % de outputs del LLM con comandos ejecutables válidos | Revisión de N diagnósticos: ¿el comando es sintácticamente correcto y semánticamente apropiado? |
| **Safety Rate** | % de outputs sin comandos destructivos | Validation layer + revisión manual |
| **Latencia E2E** | Alerta → notificación en Mattermost | Métricas Prometheus (histograma del webhook) |
| **Feedback Loop Gain** | ¿Mejora la precisión del RAG con incidentes acumulados? | Comparar retrieval precision con 0 incidentes vs N incidentes |

**Evaluación offline**: datasets de alertas simuladas (JSON payloads de Alertmanager)
contra ground truth de runbooks esperados. No requiere cluster activo.

**Evaluación online**: alertas reales del cluster en producción (provocadas o naturales)
medidas en el pipeline real.

---

## Modos de Fallo Conocidos

| Componente | Fallo | Mitigación |
|---|---|---|
| Log/Alert parsing | Alertas con labels inesperados o vacíos | Defaults seguros en normalización, log del payload raw |
| Embeddings | Error nuevo sin vecinos similares en ChromaDB | Threshold de similarity mínimo; si no hay match → LLM razona sin contexto RAG (zero-shot) |
| Retrieval | Documentos irrelevantes devueltos | Filtrado por metadata (`error_class`, `service`), top-K conservador (3-5) |
| LLM | Hallucination, comandos incorrectos | Structured output + validation layer + whitelist. Si no parsea JSON → fallback a mensaje genérico |
| LLM | Comandos destructivos | Blacklist explícita. Nunca auto-ejecutar sin pasar validation layer |
| Auto-patch | Fix aplicado pero alerta no cesa | Monitorización post-fix (30-60s). Si persiste → revertir + escalar a humano |
| ChromaDB | Pod evicted (spot node) | StatefulSet con PVC garantiza datos persistentes. Pod se re-schedula automáticamente |
| Ollama | Modelo no cargado tras restart de nodo | Readiness probe existente (/readyz) ya detecta esto. El agente no procesa hasta que Ollama esté ready |

---

## Mini-Fase 4 — Production Readiness (código S1-S6 listo, E2E pendiente)

Objetivo: pasar de "capacidad demostrada" a "evidencia medida" para defensa de TFG.

| Sesión | Objetivo | Estado |
|---|---|---|
| #1 — Chaos Engineering #1 | OOMKilled + CrashLoopBackOff, harness reproducible | ✅ Infraestructura lista (2026-05-18) |
| #2 — Chaos Engineering #2 | Deploy defectuoso + nodo saturado | ✅ Infraestructura lista (2026-05-19) |
| #3 — Dashboard Chaos | Grafana "AIOps — Chaos" con MTTD/MTTR | ✅ Infraestructura lista (2026-05-18) — screenshot pendiente (requiere cluster) |
| #4 — Slash command MM | `/aiops status`, `/aiops incidents`, `/aiops help` | ✅ Completada (2026-05-19) |
| #5 — Rollback automático | Rollback si remediación no resuelve en N min | ✅ Completada (2026-05-19) |
| #6 — Hardening pre-prod | smoke.sh, backup ChromaDB, flip `DRY_RUN=false` (acuerdo tutor) | ⏳ En ejecución (2026-05-19) |

- [x] **Sesión #1** (2026-05-18): manifests `k8s/chaos/`, script `scripts/chaos.sh`, métricas `aiops_chaos_*`, doc `docs/12-chaos-engineering.md`.
- [x] **Sesión #2** (2026-05-19): `k8s/chaos/chaos-bad-image.yaml` + `chaos-cpu-stress.yaml`, regla `KubePodImagePullBackOff` en `k8s/prometheus.yaml`, 2 casos nuevos en `scripts/chaos.sh`, 279 tests.
- [x] **Sesión #3** (2026-05-18): Dashboard "AIOps — Chaos" en `k8s/grafana.yaml` (ConfigMap `grafana-dashboard-aiops`). 7 paneles: MTTD/MTTR p95, MTTD p50/p95 global, pie outcome, stat total, ALERTS, tabla histórico.
- [x] **Sesión #4** (2026-05-19): `POST /webhook/command` — slash command `/aiops` (status/incidents/help). Schema `MattermostCommandPayload`, config `MM_COMMAND_TOKEN` (fail-open), ~15 tests. K8s env en `deployment-agent.yaml`.
- [x] **Sesión #5** (2026-05-19): Rollback automático. `ExecuteResult` dataclass (frozen) + `execute_commands() → list[ExecuteResult]`. Helpers `capture_pre_patch_value()`, `check_pod_health()`, `revert_patch()`. `IN_FLIGHT_ROLLBACKS` registry + `asyncio.create_task`. Counter `aiops_remediation_rollback_total{outcome}` (6 outcomes). ~310 tests.
- [ ] **Sesión #6** (2026-05-19): Hardening pre-prod. Artefactos entregados: `scripts/smoke.sh`, pin imagen `:3fde9f8`. Pendiente ejecución: pytest gate + Cloud Build + deploy + chaos experiments (MTTD/MTTR) + screenshot Grafana + backup ChromaDB.

---

## TODO Consolidado — Siguientes pasos

*Fuente única de verdad. Todas las tareas pendientes por orden de prioridad.*

### 1. Sesión de pruebas E2E Mini-Fase 4 (bloqueante)

Ejecutar en secuencia en el cluster. Contexto completo: `docs_sesion/2026-05-19-hardening-06.md`.

- [ ] **Gate 0 — pytest** (bloqueante): `pytest agent/tests/ -v --tb=short` — ~310 tests esperados en verde.
- [ ] **Gate 1 — Cloud Build**: `gcloud builds submit --config cloudbuild.yaml --substitutions=COMMIT_SHA=$(git rev-parse --short HEAD)`
- [ ] **Gate 2 — Deploy + rollout**: `kubectl apply -f k8s/deployment-agent.yaml -n arturo-llm-test && kubectl rollout status deployment/agent -n arturo-llm-test --timeout=180s` — imagen `:3fde9f8` desplegada.
- [ ] **Gate 3 — Logs limpios**: `kubectl logs -n arturo-llm-test deploy/agent --tail=30` — startup OK, `REMEDIATION_ROLLBACK_ENABLED=true` visible.
- [ ] **Gate 4 — Smoke test**: `bash scripts/smoke.sh` — debe terminar `=== SMOKE TEST PASSED ===` con exit 0.
- [ ] **Gate 5 — Métricas rollback**: `kubectl exec -n arturo-llm-test deploy/agent -- curl -s localhost:8000/metrics | grep aiops_remediation_rollback_total`
- [ ] **Gate 6 — Chaos experiments** (uno a uno, NO en paralelo): `bash scripts/chaos.sh oom`, `bash scripts/chaos.sh crashloop`, `bash scripts/chaos.sh bad-image`, `bash scripts/chaos.sh cpu`, `bash scripts/chaos.sh cleanup`. Anotar `T_pod_fail` y MTTD/MTTR por experimento.
- [ ] **Gate 7 — Rellenar tabla MTTD/MTTR** en `docs/12-chaos-engineering.md` líneas 103-106 con valores reales.
- [ ] **Gate 8 — Screenshots Grafana**: `kubectl port-forward svc/grafana-svc 3000:3000 -n arturo-monitoring` → capturar `docs/img/grafana-chaos.png` y `docs/img/grafana-overview.png`.
- [ ] **Gate 9 — Backup ChromaDB**: `kubectl exec -n arturo-llm-test chromadb-0 -- tar czf /tmp/chromadb-backup.tar.gz -C /chroma chroma && kubectl cp -n arturo-llm-test chromadb-0:/tmp/chromadb-backup.tar.gz ./chromadb-backup-$(date +%Y%m%d).tar.gz`

**Cierre tras pasar todos los gates**: actualizar S6 → ✅ en roadmap, `09-estado-actual-tutor.md`, `CLAUDE.md`, `12-chaos-engineering.md`.

### 2. Tutor-gates (aprobados ✅ 2026-05-23 — código implementado 2026-05-25)

- [x] **Flip `REMEDIATION_DRY_RUN=false`** — `k8s/deployment-agent.yaml` (línea 79, env var `REMEDIATION_DRY_RUN`). Implementado 2026-05-25. Rollback si falla en cluster: `kubectl set env deployment/agent REMEDIATION_DRY_RUN=true -n arturo-llm-test`. *(Nota: docs anteriores decían "línea 81" — error; línea 81 es `REMEDIATION_ROLLBACK_ENABLED`.)*
- [x] **Excepción regla 4.5** — `agent/remediation.py`: constantes `_SET_RESOURCES_EXCEPTION_MIN_CONFIDENCE=0.9` / `_MAX_RISK="medium"`, helper `_set_resources_memory_exception()`. Autoriza `kubectl set resources` si `confidence ≥ 0.9` AND `risk ≤ medium` AND `proposed_action.field == resources.limits.memory`. NUNCA scale/rollout/patch. 10 tests nuevos. Implementado 2026-05-25. *(Nota: con `risk=high` del LLM en OOM típico, rule 5 sigue escalando — defensa en profundidad.)*

### 3. Demo para chapter/tutor (post chaos experiments)

Solicitud del tutor (2026-05-26): preparar demo en vivo después de cerrar los chaos experiments.

- [ ] **Guion demo**: provocar fallo real (OOMKilled con stress pod), mostrar flujo completo en pantalla — Prometheus → Alertmanager → agente → Mattermost con botones → aprobar → ejecución.
- [ ] **Preparar entorno**: tener Grafana, Mattermost y logs del agente visibles en paralelo (3 ventanas / port-forwards activos).
- [ ] **Script de provocación**: uno o dos one-liners para lanzar el fallo y otro para hacer cleanup post-demo.
- [ ] **Fallback**: si el LLM tarda mucho en directo, tener capturas de pantalla del último chaos experiment como respaldo visual.

### 4. Cierre Fase 1 — Webhook entrante Mattermost

Pendiente desde 2026-04-22. No bloquea defensa pero cierra la fase formalmente.

- [ ] Obtener URL del incoming webhook de Mattermost (UI: Settings → Integrations → Incoming Webhooks).
- [ ] Setear env `MATTERMOST_WEBHOOK_URL` en `k8s/deployment-agent.yaml`.

### 4. Screenshots Grafana (para docs y defensa)

El directorio `docs/img/` existe pero está vacío. Capturar post Gate 8.

- [ ] `docs/img/grafana-overview.png` — dashboard "AIOps Agent — Overview".
- [ ] `docs/img/grafana-chaos.png` — dashboard "AIOps — Chaos".

---

## Gates pendientes — historial (referencia)

> Los dos gates del tutor ya tienen green light (2026-05-23). Ver § TODO Consolidado #2 para la implementación.

---

## Mejoras técnicas pendientes (transversales / post-TFM)

- [ ] Evaluar framework retrospectivamente (ej. Robusta.dev) para futuras iteraciones de AIOps.
- [ ] Mapeo de ServiceAccounts de Kubernetes (Workload Identity) a IAM GCP.
- [ ] Solicitar rol `roles/logging.logWriter` para service account de Cloud Build.
- [ ] Buckets de histograma Prometheus personalizados para /webhook/alert (5s, 10s, 30s, 60s).
- [ ] Caché in-memory (dict con TTL) para embeddings + resultados RAG: indexar por fingerprint `alertname+namespace+pod`. Alertas repetidas del mismo tipo (OOMKilled en loop) evitan llamadas redundantes a Ollama (~200ms/embedding) y a ChromaDB. Sin deps nuevas — dict Python en `main.py`. Ver `docs/01-architecture.md § Alternativas descartadas` para contexto.
- [ ] Cola de mensajes entre Alertmanager y agente (NATS in-cluster o Redis Streams): el webhook HTTP actual es fire-and-forget — si el agente se reinicia mientras procesa un diagnóstico de 78s, la alerta se pierde. At-least-once delivery + replay = salto de producción real. NATS es la opción más ligera.
- [ ] Re-ranking con cross-encoder si la colección `incidents` supera ~500 documentos.
- [ ] Clasificador supervisado (multi-label) si se acumulan >5k incidentes etiquetados.
- [ ] Migración de nodos Spot a Standard para ChromaDB (evaluación coste vs estabilidad).

---

## Fase 5 — Predicción proactiva (post Mini-Fase 4)

> **[scope abierto — definir tras cierre Mini-Fase 4]**

Objetivo: pasar de *reactivo* (responde a alertas) a *proactivo* (detecta tendencias antes de la alerta).

### Concepto

- Loop periódico que consulta `prometheus-svc:9090/api/v1/query` cada N minutos.
- Detecta tendencias preocupantes: memoria subiendo, CPU sostenida, error rate creciente.
- Si detecta riesgo inminente → notifica en Mattermost antes de que Prometheus dispare la alerta.
- Integra con el pipeline existente (`diagnosis.py` + `mattermost.py`) sin duplicar código.

### Alcance tentativo

- [ ] Módulo `prediction.py` — consulta métricas Prometheus, evalúa umbrales de tendencia.
- [ ] Endpoint `GET /prediction/status` — expone estado del loop periódico.
- [ ] Prometheus counter `aiops_prediction_total{outcome}` — accuracy de predicciones emitidas.
- [ ] Tests con mock de Prometheus HTTP API.

### Decisiones pendientes antes de implementar

- ¿Con qué frecuencia scrapeamos? (1m, 5m, 15m — trade-off detección vs carga CPU)
- ¿Umbral de confianza para notificar? (falsos positivos costosos en ChatOps)
- ¿Modelo de tendencia? (media móvil simple vs regresión lineal)
- Presentar al tutor antes de implementar (scope nuevo, afecta TFG).
