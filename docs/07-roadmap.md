# Roadmap y Estado — AIOps Infrastructure Agent

> **Fuente única de planificación y estado del proyecto.**
> Última actualización: 2026-07-03.
> Captura cruda por sesión en `docs_sesion/` (skill `/log`); promoción a este doc vía `/promote`.

---

## Contexto

Sistema AIOps de detección y remediación de incidencias en Kubernetes: Prometheus/Alertmanager → webhook FastAPI → RAG (ChromaDB) + LLM (Ollama) → diagnóstico estructurado → Mattermost (ChatOps con botones) → remediación kubectl con validación.

- **El TFM ya está evaluado.** El objetivo actual NO es la nota.
- **Modo: presentación de empresa** a un chapter de MasOrange/Telecable. Fechas posibles: **8 o 14 de julio de 2026**.
- **Foco**: dejar trabajo de calidad y *production-credible* — validar el sistema en cluster y construir features que demuestren valor real.

---

## Estado actual (qué funciona HOY, demostrable en cluster)

**Flujo E2E implementado y verificado:**
```
Prometheus detecta → Alertmanager → webhook agente
  → RAG (ChromaDB) busca runbooks → LLM genera diagnóstico JSON
    → validación (9 reglas en cascada)
      → seguro+confiable → auto-remedia (kubectl)
      → si no → Mattermost con botones Aprobar/Rechazar → operador decide
```

**Componentes vivos:**
- **Agente FastAPI** en `arturo-llm-test`, imagen desplegada `aiops-agent:da7aafb`. Endpoints: `/webhook/alert`, `/webhook/action`, `/webhook/command`, `/healthz`, `/readyz`, `/metrics`. `REMEDIATION_DRY_RUN=false`, rollback automático activo. Fijado a nodos `guaranteed=true`. `/readyz` gated por **Redis**. *(Imagen `cb2d1db` **buildeada** (Cloud Build SUCCESS, incluye temp=0 + enrich OOM + Eje A grounding + Eje B + hardening 4.7) pero **NO desplegada** — bloqueada en la recuperación de secrets. En working tree sin commitear: fixes Alta de la review senior (guard secrets, F-05, F-02, F-01 cooldown, F-04 timeout MM) + CI GitHub Actions.)*
- **Cola Redis Streams (F2)**: el webhook **encola** en `aiops:alerts` (fail-closed: 503 si Redis cae → Alertmanager reintenta) y un consumidor in-process drena 1 a 1 corriendo el pipeline. Durabilidad (replay vía reclaim del PEL), absorción de ráfagas y dedup cluster-wide por fingerprint. Dead-letter a `aiops:alerts:dead` tras >3 entregas. **Camino único** — el legacy (BackgroundTasks + dedup in-memory) fue retirado. Módulo `agent/streams.py`. **F-05 (review senior)**: si el XADD falla tras el SETNX, la dedup-key se borra (compensación) antes de propagar — sin ella, el retry de Alertmanager chocaba con la clave huérfana y la alerta se perdía hasta el `repeat_interval`.
- **Grounding (v2 Eje A — keystone, en `cb2d1db`)**: etapa determinista `agent/enrichment.py` **antes** del LLM — `gather_incident_context(labels)` hace `kubectl get pod -o json` (fail-soft, nunca bloquea) y construye un `IncidentSnapshot` (container, limits, phase, restart_count, last_state_reason) + resolución de identidad por **ownerReferences** (pod→RS→Deployment; el `get` final es gate de existencia) + `match_labels` del selector real. Consumidores: (1) **prompt** — sección "CLUSTER FACTS" en el diagnóstico ("el modelo razona"); (2) **seal** — `seal_proposed_action` sobreescribe `name/namespace/container/current_value` con la verdad del cluster, sintetiza `new_value=2×current` si el del LLM es inusable, anula la PA si el workload no se confirma o el kind≠Deployment (F-02) → marker `target_unresolved` → **regla 4.7** escala antes de la regla 5; (3) **confidence grounded** — `derive/ground_confidence` derivan la confianza de señales del cluster (`last_state_reason` + `restart_count ≥ remediation_auto_min_restarts`) y la del modelo queda preservada en `model_confidence`. El LLM queda reducido a *field + dirección*. Métrica `aiops_enrichment_total{gathered|workload_unresolved|skipped|error}`.
- **Pipeline RAG+LLM**: 16 runbooks en ChromaDB; triple fail-open (ChromaDB / Ollama / ambos).
- **Motor de remediación**: 9 reglas en cascada + **re-sourcing de decisiones** ("el modelo propone, el motor dispone"). Dimensión **CPU además de memoria** (`resources.limits.cpu`, field-agnostic vía `_LIMIT_FIELD_PARSERS` y `_limit_resource`; capture/revert derivan el recurso de `field`). `is_structured_remediation()` es la **fuente única** de elegibilidad (field elegible + name/ns/container + valores parseables + solo-subir + namespace en el allow-list `arturo-`); cuando dispara, el motor **sintetiza** el comando con `build_set_resources_command` (determinista, acotado ≤2× por la regla 4.6, reversible por snapshot) y **bypasea la regla 5** (el `risk` auto-rating del modelo lo sustituye el bound del motor; el suelo de confianza de la regla 6 se mantiene). Auto-CPU tras flag `remediation_auto_cpu_enabled` (default off, escalate-first hasta validar). Clasificación regex BLOCKED > SAFE > MUTATING. **Cooldown por workload (F-01, working tree)**: SETNX `aiops:cooldown:{ns}/{name}` TTL 600s (> ventana rollback) en la rama auto estructurada — un solo patch auto por workload por ventana (corta el patch-storm de alertas re-firing); bloqueado o Redis no verificable → ESCALATE fail-closed con el comando determinista para el humano.
- **ChatOps**: Mattermost + botones HMAC-SHA256; escalaciones persistidas en **Redis** (`escalation_store.py`, fail-open); slash command `/aiops`. **Auth fail-closed (v2 P0·2)**: con `REMEDIATION_DRY_RUN=false` un `WEBHOOK_SECRET`/`MM_COMMAND_TOKEN` vacío rechaza (401) en vez de fail-open — no hay remediación no autenticada; `secrets-setup.sh` alineado con las claves que el deployment lee de verdad + **guard anti-placeholder** (aborta si detecta `<...>` sin sustituir — respuesta al incidente que pisó el webhook real). **Paridad humano/auto (`cb2d1db`)**: `process_remediation` sintetiza el `structured_command` UNA vez; la escalación estructurada guarda/enseña ese comando (no el free-text del LLM) y el approve captura snapshot + programa rollback igual que el auto, reusando el `incident_id` — *what you approve is what runs*. Mensajes muestran la confidence grounded ("100% grounded del cluster; el modelo dijo 65%"). **F-04**: timeout dedicado `mattermost_timeout=10s` (antes heredaba los 300s del LLM → retries de minutos con MM caído).
- **Rollback durable (v2 P0·3)**: el `RollbackContext` se persiste en Redis (`rollback_store.py`, espejo de `escalation_store`) además del dict en memoria; `_recover_rollbacks()` re-arma al arranque los rollbacks que quedaron a mitad de ventana tras un reinicio del pod (antes: el patch quedaba aplicado y **nunca** se revertía). `_evaluate_rollback` duerme el tiempo restante hasta el deadline, no el timeout fijo.
- **Observabilidad**: Prometheus + kube-state-metrics + Alertmanager + Grafana (2 dashboards: Overview + Chaos; Overview incluye la fila **Cola Redis Streams** con `aiops_queue_*` — Gate 8 "Paso F" hecho). 6 reglas de alerta. Métricas de diagnóstico granulares: `aiops_diagnosis_total{outcome}` (incl. `rag_reconnect`/`llm_timeout`/`llm_error`) + `aiops_escalation_store_total{stored|redis_down}` (PR-06) + cola `aiops_queue_*` (`enqueued`/`processed{outcome}`/`reclaimed`/`dead`/`depth`). **Tenancy en cluster compartido**: reglas + scrape acotados a `arturo-.*` (KSM `--namespaces`, cadvisor `metric_relabel keep`, 6 reglas con filtro de namespace) — dejamos de leer/alertar sobre workloads ajenos.
- **Tests**: **565 funciones en 15 ficheros** (incluye `test_enrichment.py`), mockeados (cola con `AsyncMock` — `FakeRedis` no soporta streams); Cloud Build los corre como gate. *(Los ~9 de la sesión 2026-07-03 — F-05/F-01/F-04 — pendientes de pytest por Jay.)*
- **CI (GitHub Actions, working tree)**: `.github/workflows/ci.yml` en cada push/PR — job `tests` (espejo literal del gate de Cloud Build: python 3.11, mismo install, mismo pytest) + job `docker-build` (valida el Dockerfile, sin push). El build/push de imagen sigue en Cloud Build (lanzado por Jay, requiere GCP).

**Stack:** Python 3.11 · FastAPI · httpx · Pydantic v2 · Ollama (qwen2.5:1.5b + nomic-embed-text) · ChromaDB · Redis · Mattermost+PostgreSQL · Prometheus/Alertmanager/Grafana · GKE · Cloud Build.

**Infra desplegada:**
```
arturo-llm-test:   agent · ollama · chromadb (16 runbooks) · redis
arturo-monitoring: prometheus · kube-state-metrics · alertmanager · grafana
arturo-mattermost: mattermost · postgres
arturo-chaos:      manifests chaos (4 experimentos)
```

**Decisiones de diseño clave:** Ollama in-cluster (datos no salen, sin coste/token) · retrieval-first (no classification) · fail-open en todo · validation layer obligatoria (el LLM nunca ejecuta directo) · remediación config-gated · HMAC en callbacks.

---

## Roadmap a entrega (presentación chapter, 8 o 14 julio 2026)

> A groso modo. Las sesiones se planifican sobre la marcha; cada una se captura con `/log` y se promueve con `/promote`.

| Fase | Objetivo | Estado |
|---|---|---|
| **F0** — Setup de proceso | Unificar docs (07 fuente única, retirar 09) + skills `/start` `/log` `/promote` + este roadmap | ✅ 2026-06-24 |
| **F1** — Validación en cluster | Entender y validar el sistema desplegado; informe de production-readiness | 🔵 En curso — informe `docs/14` + 4 quick-wins de código hechos (PR-01/04/05/06); PR-02/03/07 cerrados por F2; falta matriz E1–E6 en cluster |
| **F2** — Cola Redis Streams | Desacoplar la ingesta de alertas del LLM lento (cuello de botella) | ✅ 2026-06-29 — `streams.py`, cola única validada en cluster (replay + dead-letter + readyz Redis); legacy retirado |
| **F3** — remediación CPU + auto real | Cerrar el hueco HighCPU + hacer que el auto **dispare de verdad** (re-sourcing) | ✅ 2026-07-02 — la slice C (sourcing determinista) quedó **absorbida y cerrada por el Eje A** (enrichment sella target/valor/selector desde el cluster); falta solo la re-validación chaos en cluster con `cb2d1db` |
| **F4** — Bucle de aprendizaje RAG | Gate de calidad en ingesta + retrieval que mejora con el histórico | Pendiente — **análisis hecho 2026-07-03** (R1-R4, ver detalle F4 abajo); R2 = el bucle real. *(Ojo: las bitácoras `f4-context-enrichment` usan "F4" para el enriquecimiento de contexto de alertas, soporte de F3)* |
| **F5** — Predicción proactiva | (stretch) Forecast de tendencia → acción preventiva | Backlog |
| **F6** — Presentación | Pulir `demo/demo.html` + Gate 8 (screenshots Grafana) + ensayo demo | Días antes (julio) |

### v2 — Auditoría arquitectónica → 3 ejes (2026-07-01)

Una auditoría senior (rol arquitecto DevOps/SRE/MLOps; informe modo-libro en `docs_sesion/2026-07-01-auditoria-arquitectura.html`) puntuó el sistema **5.9/10** con "rechazo para producción" por **3 bloqueantes P0**. Reencuadran el trabajo pendiente como una **v2** cuyo hilo es *grounding*: **"el cluster informa, el modelo razona, el motor dispone"** (el re-sourcing de F3 selló el comando; falta que el target/valor salgan del cluster, no del LLM).

| Eje | Contenido | P0 | Estado |
|---|---|---|---|
| **A — Grounding (keystone)** | `agent/enrichment.py`: gather K8s **antes** del LLM; `current_value`/target/selector desde el snapshot; snapshot al prompt; confidence grounded; regla 4.7 target fantasma; paridad comando humano/auto. Absorbió la **slice C** de F3. | P0·1 | ✅ 2026-07-02 — completo en código, en imagen `cb2d1db`; pendiente validación chaos en cluster |
| **B — Confianza** | Auth fail-closed + rollback durable. | P0·2, P0·3 | ✅ 2026-07-01 |
| **C — Cobertura** | Detectar Pending/Evicted/NotReady/FailedJob vía KSM (cero código, solo reglas de alerta). | — | Backlog |

**Review senior post-hardening (2026-07-02, `docs_sesion/2026-07-02-paper-review-senior.html`)**: 7.1/10, 18 hallazgos F-01..F-18 (post-`cb2d1db`, no incluye los P0/P1 ya cerrados). **H0 (antes del chapter)**: secrets + deploy + chaos + Gate 8 + promote. **H1 (6 Alta)**: ✅ F-05 (dedup compensación), ✅ F-02 (gate kind=Deployment), ✅ F-01 (cooldown por workload), ✅ F-04 (timeout MM); ⬜ F-03 (chroma vía `asyncio.to_thread`), ⬜ F-06 (AOF+PVC Redis o degradar el claim de durabilidad en docs). Resto: ⬜ F-11 (incident_id desde la ingesta), ⬜ F-17 (logs+events al prompt del LLM).

Findings menores del informe (no bloqueantes): selector `-l app={name}` hardcodeado en `check_pod_health`; poisoning RAG diferido (incidentes desde salidas del LLM); código muerto Fase 0 en el binario; escalabilidad Ollama serializado (~1 alerta/4min). F4 (aprendizaje RAG) y F5 (predicción) quedan como v2.5/v3 — no compiten por la ventana de julio.

### F1 — Validación en cluster (detalle)
- **Hecho (código, sin cluster)**: análisis del pipeline real → informe `docs/14` (matriz E1–E6 + 7 hallazgos PR-01..07). 4 quick-wins cerrados con tests mockeados: PR-04 (escalate sin grounding RAG), PR-06 (observabilidad: split `llm_timeout`/`llm_error` + counter de escalación), PR-05 (reconexión lazy de ChromaDB + `rag_reconnect`), PR-01 (alinear default `http_timeout`). Quedan para cluster: PR-02 (decisión readiness, →F2), PR-03 (dedup per-pod, →F2), PR-07 (pérdida de alerta, →F2).
- Trazar el pipeline E2E completo hasta poder narrarlo de memoria.
- **Chaos sobre dependencias propias**: matar Redis / Ollama / ChromaDB en mitad de un diagnóstico → demostrar fail-open.
- **Test de concurrencia**: N alertas simultáneas → ver el dedup de FASE 2 en acción.
- Salida: **informe honesto de production-readiness** (qué aguanta, qué no, qué falta) + guion de demo.

### F2 — Cola Redis Streams (detalle) — ✅ completada 2026-06-29
- **Hecho**: módulo `agent/streams.py` sobre el Redis ya desplegado. El webhook encola (`enqueue_alert`: dedup SETNX por fingerprint + `XADD MAXLEN ~`, fail-closed) y un consumidor in-process (`consume_loop`, XREADGROUP 1 a 1) corre `_process_alert_with_diagnosis` sin tocarlo. Durabilidad vía `reclaim_pending` (XPENDING idle-filtrado + XCLAIM, fail-soft); dead-letter a `aiops:alerts:dead` con forense tras `queue_max_deliveries` (3). `/readyz` gated por Redis. Métricas `aiops_queue_*`.
- **Topología in-process (no worker aparte)**: Ollama serializa la generación → la cola no compra paralelismo; su valor es durabilidad + absorber ráfagas + dedup cluster-wide. Con réplicas>1, el consumer group ya da dedup+durabilidad.
- **Semántica at-least-once** asumida y documentada: un crash tras el LLM antes del XACK reprocesa (posible Mattermost duplicado / remediación repetida); mitigada por dedup-key y dead-letter.
- **Validado en cluster (Slice 4, 2026-06-29)**: ráfaga de 10 sin pérdida (webhook 200 inmediato), replay tras matar el pod (`reclaimed_total=1`), dead-letter de un poison (`dead_total=1`), `/readyz=503` con Redis a 0.
- **Legacy retirado**: eliminados el flag `queue_enabled`, `IN_FLIGHT_ALERTS`/`_INFLIGHT_LOCK` y el camino BackgroundTasks del webhook; la cola es el único path (Redis caído → 503, sin degradación síncrona). Imagen `aiops-agent:5d5d7c7`.

### F3 — remediación CPU + auto real (detalle)
- **Mecanismo entregado** (slices 1/1b/2, imagen `da7aafb`): el motor aprende la dimensión **CPU** (bump vertical de `resources.limits.cpu`, NO scale/HPA — reusa el camino de memoria: snapshot/health/rollback). Regla 4.6 field-aware (cap ≤2× por recurso), `capture_pre_patch_value`/`revert_patch` derivan el recurso de `field`. Auto-CPU tras flag `remediation_auto_cpu_enabled` (default off). Se descartó HPA (requiere `metrics-server`, sin confirmar en GKE; cambio de estado persistente, no remediación puntual).
- **Re-sourcing (el arreglo clave del "auto nunca dispara")**: la causa raíz NO era el modelo ni el flag, era el **motor** — `process_remediation` ejecutaba los comandos investigativos del modelo (`describe/top/logs`) en vez de la `proposed_action` estructurada (que el modelo acierta 5/5). Fix: `is_structured_remediation` (fuente única) + `build_set_resources_command` (síntesis determinista) + bypass de la regla 5 (drop del `risk` del modelo, se mantiene el suelo de confianza). Guardrail namespace allow-list `arturo-` (no auto cross-tenant). "El modelo propone, el motor dispone".
- **Validación en cluster (slice 6, 2026-07-01)**: chaos OOM sobre `da7aafb` → el auto **dispara** ("Rule 5 bypassed", comando bien sintetizado). Pero 3 corridas destaparon la **causa raíz REAL**: el motor sella el comando con `name/namespace/container/current/new` que **salen de `proposed_action` (LLM)**, y el 1.5b los **alucina** (target=namespace, container=pod, current=lista fabricada) → `NotFound`/`unparseable`. temp=0 mató la varianza de *valor*, pero el *target* es problema de **sourcing, no de inteligencia**.
- **Slice C (sourcing determinista) → ✅ cerrada por el Eje A (2026-07-02)**: en vez del strip-hash planeado, la identidad se resuelve por **ownerReferences** (determinista, con gate de existencia) y el valor/selector salen del snapshot; el LLM queda en *field + dirección*. Model-agnostic sobre el 1.5b actual (el experimento de modelo grande quedó **refutado**: qwen3.5:9b = 600s timeout en M4, nada con GPU en el cluster). Pendiente: re-validar con chaos OOM sobre `cb2d1db`.

### F4 — Bucle de aprendizaje RAG (detalle)

**Análisis 2026-07-03** (base: `rag.py` + `docs/10`, p@1 73.3% / p@3 86.7% N=15, misses = confusiones entre clases próximas; harness `eval_retrieval.py` ya existe → cada mejora es medible en minutos). Orden recomendado:

1. **R1 — Retrieval guiado por metadata** (barato, el que más mueve p@1): `retrieve_context` YA acepta `metadata_filter` pero producción no lo pasa. Mapear alertname→`error_class` y hacer two-stage (query filtrada por clase + fallback sin filtro para clases desconocidas). Determinista donde se pueda, semántico como fallback — coherente con v2. Esperable p@1≈100% en clases conocidas.
2. **R3 — Higiene de query** (micro): `build_rag_query` mete nombres de pod con hash (`frontend-8d7c4b-xnq2p`) — ruido de embedding que empuja a vecinos equivocados en descripciones pobres. Strip + medir.
3. **R2 — Cerrar el bucle (el F4 real)**: hoy `ingest_incident` archiva el incidente EN el diagnóstico con `outcome=action` — antes de saber si curó. Un auto_remediate que luego hizo rollback queda como precedente "bueno" que el retrieval citará. Fix: re-upsert del mismo doc_id tras `evaluate_rollback` con outcome final (cured/rolled_back/approved) + exponer/filtrar ese outcome en retrieval. LA feature narrable del chapter ("aprende de sus incidentes, incluidos los fallidos"). Incluye el gate de calidad en ingesta (contaminación E4).
4. **R4 — Métrica feedback-loop gain**: re-correr eval_retrieval con incidentes poblados vs vacío — gráfica para el deck si R2 entra.

**Descartado/aplazado**: fine-tuning de embeddings / modelo embed mayor / reranker (coste >> valor con 16 runbooks; R1 llega antes por vía determinista); threshold de similarity (inútil con distancias comprimidas 0.22-0.30, medido en docs/10).

---

## Changelog — fases completadas

> Resumen condensado. El detalle granular vive en `docs_sesion/` y en los docs por componente (01-06, 11, 12).

- **Fase 0 (Legado)** — Agente FastAPI modular + Ollama en K8s + generador Terraform (`generate_tf.py`). 64 tests, Cloud Build con gate. (En desuso activo, archivos conservados.)
- **Fase 1 (Observabilidad + ChatOps)** — `POST /webhook/alert` + schemas Alertmanager; Prometheus + kube-state-metrics + Alertmanager standalone (sin kube-prometheus-stack por permisos IAM); Mattermost + PostgreSQL; Grafana stateless; NetworkPolicies cross-namespace. KSM mirroreado a AR con `crane` (sin Cloud NAT). *Pendiente formal: webhook entrante Mattermost (no bloqueante).*
- **Fase 2 (RAG)** — 2026-04-23. `rag.py` + `diagnosis.py`; ChromaDB StatefulSet (imagen 0.6.3); `nomic-embed-text` en Ollama; 16 runbooks ingestados; pipeline `_process_alert_with_diagnosis()` con triple fail-open.
- **Fase 3 (Remediación + Feedback)** — 2026-05-12. `remediation.py` (validation layer + 9 reglas + executor dual-mode); RBAC least-privilege; botones Mattermost + HMAC-SHA256; feedback loop (incidents en ChromaDB); excepción tutor regla 4.5 + `DRY_RUN=false` (2026-05-25); evaluación inicial (`docs/10`): retrieval p@1=60%/p@3=80%, RAG safety 100% vs zero-shot 25%. Sesiones de calidad #1-#8 (43 findings).
- **Mini-Fase 4 (Production Readiness)** — 2026-05-27. Chaos engineering (4 experimentos, `scripts/chaos.sh`, métricas `aiops_chaos_*`, dashboard Grafana Chaos); slash command `/aiops`; rollback automático; hardening; **FASE 2** (Redis persistence, dedup in-flight, timeout). Chaos verificado: OOM 5.0/205.4s, CrashLoop 5.0/205.7s, BadImage 5.1/252.1s, CPU 10.1/206.7s. *Pendiente: Gate 8 (screenshots Grafana).*
- **F1 quick-wins de código (production-readiness)** — 2026-06-25/26. Análisis del pipeline → `docs/14` (matriz E1–E6 + 7 hallazgos como hipótesis a verificar en cluster). 4 quick-wins testeables con mocks: **PR-04** (`remediation.py` regla 7.5: `rag_degraded` baja AUTO_REMEDIATE→ESCALATE — nunca auto-remediar sin grounding RAG), **PR-06** (`main.py`: `aiops_diagnosis_total` separa `llm_timeout`/`llm_error` + nuevo `aiops_escalation_store_total{stored|redis_down}`), **PR-05** (`main.py`: reconexión lazy de ChromaDB en el `except` del retrieval — descarta cliente stale, reintenta una vez, persiste el sano en `app.state`; counter `rag_reconnect`), **PR-01** (`config.py`: default `http_timeout` 120→300). +7 tests.
- **F2 — Cola Redis Streams** — 2026-06-26/29. Nuevo módulo `agent/streams.py` (enqueue fail-closed + dedup SETNX, consume_loop in-process, reclaim_pending + dead-letter fail-soft). 4 slices: (1) camino de cola tras flag, (2) durabilidad reclaim+dead-letter, (3) `/readyz` Redis-gated + Redis 64→128Mi, (4) validación en cluster. Tras validar, **retirado el legacy** (flag `queue_enabled`, `IN_FLIGHT_ALERTS`, camino BackgroundTasks) → cola = camino único. Métricas `aiops_queue_*`. Cierra PR-07 (pérdida de alerta), PR-03 (dedup cluster-wide) y la decisión de PR-02 (readiness gated por Redis). Imagen `aiops-agent:5d5d7c7`. Suite 394→419 (+`test_streams.py`).
- **F3 — remediación CPU + auto real** — 2026-06-30/07-01. Arco de 9 sesiones. **(a) Dimensión CPU** (slices 1/1b/2): motor field-agnostic (`_LIMIT_FIELD_PARSERS`, `parse_cpu_to_millicores`, `_limit_resource`, regla 4.6 por-recurso, capture/revert derivan el recurso de `field`); flag `remediation_auto_cpu_enabled` (default off, escalate-first). Descartado HPA/scale (requiere metrics-server; bump vertical reusa el camino de memoria). **(b) Evals** (`eval_model_compare.py` + datasets highcpu/highmemory): refutaron "un modelo mayor lo arregla" — con el runbook inyectado el 1.5b acierta el field CPU 5/5; `qwen3.5` más lento/frágil y no aporta. Palanca = contexto, no tamaño. **(c) F4 context-enrichment**: anotaciones Prometheus `query()` en HighCPU/HighMemory llevan el límite actual; prompt anti-alucinación. Retrieval baseline p@1=73.3%/p@3=86.7% (HighCPU/HighMemory 5/5@1). **(d) Council + re-sourcing** (`f3-auto-engine-resourcing`, imagen `da7aafb`): causa raíz del "auto nunca dispara" = el motor ejecutaba los comandos investigativos, no la `proposed_action`. Fix: `is_structured_remediation` (fuente única) + `build_set_resources_command` + bypass regla 5 + guardrail namespace `arturo-`. 4 tests de seguridad invertidos (deliberado, "drop risk / keep conf"). **(e) Validación cluster (slice 6, 2026-07-01)**: el auto dispara, pero destapa la causa raíz REAL — target/valor sourcing desde el LLM (alucinado). temp=0 + enrich OOM staged; slice C (sourcing determinista) pendiente. Suite 422→**460** (`test_remediation.py` 137→175). Modo de fallo "auto limitado por el modelo" **superado** por el re-sourcing.
- **v2 Eje B — Confianza (production-credible)** — 2026-07-01. Tras una auditoría senior (5.9/10, 3 P0; informe modo-libro `docs_sesion/2026-07-01-auditoria-arquitectura.html`), se cierran los dos P0 de "confianza". **(a) P0·2 auth fail-closed**: `_verify_hmac_token` y el slash `/webhook/command` dejan de fail-open incondicional — el fail-open queda gated por `remediation_dry_run`, así con ejecución real un `WEBHOOK_SECRET`/`MM_COMMAND_TOKEN` vacío rechaza (401) en vez de permitir remediación no autenticada; startup loguea `error`; `k8s/secrets-setup.sh` reescrito para crear `mattermost-webhook`/`url` + `agent-secrets`/`webhook-secret`+`mm-command-token` (las claves que el deployment lee de verdad — antes creaba nombre/clave equivocados y nunca los tokens → HMAC desactivado en la práctica). **(b) P0·3 rollback durable**: nuevo `agent/rollback_store.py` (espejo de `escalation_store`) persiste el `RollbackContext` en Redis; `_schedule_rollback_evaluation` persiste, `_evaluate_rollback` duerme el tiempo restante hasta el deadline + borra de Redis en `finally`, y `_recover_rollbacks()` (lifespan) re-arma tras un reinicio a mitad de ventana. Suite 460→**474** (+3 P0·2 en `test_endpoints.py`, +11 P0·3 en `test_rollback.py`). Pendiente: build/imagen + validación en cluster; **Eje A (`enrichment.py`, P0·1)** es la siguiente sesión.
- **v2 Eje A — Grounding (P0·1, keystone)** — 2026-07-02, commits `b23dc74`+`cb2d1db`. Nuevo `agent/enrichment.py` (fail-soft, kill-switch `enrichment_enabled`): snapshot del pod (`IncidentSnapshot`: container/limits/phase/restart_count/last_state_reason, `_kubectl_json` argv sin shell que nunca raisea) + identidad por **ownerReferences** (pod→RS→Deployment, `get` final = gate de existencia) + `match_labels` del selector real. Tres consumidores del snapshot único (gather una vez, arriba): **prompt** ("CLUSTER FACTS", `format_cluster_facts`), **seal** (`seal_proposed_action`: sella identidad+`current_value`, propaga `match_labels` → mata el rollback falso del selector `app={name}` adivinado; anula la PA si el workload no se confirma), **confidence grounded** (`derive_confidence`/`ground_confidence` + gate `remediation_auto_min_restarts`; `model_confidence` preservada). RBAC ampliado a lectura de replicasets/sts/ds. Cierra la clase `NotFound`/`unparseable` del slice 6 y absorbe la slice C de F3. Suite 474→~505. Gotcha: `extra={"args":...}` en logging revienta (`args` reservado en LogRecord) — usar `cmd_args`.
- **v2 hardening post-review** — 2026-07-02, commit `cb2d1db` (build SUCCESS, RBAC aplicado). Review del diff `da7aafb..HEAD` → 6 piezas: **regla 4.7** (marker `target_unresolved` del seal escala ANTES de la regla 5 — sin ella, una PA anulada con confidence grounded 1.0 y comandos SAFE habría auto-ejecutado los describes del LLM reportando "remediado"; es exactamente la rama que dispara un RBAC sin aplicar), `proc.kill()` en `_kubectl_json` (espejo del executor, mata kubectl huérfanos con API server colgado), **síntesis `new_value=2×current`** si el del LLM es inusable (conservadora: valor sano se respeta, overshoot >2× sigue escalando — no corregir en silencio una regla acordada con el tutor), **paridad humano/auto** (`structured_command` sintetizado una vez; escalación guarda/enseña el determinista; approve captura snapshot + programa rollback reusando `incident_id`), métrica `aiops_enrichment_total`, grounding visible en Mattermost. Suite ~505→~535. **INCIDENTE secrets**: ejecutar `secrets-setup.sh` con placeholders pisó el webhook real (dijo `configured`, no `created`); recuperación vía `printenv` del pod vivo pre-reinicio; lección → guard anti-placeholder.
- **Review senior post-hardening + fixes Alta** — 2026-07-02/03. Review modo-libro (7.1/10, F-01..F-18, `docs_sesion/2026-07-02-paper-review-senior.html`). Cerrados los 4 Alta testeables con mocks + guard: **guard anti-placeholder** en `secrets-setup.sh` (aborta ante `<...>` sin sustituir); **F-05** compensación de dedup-key si el XADD falla (elegida sobre invertir el orden SETNX/XADD, que abriría ventana de duplicados en ráfagas); **F-02** gate estricto `workload_kind=="Deployment"` en el seal (STS/DS/None → 4.7; en el seal y no en `is_structured_remediation` porque el seal es el dueño del contrato "target confirmado" y su drop enruta con razón visible); **F-01** cooldown por workload (SETNX `aiops:cooldown:{ns}/{name}` TTL 600s en la rama auto estructurada, fail-closed a ESCALATE — gate fuera de `decide_action` para mantenerlo puro/sin I/O); **F-04** `mattermost_timeout=10s`. **CI GitHub Actions** (`.github/workflows/ci.yml`: tests espejo de Cloud Build + docker build sin push, en cada push/PR). Suite ~535→**565** (15 ficheros). Todo en working tree, commit pendiente de Jay.
- **F2 — pulido post-cierre** — 2026-06-29. Tres frentes tras validar/retirar legacy: (1) **`consume_loop` self-healing ante NOGROUP** — el `except` del `XREADGROUP` recrea el grupo con `id="$"` (no `id="0"`, que replayearía todo el historial retenido sin pasar por el dedup) + backoff `backoff_delay`; mata el busy-spin que era la **causa raíz** del HighCPU de Redis (no el bump de CPU, que queda como defensa en profundidad). `ensure_group(start_id="0")` parametrizado. (2) **Paneles Grafana `aiops_queue_*`** (fila Cola en el dashboard Overview: enqueued vs processed, depth, durabilidad reclaimed/dead) — Gate 8 "Paso F". (3) **Tenancy en cluster compartido** (apareció una compañera en el mismo GKE): 6 reglas + scrape acotados a `arturo-.*` (KSM `--namespaces`, cadvisor `metric_relabel keep`, `TargetDown` solo `kubernetes-endpoints`), panel scrape-targets a servicios propios, bump CPU Redis 50m→150m. Suite 419→422 (+3 tests `TestConsumeLoop`).

---

## Modos de fallo conocidos

| Componente | Fallo | Mitigación |
|---|---|---|
| Alert parsing | Labels inesperados/vacíos | Defaults seguros, log del payload raw |
| Embeddings | Error sin vecinos en ChromaDB | Threshold mínimo; sin match → LLM zero-shot |
| Retrieval | Docs irrelevantes | Filtrado por metadata, top-K conservador (3-5) |
| LLM | Hallucination / comandos incorrectos | Structured output + validation layer + whitelist; sin JSON → fallback |
| LLM | Comandos destructivos | Blacklist explícita; nunca auto-ejecutar sin validation layer |
| Auto-remediación | **Target/valor de la remediación alucinados** por el modelo pequeño: el comando se sellaba con `name/namespace/container/current/new` de `proposed_action` (LLM) y el 1.5b los inventa → `NotFound`/`unparseable` | ✅ **Resuelto (v2 Eje A, `cb2d1db`)**: enrichment sella identidad (ownerReferences + gate de existencia), `current_value` y selector desde el snapshot; temp=0 mata la varianza de valor; síntesis `2×current` si el valor LLM es inusable; target sin confirmar o kind≠Deployment (F-02) → regla 4.7 escala. El LLM queda en *field + dirección*. Pendiente re-validar chaos en cluster |
| Auto-remediación | **Patch-storm**: la alerta re-firing dentro de la ventana de rollout/rollback dispara un segundo 2× encima del primero | ✅ **Resuelto (F-01, working tree)**: cooldown por workload (SETNX TTL 600s) — un patch auto por workload por ventana; bloqueado/Redis dudoso → ESCALATE fail-closed con el comando determinista |
| Rollback | **Rollback falso**: health-check post-patch con selector adivinado `app={name}` → 0 pods → "no sanos" → revertía una remediación que funcionó | ✅ **Resuelto (v2 Eje A)**: selector derivado de `match_labels` del `.spec.selector` real (fallback `app={name}` solo sin enrichment) |
| Operación | **Scripts-plantilla con placeholders**: `secrets-setup.sh` ejecutado sin editar pisó el secret real del webhook | ✅ **Resuelto (working tree)**: guard `abort_if_placeholder` — cualquier `<...>` sin sustituir aborta ANTES de tocar kubectl. Recuperación documentada: `printenv` del pod vivo pre-reinicio + token en MM UI |
| Auto-patch | Fix aplicado pero alerta no cesa | Rollback automático (monitoriza salud del pod, revierte + escala) |
| Rollback | **Pod reinicia dentro de la ventana de espera** (rollout/OOM/spot) → dict en memoria + tarea `sleep` perdidos → patch aplicado que nunca se revierte | ✅ **Resuelto (v2 P0·3)**: `RollbackContext` persistido en Redis (`rollback_store.py`); `_recover_rollbacks()` re-arma al arranque y evalúa el tiempo restante. Coherente con la cola durable de F2 |
| Auth callbacks | Secret vacío → HMAC/token fail-open → remediación no autenticada con `DRY_RUN=false` | ✅ **Resuelto (v2 P0·2)**: fail-open gated por `remediation_dry_run` → con ejecución real un secret vacío rechaza (401); `secrets-setup.sh` alineado con las claves reales del deployment |
| ChromaDB | Pod evicted (spot) | StatefulSet + PVC; re-schedule automático |
| Ollama | Modelo no cargado tras restart | Readiness probe; el agente no procesa hasta Ollama ready |
| Agente | Reinicio mid-diagnóstico | ✅ **Resuelto (F2)**: el webhook encola en Redis Streams; un reinicio deja la entrada en el PEL → `reclaim_pending` la reprocesa (replay validado en cluster). At-least-once: posible reproceso duplicado, mitigado por dedup-key + dead-letter |
| Cola | Redis caído en la ingesta | Webhook **fail-closed** → 503; Alertmanager reintenta (no se pierde la alerta). `/readyz` da 503 → el pod sale de rotación |
| Cola | Poison message (payload no decodificable) | Tras `queue_max_deliveries` (3) entregas → dead-letter a `aiops:alerts:dead` con forense; no bloquea la cola |
| Cola | Redis recreado bajo un agente vivo (sin PVC) → el consumer group desaparece → `XREADGROUP failed: NOGROUP` | `consume_loop` **self-healing**: recrea el grupo con `id="$"` + backoff exponencial (mata el busy-spin que disparaba HighCPU). `$` se salta el gap (entradas durante el hueco), recuperable porque Alertmanager reenvía las firing (`repeat_interval`); evita el replay masivo permanente que daría `id=0` sobre un stream con historia |

---

## Backlog transversal (post-entrega)

- Caché in-memory (dict TTL) para embeddings + RAG por fingerprint `alertname+namespace+pod`.
- Re-ranking con cross-encoder si `incidents` supera ~500 docs.
- Clasificador supervisado si se acumulan >5k incidentes etiquetados.
- Buckets de histograma Prometheus custom para `/webhook/alert`.
- Workload Identity (SA K8s → IAM GCP); rol `logging.logWriter` para Cloud Build.
- Migración ChromaDB de Spot a Standard (coste vs estabilidad).
- Webhook entrante Mattermost (cierra Fase 1 formalmente).

### F5 — Predicción proactiva (stretch, scope abierto)
Pasar de reactivo a proactivo: loop que consulta Prometheus cada N min, detecta tendencias (memoria subiendo, CPU sostenida) y notifica ANTES de que salte la alerta. Módulo `prediction.py`, counter `aiops_prediction_total`, tests con mock. Decisiones abiertas: frecuencia de scrape, umbral de confianza, modelo de tendencia.
