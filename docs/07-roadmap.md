# Roadmap y Estado — AIOps Infrastructure Agent

> **Fuente única de planificación y estado del proyecto.**
> Última actualización: 2026-06-26.
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
- **Agente FastAPI** en `arturo-llm-test`, imagen `aiops-agent:fd37a5d`. Endpoints: `/webhook/alert`, `/webhook/action`, `/webhook/command`, `/healthz`, `/readyz`, `/metrics`. `REMEDIATION_DRY_RUN=false`, rollback automático activo. Fijado a nodos `guaranteed=true`.
- **Pipeline RAG+LLM**: 16 runbooks en ChromaDB; triple fail-open (ChromaDB / Ollama / ambos).
- **Motor de remediación**: 9 reglas en cascada, incl. excepción de tutor (regla 4.5 set-resources memoria, conf≥0.9/risk≤medium) y regla 4.6 (memoria >2×). Clasificación regex BLOCKED > SAFE > MUTATING.
- **ChatOps**: Mattermost + botones HMAC-SHA256; escalaciones persistidas en **Redis** (`escalation_store.py`, fail-open); slash command `/aiops`.
- **Resiliencia (FASE 2)**: dedup in-flight (`IN_FLIGHT_ALERTS` + `aiops_dedup_skipped_total`), mensaje diferenciado por timeout del LLM.
- **Observabilidad**: Prometheus + kube-state-metrics + Alertmanager + Grafana (2 dashboards: Overview + Chaos). 6 reglas de alerta. Métricas de diagnóstico granulares: `aiops_diagnosis_total{outcome}` (incl. `rag_reconnect`/`llm_timeout`/`llm_error`) + `aiops_escalation_store_total{stored|redis_down}` (PR-06).
- **Tests**: 394 funciones en 13 ficheros, mockeados; Cloud Build los corre como gate.

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
| **F1** — Validación en cluster | Entender y validar el sistema desplegado; informe de production-readiness | 🔵 En curso — informe `docs/14` + 4 quick-wins de código hechos (PR-01/04/05/06); falta validación en cluster (matriz E1–E6 + PR-03) |
| **F2** — Cola Redis Streams | Desacoplar la ingesta de alertas del LLM lento (cuello de botella) | Pendiente |
| **F3** — HPA / remediación CPU | Cerrar el hueco HighCPU → cobertura E2E de los 4 modos | Pendiente |
| **F4** — Bucle de aprendizaje RAG | Gate de calidad en ingesta + retrieval que mejora con el histórico | Pendiente |
| **F5** — Predicción proactiva | (stretch) Forecast de tendencia → acción preventiva | Backlog |
| **F6** — Presentación | Pulir `demo/demo.html` + Gate 8 (screenshots Grafana) + ensayo demo | Días antes (julio) |

### F1 — Validación en cluster (detalle)
- **Hecho (código, sin cluster)**: análisis del pipeline real → informe `docs/14` (matriz E1–E6 + 7 hallazgos PR-01..07). 4 quick-wins cerrados con tests mockeados: PR-04 (escalate sin grounding RAG), PR-06 (observabilidad: split `llm_timeout`/`llm_error` + counter de escalación), PR-05 (reconexión lazy de ChromaDB + `rag_reconnect`), PR-01 (alinear default `http_timeout`). Quedan para cluster: PR-02 (decisión readiness, →F2), PR-03 (dedup per-pod, →F2), PR-07 (pérdida de alerta, →F2).
- Trazar el pipeline E2E completo hasta poder narrarlo de memoria.
- **Chaos sobre dependencias propias**: matar Redis / Ollama / ChromaDB en mitad de un diagnóstico → demostrar fail-open.
- **Test de concurrencia**: N alertas simultáneas → ver el dedup de FASE 2 en acción.
- Salida: **informe honesto de production-readiness** (qué aguanta, qué no, qué falta) + guion de demo.

### F2 — Cola Redis Streams (detalle)
- Reutilizar el Redis ya desplegado (`k8s/redis.yaml`). El webhook encola; worker(s) consumen y corren el pipeline.
- Desacopla la ingesta del LLM lento (~205-252s): at-least-once + replay si el agente reinicia mid-diagnóstico.
- Demo: absorber una ráfaga de alertas sin perder ninguna.

### F3 — HPA / remediación CPU (detalle)
- Nueva acción de remediación (scale réplicas / ajuste HPA) para presión de CPU.
- Hoy HighCPU acaba en `confidence=0 / suggest_only` — es el hueco visible. Requiere `metrics-server` (confirmar en GKE).

### F4 — Bucle de aprendizaje RAG (detalle)
- Gate de calidad en la ingesta de incidentes (evita contaminación tipo finding E4).
- Demostrar que el retrieval mejora con incidentes acumulados.

---

## Changelog — fases completadas

> Resumen condensado. El detalle granular vive en `docs_sesion/` y en los docs por componente (01-06, 11, 12).

- **Fase 0 (Legado)** — Agente FastAPI modular + Ollama en K8s + generador Terraform (`generate_tf.py`). 64 tests, Cloud Build con gate. (En desuso activo, archivos conservados.)
- **Fase 1 (Observabilidad + ChatOps)** — `POST /webhook/alert` + schemas Alertmanager; Prometheus + kube-state-metrics + Alertmanager standalone (sin kube-prometheus-stack por permisos IAM); Mattermost + PostgreSQL; Grafana stateless; NetworkPolicies cross-namespace. KSM mirroreado a AR con `crane` (sin Cloud NAT). *Pendiente formal: webhook entrante Mattermost (no bloqueante).*
- **Fase 2 (RAG)** — 2026-04-23. `rag.py` + `diagnosis.py`; ChromaDB StatefulSet (imagen 0.6.3); `nomic-embed-text` en Ollama; 16 runbooks ingestados; pipeline `_process_alert_with_diagnosis()` con triple fail-open.
- **Fase 3 (Remediación + Feedback)** — 2026-05-12. `remediation.py` (validation layer + 9 reglas + executor dual-mode); RBAC least-privilege; botones Mattermost + HMAC-SHA256; feedback loop (incidents en ChromaDB); excepción tutor regla 4.5 + `DRY_RUN=false` (2026-05-25); evaluación inicial (`docs/10`): retrieval p@1=60%/p@3=80%, RAG safety 100% vs zero-shot 25%. Sesiones de calidad #1-#8 (43 findings).
- **Mini-Fase 4 (Production Readiness)** — 2026-05-27. Chaos engineering (4 experimentos, `scripts/chaos.sh`, métricas `aiops_chaos_*`, dashboard Grafana Chaos); slash command `/aiops`; rollback automático; hardening; **FASE 2** (Redis persistence, dedup in-flight, timeout). Chaos verificado: OOM 5.0/205.4s, CrashLoop 5.0/205.7s, BadImage 5.1/252.1s, CPU 10.1/206.7s. *Pendiente: Gate 8 (screenshots Grafana).*
- **F1 quick-wins de código (production-readiness)** — 2026-06-25/26. Análisis del pipeline → `docs/14` (matriz E1–E6 + 7 hallazgos como hipótesis a verificar en cluster). 4 quick-wins testeables con mocks: **PR-04** (`remediation.py` regla 7.5: `rag_degraded` baja AUTO_REMEDIATE→ESCALATE — nunca auto-remediar sin grounding RAG), **PR-06** (`main.py`: `aiops_diagnosis_total` separa `llm_timeout`/`llm_error` + nuevo `aiops_escalation_store_total{stored|redis_down}`), **PR-05** (`main.py`: reconexión lazy de ChromaDB en el `except` del retrieval — descarta cliente stale, reintenta una vez, persiste el sano en `app.state`; counter `rag_reconnect`), **PR-01** (`config.py`: default `http_timeout` 120→300). +7 tests. Cambios en código, aún no horneados en imagen.

---

## Modos de fallo conocidos

| Componente | Fallo | Mitigación |
|---|---|---|
| Alert parsing | Labels inesperados/vacíos | Defaults seguros, log del payload raw |
| Embeddings | Error sin vecinos en ChromaDB | Threshold mínimo; sin match → LLM zero-shot |
| Retrieval | Docs irrelevantes | Filtrado por metadata, top-K conservador (3-5) |
| LLM | Hallucination / comandos incorrectos | Structured output + validation layer + whitelist; sin JSON → fallback |
| LLM | Comandos destructivos | Blacklist explícita; nunca auto-ejecutar sin validation layer |
| Auto-patch | Fix aplicado pero alerta no cesa | Rollback automático (monitoriza salud del pod, revierte + escala) |
| ChromaDB | Pod evicted (spot) | StatefulSet + PVC; re-schedule automático |
| Ollama | Modelo no cargado tras restart | Readiness probe; el agente no procesa hasta Ollama ready |
| Agente | Reinicio mid-diagnóstico | Webhook HTTP fire-and-forget → **se pierde la alerta** (lo resuelve F2: cola Redis Streams) |

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
