# CI/CD — Cloud Build + Artifact Registry

## Pipeline de build

Archivo: `cloudbuild.yaml`

```
gcloud builds submit → Docker build → Push a Artifact Registry
                                        ├── :latest
                                        └── :$COMMIT_SHA
```

## Artifact Registry

| Propiedad | Valor |
|---|---|
| Región | europe-southwest1 |
| Repositorio | aiops-agent |
| Imagen | aiops-agent |
| URL completa | `europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent/aiops-agent` |

## Versionado de imágenes

Cada build genera dos tags:
- `:latest` — siempre apunta a la última versión
- `:$COMMIT_SHA` — tag inmutable con el SHA del commit de Git

Esto permite hacer rollback a una versión anterior:
```bash
# Rollback rápido
kubectl set image deployment/agent agent=.../aiops-agent:abc1234 -n arturo-llm-test
```

## Ejecución manual del build

```bash
cd llm-kubernetes-ollama

# $COMMIT_SHA no se rellena automáticamente en builds manuales.
# Hay que pasarlo como sustitución:
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=COMMIT_SHA=$(git rev-parse --short HEAD)
```

## Dockerfile (`agent/Dockerfile`)

- Base: `python:3.11-slim`
- Usuario no-root (`appuser`) — buena práctica de seguridad
- Capa de dependencias cacheada (COPY requirements.txt antes que el código)
- Solo copia `main.py` (no tests ni dev dependencies)

## Deploy tras build

```bash
# Aplicar manifiestos actualizados
kubectl apply -f k8s/deployment-agent.yaml

# Forzar pull de la nueva imagen si usas :latest
kubectl rollout restart deployment/agent -n arturo-llm-test

# Verificar
kubectl rollout status deployment/agent -n arturo-llm-test
```

---

## Errores conocidos y soluciones

### "invalid image name ... aiops-agent:" (tag vacío)
**Causa**: `$COMMIT_SHA` está vacío porque el build se ejecutó manualmente
sin sustitución. Solo se rellena automáticamente en builds disparados por triggers.
**Solución**: pasar `--substitutions=COMMIT_SHA=$(git rev-parse --short HEAD)`

### Build tarda mucho (>2 min)
**Causa probable**: la capa de `pip install` no está cacheada porque cambió requirements.txt.
**Solución**: no modificar requirements.txt innecesariamente. Cloud Build no cachea
capas Docker entre builds por defecto — considerar `kaniko` para builds con caché.

### "permission to write logs to Cloud Logging"
**Causa**: la service account de Cloud Build no tiene el rol `roles/logging.logWriter`.
**Impacto**: solo afecta a logs, el build funciona igualmente.
**Solución**: `gcloud projects add-iam-policy-binding uniovi-ai-infra-agent --member=serviceAccount:... --role=roles/logging.logWriter`
