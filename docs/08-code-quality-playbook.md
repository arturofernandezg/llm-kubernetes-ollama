# Code Quality Playbook — AIOps Agent

Guía reutilizable para sesiones de revisión y mejora de calidad de código. Cada sesión sigue este workflow, toma un módulo como unidad de trabajo y actualiza el backlog en `docs/11-quality-backlog.md`.

---

## Workflow por sesión (5 pasos)

1. **Scan dirigido** — lanzar Explore agent con el prompt prehecho (ver sección abajo), parametrizado al módulo objetivo.
2. **Triage** — clasificar cada finding por severidad (`high / medium / low`) y categoría (ver 8 dimensiones).
3. **Selección** — elegir N fixes que quepan en la sesión (~20-45 min). Siempre priorizar `high`.
4. **Fix + tests** — aplicar cambios en el módulo, actualizar o añadir tests. Correr suite completa.
5. **Cierre** — marcar findings como `DONE` en el backlog. Commit con mensaje convencional.

---

## 8 Dimensiones de scan

### 1. Dead code
- ¿Hay imports no usados en el archivo?
- ¿Hay parámetros de función que nunca se leen en el cuerpo?
- ¿Hay ramas (`if`/`elif`) que son inalcanzables dado el flujo del código?
- ¿Hay variables asignadas pero nunca leídas?

### 2. Manejo de errores
- ¿Hay `except Exception` que tragan errores sin log estructurado?
- ¿Se maneja `asyncio.CancelledError` explícitamente o se confía en que es `BaseException`?
- ¿Hay try/except demasiado amplios que ocultan bugs de lógica?
- ¿Los errores capturados añaden suficiente contexto para diagnóstico (qué comando, qué ID)?

### 3. Naming inconsistente
- ¿Las funciones/variables siguen `snake_case` consistente con el resto del módulo?
- ¿Los nombres describen el QUÉ (no el CÓMO)?
- ¿Las constantes siguen `UPPER_SNAKE_CASE`?
- ¿Los prefijos (`_`) para privados/internos se usan consistentemente?

### 4. Edge cases sin cubrir
- ¿Hay `.get()` / `dict["key"]` sin validación previa (KeyError potencial)?
- ¿Se asume que una lista no es vacía antes de indexarla?
- ¿Se acepta `None` donde se espera un objeto y se usa sin chequeo?
- ¿Los strings de entrada se validan antes de parsear (ej. memoria, enums)?

### 5. Concurrencia
- ¿Hay estado mutable compartido entre corrutinas (`dict`, `list` globales)?
- ¿Las mutaciones a ese estado están protegidas con `asyncio.Lock`?
- ¿Las iteraciones sobre dicts compartidos usan snapshot (`list(d.items())`) para evitar `RuntimeError`?
- ¿Los background tasks pueden interferir con handlers síncronos?

### 6. Logging
- ¿Los logs usan `extra={...}` estructurado (no f-strings) para ser consultables como JSON?
- ¿Los puntos de error y decisión críticos tienen log con contexto suficiente?
- ¿Los niveles son correctos: `debug` para ruido operativo, `info` para eventos normales, `warning` para degradación, `error` para fallos)?
- ¿Hay logs de `debug` ausentes donde ayudaría en diagnóstico (ej. kill de procesos, TTL checks)?

### 7. Type hints
- ¿Las funciones públicas tienen anotaciones de retorno (`-> None`, `-> dict`, etc.)?
- ¿Los parámetros tienen tipos en las firmas?
- ¿Se usan `X | None` o `Optional[X]` donde corresponde?
- ¿Hay `Any` implícitos que podrían ser más concretos?

### 8. Reuso
- ¿Hay bloques de código duplicados (≥3 líneas idénticas en ≥2 sitios)?
- ¿Hay helpers que se podrían extraer a `tests/helpers.py` o a un módulo de utilidades?
- ¿Hay lógica de retry/backoff implementada más de una vez con variaciones menores?
- ¿Hay constantes literales repetidas (strings, números) que deberían ser constantes nombradas?

---

## Criterios de severidad

| Severidad | Criterio | Ejemplos |
|---|---|---|
| `high` | Bug correcto en producción — puede crashear, perder datos, race condition | `UnboundLocalError`, mutex faltante, pop antes de check |
| `medium` | Mantenibilidad o operabilidad degradada — no rompe hoy pero añade riesgo técnico | Logging con f-strings, duplicación x3, parámetro no usado |
| `low` | Cosmético — no afecta correctness ni operabilidad | Type hint faltante, docstring desactualizado, constante hardcoded |

---

## Prompt prehecho de scan

Copiar y adaptar este prompt para el Explore agent. Sustituir `{MÓDULO}` y `{ARCHIVO}`:

```
Necesito una revisión de calidad de código del módulo {MÓDULO} en
/Users/jay/Developer/orange/Practicas/llm-kubernetes-ollama/agent/{ARCHIVO}.

Lee el archivo completo y reporta findings usando estas 8 dimensiones:
1. Dead code (imports, parámetros, ramas inalcanzables, variables sin usar)
2. Manejo de errores (except amplios, errores tragados, CancelledError, contexto insuficiente)
3. Naming inconsistente (vs convenciones del módulo y del proyecto)
4. Edge cases (None sin chequeo, KeyError potencial, listas vacías indexadas, strings sin validar)
5. Concurrencia (estado mutable global, locks ausentes, iteración sin snapshot)
6. Logging (f-strings vs extra={}, logs faltantes en puntos críticos, niveles incorrectos)
7. Type hints (ausentes en firmas públicas, Any implícito, Optional sin anotar)
8. Reuso (duplicación ≥3 líneas, helpers extraíbles, constantes repetidas)

Para cada finding reporta:
- ID: número secuencial
- Localización: archivo:línea_inicio-línea_fin
- Dimensión: una de las 8 anteriores
- Severidad: high / medium / low
- Descripción: una frase que explica el problema
- Fix propuesto: una frase que describe la solución

Formato: tabla markdown. Total bajo 500 palabras. Sin generalidades — solo findings concretos con número de línea.
```

---

## Módulos y sesiones tentativas

| Sesión | Módulo / Área | Archivo principal | Estado |
|---|---|---|---|
| #1 | Fixes high (cross-module) | remediation.py + main.py | DONE (2026-05-11) |
| #2 | remediation.py medium/low | agent/remediation.py | DONE (2026-05-11) |
| #3 | main.py medium/low | agent/main.py | DONE (2026-05-11) — M1-M9 |
| #4 | mattermost.py | agent/mattermost.py | DONE (2026-05-11) — MM1-MM9 |
| #5 | Cross-cutting (helpers, retry, logger) | varios | DONE (2026-05-11) — R7, R8, X1, X3 |
| #6 | Security (HMAC callback auth) | main.py, mattermost.py, schemas.py, config.py | DONE (2026-05-11) — X5 |
| #7 | k8s/ (nodeSelector guaranteed, tolerations) | k8s/*.yaml | TODO |
| #8 | Docs sync (roadmap + CLAUDE.md, cierre Fase 3) | docs/07-roadmap.md | TODO |

---

## Convenciones de commit por sesión

```
refactor: code quality session #N — <resumen de lo hecho>
```

Ejemplo: `refactor: code quality session #1 — high severity fixes (lock, proc guard, TTL reorder, sweep task)`

Logs nuevos usan `extra={...}` estructurado. Código y comments en inglés. Docs en español.
