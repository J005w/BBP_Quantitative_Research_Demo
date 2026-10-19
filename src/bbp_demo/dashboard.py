"""Dependency-free, read-only playback of an already computed research report.

The server binds only to loopback. It never loads files from a request, runs a
strategy, accepts configuration changes, or connects to an execution service.
"""

from __future__ import annotations

import copy
import hmac
import ipaddress
import json
import secrets
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

MAX_REQUEST_BODY = 16_384
TOKEN_HEADER = "X-BBP-Token"


def _json(value: Any) -> str:
    """Strict JSON, also safe inside a non-executable HTML script element."""
    return (json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def render_dashboard(report: dict[str, Any], *, token: str = "", nonce: str | None = None) -> str:
    """Render a self-contained playback; report strings are always treated as data.

    ``token`` and ``nonce`` are supplied by :func:`make_server`. A standalone
    render also works without an HTTP endpoint, because playback uses its embedded
    immutable report rather than fetching remote resources.
    """
    import html

    nonce = nonce or secrets.token_urlsafe(24)
    page = _PAGE.replace("__NONCE__", html.escape(nonce, quote=True))
    page = page.replace("__TOKEN__", _json(token))
    return page.replace("__REPORT__", _json(report))


class PlaybackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _IPv6PlaybackServer(PlaybackServer):
    address_family = socket.AF_INET6


def make_server(report: dict[str, Any], host: str = "127.0.0.1", port: int = 0) -> PlaybackServer:
    """Create, but do not start, the server. Port zero selects a free local port.

    Supported hosts are ``127.0.0.1``, ``::1`` and ``localhost``. The last binds
    deterministically to IPv4 loopback, avoiding DNS resolution and wildcards.
    """
    if host == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("Dashboard host must be an explicit loopback address") from exc
    if str(address) not in {"127.0.0.1", "::1"}:
        raise ValueError("Dashboard host must be 127.0.0.1, ::1 or localhost")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("Dashboard port must be between 0 and 65535")
    # Validate before binding and retain a private snapshot of the supplied data.
    frozen_report = copy.deepcopy(report)
    report_bytes = _json(frozen_report).encode("utf-8")
    session_token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "BBPPlayback"
        sys_version = ""
        timeout = 5

        def log_message(self, _format: str, *args: Any) -> None:
            # Requests may include the per-session token; never log request URLs.
            return

        def _send(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8", *, nonce: str = "") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            csp = "default-src 'none'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'; connect-src 'self'; img-src 'self' data:; "
            csp += f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'" if nonce else "script-src 'none'; style-src 'none'"
            self.send_header("Content-Security-Policy", csp)
            if status == 405:
                self.send_header("Allow", "GET, HEAD")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _validate_request(self) -> bool:
            port_number = self.server.server_address[1]
            authorities = ({f"[::1]:{port_number}", f"localhost:{port_number}"}
                           if address.version == 6 else {f"127.0.0.1:{port_number}", f"localhost:{port_number}"})
            hosts = self.headers.get_all("Host", [])
            origins = self.headers.get_all("Origin", [])
            if len(hosts) != 1 or hosts[0].lower() not in authorities:
                self._send(403, b"Forbidden host\n")
                return False
            if origins and (len(origins) != 1 or origins[0].lower() not in {f"http://{h}" for h in authorities}):
                self._send(403, b"Forbidden origin\n")
                return False
            if self.headers.get("Sec-Fetch-Site", "same-origin").lower() not in {"same-origin", "none"}:
                self._send(403, b"Forbidden request context\n")
                return False
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) > 1:
                self._send(400, b"Unsupported request framing\n")
                return False
            if lengths:
                try:
                    length = int(lengths[0])
                except ValueError:
                    self._send(400, b"Invalid content length\n")
                    return False
                if length < 0:
                    self._send(400, b"Invalid content length\n")
                    return False
                if length > MAX_REQUEST_BODY:
                    self._send(413, b"Request body too large\n")
                    return False
                if length and self.command in {"GET", "HEAD"}:
                    self._send(400, b"Request body not supported\n")
                    return False
            return True

        def do_GET(self) -> None:
            if not self._validate_request():
                return
            try:
                request = urlsplit(self.path)
            except ValueError:
                self._send(400, b"Invalid URL\n")
                return
            if request.scheme or request.netloc or request.fragment:
                self._send(400, b"Only local relative URLs are supported\n")
                return
            if request.path == "/" and not request.query:
                nonce = secrets.token_urlsafe(24)
                body = render_dashboard(frozen_report, token=session_token, nonce=nonce).encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8", nonce=nonce)
            elif request.path == "/api/report":
                try:
                    parameters = parse_qs(request.query, keep_blank_values=True, max_num_fields=10)
                except ValueError:
                    self._send(400, b"Too many query parameters\n")
                    return
                query_tokens = parameters.get("token", [])
                header_tokens = self.headers.get_all(TOKEN_HEADER, [])
                tokens = query_tokens + header_tokens
                if (set(parameters) - {"token"} or not tokens or len(query_tokens) > 1 or len(header_tokens) > 1
                        or any(not hmac.compare_digest(value.encode("utf-8"), session_token.encode("ascii")) for value in tokens)):
                    self._send(403, b"Session token required\n")
                    return
                self._send(200, report_bytes, "application/json; charset=utf-8")
            else:
                self._send(404, b"Not found\n")

        do_HEAD = do_GET

        def _reject_write(self) -> None:
            if self._validate_request():
                self._send(405, b"Read-only playback; method not allowed\n")

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = _reject_write

        def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
            # BaseHTTPRequestHandler's default error page reflects request input.
            self._send(405 if code == 501 else code, b"Request rejected\n")

    server_type = _IPv6PlaybackServer if address.version == 6 else PlaybackServer
    return server_type((host, port), Handler)


def serve(report: dict[str, Any], host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve until Ctrl+C; the report is computed before this function is called."""
    server = make_server(report, host, port)
    authority = f"[{server.server_address[0]}]" if server.address_family == socket.AF_INET6 else server.server_address[0]
    print(f"Synthetic research playback: http://{authority}:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


_PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><title>BBP · Research playback</title>
<style nonce="__NONCE__">
:root{color-scheme:dark;--bg:#0b1119;--panel:#111c28;--line:#263444;--text:#e7edf3;--muted:#a5b5c5;--cyan:#66d5e4;--green:#82d9b1;--red:#f3a39a;--amber:#f3ce8a}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}button,input,select{font:inherit}button,select{color:var(--text);background:#1b2a39;border:1px solid #405367;border-radius:7px;padding:8px 13px}button:hover{background:#293d50}button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid var(--cyan);outline-offset:3px}button:disabled{opacity:.45}main{max-width:1440px;margin:auto;padding:30px 34px 48px}.masthead{display:flex;justify-content:space-between;align-items:center;gap:24px;margin-bottom:28px}.brand{font-size:13px;letter-spacing:.16em;text-transform:uppercase;color:var(--cyan);font-weight:650}h1{font-size:29px;letter-spacing:-.7px;margin:5px 0 0}h2{font-size:16px;margin:0}h3{font-size:13px;color:var(--muted);font-weight:500;margin:0}p{margin:0}.muted{color:var(--muted)}.badge{font-size:12px;color:var(--amber);border:1px solid #665335;border-radius:5px;padding:5px 9px;white-space:nowrap;letter-spacing:.04em}.notice{background:#24251f;border:1px solid #4e4b36;border-left:3px solid var(--amber);border-radius:7px;padding:14px 18px;color:#e7d7b5;margin-bottom:22px;font-size:13px}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:22px}.metric{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:17px 20px}.metric strong{display:block;font-variant-numeric:tabular-nums;font-size:27px;font-weight:600;margin-top:3px}.metric small{color:var(--muted);font-size:11px}.layout{display:grid;grid-template-columns:minmax(0,2.4fr) minmax(290px,1fr);gap:20px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}.panel-head{padding:18px 20px 12px;display:flex;justify-content:space-between;gap:16px;align-items:center}.tiny{font-size:11px;color:var(--muted)}#chart{width:100%;height:auto;display:block;min-height:240px}.chart-wrap{padding:0 12px}.legend{display:flex;gap:18px;font-size:11px;padding:0 20px 13px;color:var(--muted)}.dot{display:inline-block;width:7px;height:7px;background:var(--green);border-radius:50%;margin-right:5px}.dot.red{background:var(--red)}.controls{padding:16px 20px;border-top:1px solid var(--line);background:#101a25}.transport{display:flex;align-items:center;gap:9px;margin-bottom:14px}.transport #play{min-width:92px;background:#23404b;border-color:#397280}#clock{margin-left:auto;color:var(--cyan);font-size:12px;font-variant-numeric:tabular-nums}#scrubber{width:100%;accent-color:var(--cyan);cursor:pointer}.timeline-label{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:4px}.facts{padding:0 20px 18px}.fact{padding:10px 0;border-bottom:1px solid var(--line);font-size:12px;display:grid;grid-template-columns:1fr 1.1fr;gap:10px}.fact:last-child{border:0}.fact span{color:var(--muted)}.fact b{font-weight:500;overflow-wrap:anywhere}.section{margin-top:20px}.table-scroll{overflow:auto;max-height:360px}table{width:100%;border-collapse:collapse;min-width:760px;font-size:12px}th{text-align:left;font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);font-weight:500;background:#152230;position:sticky;top:0;z-index:1}th,td{padding:12px 17px;border-bottom:1px solid var(--line);vertical-align:top}td{font-variant-numeric:tabular-nums}td.reason{min-width:230px;max-width:420px}.pill{display:inline-block;font-size:10px;font-weight:650;letter-spacing:.04em;padding:3px 7px;border-radius:4px;background:#203e35;color:var(--green)}.pill.reject{background:#402b2c;color:var(--red)}.pill.other{background:#293442;color:#c0ccd9}.empty{padding:26px 20px;color:var(--muted);font-size:13px}.two-col{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:20px}.explain{padding:0 20px 18px;color:var(--muted);font-size:12px}.status{display:flex;align-items:center;gap:7px;font-size:11px;color:var(--muted)}.status i{width:6px;height:6px;border-radius:50%;background:var(--green)}footer{margin-top:24px;font-size:11px;color:#91a3b5;display:flex;justify-content:space-between;gap:15px}.checkbox{font-size:11px;color:var(--muted);display:flex;align-items:center;gap:5px}#summary-note{font-size:12px;color:var(--muted);padding:0 20px 16px}.causal-note{font-size:11px;color:var(--muted);padding:0 20px 14px}noscript{display:block;padding:20px;background:#402b2c} @media(max-width:950px){main{padding:22px 18px}.layout{grid-template-columns:1fr}.side{display:grid;grid-template-columns:1fr 1fr;gap:16px}.side .section{margin:0}.metrics{gap:8px}.metric{padding:14px}.metric strong{font-size:22px}}@media(max-width:600px){.masthead{align-items:flex-start;flex-direction:column;gap:12px}.metrics{grid-template-columns:1fr 1fr}.two-col,.side{grid-template-columns:1fr}.panel-head{align-items:flex-start}h1{font-size:25px}#clock{font-size:10px}.transport{gap:5px}.transport button,.transport select{padding:7px}footer{flex-direction:column}}
</style></head><body><main>
<header class="masthead"><div><div class="brand">BBP / Quantitative research</div><h1>From signal to decision</h1><p class="muted">An inspectable, offline research playback</p></div><div class="badge" id="evidence">SYNTHETIC CORRECTNESS ONLY</div></header>
<div class="notice"><strong>Engineering demonstration.</strong> Synthetic prices and engineered scenarios exercise causal timing, rejection rules and accounting. These results do not establish a trading edge or real-world profitability. Recovery research remains unselected; the real clock qualification gate is unresolved.</div>
<section class="metrics" aria-label="Complete report totals"><div class="metric"><h3>Data coverage</h3><strong id="metric-bars">—</strong><small id="coverage-sub">Invented raw M5 candles · full input</small></div><div class="metric"><h3>Accepted decisions</h3><strong id="metric-accept">—</strong><small>Engineered scenarios · full report</small></div><div class="metric"><h3>Rejected decisions</h3><strong id="metric-reject">—</strong><small>Risk controls · full report</small></div><div class="metric"><h3>Baseline case net P&amp;L</h3><strong id="metric-pnl">—</strong><small>Synthetic accounting units · full report</small></div></section>
<div class="layout"><section class="panel"><div class="panel-head"><div><h2>Engineered rule playback</h2><p class="tiny">Only information available at the selected bar is shown</p></div><span class="status"><i></i>Offline / read only</span></div><div class="chart-wrap"><svg id="chart" viewBox="0 0 900 330" role="img" aria-label="Synthetic OHLC price chart"></svg></div><div class="legend"><span><i class="dot"></i>Up candle</span><span><i class="dot red"></i>Down candle</span><span>▲ Accepted signal</span></div><div class="controls"><div class="transport"><button id="back" aria-label="Previous bar">←</button><button id="play">▶ Play</button><button id="next" aria-label="Next bar">→</button><select id="speed" aria-label="Playback speed"><option value="650">1×</option><option value="250">2×</option><option value="75">5×</option></select><span id="clock" aria-live="off">—</span></div><input id="scrubber" type="range" min="0" max="0" value="0" aria-label="Replay bar"><div class="timeline-label"><span id="start-time">—</span><span id="bar-count">—</span><span id="end-time">—</span></div></div></section>
<aside class="side"><section class="panel"><div class="panel-head"><h2>Coverage &amp; provenance</h2></div><div class="facts" id="coverage"></div></section><section class="panel section"><div class="panel-head"><h2>Independent accounting</h2></div><div class="facts" id="accounting"></div><p class="explain">Arithmetic reconciliation checks internal correctness. It is separate from evidence of market performance.</p></section></aside></div>
<section class="panel section"><div class="panel-head"><div><h2>Decision audit trail</h2><p class="tiny">Accepted, rejected and candidate decisions with their available evidence</p></div><label class="checkbox"><input id="future" type="checkbox">Include future decisions</label></div><p class="causal-note">Available at → decision → fill. The chart follows the replay clock; future records appear only when explicitly selected.</p><div class="table-scroll"><table><thead><tr><th>Decision / availability</th><th>Outcome</th><th>Reason</th><th>Order assumptions</th><th>Fill time</th></tr></thead><tbody id="decisions"></tbody></table><p class="empty" id="no-decisions">No decisions were available at this point.</p></div></section>
<div class="two-col"><section class="panel"><div class="panel-head"><h2>Model assumptions</h2></div><div class="facts" id="assumptions"></div></section><section class="panel"><div class="panel-head"><h2>Evidence boundaries</h2></div><p class="explain">This report separates engineered correctness scenarios from the strategy run on raw synthetic OHLC. Neither is historical private-data evidence or reproducible empirical evidence of an edge.</p><div class="facts" id="native-results"></div><p id="summary-note">Private paper trades and licensed raw data are excluded. Two historical trades cannot establish profitability.</p></section></div>
<footer><span>BBP Quantitative Research Demo · deterministic offline report</span><span>No broker connection · no order submission · no external resources</span></footer>
<noscript>Enable JavaScript for the local playback controls. The source report remains available through the command-line report output.</noscript>
<script id="report-data" type="application/json" nonce="__NONCE__">__REPORT__</script><script id="session-data" type="application/json" nonce="__NONCE__">__TOKEN__</script>
<script nonce="__NONCE__">
'use strict';
const report=JSON.parse(document.getElementById('report-data').textContent),bars=Array.isArray(report.bars)?report.bars:[],decisions=Array.isArray(report.decisions)?report.decisions:[];
const byId=id=>document.getElementById(id),txt=(id,value)=>{byId(id).textContent=String(value??'—')},num=value=>Number.isFinite(Number(value))?Number(value):null;
const label=value=>String(value).replaceAll('_',' ').replace(/\b\w/g,c=>c.toUpperCase());
const format=value=>{if(value===null||value===undefined)return '—';if(typeof value==='boolean')return value?'Pass':'Fail';if(typeof value==='number')return Number.isInteger(value)?value.toLocaleString('en-US'):value.toLocaleString('en-US',{maximumFractionDigits:5});if(typeof value==='object')return JSON.stringify(value);if(typeof value==='string'&&/^-?[0-9]+(\.[0-9]+)?(E[+-]?[0-9]+)?$/i.test(value)){const n=Number(value);if(Number.isFinite(n))return Math.abs(n)<1e-6?'0':n.toLocaleString('en-US',{maximumFractionDigits:5})}return String(value)};
const stamp=value=>{if(value===null||value===undefined||value==='')return '—';const n=typeof value==='number'?value:null,d=new Date(n===null?value:n*(n<1e12?1000:1));return Number.isNaN(d.getTime())?String(value):d.toISOString().replace('T',' ').replace('.000Z',' UTC')};
const epoch=value=>{if(typeof value==='number')return value<1e12?value*1000:value;const n=Date.parse(value);return Number.isFinite(n)?n:null};
function facts(id,values){const box=byId(id);box.replaceChildren();if(!values||!Object.keys(values).length){const p=document.createElement('p');p.className='tiny';p.textContent='No fields recorded.';box.append(p);return}for(const [key,value]of Object.entries(values)){const row=document.createElement('div'),name=document.createElement('span'),val=document.createElement('b');row.className='fact';name.textContent=label(key);val.textContent=format(value);row.append(name,val);box.append(row)}}
txt('evidence',String(report.evidence||'UNLABELLED EVIDENCE').replaceAll('_',' '));txt('metric-bars',(report.coverage?.observed_m5_bars??bars.length).toLocaleString('en-US'));txt('metric-accept',decisions.filter(d=>d.action==='ACCEPT').length);txt('metric-reject',decisions.filter(d=>d.action==='REJECT').length);txt('metric-pnl',format(report.results?.net_profit));
facts('coverage',{source:report.coverage?.source,raw_period:(report.coverage?.start_utc||'')+' to '+(report.coverage?.end_utc||''),missing_m5_bars:report.coverage?.missing_m5_bars,playback:'Separate engineered features, January 2026'});facts('assumptions',{entry:report.assumptions?.entry,spread:report.assumptions?.spread,intrabar:report.assumptions?.intrabar,cost_budget:'Commission 7 + slippage 12 + conservative financing per lot'});facts('accounting',{status:report.accounting?.status,initial_cash:report.accounting?.initial_cash,net:report.accounting?.independent_net,final_cash:report.accounting?.final_cash,difference:report.accounting?.difference});facts('native-results',report.results?.native_raw_ohlc);
const slider=byId('scrubber');slider.max=String(Math.max(0,bars.length-1));let index=Math.max(0,bars.length-1),timer=null;
txt('start-time',bars.length?stamp(bars[0].time):'No bars');txt('end-time',bars.length?stamp(bars[bars.length-1].time):'No bars');
function decisionEpoch(d){return epoch(d.decision_time??d.time??d.available_at)}
const barClock=b=>b?.available_at??b?.time;
function visible(d){const clock=epoch(barClock(bars[index])),at=decisionEpoch(d);return byId('future').checked||(clock!==null&&at!==null&&at<=clock)}
function cell(row,text,cls=''){const td=document.createElement('td');td.textContent=text;td.className=cls;row.append(td);return td}
function audit(){const body=byId('decisions');body.replaceChildren();const selected=decisions.filter(visible);byId('no-decisions').hidden=selected.length>0;for(const d of selected){const row=document.createElement('tr');const time=cell(row,stamp(d.decision_time??d.time));const small=document.createElement('div');small.className='tiny';small.textContent='Available '+stamp(d.available_at??d.time);time.append(small);const outcome=cell(row,'');const pill=document.createElement('span');pill.className='pill'+(d.action==='REJECT'?' reject':d.action==='ACCEPT'?'':' other');pill.textContent=String(d.action??'RECORDED');outcome.append(pill);const reason=cell(row,d.reason??'No reason recorded','reason');if(d.case){const scope=document.createElement('div');scope.className='tiny';scope.textContent=label(d.case);reason.append(scope)}const pieces=['side','entry','stop','target','quantity'].filter(k=>d[k]!==undefined).map(k=>label(k)+': '+format(d[k]));cell(row,pieces.join(' · ')||'—');const fill=epoch(d.fill_time),clock=epoch(barClock(bars[index]));cell(row,fill!==null&&clock!==null&&fill>clock&&!byId('future').checked?'Pending at replay time':stamp(d.fill_time));body.append(row)}}
function svg(tag,attrs,text){const el=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v]of Object.entries(attrs))el.setAttribute(k,String(v));if(text!==undefined)el.textContent=String(text);byId('chart').append(el);return el}
function chart(){const out=byId('chart');out.replaceChildren();const begin=Math.max(0,index-69),shown=bars.slice(begin,index+1);if(!shown.length){svg('text',{x:450,y:160,'text-anchor':'middle',fill:'#a5b5c5','font-size':16},'No bars recorded');return}let low=Math.min(...shown.map(b=>num(b.low)??0)),high=Math.max(...shown.map(b=>num(b.high)??0));const pad=(high-low||Math.abs(high)*.01||1)*.1;low-=pad;high+=pad;const left=22,right=798,top=19,bottom=286,y=v=>bottom-(v-low)/(high-low)*(bottom-top),step=(right-left)/Math.max(shown.length,16),x=i=>left+step*(i+.5);for(let k=0;k<=4;k++){const price=low+(high-low)*k/4,yp=y(price);svg('line',{x1:left,x2:right,y1:yp,y2:yp,stroke:'#263444','stroke-width':1});svg('text',{x:810,y:yp+4,fill:'#a5b5c5','font-size':11,'font-family':'system-ui'},price.toFixed(5))}shown.forEach((b,i)=>{const o=num(b.open),h=num(b.high),l=num(b.low),c=num(b.close);if([o,h,l,c].some(v=>v===null))return;const color=c>=o?'#82d9b1':'#f3a39a',xp=x(i),w=Math.max(2,Math.min(9,step*.62));svg('line',{x1:xp,x2:xp,y1:y(h),y2:y(l),stroke:color,'stroke-width':1.2});svg('rect',{x:xp-w/2,y:Math.min(y(o),y(c)),width:w,height:Math.max(1,Math.abs(y(o)-y(c))),fill:color});const t=epoch(b.time),prev=epoch(bars[begin+i-1]?.time);if(decisions.some(d=>d.action==='ACCEPT'&&decisionEpoch(d)!==null&&decisionEpoch(d)<=t&&(prev===null||decisionEpoch(d)>prev))){svg('text',{x:xp,y:Math.min(bottom-2,y(l)+17),fill:'#f3ce8a','text-anchor':'middle','font-size':12},'▲')}});const last=shown[shown.length-1];svg('line',{x1:left,x2:right,y1:y(num(last.close)??low),y2:y(num(last.close)??low),stroke:'#66d5e4','stroke-dasharray':'3 5',opacity:.55});svg('text',{x:left,y:316,fill:'#a5b5c5','font-size':11},stamp(shown[0].time));svg('text',{x:right,y:316,fill:'#a5b5c5','font-size':11,'text-anchor':'end'},stamp(last.time));out.setAttribute('aria-label','Synthetic OHLC chart through '+stamp(last.time)+', close '+format(last.close))}
function paint(){slider.value=String(index);txt('clock',bars.length?stamp(barClock(bars[index])):'No bars');txt('bar-count',bars.length?'Completed bar '+(index+1)+' / '+bars.length:'0 bars');byId('back').disabled=!bars.length||index===0;byId('next').disabled=!bars.length||index===bars.length-1;byId('play').disabled=!bars.length;slider.disabled=!bars.length;chart();audit()}
function pause(){clearInterval(timer);timer=null;txt('play','▶ Play')}
function play(){if(timer){pause();return}if(index===bars.length-1)index=0;txt('play','Ⅱ Pause');timer=setInterval(()=>{if(index>=bars.length-1){pause();return}index++;paint()},Number(byId('speed').value));paint()}
byId('play').addEventListener('click',play);byId('back').addEventListener('click',()=>{pause();index=Math.max(0,index-1);paint()});byId('next').addEventListener('click',()=>{pause();index=Math.min(bars.length-1,index+1);paint()});slider.addEventListener('input',()=>{pause();index=Number(slider.value);paint()});byId('future').addEventListener('change',audit);byId('speed').addEventListener('change',()=>{if(timer){pause();play()}});paint();
</script></main></body></html>'''
