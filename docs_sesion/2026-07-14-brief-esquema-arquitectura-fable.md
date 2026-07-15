---
brief_for: fable
promoted: true
objetivo: Generar un esquema visual del cluster (foto de todo lo desplegado) para la reunión con el tutor / chapter
---

# Brief para Fable — Esquema de arquitectura del cluster

> **Contexto de handoff**: Este brief lo prepara Opus en la sesión del 2026-07-14. Fable arranca EN FRÍO
> (sin el contexto de esa sesión). **Toda la verdad del cluster está aquí abajo, extraída de los
> manifiestos `k8s/*.yaml` reales (NetworkPolicy + env vars + RBAC)** — no inventes servicios, puertos
> ni aristas: usa exactamente los de este documento. Si algo no está aquí, NO lo pongas.

## Objetivo

Un **esquema visual tipo "foto del cluster"**: una caja grande = el cluster GKE, y dentro cajas por
namespace, y dentro de cada namespace los pods desplegados con su función. Flechas etiquetadas entre
pods mostrando **quién llama a quién y por qué** (ej. "agent → mattermost:8065 · notifica escalación").
Es para que un tutor/chapter vea de un vistazo TODO lo que Jay ha ido desplegando.

**Incluir sí o sí una nota**: *se quería usar **Slack**, pero por políticas de seguridad de la empresa
no se pudo (los datos no podían salir a un SaaS externo) → se desplegó **Mattermost self-hosted
in-cluster** como ChatOps, así los datos nunca salen del cluster.*

## Formato de salida (recomendado)

**Un Artifact HTML autocontenido** (usa la skill `artifact-design` antes de escribirlo). Motivo: da
control total sobre el layout "caja dentro de caja" y queda como entregable presentable offline.
- Alternativa válida si vas con prisa: un diagrama **Mermaid** `flowchart` con `subgraph` anidados
  (un subgraph por namespace dentro de un subgraph "Cluster GKE"). Mermaid renderiza nativo en Artifacts.
- Debe verse bien en claro y oscuro (theme-aware) y no hacer scroll horizontal en el body.

---

## GROUND TRUTH — no desviarse de esto

### Cluster
- **Nombre**: `ai-infra-agent` (GKE)
- **Zona**: `europe-southwest1-a`
- **Nodos**: pool `e2-standard-2` (Spot) + **2 nodos guaranteed** (label `guaranteed=true`).
  El **agent** y **ollama** están fijados a los nodos `guaranteed=true` (nodeSelector + toleration).
- **Registry**: `europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent`
- **Sin Cloud NAT**: los pods NO tienen salida a internet (los modelos LLM se cargan a mano).
- **NetworkPolicy ACTIVO**: tráfico segmentado pod-a-pod (las aristas de abajo son las ÚNICAS permitidas).

### Namespaces y pods desplegados

**`arturo-llm-test`** (núcleo del sistema):
| Pod | Service:puerto | Función |
|---|---|---|
| **agent** | `agent-svc:8000` | FastAPI. Webhook, pipeline, motor de remediación. Imagen `aiops-agent:2ac3c5d`. Fijado a `guaranteed=true` |
| **ollama** | `ollama-svc:11434` | LLM in-cluster: `qwen2.5:1.5b` (generación) + `nomic-embed-text` (embeddings). PVC `ollama-pvc`. Fijado a `guaranteed=true` |
| **chromadb** | `chromadb-svc:8000` | Base RAG vectorial, 16 runbooks. PVC persistente |
| **redis** | `redis-svc:6379` | Cola Redis Streams (`aiops:alerts`) + escalaciones + rollback durable + cooldown + índice de incidentes |
| **apache** | `apache` (:80/:8000) | Solo tests de red / validación manual. Secundario, puedes dibujarlo pequeño o en gris |

**`arturo-monitoring`** (observabilidad):
| Pod | Service:puerto | Función |
|---|---|---|
| **prometheus** (+ kube-state-metrics) | :9090 | Scrapea métricas, evalúa 6 reglas de alerta. Acotado a `arturo-.*` (cluster compartido) |
| **alertmanager** | `alertmanager-svc:9093` | Recibe alertas de Prometheus, dispara el webhook al agente |
| **grafana** | :3000 | 2 dashboards (Overview + Chaos) |

**`arturo-mattermost`** (ChatOps):
| Pod | Service:puerto | Función |
|---|---|---|
| **mattermost** | `mattermost-svc:8065` | ChatOps. Recibe notificaciones del agente, renderiza botones Aprobar/Rechazar (HMAC) |
| **postgres** | `postgres-svc:5432` | Base de datos de Mattermost (solo Mattermost accede) |

**`arturo-chaos`** (experimentos):
| Pod | Función |
|---|---|
| **chaos-oom-target** (+ otros) | Deployments de chaos engineering: OOM, crashloop, bad-image, cpu. El agente remedia aquí también |

### Aristas de comunicación (EXACTAS, de NetworkPolicy + env vars del agente)

Dibuja estas flechas y NINGUNA otra. Etiqueta cada una con puerto + motivo:

1. **prometheus → agent** `:8000` — scrapea `/metrics` del agente
2. **prometheus → alertmanager** — Prometheus detecta y enruta la alerta (misma ns monitoring)
3. **alertmanager → agent** `:8000` — POST `/webhook/alert` (dispara el pipeline)
4. **agent → ollama** `ollama-svc:11434` — genera diagnóstico + embeddings
5. **agent → chromadb** `chromadb-svc:8000` — query RAG (runbooks) + ingesta de incidentes
6. **agent → redis** `redis-svc:6379` — encola alerta, guarda escalaciones/rollback/cooldown
7. **agent → mattermost** `mattermost-svc:8065` (cross-namespace) — notifica escalación con botones
8. **mattermost → agent** `:8000` (cross-namespace) — callback del botón `/webhook/action` (HMAC)
9. **mattermost → postgres** `postgres-svc:5432` — persistencia (misma ns)
10. **grafana → agent** `:8000` — test del contact point
11. **agent → K8s API** — `kubectl get/patch deployments` para remediar. Solo en `arturo-llm-test`
    y `arturo-chaos` (ver RBAC abajo). Esta es la flecha de "el motor dispone".
12. (opcional, gris) **apache → agent** `:8000` — tests de red

**Flujo principal E2E** (resáltalo, es la narrativa): 
`Prometheus → Alertmanager → agent(webhook) → [ollama + chromadb + redis] → mattermost(botón) → operador aprueba → agent → K8s API (patch)`

### RBAC (dato de seguridad que da puntos)
El agente usa un **`Role` namespaced (NUNCA ClusterRole)** — least-privilege. Permisos SOLO en
`arturo-llm-test` y `arturo-chaos`:
- `deployments`: `get`, `patch` (remediar)
- `pods`, `replicasets`, `events`, `limitranges`: `get`/`list` (grounding)
- `pods/log`: `get`
No hay permisos de escritura cluster-scoped → el blast-radius está acotado a los namespaces propios.

---

## Especificación visual sugerida

- Caja exterior grande con título **"Cluster GKE · ai-infra-agent · europe-southwest1-a"** y un
  sub-badge "Sin Cloud NAT · NetworkPolicy activo · nodos Spot + guaranteed".
- 4 sub-cajas (namespaces) con color/borde distinto cada una, etiquetadas con el nombre del namespace.
- Dentro de cada namespace, los pods como nodos (nombre + puerto + una línea de función).
- Flechas etiquetadas (puerto + motivo). Resalta el flujo principal E2E en un color de acento.
- Marca la flecha **agent → K8s API** como la acción de remediación (icono/color distinto).
- **Recuadro/callout** con la nota de Slack → Mattermost (ver arriba).
- Leyenda: distinguir "flujo de alerta", "dependencias del agente", "acción de remediación", "datos".

## Checklist de verificación para Fable (antes de dar por hecho)
- [ ] Aparecen los 4 namespaces y TODOS los pods de las tablas.
- [ ] Las 11 aristas están y ninguna inventada (ej. NO dibujar agent↔grafana salvo la #10, NO chromadb↔ollama).
- [ ] La nota Slack→Mattermost está visible.
- [ ] Se distingue el flujo E2E principal del resto.
- [ ] Puertos correctos (agent 8000, ollama 11434, chromadb 8000, redis 6379, mattermost 8065, postgres 5432, alertmanager 9093).
- [ ] Renderiza en claro y oscuro, sin scroll horizontal.
