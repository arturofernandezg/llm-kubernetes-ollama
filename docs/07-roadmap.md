# Roadmap — Fases del proyecto (Evolución AIOps)

## Estado de las fases

| Fase | Descripción | Estado |
|---|---|---|
| Fase 0 (Legado) | Agente + Ollama + extracción de params + generación .tf | Completa (En desuso activo) |
| Fase 1 (Observabilidad) | kube-prometheus-stack, webhook Alertmanager, Mattermost ChatOps | En curso |
| Fase 2 (RAG) | ChromaDB dual-collection, embeddings in-cluster, diagnóstico contextual | Pendiente |
| Fase 3 (Remediación) | Auto-patch K8s API, validation layer, feedback loop cerrado | Pendiente |

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
- [ ] Instalación del stack `kube-prometheus-stack` (Prometheus + Alertmanager) en el cluster.
- [ ] Definir alerting rules para las alertas críticas: `KubePodOOMKilled`, `KubePodCrashLooping`,
  `KubeCPUOvercommit`, `KubeMemoryOvercommit`, `TargetDown`.
- [ ] Conectar Alertmanager → webhook del agente FastAPI (receiver config).

### ChatOps (Mattermost)
- [ ] Instalar Mattermost en el cluster (helm chart o manifiesto propio).
- [ ] Configurar webhook entrante + token bot para el agente.
- [ ] Desarrollar formateo enriquecido: diagnóstico del LLM renderizado como mensaje
  Mattermost con severity, commands sugeridos y botones de acción (Fase 3).

### Entregable Fase 1
Pipeline end-to-end: Alerta de Prometheus → Alertmanager → FastAPI webhook → notificación
formateada en Mattermost con datos de la alerta. Sin LLM/RAG aún — solo routing + formateo.

---

## Fase 2 — RAG y Diagnóstico Contextual

### Infraestructura
- [ ] Desplegar ChromaDB StatefulSet (manifiesto ya en `k8s/chromadb.yaml`).
- [ ] Cargar modelo de embeddings `nomic-embed-text` (274 MB) en Ollama (mismo flujo manual).
- [ ] Actualizar NetworkPolicy: permitir tráfico agent → chromadb-svc:8000.

### Módulos nuevos del agente
- [ ] **`rag.py`**: cliente ChromaDB (chromadb-client), funciones de ingesta y query,
  construcción de queries enriquecidas (no solo log raw).
- [ ] **`diagnosis.py`**: prompt template AIOps contextual (alerta + contexto RAG → JSON
  estructurado), parsing de respuesta del LLM con validación de schema.

### Knowledge Base
- [ ] Crear colección `runbooks` con 15-20 runbooks semilla para alertas K8s comunes:
  OOMKilled, CrashLoopBackOff, ImagePullBackOff, HighCPU, HighMemory, PodEvicted,
  NodeNotReady, DiskPressure, NetworkUnavailable, etc.
- [ ] Crear colección `incidents` (vacía inicialmente, se llena con feedback loop).
- [ ] Definir metadata schema: `type`, `error_class`, `service`, `severity`, `commands`.

### Pipeline RAG completo
- [ ] Flujo: alerta → normalizar → embedding → query ChromaDB → construir prompt
  con contexto → LLM genera JSON estructurado → formatear para Mattermost.
- [ ] Output del LLM: `{ diagnosis, commands[], confidence, risk, explanation }`.

### Entregable Fase 2
Pipeline end-to-end con RAG: misma alerta ahora genera un diagnóstico contextualizado
con runbook relevante, comandos sugeridos y nivel de confianza. Notificación enriquecida
en Mattermost.

---

## Fase 3 — Remediación Autónoma y Feedback Loop

### Auto-remediación
- [ ] **`remediation.py`**: validation layer (whitelist/blacklist de comandos), cliente K8s
  API (kubernetes python client), lógica de auto-patch con umbrales.
- [ ] RBAC least-privilege: Role + RoleBinding para el ServiceAccount del agente con
  permisos solo de `patch` y `get` sobre `deployments`, `pods`, `limitranges` en el namespace.
- [ ] Umbrales de auto-ejecución:
  - `risk == "low"` Y `confidence >= 0.8` Y cambio de recursos `< 25%` → **auto-patch**.
  - Todo lo demás → **escalar a humano** en Mattermost.

### Feedback Loop (Memoria Semántica)
- [ ] Tras cada remediación (aprobada o rechazada), persistir el incidente completo
  en la colección `incidents` de ChromaDB.
- [ ] Estructura: alerta original + diagnóstico + fix propuesto + outcome (resolved/rejected/escalated).
- [ ] Monitorización de bucle cerrado: verificar en Prometheus que la alerta cesa tras
  aplicar el fix. Si cesa → `outcome: resolved`. Si persiste → `outcome: failed`, escalar.

### Botones interactivos (Mattermost)
- [ ] Mensajes con acciones: `[Aprobar Remediación]` / `[Rechazar]` / `[Escalar]`.
- [ ] Endpoint callback para recibir la decisión del humano y ejecutar/abortar.

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

## Mejoras técnicas pendientes (transversales / post-TFM)

- [ ] Evaluar framework retrospectivamente (ej. Robusta.dev) para futuras iteraciones de AIOps.
- [ ] Mapeo de ServiceAccounts de Kubernetes (Workload Identity) a IAM GCP.
- [ ] Solicitar rol `roles/logging.logWriter` para service account de Cloud Build.
- [ ] Buckets de histograma Prometheus personalizados para /webhook/alert (5s, 10s, 30s, 60s).
- [ ] Caché in-memory (dict con TTL) para embeddings de alertas frecuentes (si el volumen lo justifica).
- [ ] Re-ranking con cross-encoder si la colección `incidents` supera ~500 documentos.
- [ ] Clasificador supervisado (multi-label) si se acumulan >5k incidentes etiquetados.
- [ ] Migración de nodos Spot a Standard para ChromaDB (evaluación coste vs estabilidad).
