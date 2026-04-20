# Estado Actual del Proyecto — Briefing para Reuniones

> Documento pensado para repasar antes de cada reunión con el tutor.
> Última actualización: 2026-04-09 (post S7)

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
- Motor de decisión con 7 reglas en cascada:
  - `risk=low` + `confidence>=80%` + comandos validados → **auto-remedia**
  - Comando peligroso detectado → **escala a humano**
  - Baja confianza o riesgo alto → **solo sugiere**
- Desactivado por defecto (`REMEDIATION_ENABLED=false`). Se activa con variable de entorno.

### 4. ChatOps (Mattermost)
- Mattermost desplegado en el cluster con PostgreSQL propio
- El agente envía notificaciones formateadas con diagnóstico, comandos sugeridos y nivel de riesgo
- Retry con exponential backoff si Mattermost no responde

### 5. Tests
- **193 tests** unitarios, todos pasando (<0.5s)
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
Namespace arturo-llm-test:     agent ✅, ollama ✅, chromadb ✅
Namespace arturo-monitoring:   alertmanager ✅
Namespace arturo-mattermost:   mattermost ✅, postgres ✅
```

## Fases del proyecto

| Fase | Qué hace | Estado |
|---|---|---|
| **Fase 0** (Legado) | Extracción de params + generación Terraform | Completa (en desuso) |
| **Fase 1** (Observabilidad) | Webhook de alertas + Mattermost + routing | ~90% (faltan alerting rules, webhook entrante Mattermost) |
| **Fase 2** (RAG) | ChromaDB + runbooks + diagnóstico con LLM | Módulos escritos y testeados. ChromaDB running. Pendiente: runbooks semilla + integrar RAG en webhook |
| **Fase 3** (Remediación) | Validation layer + auto-patch + feedback loop | ~90% (validación + integración + ejecución real + RBAC + feedback loop hechos; falta cluster deploy + e2e) |

## Qué falta por hacer (próximos pasos)

### Inmediato (próxima sesión — cluster)
1. ~~**Aplicar fix de ChromaDB**~~ ✅ (2026-04-09, ConfigMap log stdout-only)
2. **Aplicar RBAC** — (`kubectl apply -f k8s/rbac.yaml`) — manifiesto ya preparado
3. **Build + deploy** — nueva imagen con remediación + feedback loop integrados, deploy en cluster
4. **Alerting rules** — definir qué alertas disparan el sistema (consultar tutor sobre permisos Prometheus)

### Corto plazo
5. **Test end-to-end** con payload real de Alertmanager → diagnóstico → remediación → Mattermost

### Para la memoria/tesis
6. **Métricas de evaluación**: MTTR, retrieval precision, actionability rate, safety rate
7. **Tests end-to-end** con alertas reales provocadas en el cluster

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
