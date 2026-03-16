# Infraestructura Kubernetes (GKE)

## Cluster

| Propiedad | Valor |
|---|---|
| Nombre | ai-infra-agent |
| Proyecto GCP | uniovi-ai-infra-agent |
| Zona | europe-southwest1-a |
| Tipo de nodo | e2-standard-2 (spot) |
| Nodos | 2 |
| Namespace | arturo-llm-test |
| K8s version | 1.35.1-gke |

## Manifiestos (`k8s/`)

| Archivo | Recurso | Notas |
|---|---|---|
| `deployment-ollama.yaml` | Deployment ollama (1 réplica) | PVC, probes, OLLAMA_KEEP_ALIVE=24h |
| `service-ollama.yaml` | ClusterIP :11434 | Solo accesible internamente |
| `pvc-ollama.yaml` | PVC 20Gi ReadWriteOnce | Modelos LLM persistidos |
| `pdb-ollama.yaml` | PodDisruptionBudget (minAvailable: 1) | Protege durante drains |
| `deployment-agent.yaml` | Deployment agent (2 réplicas) | Probes /healthz y /readyz |
| `service-agent.yaml` | ClusterIP :8000 | Solo accesible internamente |
| `deployment-apache.yaml` | Deployment apache (1 réplica) | Validación de red |
| `service-apache.yaml` | ClusterIP :80 | Validación de red |

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

## Carga de modelos

Sin Cloud NAT, los pods no tienen internet. Los modelos se cargan manualmente:

```bash
# Desde una máquina con Ollama local instalado:
kubectl exec -i <pod-ollama> -n arturo-llm-test -- bash -c \
  "mkdir -p /root/.ollama/models/blobs && cat > /root/.ollama/models/blobs/<hash>" \
  < ~/.ollama/models/blobs/<hash>
```

## Modelos disponibles

| Modelo | Tamaño | RAM | Estado |
|---|---|---|---|
| tinyllama | 637 MB | ~1 GB | Operativo |
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
