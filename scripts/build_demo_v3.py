#!/usr/bin/env python3
"""Genera el deck v3 (presentación al chapter, registro de empresa) y su guion.

Uso:
  python3 scripts/build_demo_v3.py

Salida:
  demo/demo_v3.html   Deck offline autocontenido (imágenes + fuentes embebidas).
  demo/guion_v3.html  Runbook de la presentación + banco de preguntas probables.

Principios del v3 (acordados con Jay):
  - Registro: 1ª persona del plural / impersonal, también en las notas del orador.
  - Sin fechas en ninguna parte del deck ("validado en el cluster", sin día).
  - Sin registro académico: es una presentación de empresa, no una defensa.
  - Identidad: editorial claro (papel + tinta + hairlines), naranja como único
    acento, un verde reservado SOLO para el veredicto cured. Notas de honestidad
    en grafito. Tipografía IBM Plex (Sans/Serif/Mono) vendorizada offline.
  - Firma visual: el arco de seis etapas (alerta → informa → razona → dispone →
    humano → veredicto) — animado en portada, raíl en los dividers, replay a
    tamaño completo en la slide de demo y HUD de progreso.
  - Núcleo ~15 min + capa de RESPALDO (R1-R4) tras el cierre, contada aparte.
  - Escenario lógico 1280×720; exporta a PDF (1 diap./página) con Ctrl+P.
"""

from __future__ import annotations

import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "demo"
FONT_DIR = OUT_DIR / "assets" / "fonts"

TITLE = "Observz"
SUBTITLE = "Diagnóstico y remediación asistida de incidencias en Kubernetes"
AUTHOR = "Arturo Fernández"
SUBJECT = "MasOrange / Telecable"
# Sin DATE a propósito: el deck no lleva fechas.

# Fuente única de números — evita el drift de literales dispersos por slides.
STATS = {
    "tests": "696",  # pytest full 2026-07-11 (692 verdes + 4 arreglados en sesión) — re-confirmar verde antes de presentar
    "runbooks": "16",
    "alert_rules": "6",
    "engine_rules": "9",
    "p1_before": "73%", "p1_after": "100%",
    "p3_before": "87%", "p3_after": "100%",
    "eval_n": "15",
    "conf_rag": "0.86", "conf_zero": "0.63",
    "chaos_experiments": "4",
    "cap": "2×",
    # R4 feedback-loop gain (fixture sintético declarado)
    "r4_a_empty_p1": "0%", "r4_a_seed_p1": "46,7%", "r4_a_seed_p2": "60%",
}

CHAOS_RESULTS = [
    {"exp": "OOMKilled", "for": "0m", "mttd": "5,0", "mttr": "205,4", "conf": "0,95", "outcome": "escalate", "detect": "39"},
    {"exp": "CrashLoopBackOff", "for": "5m", "mttd": "5,0", "mttr": "205,7", "conf": "0,95", "outcome": "escalate", "detect": "195"},
    {"exp": "ImagePullBackOff", "for": "1m", "mttd": "5,1", "mttr": "252,1", "conf": "0,80", "outcome": "escalate", "detect": "171"},
    {"exp": "HighCPU", "for": "5m", "mttd": "10,1", "mttr": "206,7", "conf": "0,00", "outcome": "suggest_only", "detect": "609"},
]

IMAGES = {
    "grounded": ["demo/mattermost_escalation_grounded_confidence.png"],
    "cured": ["demo/mattermost_cured.png"],
    "grafana_overview": ["demo/grafana_overview_top.png"],
    "grafana_queue": ["demo/grafana_queue_row.png"],
}

# (familia CSS, peso, estilo, fichero) — IBM Plex, licencia OFL
FONTS = [
    ("Plex", 400, "normal", "IBMPlexSans-400.woff2"),
    ("Plex", 500, "normal", "IBMPlexSans-500.woff2"),
    ("Plex", 600, "normal", "IBMPlexSans-600.woff2"),
    ("Plex", 700, "normal", "IBMPlexSans-700.woff2"),
    ("PlexMono", 400, "normal", "IBMPlexMono-400.woff2"),
    ("PlexMono", 500, "normal", "IBMPlexMono-500.woff2"),
    ("PlexMono", 600, "normal", "IBMPlexMono-600.woff2"),
    ("PlexSerif", 600, "normal", "IBMPlexSerif-600.woff2"),
    ("PlexSerif", 600, "italic", "IBMPlexSerif-600Italic.woff2"),
    ("PlexSerif", 700, "normal", "IBMPlexSerif-700.woff2"),
]

_EMBEDDED: list[str] = []
_MISSING: list[str] = []


def data_uri(key: str) -> str | None:
    for rel in IMAGES[key]:
        path = ROOT / rel
        if path.exists():
            mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            _EMBEDDED.append(f"img {key}: {rel} ({path.stat().st_size // 1024} KB)")
            return f"data:{mime};base64,{b64}"
    _MISSING.append(f"img {key}: {', '.join(IMAGES[key])}")
    return None


def img(key: str, alt: str, cls: str = "figframe") -> str:
    uri = data_uri(key)
    if uri:
        return f'<div class="{cls}"><img src="{uri}" alt="{alt}"></div>'
    return f'<div class="placeholder">Captura pendiente: <code>{IMAGES[key][0]}</code></div>'


def font_css() -> str:
    rules = []
    for family, weight, style, fname in FONTS:
        path = FONT_DIR / fname
        if not path.exists():
            _MISSING.append(f"font: {fname}")
            continue
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        _EMBEDDED.append(f"font {fname} ({path.stat().st_size // 1024} KB)")
        ital = "font-style:italic;" if style == "italic" else ""
        rules.append(
            f"@font-face{{font-family:'{family}';font-weight:{weight};{ital}"
            f"font-display:swap;src:url(data:font/woff2;base64,{b64}) format('woff2')}}"
        )
    return "\n".join(rules)


def chaos_table() -> str:
    head = (
        "<tr><th>Experimento</th><th class='num'>for:</th><th class='num'>MTTD pipeline</th>"
        "<th class='num'>MTTR pipeline</th><th class='num'>conf.</th><th>outcome</th><th class='num'>T_detect</th></tr>"
    )
    rows = ""
    for r in CHAOS_RESULTS:
        rows += (
            "<tr>"
            f"<td>{r['exp']}</td><td class='num'>{r['for']}</td><td class='num'>{r['mttd']} s</td>"
            f"<td class='num'>{r['mttr']} s</td><td class='num'>{r['conf']}</td>"
            f"<td><span class='pill'>{r['outcome']}</span></td><td class='num'>{r['detect']} s</td>"
            "</tr>"
        )
    return f"<table>{head}{rows}</table>"


# ── Firma: el arco de seis etapas ──
ARC_STAGES = ["alerta", "informa", "razona", "dispone", "humano", "veredicto"]


def arcsig(lo: int = -1, hi: int = -2, all_on: bool = False, extra: str = "") -> str:
    """Raíl horizontal del arco; enciende el rango [lo, hi] (0-index) o todo."""
    cls = f"arcsig {extra}".strip()
    out = [f'<div class="{cls}" aria-hidden="true">']
    for i, label in enumerate(ARC_STAGES):
        on = all_on or (lo <= i <= hi)
        st = "st" + (" on" if on else "") + (" fin" if i == 5 else "")
        out.append(f'<div class="{st}"><i></i><span>{label}</span></div>')
    out.append("</div>")
    return "".join(out)


CSS_BASE = r"""
:root{
  --paper:#fcfcfa;          /* papel — casi blanco, apenas cálido */
  --ink:#101418;            /* tinta */
  --muted:#55606b;          /* secundario */
  --dim:#98a1aa;            /* terciario / captions */
  --hair:#dcdfe2;           /* hairlines */
  --hair2:#c3c9cf;          /* hairline marcada */
  --accent:#ff5a00;         /* naranja MasOrange — solo marcas pequeñas */
  --accent-deep:#b23e00;    /* naranja tinta: texto, eyebrows, énfasis */
  --accent-soft:rgba(255,90,0,.07);
  --ok:#2e7d5b;             /* verde apagado — SOLO veredicto cured/OK */
  --ok-soft:rgba(46,125,91,.10);
  --slate:#47525c;          /* notas de honestidad */
  --sans:'Plex',system-ui,sans-serif;
  --serif:'PlexSerif',Georgia,'Times New Roman',serif;
  --mono:'PlexMono','SF Mono',ui-monospace,monospace;
}

*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden;background:#e9eaec}
body{font-family:var(--sans);color:var(--ink);-webkit-font-smoothing:antialiased}

/* ── Escenario 1280×720 escalado a la ventana ── */
#stage{
  position:fixed;top:50%;left:50%;
  width:1280px;height:720px;
  transform:translate(-50%,-50%) scale(1);
  transform-origin:center center;
  overflow:hidden;
  background:var(--paper);
  box-shadow:0 0 0 1px var(--hair2), 0 24px 70px rgba(16,20,24,.14);
}

/* ── Slide ── */
.slide{
  position:absolute;inset:0;
  display:none;
  flex-direction:column;
  padding:60px 96px 76px;
}
.slide.active{display:flex;animation:fade .22s ease-out}
@keyframes fade{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){
  .slide.active{animation:none}
  .cover .arcsig .st i,.cover .arcsig .st span{animation:none!important}
  .arc::before,.arc-step{animation:none!important;opacity:1!important;transform:none!important}
}

/* pie de escenario: hairline + folio (parte del papel, se imprime) */
.slide::after{
  content:"";position:absolute;left:96px;right:96px;bottom:52px;
  height:1px;background:var(--hair);
}
.folio{
  position:absolute;left:96px;bottom:26px;
  font-family:var(--mono);font-size:11.5px;letter-spacing:.14em;
  color:var(--dim);text-transform:uppercase;
}
.cover .folio,.cover::after{display:none}

/* ── Cabecera de diapositiva ── */
.eyebrow{
  font-family:var(--mono);font-size:13px;font-weight:500;
  color:var(--accent-deep);letter-spacing:.2em;text-transform:uppercase;
  margin-bottom:16px;
}
.eyebrow b{color:var(--ink);font-weight:500}
.claim{
  font-family:var(--serif);
  font-size:40px;font-weight:600;line-height:1.14;letter-spacing:-.012em;
  max-width:1060px;margin-bottom:28px;
}
.claim em{font-style:italic;color:var(--accent-deep)}
.body{flex:1;display:flex;flex-direction:column;justify-content:center}

/* ── Texto ── */
p{font-size:18.5px;line-height:1.5}
.muted{color:var(--muted)}
strong{font-weight:600}
.mono{font-family:var(--mono)}
.sm{font-size:15.5px}
.xs{font-size:13.5px}
code{font-family:var(--mono);font-size:.88em;color:var(--accent-deep)}

/* ── Layout ── */
.split{display:grid;gap:52px;align-items:center;flex:1}
.split.s42{grid-template-columns:42fr 58fr}
.split.s55{grid-template-columns:55fr 45fr}

/* ── Filas definitorias (término — descripción), sin tarjetas ── */
.defs{margin-top:8px}
.def{
  display:grid;grid-template-columns:132px 1fr;gap:18px;align-items:baseline;
  padding:12px 0;border-top:1px solid var(--hair);
}
.def:last-child{border-bottom:1px solid var(--hair)}
.def dt{font-family:var(--mono);font-size:12.5px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--accent-deep)}
.def dd{font-size:16.5px;line-height:1.45;color:var(--ink)}
.def dd .mono{font-size:.9em;color:var(--muted)}
.def.ok dt{color:var(--ok)}

/* ── Tabla editorial (solo hairlines) ── */
table{border-collapse:collapse;width:100%;font-size:16px}
th{font-family:var(--mono);font-size:12px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);text-align:left;padding:0 16px 10px 0;border-bottom:1px solid var(--hair2)}
td{padding:11px 16px 11px 0;border-bottom:1px solid var(--hair);line-height:1.35}
td.num,th.num{font-family:var(--mono);font-size:14px;text-align:right;white-space:nowrap}
tr.hi td{color:var(--accent-deep);font-weight:600}

/* ── Píldoras ── */
.pill{display:inline-block;font-family:var(--mono);font-size:12px;border-radius:999px;padding:.22em .8em;border:1px solid rgba(178,62,0,.4);color:var(--accent-deep);background:var(--accent-soft)}
.pill.ok{border-color:rgba(46,125,91,.4);color:var(--ok);background:var(--ok-soft);font-weight:600}

/* ── Cadena de bloques (pipeline) ── */
.flow{display:flex;align-items:center;justify-content:center;gap:7px;flex-wrap:nowrap}
.box{text-align:center;background:#fff;border:1px solid var(--hair2);border-radius:6px;padding:11px 10px;font-weight:600;font-size:14px;white-space:nowrap}
.box small{display:block;font-family:var(--mono);font-weight:400;font-size:10px;color:var(--muted);margin-top:5px}
.box.hot{border-color:var(--accent);color:var(--accent-deep);background:var(--accent-soft)}
.arr{color:var(--hair2);font-size:17px;font-weight:700}

/* ── Franja de stats ── */
.statstrip{display:flex;margin-top:38px;border-top:1px solid var(--hair2)}
.statstrip>div{flex:1;padding:15px 18px 0;border-left:1px solid var(--hair)}
.statstrip>div:first-child{border-left:none;padding-left:0}
.statstrip b{display:block;font-family:var(--mono);font-size:26px;font-weight:600;line-height:1.1;margin-bottom:4px}
.statstrip b.acc{color:var(--accent-deep)}
.statstrip b.okc{color:var(--ok)}
.statstrip span{font-family:var(--mono);font-size:10.5px;color:var(--dim);letter-spacing:.08em;text-transform:uppercase}

/* ── Encabezado menor ── */
.h3{font-family:var(--mono);font-size:12.5px;font-weight:600;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin-bottom:4px}

/* ── Par de cifras comparadas ── */
.rate{display:flex;align-items:baseline;gap:20px;margin-top:10px}
.rate .from{font-family:var(--mono);font-size:30px;color:var(--dim)}
.rate .rarr{font-size:34px;color:var(--muted);font-weight:400}
.rate .to{font-family:var(--serif);font-size:88px;font-weight:700;letter-spacing:-.02em;color:var(--accent-deep);line-height:1}
.rate .to small{font-size:36px;font-weight:600;margin-left:4px}

/* ── Nota de honestidad (grafito, discreta) ── */
.honest{
  font-family:var(--mono);font-size:12.5px;line-height:1.55;color:var(--slate);
  background:rgba(85,96,107,.06);border-left:3px solid #8a97a3;
  padding:11px 15px;border-radius:0 6px 6px 0;margin-top:24px;
}
.honest::before{content:"Nota · ";color:var(--muted);font-weight:600}
.honest b{color:var(--ink);font-weight:600}

/* ── Figuras (capturas reales) ── */
.figframe{
  background:#fff;border:1px solid var(--hair);border-radius:6px;
  padding:10px;box-shadow:0 2px 12px rgba(16,20,24,.05);
  display:inline-block;line-height:0;
}
.figframe img{display:block;border-radius:2px;max-width:100%;max-height:330px;height:auto}
.figframe.wide{display:block}
.figframe.wide img{width:100%;max-height:none}
.defpair{display:grid;grid-template-columns:1fr 1fr;column-gap:52px;align-items:start}
.figcap{font-family:var(--mono);font-size:12.5px;color:var(--muted);margin-top:10px;line-height:1.5}
.figcap b{color:var(--accent-deep);font-weight:600}
.placeholder{border:2px dashed var(--accent);border-radius:8px;padding:2em;text-align:center;color:var(--muted);background:#fff}

/* ── Chips de métricas ── */
.chips{display:flex;gap:.5em;flex-wrap:wrap;margin-top:22px}
.chip{font-family:var(--mono);font-size:12.5px;border:1px solid var(--hair2);border-radius:999px;background:#fff;color:var(--muted);padding:.3em .8em}

/* ── Firma: raíl del arco de seis etapas ── */
.arcsig{position:relative;display:flex;justify-content:space-between;max-width:780px;margin-top:42px}
.arcsig::before{content:"";position:absolute;left:8px;right:8px;top:7px;height:2px;background:var(--hair)}
.arcsig .st{position:relative;display:flex;flex-direction:column;align-items:center;gap:9px;z-index:1}
.arcsig .st i{width:16px;height:16px;border-radius:50%;background:var(--paper);border:2px solid var(--hair2);font-style:normal}
.arcsig .st.on i{border-color:var(--accent);background:var(--accent)}
.arcsig .st.on.fin i{border-color:var(--ok);background:var(--ok);box-shadow:0 0 0 5px var(--ok-soft)}
.arcsig .st span{font-family:var(--mono);font-size:11.5px;color:var(--dim);letter-spacing:.08em;text-transform:uppercase}
.arcsig .st.on span{color:var(--accent-deep)}
.arcsig .st.on.fin span{color:var(--ok)}
.arcsig.mini{max-width:640px;margin:26px 0 0}
.arcsig.mini .st i{width:12px;height:12px}
.arcsig.mini::before{top:5px}
.divider .arcsig{margin:40px auto 0;width:640px}
/* portada: encendido secuencial (una vez) */
.cover .arcsig .st i{animation:nodein .45s ease-out both}
.cover .arcsig .st span{animation:nodein .45s ease-out both}
.cover .arcsig .st:nth-child(1) i,.cover .arcsig .st:nth-child(1) span{animation-delay:.20s}
.cover .arcsig .st:nth-child(2) i,.cover .arcsig .st:nth-child(2) span{animation-delay:.45s}
.cover .arcsig .st:nth-child(3) i,.cover .arcsig .st:nth-child(3) span{animation-delay:.70s}
.cover .arcsig .st:nth-child(4) i,.cover .arcsig .st:nth-child(4) span{animation-delay:.95s}
.cover .arcsig .st:nth-child(5) i,.cover .arcsig .st:nth-child(5) span{animation-delay:1.20s}
.cover .arcsig .st:nth-child(6) i,.cover .arcsig .st:nth-child(6) span{animation-delay:1.45s}
@keyframes nodein{from{opacity:0;transform:scale(.4)}to{opacity:1;transform:none}}

/* ── Replay del arco (slide demo) — animación CSS-only ── */
.arc-tag{display:inline-block;align-self:flex-start;font-family:var(--mono);color:var(--accent-deep);font-size:12.5px;font-weight:600;letter-spacing:.03em;border:1px solid rgba(178,62,0,.35);border-radius:999px;padding:.32em .95em;margin-bottom:16px;background:var(--accent-soft)}
.arc{position:relative;display:flex;flex-direction:column;gap:10px;max-width:1060px;padding-left:2px}
.arc::before{content:"";position:absolute;left:10px;top:12px;bottom:12px;width:2px;background:var(--hair2);transform:scaleY(0);transform-origin:top;animation:arcrail 3.4s linear both}
@keyframes arcrail{to{transform:scaleY(1)}}
.arc-step{display:flex;align-items:flex-start;gap:16px;opacity:0;transform:translateX(-10px);animation:arcin .5s ease-out both}
.arc-step:nth-child(1){animation-delay:.1s}
.arc-step:nth-child(2){animation-delay:.7s}
.arc-step:nth-child(3){animation-delay:1.3s}
.arc-step:nth-child(4){animation-delay:1.9s}
.arc-step:nth-child(5){animation-delay:2.5s}
.arc-step:nth-child(6){animation-delay:3.1s}
@keyframes arcin{to{opacity:1;transform:none}}
.arc-dot{flex:0 0 auto;width:22px;height:22px;border-radius:50%;background:#fff;border:2px solid var(--accent);margin-top:.1em;z-index:1}
.s-engine .arc-dot{border-color:var(--ink)}
.s-cured .arc-dot{border-color:var(--ok);box-shadow:0 0 0 5px var(--ok-soft)}
.arc-body b{color:var(--ink);font-size:16px;display:block;margin-bottom:.05em}
.arc-body span{color:var(--muted);font-size:13.5px;line-height:1.38}

/* ── Portada ── */
.cover{justify-content:center;padding:70px 96px}
.cover .eyebrow{margin-bottom:24px}
.cover h1{font-family:var(--serif);font-size:76px;font-weight:700;line-height:1.02;letter-spacing:-.015em;margin-bottom:14px}
.cover .thesis{font-family:var(--serif);font-style:italic;font-size:26px;color:var(--accent-deep);margin-bottom:18px}
.cover .sub{font-size:20px;line-height:1.5;color:var(--muted);max-width:880px}
.cover .meta{display:flex;gap:44px;margin-top:40px;padding-top:22px;border-top:1px solid var(--hair)}
.cover .meta div{font-size:14.5px;color:var(--muted);line-height:1.45;max-width:420px}
.cover .meta b{display:block;font-weight:600;color:var(--ink);margin-bottom:2px}
.cover .meta .mono{font-family:var(--mono);font-size:12.5px}
.timechip{
  position:absolute;right:96px;bottom:64px;
  font-family:var(--mono);font-size:12.5px;color:var(--accent-deep);
  border:1px solid rgba(178,62,0,.35);background:var(--accent-soft);
  border-radius:999px;padding:7px 16px;white-space:nowrap;
}
.timechip b{font-weight:600}

/* ── Divider de sección ── */
.divider{justify-content:center;text-align:center;align-items:center}
.divider .part{font-family:var(--mono);font-size:13.5px;font-weight:500;letter-spacing:.22em;text-transform:uppercase;color:var(--accent-deep);margin-bottom:22px}
.divider h2{font-family:var(--serif);font-size:56px;font-weight:700;letter-spacing:-.012em;line-height:1.08;max-width:940px}
.divider .sub{font-size:19px;color:var(--muted);margin-top:18px;max-width:780px;line-height:1.5}

/* ── Capa RESPALDO ── */
.slide.opt .eyebrow{color:var(--slate)}
.optflag{
  position:absolute;top:28px;right:96px;z-index:5;
  font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.16em;text-transform:uppercase;
  color:var(--slate);background:rgba(85,96,107,.07);border:1px solid #8a97a3;
  border-radius:999px;padding:5px 13px;
}

/* ── HUD (fuera del papel; no se imprime) ── */
#hud{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);width:340px;z-index:50;transition:opacity .25s}
#hud .track{position:relative;height:2px;background:#cdd2d6;border-radius:2px}
#hud .fill{position:absolute;left:0;top:0;height:2px;background:var(--accent);width:0;transition:width .3s;border-radius:2px}
#hud .dots i{position:absolute;top:-3px;width:8px;height:8px;border-radius:50%;background:#cdd2d6;transform:translateX(-50%);transition:background .3s;font-style:normal}
#hud .dots i.on{background:var(--accent)}
#hud .dots i.on.fin{background:var(--ok)}
#hud.bk{opacity:.3}
#counter{position:fixed;right:22px;bottom:14px;font-family:var(--mono);font-size:13px;color:#7b848d;z-index:50;letter-spacing:.05em}
#brand{position:fixed;left:22px;bottom:13px;font-family:var(--mono);font-size:12px;color:#7b848d;z-index:50;letter-spacing:.1em}
#nav{position:fixed;right:20px;bottom:36px;display:flex;gap:7px;z-index:50}
#nav button{width:30px;height:30px;border-radius:7px;border:1px solid var(--hair2);background:#fff;color:var(--muted);font-size:15px;cursor:pointer;transition:.15s}
#nav button:hover,#nav button:focus-visible{border-color:var(--accent);color:var(--accent-deep);outline:none}
#help{position:fixed;left:22px;top:16px;font-family:var(--mono);font-size:11.5px;color:#9aa2ab;z-index:50}

/* ── Panel de notas (tecla N) ── */
#notes{
  position:fixed;left:0;right:0;bottom:0;max-height:38vh;overflow:auto;
  background:rgba(255,255,255,.98);border-top:2px solid var(--accent);
  box-shadow:0 -8px 24px rgba(16,20,24,.10);
  padding:18px 26px;z-index:60;transform:translateY(101%);transition:transform .25s ease;
  font-size:15px;line-height:1.55;color:var(--ink);
}
#notes.open{transform:translateY(0)}
#notes .ntag{font-family:var(--mono);font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--accent-deep);margin-bottom:8px}
#notes b{color:var(--accent-deep)}

/* ── Impresión / export a PDF: 1 diapositiva por página ── */
@media print{
  @page{size:1280px 720px;margin:0}
  html,body{height:auto;overflow:visible;background:#fff;-webkit-print-color-adjust:exact;print-color-adjust:exact}
  #stage{position:static;left:0;top:0;transform:none;width:1280px;height:auto;box-shadow:none;overflow:visible}
  .slide{
    display:flex!important;position:relative;inset:auto;
    width:1280px;height:720px;overflow:hidden;
    page-break-after:always;break-after:page;animation:none;
    -webkit-print-color-adjust:exact;print-color-adjust:exact;
  }
  .slide:last-child{page-break-after:auto;break-after:auto}
  .cover .arcsig .st i,.cover .arcsig .st span{animation:none!important}
  .arc::before,.arc-step{animation:none!important;opacity:1!important;transform:none!important}
  #hud,#counter,#brand,#nav,#help,#notes{display:none!important}
  .notes{display:none!important}
}
"""

JS = r"""
(function(){
  const stage=document.getElementById('stage');
  const slides=[...document.querySelectorAll('.slide')];
  const counter=document.getElementById('counter');
  const hud=document.getElementById('hud');
  const hudFill=hud.querySelector('.fill');
  const notesPanel=document.getElementById('notes');
  const notesBody=document.getElementById('notes-body');
  let idx=0;

  // HUD: raíl con los 6 hitos del arco
  const dotsWrap=hud.querySelector('.dots');
  for(let i=0;i<6;i++){
    const d=document.createElement('i');
    d.style.left=(i/5*100)+'%';
    if(i===5)d.className='fin';
    dotsWrap.appendChild(d);
  }
  const hudDots=[...dotsWrap.children];

  // Índices del NÚCLEO (las de respaldo llevan .opt y cuentan aparte)
  const coreIdx=[];
  slides.forEach((s,k)=>{ if(!s.classList.contains('opt')) coreIdx.push(k); });
  const coreTotal=coreIdx.length;
  const optTotal=slides.filter(s=>s.classList.contains('opt')).length;

  // Escala 1280×720 a la ventana (letterbox)
  function fit(){
    const s=Math.min(innerWidth/1280, innerHeight/720);
    stage.style.transform=`translate(-50%,-50%) scale(${s})`;
  }
  addEventListener('resize',fit); fit();

  function updateNotes(){
    const n=slides[idx].querySelector('.notes');
    notesBody.innerHTML = n ? n.innerHTML : '<span style="color:#98a1aa">— sin notas —</span>';
  }
  function updateHud(){
    const cur=slides[idx];
    if(cur.classList.contains('opt')){
      const optPos=slides.slice(0,idx+1).filter(s=>s.classList.contains('opt')).length;
      counter.textContent='R'+optPos+' / '+optTotal+' · respaldo';
      hud.classList.add('bk');
    }else{
      const corePos=coreIdx.indexOf(idx)+1;
      counter.textContent=String(corePos).padStart(2,'0')+' / '+String(coreTotal).padStart(2,'0');
      hud.classList.remove('bk');
      const pct=coreTotal>1?(corePos-1)/(coreTotal-1):0;
      hudFill.style.width=(pct*100)+'%';
      hudDots.forEach((d,i)=>{
        const isFin=d.classList.contains('fin');
        d.className=(pct>=i/5-1e-6?'on':'')+(isFin?' fin':'');
      });
    }
  }
  function show(i){
    idx=Math.max(0,Math.min(slides.length-1,i));
    slides.forEach((s,k)=>s.classList.toggle('active',k===idx));
    updateHud();
    try{history.replaceState(null,'','#'+(idx+1));}catch(e){}
    updateNotes();
  }
  function toggleFs(){ if(!document.fullscreenElement){document.documentElement.requestFullscreen&&document.documentElement.requestFullscreen();}else{document.exitFullscreen&&document.exitFullscreen();} }

  addEventListener('keydown',e=>{
    if(['ArrowRight','ArrowDown','PageDown',' '].includes(e.key)){show(idx+1);e.preventDefault();}
    else if(['ArrowLeft','ArrowUp','PageUp'].includes(e.key)){show(idx-1);e.preventDefault();}
    else if(e.key==='Home'){show(0);}
    else if(e.key==='End'){show(slides.length-1);}
    else if(e.key==='n'||e.key==='N'){notesPanel.classList.toggle('open');}
    else if(e.key==='f'||e.key==='F'){toggleFs();}
  });
  document.getElementById('next').onclick=()=>show(idx+1);
  document.getElementById('prev').onclick=()=>show(idx-1);
  stage.addEventListener('click',e=>{ if(e.target.closest('button,a'))return; show(e.clientX < innerWidth*.3 ? idx-1 : idx+1); });

  const start=parseInt((location.hash||'').slice(1));
  show(Number.isFinite(start)&&start>0?start-1:0);
})();
"""


def slides() -> list[str]:
    return [
        # ══════════ 01 · PORTADA ══════════
        f"""<section class="slide active cover">
          <div class="eyebrow">Prototipo AIOps · Prácticas · <b>{SUBJECT}</b></div>
          <h1>{TITLE}</h1>
          <p class="thesis">El cluster informa, el modelo razona, el motor dispone.</p>
          <p class="sub">{SUBTITLE}: un agente que detecta la alerta, reúne el contexto real del cluster, propone el arreglo y solo actúa cuando es seguro. Diseñado, desarrollado y validado en un cluster GKE real.</p>
          {arcsig(all_on=True)}
          <div class="meta">
            <div><b>{AUTHOR}</b>Prácticas de ingeniería AIOps</div>
            <div class="mono" style="align-self:center;color:var(--dim)">GKE · Prometheus · Ollama · ChromaDB · Redis · Mattermost · FastAPI</div>
          </div>
          <div class="timechip"><b>~15 min</b> + preguntas · demo integrada</div>
          <div class="notes" hidden><b>0:00 · ~30 s.</b> Apertura sin humos: es un prototipo de prácticas, una prueba de concepto para comprobar si la idea es viable. La frase ancla es la tesis: «el cluster informa, el modelo razona, el motor dispone». El arco de seis etapas de la portada es el hilo de toda la presentación y se verá en movimiento en la demo. Presupuesto: ~15 min de diapositivas con la demo integrada; preguntas aparte (hay capa de respaldo tras el cierre).</div>
        </section>""",

        # ══════════ 02 · ESCENARIO ══════════
        """<section class="slide">
          <div class="eyebrow">Escenario</div>
          <div class="claim">Cuando salta una alerta, el contexto está disperso <em>y se junta a mano</em></div>
          <div class="body">
            <dl class="defs">
              <div class="def"><dt>Hoy</dt><dd>ante una alerta de Kubernetes, el operador reúne a mano tres piezas: qué pod falla, qué runbook aplica y qué comando es seguro ejecutar.</dd></div>
              <div class="def"><dt>La pregunta</dt><dd>¿puede un LLM reunir ese contexto y proponer el arreglo, <strong>sin convertirse en una caja negra con permisos sobre el cluster</strong>?</dd></div>
              <div class="def"><dt>El entorno</dt><dd>datos dentro del cluster, sin API externa y con recursos limitados. Se buscaba comprobar la viabilidad del enfoque <strong>en esas condiciones reales</strong>.</dd></div>
            </dl>
          </div>
          <div class="folio">Escenario</div>
          <div class="notes" hidden><b>0:30 · ~50 s.</b> Enmarcar el problema sin vender dolor de negocio: el diagnóstico de una alerta exige juntar contexto disperso, y ese trabajo es repetitivo. La pregunta que se quiso responder: ¿puede un LLM ayudar sin ser una caja negra con permisos? Las restricciones del entorno (datos in-cluster, sin API externa) son parte del enunciado, no un accidente.</div>
        </section>""",

        # ══════════ 03 · PROPUESTA ══════════
        """<section class="slide">
          <div class="eyebrow">Propuesta</div>
          <div class="claim">Unir alerta, contexto y decisión segura <em>en el mismo flujo</em></div>
          <div class="body">
            <dl class="defs">
              <div class="def"><dt>La idea</dt><dd>el sistema trae el runbook, propone el arreglo y lo ejecuta <strong>solo cuando es seguro</strong>; si no lo es, escala a una persona con el comando ya preparado.</dd></div>
              <div class="def"><dt>La clave</dt><dd>no es el modelo: un LLM con <code>kubectl</code> libre no es aceptable. La clave es la <strong>capa de seguridad determinista</strong> que lo rodea y decide qué se ejecuta.</dd></div>
              <div class="def"><dt>El listón</dt><dd>cada afirmación de esta presentación mapea a algo que el sistema <strong>hace</strong> hoy en un cluster real, y lo que no está medido se dice.</dd></div>
            </dl>
          </div>
          <div class="folio">Escenario</div>
          <div class="notes" hidden><b>1:20 · ~45 s.</b> La propuesta en una frase: alerta + contexto + decisión segura en el mismo flujo. Adelantar la idea que ordena todo el diseño: el LLM nunca ejecuta; propone, y un motor determinista decide. El tercer punto fija el tono de la sesión: prototipo honesto, con medidas y con límites declarados.</div>
        </section>""",

        # ══════════ DIV · PARTE 01 ══════════
        f"""<section class="slide divider">
          <div class="part">Parte 01 / 03</div>
          <h2>Qué se ha diseñado y desarrollado</h2>
          <p class="sub">El pipeline de extremo a extremo, la arquitectura en tres capas y las reglas que gobiernan cuándo el sistema actúa solo.</p>
          {arcsig(0, 3)}
          <div class="folio">{TITLE}</div>
          <div class="notes" hidden><b>2:05 · ~5 s.</b> «Primero, cómo está construido.» El raíl enciende las cuatro primeras etapas del arco: de la alerta a la decisión del motor.</div>
        </section>""",

        # ══════════ 04 · PIPELINE ══════════
        f"""<section class="slide">
          <div class="eyebrow">Diseño · el pipeline</div>
          <div class="claim">De la alerta de Prometheus al patch aplicado, <em>con la seguridad como principio</em></div>
          <div class="body">
            <div class="flow">
              <div class="box">Prometheus<small>{STATS['alert_rules']} reglas K8s</small></div><span class="arr">→</span>
              <div class="box">Alertmanager<small>routing</small></div><span class="arr">→</span>
              <div class="box">Cola Redis<small>Streams · fail-closed</small></div><span class="arr">→</span>
              <div class="box hot">Grounding<small>snapshot del cluster</small></div><span class="arr">→</span>
              <div class="box hot">RAG + LLM<small>ChromaDB + Ollama</small></div><span class="arr">→</span>
              <div class="box hot">Motor<small>{STATS['engine_rules']} reglas</small></div><span class="arr">→</span>
              <div class="box">Mattermost / K8s<small>aprobar o actuar</small></div>
            </div>
            <p class="mono xs" style="color:var(--dim);text-align:center;margin-top:18px">todo in-cluster: los datos no salen · sin coste por token · CI en cada push + build con los tests como gate</p>
            <div class="statstrip">
              <div><b>{STATS['alert_rules']}</b><span>reglas de alerta</span></div>
              <div><b>{STATS['engine_rules']}</b><span>reglas del motor</span></div>
              <div><b>{STATS['runbooks']}</b><span>runbooks RAG</span></div>
              <div><b class="acc">{STATS['tests']}</b><span>tests en verde</span></div>
              <div><b>{STATS['chaos_experiments']}</b><span>experimentos chaos</span></div>
            </div>
          </div>
          <div class="folio">Diseño del sistema</div>
          <div class="notes" hidden><b>2:10 · ~60 s.</b> Recorrer el flujo de izquierda a derecha. Las tres cajas encendidas son las tres capas de la tesis y se abren en las siguientes diapositivas. Dos detalles de ingeniería que conviene decir en alto: la cola es fail-closed (si Redis cae, Alertmanager reintenta: no se pierde la alerta) y todo corre dentro del cluster: los datos no salen y no hay coste por token.</div>
        </section>""",

        # ══════════ 05 · TRES CAPAS ══════════
        f"""<section class="slide">
          <div class="eyebrow">Diseño · la tesis</div>
          <div class="claim">El cluster informa, el modelo razona, <em>el motor dispone</em></div>
          <div class="body">
            <dl class="defs">
              <div class="def"><dt>Informa</dt><dd>una etapa determinista consulta <code>kubectl</code> <strong>antes</strong> del LLM y construye un snapshot real: límites, fase, reinicios, identidad del workload. Todo son hechos leídos del cluster.</dd></div>
              <div class="def"><dt>Razona</dt><dd>el LLM recibe esos hechos más los runbooks del RAG y produce un diagnóstico. Queda reducido a <em>qué campo</em> y <em>en qué dirección</em>; los valores no los pone el modelo.</dd></div>
              <div class="def"><dt>Dispone</dt><dd>un motor de {STATS['engine_rules']} reglas re-fuente la decisión: sella la identidad con la verdad del cluster, acota el cambio y decide auto / escalar / sugerir. <strong>El LLM nunca ejecuta.</strong></dd></div>
            </dl>
            {arcsig(1, 3, extra="mini")}
            <p class="sm muted" style="margin-top:18px">Cada capa desconfía de la anterior. Esa desconfianza es el diseño.</p>
          </div>
          <div class="folio">Diseño del sistema</div>
          <div class="notes" hidden><b>3:10 · ~50 s.</b> Esta es la diapositiva que ordena todo lo demás. Tres capas, cada una desconfiando de la anterior: los hechos vienen del cluster, el razonamiento del modelo, y la decisión de un motor determinista que vuelve a comprobar contra el cluster. Si solo se recuerda una cosa de la sesión, que sea esta frase.</div>
        </section>""",

        # ══════════ 06 · GROUNDING ══════════
        f"""<section class="slide">
          <div class="eyebrow">Diseño · el cluster informa</div>
          <div class="claim">Antes de que el modelo hable, <em>se consulta la verdad del cluster</em></div>
          <div class="body">
            {img("grounded", "Escalación en Mattermost con confianza fundada en el cluster", "figframe wide")}
            <div class="figcap">escalación real en Mattermost: confianza <b>100 % fundada en el cluster</b>, frente al 65 % que declaraba el modelo</div>
            <div class="defpair" style="margin-top:22px">
              <dl class="defs">
                <div class="def"><dt>Snapshot</dt><dd>container, límites, fase, reinicios y último motivo de terminación, leídos del cluster antes del diagnóstico.</dd></div>
                <div class="def"><dt>Identidad</dt><dd>pod → ReplicaSet → Deployment vía <code>ownerReferences</code>: el target se <strong>confirma</strong> contra el cluster.</dd></div>
              </dl>
              <dl class="defs">
                <div class="def"><dt>Contexto</dt><dd>logs recientes (del contenedor caído si lo hay) y eventos del pod, en bloques separados de los hechos sellados.</dd></div>
                <div class="def"><dt>Confianza</dt><dd>derivada de señales del cluster; la del modelo se conserva aparte y <strong>no gobierna</strong> la decisión.</dd></div>
              </dl>
            </div>
            <p class="sm muted" style="margin-top:14px">Fail-soft: si el <code>kubectl</code> falla, el pipeline sigue sin snapshot. Perder contexto es mejor que perder la alerta.</p>
          </div>
          <div class="folio">Diseño del sistema</div>
          <div class="notes" hidden><b>4:00 · ~60 s.</b> La pieza clave del diseño. El valor actual del límite y la identidad del workload vienen del snapshot de kubectl y no del modelo, lo que elimina la clase de fallo «el LLM se inventa el target o el valor». Señalar la captura: la confianza que se muestra está fundada en señales del cluster (motivo de terminación + reinicios); la que declaraba el modelo se conserva aparte. El snapshot incluye además logs y eventos recientes, separados de los hechos sellados para no diluir su autoridad.</div>
        </section>""",

        # ══════════ 07 · EL MOTOR DISPONE ══════════
        f"""<section class="slide">
          <div class="eyebrow">Diseño · el motor dispone</div>
          <div class="claim">El criterio del automático no es la alerta: <em>es el arreglo</em></div>
          <div class="body">
            <div class="split s55" style="align-items:start">
              <div>
                <div class="h3">Tres condiciones, a la vez</div>
                <dl class="defs">
                  <div class="def"><dt>Confirmable</dt><dd>el target se sella desde el snapshot del cluster; sin confirmación no hay acción y se escala.</dd></div>
                  <div class="def"><dt>Acotado</dt><dd>cambios de hasta <strong>{STATS['cap']}</strong> en automático, reversibles por snapshot; un salto mayor va a una persona <strong>con las dos opciones a la vista</strong>: la ×2 del motor y el valor del modelo, cada una en su propio botón firmado.</dd></div>
                  <div class="def"><dt>Verificable</dt><dd>el resultado se comprueba después (<code>cured</code> / <code>rolled_back</code>); sin veredicto posible, no hay automático.</dd></div>
                </dl>
              </div>
              <div>
                <div class="h3">Lo que frena</div>
                <dl class="defs">
                  <div class="def"><dt>Blacklist</dt><dd>comandos destructivos o desconocidos, bloqueados por patrón.</dd></div>
                  <div class="def"><dt>Escala</dt><dd>target fantasma o tipo de workload no soportado → a una persona.</dd></div>
                  <div class="def"><dt>Cooldown</dt><dd>un patch por workload y ventana: corta la tormenta de patches.</dd></div>
                  <div class="def"><dt>Permisos</dt><dd>pre-flight <code>kubectl auth can-i</code>: lo que RBAC deniega se ofrece como sugerencia, nunca como botón que fallaría.</dd></div>
                </dl>
              </div>
            </div>
            <div class="honest">Hoy solo <b>subir el límite de memoria de un Deployment</b> cumple las tres condiciones; todo lo demás escala o sugiere. Es poco a propósito.</div>
          </div>
          <div class="folio">Diseño del sistema</div>
          <div class="notes" hidden><b>5:00 · ~70 s.</b> La diapositiva que responde «¿por qué no es peligroso?». El criterio de lo autónomo son tres condiciones simultáneas: target confirmable desde el cluster, acción acotada y reversible, y resultado verificable. Hoy solo el caso memoria cumple las tres. Esa estrechez es una decisión: antes de ampliar el catálogo hay que ampliar la medición. El {STATS['cap']} no es un techo absoluto: es la frontera de lo que el sistema hace solo; más allá, la escalación ofrece los dos valores en botones separados (la ×2 determinista del motor, recomendada, y la propuesta del modelo) y la persona arbitra. Se verá en la demo.</div>
        </section>""",

        # ══════════ 08 · APRENDIZAJE ══════════
        """<section class="slide">
          <div class="eyebrow">Diseño · el bucle de aprendizaje</div>
          <div class="claim">Cada incidente termina con un veredicto, <em>y el veredicto vuelve a la memoria</em></div>
          <div class="body">
            <div class="defpair">
              <dl class="defs">
                <div class="def ok"><dt>Veredicto</dt><dd>tras remediar se evalúa el resultado contra la salud real del pod: <code>cured</code> o <code>rolled_back</code>, con rollback automático.</dd></div>
                <div class="def"><dt>Memoria</dt><dd>el veredicto re-marca el incidente en ChromaDB; los fracasos se conservan como <strong>conocimiento negativo recuperable</strong>.</dd></div>
              </dl>
              <dl class="defs">
                <div class="def"><dt>Paridad</dt><dd>aprobar en Mattermost alimenta el mismo bucle que el camino automático.</dd></div>
                <div class="def"><dt>Observación</dt><dd>las alertas que se resuelven sin veredicto se correlan por huella → <code>resolved_observed</code> + tiempo de resolución. Señal débil: nunca pisa a la fuerte.</dd></div>
              </dl>
            </div>
            <div style="margin-top:24px">{IMG_CURED}</div>
            <div class="figcap">veredicto <b>cured</b> notificado en Mattermost tras verificar la salud real del pod</div>
          </div>
          <div class="folio">Diseño del sistema</div>
          <div class="notes" hidden><b>6:10 · ~60 s.</b> El sistema no solo actúa: comprueba si lo que hizo funcionó y guarda el resultado. El veredicto re-marca el incidente en la memoria RAG, incluidos los fracasos, que valen tanto como los éxitos. La paridad humano/auto importa: aprobar un botón alimenta el mismo aprendizaje. Y para las alertas donde no hay veredicto verificado, el bucle observacional da un resultado más débil (resolved_observed) que nunca sobreescribe al fuerte. Si preguntan cuánto aporta esta memoria: está medido, dos diapositivas más adelante.</div>
        </section>""".replace("{IMG_CURED}", img("cured", "Veredicto cured notificado en Mattermost", "figframe wide")),

        # ══════════ DIV · PARTE 02 ══════════
        f"""<section class="slide divider">
          <div class="part">Parte 02 / 03</div>
          <h2>Evidencia y medición</h2>
          <p class="sub">El comportamiento no se supone: se mide. Un run real de extremo a extremo, el retrieval evaluado, la memoria cuantificada y cuatro experimentos de chaos.</p>
          {arcsig(4, 5)}
          <div class="folio">{TITLE}</div>
          <div class="notes" hidden><b>7:20 · ~5 s.</b> «Construido el sistema, la pregunta es: ¿funciona? Todo lo que sigue está medido.» El raíl enciende las dos últimas etapas, el humano y el veredicto, que son las que la evidencia pone a prueba.</div>
        </section>""",

        # ══════════ 09 · LA DEMO (REPLAY) ══════════
        """<section class="slide">
          <div class="eyebrow">Evidencia · el arco completo</div>
          <div class="claim" style="margin-bottom:14px">Un run real, de la alerta al veredicto <em>cured</em></div>
          <div class="arc-tag">&#9654; Replay de un run real en el cluster GKE &middot; valores capturados, no simulados</div>
          <div class="arc">
            <div class="arc-step"><span class="arc-dot"></span><div class="arc-body"><b>1 &middot; Salta la alerta</b><span>Pod <code>OOMKilled</code> &rarr; Prometheus &rarr; Alertmanager &rarr; webhook. Detecci&oacute;n del pipeline: <strong>~5 s</strong>.</span></div></div>
            <div class="arc-step"><span class="arc-dot"></span><div class="arc-body"><b>2 &middot; El cluster informa</b><span>Snapshot antes del LLM: <code>memory=32Mi</code>, <code>restarts=6</code>, identidad del workload por <code>ownerReferences</code>.</span></div></div>
            <div class="arc-step"><span class="arc-dot"></span><div class="arc-body"><b>3 &middot; El modelo razona</b><span>Diagn&oacute;stico JSON sobre los hechos + runbook: subir <code>memory</code>. El modelo propone <code>512Mi</code>.</span></div></div>
            <div class="arc-step s-engine"><span class="arc-dot"></span><div class="arc-body"><b>4 &middot; El motor dispone</b><span>Sella <code>current=32Mi</code> del cluster. El sistema solo act&uacute;a por su cuenta hasta <strong>&times;2</strong>; <code>512Mi</code> = <strong>16&times;</strong> &rarr; ni lo aplica solo ni lo recorta a la callada &rarr; <span class="pill">escala a una persona</span>.</span></div></div>
            <div class="arc-step"><span class="arc-dot"></span><div class="arc-body"><b>5 &middot; Human-in-the-loop</b><span>Una persona ve el pod muriendo con unos irrisorios <code>32Mi</code> (donde &times;2 volver&iacute;a a fallar) y aprueba <code>512Mi</code> con el comando ya preparado y firma HMAC-SHA256 &rarr; patch.</span></div></div>
            <div class="arc-step s-cured"><span class="arc-dot"></span><div class="arc-body"><b>6 &middot; Veredicto que cierra el bucle</b><span>+300 s &rarr; el pod queda sano a <code>512Mi</code> &rarr; <span class="pill ok">cured</span> &middot; el incidente queda re-marcado en la memoria RAG.</span></div></div>
          </div>
          <p class="mono xs" style="color:var(--dim);margin-top:14px">en directo, el modelo 1.5b en CPU tarda ~150-270 s por diagn&oacute;stico; el replay muestra el arco validado sin esa espera</p>
          <div class="folio">Evidencia y medici&oacute;n</div>
          <div class="notes" hidden><b>7:25 · ~2 min.</b> Esta es la demo. Es un replay de un run real en el cluster y no una simulaci&oacute;n; decirlo expl&iacute;citamente. Dejar correr la animaci&oacute;n y narrar las seis etapas al ritmo en que aparecen; para repetirla, retroceder una diapositiva y avanzar. Los dos momentos que importan: el motor sella 32Mi desde el cluster (no del modelo) y, ante un salto de 16&times;, ni lo aplica ni lo recorta en silencio: lo lleva a una persona con el comando preparado. El veredicto cured cierra el bucle y alimenta la memoria. Cada capa desconfi&oacute; de la anterior, y por eso el arco es defendible. Matiz si lo preguntan: desde ese run, la escalaci&oacute;n ofrece adem&aacute;s un segundo bot&oacute;n con la &times;2 determinista del motor junto al valor del modelo — la elecci&oacute;n del operador es hoy expl&iacute;cita, no impl&iacute;cita.</div>
        </section>""",

        # ══════════ 10 · RETRIEVAL Y SEGURIDAD ══════════
        f"""<section class="slide">
          <div class="eyebrow">Evidencia · retrieval y seguridad</div>
          <div class="claim">Con el runbook correcto delante, <em>el modelo no inventa</em></div>
          <div class="body">
            <div class="split s42">
              <div>
                <div class="h3">Runbook correcto a la primera</div>
                <div class="rate"><span class="from">{STATS['p1_before']}</span><span class="rarr">→</span><span class="to">{STATS['p1_after'].replace('%','')}<small>%</small></span></div>
                <p class="mono xs" style="color:var(--dim);margin-top:12px">precision@1 al filtrar el retrieval por la clase de error derivada de la alerta · p@3 {STATS['p3_before']}&rarr;{STATS['p3_after']}</p>
              </div>
              <dl class="defs">
                <div class="def"><dt>Seguridad</dt><dd>comandos seguros: <strong>100 % con RAG</strong> frente a 25 % en zero-shot. El zero-shot llegó a alucinar un <code>kubectl delete</code> y el motor lo bloqueó.</dd></div>
                <div class="def"><dt>Confianza</dt><dd>media <strong>{STATS['conf_rag']} con RAG</strong> vs {STATS['conf_zero']} zero-shot, sobre el mismo dataset.</dd></div>
                <div class="def"><dt>Dataset</dt><dd>N={STATS['eval_n']} alertas de 5 clases (OOM, CrashLoop, ImagePull, HighCPU, HighMemory), medido en el cluster.</dd></div>
              </dl>
            </div>
            <div class="honest">N={STATS['eval_n']} valida el <b>mecanismo</b>; para afirmar estadística fina hace falta ampliar el dataset a 30-50 alertas, y está en el plan.</div>
          </div>
          <div class="folio">Evidencia y medición</div>
          <div class="notes" hidden><b>9:25 · ~60 s.</b> Dos resultados. Primero: con el filtro por clase de error derivada del nombre de la alerta, el retrieval trae el runbook correcto a la primera el 100 % de las veces (antes, 73 %). Segundo, el que más importa: con el runbook delante, el 100 % de los comandos propuestos son seguros; sin RAG, solo el 25 %, con una alucinación de kubectl delete que el motor bloqueó. La lección: la palanca de seguridad fue servir mejor contexto antes que buscar un modelo más grande. Decir el tamaño del dataset sin que lo pregunten.</div>
        </section>""",

        # ══════════ 11 · ¿APORTA LA MEMORIA? ══════════
        f"""<section class="slide">
          <div class="eyebrow">Evidencia · la memoria, cuantificada</div>
          <div class="claim">¿Aporta algo que el sistema recuerde? <em>Dos preguntas, dos respuestas medidas</em></div>
          <div class="body">
            <div class="split s42" style="align-items:start">
              <div>
                <div class="h3">1 · ¿Encuentra casos pasados parecidos? Sí</div>
                <div class="rate"><span class="from">{STATS['r4_a_empty_p1']}</span><span class="rarr">→</span><span class="to" style="font-size:72px">{STATS['r4_a_seed_p1'].replace('%','')}<small>%</small></span></div>
                <p class="mono xs" style="color:var(--dim);margin-top:12px">acierta la clase del incidente: sin memoria &rarr; con memoria · con los dos m&aacute;s parecidos: 0 % &rarr; {STATS['r4_a_seed_p2']}</p>
                <p class="sm muted" style="margin-top:14px">Sin memoria no rescata nada; con ella, acierta la clase aproximadamente la mitad de las veces. La memoria <strong>emerge</strong>.</p>
              </div>
              <div>
                <div class="h3">2 · ¿Le hace caso el modelo? No, a este tamaño</div>
                <table style="margin-top:10px">
                  <tr><th>Lo que el modelo recuerda</th><th>Propone</th><th class="num">Conf.</th></tr>
                  <tr><td>nada</td><td>subir memoria</td><td class="num">0,80</td></tr>
                  <tr><td>&laquo;subir memoria <strong>no</strong> funcionó&raquo;</td><td>subir memoria</td><td class="num">0,80</td></tr>
                  <tr><td>&laquo;subir memoria <strong>sí</strong> funcionó&raquo;</td><td>subir memoria</td><td class="num">0,80</td></tr>
                </table>
                <p class="sm muted" style="margin-top:12px">Tres respuestas idénticas: el modelo de 1.5b <strong>ignora</strong> la etiqueta del resultado.</p>
              </div>
            </div>
            <div class="honest">Resultado <b>pre-registrado antes de medir</b>, incluido el «no», sobre un fixture sintético declarado. La seguridad no depende de que el modelo recuerde: <b>la impone el motor</b>.</div>
          </div>
          <div class="folio">Evidencia y medición</div>
          <div class="notes" hidden><b>10:25 · ~75 s.</b> Contarlo como dos preguntas encadenadas. Primera: ¿el sistema encuentra casos pasados parecidos? Sí: de 0 % a 46,7 %. Sin memoria no rescata nada; con ella acierta la clase la mitad de las veces. Segunda: ¿el modelo cambia su diagnóstico según lo que recuerda? No, a este tamaño: se le presentó el mismo aviso OOM tres veces, variando solo el recuerdo (nada / «no funcionó» / «sí funcionó»), y propuso lo mismo las tres. El remate: por eso el diseño es correcto, porque la seguridad la pone el motor y no la memoria del modelo. Y el resultado se pre-registró antes de medir, incluido que saliera «no»: un null honesto vale más que un positivo inventado.</div>
        </section>""",

        # ══════════ 12 · CHAOS ══════════
        f"""<section class="slide">
          <div class="eyebrow">Evidencia · chaos engineering</div>
          <div class="claim">Cuatro fallos inyectados: <em>detección en segundos, decisión con gates</em></div>
          <div class="body">
            {chaos_table()}
            <p class="sm muted" style="margin-top:20px">Los cuatro experimentos escalan o sugieren <strong>por diseño</strong> (riesgo alto o target no resoluble). El arco completo hasta <span class="pill ok">cured</span> es la medición del replay: escalación &rarr; aprobación &rarr; patch &rarr; verificación.</p>
            <div class="honest">El MTTR lo domina el <b>hardware</b>: el modelo 1.5b en CPU tarda ~150-270 s por diagnóstico. El pipeline detecta en ~5-10 s; la espera restante es la inferencia.</div>
          </div>
          <div class="folio">Evidencia y medición</div>
          <div class="notes" hidden><b>11:40 · ~45 s.</b> Cuatro tipos de fallo inyectados en un namespace aislado del cluster. La detección del pipeline (alerta firing → webhook) es de segundos en los cuatro. El MTTR de ~200-250 s lo domina la inferencia del LLM en CPU, un techo de hardware; con GPU o un modelo servido mejor, baja. Que los cuatro escalen o sugieran no es un fallo: los gates de seguridad hacen su trabajo; el arco completo con patch y veredicto es el del replay.</div>
        </section>""",

        # ══════════ 13 · OBSERVABILIDAD ══════════
        """<section class="slide">
          <div class="eyebrow">Evidencia · observabilidad</div>
          <div class="claim">El sistema que vigila el cluster <em>también se vigila a sí mismo</em></div>
          <div class="body">
            <div class="split" style="grid-template-columns:1fr 1fr;gap:24px">
              <div>{IMG_G1}<div class="figcap">panel general del agente en Grafana</div></div>
              <div>{IMG_G2}<div class="figcap">la cola Redis Streams, instrumentada</div></div>
            </div>
            <div class="chips">
              <span class="chip">aiops_diagnosis_total</span><span class="chip">aiops_enrichment_total</span>
              <span class="chip">aiops_queue_*</span><span class="chip">aiops_feedback_verdict_total</span>
              <span class="chip">aiops_chaos_*</span><span class="chip">aiops_incident_resolution_*</span>
            </div>
            <p class="sm muted" style="margin-top:16px">Cada decisión del pipeline emite métrica: grounding, cola, remediación, veredicto, tiempo de resolución. Zero black-box.</p>
          </div>
          <div class="folio">Evidencia y medición</div>
          <div class="notes" hidden><b>12:25 · ~35 s.</b> Principio de cero caja negra: cada decisión del pipeline emite una métrica Prometheus y se ve en Grafana, del grounding al veredicto, pasando por la cola. Las cifras de las diapositivas anteriores salen de esta capa, no de logs sueltos. La última familia de métricas (tiempo de resolución observado) es la más reciente: da resultado real y barato a las clases de alerta que no tienen veredicto verificado.</div>
        </section>"""
        .replace("{IMG_G1}", img("grafana_overview", "Panel general del agente AIOps en Grafana"))
        .replace("{IMG_G2}", img("grafana_queue", "Fila de la cola Redis Streams en Grafana")),

        # ══════════ DIV · PARTE 03 ══════════
        f"""<section class="slide divider">
          <div class="part">Parte 03 / 03</div>
          <h2>Límites y continuación</h2>
          <p class="sub">Los bordes conocidos del prototipo, con su estado real, y qué merecería medirse después.</p>
          {arcsig(all_on=True)}
          <div class="folio">{TITLE}</div>
          <div class="notes" hidden><b>13:00 · ~5 s.</b> «Y esto es lo que el sistema NO hace todavía.» El raíl completo: el arco entero está en juego, incluidos sus bordes.</div>
        </section>""",

        # ══════════ 14 · LÍMITES ══════════
        f"""<section class="slide">
          <div class="eyebrow">Límites · declarados</div>
          <div class="claim">La credibilidad está en saber <em>dónde están los bordes</em></div>
          <div class="body">
            <dl class="defs">
              <div class="def"><dt>Latencia</dt><dd>techo de hardware: el modelo 1.5b en CPU tarda ~150-270 s por diagnóstico. Se detecta en segundos y se diagnostica al ritmo del hardware. Palanca: GPU o un modelo mayor.</dd></div>
              <div class="def"><dt>Durabilidad</dt><dd>el estado vive en Redis y se re-arma tras un reinicio, pero sin AOF+PVC no sobrevive a la pérdida del pod. Es trabajo pendiente conocido.</dd></div>
              <div class="def"><dt>Dataset</dt><dd>N={STATS['eval_n']} valida los mecanismos; corto para estadística. Ampliar a 30-50 alertas.</dd></div>
              <div class="def"><dt>Memoria</dt><dd>el modelo pequeño no explota la etiqueta del resultado (medido). Explotarla pide un modelo mayor o un guard determinista en el motor.</dd></div>
              <div class="def"><dt>Curación</dt><dd>fuera del caso memoria está medido que el contexto es correcto y los comandos son seguros, pero <strong>no</strong> que el fix cure. Safety ≠ correctness; solo memoria está validado de extremo a extremo como <code>cured</code>.</dd></div>
            </dl>
            <p class="sm muted" style="margin-top:18px">Si la idea mereciera continuar, el paso natural sería probarla sobre alertas reales de un equipo y medir si ahorra tiempo. Hoy es una hipótesis pendiente de probar.</p>
          </div>
          <div class="folio">Límites y continuación</div>
          <div class="notes" hidden><b>13:05 · ~75 s.</b> La diapositiva que más credibilidad da si se cuenta con calma. Cinco bordes, cada uno con su estado: latencia (hardware), durabilidad de Redis (pendiente), dataset corto, la memoria que el modelo pequeño no explota, y la distinción safety ≠ correctness: para las clases fuera de memoria está medido que el contexto y la seguridad son correctos pero no que el arreglo cure, y por eso ese camino nunca es automático. Cerrar sin sobrevender: probar con alertas reales sería el siguiente paso, y hoy es una hipótesis.</div>
        </section>""",

        # ══════════ 15 · CIERRE ══════════
        f"""<section class="slide cover">
          <div class="eyebrow">Cierre · <b>gracias</b></div>
          <h1 style="font-size:46px;max-width:1040px">El cluster informa, el modelo razona, el motor dispone.</h1>
          <p class="sub" style="margin-top:14px">Un prototipo construido despacio, con la seguridad como principio y cada afirmación respaldada por una medida.</p>
          {arcsig(all_on=True)}
          <div class="statstrip" style="max-width:900px">
            <div><b>{STATS['runbooks']}</b><span>runbooks RAG</span></div>
            <div><b class="acc">{STATS['tests']}</b><span>tests en verde</span></div>
            <div><b>{STATS['chaos_experiments']}</b><span>experimentos chaos</span></div>
            <div><b class="acc">{STATS['p1_after']}</b><span>precision@1</span></div>
            <div><b class="okc">100%</b><span>comandos seguros</span></div>
          </div>
          <div class="meta">
            <div><b>{AUTHOR}</b>Prácticas · {SUBJECT}</div>
            <div class="mono" style="align-self:center;color:var(--dim)">preguntas bienvenidas · detalle técnico adicional en la capa de respaldo</div>
          </div>
          <div class="notes" hidden><b>14:20 · ~40 s.</b> Cerrar con la tesis y el arco completo encendido. Agradecer el tiempo e invitar a las preguntas. Detrás de esta diapositiva queda la capa de respaldo (R1 alternativas · R2 seguridad de plataforma · R3 el null en detalle · R4 stack): no presentarla — saltar a ella solo si una pregunta lo pide.</div>
        </section>""",

        # ══════════ R1 · RESPALDO — ALTERNATIVAS ══════════
        """<section class="slide opt">
          <div class="optflag">Respaldo</div>
          <div class="eyebrow">Respaldo · posicionamiento</div>
          <div class="claim">¿Por qué no VPA, <em>o una herramienta comercial</em>?</div>
          <div class="body">
            <dl class="defs">
              <div class="def"><dt>VPA</dt><dd>subir límites de memoria es lo que hace el Vertical Pod Autoscaler, y para ese caso puro sería la herramienta madura. Aquí el caso memoria es el <strong>primer caso de un patrón general</strong>: diagnóstico con contexto, decisión con gates, veredicto verificado y memoria de incidentes. VPA no explica el porqué, no escala a una persona con el contexto reunido, ni aprende del resultado.</dd></div>
              <div class="def"><dt>Comercial</dt><dd>Datadog y similares existen y para muchos equipos son la respuesta; no se compite con ellas. Se exploró qué se puede construir <strong>self-hosted</strong>: los datos no salen del cluster y la capa de decisión es código propio y auditable.</dd></div>
              <div class="def"><dt>Aprendizaje</dt><dd>el valor de las prácticas era construirlo y medirlo: cada regla del motor nace de un fallo observado en el cluster.</dd></div>
            </dl>
            <div class="honest">Sin ROI que reclamar: quedan <b>instrumentadas</b> las métricas para medirlo (tiempo de diagnóstico, % de alertas pre-diagnosticadas) si algún día se probara con alertas reales.</div>
          </div>
          <div class="folio">Respaldo</div>
          <div class="notes" hidden>Si preguntan «¿esto no lo hace ya VPA/Datadog?»: conceder lo que es verdad (para el caso puro de memoria, VPA es la herramienta madura; las suites comerciales existen) y delimitar la aportación: patrón general con explicación, escalación con contexto, veredicto y memoria — self-hosted y auditable. Y el motivo honesto: era un ejercicio de construir y medir.</div>
        </section>""",

        # ══════════ R2 · RESPALDO — SEGURIDAD DE PLATAFORMA ══════════
        """<section class="slide opt">
          <div class="optflag">Respaldo</div>
          <div class="eyebrow">Respaldo · seguridad de plataforma</div>
          <div class="claim">Fail-open en el diagnóstico, <em>fail-closed en lo que actúa</em></div>
          <div class="body">
            <dl class="defs">
              <div class="def"><dt>Auth</dt><dd>callbacks de Mattermost firmados con HMAC-SHA256; secret ausente o vacío → rechazo (401), nunca fail-open.</dd></div>
              <div class="def"><dt>Cola</dt><dd>si Redis cae, el webhook devuelve 503 y Alertmanager reintenta: <strong>no se pierde la alerta</strong>.</dd></div>
              <div class="def"><dt>Blast radius</dt><dd>escritura solo en namespaces propios del proyecto; sin roles de escritura a nivel de cluster.</dd></div>
              <div class="def"><dt>Rollback</dt><dd>contexto de rollback persistido en Redis con TTL; se re-arma al arranque tras un reinicio a mitad de ventana.</dd></div>
              <div class="def"><dt>Cooldown</dt><dd>un patch por workload y ventana (SETNX, fail-closed): sin tormenta de patches aunque la alerta repita.</dd></div>
            </dl>
            <div class="honest">Durabilidad: sin AOF+PVC, el estado en Redis <b>no sobrevive a la pérdida del pod</b>. Es un límite conocido; la solución (AOF+PVC) está en el backlog.</div>
          </div>
          <div class="folio">Respaldo</div>
          <div class="notes" hidden>El principio que ordena esta capa: perder una alerta es peor que perder contexto. Por eso el diagnóstico es fail-open (sin RAG o sin LLM se notifica degradado) y todo lo que actúa es fail-closed (auth, cola, cooldown). Si preguntan por la durabilidad de Redis, la respuesta honesta está en la nota: se persiste y se re-arma, pero no se reclama durabilidad fuerte sobre memoria volátil.</div>
        </section>""",

        # ══════════ R3 · RESPALDO — EL NULL EN DETALLE ══════════
        """<section class="slide opt">
          <div class="optflag">Respaldo</div>
          <div class="eyebrow">Respaldo · la memoria y el modelo</div>
          <div class="claim">El null también es un resultado: <em>se pre-registró antes de medir</em></div>
          <div class="body">
            <div class="split s55" style="align-items:start">
              <div>
                <div class="h3">Mismo aviso, tres recuerdos distintos</div>
                <table style="margin-top:10px">
                  <tr><th>Brazo</th><th>Propone</th><th class="num">Conf.</th><th class="num">Menciona el fracaso</th></tr>
                  <tr><td>control (sin memoria)</td><td>subir memoria</td><td class="num">0,80</td><td class="num">—</td></tr>
                  <tr><td>recuerda «no funcionó»</td><td>subir memoria</td><td class="num">0,80</td><td class="num">0 / 3</td></tr>
                  <tr><td>recuerda «sí funcionó»</td><td>subir memoria</td><td class="num">0,80</td><td class="num">0 / 3</td></tr>
                </table>
                <p class="sm muted" style="margin-top:14px">A escala 1.5b, la etiqueta del resultado no cambia el diagnóstico: salidas idénticas en los tres brazos.</p>
              </div>
              <dl class="defs">
                <div class="def ok"><dt>Sí funciona</dt><dd>la mitad que importa: el conocimiento se guarda y se recupera por clase (0 % → 46,7 % p@1).</dd></div>
                <div class="def"><dt>No rompe</dt><dd>la seguridad la impone el motor determinista, no la memoria del modelo — el null no debilita ningún gate.</dd></div>
                <div class="def"><dt>Lo movería</dt><dd>un modelo mayor, o un guard en el motor que vete un fix ya marcado como fallido.</dd></div>
              </dl>
            </div>
          </div>
          <div class="folio">Respaldo</div>
          <div class="notes" hidden>Si preguntan «entonces, ¿el aprendizaje no funciona?»: funciona la mitad que importa, y está medida — el conocimiento negativo se guarda y se recupera. Lo que no ocurre a este tamaño de modelo es que lo explote al diagnosticar, y eso se pre-registró antes de la medición como resultado posible. Publicar el null es una decisión: vale más que un positivo inventado, y apunta la mejora concreta (modelo mayor o guard determinista).</div>
        </section>""",

        # ══════════ R4 · RESPALDO — STACK ══════════
        """<section class="slide opt">
          <div class="optflag">Respaldo</div>
          <div class="eyebrow">Respaldo · stack e infraestructura</div>
          <div class="claim">Todo dentro del cluster: <em>los datos no salen, el coste por token es cero</em></div>
          <div class="body">
            <dl class="defs">
              <div class="def"><dt>Inferencia</dt><dd>Ollama in-cluster: un modelo de 1.5b para generación y otro para embeddings. Sin API externa.</dd></div>
              <div class="def"><dt>Estado</dt><dd>ChromaDB (memoria RAG de runbooks e incidentes) + Redis (cola Streams, escalaciones, rollback, cooldown).</dd></div>
              <div class="def"><dt>Entrega</dt><dd>CI en cada push + pipeline de build con los tests como gate; la imagen desplegada se valida de extremo a extremo en el cluster.</dd></div>
              <div class="def"><dt>Observabilidad</dt><dd>Prometheus + Grafana propios, con dashboards del agente y de la cola.</dd></div>
            </dl>
            <p class="mono xs" style="color:var(--dim);margin-top:20px">FastAPI · Python 3.11 · Pydantic v2 · httpx · GKE · namespaces aislados para el chaos</p>
          </div>
          <div class="folio">Respaldo</div>
          <div class="notes" hidden>Si preguntan por el stack o el coste: todo es open source y corre dentro del cluster — la factura marginal por diagnóstico es cero y los datos no salen. El chaos se inyecta en un namespace aislado para no afectar a nadie más en el cluster compartido. La entrega tiene dos gates: CI en cada push y los tests como gate del build de la imagen.</div>
        </section>""",
    ]


# ── Guion: runbook de la presentación + banco de preguntas ──

RUNBOOK = [
    ("Cómo usar el deck", "Un único fichero HTML offline (funciona sin red). Flechas / espacio / clic para avanzar; <strong>N</strong> abre las notas del orador de cada diapositiva (con presupuesto de tiempo); <strong>F</strong> pantalla completa; <strong>Ctrl+P</strong> exporta a PDF (una diapositiva por página) como copia de seguridad. La capa de respaldo (R1–R4) está después del cierre y el contador la marca aparte: no se presenta, se salta a ella solo si una pregunta lo pide."),
    ("Plan B", "La demo es un replay embebido en el propio HTML (sin dependencia de red ni de cluster en directo), así que el riesgo clásico de demo en vivo no existe. Si el portátil fallara: copia del fichero en el móvil/USB y el PDF exportado como último recurso."),
    ("El hilo", "Tesis en una frase: «el cluster informa, el modelo razona, el motor dispone». Parte 1 — cómo está construido (pipeline, tres capas, grounding, motor, bucle de aprendizaje). Parte 2 — la evidencia (replay del run real, retrieval medido, la memoria cuantificada con su null, chaos, observabilidad). Parte 3 — límites sin maquillar y cierre. Registro: plural/impersonal, tono de prototipo honesto: nada de ROI ni de competir con suites comerciales."),
    ("Los dos momentos de la demo", "En el replay, detenerse en la etapa 4 (el motor sella 32Mi desde el cluster y, ante un 16×, ni aplica ni recorta: escala) y en la 6 (el veredicto cured verificado contra la salud real del pod, que re-marca la memoria). Es la tesis completa en un run real."),
]

QA = [
    ("¿Por qué escala a humano si hay remediación automática?", "El motor solo auto-remedia los casos estructurados que cumplen las tres condiciones (target confirmable, acción acotada y reversible, resultado verificable), dentro de los namespaces permitidos y con confianza fundada suficiente. Todo lo demás se escala con el comando ya sintetizado para que una persona lo apruebe. Los experimentos de chaos generan riesgo alto o target no resoluble, así que escalan por diseño."),
    ("¿Por qué un LLM local tan pequeño?", "Por las restricciones del entorno: datos dentro del cluster, sin API externa y sin coste por token. Y porque se midió que, con el contexto bien servido (grounding + runbook), el modelo pequeño acierta el campo a corregir; el fallo típico estaba en el contexto ausente, no en el modelo. La palanca era enriquecer el contexto, no más GPU."),
    ("Un modelo pequeño puede declarar confianza alta con razonamiento imperfecto. ¿Cómo se mitiga?", "No se confía en la confianza del modelo: se deriva de señales del cluster (último motivo de terminación + número de reinicios). La del modelo se conserva aparte como model_confidence, pero la que gobierna la decisión es la fundada. Además el motor sella los valores: el texto libre del LLM no llega a ejecutarse."),
    ("¿Qué pasa si ChromaDB, Ollama o Redis fallan?", "Fail-open en el diagnóstico: sin RAG o sin LLM se notifica en modo degradado — no se pierde la alerta. Fail-closed en lo que actúa: si Redis cae, la cola devuelve 503 y Alertmanager reintenta; la autenticación con secret vacío rechaza. Perder una alerta es peor que perder contexto."),
    ("¿Qué demuestra la evaluación con N=15?", "Que el filtro por clase de error sube precision@1 del 73 % al 100 % y que el RAG produce el 100 % de comandos seguros frente al 25 % en zero-shot. N=15 valida el mecanismo, no afirma estadística fina; el plan es ampliar a 30-50. No se vende de más."),
    ("Entonces, ¿el aprendizaje no funciona?", "Funciona la mitad que importa, y está medida. El conocimiento (incluido el negativo) se guarda y se recupera por clase: de 0 % a 46,7 % de acierto. Lo que no ocurre a escala 1.5b es que el modelo lo explote para cambiar su diagnóstico — un null que se pre-registró antes de correr la medición. Por diseño no rompe nada: la seguridad la impone el motor determinista, no la memoria del modelo. Con un modelo mayor, o con un guard que vete un fix marcado como fallido, esa capa se movería. Se publica el null porque un null pre-registrado vale más que un positivo inventado."),
    ("¿Se reclama durabilidad con el estado en Redis?", "No del todo, y se dice abiertamente. El estado (escalaciones, rollback) se persiste en Redis y se re-arma al reinicio, pero sin AOF+PVC no sobrevive a la pérdida del pod. Es un pendiente conocido del backlog: o se endurece con AOF+PVC o se mantiene el claim degradado. No se reclama durabilidad fuerte sobre memoria volátil."),
    ("El ×2 era la regla de seguridad. ¿No la rompe aprobar un 16×?", "No, porque el ×2 no es un techo absoluto: es la frontera de lo que el sistema hace solo. Hasta ×2 se aplica en automático; un salto mayor no lo ejecuta la máquina — va a una persona con el comando ya preparado. En el run el pod moría con 32Mi, un límite irrisorio donde ×2 casi seguro volvería a fallar; una persona miró el contexto y aprobó 512Mi. La regla hizo exactamente su trabajo: impedir que la máquina aplicara un 16× a la callada. Y ese refinamiento ya está construido: hoy la escalación ofrece dos botones bien formados — la ×2 determinista del motor, recomendada, y el valor que propuso el modelo — cada uno firmado por separado (HMAC por acción, la elección no se puede alterar en tránsito). El sistema no esconde la propuesta del LLM: la pone junto a la opción segura y deja arbitrar."),
    ("¿Por qué en automático solo se remedia memoria? ¿No es muy poco?", "Es poco a propósito, y el criterio es explícito: una acción entra en el camino automático solo si cumple tres condiciones a la vez — target confirmable desde el cluster, acción acotada y reversible, y resultado verificable. Subir un límite de memoria cumple las tres. Un CrashLoop de configuración no cumple la primera, y para casi nada fuera de memoria existe hoy una acción acotada genérica. Antes de ampliar el catálogo, lo honesto es ampliar la medición: extender el veredicto a las remediaciones humanas por clase y promover a automático solo lo que demuestre curar."),
    ("¿Los comandos que sugiere para un CrashLoop o un ImagePull funcionan?", "Lo que está medido: el runbook recuperado es el correcto (precision@1 100 %) y los comandos son 100 % seguros frente al 25 % en zero-shot. Lo que no está medido es que ese fix cure — safety no es correctness, y solo el caso memoria está validado de extremo a extremo como cured. Además esos comandos son texto libre del modelo, no sellados por el motor: pasan un pre-flight de permisos (kubectl auth can-i) y lo que RBAC deniega se muestra como sugerencia sin botón. Por eso ese camino nunca es automático: es asistencia a una persona, y se presenta como tal."),
    ("Si el sistema no ejecuta la mayoría de los fixes, ¿cómo sabe si lo que sugirió funcionó?", "Con dos señales de distinta fuerza. La fuerte: el veredicto del rollback (cured / rolled_back), que el motor verifica mirando la salud real del pod — solo existe en el camino auto/aprobación. La observacional: cuando Alertmanager envía el resolved, se correla por huella con el incidente que lo disparó → tiempo de resolución y outcome resolved_observed para los que no tenían veredicto. Es señal débil (la causa no está verificada: pudo arreglarlo una persona o auto-sanarse) y por eso nunca pisa a la fuerte — pero da un resultado real y barato para todas las clases de alerta."),
    ("Si el sistema propone algo factible pero no seguro, ¿qué gana?", "La seguridad. La capa de validación valida seguridad, no factibilidad — que un comando dé Forbidden por least-privilege no es una vía para saltarse un gate. La factibilidad se comprueba aparte: antes de ofrecer botones, cada comando de texto libre se pre-vuela con kubectl auth can-i y lo denegado se muestra como sugerencia, sin ampliar RBAC. Ante la duda, se escala a una persona con el comando ya preparado."),
    ("¿Esto toca producción real?", "Es un cluster GKE real y compartido, no docker-compose, con observabilidad y CI reales. Los fallos se inyectan en un namespace aislado y toda la escritura está acotada a los namespaces del proyecto, sin roles de escritura a nivel de cluster, para no afectar a nadie más."),
    ("¿Por qué no usar Datadog u otra herramienta comercial?", "Existen y para muchos casos serían la respuesta; no se compite con ellas. Se exploró qué se puede construir self-hosted: el LLM local (los datos no salen del cluster) y una capa de decisión que es código propio y auditable. El valor era, sobre todo, aprender construyéndolo y midiéndolo."),
    ("¿Esto ahorra tiempo o dinero? ¿Hay ROI?", "No se ha medido y no se va a inventar: es un prototipo de prácticas. Lo que sí queda instrumentado son las métricas para medirlo (tiempo de diagnóstico, porcentaje de alertas pre-diagnosticadas, tiempo de resolución observado) si algún día se probara con alertas reales. Prometer un número hoy sería deshonesto."),
    ("¿Por qué no VPA para el caso de memoria?", "Para el caso puro de memoria, VPA sería la herramienta madura, y se reconoce. Aquí ese caso es el primero de un patrón más general: diagnóstico con contexto y explicación, decisión con gates, escalación a una persona con todo reunido, veredicto verificado y memoria de incidentes. VPA no explica el porqué, no escala con contexto ni aprende del resultado. Son cosas distintas que se tocan en un punto."),
]


def build_deck() -> str:
    css = font_css() + "\n" + CSS_BASE
    return (
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{TITLE}</title><style>{css}</style></head><body>"
        "<div id='stage'>"
        + "\n".join(slides())
        + "</div>"
        "<div id='hud' aria-hidden='true'><div class='track'><div class='fill'></div><div class='dots'></div></div></div>"
        f"<div id='brand'>{TITLE.upper()} · V3</div>"
        "<div id='counter'></div>"
        "<div id='nav'><button id='prev' title='Anterior (←)'>‹</button><button id='next' title='Siguiente (→)'>›</button></div>"
        "<div id='help'>← → navegar · N notas · F pantalla completa</div>"
        "<div id='notes'><div class='ntag'>Notas del orador</div><div id='notes-body'></div></div>"
        f"<script>{JS}</script></body></html>"
    )


def build_guion() -> str:
    css = """
    body{margin:0;background:#fcfcfa;color:#101418;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;line-height:1.6;padding:5vh 8vw;max-width:980px}
    h1{color:#b23e00;font-size:1.9rem} h2{border-bottom:1px solid #dcdfe2;padding-bottom:.25em;margin-top:1.6em;font-size:1.25rem}
    .block{background:#fff;border:1px solid #dcdfe2;border-left:4px solid #ff5a00;border-radius:8px;padding:1em 1.2em;margin:.9em 0}
    .q{color:#b23e00;font-weight:700;margin-top:1.1em}.a{color:#47525c}
    code{background:#f2f0ec;border:1px solid #dcdfe2;border-radius:5px;padding:.04em .3em;color:#b23e00}
    .lead{color:#55606b}
    """
    blocks = "".join(f"<div class='block'><h2>{title}</h2><p>{text}</p></div>" for title, text in RUNBOOK)
    qa = "".join(f"<p class='q'>{q}</p><p class='a'>{a}</p>" for q, a in QA)
    return (
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        f"<title>{TITLE} — Guion</title><style>{css}</style></head><body>"
        f"<h1>Guion — {TITLE}</h1><p class='lead'>{SUBTITLE} · {AUTHOR} · {SUBJECT}</p>"
        f"{blocks}<h2>Preguntas probables</h2>{qa}</body></html>"
    )


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    deck = build_deck()
    guion = build_guion()
    (OUT_DIR / "demo_v3.html").write_text(deck, encoding="utf-8")
    (OUT_DIR / "guion_v3.html").write_text(guion, encoding="utf-8")

    print("== build_demo_v3 ==")
    print(f"  demo/demo_v3.html  {len(deck) // 1024} KB")
    print(f"  demo/guion_v3.html {len(guion) // 1024} KB")
    if _EMBEDDED:
        print("  recursos embebidos:")
        for item in _EMBEDDED:
            print(f"    + {item}")
    if _MISSING:
        print("  recursos pendientes:")
        for item in _MISSING:
            print(f"    - {item}")


if __name__ == "__main__":
    main()
