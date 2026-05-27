#!/usr/bin/env python3
"""Genera la demo HTML autocontenida y el guion del presentador.

Uso:
  python3 scripts/build_demo.py

Salida:
  demo/demo.html   Presentación offline, navegable con flechas/click.
  demo/guion.html  Guion práctico para la primera demo con tutor de empresa.
"""

from __future__ import annotations

import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "demo"

TITLE = "AIOps Infrastructure Agent"
SUBTITLE = "Diagnóstico y remediación asistida de incidencias en Kubernetes"
AUTHOR = "Arturo Fernández"
DATE = "2026-05-26"
SUBJECT = "Prácticas / TFG · MasOrange / Telecable"

CHAOS_RESULTS = [
    {"exp": "OOMKilled", "for": "0m", "mttd": "5.1", "mttr": "211.6", "conf": "0.95", "outcome": "escalate", "detect": "39"},
    {"exp": "CrashLoopBackOff", "for": "5m", "mttd": "5.0", "mttr": "214.0", "conf": "0.95", "outcome": "escalate", "detect": "195"},
    {"exp": "ImagePullBackOff", "for": "1m", "mttd": "5.1", "mttr": "273.5", "conf": "0.85", "outcome": "escalate", "detect": "171"},
    {"exp": "HighCPU", "for": "5m", "mttd": "10.0", "mttr": "226.7", "conf": "0.98", "outcome": "escalate", "detect": "609"},
]

IMAGES = {
    "mattermost": [
        "memoria/demos/chaos_third_test_mattermost.png",
        "memoria/demos/chaos_second_test_mattermost.png",
        "memoria/demos/chaos_first_test_mattermost.png",
    ],
    "grafana": [
        "memoria/demos/grafana2_todoup.png",
        "memoria/demos/grafana1.png",
    ],
    "prometheus": [
        "memoria/demos/prometheus_firing_kubepodimagepullbackoff.png",
        "memoria/demos/prometheus_rules.png",
        "memoria/demos/prometheus_targets.png",
    ],
    "remediation_log": ["memoria/demos/remediation_log.png"],
}

_EMBEDDED: list[str] = []
_MISSING: list[str] = []


def data_uri(key: str) -> str | None:
    for rel in IMAGES[key]:
        path = ROOT / rel
        if path.exists():
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            _EMBEDDED.append(f"{key}: {rel} ({path.stat().st_size // 1024} KB)")
            return f"data:image/png;base64,{b64}"
    _MISSING.append(f"{key}: {', '.join(IMAGES[key])}")
    return None


def img(key: str, alt: str, cls: str = "shot") -> str:
    uri = data_uri(key)
    if uri:
        return f'<img class="{cls}" src="{uri}" alt="{alt}">'
    return f'<div class="placeholder">Captura pendiente: <code>{IMAGES[key][0]}</code></div>'


def chaos_table() -> str:
    head = (
        "<tr><th>Experimento</th><th>for:</th><th>MTTD pipeline</th>"
        "<th>MTTR pipeline</th><th>conf.</th><th>outcome</th><th>T_detect</th></tr>"
    )
    rows = ""
    for r in CHAOS_RESULTS:
        rows += (
            "<tr>"
            f"<td>{r['exp']}</td><td>{r['for']}</td><td>{r['mttd']} s</td>"
            f"<td>{r['mttr']} s</td><td>{r['conf']}</td>"
            f"<td><span class='pill warn'>{r['outcome']}</span></td><td>{r['detect']} s</td>"
            "</tr>"
        )
    return f"<table>{head}{rows}</table>"


CSS = r"""
:root{
  --bg:#0b1017; --panel:#151d28; --panel2:#101823; --fg:#edf2f7; --muted:#9aa8b8;
  --line:#293747; --orange:#ff5a00; --orange2:#ff8a3d; --blue:#54a3ff; --green:#57c28b;
  --red:#f06464; --yellow:#ffd166;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;overflow:hidden}
.slide{position:fixed;inset:0;display:none;flex-direction:column;justify-content:center;padding:5vh 6vw}
.slide.active{display:flex;animation:fade .18s ease-out}
@keyframes fade{from{opacity:.45;transform:translateY(6px)}to{opacity:1;transform:none}}
.kicker{font-size:.9rem;color:var(--orange2);font-weight:700;letter-spacing:.12em;text-transform:uppercase}
h1{font-size:4.1rem;line-height:1.02;margin:.15em 0 .1em;letter-spacing:0}
h2{font-size:2.35rem;line-height:1.12;margin:0 0 .75em;padding-bottom:.28em;border-bottom:2px solid var(--line)}
h3{font-size:1.15rem;margin:.1em 0 .55em;color:var(--orange2)}
p{font-size:1.2rem;line-height:1.55;margin:.25em 0}
ul{font-size:1.16rem;line-height:1.55;margin:.2em 0;padding-left:1.1em}
li{margin:.25em 0}
code{background:#080d13;border:1px solid var(--line);border-radius:5px;padding:.04em .34em;color:#ffd8bd}
.subtitle{max-width:920px;color:var(--muted);font-size:1.45rem;line-height:1.45}
.meta{display:flex;gap:1.3em;flex-wrap:wrap;margin-top:1.8em;color:var(--muted);font-size:1.05rem}
.meta span{border:1px solid var(--line);background:var(--panel2);border-radius:6px;padding:.45em .75em}
.cols2{display:grid;grid-template-columns:1fr 1fr;gap:1.35em;align-items:start}
.cols3{display:grid;grid-template-columns:repeat(3,1fr);gap:1.05em;align-items:stretch}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:1.05em 1.2em}
.card strong{color:var(--fg)}
.card.orange{border-left:4px solid var(--orange)}
.card.blue{border-left:4px solid var(--blue)}
.card.green{border-left:4px solid var(--green)}
.card.red{border-left:4px solid var(--red)}
.note{color:var(--muted);font-size:1.04rem;margin-top:1em}
.bigstat{font-size:4.5rem;font-weight:800;line-height:1;color:var(--orange)}
.statrow{display:grid;grid-template-columns:repeat(4,1fr);gap:1em;margin:1em 0}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:1em;text-align:center}
.stat b{display:block;font-size:2rem;color:var(--orange2);margin-bottom:.15em}
.stat span{color:var(--muted);font-size:.98rem}
.flow{display:flex;align-items:center;justify-content:center;gap:.55em;flex-wrap:wrap;margin:.8em 0 1em}
.box{min-width:120px;text-align:center;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:.85em .9em;font-weight:700}
.box small{display:block;color:var(--muted);font-weight:400;font-size:.78rem;margin-top:.2em}
.box.hot{border-color:var(--orange);box-shadow:0 0 0 2px rgba(255,90,0,.18)}
.arr{color:var(--orange);font-size:1.55rem;font-weight:800}
.chips{display:flex;gap:.5em;flex-wrap:wrap;margin-top:1em}
.chip,.pill{display:inline-block;border-radius:999px;border:1px solid var(--line);background:#090f16;color:var(--muted);padding:.26em .75em;font-size:.92rem}
.pill.warn{color:#201000;background:var(--yellow);border-color:var(--yellow);font-weight:700}
.pill.ok{color:#07180f;background:var(--green);border-color:var(--green);font-weight:700}
.shot{max-width:82vw;max-height:56vh;display:block;margin:0 auto;border:1px solid var(--line);border-radius:8px;box-shadow:0 14px 44px rgba(0,0,0,.45)}
.shot.side{max-width:43vw;max-height:48vh}
.placeholder{border:2px dashed var(--orange);border-radius:8px;padding:2em;text-align:center;color:var(--muted);background:#080d13}
table{border-collapse:collapse;width:100%;font-size:1rem;background:var(--panel2)}
th,td{border:1px solid var(--line);padding:.55em .7em;text-align:center}
th{color:var(--orange2);background:#080d13}
td:first-child,th:first-child{text-align:left}
.counter{position:fixed;right:2.4vw;bottom:2vh;color:var(--muted);font-size:.92rem}
.bar{position:fixed;left:0;bottom:0;height:4px;background:var(--orange);transition:width .18s}
.twocol-list{columns:2;column-gap:2.2em}
.quote{font-size:1.55rem;line-height:1.45;color:var(--fg);max-width:920px}
@media (max-width:900px){
  .cols2,.cols3,.statrow{grid-template-columns:1fr}
  h1{font-size:2.7rem} h2{font-size:1.8rem}
  .slide{padding:4vh 5vw;overflow:auto}
  body{overflow:auto}
  .shot,.shot.side{max-width:90vw;max-height:42vh}
}
"""

JS = r"""
const slides=[...document.querySelectorAll('.slide')];
let i=0;
function show(n){
  i=Math.max(0,Math.min(slides.length-1,n));
  slides.forEach((s,k)=>s.classList.toggle('active',k===i));
  document.querySelector('.counter').textContent=(i+1)+' / '+slides.length;
  document.querySelector('.bar').style.width=((i+1)/slides.length*100)+'%';
  location.hash='s'+(i+1);
}
document.addEventListener('keydown',e=>{
  if(['ArrowRight','ArrowDown',' ','PageDown'].includes(e.key)){show(i+1);e.preventDefault();}
  if(['ArrowLeft','ArrowUp','PageUp'].includes(e.key)){show(i-1);e.preventDefault();}
  if(e.key==='Home')show(0);
  if(e.key==='End')show(slides.length-1);
});
document.addEventListener('click',e=>show(e.clientX < innerWidth*.3 ? i-1 : i+1));
const start=(location.hash||'').match(/s(\d+)/);
show(start ? Number(start[1])-1 : 0);
"""


def slides() -> list[str]:
    return [
        f"""<section class="slide">
          <div class="kicker">{SUBJECT}</div>
          <h1>{TITLE}</h1>
          <p class="subtitle">{SUBTITLE}</p>
          <p class="quote">Una demo de prácticas orientada a empresa: detectar una alerta real, entenderla con contexto, decidir con seguridad y dejar evidencia medible.</p>
          <div class="meta"><span>{AUTHOR}</span><span>{DATE}</span><span>GKE · Prometheus · Ollama · ChromaDB · Mattermost</span></div>
        </section>""",

        """<section class="slide">
          <h2>El problema</h2>
          <div class="cols3">
            <div class="card red"><h3>Operación bajo presión</h3><p>Una alerta de Kubernetes exige contexto rápido: qué pod falla, qué runbook aplica, qué comando es seguro y cuál no.</p></div>
            <div class="card orange"><h3>Conocimiento disperso</h3><p>Runbooks, histórico y síntomas viven separados del flujo de alerta. El operador busca información cuando el incidente ya está abierto.</p></div>
            <div class="card blue"><h3>Restricciones reales</h3><p>Datos del cluster dentro del entorno, sin depender de APIs externas y con recursos limitados. No es una prueba de notebook.</p></div>
          </div>
          <p class="note">Objetivo práctico: reducir la carga del operador sin convertir el LLM en una caja negra con permisos de producción.</p>
        </section>""",

        """<section class="slide">
          <h2>Qué he construido</h2>
          <div class="flow">
            <div class="box">Prometheus<small>6 reglas K8s</small></div><span class="arr">→</span>
            <div class="box">Alertmanager<small>routing</small></div><span class="arr">→</span>
            <div class="box hot">FastAPI Agent<small>/webhook/alert</small></div><span class="arr">→</span>
            <div class="box hot">RAG + LLM<small>ChromaDB + Ollama</small></div><span class="arr">→</span>
            <div class="box">Validation<small>9 reglas</small></div><span class="arr">→</span>
            <div class="box">Mattermost / K8s<small>aprobar o actuar</small></div>
          </div>
          <div class="cols2">
            <div class="card green"><h3>Resultado funcional</h3><ul><li>Alerta real entra por webhook.</li><li>El agente recupera runbooks relevantes.</li><li>El LLM genera diagnóstico JSON con comandos, confianza y riesgo.</li><li>La capa de seguridad decide: auto-remediar, escalar o sugerir.</li></ul></div>
            <div class="card blue"><h3>Resultado operativo</h3><ul><li>Mensaje en Mattermost con contexto y botones.</li><li>Métricas Prometheus y dashboards Grafana.</li><li>Feedback loop a ChromaDB.</li><li>CI/CD con tests antes de publicar imagen.</li></ul></div>
          </div>
        </section>""",

        """<section class="slide">
          <h2>Arquitectura: decisiones importantes</h2>
          <div class="cols2">
            <div class="card orange"><h3>LLM local in-cluster</h3><p>Ollama con <code>qwen2.5:1.5b</code>. Los datos de la incidencia no salen del cluster y no hay coste por token. Trade-off: latencia alta en CPU.</p></div>
            <div class="card blue"><h3>Retrieval-first</h3><p>ChromaDB busca por similitud semántica en 16 runbooks e incidentes previos. Mejor encaje que un clasificador supervisado con pocos datos.</p></div>
            <div class="card green"><h3>Fail-open</h3><p>Si falla ChromaDB u Ollama, la alerta no se pierde: se notifica degradada. Perder una alerta es peor que perder contexto.</p></div>
            <div class="card red"><h3>Validation layer</h3><p>El LLM nunca ejecuta directamente. Whitelist/blacklist, reglas de riesgo, límites de memoria, bloqueo de acciones destructivas y escalado humano.</p></div>
          </div>
        </section>""",

        """<section class="slide">
          <h2>Fases del proyecto</h2>
          <div class="cols2">
            <div class="card"><h3>Fase 0 · legado Terraform</h3><p>Extracción NLP y generación de IaC. Se conserva como base histórica, pero el proyecto pivota a AIOps sobre Kubernetes.</p></div>
            <div class="card"><h3>Fase 1 · observabilidad</h3><p>Prometheus, Alertmanager, Grafana, kube-state-metrics, reglas K8s y webhook al agente.</p></div>
            <div class="card"><h3>Fase 2 · RAG</h3><p>ChromaDB, embeddings, ingesta de 16 runbooks y diagnóstico contextual con salida JSON estructurada.</p></div>
            <div class="card"><h3>Fase 3 · remediación segura</h3><p>Motor de decisión, ChatOps, botones Aprobar/Rechazar, HMAC, ejecución kubectl y feedback loop.</p></div>
            <div class="card"><h3>Mini-Fase 4 · readiness</h3><p>Rollback automático, slash command <code>/aiops</code>, chaos engineering, dashboards, hardening y pruebas E2E.</p></div>
            <div class="card"><h3>Estado actual</h3><p>Demo end-to-end lista. Lo que queda es mejorar fiabilidad del modelo, deduplicación in-flight y limpieza del store histórico.</p></div>
          </div>
        </section>""",

        f"""<section class="slide">
          <h2>Demo en vivo: lo que voy a enseñar</h2>
          <div class="cols2">
            <div>
              {img("mattermost", "Mattermost con diagnóstico AIOps", "shot side")}
            </div>
            <div class="card orange">
              <h3>Secuencia propuesta</h3>
              <ul>
                <li>Comprobar que el stack está vivo: pods, targets y dashboard.</li>
                <li>Provocar una incidencia controlada en <code>arturo-chaos</code>.</li>
                <li>Ver alerta Prometheus/Alertmanager.</li>
                <li>Ver Mattermost: diagnóstico, pod/namespace, comandos y botones.</li>
                <li>Explicar por qué escala a humano en vez de tocar el cluster automáticamente.</li>
              </ul>
            </div>
          </div>
          <p class="note">En una primera demo es mejor demostrar control y criterio que intentar hacer demasiada magia en directo.</p>
        </section>""",

        """<section class="slide">
          <h2>Seguridad: por qué no es un LLM con kubectl libre</h2>
          <div class="statrow">
            <div class="stat"><b>9</b><span>reglas de decisión</span></div>
            <div class="stat"><b>HMAC</b><span>callbacks Mattermost</span></div>
            <div class="stat"><b>TTL</b><span>escalaciones pendientes</span></div>
            <div class="stat"><b>Rollback</b><span>captura pre-patch</span></div>
          </div>
          <div class="cols2">
            <div class="card green"><h3>Lo permitido</h3><ul><li>Comandos de diagnóstico seguros: <code>get</code>, <code>describe</code>, <code>logs</code>, <code>top</code>.</li><li>Remediación solo si riesgo bajo y confianza alta.</li><li>Persistencia del resultado para auditoría y aprendizaje.</li></ul></div>
            <div class="card red"><h3>Lo bloqueado o escalado</h3><ul><li>Comandos destructivos o desconocidos.</li><li>Reinicios/cambios estructurales sin condiciones seguras.</li><li>Aumentos de memoria excesivos.</li><li>Riesgo alto o baja confianza.</li></ul></div>
          </div>
        </section>""",

        """<section class="slide">
          <h2>Evaluación offline: RAG vs zero-shot</h2>
          <div class="cols2">
            <div class="card blue"><h3>Dataset</h3><ul><li>10 alertas: OOMKilled, CrashLoopBackOff, ImagePullBackOff.</li><li>Scripts en <code>agent/evaluation/</code>.</li><li>Compara recuperación, comandos, safety y confianza.</li></ul></div>
            <div class="card green"><h3>Lectura del resultado</h3><ul><li>RAG aumenta confianza media: <strong>0.86 vs 0.63</strong>.</li><li>Safety RAG: <strong>100% SAFE</strong>.</li><li>Zero-shot genera alucinaciones y un <code>kubectl delete</code> bloqueado.</li></ul></div>
          </div>
          <div class="statrow">
            <div class="stat"><b>60%</b><span>precision@1 retrieval</span></div>
            <div class="stat"><b>80%</b><span>precision@3 retrieval</span></div>
            <div class="stat"><b>100%</b><span>RAG safe commands</span></div>
            <div class="stat"><b>+37%</b><span>confianza relativa</span></div>
          </div>
        </section>""",

        f"""<section class="slide">
          <h2>Validación E2E con chaos engineering</h2>
          {chaos_table()}
          <p class="note"><b>MTTD pipeline</b> mide firing → webhook. <b>T_detect</b> incluye scheduling, periodo <code>for:</code> y ramp de la métrica. Los 4 experimentos escalan a humano: esperado por los gates de seguridad.</p>
        </section>""",

        f"""<section class="slide">
          <h2>Observabilidad del propio sistema</h2>
          <div class="cols2">
            <div>{img("grafana", "Dashboard Grafana AIOps", "shot side")}</div>
            <div class="card blue">
              <h3>Qué se mide</h3>
              <ul>
                <li>Requests al webhook y latencia.</li>
                <li>Diagnósticos OK/error.</li>
                <li>Remediaciones: escalate, auto, human approve/reject.</li>
                <li>Feedback persistido en ChromaDB.</li>
                <li>Métricas chaos MTTD/MTTR.</li>
              </ul>
              <div class="chips"><span class="chip">Prometheus</span><span class="chip">Grafana</span><span class="chip">JSON logs</span><span class="chip">Cloud Build</span></div>
            </div>
          </div>
        </section>""",

        """<section class="slide">
          <h2>Lo que aprendí construyéndolo</h2>
          <div class="cols2">
            <div class="card orange"><h3>Parte técnica</h3><ul><li>Kubernetes real: Deployments, Services, PVC, RBAC, NetworkPolicy, probes y scheduling.</li><li>Prometheus/Alertmanager/Grafana sin operador, con reglas propias.</li><li>FastAPI asíncrono, Pydantic, httpx, tests y CI/CD.</li><li>LLM + RAG en entorno con restricciones.</li></ul></div>
            <div class="card green"><h3>Parte de ingeniería</h3><ul><li>Medir antes de afirmar.</li><li>Documentar trade-offs y deuda técnica.</li><li>Diseñar fallos controlados para validar comportamiento.</li><li>Priorizar seguridad operacional sobre automatización vistosa.</li></ul></div>
          </div>
        </section>""",

        """<section class="slide">
          <h2>Limitaciones honestas</h2>
          <div class="cols2">
            <div class="card red"><h3>Latencia del LLM</h3><p>qwen2.5:1.5b en CPU tarda ~205-270 s en algunos diagnósticos. El pipeline detecta rápido, pero el diagnóstico completo depende del modelo.</p></div>
            <div class="card red"><h3>Modelo pequeño</h3><p>Puede tener confianza alta con razonamiento textual imperfecto. Los campos estructurados vienen de la alerta; el texto libre requiere supervisión.</p></div>
            <div class="card orange"><h3>RAG contaminable</h3><p>Un bug de labels guardó incidentes con pod/namespace incorrecto. Detectado y documentado; falta limpieza/versionado del store histórico.</p></div>
            <div class="card orange"><h3>Escalaciones en memoria</h3><p>El estado de botones vive en memoria con TTL. Para producción real convendría Redis, Postgres o ChromaDB como backend persistente.</p></div>
          </div>
        </section>""",

        """<section class="slide">
          <h2>Próximos pasos</h2>
          <div class="cols3">
            <div class="card green"><h3>Corto plazo</h3><ul><li>Deduplicación in-flight por alerta+pod.</li><li>Limpiar incidentes pre-fix del RAG.</li><li>Mejorar mensajes cuando hay timeout.</li></ul></div>
            <div class="card blue"><h3>Medio plazo</h3><ul><li>Persistir escalaciones fuera de memoria.</li><li>Calibrar confidence con validaciones deterministas.</li><li>Ampliar dataset a 30-50 alertas.</li></ul></div>
            <div class="card orange"><h3>Línea futura</h3><ul><li>Modelo mayor o GPU.</li><li>Predicción proactiva desde Prometheus.</li><li>Integración con runbooks corporativos.</li></ul></div>
          </div>
        </section>""",

        """<section class="slide">
          <h2>Cierre</h2>
          <p class="quote">He construido un pipeline AIOps completo sobre Kubernetes: observa, diagnostica con contexto, decide con reglas de seguridad, escala al operador y deja evidencia.</p>
          <div class="statrow">
            <div class="stat"><b>16</b><span>runbooks RAG</span></div>
            <div class="stat"><b>369</b><span>tests</span></div>
            <div class="stat"><b>4</b><span>experimentos chaos</span></div>
            <div class="stat"><b>5-10s</b><span>MTTD pipeline</span></div>
          </div>
          <p class="note">Mensaje principal para el tutor: no es solo una demo de IA; es una pieza de plataforma con seguridad, observabilidad y criterios de operación.</p>
        </section>""",
    ]


GUION = [
    ("1. Apertura", "No empieces pidiendo perdón por ser tu primera demo. Di el objetivo: enseñar un sistema AIOps real en Kubernetes, construido durante las prácticas, y explicar decisiones como lo haría un equipo de plataforma."),
    ("2. Problema", "Enmarca la necesidad: alertas, runbooks y decisión operacional están separados. La demo intenta unirlos sin dar permisos ciegos a un LLM."),
    ("3. Arquitectura", "Recorre el flujo de izquierda a derecha. Repite tres ideas: todo in-cluster, fail-open y validation layer obligatoria."),
    ("4. Demo en vivo", "Antes de romper nada, enseña estado estable: pods Running, Prometheus targets UP, dashboard. Luego inyecta un fallo controlado. Si se retrasa, usa capturas/logs como evidencia."),
    ("5. Seguridad", "Explica que 4/4 escalate no es fracaso. Es la capa de seguridad haciendo lo correcto: riesgo alto implica humano en el loop."),
    ("6. Evidencia", "Usa los datos sin venderlos de más: RAG mejora safety/confianza; chaos valida latencia pipeline; MTTR está dominado por CPU del LLM."),
    ("7. Limitaciones", "Sé concreto: latencia, modelo pequeño, RAG contaminable, estado en memoria. Tu credibilidad sube si sabes decir dónde están los límites."),
    ("8. Cierre", "Cierra con lo construido y lo aprendido: plataforma, observabilidad, seguridad operacional y una ruta clara de mejora."),
]

QA = [
    ("¿Por qué escala a humano si hay remediación automática?", "Porque los experimentos generan riesgo alto o cambios que pueden reiniciar pods. El motor está diseñado para auto-remediar solo casos de bajo riesgo y alta confianza; lo demás se aprueba en Mattermost."),
    ("¿Por qué usar un LLM local tan pequeño?", "Por restricciones de entorno: datos dentro del cluster, sin API externa y sin coste por token. La contrapartida es latencia y menor calidad de razonamiento; por eso hay RAG y validación determinista."),
    ("¿Qué pasa si ChromaDB u Ollama fallan?", "Fail-open: la alerta sigue notificándose. Si falla RAG se diagnostica sin contexto; si falla LLM se manda alerta cruda. La prioridad es no perder alertas."),
    ("¿Esto toca producción real?", "Es un cluster GKE real del proyecto, no docker-compose, pero el scope es académico y los fallos se inyectan en un namespace aislado para no afectar sistemas productivos."),
    ("¿Qué demuestra la evaluación?", "Que RAG mejora la seguridad y la confianza frente a zero-shot. No demuestra perfección estadística porque N=10 es pequeño; sirve como primera validación y base para ampliar dataset."),
    ("¿Cuál es la mayor deuda técnica?", "Deduplicar diagnósticos in-flight y limpiar/versionar el store de ChromaDB tras el bug de labels. Ambas son mejoras claras antes de un piloto más serio."),
]


def build_deck() -> str:
    return (
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{TITLE} — Demo</title><style>{CSS}</style></head><body>"
        + "\n".join(slides())
        + "<div class='counter'></div><div class='bar'></div>"
        + f"<script>{JS}</script></body></html>"
    )


def build_guion() -> str:
    css = """
    body{margin:0;background:#0b1017;color:#edf2f7;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;line-height:1.6;padding:5vh 8vw}
    h1{color:#ff8a3d} h2{border-bottom:1px solid #293747;padding-bottom:.25em;margin-top:1.4em}
    .block{background:#151d28;border:1px solid #293747;border-left:4px solid #ff5a00;border-radius:8px;padding:1em 1.2em;margin:.9em 0}
    .q{color:#ff8a3d;font-weight:700;margin-top:1em}.a{color:#b6c2cf} code{background:#080d13;border:1px solid #293747;border-radius:5px;padding:.04em .3em;color:#ffd8bd}
    """
    blocks = "".join(f"<div class='block'><h2>{title}</h2><p>{text}</p></div>" for title, text in GUION)
    qa = "".join(f"<p class='q'>{q}</p><p class='a'>{a}</p>" for q, a in QA)
    return (
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        f"<title>{TITLE} — Guion</title><style>{css}</style></head><body>"
        f"<h1>Guion — {TITLE}</h1><p>{SUBTITLE} · {AUTHOR} · {DATE}</p>"
        "<div class='block'><strong>Fallback si la demo falla:</strong> di que es una incidencia real del directo, cambia a capturas/logs y continúa el flujo. No intentes depurar en silencio delante del tutor.</div>"
        f"{blocks}<h2>Preguntas probables</h2>{qa}</body></html>"
    )


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    deck = build_deck()
    guion = build_guion()
    (OUT_DIR / "demo.html").write_text(deck, encoding="utf-8")
    (OUT_DIR / "guion.html").write_text(guion, encoding="utf-8")

    print("== build_demo ==")
    print(f"  demo/demo.html  {len(deck)//1024} KB")
    print(f"  demo/guion.html {len(guion)//1024} KB")
    if _EMBEDDED:
        print("  imagenes embebidas:")
        for item in _EMBEDDED:
            print(f"    + {item}")
    if _MISSING:
        print("  imagenes pendientes:")
        for item in _MISSING:
            print(f"    - {item}")


if __name__ == "__main__":
    main()
