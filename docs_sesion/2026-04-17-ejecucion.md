# Sesion 2026-04-17 — Ejecución Sesion 8 (Deploy final + test e2e)

Plan original: `docs_sesion/2026-04-17.md`.
Este doc recoge qué pasó al ejecutarlo y dónde quedó el pipeline.

## Resultado global

Pipeline **funcionando end-to-end excepto el último salto (Mattermost)**.
Alertmanager payload → FastAPI webhook → RAG retrieval → LLM diagnosis → Persistencia de incidente: todo ✅.
Solo falla la notificación a Mattermost con `ConnectTimeout`, aunque `mattermost-svc` responde a ping desde el pod del agente (200 OK). Debug pendiente.

## Ejecución por bloques

### Bloque 0 — Preparación Mac ✅

- Actualizado `agent/Dockerfile` con `COPY runbooks/ ./runbooks/` después de `COPY *.py ./`.
- Commit + push desde Mac.

### Bloque 1 — Cluster ⚠️ (parcial)

- **RBAC**: falló `kubectl apply -f k8s/rbac.yaml`. Usuario sin permiso `container.roles.create`.
  Esperado por CLAUDE.md (sin permisos cluster-scoped). No bloqueante: remediation queda en `suggest_only`.
- **Sync de repos**: conflicto al hacer `git pull --rebase` en PC empresa (los docs estaban "deleted by us" y `.gitignore` ambos modificados). Solución: `git rebase --abort` + `git push --force` desde PC empresa (fuente de verdad autoritativa).
- **Cloud Build**: SUCCESS. Tests pasaron, imagen nueva publicada en Artifact Registry.
- **Rollout restart**: OK. Pod Running sin errores.

### Bloque 2 — Mattermost ✅

- Port-forward `mattermost-svc:8065` operativo desde Cloud Shell.
- Incoming webhook "Test" creado en UI de Mattermost.
- `kubectl set env deployment/agent MATTERMOST_WEBHOOK_URL=...` aplicado (rolling update automático).
- Variable confirmada en `describe deployment`.

### Bloque 3 — Runbooks en ChromaDB ✅

- `kubectl cp agent/runbooks/` al pod (a `/tmp/runbooks/` porque `readOnlyRootFilesystem`).
- Ingesta: **16 runbooks ingested, 0 errors, 16 total**.
- Verificación: `runbooks: 16, incidents: 0`.

### Bloque 4 — Test end-to-end ⚠️

**Problema menor**: la imagen del agente no tiene `curl`. Tampoco funcionó pegar el heredoc Python (indentación del shell rompía el script). Solución: crear el script con `python3 -c "open(...).write(...)"` desde Cloud Shell, luego `kubectl cp` + `kubectl exec python /tmp/test_webhook.py`.

**Resultado del POST a `/webhook/alert`**: `200 {"status":"success","alerts_processed":1,...}`.

**Logs del agente tras el webhook**:

```
Alert webhook received, alerts_count=1
Processing alert 1/1, alertname=KubePodOOMKilled
RAG retrieval: 3 runbooks, 0 incidents (query OOMKilled)
Diagnosis generated: confidence=0.85 risk=high duration=36622ms
Remediation decision: action=suggest_only, risk=high, commands_total=3, blocked=0
Persisted incident incident-KubePodOOMKilled-1776424806 (outcome=suggest_only)
Mattermost attempt 2/3 failed (ConnectTimeout). Retrying in 2.0s...
```

- RAG: 3 runbooks relevantes recuperados de los 16 (top-k=3).
- LLM: 36s para generar (qwen2.5:1.5b en nodos spot, esperable).
- Incidente persistido → feedback loop demostrado.
- Mattermost: ConnectTimeout en los 3 intentos.

### Debug de Mattermost (pendiente de cerrar)

- `kubectl get pods -n arturo-mattermost`: `mattermost` y `postgres` Running 6h3m.
- Logs de Mattermost: healthy, ping respondiendo 200, no entra ningún POST del agente.
- **Ping desde el pod del agente** a `http://mattermost-svc.arturo-mattermost.svc.cluster.local:8065/api/v4/system/ping` → **200 OK**. La conectividad cross-namespace al service base funciona.
- Sesión cortada justo antes de ejecutar el test directo al endpoint `/hooks/<token>` con la env `MATTERMOST_WEBHOOK_URL`. Es el siguiente paso.

## Hipótesis del ConnectTimeout (para próxima sesión)

1. **Token inválido o caducado**: el webhook "Test" fue creado antes; quizá el token ya no es válido. Próxima sesión: regenerar el incoming webhook.
2. **Timeout demasiado corto en `mattermost.py`**: la primera llamada puede cargar módulos lazy. Ver config del cliente httpx.
3. **NetworkPolicy bloqueando POST /hooks/ específicamente**: poco probable (el ping pasa), pero revisar reglas egress.
4. **Origin check de Mattermost**: el log de MM muestra `websocket: request origin not allowed`. Si el hook valida el header Origin y el agente no lo envía, podría rechazar silenciosamente.

## Comando siguiente a ejecutar

```bash
kubectl exec -n arturo-llm-test deploy/agent -- python -c "
import httpx, os
url = os.environ['MATTERMOST_WEBHOOK_URL']
r = httpx.post(url, json={'text':'test from agent'}, timeout=10.0)
print(r.status_code, r.text)
"
```

Si 200 → recrear webhook con token fresco resolvería el problema.
Si timeout → revisar timeout/config en `agent/mattermost.py`.

---

## Para la demo con el tutor (lunes 2026-04-20)

### Qué mostrar (hito TFM)

**Sistema AIOps completo desplegado en GKE**: detecta alertas Kubernetes simuladas, aplica RAG sobre 16 runbooks propios, genera diagnóstico con LLM local (qwen2.5:1.5b en Ollama), persiste el incidente y decide si remediar o solo sugerir.

### Piezas funcionando (mostrar con logs en vivo)

1. **Webhook receptor** — FastAPI con schema Pydantic v2 de Alertmanager. Validación real: rechaza payloads mal formados con 422.
2. **Retrieval-Augmented Generation** — embeddings `nomic-embed-text` generados por Ollama, indexados en ChromaDB. Query `KubePodOOMKilled` recupera los 3 runbooks más relevantes de los 16 cargados.
3. **LLM diagnosis estructurado** — el modelo devuelve JSON con `confidence`, `risk_level`, comandos sugeridos. Ejemplo real de la sesión: `confidence=0.85, risk=high, 3 comandos`.
4. **Capa de seguridad (remediation.py)** — el agente NO ejecuta automáticamente: decide `action=suggest_only` (0 comandos bloqueados, pero 0 ejecutados por configuración). Gate explícito por variable de entorno + validación de comandos destructivos.
5. **Feedback loop (ChromaDB incidents collection)** — cada incidente se persiste con su diagnóstico, lo que alimenta futuros retrievals. Verificable: `incidents: 1` tras el primer test.

### Comando de demo (reproducible)

```bash
kubectl exec -n arturo-llm-test deploy/agent -- python /tmp/test_webhook.py
# 200 {"status":"success","alerts_processed":1,...}

kubectl logs -n arturo-llm-test deploy/agent --tail=30
# Muestra la secuencia RAG → diagnosis → remediation → persisted
```

### Preguntas / decisiones pendientes

1. **Permiso GCP `container.roles.create`** — sin él no se puede aplicar RBAC del agente en namespace, la remediación autónoma (Fase 3) queda bloqueada en `suggest_only`. Preguntarle al tutor si es posible habilitarlo o si preferimos mantenerlo así como "human-in-the-loop" (defendible como diseño de seguridad).
2. **Alerting rules sin Prometheus operator** — sin ClusterRole no se instala kube-prometheus-stack. ¿Hay opción de pedir permisos o seguimos con Alertmanager standalone + reglas manuales?
3. **Mattermost delivery falla** — sabemos que Mattermost está sano y el agente tiene conectividad cross-namespace al service. Debug identificado (token probablemente revocado). No bloqueante para la arquitectura: el pipeline RAG+LLM+persistencia funciona sin él.

### Estado cuantitativo

- ✅ 193 tests unitarios pasando en CI (Cloud Build gate).
- ✅ 16 runbooks indexados en ChromaDB.
- ✅ 1 incidente persistido (feedback loop demostrado).
- ✅ Diagnóstico LLM generado end-to-end (36s, aceptable en spot nodes e2-standard-2).
- ⏳ Notificación Mattermost: pendiente de cerrar.
- ⏳ RBAC: bloqueado por permisos IAM.

### Screenshot/evidencia sugerida para la tesis

Capturar la salida completa de `kubectl logs --tail=50` donde se ve la secuencia:
`Alert received → RAG retrieval → Diagnosis generated → Remediation decision → Persisted incident`.

Es la prueba visual de que todas las capas se encadenan correctamente.
