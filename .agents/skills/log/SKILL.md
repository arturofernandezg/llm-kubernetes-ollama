---
name: log
description: "Bitácora de sesión del proyecto AIOps (capa cruda). Al final de una sesión, captura en docs_sesion/ qué se hizo, con qué nos topamos y qué decisiones se tomaron (con el porqué), dejando el 'Siguiente' para la próxima. NO toca la documentación canónica (eso es promote). TRIGGERS (EN): 'log', 'log session', 'log this', 'note this down'. TRIGGERS (ES): 'apunta', 'apunta la sesión', 'bitácora', 'registra', 'guarda lo de hoy', 'cierra y apunta'. Úsalo al CERRAR la sesión."
---

# /log — Bitácora de sesión

Captura cruda y honesta de la sesión en `docs_sesion/`. Append-only, baja fricción.
Es la FUENTE de la que `promote` destila luego la documentación real. **No promuevas aquí.**

## Paso 1 — Revisar la sesión
Repasa la conversación y extrae:
- **Objetivo**: qué nos propusimos.
- **Hecho**: cambios concretos (ficheros, módulos, manifiestos, comandos relevantes).
- **Encontrado / gotchas**: problemas, sorpresas, cosas aprendidas del cluster/sistema, callejones sin salida.
- **Decisiones + por qué**: qué se eligió, alternativas descartadas, trade-offs. **Captura los PORQUÉS** — son lo que da valor a la bitácora.
- **Siguiente**: próximos pasos concretos (esto lo lee `start` la próxima vez).

## Paso 2 — Escribir la entrada
Crea `docs_sesion/YYYY-MM-DD-<slug>.md` (slug corto en kebab-case del tema). Si ya existe entrada de hoy, añade/actualiza en vez de duplicar.

```
---
fecha: YYYY-MM-DD
slug: <tema>
promoted: false
---

## Objetivo
...

## Hecho
- ...

## Encontrado / gotchas
- ...

## Decisiones + por qué
- ...

## Siguiente
- ...
```

## Paso 3 — Cerrar
- NO actualices docs canónicos (07, CLAUDE.md, AGENTS.md, 01-06...) — eso es trabajo de `promote`.
- NO hagas commit (lo hace Jay).
- Imprime un resumen de 3-5 bullets de lo capturado.
