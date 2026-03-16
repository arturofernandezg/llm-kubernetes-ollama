# Arquitectura del sistema

## Visión general

Agente de IA de ciclo completo para automatizar despliegues de infraestructura GCP
en MasOrange/Telecable. TFG/TFM — rol: ingeniero AIOps.

## Flujo objetivo (zero-touch)

```
Slack message
    │
    ▼
FastAPI Agent ──► Ollama/tinyllama (extracción de parámetros)
    │
    ▼
Terraform Generator ──► genera .tf a partir de módulo corporativo
    │
    ▼
GitHub API ──► crea rama + commit + Pull Request
    │
    ▼
Validation Agent ──► revisa PR vs petición original + políticas internas
    │
    ▼
GitHub Actions ──► terraform plan → aprobación → terraform apply
```

## Flujo actual (Fase 1)

```
[Port-forward / curl / generate_tf.py]
         │
         ▼
   agent-svc:8000      ← FastAPI (2 réplicas, probes, shared httpx client)
         │
         ▼
   ollama-svc:11434    ← Ollama (1 réplica, PVC 20Gi, PDB)
         │
         ▼
     ollama-pvc         ← Modelos persistidos en disco
```

## Componentes

| Componente | Tecnología | Ubicación | Réplicas |
|---|---|---|---|
| Agente | Python 3.11, FastAPI, httpx, Pydantic v2 | `agent/main.py` | 2 |
| LLM | Ollama 0.17.5 (tinyllama) | Pod K8s | 1 |
| TF Generator | Python stdlib (urllib) | `generate_tf.py` | CLI local |
| Build pipeline | Google Cloud Build | `cloudbuild.yaml` | — |
| Infra | GKE (e2-standard-2 spot, 2 nodos) | `k8s/` | — |

## Decisiones de arquitectura

- **Ollama en K8s** (no API externa): control total, sin costes por token,
  datos no salen del cluster. Trade-off: modelos pequeños, menos precisión.
- **PVC ReadWriteOnce**: los modelos sobreviven a reinicios. Trade-off:
  no se puede escalar Ollama a >1 réplica sin migrar a StatefulSet o GCS.
- **Nodos spot**: reduce costes ~60-70%. Trade-off: los pods pueden ser
  desalojados. Mitigado con PDB y PVC.
- **httpx compartido en lifespan**: reutiliza pool de conexiones TCP,
  evita overhead de crear/cerrar cliente por request.
- **Probes separadas**: /healthz (liveness, sin deps) y /readyz (readiness,
  verifica Ollama). Evita crash loops cuando Ollama está down.

## Red interna

Todos los servicios usan ClusterIP (no expuestos a internet):
- `agent-svc:8000` → agente FastAPI
- `ollama-svc:11434` → API Ollama
- `apache-svc:80` → servidor web de validación de red

Sin Cloud NAT: los pods no tienen salida a internet. Modelos se cargan
manualmente vía `kubectl exec`.
