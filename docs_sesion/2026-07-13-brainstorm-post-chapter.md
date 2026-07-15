---
fecha: 2026-07-13
slug: brainstorm-post-chapter
promoted: false
---

> Sesión de análisis + brainstorm (madrugada 12→13 jul, antes de S7). Sin cambios de código.

## Objetivo
Petición de Jay: "analiza el repo y dime cuánto queda por hacer; si queda poco código,
brainstorm (ultrathink) buscando por internet siguientes pasos".

## Hecho
- **Análisis de estado**: confirmado que el código está COMPLETO para el chapter — todo en
  `588e3a9`, 691 tests verdes (07-11). Lo pendiente antes del 15-jul no es código: commit de
  la consolidación de docs (13 ficheros en working tree, comando ya en la bitácora del 07-12),
  S7 (deploy + arco chaos, 13-jul) y S8 (ensayo, 14-jul).
- **Investigación externa** (5 búsquedas web): paisaje AI-SRE 2026 (K8sGPT / HolmesGPT / Aurora),
  tendencias AIOps+LLM, structured outputs de Ollama, kagent (CNCF Sandbox), benchmarks
  académicos (AIOpsLab de MSR, ITBench de IBM con leaderboard público, MicroRemed), paper RSA
  "When AIOps become AI Oops" (subversión vía manipulación de telemetría).
- **Brainstorm entregado** con postura (6 bloques, resumen abajo en Decisiones). Ningún doc
  canónico tocado — es material para decidir el rumbo post-chapter.

## Encontrado / gotchas
- **La tesis del proyecto ES el patrón que la industria describe como objetivo 2026**: separar
  lo determinista de donde el LLM razona, scope por policy, presupuestos duros. Los 3 modos de
  fallo predecibles de la literatura (fuera de scope / coste sin límite / no-determinismo) los
  cubrimos con allow-list de namespace, cap 2×, temp=0, validation layer. Munición directa para
  el ensayo S8 ("no vamos detrás del estado del arte en diseño, vamos alineados en pequeño").
- **Ningún open source creíble cierra el bucle de actuación**: K8sGPT (7.8k stars) y HolmesGPT
  (CNCF, operator + toolsets read-only) son diagnóstico/investigación; el actuador seguro con
  rollback verificado + aprendizaje del veredicto es NUESTRO diferenciador. Respuesta lista para
  la QA "¿por qué no K8sGPT?" — y HolmesGPT valida a posteriori la decisión F-17 (su enfoque es
  exactamente dar logs/events/toolsets al LLM).
- **Ollama ≥0.5 compila el JSON schema de `format` a gramática que constriñe el decoder** —
  sintaxis perfecta incluso en modelos pequeños; Qwen 2.5 de los mejores rellenando contenido.
  Confirma el item de v2.1 y lo sube de prioridad. Matiz: garantiza forma, no contenido → el
  seal/motor siguen siendo necesarios (refuerza la Capa B null, no la contradice).
- **Existen benchmarks públicos de exactamente nuestro problema**: AIOpsLab (despliega
  microservicios + inyecta faults), ITBench (RCA sobre snapshots offline K8s, leaderboard en
  artificialanalysis.ai — el más viable sin cluster dedicado), MicroRemed (remediación).
  No estaban en el radar del backlog.
- **El paper "AI Oops" (RSA) conecta con dos frentes nuestros**: el finding aplazado de
  poisoning RAG y el hecho de que F-17 mete logs del workload (texto controlable por un
  atacante/tenant) en el prompt — vector de inyección a threat-modelar.

## Decisiones + por qué
- (Recomendación, pendiente de decisión de Jay — nada ejecutado.) **Orden v2.1 propuesto**:
  1. F-15 (modularizar `main.py`) ANTES de apilar features — la suite 691 es la red;
  2. structured outputs en el decoder + re-medición con el eval propio (quick-win más rentable:
     mata la clase "JSON malformado" y tenemos harness para el antes/después);
  3. F-11 + AOF+PVC Redis (devuelve el claim F-06 de degradado a fuerte);
  4. capa de negocio (panel KPIs con `aiops_incident_resolution_seconds` de R5 — la mitad de la
     industria no puede demostrar reducción de toil; nosotros tendríamos el número).
- **Apuestas diferenciales del trimestre siguiente**: (a) correr el sistema contra ITBench —
  un número en benchmark público cambia la liga de credibilidad y es material publicable;
  (b) hardening adversarial (threat-model de inyección vía logs/events/anotaciones). Son los
  dos movimientos "de prototipo honesto a sistema citable".
- **kagent/MCP: NO por ahora** (v3 como mucho, sustrato de un catálogo de acciones) —
  dependencias sin retorno frente a lo que el motor ya hace con menos superficie.
- **Antes del chapter no se toca código**: el único opcional es regenerar el deck v3 si la
  captura C-08 del 13-jul merece embeberse.

## Siguiente
1. **Jay — commit pendiente del 07-12** (consolidación docs + `git rm` del json espurio; el
   comando exacto está en `2026-07-12-promote-unificacion-ruta-chapter.md` §Siguiente).
2. **13-jul (S7)**: `/ship` de `588e3a9` + `/chaos-run` arco OOM → captura C-08 + métrica R5 +
   Gate 8; teardown = último gate. Contingencia: quedarse en `0914611`.
3. **14-jul (S8)**: `/ensayo` (16 QA; sumar la nueva munición: alineamiento con el patrón 2026 +
   posicionamiento vs K8sGPT/HolmesGPT) + plan B físico + vault end-session.
4. **Post-chapter**: decidir sobre el orden v2.1 propuesto y las 2 apuestas (ITBench, hardening
   adversarial) — promover lo aceptado a docs/07 horizontes vía `/promote`.

## Vault Impact
| Archivo | Cambio |
|---|---|
| 01_Projects/AIOps node | Código completo pre-chapter; brainstorm post-chapter: structured outputs (decoder grammar), ITBench, hardening adversarial "AI Oops" |
| 02_Ideas/Raw | Idea: puntuar un agente propio en benchmark público (ITBench/AIOpsLab) como salto de credibilidad de prototipo→citable |
| 03_Knowledge/AI_ML | Patrón: grammar-constrained decoding garantiza *forma*, no *contenido* — la capa de validación determinista sigue siendo necesaria (encaja con el null de Capa B) |
