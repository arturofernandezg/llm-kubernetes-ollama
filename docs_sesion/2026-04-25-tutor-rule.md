# Sesión 2026-04-25 — Condición del tutor para auto-remediación

## Objetivo
Implementar la condición exacta que pidió el tutor:
> "auto si memory.limits nuevo ≤ 2× actual; bloquear si implica reinicio del pod"

La sesión anterior (2026-04-24) dejó `REMEDIATION_ENABLED=true, DRY_RUN=true` con una regla genérica `risk=low + confidence≥0.8`. Esta sesión la endurece con la condición real, antes de hablar con el tutor de pasar a `DRY_RUN=false`.

---

## Decisiones de diseño y por qués

### D1 — Bloqueo conservador de TODA acción que reinicia pods

**Decisión**: regla 4.5 bloquea CUALQUIER comando MUTATING que implique reinicio, sin excepción. Rolling update con N replicas incluido.

**Por qué**: el tutor dijo "bloquear si implica reinicio del pod". Rolling update (`kubectl set resources deployment`) recrea pods uno a uno — el pod original sí se reinicia. La interpretación más segura es que "reinicio del pod" incluye esto. Si el tutor quiere una excepción para HA (≥2 réplicas con rolling update sin downtime), que la confirme explícitamente — no asumir.

**Consecuencia conocida**: en la práctica, con los runbooks actuales, OOM → LLM → `set resources deployment` → ESCALATE siempre. El `auto_remediate` solo se dispara si el LLM genera comandos exclusivamente `label/annotate/get/describe`. Eso es correcto para el dry-run de esta fase.

**Alternativa descartada**: bloquear solo si deployment tiene `spec.replicas < 2` + leer el cluster. Descartado porque añade I/O async a `decide_action` (que hoy es pura), y introduce un race condition (replicas en el cluster vs replicas en el momento de la decisión).

**Alternativa descartada 2**: in-place pod resize (`kubectl patch pod --subresource resize`, k8s 1.27+). Es el camino técnico correcto para "aumentar memoria sin reiniciar el pod". Se deja para cuando el tutor lo confirme.

---

### D2 — Fuente de datos: extender schema del LLM, no regex sobre el string del comando

**Decisión**: añadir `proposed_action: {kind, name, namespace, container, field, current_value, new_value}` al output del LLM. La regla 4.6 (≤2×) lee de esa estructura, no de regex sobre el comando.

**Por qué**: regex sobre `--limits=memory=X` en `kubectl set resources` es frágil. El LLM puede generar `kubectl patch deployment ... -p '{"spec":...}'` en JSON, YAML, o variantes de sintaxis kubectl. Parsear todos los formatos posibles es un agujero de mantenimiento. Con `proposed_action` estructurado, la regla siempre lee `current_value` y `new_value` — el LLM es responsable de poblarlos.

**Trade-off aceptado**: `qwen2.5:1.5b` puede no poblar `proposed_action` correctamente (modelos pequeños tienden a ignorar nuevos campos del schema). Mitigación: la regla 4.6 es **fail-open** si `proposed_action` está ausente o malformado — cae al legacy path (risk/confidence). Solo cuando el LLM lo puebla se aplica el check de 2×. Esto implica que la protección de 2× no es garantizada con el modelo actual.

**Alternativa más segura**: forzar `proposed_action` siempre en el post-parse, fetching el valor actual del cluster vía `kubectl get`. Descartado porque `decide_action` quedaría async con I/O de red, rompe la separación de concerns, y añade latencia al pipeline hot-path.

---

### D3 — Separación BLOCKED_PATTERNS vs _RESTART_PATTERNS

**Por qué dos listas**: los `BLOCKED_PATTERNS` clasifican comandos como `CommandSafety.BLOCKED` en la capa de validación sintáctica. Un comando BLOCKED escala en la regla 3 (sin llegar a la regla 4.5). Los `_RESTART_PATTERNS` capturan los comandos que son MUTATING (permitidos por clasificación) pero que el tutor considera inaceptables porque reinician pods.

`kubectl delete pod` está en `BLOCKED_PATTERNS` (nunca llega a `_RESTART_PATTERNS`). `kubectl set resources deployment` está en MUTATING y también en `_RESTART_PATTERNS`. Si alguien se pregunta "¿por qué kubectl delete pod no está en _RESTART_PATTERNS?": porque ya lo bloquea la regla 3.

---

### D4 — Fail-safe en implies_pod_restart: comandos desconocidos = restart

**Decisión**: si un comando MUTATING no coincide con ningún patrón de la lista blanca (`label`, `annotate`) ni con ningún patrón conocido, se trata como restart.

**Por qué**: el LLM puede generar comandos inesperados. Si el fail-safe fuera "no restart", un comando desconocido pasaría al executor — potencialmente destructivo en modo real. Es mejor escalar y que un humano revise. En el peor caso, genera ruido en Mattermost (falso positivo conservador), que es preferible a un falso negativo que ejecuta algo no previsto.

---

### D5 — decide_action sigue siendo pura (sin I/O)

Se eligió deliberadamente no añadir lecturas de cluster a `decide_action`. La función recibe `diagnosis: dict` y `command_validations: list[dict]` y devuelve `RemediationAction` sin await. Esto hace que:
- Los tests sean triviales (no necesitan mock de subprocess ni async fixtures).
- La lógica sea razonable bajo carga (no bloquea el event loop de FastAPI).
- Las reglas sean reproducibles dado el mismo input.

El `proposed_action` viene del LLM (que ya tuvo acceso al estado del cluster via el alert context y el RAG), no de una llamada fresca al API. Es un snapshot del momento de la alerta, no del momento de la decisión — gap aceptable para dry-run; revisable antes de `DRY_RUN=false`.

---

### D6 — parse_memory_to_bytes: IEC antes que SI, sufijos largos antes que cortos

Importante: el parser evalúa IEC (Ki/Mi/Gi/Ti) antes que SI (K/M/G/T) para evitar false matches. Si evaluara `k` de SI antes de `ki` de IEC, `"512Ki"` matchearía SI-`k` con número `"512"` truncado, devolviendo 512000 en vez de 524288. El orden es: primero sufijos más largos dentro de cada categoría.

---

## Matices de implementación

### Fixture mock_diagnosis_auto_remediate actualizada
El fixture antes tenía `kubectl set resources deployment` — ese comando ahora escala bajo la regla 4.5. Se cambió a `kubectl annotate deployment engine aiops-checked=true -n prod`. El happy path del agente (AUTO_REMEDIATE con diagnóstico estándar) queda representado con comandos de anotación, lo que es realista para el modo dry-run actual.

### Test test_auto_remediate_all_conditions_met
Ajustado a validaciones con solo `kubectl describe` + `kubectl annotate`. El test sigue verificando la regla 7 (las reglas 1-6 pasan), pero ahora con comandos que el tutor permitiría.

### kubectl delete pod: de UNKNOWN a BLOCKED
`kubectl delete pod <name>` no estaba en `BLOCKED_PATTERNS` (solo `delete namespace/pvc/node/...`). Esto era una omisión: eliminar un pod individual es un restart directo. Añadido a `BLOCKED_PATTERNS`. Los tests existentes no cubrían este caso — el gap queda ahora cubierto por `test_delete_pod_escalates`.

---

## Consecuencia en flujos reales

Con estas reglas activas, un KubePodOOMKilled típico:
1. Prometheus → Alertmanager → webhook agent.
2. RAG: 3 runbooks + incidents relevantes.
3. LLM: genera diagnóstico con `proposed_action` (si el modelo lo puebla) + comandos `set resources deployment`.
4. Regla 4.5: `set resources deployment` → `implies_pod_restart=True` → ESCALATE (`pod_restart_blocked`).
5. Mattermost: notificación con `decision=escalate`, `reason_code=pod_restart_blocked`.
6. Log JSON: incluye `reason_code` y el comando que activó el bloqueo.

El flujo AUTO_REMEDIATE solo se activa si el LLM propone exclusivamente comandos de solo lectura o `label/annotate`.

---

## E2E en cluster verificado (2026-04-24, imagen c3b0975)

### Cloud Build + rollout
- Cloud Build: 81 tests gate → imagen `c3b0975` (`sha256:9c22a3fca9...`) en AR. Duración: 1m41s.
- `kubectl set image deployment/agent agent=...aiops-agent:c3b0975 -n arturo-llm-test` → rollout OK.

### Escenario A — webhook E2E
Alerta KubePodOOMKilled → RAG → LLM → `action=escalate, risk=high, confidence=0.85, commands_total=3`.
- LLM generó comandos read-only/annotate esta iteración → regla 5 (risk=high) disparó antes que la 4.5.
- `outcome=escalate` persistido en ChromaDB. Mensaje en Mattermost OK.
- Nota: `qwen2.5:1.5b` no determinista — en sesiones anteriores generó `set resources deployment` (regla 4.5). En esta iteración tomó otro camino. Ambos comportamientos son correctos.

### Escenario B — kubectl exec sobre binario desplegado (determinista)

Método: `kubectl exec -n arturo-llm-test agent-859cd44489-s52f4 -- python -c "..."` importando `remediation` del pod.

| Test | Regla | Input | `action` | `reason_code` |
|---|---|---|---|---|
| 4.5 | `set resources deployment` | `escalate` | `set_resources_triggers_rollout` |
| 4.6 B.1 | `256Mi → 1Gi` (4×) | `escalate` | `memory_exceeds_2x` |
| 4.6 B.2 | `256Mi → 512Mi` (2×, límite) | `auto_remediate` | — |
| 4.6 B.3 | `256XYZ` inválido | `escalate` | `unparseable_memory` |

Todos los `reason_code` y valores logueados como JSON estructurado en el pod (visible en `kubectl logs`).

### Métricas verificadas
```
aiops_remediation_total{action="escalate"} 2.0
aiops_diagnosis_total{outcome="success"}   2.0
aiops_feedback_total{outcome="persisted"}  2.0
```
Los 2 escalates corresponden a los dos runs del webhook (13:52 y 13:59). Feedback loop activo.
Nota: `curl ... | grep aiops` (minúsculas) — el primer intento falló por buffer vacío, no por ausencia de métricas.

### Pendiente
- Screenshot Grafana (port-forward svc/grafana-svc 3000:3000 -n arturo-monitoring).

---

## Pendiente para próxima sesión

1. **Grafana + métricas**: investigar `/metrics` vacío y verificar paneles del dashboard.
2. **Conversación con el tutor**: presentar regla 4.5 estricta + evidencia E2E. Preguntar: ¿rolling update en HA (≥2 réplicas, `maxUnavailable=0`) es aceptable? ¿In-place resize (k8s 1.27+)?
3. **Webhook entrante Mattermost** (pendiente cierre Fase 1).

---

## Vault Impact

| Área | Tipo | Acción |
|---|---|---|
| `01_Projects/AIOps-TFG` | Project node | Actualizar: condición tutor implementada; flujo OOM siempre escala con dry-run; pendiente validar con tutor excepción a regla 4.5 |
| `03_Knowledge/AI_ML/AIOps-Patterns` | Knowledge | Patrón: "fail-safe conservador en reglas de remediación autónoma: ESCALAR > ACTUAR cuando la acción es irreversible o destruye estado running" |
| `03_Knowledge/Programming/Python` | Knowledge | Patrón: parse_memory_to_bytes con IEC/SI — sufijos más largos primero para evitar false match |
