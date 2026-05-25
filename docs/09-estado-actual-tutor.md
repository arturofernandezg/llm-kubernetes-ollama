# Estado Actual del Proyecto — Briefing para Reuniones

> Documento pensado para repasar antes de cada reunión con el tutor.
> Última actualización: 2026-05-23 (consolidación pre-reunión chapter)
> **Pendientes**: ver [docs/07-roadmap.md § TODO Consolidado](07-roadmap.md#todo-consolidado--siguientes-pasos) — fuente única de verdad.

---

## Qué es este proyecto (en una frase)

Un **agente AIOps** que recibe alertas de Kubernetes (vía Prometheus/Alertmanager), las diagnostica usando un LLM local + base de conocimiento (RAG), y notifica por chat (Mattermost) — con capacidad de **auto-remediar** fallos simples (ej. aumentar memoria a un pod que se queda sin RAM).

## Flujo completo (implementado y verificado E2E)

```
Prometheus detecta problema
    → Alertmanager envía webhook al agente
        → El agente busca runbooks relevantes en ChromaDB (RAG)
            → El LLM genera un diagnóstico estructurado (JSON)
                → Se validan los comandos sugeridos (9 reglas en cascada)
                    → Si es seguro y confiable → auto-remedia (kubectl)
                    → Si no → notifica en Mattermost con botones Aprobar/Rechazar
                        → Operador aprueba → ejecuta; rechaza → cancela
```

---

## Qué funciona HOY (demostrable en cluster)

### 1. Agente FastAPI desplegado en GKE
- Pod corriendo en `arturo-llm-test`, imagen `aiops-agent:1033c9f` (S4 — pendiente build + deploy S6)
- CI/CD: Cloud Build ejecuta ~310 tests antes de construir imagen (pendiente ejecución S6)
- Endpoints: `/webhook/alert`, `/webhook/action`, `/healthz`, `/readyz`, `/metrics`
- Scheduled en nodos `guaranteed=true` (NodeSelector + tolerations — nunca en spot)

### 2. Pipeline de diagnóstico (RAG + LLM)
- **16 runbooks** cargados como base de conocimiento (OOMKilled, CrashLoopBackOff, ImagePullBackOff, HighCPU, etc.)
- El agente recibe una alerta → busca runbooks similares en ChromaDB → construye prompt contextualizado → el LLM genera:
  ```json
  {
    "diagnosis": "Pod killed por OOM. Memory limit insuficiente.",
    "commands": ["kubectl describe pod ...", "kubectl patch deployment ..."],
    "confidence": 0.90,
    "risk": "high",
    "proposed_action": {"field": "resources.limits.memory", "current_value": "256Mi", "new_value": "512Mi"}
  }
  ```
- Triple fail-open: ChromaDB caída → sin contexto; LLM caído → alerta sin diagnóstico; todo falla → alerta raw

### 3. Motor de remediación autónoma (9 reglas en cascada)
Cada alerta pasa por las 9 reglas en este orden:

| Regla | Condición | Acción |
|---|---|---|
| 1 | Remediación deshabilitada (`REMEDIATION_ENABLED=false`) | suggest_only |
| 2 | No hay diagnosis | suggest_only |
| 3 | Comando BLOCKED detectado | escalate |
| 4 | No hay comandos ejecutables | suggest_only |
| 4.5 | Comando implica reinicio de pod (set resources, scale, rollout) | **escalate** (excepción aprobada por tutor ✅ 2026-05-23 — pendiente implementar) |
| 4.6 | new_memory > 2 × current_memory | **escalate** |
| 5 | risk=high o confidence<0.7 | escalate |
| 6 | risk=medium y confidence≥0.8 | escalate |
| 7 | risk=low y confidence≥0.8 | **auto_remediate** |
| 8 | Sin clasificar | suggest_only |

- `REMEDIATION_ENABLED=true`, `REMEDIATION_DRY_RUN=false` en manifest (`k8s/deployment-agent.yaml` línea 79). Flip implementado 2026-05-25 — pendiente deploy en cluster (Gate 2).
- **Regla 4.5 excepción**: implementada en `agent/remediation.py` (2026-05-25). Autoriza `kubectl set resources` sobre memoria con confidence ≥ 0.9 AND risk ≤ medium AND `proposed_action.field == resources.limits.memory`. Scale/rollout/patch siguen bloqueados. 10 tests nuevos. Con `risk=high` típico del LLM en OOM → sigue escalando por rule 5 (defensa en profundidad).

### 4. ChatOps (Mattermost) con botones interactivos
- Mattermost en cluster con PostgreSQL propio
- Escalaciones: mensaje con botones **✅ Aprobar / ❌ Rechazar** en Mattermost
- Callback HMAC-SHA256 (`WEBHOOK_SECRET`) → POST `/webhook/action` → ejecuta o cancela
- PENDING_ESCALATIONS dict en memoria del agente (TTL 60 min)
- Retry exponential backoff si Mattermost no responde

### 5. Observabilidad completa
- **Prometheus standalone** en `arturo-monitoring` — 5 reglas AIOps activas
- **Alertmanager** → webhook del agente (verificado E2E)
- **Grafana stateless** — dashboard "AIOps Agent — Overview" (9 paneles: counters, latencia p95, pod phases, targets UP, retries/extraction)
- **kube-state-metrics** — mirror a Artifact Registry (registry.k8s.io inaccesible sin Cloud NAT)

### 6. Tests
- **~310 tests** en 11 ficheros, todos pasando (<1s)
- Mocking completo: no necesitan cluster ni LLM
- CI/CD: Cloud Build ejecuta todos antes de construir imagen

---

## Stack tecnológico

| Componente | Tecnología |
|---|---|
| Agente | Python 3.11, FastAPI, httpx, Pydantic v2 |
| LLM | Ollama (qwen2.5:1.5b), in-cluster |
| Embeddings | nomic-embed-text (768 dims), via Ollama |
| Base vectorial (RAG) | ChromaDB StatefulSet + PVC |
| ChatOps | Mattermost + PostgreSQL |
| Alerting | Prometheus + Alertmanager (standalone) |
| Observabilidad | Grafana (stateless, provisioned) |
| Cluster | GKE europe-southwest1-a (e2-standard-2 spot + 2 nodos guaranteed) |
| CI/CD | Google Cloud Build |
| IaC | Manifiestos K8s en YAML |

---

## Infraestructura desplegada

```
Namespace arturo-llm-test:     agent ✅, ollama ✅, chromadb ✅ (16 runbooks ingestados)
Namespace arturo-monitoring:   prometheus ✅, kube-state-metrics ✅, alertmanager ✅, grafana ✅
Namespace arturo-mattermost:   mattermost ✅, postgres ✅
Namespace arturo-chaos:        manifests listos — experimentos chaos pendientes de ejecutar (requiere sesión E2E)
```

---

## Fases del proyecto

Ver tabla completa en [docs/07-roadmap.md § Estado de las fases](07-roadmap.md#estado-de-las-fases).

**Resumen:** Fases 0-3 completas. Mini-Fase 4 (Production Readiness) en ejecución — código S1-S6 listo (2026-05-19), sesión de pruebas E2E pendiente.

---

## Mini-Fase 4 — Production Readiness (código S1-S6 listo, E2E pendiente)

Objetivo: pasar de "capacidad demostrada" a **evidencia medida** para defensa.

**Estado:** 6/6 sesiones con código y artefactos entregados (2026-05-19). La sesión de pruebas E2E (pytest → Cloud Build → deploy → smoke → chaos → MTTD/MTTR → Grafana → backup) está pendiente de ejecutar en cluster.

Ver tabla de sesiones y secuencia completa de gates en [docs/07-roadmap.md § TODO Consolidado](07-roadmap.md#todo-consolidado--siguientes-pasos).

---

## Decisiones de diseño importantes (para defender en la memoria)

1. **Ollama in-cluster** (no API externa): datos no salen del cluster, sin costes por token, control total. Trade-off: modelos más pequeños.
2. **Retrieval-first** (no classification-first): con pocos datos, buscar por similitud semántica funciona mejor que clasificar primero.
3. **Fail-open everywhere**: si cualquier componente falla, el pipeline degrada pero nunca pierde la alerta.
4. **Validation layer obligatoria**: el LLM NUNCA ejecuta comandos directamente. Siempre pasan por 9 reglas + clasificación regex (BLOCKED > SAFE > MUTATING).
5. **Config-gated remediation**: desactivada por defecto (`REMEDIATION_ENABLED`), ejecución dry-run por defecto (`DRY_RUN`). Dos capas de seguridad independientes.
6. **HMAC-SHA256 en callbacks**: los botones de Mattermost incluyen token HMAC para verificar que el callback es legítimo y no fue manipulado.

---

## Restricciones del entorno

- Sin permisos de ClusterRole de escritura (no se puede instalar kube-prometheus-stack completo)
- Sin Cloud NAT (pods sin internet, modelos se cargan manualmente con crane + kubectl cp)
- Nodos spot (mitigado con PDB + PVC + nodeSelector guaranteed para workloads críticos)
- Cluster compartido con otra alumna — siempre `-n <namespace>` explícito

---

*Pendientes: ver [docs/07-roadmap.md § TODO Consolidado](07-roadmap.md#todo-consolidado--siguientes-pasos). Actualizar este doc tras ejecutar la sesión de pruebas E2E (imagen deployada, MTTD/MTTR reales, DRY_RUN flip confirmado).*
