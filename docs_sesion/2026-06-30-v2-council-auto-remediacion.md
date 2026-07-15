---
fecha: 2026-06-30
slug: v2-council-auto-remediacion
promoted: true
---

> Sesión de estrategia (no de código). Arrancó con `/start` pidiendo brainstorm de límites/mejoras/v2 + research. Derivó en: (1) brainstorm v2 con LangGraph como eje por fit con la audiencia MasOrange, (2) un **council** completo (5 asesores) para presionar la jugada, (3) una re-priorización del usuario hacia **hacer que la auto-remediación dispare de verdad**, con análisis del motor real. Sin tocar código de producto todavía.

## Objetivo
- Pensar la **v2** del sistema (más allá de lo escrito): límites actuales, mejoras, pendientes, con research del estado del arte AIOps/LLM.
- Dato nuevo decisivo: **MasOrange usa muchísimo LangGraph** → la presentación al chapter pasa a optimizarse por *fit de audiencia*, no solo mérito técnico.
- Presionar la estrategia de presentación con el **council** antes de comprometer trabajo.
- Re-priorización del usuario a media sesión: **"hay que hacer que auto-remedie; que funcione lo que ya está como debería"** → ¿se puede arreglar el motor para que dispare el auto?

## Hecho
- **Research** (WebSearch) del estado del arte: bucle ReAct read-only con tool allow-list es el patrón estándar 2026 (HolmesGPT, Aurora/LangGraph, RCA grafo-guiada); constrained/grammar decoding mejora structured output incluso en 1.5B–3B; `ChatOllama.bind_tools()` exige modelo tool-capable (un modelo sin tools responde en texto plano → recomendado qwen2.5:7b/llama3.1:8b); LangGraph HITL = `interrupt()`+`Command(resume)`+checkpointer (Redis/Postgres, multi-pod); NO LangSmith (datos no salen).
- **Brainstorm v2** con espectro de adopción LangGraph (Nivel 1 "graph-skin" → Nivel 4 multi-agente). Tesis: tu sistema ya es un grafo escrito a mano; LangGraph reexpresa orquestación y nativiza el HITL.
- **Council ejecutado** (skill `llm-council`): 5 asesores en paralelo + peer-review anónimo + síntesis del chairman. Artefactos guardados en `/.claude/skills/llm-council/`: `council-report-2026-06-30-1624.html` (abierto en navegador) + `council-transcript-2026-06-30-1624.md`.
- **Análisis del motor de remediación** (`agent/remediation.py` + `config.py` leídos enteros) para responder "¿se puede hacer que el auto dispare?". Identificadas 2 desconexiones (ver gotchas). Propuesto plan en 6 slices. **Sin tocar código aún** (esperando confirmación de por dónde arrancar).

## Encontrado / gotchas
- **VEREDICTO DEL COUNCIL (el activo principal de la sesión)**:
  - **Coinciden (4/5)**: el "auto nunca disparó E2E" lo sostiene todo → reencuadrar como HITL deliberado y *decirlo en voz alta*. El graph-skin (Track B = misma lógica, otra piel) es lo **más débil**: a una sala LangGraph-native le lee a **cargo cult / impuesto de traducción / box-ticking** ("¿el framework no aportó nada funcional?"). El activo real es el **hallazgo empírico motor>modelo** (nuevo porque LangGraph no lo resuelve). El nodo **ReAct read-only** es lo único genuinamente nuevo → centro, no teaser. Separar el artefacto de demo del cluster (corre en local con los mocks de los tests).
  - **Choca**: el Expansionist (único "dual-track gana", escalar a plataforma interna) fue marcado por **los 5 revisores** como el mayor punto ciego (ambición sobre una capacidad que nunca corrió). Se rescatan sus activos reales: **soberanía de datos / LLM local = moat telco** y el patrón "modelo propone, cascada dispone".
  - **Puntos ciegos que cazó el peer-review**: "ya prototipado" es la **misma sobre-venta** que "auto funciona" (si piden verlo en vivo y es mock, se cae la honestidad); nadie costeó las 2 semanas en sesiones de 20 min; **asimetría reputacional junior→pares** → presentar el negativo como *pregunta abierta* a la sala (mayor estatus, menor riesgo); la **latencia 200-270s es un hallazgo en sí** (coste de inferencia local CPU-only); ¿es LangGraph mandato en MasOrange y qué financia el chapter?
  - **Recomendación del chairman**: ni dual-track tal cual ni reescritura completa → **(a)-reencuadrado-plus**: titular = límite empírico (motor>modelo); ancla = sistema HITL honesto que funciona hoy + soberanía; único elemento LangGraph = nodo ReAct read-only en local ("hacia aquí va", no "ya hecho"); matar el framing "promesa cumplida".
- **CAUSA RAÍZ de por qué el auto nunca dispara — es del MOTOR, no del modelo** (el usuario re-priorizó aquí; esto es lo más accionable):
  - **Desconexión nº1**: `process_remediation` (remediation.py:846-851) ejecuta en AUTO los comandos de `diagnosis["commands"]` (el modelo pone ahí *investigativos*: describe/top/logs — `has_set_resources=null` en 4/5). La intención real de remediar vive en `diagnosis["proposed_action"]` (estructurada, el modelo la acierta **5/5**). El motor usa `proposed_action` para el cap 2× (4.6), el snapshot y el **rollback** (`revert_patch` la sintetiza, líneas 751-757) **pero NO para construir el comando que ejecuta**. → Aunque AUTO disparara, correría `kubectl describe` — no remedia nada.
  - **Desconexión nº2**: los gates de auto preguntan al modelo lo que hace mal. Rule 4.5 busca un `set resources` MUTATING *en la lista del modelo* (que no existe → se salta). Cae a Rule 5: `risk > max_risk`. Config real: `remediation_auto_max_risk="low"`, `remediation_auto_confidence=0.8`, `remediation_enabled=False`, `remediation_dry_run=True`. El modelo da `risk=high/medium`, `conf=0.8` → **ESCALATE**. La excepción 4.5 pide conf≥0.9+risk≤medium → falla por ambos.
- **Conexión que cierra el círculo**: **F4 ya enriqueció la alerta con el límite actual** (anotación Prometheus) → `current_value` real, no alucinado. Solo falta que el motor *actúe* sobre la propuesta estructurada. F4 + este arreglo = auto que dispara.
- **Detalle del council mecánico**: los 5 asesores corrieron como sub-agentes en background; peer-review con mapa anónimo A=Contrarian, B=Expansionist, C=Executor, D=First Principles, E=Outsider.

## Decisiones + por qué
- **Convocar el council antes de comprometer trabajo de presentación**. *Por qué*: decisión de alto coste (2 semanas, audiencia de pares en su framework, asimetría reputacional junior). El council destapó que el dual-track/graph-skin era débil — habría sido esfuerzo mal invertido.
- **El usuario re-prioriza: arreglar el auto > narrativa**. *Por qué*: en vez de *presentar honestamente* que el auto no dispara (recomendación del council), prefiere **arreglar la cosa real**. Postura correcta: arreglar > spinear. Y resulta arreglable honestamente.
- **El arreglo es re-sourcing de decisiones, NO relajar gates**. *Por qué*: cada decisión va a quien tiene la competencia — diagnóstico (field/current/new) al modelo (ya 5/5); **síntesis del comando, riesgo y ejecución al motor** (que ya sintetiza en rollback). El comando sintetizado por el motor (determinista, acotado ≤2×, reversible con health-check) es **más seguro** que ejecutar el string de texto libre de un 1.5b → es un *upgrade* de seguridad, no una rebaja. No contradice la decisión F4 de "no bajar conf≥0.9": el gate de confianza del modelo *medía lo que no debía* para esta vía (no confiamos en el juicio del modelo; el motor acota y revierte).
- **Guardrails a añadir para mantenerlo honesto**: namespace allow-list `arturo-*` (no auto cross-tenant), solo-subir-límite (bajar escala), field elegible, riesgo derivado conservador (si no clasifica como acotado+reversible → escala).
- **NO comprometer LangGraph para el chapter** (de momento). *Por qué*: el council lo desaconseja como graph-skin; si entra, que sea el spike ReAct read-only como "qué sigue", no como reescritura. El foco se mueve a arreglar el auto.

## Siguiente
- **Decisión de arranque pendiente (preguntada al usuario)**: empezar por **Slice 1+2** (refactor `build_set_resources_command` compartido + enrutar la propuesta estructurada por ejecución en `process_remediation`) **o** escribir primero el **Slice 5** (test de integración como red de seguridad) antes de tocar `process_remediation`.
- **Plan completo en 6 slices** para que el auto dispare honestamente:
  1. Refactor: extraer `build_set_resources_command(proposed_action)`; `revert_patch` lo usa (puro, tests verdes).
  2. Camino estructurado: en `process_remediation`, si hay `proposed_action` válida, sintetizar el comando y enrutarlo por validación/ejecución (en vez de los comandos investigativos del modelo).
  3. Re-sourcing de gates: riesgo derivado por el motor + elegibilidad por grounding/validez (la excepción 4.5 deja de depender del float del modelo).
  4. Guardrails: namespace allow-list `arturo-*` + solo-subir.
  5. Test de integración determinista: diagnóstico mock con `proposed_action` válida + `commands` solo investigativos + `risk=high`/`conf=0.8` → **AUTO por vía estructurada** + comando sintetizado ejecutado + rollback field-aware. Sin cluster ni LLM.
  6. Validación en cluster: `scripts/chaos.sh cpu`/`oom` → auto-fire real + tarjeta Mattermost + rollback (la demo que el F3 no pudo dar).
- **Narrative del chapter** (si el auto dispara): pasa de "honest negative result" a "honest positive result" — *descubrimos qué sabe y qué no hacer un LLM pequeño y rediseñamos el motor en consecuencia* ("el modelo propone, el motor dispone", funcionando).
- **Pendientes arrastrados**: `/promote` de F3 (slice 1+1b+2) + F4 + evals (4 bitácoras `promoted: true`); matriz E1–E6 (`docs/14`); Gate 8 screenshots; validar self-heal NOGROUP live; `remediation_enabled=False` por defecto (habilitar auto real = flip de flag tras validar).
