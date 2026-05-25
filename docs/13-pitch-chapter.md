# AIOps Infrastructure Agent — Pitch para reunión con chapter

> Audiencia: chapter principal MasOrange/Telecable (reunión 2026-05-25).
> Lectura estimada: 5 min. No es la memoria de defensa — es el resumen ejecutivo para una conversación.

---

## TL;DR

Sistema que **detecta fallos en Kubernetes, los diagnostica con un LLM local y actúa automáticamente** — sin depender de APIs externas, sin exponer datos del cluster, sin coste por token. Un operador recibe en Mattermost el diagnóstico con los comandos sugeridos y los botones Aprobar/Rechazar. Si la incidencia es simple y de bajo riesgo, el sistema actúa solo.

**Problema que resuelve**: en un cluster en producción, el tiempo entre "alerta disparada" y "operador enterado + fix aplicado" puede ser de minutos o horas según la guardia. Este sistema reduce ese tiempo a segundos para los casos comunes (OOMKilled, CrashLoopBackOff, ImagePullBackOff).

---

## Arquitectura

```
Prometheus (métricas)
    │
    ▼
Alertmanager (routing)
    │  POST /webhook/alert
    ▼
FastAPI Agent  ──► ChromaDB (RAG — 16 runbooks + histórico incidentes)
    │               │
    │◄──────────────┘ contexto relevante
    │
    ▼
Ollama (qwen2.5:1.5b, in-cluster)
    │  JSON estructurado: diagnosis + commands + confidence + risk
    ▼
Validation Layer (9 reglas en cascada)
    │
    ├── risk=low, confidence≥0.8 ──► kubectl auto-patch + rollback automático
    │
    └── risk=high / baja confianza ──► Mattermost (botones ✅ Aprobar / ❌ Rechazar)
                                            │
                                            ▼
                                       K8s API (si aprobado)
                                       ChromaDB (feedback loop)
```

**Todo in-cluster**: LLM, vector store, ChatOps. Los datos no salen del cluster.

---

## Stack

| Capa | Tecnología |
|---|---|
| Agente | Python 3.11, FastAPI, httpx, Pydantic v2 |
| LLM | Ollama + qwen2.5:1.5b (in-cluster, 1.5B params) |
| RAG | ChromaDB StatefulSet + nomic-embed-text (768 dims) |
| ChatOps | Mattermost + PostgreSQL, botones interactivos, HMAC-SHA256 |
| Observabilidad | Prometheus standalone + Alertmanager + Grafana (2 dashboards provisionados) |
| Cluster | GKE europe-southwest1-a, e2-standard-2 spot + nodos guaranteed |
| CI/CD | Google Cloud Build (tests gate + build + push) |

---

## Lo que funciona hoy (verificado E2E en cluster)

1. **Pipeline completo**: KubePodOOMKilled → alerta Prometheus → Alertmanager → webhook → RAG (3 runbooks relevantes) → LLM (diagnóstico en ~78s) → Mattermost con botones Aprobar/Rechazar → callback HMAC verificado → ChromaDB (feedback).
2. **Motor de remediación con 9 reglas**: validation layer en cascada — bloquea comandos destructivos, escala si riesgo alto, auto-parchea si riesgo bajo + alta confianza.
3. **Rollback automático**: si un auto-patch no resuelve la incidencia en N segundos → revierte el patch y escala. Captura el estado pre-patch antes de actuar.
4. **Observabilidad propia**: métricas `aiops_*` custom (counters + histograma latencia) + 2 dashboards Grafana (Overview + Chaos Engineering con MTTD/MTTR).
5. **~310 tests** en 11 ficheros, CI/CD con Cloud Build como gate — sin test no hay imagen.

---

## Decisiones técnicas que vale la pena contar

| Decisión | Alternativa descartada | Por qué |
|---|---|---|
| Ollama in-cluster (LLM local) | API externa (OpenAI, Vertex AI) | Sin costes por token, sin latencia de red externa, datos no salen del cluster — restricción del entorno TFG |
| Retrieval-first RAG (ChromaDB) | Clasificador supervisado | Con pocos datos de entrenamiento, búsqueda semántica supera a clasificación. El clasificador necesita >5k incidentes etiquetados |
| Fail-open everywhere | Fail-closed | Una alerta perdida por caída de ChromaDB es peor que un diagnóstico sin contexto RAG. El pipeline degrada, nunca bloquea |
| Validation layer obligatorio (9 reglas) | LLM decide directamente | El LLM nunca ejecuta comandos sin pasar por la capa de validación. Dos capas independientes: whitelist regex + motor de decisión |
| HMAC-SHA256 en callbacks | Sin autenticación | Los botones de Mattermost generan POST al agente — cualquiera con la URL podría aprobar una remediación. HMAC verifica origen legítimo |
| Rollback automático con captura pre-patch | Rollback manual | El estado del campo antes del patch (ej. `256Mi`) se captura antes de actuar. Si la remediación falla el health check, se revierte determinísticamente |

---

## Estado del proyecto y próximos pasos

**Completado:**
- Fases 0–3 (Observabilidad + RAG + Remediación Autónoma + ChatOps) — E2E verificado en cluster
- Mini-Fase 4 código: Chaos Engineering (4 experimentos), dashboard MTTD/MTTR, slash command `/aiops`, rollback automático, hardening pre-prod

**Pendiente inmediato (sesión de pruebas E2E):**
- Pytest gate + Cloud Build + deploy imagen `:3fde9f8`
- 4 chaos experiments en cluster → MTTD/MTTR reales (la métrica diferencial del TFG)
- Screenshots Grafana, backup ChromaDB

**Próxima fase (Fase 5 — Predicción proactiva):**
- Loop periódico consultando métricas Prometheus → detección de tendencias preocupantes antes de que salte la alerta
- [scope abierto — a definir tras cierre Mini-Fase 4]

Ver detalle completo: [`docs/07-roadmap.md § TODO Consolidado`](07-roadmap.md#todo-consolidado--siguientes-pasos)

---

## Mi perfil técnico en este proyecto

> *Rellena esta sección con lo que más quieras destacar en la reunión.*

**He construido:**
- [ ] _ej. el motor de remediación completo (validation layer, 9 reglas, executor, rollback)_
- [ ] _ej. la integración RAG + LLM end-to-end con fail-open_
- [ ] _ej. el sistema de chaos engineering para medir MTTD/MTTR_

**He aprendido:**
- [ ] _ej. operaciones en GKE con restricciones reales (sin Cloud NAT, sin ClusterRoles de escritura, sin API externa)_
- [ ] _ej. integración LLM + retrieval semántico en producción_

**Me interesa profundizar en:**
- [ ] _ej. SRE / Platform Engineering (observabilidad, resiliencia, automatización de operaciones)_
- [ ] _ej. MLOps / AIOps (pipelines de modelos en producción, feedback loops, evaluación offline)_
- [ ] _ej. Backend distribuido (FastAPI, sistemas reactivos, K8s)_

---

## Tipo de equipo que me interesa incorporar

> *Rellena con tus preferencias reales antes de la reunión.*

- [ ] _ej. Equipo de Platform / SRE — trabajo en infraestructura, fiabilidad, automatización_
- [x ] _ej. Equipo de Data / AI — MLOps, modelos en producción, observabilidad de sistemas IA_
- [ ] _ej. Equipo de Backend — APIs distribuidas, sistemas event-driven_

**Lo que NO quiero:** _ej. trabajo puramente de mantenimiento sin margen de diseño / equipos sin cultura de testing_

---

*Este doc es el resumen ejecutivo para la reunión. Para profundidad técnica: `docs/01-architecture.md`. Para defensa TFG: `docs/defensa.md`.*
