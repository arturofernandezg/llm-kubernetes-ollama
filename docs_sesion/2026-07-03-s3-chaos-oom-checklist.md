---
fecha: 2026-07-03
slug: s3-chaos-oom-checklist
promoted: true
tipo: checklist-ejecutable (S3 del sprint chapter — no es bitácora; la bitácora se escribe al ejecutarlo)
---

# S3 — Chaos OOM del arco completo: checklist ejecutable

**Qué valida de una tacada**: Eje A (seal desde snapshot, sin `NotFound`), métrica de
enrichment, confidence grounded en Mattermost, cooldown F-01 al segundo episodio,
veredicto R2 re-upsertado en ChromaDB y etiqueta `FAILED FIX` (R2·3).

**Física del experimento**: `chaos-oom-target` tiene limit **32Mi** y el stress pide
**100M** → OOMKilled. El auto-bump 2× lo sube a **64Mi**, que SIGUE sin bastar → el
health-check a los 300s ve restarts → **rollback a 32Mi** → veredicto `rolled_back`.
La alerta re-dispara (pod nuevo tras el rollout ⇒ esquiva la dedup) → **cooldown F-01
escala** en vez de re-parchear. Es decir: el camino "negativo" completo sale gratis del
manifiesto tal cual está. El camino "positivo" (veredicto `cured`) es el escenario B.

---

## Fase 0 — Prerequisitos (S1 + S2): comandos copy-paste

> Todos one-line. Orden estricto: **los secrets se recuperan ANTES de tocar el
> deployment** — el pod vivo (pre-incidente) es la única copia de la URL real.

### S1 — Consolidar código

```
python -m pytest agent/tests/ -q
```
```
git add -A && git commit -m "feat(rag): F4 bucle aprendizaje real (R2 veredicto + R2.3 outcome-aware + R3 higiene query + gate E4) + fixes review F-01/F-04/F-05 + CI GitHub Actions + roadmap sprint chapter"
```
```
git push origin main
```
- [ ] Suite verde (esperable ~613 funciones; apuntar el número real para reconciliar docs)
- [ ] CI de GitHub Actions verde en el push (primer estreno del workflow)

### S2·1 — Recuperar secrets (ANTES de reiniciar nada)

Comprobar que el pod vivo es el de antes del incidente (edad > 1 día aprox.):
```
kubectl get pods -n arturo-llm-test -l app=agent
```
Rescatar la URL real del webhook desde el env del pod vivo y verla:
```
RECOVERED_URL=$(kubectl exec -n arturo-llm-test deploy/agent -- printenv MATTERMOST_WEBHOOK_URL) && echo "$RECOVERED_URL"
```
> ⚠️ Si sale con `<...>` o vacío, el pod ya reinició con el secret pisado: recuperar el
> token en Mattermost UI → Integrations → Incoming Webhooks y montar la URL a mano:
> `http://mattermost-svc.arturo-mattermost.svc.cluster.local:8065/hooks/<token>`.

Restaurar el secret del webhook con la URL rescatada:
```
kubectl create secret generic mattermost-webhook --from-literal=url="$RECOVERED_URL" -n arturo-llm-test --dry-run=client -o yaml | kubectl apply -f -
```
Token del slash command (Mattermost UI → Integrations → Slash Commands → `/aiops` → Token). Pegarlo aquí:
```
MM_TOKEN="PEGAR-TOKEN-REAL-AQUI"
```
Recrear `agent-secrets` (HMAC nuevo aleatorio + token real). **P0·2**: sin estas claves,
con `DRY_RUN=false` los botones rechazan (fail-closed):
```
kubectl create secret generic agent-secrets --from-literal=webhook-secret="$(openssl rand -hex 32)" --from-literal=mm-command-token="$MM_TOKEN" -n arturo-llm-test --dry-run=client -o yaml | kubectl apply -f -
```
Verificar que NO queda ningún placeholder (ninguno debe contener `<`):
```
kubectl get secret mattermost-webhook -n arturo-llm-test -o jsonpath='{.data.url}' | base64 -d; echo; kubectl get secret agent-secrets -n arturo-llm-test -o jsonpath='{.data.mm-command-token}' | base64 -d; echo
```
- [ ] URL real restaurada (termina en el token del hook, sin `<`)
- [ ] `mm-command-token` real (sin `<`)

### S2·2 — Build + deploy de la imagen nueva

> `cb2d1db` quedó atrás: la imagen a desplegar es la del commit de S1. El tag es
> SIEMPRE el short SHA del commit (nunca el build ID — da ImagePullBackOff).

```
gcloud builds submit --config cloudbuild.yaml --substitutions=COMMIT_SHA=$(git rev-parse --short HEAD) .
```
Aplicar manifiestos que podrían estar pendientes del 07-02 (idempotente):
```
kubectl apply -f k8s/rbac.yaml && kubectl apply -f k8s/prometheus.yaml
```
Deploy + esperar el rollout:
```
kubectl -n arturo-llm-test set image deployment/agent agent=europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent/aiops-agent:$(git rev-parse --short HEAD) && kubectl -n arturo-llm-test rollout status deployment/agent
```
- [ ] Build SUCCESS (los tests son gate dentro del build)
- [ ] Rollout OK, pod `Running` y `READY 1/1` (readyz = Redis OK)
- [ ] Nota post-sesión: actualizar el tag en `k8s/deployment-agent.yaml` en el siguiente commit (queda apuntando a `da7aafb`)

---

## Fase 1 — Pre-vuelo (5 min, aborta aquí si algo falla)

Port-forwards, cada uno en su terminal:
```
kubectl port-forward -n arturo-llm-test svc/agent-svc 8000:8000
```
```
kubectl port-forward -n arturo-monitoring svc/prometheus-svc 9090:9090
```
```
kubectl port-forward -n arturo-monitoring svc/grafana-svc 3000:3000
```
```
kubectl port-forward -n arturo-mattermost svc/mattermost-svc 8065:8065
```
Checks (cada uno debe salir como se indica):

| # | Check | Comando | Esperado |
|---|---|---|---|
| P1 | Imagen desplegada = commit S1 | `kubectl get deploy agent -n arturo-llm-test -o jsonpath='{.spec.template.spec.containers[0].image}'; echo` | tag = short SHA de S1 |
| P2 | RBAC del grounding en arturo-chaos | `kubectl auth can-i get replicasets -n arturo-chaos --as=system:serviceaccount:arturo-llm-test:default` | `yes` |
| P3 | Readyz (Redis vivo) | `curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/readyz` | `200` |
| P4 | Métricas nuevas pre-inicializadas | `curl -s localhost:8000/metrics \| grep -cE 'aiops_(enrichment\|feedback_verdict)_total'` | > 0 |
| P5 | Runbooks en ChromaDB | `kubectl exec -n arturo-llm-test deploy/agent -- python -c "import chromadb; c=chromadb.HttpClient(host='chromadb-svc', port=8000); print(c.get_collection('runbooks').count())"` | `16` |
| P6 | Sin cooldown residual | `kubectl exec -n arturo-llm-test deploy/redis -- redis-cli ttl "aiops:cooldown:arturo-chaos/chaos-oom-target"` | `-2` (no existe) |
| P7 | Baseline de incidents (apuntar N) | `kubectl exec -n arturo-llm-test deploy/agent -- python -c "import chromadb; c=chromadb.HttpClient(host='chromadb-svc', port=8000); print(c.get_collection('incidents').count())"` | apuntar N₀ |

Logs del agente en seguimiento (terminal aparte, se queda abierto todo el experimento):
```
kubectl logs -n arturo-llm-test deploy/agent -f --tail=20
```

---

## Fase 2 — Escenario A: arco negativo (rollback → FAILED FIX → cooldown)

Lanzar (el runner mide MTTD/MTTR solo):
```
bash scripts/chaos.sh oom
```

### Checkpoints en orden cronológico (~15–20 min total)

| # | Etapa (≈t) | Evidencia | Comando / dónde mirar | Esperado |
|---|---|---|---|---|
| A1 | Alerta dispara (~1–3 min) | `KubePodOOMKilled` firing | Prometheus `localhost:9090/alerts` | firing, `namespace=arturo-chaos` |
| A2 | Ingesta encolada | métrica cola | `curl -s localhost:8000/metrics \| grep aiops_queue_enqueued` | +1 |
| A3 | **Enrichment (Eje A)** | métrica + log | `curl -s localhost:8000/metrics \| grep 'aiops_enrichment_total{outcome="gathered"}'` y en logs `seal_proposed_action: sealed from cluster snapshot` | `gathered` +1 · **CERO** `workload_unresolved` |
| A4 | Retrieval R1 filtrado | runbook OOMKilled recuperado | logs del retrieval (error_class=`OOMKilled`) | sin fallback semántico |
| A5 | **Auto dispara** | log de la cascada | logs: `Rule 5 bypassed: structured remediation, engine bound supersedes model risk` | AUTO_REMEDIATE |
| A6 | Cooldown adquirido (F-01) | clave Redis | `kubectl exec -n arturo-llm-test deploy/redis -- redis-cli ttl "aiops:cooldown:arturo-chaos/chaos-oom-target"` | TTL ~600 bajando |
| A7 | **Patch sellado, sin NotFound** | limit duplicado | `kubectl get deploy chaos-oom-target -n arturo-chaos -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'; echo` | `64Mi` · en logs NINGÚN `NotFound`/`unparseable` |
| A8 | Rollback durable (P0·3) | contexto en Redis | `kubectl exec -n arturo-llm-test deploy/redis -- redis-cli keys "rollback:*"` | 1 clave |
| A9 | Ingesta provisional (R2) | doc en ChromaDB | comando "inspección incidents" (abajo) | doc nuevo `outcome=auto_pending`, `error_class=OOMKilled` |
| A10 | **Mattermost grounded** | mensaje del canal | MM `localhost:8065` | comando determinista del motor + "Confidence: N% _(grounded del cluster; el modelo dijo M%)_" |
| A11 | Rollback ejecuta (+300s) | revert + métrica | `kubectl get deploy chaos-oom-target -n arturo-chaos -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'; echo` y `curl -s localhost:8000/metrics \| grep aiops_remediation_rollback_total` | vuelve a `32Mi` · rollback +1 · mensaje MM de rollback |
| A12 | **Veredicto R2 re-upsertado** | métrica + doc | `curl -s localhost:8000/metrics \| grep aiops_feedback_verdict_total` + inspección incidents | `{outcome="rolled_back"}` = 1 · el MISMO doc_id ahora `outcome=rolled_back` (no hay doc duplicado: count = N₀+1) |
| A13 | **2º episodio → cooldown escala (F-01)** | la alerta re-firing (pod nuevo) NO re-parchea | logs: `Auto-remediation blocked by workload cooldown, escalating` (`reason_code=workload_cooldown`) | ESCALATE con botones en MM, comando determinista visible; limits SIGUEN en 32Mi |
| A14 | **FAILED FIX en contexto (R2·3)** | el 2º diagnóstico recibe el precedente etiquetado | inspección incidents (doc `rolled_back` existe y es recuperable) + en el MM del 2º episodio el diagnóstico NO repite el bump como si fuera precedente bueno | etiqueta `outcome: rolled_back — FAILED FIX` operativa |

Inspección de incidents (usar en A9/A12/A14):
```
kubectl exec -n arturo-llm-test deploy/agent -- python -c "import chromadb; c=chromadb.HttpClient(host='chromadb-svc', port=8000); r=c.get_collection('incidents').get(include=['metadatas']); [print(i,'|',m.get('outcome'),'|',m.get('error_class')) for i,m in zip(r['ids'],r['metadatas'])]"
```

**Capturas para Gate 8 mientras pasa** (no dejarlo para el final): panel Grafana Overview
(fila cola con caudal), `/alerts` de Prometheus firing, mensaje MM con confidence grounded,
mensaje MM de la escalación por cooldown, y `aiops_enrichment_total`/`aiops_feedback_verdict_total`
en el explorador de Prometheus.

### No pasa / debugging rápido

| Síntoma | Causa probable | Ver |
|---|---|---|
| `workload_unresolved` en vez de `gathered` | RBAC de replicasets no aplicado en arturo-chaos | P2 del pre-vuelo; `kubectl apply -f k8s/rbac.yaml` |
| ESCALATE con `reason_code=target_unresolved` | mismo caso — la 4.7 haciendo su trabajo | logs del seal |
| Nada llega al webhook | Alertmanager → revisar route/receiver | `kubectl logs -n arturo-monitoring deploy/alertmanager` |
| MM sin mensajes | secret del webhook mal recuperado | S2·1, verificación final |
| Botones dan 401 | `agent-secrets` sin recrear o token viejo | P0·2 es fail-closed: es el comportamiento esperado con secret malo |

---

## Fase 3 — Escenario B (opcional, +15 min): veredicto `cured`

Para el deck conviene tener TAMBIÉN un veredicto positivo (la gráfica de
`aiops_feedback_verdict_total` con `cured` y `rolled_back` cuenta la historia completa).
Bajar el hambre del stress a 40M → el bump a 64Mi SÍ cura:

Limpiar el cooldown del escenario A (o esperar 10 min):
```
kubectl exec -n arturo-llm-test deploy/redis -- redis-cli del "aiops:cooldown:arturo-chaos/chaos-oom-target"
```
Reducir el stress y re-lanzar el pod:
```
kubectl -n arturo-chaos patch deploy chaos-oom-target --type=json -p='[{"op":"replace","path":"/spec/template/spec/containers/0/args","value":["--vm","1","--vm-bytes","40M","--timeout","60"]}]'
```
- [ ] B1: auto-bump a 64Mi (como A7)
- [ ] B2: a los +300s NO hay rollback (pod sano) → mensaje MM de éxito
- [ ] B3: `aiops_feedback_verdict_total{outcome="cured"}` = 1 · doc re-upsertado a `outcome=cured`
- [ ] B4: limits se quedan en 64Mi (el fix persiste)

---

## Fase 4 — Cierre

```
bash scripts/chaos.sh cleanup
```
```
kubectl exec -n arturo-llm-test deploy/redis -- redis-cli del "aiops:cooldown:arturo-chaos/chaos-oom-target"
```
- [ ] `/log` de la sesión (resultados A1–A14 + B, MTTD/MTTR del runner, sorpresas)
- [ ] Con incidents ya poblados de outcomes reales → **R4** es el siguiente paso natural
      (re-correr `eval_retrieval` con incidents vs vacío — S4)
- [ ] Marcar S3 ✅ en `docs/07`
