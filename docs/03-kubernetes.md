# Infraestructura Kubernetes (GKE)

## Cluster

| Propiedad | Valor |
|---|---|
| Nombre | ai-infra-agent |
| Proyecto GCP | uniovi-ai-infra-agent |
| Zona | europe-southwest1-a |
| Tipo de nodo | e2-standard-2 Spot (pool principal) + 2 nodos guaranteed (`guaranteed=true`) asignados por tutor 2026-05-06 |
| Nodos | 2 Spot + 2 guaranteed |
| Namespace | arturo-llm-test |
| K8s version | 1.35.1-gke.1396001 |

## Manifiestos (`k8s/`)

| Archivo | Recurso | Notas |
|---|---|---|
| `deployment-ollama.yaml` | Deployment ollama (1 réplica) | PVC, probes, OLLAMA_KEEP_ALIVE=24h |
| `service-ollama.yaml` | ClusterIP :11434 | Solo accesible internamente |
| `pvc-ollama.yaml` | PVC 20Gi ReadWriteOnce | Modelos LLM persistidos |
| `pdb-ollama.yaml` | PodDisruptionBudget (minAvailable: 1) | Protege durante drains |
| `deployment-agent.yaml` | Deployment agent (1 réplica) | Probes /healthz y /readyz |
| `service-agent.yaml` | ClusterIP :8000 | Solo accesible internamente |
| `deployment-apache.yaml` | Deployment apache (1 réplica) | Validación de red |
| `service-apache.yaml` | ClusterIP :80 | Validación de red |
| `chromadb.yaml` | StatefulSet + Service chromadb (1 réplica) | PVC 10Gi, imagen 0.6.3, probes /api/v1/heartbeat |
| `redis.yaml` | Deployment `redis:7-alpine` + Service `redis-svc:6379` | Estado de escalaciones (TTL 60 min), sobrevive reinicios del agente |
| `networkpolicy.yaml` | NetworkPolicy (6 políticas) | Segmentación de tráfico: ollama←agent, agent←{apache,alertmanager,prometheus,grafana,mattermost}, chromadb←agent, redis←agent, mattermost←agent, postgres←mattermost |
| `prometheus.yaml` | ServiceAccounts, ClusterRoles, ConfigMaps, Deployments, Services para Prometheus + KSM | Monitoring stack en `arturo-monitoring` |
| `alertmanager.yaml` | Deployment + ConfigMap + Service para Alertmanager | Routing de alertas a webhook del agente |
| `grafana.yaml` | Deployment stateless + ConfigMaps provisioning + Service + Secret ref | Datasource Prometheus, dashboards "AIOps Agent — Overview" + "AIOps — Chaos", contact point webhook |
| `job-ingest-runbooks.yaml` | K8s Job `runbooks-ingest` en `arturo-llm-test` | Ingesta idempotente (upsert) de 16 runbooks en ChromaDB |
| `rbac.yaml` | Role + RoleBinding en `arturo-llm-test` y `arturo-chaos` | RBAC para remediación autónoma del agente; RoleBinding cross-namespace permite al SA de `arturo-llm-test` remediar workloads en `arturo-chaos` |

## Prometheus standalone

Desplegado en `arturo-monitoring` vía `k8s/prometheus.yaml`. Componentes:

| Recurso | Descripción |
|---|---|
| `ServiceAccount prometheus` | SA del pod Prometheus |
| `ClusterRole prometheus-viewer` | Lectura de nodes, pods, services, endpoints + `/metrics/cadvisor` |
| `ClusterRoleBinding prometheus-viewer` | Vincula SA `prometheus` al ClusterRole |
| `ServiceAccount kube-state-metrics` | SA del pod KSM |
| `ClusterRole kube-state-metrics-viewer` | Lectura de todos los recursos que KSM expone |
| `ConfigMap prometheus-config` | `prometheus.yml`: scrape_configs + alertmanager URL + rule_files |
| `ConfigMap prometheus-rules` | 5 reglas de alerting AIOps (ver abajo) |
| `Deployment prometheus` | `prom/prometheus:v2.54.0`, emptyDir storage (2h retención), :9090 |
| `Service prometheus-svc` | ClusterIP :9090 — datasource de Grafana (futura sesión) |
| `Deployment kube-state-metrics` | `kube-state-metrics:v2.13.0`, expone métricas K8s en :8080 |
| `Service kube-state-metrics-svc` | ClusterIP :8080, annotado con `prometheus.io/scrape=true` |

**Nota ClusterRole**: necesario (no namespace-scoped) porque el scrape de kubelet/cAdvisor
requiere `list/watch` de nodes y acceso a `/api/v1/nodes/<name>/proxy/metrics/cadvisor`.
Permisos de admin confirmados por el tutor (2026-04-20).

### Scrape targets

Prometheus descubre targets en dos jobs:

1. **`kubernetes-endpoints`**: servicios anotados con `prometheus.io/scrape=true` en namespaces
   `arturo-llm-test` y `arturo-monitoring`. Actualmente: `agent-svc` (port 8000) y `kube-state-metrics-svc` (port 8080).
2. **`kubernetes-cadvisor`**: kubelet cAdvisor en todos los nodos vía proxy API
   (`/api/v1/nodes/<name>/proxy/metrics/cadvisor`).

Para añadir un nuevo target: añadir las annotations a su Service:
```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "XXXX"
  prometheus.io/path: "/metrics"
```

### 6 reglas de alerting

| Alerta | Expresión | For | Severity |
|---|---|---|---|
| `KubePodOOMKilled` | `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} == 1` | 0m | critical |
| `KubePodCrashLoopBackOff` | `increase(kube_pod_container_status_restarts_total[15m]) > 3` | 5m | critical |
| `HighMemory` | `container_memory_working_set_bytes / kube_pod_container_resource_limits{resource="memory"} > 0.9` | 5m | warning |
| `HighCPU` | `rate(container_cpu_usage_seconds_total[5m]) / kube_pod_container_resource_limits{resource="cpu"} > 0.9` | 5m | warning |
| `TargetDown` | `up == 0` | 2m | critical |
| `KubePodImagePullBackOff` | `kube_pod_container_status_waiting_reason{reason="ImagePullBackOff"} == 1` | 1m | critical |

Todas tienen `labels.team: aiops` — Alertmanager las enruta al webhook del agente.

### Namespace label collision KSM/cAdvisor (fix aplicado 2026-05-26)

Prometheus scrapeaba KSM (en `arturo-monitoring`) y aplicaba `namespace=arturo-monitoring` como target label, renombrando el `namespace` original de los pods a `exported_namespace`. Esto causaba:
- Join `on(namespace, pod, container)` de HighCPU/HighMemory fallaba (cAdvisor tiene `namespace=arturo-chaos`, KSM tenía `namespace=arturo-monitoring`)
- Alertas en Mattermost mostraban `pod=kube-state-metrics-xxx` en lugar del pod afectado real

**Fix** (`k8s/prometheus.yaml`, job `kubernetes-endpoints`):
```yaml
metric_relabel_configs:
  - source_labels: [exported_namespace]
    regex: (.+)
    target_label: namespace
  - regex: exported_namespace
    action: labeldrop
  - source_labels: [exported_pod]
    regex: (.+)
    target_label: pod
  - regex: exported_pod
    action: labeldrop
```

### Verificación

```bash
kubectl get pods -n arturo-monitoring
kubectl port-forward svc/prometheus-svc 9090:9090 -n arturo-monitoring
# http://localhost:9090/targets  → agent-svc + kube-state-metrics-svc + cadvisor UP
# http://localhost:9090/rules    → 6 reglas cargadas
```

## Redis (estado de escalaciones)

Desplegado en `arturo-llm-test` via `k8s/redis.yaml`. Proporciona persistencia de escalaciones pendientes entre reinicios del pod agente (reemplaza el dict in-memory `PENDING_ESCALATIONS`).

| Propiedad | Valor |
|---|---|
| Imagen | `europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent/redis:7-alpine` |
| Service | `redis-svc:6379` (ClusterIP, solo accesible desde el agente) |
| Recursos | req 32Mi/10m — limits 64Mi/50m |
| Persistencia | **Sin PVC** — estado efímero (TTL 60 min por key). Sobrevive reinicios del *agente* (objetivo principal), no del pod Redis en sí |
| SecurityContext | `runAsUser: 999`, `allowPrivilegeEscalation: false`, `capabilities: drop ALL` |
| Probes | `tcpSocket :6379` para liveness y readiness |
| NetworkPolicy | `redis-allow-agent-only` — solo acepta ingress del pod `app=agent` en puerto 6379 |

**Mirror de imagen** (sin Cloud NAT):
```bash
crane copy --platform linux/amd64 redis:7-alpine europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent/redis:7-alpine
```

**Verificación**:
```bash
kubectl get pods -n arturo-llm-test -l app=redis
kubectl exec -n arturo-llm-test deploy/redis -- redis-cli ping
```

## Probes del agente

```yaml
livenessProbe:         # /healthz — sin dependencias, siempre 200
  path: /healthz       # Si falla → K8s reinicia el pod
  initialDelaySeconds: 10
  periodSeconds: 15

readinessProbe:        # /readyz — verifica Ollama + modelo
  path: /readyz        # Si falla → K8s deja de enrutar tráfico
  initialDelaySeconds: 5
  periodSeconds: 10
```

## Probes de Ollama

```yaml
readinessProbe:        # GET / — verifica que Ollama responde
  path: /
  initialDelaySeconds: 10
  periodSeconds: 5

livenessProbe:         # GET / — reinicia si Ollama se cuelga
  path: /
  initialDelaySeconds: 30
  periodSeconds: 15
```

## PodDisruptionBudget (Ollama)

`minAvailable: 1` — garantiza que durante operaciones de mantenimiento
(node drain, actualizaciones K8s) siempre haya al menos 1 pod de Ollama.
Importante con nodos spot que pueden ser reciclados.

**Verificado** (2026-03-18): `ALLOWED DISRUPTIONS: 0` con 1 réplica activa.
Kubernetes no puede desalojar el pod voluntariamente mientras no haya un reemplazo.

## NetworkPolicy

Fichero: `k8s/networkpolicy.yaml` — segmentación de tráfico entre namespaces. Reglas ingress principales:

1. **`ollama-allow-agent-only`**: pods de Ollama solo aceptan tráfico desde pods con `app: agent` en el port 11434.
2. **`agent-allow-ingress`**: pods del agent (`arturo-llm-test`) aceptan tráfico en el port 8000 desde:
   - `app: apache` (validación de red interna)
   - `app: alertmanager` (namespace `arturo-monitoring`) — webhook de alertas
   - `app: prometheus` (namespace `arturo-monitoring`) — scrape de `/metrics`
   - `app: grafana` (namespace `arturo-monitoring`) — test del contact point

El Job `runbooks-ingest` usa label `app: agent`, así que hereda las reglas egress del agente
(ChromaDB port 8000, Ollama port 11434) sin cambios en la NetworkPolicy.

**Requisito**: el cluster debe tener NetworkPolicy habilitado (GKE Dataplane V2 o Calico).
Si no está habilitado, las políticas se aceptan pero no se aplican (fallan silenciosamente).

```bash
# Verificar si NetworkPolicy está activo
gcloud container clusters describe ai-infra-agent \
  --zone europe-southwest1-a \
  --format="value(networkPolicy, networkConfig.datapathProvider)"
```

## SecurityContext (agent)

El deployment del agente aplica un security context restrictivo (commit 5ec78f5):

```yaml
securityContext:
  runAsNonRoot: true        # impide ejecución como root
  runAsUser: 1000           # OBLIGATORIO en GKE — UID numérico, no simbólico
  readOnlyRootFilesystem: true  # filesystem de solo lectura
  allowPrivilegeEscalation: false
  capabilities:
    drop: [ALL]             # elimina todas las capabilities Linux
```

**Nota `runAsUser`** (2026-04-23): GKE rechaza el contenedor con `CreateContainerConfigError`
si `runAsNonRoot: true` se usa con `USER appuser` simbólico en el Dockerfile (kubelet: "image has
non-numeric user"). El UID numérico explícito (`runAsUser: 1000`) es obligatorio. Aplica también
al Job `runbooks-ingest`.

Se monta un volumen `emptyDir` en `/tmp` porque `readOnlyRootFilesystem: true`
impide que uvicorn escriba ficheros temporales en el filesystem del contenedor.

**Nota sobre Ollama**: la imagen oficial de Ollama requiere root (escribe en `/root/.ollama`).
Se aplica `allowPrivilegeEscalation: false` y `capabilities.drop: [ALL]` como mitigación.

## Node Scheduling — Nodos Guaranteed (Sesión #8, 2026-05-12)

Los workloads críticos (`agent`, `ollama`, `chromadb`) están anclados a los 2 nodos con label `guaranteed=true` asignados por el tutor (2026-05-06). Esto evita que sean interrumpidos por preemption de nodos Spot.

**Configuración aplicada** en `deployment-agent.yaml`, `deployment-ollama.yaml` y `chromadb.yaml` (bajo `spec.template.spec`):

```yaml
nodeSelector:
  guaranteed: "true"
tolerations:
- key: "guaranteed"
  operator: "Equal"
  value: "true"
  effect: "NoSchedule"
```

**Notas:**
- Si los nodos guaranteed no tienen taint `guaranteed:NoSchedule`, las tolerations son inocuas (no afectan al scheduling).
- `chromadb.yaml` mantiene también la toleration GCP Spot existente como safety net por si se elimina el nodeSelector accidentalmente.
- Verificación post-apply: `kubectl get pods -n arturo-llm-test -o wide` → columna NODE debe mostrar nodos guaranteed.

## Namespace `arturo-chaos` — Chaos Engineering (Mini-Fase 4)

Namespace aislado para experimentos de chaos engineering. No interfiere con `arturo-llm-test`, `arturo-monitoring` ni `arturo-mattermost`.

**Manifests:**
- `k8s/chaos/chaos-oom.yaml` — Deployment que consume >memory limit (OOMKilled)
- `k8s/chaos/chaos-crashloop.yaml` — Deployment con `/bin/false` (CrashLoopBackOff)

**Prerrequisito**: mirror de `polinux/stress` a AR antes de aplicar chaos-oom.yaml:
```
crane copy --platform linux/amd64 polinux/stress:latest europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent/polinux-stress:latest
```

**Comandos rápidos:**
```bash
./scripts/chaos.sh oom         # experimento OOMKilled
./scripts/chaos.sh crashloop   # experimento CrashLoopBackOff
./scripts/chaos.sh status      # ver pods en arturo-chaos
./scripts/chaos.sh cleanup     # borrar namespace arturo-chaos
```

El agente detecta alertas de este namespace e incrementa `aiops_chaos_*` en `/metrics`. Ver `docs/12-chaos-engineering.md` para tabla de resultados.

**RBAC en arturo-chaos** (añadido 2026-05-28): `k8s/rbac.yaml` incluye un Role + RoleBinding en `arturo-chaos` que otorga al SA `default` de `arturo-llm-test` los mismos verbos de remediación (`get/patch deployments`, `get/list pods/log/events/limitranges`). RoleBinding cross-namespace — el sujeto especifica `namespace: arturo-llm-test` explícitamente.

**metrics-server**: GKE Standard gestiona metrics-server como addon del sistema (APIService `v1beta1.metrics.k8s.io`, `Available=True`). No requiere instalación manual. `kubectl top node` y `kubectl top pod` funcionan en todos los namespaces sin configuración adicional.

## Grafana — Acceso y dashboard

Dashboard "AIOps Agent — Overview" desplegado en `arturo-monitoring` vía `k8s/grafana.yaml` (stateless, emptyDir).

```bash
kubectl port-forward svc/grafana-svc 3000:3000 -n arturo-monitoring
kubectl -n arturo-monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d
```

Abrir `http://localhost:3000`, login `admin` + contraseña del secret. Dashboard en Dashboards → "AIOps Agent — Overview".

**9 paneles**: aiops_* counters (webhook, diagnosis, remediation, feedback), latencia p95 webhook, targets UP, pod phases, retries/extraction.

## Carga de modelos (sin Cloud NAT)

Los pods NO tienen internet. Para cargar un modelo nuevo:

```bash
# 1. Instalar ollama en Cloud Shell (si no está)
sudo apt-get install -y zstd && curl -fsSL https://ollama.com/install.sh | sh

# 2. Arrancar ollama y descargar el modelo en Cloud Shell
ollama serve &
sleep 2
ollama pull <modelo>   # ej: qwen2.5:1.5b

# 3. Comprimir y copiar al pod
tar czf /tmp/models.tar.gz -C ~/.ollama models
POD=$(kubectl get pod -l app=ollama -n arturo-llm-test -o jsonpath='{.items[0].metadata.name}')
kubectl cp /tmp/models.tar.gz arturo-llm-test/$POD:/tmp/models.tar.gz

# 4. Descomprimir dentro del pod
kubectl exec $POD -n arturo-llm-test -- tar xzf /tmp/models.tar.gz -C /root/.ollama/

# 5. Verificar
kubectl exec $POD -n arturo-llm-test -- ollama list
```

**Nota**: Cloud Shell es efímero — lo instalado se pierde entre sesiones.
Si `kubectl cp` falla por timeout, subir a GCS bucket como alternativa.

## Resources (ajustados para 1 nodo spot e2-standard-2)

| Deployment | CPU request | CPU limit | Mem request | Mem limit |
|---|---|---|---|---|
| ollama | 100m | 2 | 512Mi | 4Gi |
| agent | 50m | 300m | 128Mi | 384Mi |

**Nota**: los requests son bajos a propósito para que todo quepa en 1 nodo (~1930m CPU).
Los limits son altos para que Ollama pueda usar más CPU/RAM cuando carga un modelo.
Si el cluster vuelve a tener 2 nodos, se pueden subir los requests.

## Modelos disponibles

| Modelo | Tamaño | RAM | Estado |
|---|---|---|---|
| qwen2.5:1.5b | 986 MB | ~2 GB | **Activo** (modelo principal) |
| tinyllama | 637 MB | ~1 GB | Almacenado |
| qwen2:1.5b | 934 MB | ~2 GB | Almacenado |
| phi3:mini | 2.2 GB | ~3.5 GB | Almacenado (requiere más RAM) |

## Comandos frecuentes

```bash
# Conectar kubectl al cluster
gcloud container clusters get-credentials ai-infra-agent \
  --zone europe-southwest1-a --project uniovi-ai-infra-agent

# Ver pods
kubectl get pods -n arturo-llm-test

# Ver PDB
kubectl get pdb -n arturo-llm-test

# Port-forward al agente
kubectl port-forward svc/agent-svc 8000:8000 -n arturo-llm-test

# Port-forward a Ollama
kubectl port-forward svc/ollama-svc 11434:11434 -n arturo-llm-test

# Logs del agente
kubectl logs -l app=agent -n arturo-llm-test --tail=50

# Logs de Ollama
kubectl logs -l app=ollama -n arturo-llm-test --tail=50
```

---

## Errores conocidos y soluciones

### kubectl: "connection refused" en Cloud Shell
**Causa**: no has configurado las credenciales del cluster.
**Solución**: `gcloud container clusters get-credentials ai-infra-agent --zone europe-southwest1-a`

### ChromaDB en CrashLoopBackOff — "Could not import module chromadb.app"
**Causa**: La imagen `chromadb/chroma:0.4.24` tenía un bug — su entrypoint ejecutaba
`uvicorn chromadb.app:app` pero ese módulo no existía en el paquete Python interno.
Además, había incompatibilidad de versión: el cliente Python (`chromadb-client==0.6.3`)
no era compatible con el servidor `0.4.24`.
**Solución**: Actualizar imagen a `chromadb/chroma:0.6.3` (coincide con el cliente Python).
Se añadieron también liveness/readiness probes (`GET /api/v1/heartbeat`).
Si `kubectl apply` falla por un ConfigMap huérfano (`chroma-log-config`) del despliegue
anterior, borrar el StatefulSet con `kubectl delete sts chromadb -n arturo-llm-test --cascade=orphan`
(preserva el PVC) y re-aplicar.

### Pod del agente en CrashLoopBackOff
**Causa probable**: la readinessProbe antigua apuntaba a /health que dependía de Ollama.
Si Ollama estaba down, el pod se reiniciaba infinitamente.
**Solución**: con la separación /healthz (liveness) y /readyz (readiness), esto ya no pasa.
El pod sigue vivo pero no recibe tráfico hasta que Ollama esté ready.

### Modelos desaparecen tras reinicio de pod Ollama
**Causa**: el pod se re-scheduled en otro nodo sin acceso al PVC antiguo, o el PVC se borró.
**Solución**: verificar que el PVC existe (`kubectl get pvc -n arturo-llm-test`).
NUNCA borrar el PVC manualmente a menos que quieras perder los modelos.

### El PVC no permite escalar Ollama a >1 réplica
**Causa**: ReadWriteOnce solo permite que un nodo monte el volumen.
**Solución futura**: migrar a init container que descarga modelos desde GCS,
o usar un StatefulSet con volumeClaimTemplates.

### Ollama en Pending con "Insufficient cpu"
**Causa**: con 1 solo nodo spot, los resource requests de todos los pods suman más
de lo disponible (~1930m CPU en e2-standard-2). Típico cuando el 2º nodo spot es
reclamado por Google.
**Solución**: bajar los resource requests (NO limits) al mínimo. Los requests son
lo que Kubernetes reserva para scheduling. Los limits permiten usar más si hay disponible.
**Procedimiento si pasa**: escalar agent a 0 (`kubectl scale deployment agent --replicas=0`),
esperar a que Ollama arranque, luego escalar agent a 1.

### Ollama tarda mucho en ContainerCreating
**Causa**: nodo spot nuevo que no tiene la imagen Docker de Ollama cacheada (~3GB).
El PVC guarda los modelos LLM, NO la imagen Docker del programa Ollama.
**Solución**: esperar 3-5 minutos. Solo pasa la primera vez en cada nodo nuevo.
**Observado** (2026-03-18): tras preemption de nodo spot, Ollama estuvo en ContainerCreating
~2-3 minutos mientras se pullaba la imagen. Durante ese tiempo, el agente estaba Running
pero `/readyz` devolvía 503 (23 requests 5xx contabilizados en métricas Prometheus).
Comportamiento correcto: el pod no recibió tráfico de `/extract` hasta que Ollama estuvo ready.

### kubectl cp falla con "context deadline exceeded"
**Causa**: copiar archivos grandes (>500MB) al pod puede hacer timeout.
**Solución**: comprimir con tar antes de copiar, o subir a GCS bucket y descargar desde el pod.

### Cloud Shell pierde autenticación de kubectl
**Causa**: al abrir múltiples pestañas de Cloud Shell, las credenciales pueden expirar.
**Solución**: `gcloud auth login --update-adc` y luego `gcloud container clusters get-credentials ...`

### Pod `CreateContainerConfigError` con `runAsNonRoot: true`
**Descubierto**: 2026-04-23 al aplicar `k8s/job-ingest-runbooks.yaml`.
**Causa**: el Dockerfile define `USER appuser` (nombre simbólico). El kubelet de GKE requiere
un UID numérico cuando `runAsNonRoot: true` — no resuelve el nombre automáticamente.
`kubectl describe pod` muestra: `container has runAsNonRoot and image has non-numeric user (appuser)`.
**Solución**: añadir `runAsUser: 1000` al `securityContext` del contenedor (UID que genera
`adduser` por defecto en la imagen). Aplicado a `deployment-agent.yaml` y `job-ingest-runbooks.yaml`.

### LLM timeout con prompts RAG-enriquecidos
**Descubierto**: 2026-04-23. Tras la ingesta de runbooks, el diagnóstico empieza a fallar
con un gap de exactamente 120s en los logs (`RAG retrieval: ...` → 120s → `Diagnosis generation failed`).
**Causa**: el prompt enriquecido con contexto RAG (~2200 tokens input + ~300 output) excede el
valor default de `HTTP_TIMEOUT` (120s) en `qwen2.5:1.5b` sobre CPU.
**Solución**: añadir `HTTP_TIMEOUT: "240"` como env var en `deployment-agent.yaml`.
pydantic-settings lo recoge sin rebuild de imagen (duración observada: ~187s).

### Port-forward: "address already in use"
**Causa**: un port-forward anterior sigue corriendo en background.
**Solución**: `pkill -f "port-forward"` y relanzar.

### Nodo spot reclamado (1 nodo en vez de 2)
**Causa**: los nodos spot son preemptibles — Google los reclama cuando necesita capacidad.
El cluster-autoscaler puede no conseguir reemplazarlo si no hay capacidad spot en la zona.
**Solución**: con los resources ajustados actuales, todo cabe en 1 nodo.
Si necesitas forzar 2 nodos: `gcloud container clusters resize ai-infra-agent --node-pool spot-e411 --num-nodes 2 --zone europe-southwest1-a`

---

## Backup ChromaDB

ChromaDB persiste los runbooks y los incidentes procesados en un PVC (`chromadb-pvc`). **Nunca borrar este PVC** — perderías los 16 runbooks ingestados y el histórico de incidentes. El backup manual extrae el contenido del PVC al directorio local.

### Procedimiento (ejecutar desde Cloud Shell)

```bash
# Paso 1 — Crear el tar dentro del pod
kubectl exec -n arturo-llm-test chromadb-0 -- tar czf /tmp/chromadb-backup.tar.gz -C /chroma chroma

# Paso 2 — Copiar al entorno local (Cloud Shell)
kubectl cp -n arturo-llm-test chromadb-0:/tmp/chromadb-backup.tar.gz ./chromadb-backup-$(date +%Y%m%d).tar.gz

# Paso 3 — Verificar tamaño (debe ser > 0)
ls -lh chromadb-backup-*.tar.gz

# Paso 4 — Limpiar /tmp del pod
kubectl exec -n arturo-llm-test chromadb-0 -- rm /tmp/chromadb-backup.tar.gz
```

### Restauración (si el PVC se pierde)

```bash
# Copiar el tar al pod
kubectl cp chromadb-backup-<fecha>.tar.gz arturo-llm-test/chromadb-0:/tmp/chromadb-restore.tar.gz

# Extraer al directorio correcto
kubectl exec -n arturo-llm-test chromadb-0 -- tar xzf /tmp/chromadb-restore.tar.gz -C /chroma

# Reiniciar el pod para que ChromaDB recargue
kubectl rollout restart statefulset/chromadb -n arturo-llm-test
```

**Nota**: un backup automatizado vía CronJob o VolumeSnapshot requiere configuración adicional — queda como mejora post-TFM.
