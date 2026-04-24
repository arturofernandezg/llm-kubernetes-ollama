# Estado Actual del Proyecto — Briefing para Reuniones

> Documento pensado para repasar antes de cada reunión con el tutor.
> Última actualización: 2026-04-23 (Fase 1 + Fase 2 cerradas)

---

## Qué es este proyecto (en una frase)

Un **agente AIOps** que recibe alertas de Kubernetes (vía Prometheus/Alertmanager), las diagnostica usando un LLM local + base de conocimiento (RAG), y notifica por chat (Mattermost) — con capacidad de **auto-remediar** fallos simples (ej. aumentar memoria a un pod que se queda sin RAM).

## Flujo completo (objetivo final)

```
Prometheus detecta problema
    → Alertmanager envía webhook al agente
        → El agente busca runbooks relevantes en ChromaDB (RAG)
            → El LLM genera un diagnóstico estructurado (JSON)
                → Se validan los comandos sugeridos (whitelist/blacklist)
                    → Si es seguro y confiable → auto-remedia (kubectl)
                    → Si no → notifica en Mattermost para aprobación humana
```

## Qué funciona HOY (lo que se puede demostrar)

### 1. Agente FastAPI desplegado en GKE
- Pod corriendo en el cluster GKE de MasOrange (`arturo-llm-test`)
- Endpoints operativos: webhook de alertas, health probes, métricas Prometheus
- Imagen Docker buildeada automáticamente con Cloud Build (CI/CD)

### 2. Pipeline de diagnóstico (RAG + LLM)
- **16 runbooks** cargados como base de conocimiento (OOMKilled, CrashLoopBackOff, ImagePullBackOff, HighCPU, etc.)
- El agente recibe una alerta → busca runbooks similares en ChromaDB → construye un prompt contextualizado → el LLM genera:
  ```json
  {
    "diagnosis": "Pod killed por OOM. Memory limit 256Mi insuficiente.",
    "commands": ["kubectl describe pod ...", "kubectl patch deployment ..."],
    "confidence": 0.82,
    "risk": "low"
  }
  ```
- Triple fail-open: si ChromaDB cae → funciona sin contexto; si el LLM cae → notifica la alerta sin diagnóstico; si todo falla → envía alerta raw

### 3. Capa de seguridad para comandos (validation layer)
- Cada comando sugerido por el LLM se clasifica automáticamente:
  - **SAFE** (solo lectura): `kubectl describe`, `kubectl get`, `kubectl logs`
  - **MUTATING** (permitido): `kubectl scale`, `kubectl patch`, `kubectl set resources`
  - **BLOCKED** (peligroso): `kubectl delete namespace`, `kubectl drain`, `rm -rf`
- Motor de decisión con 9 reglas en cascada (2026-04-25 — condición del tutor):
  - Regla 4.5: comando MUTATING implica reinicio de pod → **ESCALATE** (bloqueo hasta confirmación tutor)
  - Regla 4.6: `proposed_action` en `resources.limits.memory` con `new > 2 × current` → **ESCALATE**
  - `risk=low` + `confidence>=80%` + comandos validados (sin restart) → **auto-remedia**
  - Comando BLOCKED/peligroso detectado → **escala a humano**
  - Baja confianza o riesgo alto → **solo sugiere**
  - `REMEDIATION_DRY_RUN=true` activo — ejecución real requiere acuerdo con tutor
- Desactivado por defecto (`REMEDIATION_ENABLED=false`). Se activa con variable de entorno.

### 4. ChatOps (Mattermost)
- Mattermost desplegado en el cluster con PostgreSQL propio
- El agente envía notificaciones formateadas con diagnóstico, comandos sugeridos y nivel de riesgo
- Retry con exponential backoff si Mattermost no responde

### 5. Observabilidad completa (Fase 1 cerrada 2026-04-22)
- **Prometheus standalone** en `arturo-monitoring` + kube-state-metrics (mirror a Artifact Registry vía `crane`, `registry.k8s.io` inaccesible sin Cloud NAT). 5 reglas de alerta AIOps activas (OOMKilled, CrashLoop, HighCPU, HighMemory, TargetDown).
- **Alertmanager standalone** → webhook del agente.
- **Grafana stateless + provisioned** (dashboard "AIOps Agent — Overview": 9 paneles, 4 filas) + contact point webhook hacia el agente. Persistencia `emptyDir` (patrón enterprise: provisioning-as-code, no drift local).
- E2E verificado en cluster: alerta real → webhook → RAG → LLM → Mattermost.

### 6. Tests
- **196 tests** unitarios, todos pasando (<0.5s)
- Mocking completo: no necesitan cluster ni LLM para ejecutarse
- CI/CD: Cloud Build ejecuta todos los tests antes de construir la imagen

## Stack tecnológico

| Componente | Tecnología |
|---|---|
| Agente | Python 3.11, FastAPI, httpx |
| LLM | Ollama (qwen2.5:1.5b), in-cluster |
| Embeddings | nomic-embed-text (768 dims), via Ollama |
| Base vectorial (RAG) | ChromaDB |
| ChatOps | Mattermost |
| Alerting | Alertmanager (standalone) |
| Cluster | GKE (e2-standard-2 spot, 2 nodos) |
| CI/CD | Google Cloud Build |
| IaC | Manifiestos K8s en YAML |

## Infraestructura desplegada

```
Namespace arturo-llm-test:     agent ✅, ollama ✅, chromadb ✅ (16 runbooks ingestados)
Namespace arturo-monitoring:   prometheus ✅, kube-state-metrics ✅, alertmanager ✅, grafana ✅
Namespace arturo-mattermost:   mattermost ✅, postgres ✅
```

## Fases del proyecto

| Fase | Qué hace | Estado |
|---|---|---|
| **Fase 0** (Legado) | Extracción de params + generación Terraform | Completa (en desuso) |
| **Fase 1** (Observabilidad) | Prometheus + Alertmanager + Grafana + webhook + Mattermost | **Completa** (2026-04-22). E2E verificado |
| **Fase 2** (RAG) | ChromaDB + runbooks + diagnóstico contextual con LLM | **Completa** (2026-04-23). 16 runbooks ingestados vía K8s Job idempotente; E2E verificado (KubePodOOMKilled → RAG 3 runbooks + 2 incidents → LLM 187s, confidence=0.85, risk=high → Mattermost enriquecido) |
| **Fase 3** (Remediación) | Activar validation layer + auto-patch + feedback loop en cluster | Código listo y testeado. Pendiente: activar `REMEDIATION_ENABLED=true, REMEDIATION_DRY_RUN=true` y aplicar `k8s/rbac.yaml` (Role namespace-scoped, no bloqueado por IAM) |

## Qué falta por hacer (próximos pasos)

### Inmediato (próxima sesión — activar Fase 3 dry-run)
1. **Aplicar RBAC** — `kubectl apply -f k8s/rbac.yaml` (Role + RoleBinding namespace-scoped; `container.roles.create` IAM ya no es bloqueante al ser namespace y no ClusterRole).
2. **Activar remediation flags** — añadir `REMEDIATION_ENABLED=true`, `REMEDIATION_DRY_RUN=true` al env del `deployment-agent.yaml`.
3. **Disparar alerta real de OOMKilled** — verificar decisión en logs (`auto_remediate` si `risk=low` + `confidence≥0.8`, `suggest_only` si no) y que Mattermost muestra el bloque de remediación dry-run.

### Corto plazo
4. **Webhook entrante de Mattermost interactivo** — botones Aprobar/Rechazar para remediaciones que requieran humano (Fase 3 iteración 2).
5. **Modo proactivo** — loop periódico que consulta `prometheus-svc:9090/api/v1/query` y detecta tendencias antes de que dispare una alerta.

### Para la memoria/tesis
6. **Métricas de evaluación**: MTTR, retrieval precision, actionability rate, safety rate.
7. **Evaluación experimental**: comparar diagnóstico zero-shot (ChromaDB vacío) vs. diagnóstico RAG con los 16 runbooks. Mostrar mejora en `confidence` y en calidad de `commands[]`.

## Decisiones de diseño importantes (para defender en la memoria)

1. **Ollama in-cluster** (no API externa): datos no salen del cluster, sin costes por token, control total. Trade-off: modelos más pequeños.
2. **Retrieval-first** (no classification-first): con pocos datos, buscar por similitud semántica funciona mejor que clasificar primero.
3. **Fail-open everywhere**: si cualquier componente falla, el pipeline degrada pero nunca pierde la alerta.
4. **Validation layer obligatoria**: el LLM NUNCA ejecuta comandos directamente. Siempre pasan por clasificación regex (BLOCKED > SAFE > MUTATING > UNKNOWN).
5. **Config-gated remediation**: desactivada por defecto, opt-in explícito. Seguro para desplegar sin riesgo.

## Restricciones del entorno

- Sin permisos de ClusterRole (no se puede instalar kube-prometheus-stack)
- Sin Cloud NAT (pods sin internet, modelos se cargan manualmente)
- Nodos spot (pods pueden ser desalojados, mitigado con PDB + PVC)

---

*Actualizar este archivo al final de cada sesión de trabajo.*
