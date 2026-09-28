"""Self-contained local HTML renderer for a Hedge paper dashboard."""
from __future__ import annotations

from decimal import Decimal
from html import escape
import json
from pathlib import Path
from typing import Any, Mapping

from .flows import DASHBOARD_SCHEMA


def _json_default(value: object) -> float:
    """Serialize finite Decimals supplied to this defensive renderer boundary."""
    if isinstance(value, Decimal) and value.is_finite():
        return float(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _json_for_script(value: Mapping[str, Any]) -> str:
    """Serialize safely inside a non-executable JSON script element."""
    return (json.dumps(value, allow_nan=False, default=_json_default, separators=(",", ":"))
            .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render_dashboard(report: Mapping[str, Any], *, title: str = "Hedge paper dashboard") -> str:
    """Render a :func:`hedge.flows.build_report` result as one local HTML file."""
    if report.get("schema_version") != DASHBOARD_SCHEMA:
        raise ValueError(f"expected {DASHBOARD_SCHEMA} report")
    data = _json_for_script(report)
    safe_title = escape(title)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title><style>
:root {{color-scheme:dark;--bg:#0b1020;--panel:#131b31;--line:#2a385f;--text:#eaf0ff;--muted:#aebbd8;--good:#4ade80;--warn:#fbbf24;--bad:#fb7185;--blue:#60a5fa}}
*{{box-sizing:border-box}}body{{margin:0;padding:24px;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,sans-serif}}h1{{margin:0 0 4px;font-size:25px}}h2{{font-size:16px;margin:0 0 12px}}h3{{font-size:14px;margin:0 0 6px}}.subtle,.empty{{color:var(--muted)}}
.grid{{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));margin:20px 0}}.layout{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}}.wide{{grid-column:1/-1}}.panel,.metric,.position{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:15px}}.metric .label{{color:var(--muted);font-size:12px}}.metric .value{{font-size:22px;font-weight:650;margin-top:3px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(245px,1fr));gap:10px}}.position dl{{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:10px 0 0}}.position dt{{color:var(--muted)}}.position dd{{margin:0;text-align:right}}svg{{display:block;width:100%;height:185px;overflow:visible}}.axis{{stroke:var(--line);stroke-width:1}}.line{{fill:none;stroke:var(--blue);stroke-width:2.5}}.dot{{fill:var(--blue)}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:8px 5px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{color:var(--muted);font-size:12px}}.badge{{display:inline-block;padding:2px 7px;border-radius:999px;font-size:12px;font-weight:650;background:#253354}}.ok,.fresh,.complete,.approved{{color:var(--good)}}.warning,.stale,.running{{color:var(--warn)}}.blocked,.failed,.rejected{{color:var(--bad)}}.flow{{list-style:none;padding:0;margin:0}}.flow li{{padding:10px 0 10px 15px;border-left:2px solid var(--line)}}.flow li:last-child{{padding-bottom:0}}code{{color:#c4b5fd}}
</style></head><body><header><h1>{safe_title}</h1><div id="meta" class="subtle"></div></header><section id="metrics" class="grid"></section><section class="layout">
<article class="panel wide"><h2>Fund return periods</h2><div id="returns"></div></article><article class="panel"><h2>Paper equity curve</h2><div id="equity-chart"></div></article><article class="panel"><h2>P&amp;L</h2><div id="pnl-chart"></div></article><article class="panel"><h2>Drawdown</h2><div id="drawdown-chart"></div></article><article class="panel"><h2>Virtual contributions</h2><div id="contribution-chart"></div></article><article class="panel wide"><h2>Capital accounts</h2><div id="capital-accounts"></div></article><article class="panel wide"><h2>Data freshness</h2><div id="freshness"></div></article><article class="panel"><h2>Research-agent flow</h2><ol id="research" class="flow"></ol></article><article class="panel"><h2>Decision and proposed-order flow</h2><ol id="decisions" class="flow"></ol></article><article class="panel wide"><h2>Paper positions</h2><div id="positions"></div></article></section>
<script id="hedge-report" type="application/json">{data}</script><script>
(() => {{
 const report=JSON.parse(document.getElementById('hedge-report').textContent),$=id=>document.getElementById(id),finite=v=>Number.isFinite(Number(v))?Number(v):null;
 const money=v=>{{const n=finite(v);return n==null?'—':new Intl.NumberFormat('en-US',{{style:'currency',currency:'USD'}}).format(n)}},percent=v=>{{const n=finite(v);return n==null?'—':`${{n.toFixed(2)}}%`}},date=v=>{{const d=v?new Date(v):null;return d&&!Number.isNaN(d.valueOf())?d.toLocaleString():'No timestamp'}},esc=v=>String(v??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])),status=v=>`<span class="badge ${{esc(v)}}">${{esc(v)}}</span>`;
 $('meta').textContent=`Generated ${{date(report.generated_at)}} · PAPER ONLY · No order submission`;
 const summary=report.summary||{{}},fund=summary.fund||{{}},risk=summary.risk||{{status:'unknown',reasons:[]}},metrics=[['Fund NAV',money(fund.nav??summary.nav)],['Cash',money(fund.cash??summary.cash)],['Paper P&L',money(summary.pnl)],['Max drawdown',percent(summary.max_drawdown_pct)],['Virtual pool',money((finite(summary.virtual_contributions_cents)||0)/100)],['Risk status',status(risk.status)+((risk.reasons||[]).length?`<div class="subtle">${{esc((risk.reasons||[]).join('; '))}}</div>`:'')]];
 $('metrics').innerHTML=metrics.map(([label,value])=>`<div class="metric"><div class="label">${{esc(label)}}</div><div class="value">${{value}}</div></div>`).join('');
 const periods=fund.return_periods||summary.return_periods||[]; $('returns').innerHTML=periods.length?`<table><thead><tr><th>Period</th><th>Fund return</th><th>${{esc((fund.benchmark||{{}}).name||'Benchmark')}}</th><th>Delta</th></tr></thead><tbody>${{periods.map(r=>`<tr><td>${{esc(r.period)}}</td><td>${{esc(percent(r.return_pct))}}</td><td>${{esc(percent(r.benchmark_return_pct))}}</td><td>${{esc(percent(r.benchmark_delta_pct))}}</td></tr>`).join('')}}</tbody></table>`:'<p class="empty">No normalized return periods supplied.</p>';
 function line(target,series){{const points=(series&&series.points||[]).filter(p=>finite(p.value)!=null);if(!points.length){{$(target).innerHTML='<p class="empty">No timestamped paper data.</p>';return}}const values=points.map(p=>finite(p.value)),lo=Math.min(...values),hi=Math.max(...values),span=hi-lo||1,coords=points.map((p,i)=>[i*100/Math.max(1,points.length-1),100-((finite(p.value)-lo)/span*100)]),path=coords.map((p,i)=>`${{i?'L':'M'}}${{p[0].toFixed(2)}},${{p[1].toFixed(2)}}`).join(' '),last=points.at(-1),lastCoord=coords.at(-1);$(target).innerHTML=`<svg viewBox="-3 -8 106 120" role="img" aria-label="${{esc(series.name)}} chart"><line class="axis" x1="0" y1="100" x2="100" y2="100"/><path class="line" d="${{path}}"/><circle class="dot" cx="${{lastCoord[0].toFixed(2)}}" cy="${{lastCoord[1].toFixed(2)}}" r="2.5"/></svg><div class="subtle">${{esc(series.name)}}: ${{esc(last.value)}} ${{esc(series.unit)}} · ${{esc(date(last.at))}}</div>`}}
 const chart=n=>(report.charts&&report.charts[n]||[]),named=(n,label)=>chart(n).find(s=>s.name===label);line('equity-chart',named('equity_curve','Paper equity')||named('equity','Paper equity'));line('pnl-chart',named('equity','Paper P&L'));line('drawdown-chart',named('equity','Drawdown'));line('contribution-chart',named('contributions','Virtual contributions'));
 const fresh=report.freshness||[];$('freshness').innerHTML=`<table><thead><tr><th>Feed</th><th>Status</th><th>Last update</th><th>Age</th></tr></thead><tbody>${{fresh.map(i=>`<tr><td>${{esc(i.name)}}</td><td>${{status(i.status)}}</td><td>${{esc(date(i.at))}}</td><td>${{i.age_seconds==null?'—':esc(i.age_seconds+'s')}}</td></tr>`).join('')}}</tbody></table>`;
 const accounts=report.capital_accounts||[];$('capital-accounts').innerHTML=accounts.length?`<table><thead><tr><th>Member</th><th>Contributed</th><th>Withdrawn</th><th>NAV</th><th>Realized P&amp;L</th><th>Unrealized P&amp;L</th><th>Allocation</th></tr></thead><tbody>${{accounts.map(r=>`<tr><td><b>${{esc(r.display_name||r.member_id)}}</b></td><td>${{esc(money((finite(r.contributed_cents)||0)/100))}}</td><td>${{esc(money((finite(r.withdrawn_cents)||0)/100))}}</td><td>${{esc(money((finite(r.nav_cents)||0)/100))}}</td><td>${{esc(money((finite(r.realized_pnl_cents)||0)/100))}}</td><td>${{esc(money((finite(r.unrealized_pnl_cents)||0)/100))}}</td><td>${{esc(percent(r.allocation_pct))}}</td></tr>`).join('')}}</tbody></table>`:'<p class="empty">No normalized capital accounts supplied.</p>';
 const stages=report.flows&&report.flows.research||[];$('research').innerHTML=stages.length?stages.map(s=>`<li><b>${{esc(s.agent)}}</b> ${{status(s.status)}}<br><span class="subtle">${{esc(date(s.started_at))}} → ${{esc(date(s.completed_at))}}${{s.duration_seconds==null?'':' · '+esc(s.duration_seconds)+'s'}}</span></li>`).join(''):'<li class="empty">No research stages supplied.</li>';
 const decisions=report.flows&&report.flows.decisions||[];$('decisions').innerHTML=decisions.length?decisions.map(i=>`<li><b><code>${{esc(i.decision_id)}}</code></b> ${{status(i.status)}}<br><span class="subtle">${{esc(i.intent_count)}} proposed order(s) · ${{esc(date(i.at))}} · age ${{i.age_seconds==null?'—':esc(i.age_seconds)+'s'}}</span></li>`).join(''):'<li class="empty">No normalized decisions supplied.</li>';
 const rows=report.positions||[];$('positions').innerHTML=rows.length?`<div class="cards">${{rows.map(r=>`<article class="position"><h3>${{esc(r.symbol)}}</h3><div>${{status(r.thesis_status)}} ${{status(r.risk_status)}}</div><dl><dt>Quantity</dt><dd>${{esc(r.quantity)}}</dd><dt>Entry price</dt><dd>${{esc(money(r.entry_price??r.avg_cost))}}</dd><dt>Current price</dt><dd>${{esc(money(r.current_price??r.mark_price))}}</dd><dt>Market value</dt><dd>${{esc(money(r.market_value))}}</dd><dt>Unrealized P&amp;L</dt><dd>${{esc(money(r.unrealized_pnl))}}</dd><dt>Realized P&amp;L</dt><dd>${{esc(money(r.realized_pnl))}}</dd><dt>Allocation</dt><dd>${{esc(percent(r.allocation_pct))}}</dd></dl><div class="subtle">As of ${{esc(date(r.as_of))}}</div></article>`).join('')}}</div>`:'<p class="empty">No paper positions supplied.</p>';
}})();</script></body></html>"""


def write_dashboard(path: str | Path, report: Mapping[str, Any], *, title: str = "Hedge paper dashboard") -> Path:
    """Write a self-contained report file. Parent directories are created."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_dashboard(report, title=title), encoding="utf-8")
    return output
