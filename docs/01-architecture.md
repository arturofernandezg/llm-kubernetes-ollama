# Arquitectura del sistema

## Visión general

Agente AIOps de remediación automática en Kubernetes. Detecta problemas en el cluster (mediante Prometheus/Alertmanager), consulta a un sistema RAG (ChromaDB) y procesa todo por el LLM para notificar/actuar sobre el cluster.
en MasOrange/Telecable. TFG/TFM — rol: ingeniero AIOps.

*(Nota: Originalmente el proyecto se basaba en la generación de IaC (Terraform) para GCP. Esa funcionalidad ha sido desplazada en favor de la remediación de Kubernetes, aunque el código original se conserva en `agent/`).*

## Topología del Entorno de Trabajo (Dual-Environment)

El ciclo de desarrollo y operaciones del proyecto se reparte entre dos ecosistemas para separar control de versiones y operaciones del clúster:

1. **Local Desktop (Windows / VSCode)**: Base principal de programación. Sede de control de versiones locales (*Git*), programación del software de la API (TDD), y manipulación de artefactos de diseño.
2. **Google Cloud Shell (Navegador)**: Consola de operaciones en GCP. Herramientas pesadas acopladas nativamente al clúster perimetral GKE `ai-infra-agent` (`gcloud`, `kubectl`, Cloud Build CLI), facilitando la interacción continua con los *Node Pools* e insumos de la API privada de Google.

## Flujo objetivo (Sistema de Remediación RAG)

```
Prometheus / Alertmanager
    │
    ▼
FastAPI Agent (/webhook/alert)
    │
    ├─► 1. Normalizar alerta (labels, annotations → estructura interna)
    │
    ├─► 2. Embedding del error (Ollama /api/embeddings con nomic-embed-text)
    │
    ├─► 3. Retrieval híbrido en ChromaDB:
    │       ├── Colección "runbooks"   → conocimiento estático (procedimientos, docs K8s)
    │       └── Colección "incidents"  → memoria viva (alertas pasadas + fixes confirmados)
    │
    ├─► 4. LLM genera diagnóstico estructurado (Ollama qwen2.5:1.5b)
    │       → JSON: { diagnosis, commands[], confidence, risk }
    │
    ├─► 5. Validation Layer (whitelist de comandos, bloqueo de destructivos)
    │
    ├─► 6. Mattermost (ChatOps - notificación con diagnóstico + acciones sugeridas)
    │       └── Botones: [Aprobar Remediación] [Rechazar] (Fase 3)
    │
    └─► 7. Kubernetes API Server (auto-patch si risk=low Y confianza alta)
            └── Feedback loop: si el fix funciona, se guarda en colección "incidents"
```

## Paradigma: Retrieval-First (no Classification-First)

La arquitectura sigue un paradigma **retrieval-first**: en lugar de clasificar la alerta con un
modelo supervisado y luego buscar documentación para esa clase, el sistema genera un embedding
del error completo y busca directamente los documentos más similares en la base vectorial.

**Justificación**:
- Con pocos datos etiquetados (<100 incidentes iniciales), un clasificador supervisado
  tiene precisión insuficiente exactamente cuando más se necesita (errores raros).
- Los embeddings capturan similitud semántica sin necesitar taxonomía previa.
- Las categorías conocidas (OOMKilled, CrashLoopBackOff, etc.) se capturan como
  metadata en ChromaDB, no como clases de un modelo ML.
- Un clasificador supervisado solo se justifica con >5k ejemplos etiquetados y
  taxonomía estable — alcanzable en Fase 3+ con el feedback loop acumulado.

## Flujo actual (Fase 1 - Legado)

```
[Port-forward / curl / generate_tf.py]
         │
         ▼
   agent-svc:8000      ← FastAPI (1 réplica, probes, retry, metrics, shared httpx client)
         │
         ▼
   ollama-svc:11434    ← Ollama (1 réplica, PVC 20Gi, PDB)
         │
         ▼
     ollama-pvc         ← Modelos persistidos en disco
```

## Componentes

| Componente | Tecnología | Ubicación | Réplicas | Fase |
|---|---|---|---|---|
| Agente | Python 3.11, FastAPI, httpx, BackgroundTasks | `agent/` | 1 | 1 |
| LLM (generación) | Ollama (qwen2.5:1.5b) | Pod K8s | 1 | 1 |
| LLM (embeddings) | Ollama (nomic-embed-text, 768 dims) | Pod K8s (mismo) | — | 2 |
| Vector DB | ChromaDB 0.4.24 (StatefulSet, PVC 10Gi) | Pod K8s | 1 | 2 |
| ChatOps | Mattermost (webhook + bot) | Pod K8s | 1 | 1 |
| Monitoring | kube-prometheus-stack (Prometheus + Alertmanager) | Pods K8s | — | 1 |
| TF Generator | Python stdlib (urllib) — legado | `generate_tf.py` | CLI local | 0 |
| Build pipeline | Google Cloud Build | `cloudbuild.yaml` | — | 1 |
| Infra | GKE (e2-standard-2 spot, 2 nodos) | `k8s/` | — | 1 |

## Sistema RAG — Diseño Detallado

### Modelo de Embeddings

| Propiedad | Valor |
|---|---|
| Modelo | `nomic-embed-text` |
| Tamaño en disco | 274 MB |
| RAM estimada | ~600 MB |
| Dimensiones | 768 |
| Carga | Manual (mismo flujo que qwen2.5 — sin Cloud NAT) |
| API | `POST ollama-svc:11434/api/embeddings` |

**Alternativas evaluadas y descartadas**:
- `text-embedding-3-large` (OpenAI): requiere internet → imposible sin Cloud NAT.
- `bge-large-en` (1.3 GB): demasiada RAM para nodo spot e2-standard-2 compartido con Ollama + ChromaDB.
- `all-minilm` (45 MB, 384 dims): viable como fallback si la RAM es crítica, pero calidad inferior.

### ChromaDB — Dual Collection

El diseño utiliza **dos colecciones** con propósitos complementarios:

**Colección `runbooks`** — Conocimiento estático

Runbooks operacionales, documentación de Kubernetes/Terraform, procedimientos internos de
MasOrange/Telecable. Se carga manualmente y se actualiza infrecuentemente.

```
Documento ejemplo:
  id:       "runbook-oomkilled-001"
  document: "Pod terminado por OOM (Out of Memory). Exit code 137.
             Síntomas: container killed por kernel OOM killer.
             Checks: kubectl describe pod <pod>, kubectl top pod.
             Fix: aumentar memory limits del container en el Deployment."
  metadata:
    type:        "runbook"
    error_class: "OOMKilled"
    service:     "kubernetes"
    severity:    "high"
    commands:    "kubectl describe pod,kubectl top pod,kubectl edit deployment"
```

**Colección `incidents`** — Memoria semántica viva (Feedback Loop)

Cada vez que el sistema procesa una alerta y un humano confirma o rechaza la remediación
en Mattermost, el ciclo completo se persiste como incidente resuelto. Así el RAG aprende
de la experiencia real del cluster, no solo de documentación genérica.

```
Documento ejemplo:
  id:       "incident-2026-03-25-oom-nginx"
  document: "Alerta OOMKilled en pod nginx-7d4f8b-x2k (ns: arturo-llm-test).
             Diagnóstico: memory limit 256Mi insuficiente para carga actual.
             Fix aplicado: patch memory limit a 384Mi. Resultado: alerta cesó."
  metadata:
    type:        "incident"
    error_class: "OOMKilled"
    outcome:     "resolved"        ← "resolved" | "rejected" | "escalated"
    fix_applied: "patch-memory"
    confidence:  0.85
    timestamp:   "2026-03-25T14:30:00Z"
```

**Criterio de persistencia (memoria selectiva)**:
- **SÍ guardar**: incidentes con acción humana (aprobada o rechazada), fixes automáticos exitosos.
- **NO guardar**: alertas que se resolvieron solas (resolved sin intervención), alertas de flapping,
  estados transitorios sin valor diagnóstico.
- **Regla**: solo se persiste si hubo intervención (humana o automática) que modificó estado del cluster.

### Chunking

- Tamaño objetivo: 200-500 tokens por chunk.
- Los runbooks se mantienen como documentos completos (no se fragmentan) porque son cortos
  y semánticamente coherentes.
- La documentación larga (K8s docs) se fragmenta con overlap del 10-20% respetando
  fronteras semánticas (no cortar a mitad de sección).

### Query Construction

La query al vector DB **no es el log raw**. Se construye combinando:
1. Texto normalizado de la alerta (alertname + description)
2. Features extraídas (exit code, pod status, namespace)
3. Labels de Prometheus (severity, job, instance)

```
Ejemplo query construida:
"OOMKilled pod nginx-7d4f8b restart container exit code 137
 namespace arturo-llm-test memory high severity critical"
```

### Retrieval

- **Top-K**: 3-5 documentos (suficiente dado el tamaño reducido de la KB).
- **Filtrado por metadata**: `where={"service": "kubernetes"}` para acotar.
- ChromaDB realiza búsqueda vectorial (cosine similarity) con filtrado opcional.
- **Sin re-ranking** (cross-encoder): innecesario con <200 documentos. Se evaluará
  si la colección `incidents` crece significativamente en Fase 3.

### Output Estructurado del LLM

El LLM genera una respuesta JSON constrained:

```json
{
  "diagnosis": "Pod nginx-7d4f8b killed por OOM. Memory limit 256Mi insuficiente.",
  "confidence": 0.82,
  "commands": [
    "kubectl describe pod nginx-7d4f8b -n arturo-llm-test",
    "kubectl top pod nginx-7d4f8b -n arturo-llm-test",
    "kubectl patch deployment nginx -n arturo-llm-test -p '{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"nginx\",\"resources\":{\"limits\":{\"memory\":\"384Mi\"}}}]}}}}'"
  ],
  "risk": "low",
  "explanation": "El container excedió su memory limit. Aumentar a 384Mi (+50%) debería resolver el OOM sin impacto en otros pods del nodo."
}
```

### Safety / Validation Layer

Capa obligatoria entre el LLM y la ejecución:

- **Whitelist de comandos**: solo `kubectl describe`, `kubectl top`, `kubectl logs`,
  `kubectl get`, `kubectl patch`, `kubectl scale`, `kubectl rollout restart`.
- **Blacklist explícita**: `kubectl delete namespace`, `kubectl delete pvc`,
  `rm -rf`, `terraform destroy`, cualquier comando con `--force` sin `--dry-run`.
- **Umbral de auto-ejecución** (Fase 3): solo si `risk == "low"` Y `confidence >= 0.8`
  Y el cambio de recursos es `< 25%` del valor actual.
- **Escalado a humano**: todo lo que no pase el umbral se envía a Mattermost
  como sugerencia para aprobación manual.

## Decisiones de arquitectura

### Fase 1 (vigentes)

- **Ollama en K8s** (no API externa): control total, sin costes por token,
  datos no salen del cluster. Trade-off: modelos pequeños, menos precisión.
- **BackgroundTasks (FastAPI)**: El cliente `httpx` de Mattermost implementa llamadas asíncronas no bloqueantes delegadas al event-loop base (BackgroundTasks) protegiendo el throughput del webhook entrante de Alertmanager de los posibles picos de latencia / timeout.
- **PVC ReadWriteOnce**: los modelos sobreviven a reinicios. Trade-off:
  no se puede escalar Ollama a >1 réplica sin migrar a StatefulSet o GCS.
- **Nodos spot**: reduce costes ~60-70%. Trade-off: los pods pueden ser
  desalojados. Mitigado con PDB y PVC.
- **httpx compartido en lifespan**: reutiliza pool de conexiones TCP,
  evita overhead de crear/cerrar cliente por request.
- **Probes separadas**: /healthz (liveness, sin deps) y /readyz (readiness,
  verifica Ollama). Evita crash loops cuando Ollama está down.
- **1 réplica del agente**: originalmente eran 2, pero se redujo a 1
  (commit 8e32b0c) para que todo quepa en un solo nodo spot e2-standard-2
  (~1930m CPU). Con los resources ajustados, es suficiente para Fase 1.
- **qwen2.5:1.5b** en lugar de tinyllama: mejor precisión en extracción de
  parámetros. tinyllama se mantiene como default en config.py para desarrollo local.
- **Código modularizado**: el agente se dividió en 6 módulos (commit 7ec4a3a)
  para facilitar testing unitario y preparar la integración Slack/GitHub de Fase 2.

### Fase 2+ (planificadas)

- **Retrieval-first, no classification-first**: el sistema busca por similitud
  semántica en vez de clasificar y luego buscar. Ver sección "Paradigma" arriba.
- **ChromaDB dual-collection (runbooks + incidents)**: separa conocimiento estático
  de memoria operativa. La colección `incidents` implementa un feedback loop que
  mejora el sistema con cada alerta procesada.
- **nomic-embed-text para embeddings**: modelo ligero (274MB) que corre en el mismo
  pod de Ollama. Calidad suficiente (768 dims) para el volumen de documentos del proyecto.
- **Memoria selectiva**: solo se persisten incidentes con intervención real
  (humana o automática). Alertas transitorias y flapping se descartan para evitar
  contaminar la base vectorial con ruido.
- **Structured JSON output**: el LLM genera JSON con schema fijo (diagnosis,
  commands, confidence, risk). Esto permite validación automática y decisiones
  programáticas sobre auto-remediación.
- **Validation layer obligatoria**: whitelist de comandos + umbral de confianza
  entre el LLM y cualquier acción sobre el cluster. Sin esta capa, el sistema
  no puede ejecutar nada automáticamente.

### Alternativas evaluadas y descartadas

| Alternativa | Por qué se descartó |
|---|---|
| **Qdrant** (en lugar de ChromaDB) | Superior técnicamente (Rust, mejor rendimiento), pero ChromaDB ya está en manifiestos K8s, es más ligero y suficiente para <500 documentos. Cambio innecesario. |
| **Clasificador supervisado** (DeBERTa, etc.) | Requiere >5k ejemplos etiquetados. Con <100 incidentes iniciales, embeddings + similitud generalizan mejor. Se reconsiderará en Fase 3+ si el volumen de incidentes lo justifica. |
| **HDBSCAN clustering offline** | Sobreingeniería con <100 incidentes. Categorías manuales simples (OOMKilled, CrashLoopBackOff, HighCPU) son suficientes y más defendibles. |
| **Fluentd/Vector para ingesta** | Prometheus + Alertmanager ya capturan los eventos. Añadir otro colector de logs duplica funcionalidad y consume recursos escasos en nodos spot. |
| **Redis para caché** | Con el volumen actual (<10 alertas/hora estimadas), el overhead de otro pod no se justifica. Caché in-memory en el agente (dict Python con TTL) es suficiente si se necesita. |
| **NetworkX para grafos de dependencia** | La K8s API ya expone ownerReferences. Útil como visualización en la tesis pero no como componente runtime. |
| **OpenAI/Vertex AI embeddings** | Requieren internet (OpenAI) o Private Google Access (Vertex). Sin Cloud NAT y sin confirmación de PGA, no son viables. Ollama resuelve embeddings in-cluster. |

## Namespaces

El proyecto usa 3 namespaces separados por grupo funcional:

| Namespace | Componentes | Justificación |
|---|---|---|
| `arturo-llm-test` | agent, ollama, chromadb, apache | Core AIOps — comunicación intensiva entre sí |
| `arturo-monitoring` | Prometheus, Alertmanager, kube-state-metrics, node-exporter | Observabilidad — convención del helm chart, separado del workload |
| `arturo-mattermost` | Mattermost, PostgreSQL | ChatOps — DB aislada del stack de IA |

## Red interna

Todos los servicios usan ClusterIP (no expuestos a internet). La comunicación
cross-namespace usa FQDNs completos:

| Servicio | FQDN | Puerto |
|---|---|---|
| agent | `agent-svc.arturo-llm-test.svc.cluster.local` | 8000 |
| ollama | `ollama-svc.arturo-llm-test.svc.cluster.local` | 11434 |
| chromadb | `chromadb-svc.arturo-llm-test.svc.cluster.local` | 8000 |
| mattermost | `mattermost-svc.arturo-mattermost.svc.cluster.local` | 8065 |

Comunicación cross-namespace relevante:
- `arturo-monitoring/alertmanager` → `arturo-llm-test/agent-svc:8000` (webhook de alertas)
- `arturo-llm-test/agent` → `arturo-mattermost/mattermost-svc:8065` (notificaciones ChatOps)

Sin Cloud NAT: los pods no tienen salida a internet. Modelos (LLM + embeddings) se cargan
manualmente o consumiendo APIs internas privadas.

## Integración y Seguridad con Vertex AI (GCP)

El uso de los modelos fundacionales de Google (Gemini/Claude vía Vertex AI) está **estrictamente delimitado al entorno privado de trabajo** (VPC).

1. **Aislamiento de Red**: Las peticiones desde el agente FastAPI hacia Vertex nunca cruzan internet público. Se resuelven con routing interno puro (requiere tener *Private Google Access* habilitado en la *Subnet* de GCP).
2. **Workload Identity / Service Accounts**: Queda terminantemente prohibido el uso de API keys genéricas. La app se autentica tácitamente contra el control de accesos (IAM) de Google Cloud usando la identidad confiable de su entorno de ejecución (Namespace y Pod).

## Seguridad

- **SecurityContext (agent)**: `runAsNonRoot`, `readOnlyRootFilesystem`,
  `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`.
  Volume emptyDir montado en /tmp para uvicorn.
- **NetworkPolicy**: 2 políticas que restringen tráfico entre pods:
  - Ollama solo acepta ingress del agent (port 11434)
  - Agent solo acepta ingress de Apache (port 8000)
- **Dockerfile non-root**: usuario `appuser` creado explícitamente.
- **Ollama**: corre como root (limitación de la imagen oficial), pero con
  `allowPrivilegeEscalation: false` y `capabilities.drop: [ALL]`.
