---
fecha: 2026-07-15
slug: runbook-grabacion-y-chaos-fallido
promoted: true
---

## Objetivo

Noche del 14→15-jul (pasada medianoche, día del chapter). Sacar el arco chaos completo EN
CLUSTER hasta `outcome=cured` y capturar toda la evidencia para montar los slides actualizados
(foto grounding de la slide 07, Gate 8 R5, test count). El arco se torció por colisión de runs
solapados; se hizo forense en caliente, se limpió el estado y se dejó **este runbook** para
ejecutarlo a mano por la mañana con la cabeza fresca.

## Hecho

- Preparado el plan completo del arco (comandos exactos, port-forwards, capturas) contra el
  `guion_video.md` + skill `/chaos-run` + `scripts/chaos_arc.sh`.
- Verificados contra los manifiestos los nombres/puertos reales de servicio (no de memoria):
  `agent-svc:8000` + `ollama-svc:11434` (arturo-llm-test), `mattermost-svc:8065`
  (arturo-mattermost), `grafana-svc:3000` + `prometheus-svc:9090` (arturo-monitoring).
- Pre-vuelo verde: Redis sin residuos, ollama calentado (primera `/api/generate` cargó el modelo).
- Se lanzó el arco **tres veces** con solapamiento (cancelaciones + relanzamientos) → estado
  contaminado en Redis y en Alertmanager. Forense en caliente sobre los logs del agente + los
  mensajes de Mattermost.
- **Reset limpio ejecutado**: `kubectl delete` del deployment chaos + flush de las 5 familias de
  claves Redis (`aiops:cooldown:* rollback:* escalation:* incident:active:* aiops:seen:*`).
  Verificado vacío.
- Al cierre quedaba una alerta `KubePodCrashLoopBackOff` del pod muerto `-v7v87` aún `firing` en
  Alertmanager (histéresis de la ventana de 15 min); se decidió esperar a que se auto-resuelva
  antes de relanzar.
- Confirmado en código el hallazgo R5 del flujo humano (ver gotcha #6): `main.py:1483` +
  `_correlate_resolution` (`main.py:288`).

## Encontrado / gotchas

1. **Runs solapados = colisión de estado (la raíz del caos de esta noche).** Cada
   `./scripts/chaos_arc.sh` hace `kubectl apply` y recrea el deployment a 32Mi. Cancelar/relanzar
   deja en Redis contextos de escalación y de rollback de pods anteriores. Cuando un `apply` o el
   teardown borra el deployment a mitad de una ventana de rollback pendiente, el health-check
   dispara y falla con **"Rollback FAILED — deployments.apps chaos-oom-target not found"** /
   **"no_pods_found"**. → Son **artefactos de la colisión, NO el safety-net `rolled_back` limpio**.
   No usarlos como evidencia.
2. **"Duplicate alert skipped (dedup window)" es CORRECTO, no es un bug.** Es la dedup por
   fingerprint (`aiops:seen:{fp}`, TTL `dedup_window_seconds=300`, `streams.py:64/121`). El mismo
   pod ya se estaba procesando y una re-emisión de la misma alerta se descarta. Funciona como debe.
3. **El `trap EXIT` del script NO limpia todo.** El teardown borra el deployment +
   `escalation:*`/`rollback:*`/`aiops:cooldown:*`, pero **deja `incident:active:*` (R5) y
   `aiops:seen:*` (dedup)**. Entre run y run hay que flushearlas a mano o el siguiente arranca sucio.
4. **Las alertas firing sobreviven al pod.** `KubePodCrashLoopBackOff` se calcula sobre "N
   reinicios en los últimos 15 min" (kube-state-metrics); aunque borres el pod, la ventana
   histórica la mantiene `firing` unos minutos hasta que la serie se vuelve stale. Si relanzas con
   una alerta fantasma viva, el agente escala un pod que ya no existe → más confusión. **Esperar a
   que arturo-chaos quede en silencio antes de relanzar.**
5. **La regla de oro se cumplió pero el run no llegó a `cured` por interrupción.** A las 11:57
   Jay aprobó 512Mi y se aplicó ("resource requirements updated"); no curó porque el deployment se
   borró/recreó a mitad de la ventana de 300s. Confirmado: **512Mi cura, pero solo si NO tocas nada
   ~5 min tras aprobar.** El cap `memory_exceeds_2x` (512 > 2×32=64) bloquea el auto y fuerza la
   escalación con doble botón — comportamiento de diseño (safety cap ≤2×, el humano puede
   sobrepasarlo).
6. **[IMPORTANTE — corrige un overclaim] El arco con aprobación humana NO regenera la serie R5**
   `aiops_incident_resolution_seconds`. Al aprobar en Mattermost, `main.py:1483` hace
   `pop_active_incident(fingerprint)` (para que el veredicto del rollback, más fuerte, sea dueño del
   outcome). Cuando luego llega el `resolved` de Alertmanager, `_correlate_resolution`
   (`main.py:288`) hace `pop` → **miss** → NO emite `aiops_incident_resolution_seconds`
   (`INCIDENT_RESOLUTION_COUNTER.labels(correlated="miss")`). La métrica solo se emite en un "hit":
   cuando un `resolved` correlaciona con un incidente **aún indexado** (indexado en escalate/diagnosis
   con `awaits_verdict=False` y todavía no aprobado). El 92.47s del 07-13 salió de un `resolved`
   transitorio que pegó con la entrada aún viva durante los ciclos OOM, no del approve. → **Para el
   Gate 8: la evidencia fuerte del flujo humano es `aiops_feedback_verdict_total{outcome="cured"}`
   (el counter del veredicto de rollback) + MTTD/MTTR de chaos, no la serie R5.** R5 se captura
   "si sale" (ver Fase 8), no se promete.
7. **El LLM en frío tardó `duration=194341ms` (~194s).** Silencio normal durante el arco. Calentar
   ollama por port-forward (el pod no tiene `curl`) reduce, pero el primer diagnóstico sigue siendo
   lento en CPU. No confundir con "colgado".
8. **Ctrl-C sobre el arco dispara el teardown limpio** (visto: "TEARDOWN (trap EXIT,
   exit_code=130)", deployment borrado, Redis del run limpio). Fiable, pero recuerda gotcha #3.

## Decisiones + por qué

- **Reset total + un solo arco disciplinado, en vez de salvar la maraña.** El estado solapado no
  se desenreda limpio de forma fiable; una pizarra limpia + un run sin tocar llega antes a un
  `cured` inequívoco que intentar rescatar 3 runs mezclados.
- **Esperar el silencio de Alertmanager antes de relanzar** (gotcha #4): evita escalaciones de
  pods fantasma que reintroducen la ambigüedad "¿cuál apruebo?".
- **Dejar el cierre para la mañana si se vuelve a torcer.** Es el día del chapter y la fatiga fue
  la causa de los solapamientos. Ya hay material de respaldo parcial (doble botón, logs de
  grounding, una aprobación aplicada); un run limpio con la cabeza fresca es más barato que forzarlo
  de madrugada.
- **Gate 8 reencuadrado** (gotcha #6): el entregable del flujo humano es el counter de veredicto
  `cured`, no la serie R5. Honestidad > foto bonita.

## Material parcial ya capturable de esta noche (respaldo)

- Escalación con **doble botón** en MM (Opción A ×2=64Mi / Opción B modelo=512Mi) → C3.
- Escalación OOM con botón único 512Mi **aprobada** → "deployment.apps/chaos-oom-target resource
  requirements updated" (human-in-the-loop aplicando de verdad).
- Logs de grounding: `snapshot gathered` (`grounded=1.0`, `current_value=32Mi`,
  `last_state_reason=OOMKilled`, `restart_count=2`) + cap `memory_exceeds_2x` → C2 + historia de safety.
- **NO** hay un `cured` limpio de un solo arco todavía. El `rollback FAILED` de esta noche es
  artefacto de colisión → **no usarlo como evidencia**.

---

# RUNBOOK GRABACIÓN — ejecutar a mano, de arriba abajo

> Objetivo: un arco limpio hasta `outcome=cured` + todas las capturas. **Un solo arco a la vez.
> Tras aprobar, NO tocar nada ~5 min.** Entre intentos: repetir FASE 3 completa.

## FASE 0 — Salud del cluster (sin port-forward)

Nodos guaranteed arriba (el 14-jul hubo churn/preemption — verificar que no se está recreando):
```
kubectl get nodes -l guaranteed=true
```
Pods sanos en los namespaces del arco:
```
kubectl get pods -n arturo-llm-test -o wide
```
```
kubectl get pods -n arturo-monitoring
```
```
kubectl get pods -n arturo-mattermost
```
Imagen del agente == `aiops-agent:2ac3c5d`:
```
kubectl get deploy agent -n arturo-llm-test -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```
startupProbe aplicada (fix del incidente de liveness; si sale VACÍO, aplícala):
```
kubectl get deploy agent -n arturo-llm-test -o jsonpath='{.spec.template.spec.containers[0].startupProbe}{"\n"}'
```
→ si vacío:
```
kubectl apply -f k8s/deployment-agent.yaml
```
```
kubectl rollout status deploy/agent -n arturo-llm-test --timeout=300s
```
Readyz 200 + RESTARTS estable (AGE > ~5 min, sin subir):
```
kubectl exec -n arturo-llm-test deploy/agent -- python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/readyz',timeout=5).status)"
```
```
kubectl get pods -n arturo-llm-test -l app=agent
```

## FASE 1 — Port-forwards (5 terminales dedicadas, déjalas abiertas todo el run)
```
kubectl port-forward -n arturo-llm-test svc/agent-svc 8000:8000
```
```
kubectl port-forward -n arturo-llm-test svc/ollama-svc 11434:11434
```
```
kubectl port-forward -n arturo-mattermost svc/mattermost-svc 8065:8065
```
```
kubectl port-forward -n arturo-monitoring svc/grafana-svc 3000:3000
```
```
kubectl port-forward -n arturo-monitoring svc/prometheus-svc 9090:9090
```
(Mattermost en http://localhost:8065 · Grafana en http://localhost:3000 · Prometheus en http://localhost:9090)

## FASE 2 — Calentar ollama (evita que el 1er diagnóstico cargue el modelo en frío)
```
curl -s http://localhost:11434/api/generate -d '{"model":"qwen2.5:1.5b","prompt":"ping","stream":false}' | head -c 200; echo
```
(debe devolver texto = modelo cargado; si sale vacío, repite una vez)

## FASE 3 — Pizarra limpia (OBLIGATORIA antes de CADA arco)
Borra el deployment chaos (idempotente) + flushea las 5 familias de claves (incluidas las 2 que el
trap NO limpia):
```
kubectl delete deployment -n arturo-chaos chaos-oom-target --ignore-not-found
```
```
kubectl exec -n arturo-llm-test deploy/redis -- sh -c "for p in 'aiops:cooldown:*' 'rollback:*' 'escalation:*' 'incident:active:*' 'aiops:seen:*'; do redis-cli --scan --pattern \$p | xargs -r redis-cli del; done"
```
Verifica Redis vacío (no debe imprimir NADA):
```
kubectl exec -n arturo-llm-test deploy/redis -- sh -c "for p in 'aiops:cooldown:*' 'rollback:*' 'escalation:*' 'incident:active:*' 'aiops:seen:*'; do redis-cli --scan --pattern \$p; done"
```
**Espera a que arturo-chaos NO tenga alertas `firing`** (gotcha #4 — el fantasma tarda unos minutos):
```
curl -s http://localhost:9090/api/v1/alerts | python3 -m json.tool | grep -iE "alertname|state|arturo-chaos"
```
→ relanza solo cuando NO aparezca ningún `"state": "firing"` con `namespace: arturo-chaos`.

## FASE 4 — Ventanas de observación (2 terminales)
```
kubectl logs -n arturo-llm-test -l app=agent -f
```
```
kubectl get pods -n arturo-chaos -w
```

## FASE 5 — pytest en paralelo (no necesita cluster; confirma el "696" del deck)
```
python3 -m pytest agent/tests/ -q 2>&1 | tail -5
```
→ anota el número. Si ≠ 696, mañana toca `build_demo_v3.py:43` y regenerar el deck.

## FASE 6 — Lanzar el arco (¡y NO tocarlo!)
```
./scripts/chaos_arc.sh
```
- ~194 s de silencio (LLM en CPU) = **normal**. No hagas Ctrl-C, no lances otro arco.
- Cuando salga la escalación en Mattermost (#alerts): **aprueba el comando de 512Mi (valor del
  modelo)**. Si hay doble botón, es la **Opción B**. **Nunca apruebes 64Mi** (el ×2 del motor →
  `rolled_back` con este manifiesto).
- Empareja el pod de la escalación con el pod VIVO que veas en `kubectl get pods -n arturo-chaos -w`.
  Si el nombre no coincide, es un fantasma → no lo apruebes.
- Tras aprobar: **NO toques nada ~5 min.** La ventana de rollback (300 s) tiene que cerrar con el
  pod sano → en los logs saldrá `outcome=cured` y `Persisted incident ... (outcome=cured)`.

## FASE 7 — Capturas (en el momento, no "luego")

| # | Qué capturar | Dónde | Alimenta |
|---|---|---|---|
| C1 | Pod chaos en `OOMKilled`/`CrashLoopBackOff` | terminal `get pods -w` | Escena 2 (el fallo) |
| C2 | Logs: `snapshot gathered` + `current_value=32Mi` + `grounded=1.0` + `CLUSTER FACTS` + cap `memory_exceeds_2x` | terminal logs | **Escena 3 + reemplaza foto slide 07** |
| C3 | Escalación con doble botón `approve_engine`(64Mi)/`approve_model`(512Mi) | Mattermost | Escena 4 (human-in-the-loop) |
| C4 | Confirmación tras aprobar el modelo (512Mi) + "resource requirements updated" | Mattermost | Escena 4 |
| C5 | Logs: patch `kubectl set resources` + `outcome=cured` + re-upsert R2 a ChromaDB | terminal logs | Escena 5 (veredicto) |
| C6 | Salida del counter `feedback_verdict{outcome="cured"}` (comando Fase 8) | terminal | Escena 5 / evidencia fuerte |
| C7 | Grafana: dashboard Chaos + fila Cola (`aiops_queue_*`) del Overview | Grafana | Escena 6 (evidencia) |

## FASE 8 — Evidencia numérica (tras `cured`)

Veredicto (evidencia FUERTE del flujo humano — esto es lo que va al slide):
```
kubectl exec -n arturo-llm-test deploy/agent -- python -c "import urllib.request;print([l for l in urllib.request.urlopen('http://localhost:8000/metrics').read().decode().splitlines() if 'feedback_verdict' in l])"
```
MTTD/MTTR de chaos:
```
kubectl exec -n arturo-llm-test deploy/agent -- python -c "import urllib.request;print([l for l in urllib.request.urlopen('http://localhost:8000/metrics').read().decode().splitlines() if 'aiops_chaos' in l])"
```
R5 resolution (**puede salir vacío/`miss` en el flujo con aprobación humana — gotcha #6**; captúrala
solo si `count>0`):
```
kubectl exec -n arturo-llm-test deploy/agent -- python -c "import urllib.request;print([l for l in urllib.request.urlopen('http://localhost:8000/metrics').read().decode().splitlines() if 'incident_resolution' in l])"
```
→ Si `aiops_incident_resolution_seconds_count > 0`, saca la foto del panel Grafana R5. Si sale
`miss`/vacío, NO es un fallo: el veredicto de rollback es dueño del outcome (usa C6 como evidencia).

## FASE 9 — Teardown = último gate del run
El trap del script borra el deployment + `escalation/rollback/cooldown`. Verifícalo y limpia lo que
el trap NO toca:
```
kubectl get deployment -n arturo-chaos
```
```
kubectl exec -n arturo-llm-test deploy/redis -- sh -c "for p in 'aiops:cooldown:*' 'rollback:*' 'escalation:*' 'incident:active:*' 'aiops:seen:*'; do redis-cli --scan --pattern \$p | xargs -r redis-cli del; done"
```
(el primero debe estar vacío / "No resources found"; el segundo deja Redis limpio para el siguiente)

## Reglas de aborto (fatiga = errores)
- **Nunca 2 arcos a la vez. Nunca Ctrl-C a mitad de ventana de rollback.**
- Entre intento e intento: **FASE 3 completa** (delete + flush + esperar alertas quiet).
- Si a la 2ª vez se tuerce → **PARA**. Hay material de respaldo suficiente; no quemes la mañana del
  chapter. Se puede grabar con el material parcial + una toma limpia posterior.

## Siguiente (para /start)

- Ejecutar este runbook de arriba abajo → arco limpio `cured` + capturas C1–C7.
- Con las capturas frescas, actualizar los slides desactualizados: **foto slide 07** (grounding,
  con C2), **Gate 8** (C6 counter de veredicto `cured` como evidencia fuerte; R5 solo si salió
  `hit`), **test count** (Fase 5, cuadrar el 696 en `build_demo_v3.py:43`).
- `/ensayo` hostil con munición nueva: incidente nocturno (self-target 384Mi→10Gi), doble botón
  ya desplegado, y el matiz honesto de que el flujo humano cede el outcome al veredicto del rollback
  (no infla la métrica R5).
- Vault end-session (tabla Vault Impact del doc `2026-07-14-incidente-liveness-coldstart.md`).
