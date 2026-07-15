---
fecha: 2026-07-15
slug: arco-cured-limpio-evidencia
promoted: false
---

## Objetivo

Mañana del chapter. Ejecutar el runbook de grabación (`2026-07-15-runbook-grabacion-y-chaos-fallido.md`)
de arriba abajo con la cabeza fresca → **un arco chaos OOM limpio hasta `outcome=cured`** + capturar
toda la evidencia numérica para actualizar los slides desactualizados (foto grounding slide 07,
Gate 8, test count).

## Hecho

- **Arco limpio conseguido `outcome=cured`** (imagen `2ac3c5d`). Flujo completo E2E:
  - FASE 0 verde: imagen `aiops-agent:2ac3c5d`, startupProbe puesta (`failureThreshold:30` sobre
    `/healthz`), 4 pods `Running` RESTARTS=0. **Solo 1 nodo guaranteed** (churn/preemption del 14),
    no bloqueante — todos los pods en ese nodo y sanos.
  - FASE 2: ollama calentó (`qwen2.5:1.5b` respondió) → el 1er diagnóstico no pagó frío.
  - FASE 6: `./scripts/chaos_arc.sh` → OOM → grounding → escalación doble botón → **Jay aprobó
    `approve_model` (512Mi)** → patch al deployment → `outcome=auto_pending` → ventana rollback 300s
    → health check `all_pods_running_no_failure_restarts` → **`Persisted incident
    incident-KubePodOOMKilled-1784100835 (outcome=cured)`** + re-upsert a ChromaDB como `cured`.
- **Test count actualizado**: `pytest agent/tests/ -q` → **700 passed** (antes 696). Editado
  `scripts/build_demo_v3.py`: `STATS["tests"]` 696→700 (línea 43) + la QA "¿Cómo se testean 696
  veces…" 696→700 (línea ~1093). La línea 1048 (`696,373`) es una coordenada de `<path>` SVG — NO
  se toca. **Deck aún SIN regenerar** (pendiente: regenerar una sola vez con foto slide 07 + Gate 8
  + 700).
- **Evidencia numérica capturada** (FASE 8), capturas guardadas en `~/Desktop/video_demo_hoy`:
  - **C6 (evidencia fuerte Gate 8)**: `aiops_feedback_verdict_total{outcome="cured"} 1.0`.
  - Chaos: **MTTD** OOM 5.18s / CrashLoop 5.04s; **MTTR** OOM 264.4s / CrashLoop 133.4s. Dos
    experimentos contados (OOM real + CrashLoop phantom), ambos `escalate`.
  - **R5**: `aiops_incident_resolution_total{correlated="miss"} 1.0`, sin muestra en
    `aiops_incident_resolution_seconds` → **exactamente lo que predijo el gotcha #6 del runbook**
    (el approve hace `pop` del índice → el `resolved` posterior da miss). Honesto: la evidencia del
    flujo humano es C6, no R5.

## Encontrado / gotchas

1. **El "Unknown or expired incident_id" NO fue contaminación — fue un doble click.** El
   `approve_model` auditado (incident `845e9168`) dio "Unknown or expired" en un callback… pero el
   **rollback evaluation salió con ese mismo `845e9168` → healthy → cured**. O sea: el 1er click
   consumió la escalación (patch + rollback programado); un 2º disparo (doble click / re-fire de MM)
   sobre la misma escalación ya consumida dio el "expired". Inofensivo. Lección: "expired" tras un
   approve exitoso = duplicado, no error.
2. **Phantom `KubePodCrashLoopBackOff` esperado.** A +2min del cured llegó un CrashLoop del pod
   viejo (`-l2b97`); el enrichment devolvió `NotFound` → el sistema NO auto-remedia un ghost (hace
   lo correcto). Es ruido, no fallo. Aparece en el chaos counter como 2º experimento.
3. **R5 miss confirmado en vivo** (gotcha #6 del runbook validado empíricamente): flujo con
   aprobación humana → serie R5 vacía. No prometer R5 en el slide.
4. **Grafana: 3 scrape targets de agente `down`.** Casi seguro ghosts de pods de agente viejos
   (churn del 14 + pod actual AGE ~10m). El agente está demostrablemente vivo (sirve `/metrics`,
   `/readyz 200`, cerró el cured). Pendiente confirmar que las IPs `down` ≠ `10.120.0.135` (pod
   actual). Para la foto C7: NO meter el panel de scrape-targets si muestra ghosts — fotografiar
   solo dashboard Chaos + fila Cola.

## Decisiones + por qué

- **Aprobar 512Mi (valor modelo), nunca 64Mi.** El ×2 del motor (64Mi) no cura contra el stress de
  este manifiesto → `rolled_back`. Solo el botón modelo cura. Regla de oro del runbook, cumplida.
- **No tocar nada ~5 min tras aprobar.** La causa del caos de anoche eran runs solapados / Ctrl-C a
  mitad de ventana. Esta vez: un solo arco, cero interferencia → cured inequívoco.
- **Usar C6 (feedback_verdict cured) como evidencia del Gate 8, no R5.** Honestidad > foto bonita:
  en el flujo humano el veredicto de rollback es dueño del outcome y R5 da miss por diseño.
- **Deck: regenerar una sola vez al final** (no tres) con las 3 actualizaciones juntas.

## Siguiente

> **FOCO de la próxima sesión (dicho por Jay): montar el vídeo + repasar los slides.**
> Todas las capturas C1–C7 (incluida C2 grounding) ya están en `~/Desktop/video_demo_hoy`.

- **Montar el vídeo demo** con las capturas de `~/Desktop/video_demo_hoy` (C1–C7 según el guion
  `demo/guion_video.md`). **C2 (grounding) ya añadida a la carpeta** → set completo.
- **Repasar/actualizar slides desactualizados** y **regenerar el deck una sola vez**
  (`python3 scripts/build_demo_v3.py`, NUNCA editar el HTML de 2.7MB):
  - Foto slide 07 (grounding real, con la captura **C2**: `grounded=1.0` / `current_value=32Mi` /
    CLUSTER FACTS / `memory_exceeds_2x`) — reemplaza la foto vieja donde el modelo repetía el pod.
  - Gate 8 (C6: `feedback_verdict{outcome="cured"} 1.0` como evidencia fuerte; R5 = miss, no va).
  - Test count 700 (ya editado en `build_demo_v3.py`, solo falta regenerar).
- Verificar que se ejecutó **FASE 9 teardown** (deployment chaos borrado + Redis flush de las 5
  familias) — último gate del run.
- `/ensayo` hostil con munición nueva: doble click → "expired" inofensivo, phantom CrashLoop no
  auto-remediado, R5 miss honesto en flujo humano, MTTD ~5s / MTTR ~264s.
- Vault end-session (Vault Impact acumulada de los docs 07-14 + este run).
