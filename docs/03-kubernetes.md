# Infraestructura Kubernetes (GKE)

## Cluster

| Propiedad | Valor |
|---|---|
| Nombre | ai-infra-agent |
| Proyecto GCP | uniovi-ai-infra-agent |
| Zona | europe-southwest1-a |
| Tipo de nodo | e2-standard-2 (Spot en Fase 1, se migrará a **Standard** en Fase 2 para albergar ChromaDB) |
| Nodos | 2 (Frecuentemente 1 por preemption spot en Fase 1) |
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
| `networkpolicy.yaml` | NetworkPolicy (2 políticas) | Segmentación de tráfico entre pods |

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

Fichero: `k8s/networkpolicy.yaml` — dos políticas de ingress:

1. **`ollama-allow-agent-only`**: pods de Ollama solo aceptan tráfico desde pods con `app: agent` en el port 11434.
2. **`agent-allow-apache-only`**: pods del agent solo aceptan tráfico desde pods con `app: apache` en el port 8000.

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
  readOnlyRootFilesystem: true  # filesystem de solo lectura
  allowPrivilegeEscalation: false
  capabilities:
    drop: [ALL]             # elimina todas las capabilities Linux
```

Se monta un volumen `emptyDir` en `/tmp` porque `readOnlyRootFilesystem: true`
impide que uvicorn escriba ficheros temporales en el filesystem del contenedor.

**Nota sobre Ollama**: la imagen oficial de Ollama requiere root (escribe en `/root/.ollama`).
Se aplica `allowPrivilegeEscalation: false` y `capabilities.drop: [ALL]` como mitigación.

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

### Port-forward: "address already in use"
**Causa**: un port-forward anterior sigue corriendo en background.
**Solución**: `pkill -f "port-forward"` y relanzar.

### Nodo spot reclamado (1 nodo en vez de 2)
**Causa**: los nodos spot son preemptibles — Google los reclama cuando necesita capacidad.
El cluster-autoscaler puede no conseguir reemplazarlo si no hay capacidad spot en la zona.
**Solución**: con los resources ajustados actuales, todo cabe en 1 nodo.
Si necesitas forzar 2 nodos: `gcloud container clusters resize ai-infra-agent --node-pool spot-e411 --num-nodes 2 --zone europe-southwest1-a`
