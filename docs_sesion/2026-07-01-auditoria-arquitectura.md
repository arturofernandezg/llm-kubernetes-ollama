---
fecha: 2026-07-01
slug: auditoria-arquitectura
promoted: false
---

## Objetivo
Auditoría técnica completa del proyecto (rol: arquitecto Senior DevOps/SRE/MLOps),
crítica y sin complacencia, como revisor de empresa pre-producción. Entregable doble:
(1) esta bitácora; (2) un HTML "modo libro" en `docs_sesion/` para estudio y brainstorm
sobre el roadmap activo.

## Hecho
- Revisión de ~3.800 líneas de `agent/` + manifiestos K8s + RBAC + Dockerfile + reglas
  Prometheus + NetworkPolicy + cola Redis Streams + flujo de remediación.
- Puntuación por 9 dimensiones (global ~5.9/10) + tabla de acción priorizada (P0→P3).
- Generado `docs_sesion/2026-07-01-auditoria-arquitectura.html` (informe navegable, modo libro).

## Encontrado / gotchas
- **P0 arquitectónico**: el agente NO enriquece contexto con la API de K8s antes del LLM.
  `generate_diagnosis()` solo recibe labels+annotations+RAG. Grep confirma: cero `kubectl
  logs/describe/get events` en el camino de diagnóstico (solo post-decisión en
  `capture_pre_patch_value`/`check_pod_health`). Tiene el RBAC (pods, pods/log, events) y no
  lo usa. → Es la causa raíz REAL de la alucinación de `current_value` (la "slice C"): la
  fuente de verdad es el LLM, no el cluster.
- **P0 seguridad**: auth de callbacks fail-open. `_verify_hmac_token` devuelve True si
  `webhook_secret==""`. Igual `mm_command_token`. Con `REMEDIATION_DRY_RUN=false` → ejecución
  de remediaciones no autenticada si el Secret no existe.
- **Mismatch de Secrets**: `secrets-setup.sh` crea `agent-secrets/mattermost-webhook-url`,
  pero el deployment lee Secret `mattermost-webhook` clave `url`, y espera `webhook-secret` +
  `mm-command-token` en `agent-secrets` que el script NUNCA crea → HMAC/token efectivamente
  desactivados en la práctica (agrava el fail-open).
- **P0 resiliencia**: `IN_FLIGHT_ROLLBACKS` es un dict en memoria + `asyncio.sleep(300)`. Si
  el pod reinicia en esos 5 min (rollout/OOM/spot), el patch queda aplicado y NUNCA se
  revierte. Inconsistente con el enorme esfuerzo de durabilizar la cola.
- **Bug selector**: `check_pod_health` usa `-l app={name}` hardcodeado (`remediation.py:654`).
  Si el deployment usa otra label → `no_pods_found` → `healthy=False` → rollback falso sobre
  una remediación que funcionó.
- **Approve humano** ejecuta comandos free-text del LLM (`incident.safe_commands`), NO el
  comando determinista `build_set_resources_command`. Según F4 esos comandos suelen ser no
  ejecutables → un operador pulsa "Ejecutar" y no pasa nada.
- **Detección**: solo Prometheus (sin Watch API). Cubre OOM/CrashLoop(proxy por reinicios)/
  ImagePull/HighCPU/HighMem/TargetDown. NO cubre Pending, NotReady, Evicted, FailedJob (todos
  detectables con KSM sin tocar código).
- **Escalabilidad**: Ollama serializado (~205-252s/diagnóstico) + consumidor 1-a-1 +
  `replicas:1` → ~1 alerta/4min. Rollback en memoria bloquea `replicas>1`. Redis SPOF sin
  persistencia (sin AOF/PVC).
- **Poisoning RAG**: los incidentes se persisten desde salidas del LLM (incl. `raw_response`)
  y se reinyectan como contexto → feedback poisoning diferido.
- **Deuda**: `main.py` 1201 líneas (monolito), `remediation.py` 937; código muerto Fase 0
  (tf_generator, extraction, validation, /extract) shippeado en el binario de producción;
  `.DS_Store`/`.pytest_cache` en repo.
- **Positivo real**: guardrails de decide_action (cascada 1→7.5), cola Redis Streams
  (fail-closed, dead-letter, reclaim, self-heal NOGROUP), securityContext ejemplar, sin
  shell-injection (create_subprocess_exec + shlex + argv[0]==kubectl), observabilidad fuerte
  (métricas + logs JSON + chaos MTTD/MTTR).

## Decisiones + por qué
- **Nota global 5.9/10** y "rechazo para producción" con 3 bloqueantes P0. Por qué: los
  cimientos (guardrails/cola/observabilidad) están bien, pero diagnóstico a ciegas + auth
  fail-open + rollback no durable son inaceptables en prod. Ninguno es rediseño total.
- **Priorización P0 = context-enrichment primero**. Por qué: desbloquea la "slice C" del
  roadmap y mata la clase entera de alucinaciones de `current_value` de un tiro; el resto de
  mejoras de IA (modelo mayor, format:json) rinden poco sin esto. "Contexto > tamaño de modelo"
  (ya medido por el propio proyecto) es el argumento.
- **HTML modo libro** (no PDF/MD): navegable con índice lateral, secciones colapsables mentales,
  tablas de scoring y de acción, pensado para leer de corrido Y para saltar a una sección en
  la sesión de brainstorm. Autocontenido (CSS inline, sin dependencias) porque el repo es
  "sin binarios/CDN" y debe abrirse offline.

## Siguiente
- Brainstorm sobre encaje de los 3 P0 en el roadmap activo (F3 slice C ya apunta al P0-1).
- Candidato de microtarea inmediata: `agent/enrichment.py` — gather paralelo
  (get pod/logs/events) con fail-soft por campo; `current_value`+target desde el snapshot del
  cluster, no del LLM. Sella slice C.
- Bloqueante rápido y barato: alinear `secrets-setup.sh` con las claves que lee el deployment
  + fail-closed de HMAC cuando `REMEDIATION_DRY_RUN=false`.
- Durabilizar `RollbackContext` en Redis (TTL) + reevaluación al arranque en `_periodic_reclaim`.
