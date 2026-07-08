#!/usr/bin/env python3
"""Genera la demo HTML autocontenida y el guion del presentador.

Uso:
  python3 scripts/build_demo.py

Salida:
  demo/demo.html   Presentación offline, navegable con flechas/click.
  demo/guion.html  Guion práctico para la presentación al chapter de empresa.
"""

from __future__ import annotations

import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "demo"

TITLE = "AIOps Infrastructure Agent"
SUBTITLE = "Diagnóstico y remediación asistida de incidencias en Kubernetes"
AUTHOR = "Arturo Fernández"
DATE = "2026-07-07"
SUBJECT = "Presentación a chapter · MasOrange / Telecable"

# Fuente única de números — evita el drift de literales dispersos por slides.
STATS = {
    "tests": "621",
    "runbooks": "16",
    "p1_before": "73%", "p1_after": "100%",
    "p3_before": "87%", "p3_after": "100%",
    "eval_n": "15",
    "conf_rag": "0.86", "conf_zero": "0.63",
    "audit_before": "5.9", "audit_after": "7.1",
    "chaos_experiments": "4",
    "cap": "2×",
}

CHAOS_RESULTS = [
    {"exp": "OOMKilled", "for": "0m", "mttd": "5.0", "mttr": "205.4", "conf": "0.95", "outcome": "escalate", "detect": "39"},
    {"exp": "CrashLoopBackOff", "for": "5m", "mttd": "5.0", "mttr": "205.7", "conf": "0.95", "outcome": "escalate", "detect": "195"},
    {"exp": "ImagePullBackOff", "for": "1m", "mttd": "5.1", "mttr": "252.1", "conf": "0.80", "outcome": "escalate", "detect": "171"},
    {"exp": "HighCPU", "for": "5m", "mttd": "10.1", "mttr": "206.7", "conf": "0.00", "outcome": "suggest_only", "detect": "609"},
]

IMAGES = {
    "grounded": ["demo/mattermost_escalation_grounded_confidence.png"],
    "cured": ["demo/mattermost_cured.png"],
    "grafana_overview": ["demo/grafana_overview_top.png"],
    "grafana_queue": ["demo/grafana_queue_row.png"],
}

_EMBEDDED: list[str] = []
_MISSING: list[str] = []


def data_uri(key: str) -> str | None:
    for rel in IMAGES[key]:
        path = ROOT / rel
        if path.exists():
            mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            _EMBEDDED.append(f"{key}: {rel} ({path.stat().st_size // 1024} KB)")
            return f"data:{mime};base64,{b64}"
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
        # 1 · Portada — tesis
        f"""<section class="slide">
          <div class="kicker">{SUBJECT}</div>
          <h1>{TITLE}</h1>
          <p class="subtitle">{SUBTITLE}</p>
          <p class="quote">El cluster informa, el modelo razona, el motor dispone.</p>
          <p class="note">Prototipo de prácticas: una prueba de concepto para explorar si un LLM local, con una capa de seguridad determinista alrededor, puede ayudar a diagnosticar incidencias de Kubernetes. Construido y validado en un cluster real.</p>
          <div class="meta"><span>{AUTHOR}</span><span>{DATE}</span><span>GKE · Prometheus · Ollama · ChromaDB · Redis · Mattermost</span></div>
        </section>""",

        # 2 · El problema que quise explorar
        """<section class="slide">
          <h2>El problema que quise explorar</h2>
          <div class="cols3">
            <div class="card red"><h3>Contexto disperso</h3><p>Cuando salta una alerta de Kubernetes, el operador tiene que juntar a mano: qué pod falla, qué runbook aplica y qué comando es seguro.</p></div>
            <div class="card orange"><h3>La pregunta</h3><p>¿Puede un LLM ayudar a juntar ese contexto y proponer el arreglo, sin convertirse en una caja negra con permisos sobre el cluster?</p></div>
            <div class="card blue"><h3>Restricciones del entorno</h3><p>Datos dentro del cluster, sin API externa y con recursos limitados. Quería ver si el enfoque era viable en esas condiciones, no en un notebook.</p></div>
          </div>
          <p class="note">El objetivo no era un producto: era una prueba de concepto para ver si la idea merecería desarrollarse.</p>
        </section>""",

        # 3 · La idea, y por qué ahora
        """<section class="slide">
          <h2>La idea, y por qué ahora</h2>
          <div class="cols2">
            <div class="card green"><h3>La idea en una frase</h3><p>Unir <strong>alerta + contexto + decisión segura</strong> en el mismo flujo: el sistema trae el runbook, propone el arreglo y lo ejecuta solo cuando es seguro — o escala con el comando ya preparado.</p></div>
            <div class="card orange"><h3>Por qué ahora</h3><p>Los LLMs hacen viable diagnosticar en lenguaje natural sobre runbooks. Pero un LLM con <code>kubectl</code> libre es peligroso. La clave no es el modelo: es la <strong>capa de seguridad determinista</strong> que lo rodea.</p></div>
          </div>
        </section>""",

        # 4 · Qué he construido — pipeline con cola F2 + grounding
        """<section class="slide">
          <h2>Qué he construido</h2>
          <div class="flow">
            <div class="box">Prometheus<small>6 reglas K8s</small></div><span class="arr">→</span>
            <div class="box">Alertmanager<small>routing</small></div><span class="arr">→</span>
            <div class="box hot">Cola Redis<small>Streams · fail-closed</small></div><span class="arr">→</span>
            <div class="box hot">Grounding<small>snapshot cluster</small></div><span class="arr">→</span>
            <div class="box hot">RAG + LLM<small>ChromaDB + Ollama</small></div><span class="arr">→</span>
            <div class="box">Motor<small>9 reglas</small></div><span class="arr">→</span>
            <div class="box">Mattermost / K8s<small>aprobar o actuar</small></div>
          </div>
          <div class="cols2">
            <div class="card green"><h3>Resultado funcional</h3><ul><li>La alerta se encola (fail-closed) y se drena 1 a 1.</li><li>El cluster se consulta <strong>antes</strong> del LLM (grounding).</li><li>El LLM genera diagnóstico JSON; el motor re-fuente la decisión.</li><li>Auto-remedia, escala o sugiere según reglas de seguridad.</li></ul></div>
            <div class="card blue"><h3>Resultado operativo</h3><ul><li>Mensaje en Mattermost con contexto grounded y botones HMAC.</li><li>Métricas Prometheus y dashboards Grafana.</li><li>Feedback loop a ChromaDB (veredicto <code>cured</code>/<code>rolled_back</code>).</li><li>CI en cada push + Cloud Build con tests como gate.</li></ul></div>
          </div>
        </section>""",

        # 4 · Tesis en 3 capas (keystone)
        """<section class="slide">
          <h2>La tesis en tres capas</h2>
          <div class="cols3">
            <div class="card blue"><h3>El cluster informa</h3><p>Una etapa determinista consulta <code>kubectl</code> y construye un <em>snapshot</em> real: límites, fase, reinicios, identidad del workload. Hechos, no suposiciones.</p></div>
            <div class="card orange"><h3>El modelo razona</h3><p>El LLM recibe esos hechos + los runbooks del RAG y produce un diagnóstico. Queda reducido a <em>qué campo</em> y <em>en qué dirección</em>, no a inventar valores.</p></div>
            <div class="card green"><h3>El motor dispone</h3><p>Un motor de 9 reglas re-fuente la decisión: sella la identidad con la verdad del cluster, acota el cambio y decide auto / escalar / sugerir. El LLM nunca ejecuta.</p></div>
          </div>
          <p class="note">Cada capa desconfía de la anterior. Esa desconfianza es el diseño.</p>
        </section>""",

        # 5 · Grounding (Eje A)
        f"""<section class="slide">
          <h2>El cluster informa: grounding</h2>
          <div class="cols2">
            <div>{img("grounded", "Escalación con confidence grounded en Mattermost", "shot side")}</div>
            <div class="card blue">
              <h3><code>enrichment.py</code> antes del LLM</h3>
              <ul>
                <li>Snapshot: container, limits, phase, restart_count, last_state_reason.</li>
                <li>Identidad por <code>ownerReferences</code> (pod→ReplicaSet→Deployment).</li>
                <li>Los hechos entran al prompt como sección <strong>CLUSTER FACTS</strong>.</li>
                <li>La <em>confianza</em> se deriva del cluster, no del modelo.</li>
              </ul>
              <p class="note">En el ejemplo: <strong>100% grounded del cluster</strong>; el modelo decía 65%. Fail-soft: si el <code>kubectl</code> falla, nunca bloquea el pipeline.</p>
            </div>
          </div>
        </section>""",

        # 6 · El motor dispone
        f"""<section class="slide">
          <h2>El motor dispone: por qué no es un LLM con kubectl libre</h2>
          <div class="statrow">
            <div class="stat"><b>9</b><span>reglas en cascada</span></div>
            <div class="stat"><b>≤{STATS['cap']}</b><span>cap del cambio (regla 4.6)</span></div>
            <div class="stat"><b>HMAC</b><span>callbacks Mattermost</span></div>
            <div class="stat"><b>fail-closed</b><span>auth y cooldown</span></div>
          </div>
          <div class="cols2">
            <div class="card green"><h3>Re-sourcing: el modelo propone, el motor dispone</h3><ul><li>El motor <strong>sella</strong> name/ns/container con la verdad del cluster.</li><li>Sintetiza el comando (determinista, reversible por snapshot).</li><li>Auto solo si estructurado, subir-solo y en el allow-list <code>arturo-</code>.</li></ul></div>
            <div class="card red"><h3>Lo bloqueado o escalado</h3><ul><li>Comandos destructivos o desconocidos (blacklist regex).</li><li>Target fantasma o <code>kind≠Deployment</code> → escala (regla 4.7).</li><li>Cooldown por workload: un patch por ventana (corta el patch-storm).</li></ul></div>
          </div>
        </section>""",

        # 7 · Learning loop + cured
        f"""<section class="slide">
          <h2>El sistema aprende de lo que hace</h2>
          <div class="cols2">
            <div>{img("cured", "Veredicto cured notificado en Mattermost", "shot side")}</div>
            <div class="card green">
              <h3>Feedback loop cerrado</h3>
              <ul>
                <li>Tras remediar, se programa una evaluación de rollback.</li>
                <li>El veredicto <code>cured</code> / <code>rolled_back</code> re-marca el doc en ChromaDB.</li>
                <li>Los fracasos se conservan como <em>conocimiento negativo</em> recuperable.</li>
                <li><strong>Paridad humano/auto</strong>: aprobar en Mattermost alimenta el mismo bucle.</li>
              </ul>
              <p class="note">Validado E2E en cluster (06-jul): <code>aiops_feedback_verdict_total{{outcome="cured"}}=1</code>. Cuantificar el <em>feedback-loop gain</em> es el siguiente paso medible.</p>
            </div>
          </div>
        </section>""",

        # 8 · Retrieval evaluado
        f"""<section class="slide">
          <h2>El modelo razona: retrieval evaluado</h2>
          <div class="cols2">
            <div class="card blue"><h3>Dataset y método</h3><ul><li>N={STATS['eval_n']} alertas: OOM, CrashLoop, ImagePull, HighCPU, HighMemory.</li><li>Scripts en <code>agent/evaluation/</code> — medido en cluster.</li><li>R1: filtro por <code>error_class</code> derivado del alertname, fallback semántico.</li></ul></div>
            <div class="card green"><h3>RAG vs zero-shot</h3><ul><li>Safety RAG: <strong>100% SAFE</strong> vs 25% zero-shot.</li><li>Confianza media: <strong>{STATS['conf_rag']} vs {STATS['conf_zero']}</strong> (+37% rel.).</li><li>Zero-shot alucinó un <code>kubectl delete</code> — bloqueado por el motor.</li></ul></div>
          </div>
          <div class="statrow">
            <div class="stat"><b>{STATS['p1_before']}→{STATS['p1_after']}</b><span>precision@1 (R1 filter)</span></div>
            <div class="stat"><b>{STATS['p3_before']}→{STATS['p3_after']}</b><span>precision@3 (R1 filter)</span></div>
            <div class="stat"><b>100%</b><span>RAG safe commands</span></div>
            <div class="stat"><b>+37%</b><span>confianza relativa</span></div>
          </div>
          <p class="note">Determinista donde se puede, semántico como fallback.</p>
        </section>""",

        # 9 · Seguridad de plataforma
        """<section class="slide">
          <h2>Seguridad y guardarraíles</h2>
          <div class="cols2">
            <div class="card green"><h3>Autenticación y estado durables</h3><ul><li>Callbacks firmados con HMAC-SHA256.</li><li>Auth <strong>fail-closed</strong>: secret vacío rechaza (401), no fail-open.</li><li>Escalaciones y rollback persistidos en <strong>Redis</strong>.</li></ul></div>
            <div class="card red"><h3>Blast radius acotado</h3><ul><li>Solo namespaces <code>arturo-*</code>; sin ClusterRoles de escritura.</li><li>Rollback durable: se re-arma al reinicio a mitad de ventana.</li><li>Cola fail-closed: si Redis cae, Alertmanager reintenta — no se pierde la alerta.</li></ul></div>
          </div>
          <p class="note">Perder una alerta es peor que perder contexto: fail-open en el diagnóstico, fail-closed en lo que actúa.</p>
        </section>""",

        # 10 · Validación E2E chaos
        f"""<section class="slide">
          <h2>Validación E2E con chaos engineering</h2>
          {chaos_table()}
          <p class="note"><b>MTTD pipeline</b> (firing → webhook) es de segundos. El <b>MTTR</b> está dominado por el <strong>hardware</strong> — el LLM 1.5b en CPU tarda ~150-270 s —, no por el sistema. Los 4 experimentos escalan a humano: esperado por los gates de seguridad.</p>
        </section>""",

        # 11 · Auditoría honesta — el foso
        f"""<section class="slide">
          <h2>Auditoría honesta: {STATS['audit_before']} → {STATS['audit_after']}</h2>
          <p class="quote">Encontré tres razones para rechazar mi propio sistema. Las cerré.</p>
          <div class="cols3">
            <div class="card red"><h3>Auth fail-open</h3><p>Un secret vacío dejaba pasar remediación no autenticada. Ahora rechaza (401) con <code>DRY_RUN=false</code>.</p></div>
            <div class="card red"><h3>Rollback volátil</h3><p>Un reinicio a mitad de ventana dejaba el patch aplicado para siempre. Ahora se persiste en Redis y se re-arma.</p></div>
            <div class="card red"><h3>Grounding ausente</h3><p>El LLM inventaba el valor actual. Ahora el snapshot del cluster sella la identidad y el valor.</p></div>
          </div>
          <p class="note">Me auditué el proyecto con lente de arquitecto senior: la nota pasó de {STATS['audit_before']} a {STATS['audit_after']} cerrando 3 P0 + hallazgos F-xx. Prefiero enseñar eso a esconderlo.</p>
        </section>""",

        # 12 · Observabilidad
        f"""<section class="slide">
          <h2>Observabilidad del propio sistema</h2>
          <div class="cols2">
            <div>{img("grafana_overview", "Grafana overview AIOps", "shot side")}</div>
            <div>{img("grafana_queue", "Fila Cola Redis Streams en Grafana", "shot side")}</div>
          </div>
          <div class="chips">
            <span class="chip">aiops_diagnosis_total</span><span class="chip">aiops_enrichment_total</span>
            <span class="chip">aiops_queue_* (cola F2)</span><span class="chip">aiops_feedback_verdict_total</span>
            <span class="chip">aiops_chaos_* (MTTD/MTTR)</span>
          </div>
          <p class="note">Cada decisión del pipeline emite métrica: grounding, cola, remediación, veredicto. Zero black-box.</p>
        </section>""",

        # 13 · Evidencia en cluster
        """<section class="slide">
          <h2>Evidencia en cluster (no en la teoría)</h2>
          <div class="cols2">
            <div class="card green"><h3>Grounding real (04-jul)</h3><ul><li><code>current_value=32Mi</code> vino del snapshot, no del LLM (<code>grounded=1.0</code>).</li><li>Safety cap 4.6: LLM pidió 512Mi (16×) → el motor escaló a 2×, no clampó a ciegas.</li></ul></div>
            <div class="card blue"><h3>Ciclo completo (06-jul)</h3><ul><li>Human-in-the-loop E2E: approve HMAC OK → patch persiste.</li><li>Arco <code>cured</code> completo → doc ChromaDB re-marcado + métrica.</li></ul></div>
          </div>
          <p class="note">Todos los hallazgos del run tienen causa raíz documentada; ninguno bloquea el diseño.</p>
        </section>""",

        # 14 · Límites honestos + qué quedaría por hacer
        """<section class="slide">
          <h2>Límites honestos, y qué quedaría por comprobar</h2>
          <div class="cols2">
            <div class="card red"><h3>Latencia = techo de hardware</h3><p>qwen2.5:1.5b en CPU ~150-270 s. El pipeline detecta en segundos; el diagnóstico lo limita el hardware, no el diseño. Palanca: GPU o modelo desplegable mayor.</p></div>
            <div class="card orange"><h3>Durabilidad de Redis</h3><p>El estado vive en Redis, pero sin AOF+PVC no sobrevive a la pérdida del pod. No reclamo durabilidad fuerte sobre memoria volátil: es trabajo pendiente (F-06).</p></div>
            <div class="card orange"><h3>Dataset acotado</h3><p>N=15 alertas: suficiente para validar el filtro R1, corto para afirmar estadística. Ampliar a 30-50.</p></div>
            <div class="card blue"><h3>Feedback-loop gain</h3><p>El bucle está vivo (veredictos reales en ChromaDB). Falta <em>cuantificar</em> la mejora de retrieval con incidentes poblados vs vacío.</p></div>
          </div>
          <p class="note">Si la idea mereciera seguir, el paso natural sería probarla sobre alertas reales de un equipo y ver si de verdad ahorra tiempo. Hoy es una hipótesis, no una promesa.</p>
        </section>""",

        # 15 · Cierre
        f"""<section class="slide">
          <h2>Cierre</h2>
          <p class="quote">El cluster informa, el modelo razona, el motor dispone. Un prototipo construido despacio, con la seguridad y la honestidad sobre lo que hace (y lo que no) como principio.</p>
          <div class="statrow">
            <div class="stat"><b>{STATS['runbooks']}</b><span>runbooks RAG</span></div>
            <div class="stat"><b>{STATS['tests']}</b><span>tests (verde)</span></div>
            <div class="stat"><b>{STATS['audit_before']}→{STATS['audit_after']}</b><span>nota de auditoría</span></div>
            <div class="stat"><b>{STATS['p1_after']}</b><span>precision@1 retrieval</span></div>
          </div>
        </section>""",
    ]


GUION = [
    ("1. Apertura", "Abre sin disculpas pero sin humos: es un prototipo de prácticas, una prueba de concepto para ver si la idea es viable. La tesis 'el cluster informa, el modelo razona, el motor dispone' es el hilo. Lo construí y lo validé en un cluster real."),
    ("2. El problema", "Enmarca la pregunta que quisiste explorar: cuando salta una alerta, el contexto está disperso. ¿Puede un LLM ayudar a juntarlo y proponer el arreglo sin ser una caja negra con permisos? No vendas dolor de negocio: cuenta por qué te pareció interesante."),
    ("3. Las tres capas", "Este es el hilo conductor. Cada capa desconfía de la anterior: el grounding aporta hechos del cluster, el LLM razona sobre ellos, el motor re-fuente y decide. El LLM nunca ejecuta."),
    ("4. Grounding", "Insiste en el keystone: el valor actual y la identidad del workload vienen del snapshot de kubectl, no del modelo. Enseña la captura del 100% grounded frente al 65% del modelo."),
    ("5. Motor y seguridad", "Explica el re-sourcing: el motor sella la identidad, acota el cambio a ≤2× y decide auto/escalar/sugerir. Auth fail-closed, cooldown por workload, rollback durable en Redis."),
    ("6. Aprendizaje", "El bucle está cerrado y validado: el veredicto cured re-marca el doc en ChromaDB, y aprobar en Mattermost alimenta el mismo bucle (paridad humano/auto). Cuantificar el gain es el siguiente paso."),
    ("7. Auditoría honesta", "Este es el foso. Cuenta que una auto-auditoría subió la nota de 5.9 a 7.1 cerrando 3 P0 (auth, rollback, grounding). Madurez sobre features vistosas."),
    ("8. Límites y cierre", "Sé concreto con los límites: latencia = techo de hardware, durabilidad de Redis pendiente (F-06), dataset N=15. Di, sin sobrevenderlo, que si mereciera seguir el paso natural sería probarlo sobre alertas reales y medir si ahorra tiempo — hoy es una hipótesis. Cierra con la tesis y los 621 tests. La credibilidad sube al decir dónde están los bordes."),
]

QA = [
    ("¿Por qué escala a humano si hay remediación automática?", "El motor auto-remedia solo casos estructurados, de subir-solo, en el allow-list arturo-, con confianza grounded suficiente. Lo demás escala con el comando ya sintetizado para que el humano apruebe. Los experimentos chaos generan riesgo alto o target no resoluble, así que escalan por diseño."),
    ("¿Por qué un LLM local tan pequeño?", "Por restricciones de entorno: datos dentro del cluster, sin API externa, sin coste por token. Y porque medí que con el contexto servido (grounding + runbook), el 1.5b acierta el field 5/5 en ~2s. El fallo de cluster era el límite ausente en la alerta, no el modelo. La palanca era enriquecer contexto, no GPU."),
    ("El modelo pequeño puede tener confianza alta con razonamiento imperfecto. ¿Cómo lo mitigas?", "No confío en la confianza del modelo: la derivo de señales del cluster (last_state_reason + restart_count). La del modelo queda preservada como model_confidence, pero la que gobierna la decisión es la grounded. Además el motor sella los valores, así que el texto libre del LLM no llega a ejecutarse."),
    ("¿Qué pasa si ChromaDB, Ollama o Redis fallan?", "Fail-open en el diagnóstico (si falta RAG o LLM, se notifica degradado — no se pierde la alerta) y fail-closed en lo que actúa (si Redis cae, la cola devuelve 503 y Alertmanager reintenta; auth con secret vacío rechaza). Perder una alerta es peor que perder contexto."),
    ("¿Qué demuestra la evaluación con N=15?", "Que el filtro R1 por error_class sube precision@1 de 73% a 100% y que RAG da 100% de comandos SAFE frente al 25% de zero-shot. N=15 valida el mecanismo, no afirma estadística fina; el plan es ampliar a 30-50. No lo vendo de más."),
    ("¿Reclamas durabilidad con el estado en Redis?", "No del todo, y lo digo abiertamente. El estado (escalaciones, rollback) se persiste en Redis y se re-arma al reinicio, pero sin AOF+PVC no sobrevive a la pérdida del pod. Es el hallazgo F-06 del backlog: o lo endurezco con AOF+PVC o degrado el claim. Prefiero no reclamar durabilidad fuerte sobre memoria volátil."),
    ("Si el sistema propone factible pero no seguro, ¿qué gana?", "La seguridad. La capa de validación valida seguridad, no factibilidad — que kubectl top diera Forbidden por least-privilege no es una vía para saltarse un gate. Ante la duda, escala al humano con el comando ya preparado."),
    ("¿Esto toca producción real?", "Es un cluster GKE real del proyecto, no docker-compose, con observabilidad y CI reales. Los fallos se inyectan en un namespace aislado (arturo-chaos) y toda la escritura está acotada a arturo-* sin ClusterRoles de escritura, para no afectar a nadie más en el cluster compartido."),
    ("¿Por qué no usar Datadog u otra herramienta comercial?", "Existen y para muchos casos serían la respuesta. Yo no intentaba competir con ellas: quería explorar como ejercicio si se podía hacer algo self-hosted, con el LLM local (los datos no salen del cluster) y una capa de decisión que fuera código propio y auditable. El valor era sobre todo aprender construyéndolo."),
    ("¿Esto ahorra tiempo/dinero? ¿Tienes ROI?", "No, y no voy a inventarlo: es un prototipo de prácticas. Lo que sí tengo instrumentado son las métricas para medirlo (tiempo de diagnóstico, % de alertas pre-diagnosticadas) si algún día se probara con alertas reales. Prometer un número hoy sería deshonesto."),
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
        "<div class='block'><strong>Fallback si la demo falla:</strong> di que es una incidencia real del directo, cambia a capturas/logs y continúa el flujo. No intentes depurar en silencio delante de la sala.</div>"
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
