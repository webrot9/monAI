"""monAI Dashboard — real-time web UI for monitoring the autonomous agent.

Single-page dashboard served over HTTP with Server-Sent Events (SSE)
for live updates. Zero external dependencies — uses Python's built-in
asyncio HTTP server.

Features:
- Real-time financial overview (balance, revenue, expenses, P&L)
- Strategy performance with ROI indicators
- Active accounts and provisioning status
- Agent activity log (live-streaming)
- Reinvestment engine status
- CAPTCHA/email verification stats
- Alert system for critical events

Usage:
    monai dashboard              # Start on default port 8421
    monai dashboard --port 9000  # Custom port
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from monai.business.commercialista import Commercialista
from monai.business.finance import Finance
from monai.business.risk import RiskManager
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import get_cost_tracker

logger = logging.getLogger(__name__)

# SSE clients waiting for updates
_sse_clients: list[asyncio.Queue] = []


class DashboardServer:
    """Async HTTP server serving the monAI dashboard."""

    def __init__(self, config: Config, db: Database, host: str = "0.0.0.0",
                 port: int = 8421):
        self.config = config
        self.db = db
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None
        self._push_task: asyncio.Task | None = None

        # Business modules
        self.commercialista = Commercialista(config, db)
        self.finance = Finance(db)
        self.risk = RiskManager(config, db)

    async def start(self):
        """Start the dashboard server."""
        self._server = await asyncio.start_server(
            self._handle_connection, self.host, self.port)
        self._push_task = asyncio.create_task(self._push_updates())
        logger.info(f"Dashboard running at http://{self.host}:{self.port}")

    async def stop(self):
        if self._push_task:
            self._push_task.cancel()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _push_updates(self):
        """Push updates to all SSE clients every 5 seconds."""
        while True:
            try:
                await asyncio.sleep(5)
                if _sse_clients:
                    data = self._collect_data()
                    event = f"data: {json.dumps(data)}\n\n"
                    dead = []
                    for q in _sse_clients:
                        try:
                            q.put_nowait(event)
                        except asyncio.QueueFull:
                            dead.append(q)
                    for q in dead:
                        _sse_clients.remove(q)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Dashboard push error: {e}")

    async def _handle_connection(self, reader: asyncio.StreamReader,
                                 writer: asyncio.StreamWriter):
        """Handle an incoming HTTP connection."""
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not request_line:
                writer.close()
                return

            request_str = request_line.decode("utf-8", errors="ignore").strip()
            parts = request_str.split(" ")
            if len(parts) < 2:
                writer.close()
                return

            method, path = parts[0], parts[1]

            # Read headers
            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                line_str = line.decode("utf-8", errors="ignore").strip()
                if not line_str:
                    break
                if ":" in line_str:
                    key, val = line_str.split(":", 1)
                    headers[key.strip().lower()] = val.strip()

            parsed = urlparse(path)
            route = parsed.path

            if route == "/" or route == "/dashboard":
                await self._serve_html(writer)
            elif route == "/api/data":
                await self._serve_json(writer, self._collect_data())
            elif route == "/api/logs":
                params = parse_qs(parsed.query)
                limit = int(params.get("limit", ["50"])[0])
                await self._serve_json(writer, self._get_logs(limit))
            elif route == "/api/accounts":
                await self._serve_json(writer, self._get_accounts())
            elif route == "/api/reinvestment":
                await self._serve_json(writer, self._get_reinvestment())
            elif route == "/events":
                await self._serve_sse(writer)
            else:
                await self._send_response(writer, 404, "text/plain", b"Not Found")

        except Exception as e:
            logger.debug(f"Dashboard connection error: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _collect_data(self) -> dict[str, Any]:
        """Collect all dashboard data from business modules."""
        try:
            budget = self.commercialista.get_budget()
            health = self.risk.get_portfolio_health()
            daily = self.finance.get_daily_summary()
            costs_agent = self.commercialista.get_cost_by_agent()
            costs_model = self.commercialista.get_cost_by_model()
            daily_costs = self.commercialista.get_daily_costs(days=7)
            reinvestment = self.commercialista.compute_reinvestment()

            # CAPTCHA stats
            captcha_stats = {}
            try:
                rows = self.db.execute(
                    "SELECT captcha_type, COUNT(*) as total, SUM(success) as solved, "
                    "SUM(cost_usd) as cost FROM captcha_solves GROUP BY captcha_type"
                )
                captcha_stats = {r["captcha_type"]: dict(r) for r in rows}
            except Exception:
                pass  # Table may not exist yet

            # Email verification stats
            email_stats = {}
            try:
                rows = self.db.execute(
                    "SELECT status, COUNT(*) as count FROM email_verifications GROUP BY status"
                )
                email_stats = {r["status"]: r["count"] for r in rows}
            except Exception:
                pass

            # Active strategies
            strategies = self.db.execute(
                "SELECT id, name, category, status, allocated_budget FROM strategies "
                "ORDER BY status, name"
            )
            strategy_list = []
            for s in strategies:
                s_dict = dict(s)
                roi = self.finance.get_roi(s_dict["id"], days=30)
                s_dict["roi_30d"] = round(roi, 2)
                pnl = self.finance.get_net_profit(s_dict["id"])
                s_dict["net_profit"] = round(pnl, 2)
                strategy_list.append(s_dict)

            # Recent agent actions
            recent_actions = self.db.execute(
                "SELECT agent_name, action, details, created_at FROM agent_log "
                "ORDER BY created_at DESC LIMIT 20"
            )

            return {
                "timestamp": datetime.now().isoformat(),
                "budget": budget,
                "health": health,
                "today": daily,
                "costs_by_agent": costs_agent[:10],  # Top 10
                "costs_by_model": costs_model,
                "daily_costs": daily_costs,
                "strategies": strategy_list,
                "reinvestment": reinvestment,
                "captcha": captcha_stats,
                "email_verification": email_stats,
                "recent_actions": [dict(r) for r in recent_actions],
            }
        except Exception as e:
            logger.error(f"Data collection error: {e}")
            return {"error": str(e), "timestamp": datetime.now().isoformat()}

    def _get_logs(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM agent_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def _get_accounts(self) -> list[dict]:
        try:
            rows = self.db.execute(
                "SELECT type, platform, identifier, status, created_at "
                "FROM identities WHERE status = 'active' ORDER BY created_at DESC"
            )
            return [dict(r) for r in rows]
        except Exception:
            return []

    def _get_reinvestment(self) -> dict[str, Any]:
        return self.commercialista.compute_reinvestment()

    # ── HTTP Helpers ─────────────────────────────────────────────

    async def _send_response(self, writer: asyncio.StreamWriter,
                             status: int, content_type: str, body: bytes):
        status_text = HTTPStatus(status).phrase
        header = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Cache-Control: no-cache\r\n"
            f"\r\n"
        )
        writer.write(header.encode() + body)
        await writer.drain()

    async def _serve_json(self, writer: asyncio.StreamWriter, data: Any):
        body = json.dumps(data, default=str).encode()
        await self._send_response(writer, 200, "application/json", body)

    async def _serve_html(self, writer: asyncio.StreamWriter):
        body = DASHBOARD_HTML.encode()
        await self._send_response(writer, 200, "text/html; charset=utf-8", body)

    async def _serve_sse(self, writer: asyncio.StreamWriter):
        """Serve Server-Sent Events stream."""
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: keep-alive\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "\r\n"
        )
        writer.write(header.encode())
        await writer.drain()

        # Send initial data
        data = self._collect_data()
        writer.write(f"data: {json.dumps(data, default=str)}\n\n".encode())
        await writer.drain()

        # Subscribe to updates
        q: asyncio.Queue = asyncio.Queue(maxsize=10)
        _sse_clients.append(q)

        try:
            while True:
                event = await q.get()
                writer.write(event.encode())
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if q in _sse_clients:
                _sse_clients.remove(q)


# ── Dashboard HTML ─────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>monAI Dashboard</title>
<style>
  :root {
    --bg: #0a0a0f;
    --card: #12121a;
    --border: #1e1e2e;
    --text: #e0e0e0;
    --dim: #888;
    --green: #4ade80;
    --red: #f87171;
    --yellow: #fbbf24;
    --blue: #60a5fa;
    --purple: #c084fc;
    --cyan: #22d3ee;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    padding: 16px;
    min-height: 100vh;
  }
  h1 {
    font-size: 20px;
    font-weight: 600;
    margin-bottom: 4px;
    color: var(--cyan);
  }
  .subtitle { color: var(--dim); font-size: 12px; margin-bottom: 16px; }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 12px;
    margin-bottom: 12px;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px;
  }
  .card h2 {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--dim);
    margin-bottom: 10px;
  }
  .stat {
    display: flex;
    justify-content: space-between;
    padding: 3px 0;
    font-size: 13px;
  }
  .stat .label { color: var(--dim); }
  .stat .value { font-weight: 600; }
  .positive { color: var(--green); }
  .negative { color: var(--red); }
  .warning { color: var(--yellow); }
  .neutral { color: var(--blue); }
  .big-number {
    font-size: 28px;
    font-weight: 700;
    line-height: 1.2;
  }
  .big-label {
    font-size: 11px;
    color: var(--dim);
    text-transform: uppercase;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  th {
    text-align: left;
    color: var(--dim);
    font-weight: 500;
    padding: 6px 8px;
    border-bottom: 1px solid var(--border);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  td {
    padding: 5px 8px;
    border-bottom: 1px solid #1a1a24;
  }
  tr:hover { background: #1a1a24; }
  .pill {
    display: inline-block;
    padding: 1px 8px;
    border-radius: 10px;
    font-size: 10px;
    font-weight: 600;
  }
  .pill-active { background: #064e3b; color: var(--green); }
  .pill-paused { background: #422006; color: var(--yellow); }
  .pill-stopped { background: #3b1219; color: var(--red); }
  .pill-boost { background: #064e3b; color: var(--green); }
  .pill-reduce { background: #3b1219; color: var(--red); }
  .pill-maintain { background: #1e293b; color: var(--blue); }
  .log-entry {
    font-size: 11px;
    padding: 4px 0;
    border-bottom: 1px solid #1a1a24;
    display: flex;
    gap: 10px;
  }
  .log-time { color: var(--dim); min-width: 60px; }
  .log-agent { color: var(--purple); min-width: 120px; }
  .log-action { color: var(--cyan); min-width: 100px; }
  .log-details { color: var(--text); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .status-dot {
    display: inline-block;
    width: 8px; height: 8px;
    border-radius: 50%;
    margin-right: 6px;
    animation: pulse 2s infinite;
  }
  .status-dot.live { background: var(--green); }
  .status-dot.dead { background: var(--red); animation: none; }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
  .wide { grid-column: 1 / -1; }
  .two-col {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }
  @media (max-width: 768px) {
    .two-col { grid-template-columns: 1fr; }
  }
  .bar {
    height: 6px;
    background: var(--border);
    border-radius: 3px;
    margin-top: 4px;
    overflow: hidden;
  }
  .bar-fill {
    height: 100%;
    border-radius: 3px;
    transition: width 0.5s ease;
  }
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
  }
  .header-right { display: flex; align-items: center; gap: 12px; font-size: 12px; }
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>monAI</h1>
    <div class="subtitle" id="subtitle">Connecting...</div>
  </div>
  <div class="header-right">
    <span><span class="status-dot live" id="live-dot"></span><span id="status-text">Live</span></span>
    <span id="clock" style="color:var(--dim)"></span>
  </div>
</div>

<!-- Top KPIs -->
<div class="grid" id="kpis">
  <div class="card">
    <h2>Balance</h2>
    <div class="big-number" id="balance">--</div>
    <div class="big-label">EUR remaining</div>
    <div class="bar"><div class="bar-fill" id="balance-bar" style="width:100%;background:var(--green)"></div></div>
  </div>
  <div class="card">
    <h2>Net Profit</h2>
    <div class="big-number" id="profit">--</div>
    <div class="big-label">EUR total</div>
  </div>
  <div class="card">
    <h2>Today</h2>
    <div class="big-number" id="today-net">--</div>
    <div class="big-label">EUR today</div>
  </div>
  <div class="card">
    <h2>Burn Rate</h2>
    <div class="big-number" id="burn">--</div>
    <div class="big-label" id="burn-label">EUR/day</div>
  </div>
</div>

<!-- Finance Details + Strategies -->
<div class="two-col">
  <div class="card">
    <h2>Financial Overview</h2>
    <div class="stat"><span class="label">Initial Capital</span><span class="value" id="initial">--</span></div>
    <div class="stat"><span class="label">Total Revenue</span><span class="value positive" id="revenue">--</span></div>
    <div class="stat"><span class="label">Total Expenses</span><span class="value negative" id="expenses">--</span></div>
    <div class="stat"><span class="label">Self-Sustaining</span><span class="value" id="sustaining">--</span></div>
    <div class="stat"><span class="label">Days Until Broke</span><span class="value" id="days-left">--</span></div>
    <div class="stat"><span class="label">Active Strategies</span><span class="value" id="active-strats">--</span></div>
    <div class="stat"><span class="label">Profitable / Losing</span><span class="value" id="pnl-ratio">--</span></div>
  </div>
  <div class="card">
    <h2>Reinvestment Engine</h2>
    <div id="reinvest-content">
      <div class="stat"><span class="label">Status</span><span class="value" id="reinvest-status">--</span></div>
      <div class="stat"><span class="label">Reinvest</span><span class="value neutral" id="reinvest-amount">--</span></div>
      <div class="stat"><span class="label">Reserve</span><span class="value" id="reserve-amount">--</span></div>
      <div class="stat"><span class="label">Creator Sweep</span><span class="value positive" id="creator-amount">--</span></div>
    </div>
    <h2 style="margin-top:14px">Autonomy Stats</h2>
    <div class="stat"><span class="label">CAPTCHAs Solved</span><span class="value" id="captcha-total">0</span></div>
    <div class="stat"><span class="label">CAPTCHA Cost</span><span class="value" id="captcha-cost">$0.00</span></div>
    <div class="stat"><span class="label">Emails Verified</span><span class="value" id="email-verified">0</span></div>
  </div>
</div>

<!-- Strategies Table -->
<div class="card wide" style="margin-top:12px">
  <h2>Strategies</h2>
  <table>
    <thead>
      <tr>
        <th>Name</th>
        <th>Category</th>
        <th>Status</th>
        <th>Budget</th>
        <th>Net P&L</th>
        <th>ROI 30d</th>
      </tr>
    </thead>
    <tbody id="strategies-body"></tbody>
  </table>
</div>

<!-- Costs + Activity Log -->
<div class="two-col" style="margin-top:12px">
  <div class="card">
    <h2>API Costs by Agent (Top 10)</h2>
    <table>
      <thead><tr><th>Agent</th><th>Calls</th><th>Cost</th></tr></thead>
      <tbody id="costs-body"></tbody>
    </table>
  </div>
  <div class="card">
    <h2>Recent Activity</h2>
    <div id="activity-log" style="max-height:320px;overflow-y:auto"></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const eur = v => typeof v === 'number' ? '€' + v.toFixed(2) : '--';
const usd = v => typeof v === 'number' ? '$' + v.toFixed(4) : '--';

function update(d) {
  if (d.error) {
    $('subtitle').textContent = 'Error: ' + d.error;
    return;
  }

  const b = d.budget || {};
  const h = d.health || {};
  const t = d.today || {};
  const r = d.reinvestment || {};

  // KPIs
  $('balance').textContent = eur(b.balance);
  $('balance').className = 'big-number ' + (b.balance > 0 ? 'positive' : 'negative');
  $('profit').textContent = eur(b.net_profit);
  $('profit').className = 'big-number ' + (b.net_profit >= 0 ? 'positive' : 'negative');
  $('today-net').textContent = eur(t.net);
  $('today-net').className = 'big-number ' + ((t.net || 0) >= 0 ? 'positive' : 'negative');
  $('burn').textContent = eur(b.burn_rate_daily);
  $('burn-label').textContent = b.days_until_broke ? b.days_until_broke + ' days left' : 'EUR/day';

  // Balance bar
  const pct = b.initial > 0 ? Math.max(0, (b.balance / b.initial) * 100) : 100;
  const barColor = pct > 50 ? 'var(--green)' : pct > 20 ? 'var(--yellow)' : 'var(--red)';
  $('balance-bar').style.width = pct + '%';
  $('balance-bar').style.background = barColor;

  // Finance details
  $('initial').textContent = eur(b.initial);
  $('revenue').textContent = eur(b.revenue);
  $('expenses').textContent = eur(b.expenses);
  $('sustaining').textContent = b.self_sustaining ? 'YES' : 'NO';
  $('sustaining').className = 'value ' + (b.self_sustaining ? 'positive' : 'warning');
  $('days-left').textContent = b.days_until_broke != null ? b.days_until_broke + ' days' : '∞';
  $('active-strats').textContent = h.active_strategies || 0;
  $('pnl-ratio').innerHTML = '<span class="positive">' + (h.profitable_strategies || 0) +
    '</span> / <span class="negative">' + (h.losing_strategies || 0) + '</span>';

  // Reinvestment
  $('reinvest-status').textContent = r.status || 'N/A';
  $('reinvest-status').className = 'value ' + (r.status === 'ready' ? 'positive' :
    r.status === 'disabled' ? 'negative' : 'warning');
  $('reinvest-amount').textContent = eur(r.reinvest);
  $('reserve-amount').textContent = eur(r.reserve);
  $('creator-amount').textContent = eur(r.creator_sweep);

  // CAPTCHA / Email stats
  const captcha = d.captcha || {};
  let cTotal = 0, cCost = 0;
  for (const k in captcha) { cTotal += captcha[k].total || 0; cCost += captcha[k].cost || 0; }
  $('captcha-total').textContent = cTotal;
  $('captcha-cost').textContent = '$' + cCost.toFixed(4);
  const ev = d.email_verification || {};
  $('email-verified').textContent = ev.found || 0;

  // Strategies table
  const strats = d.strategies || [];
  $('strategies-body').innerHTML = strats.map(s => {
    const statusCls = s.status === 'active' ? 'pill-active' :
      s.status === 'paused' ? 'pill-paused' : 'pill-stopped';
    const roiCls = s.roi_30d > 1 ? 'positive' : s.roi_30d > 0 ? 'warning' : 'negative';
    const netCls = s.net_profit >= 0 ? 'positive' : 'negative';
    return '<tr>' +
      '<td>' + s.name + '</td>' +
      '<td style="color:var(--dim)">' + s.category + '</td>' +
      '<td><span class="pill ' + statusCls + '">' + s.status + '</span></td>' +
      '<td>' + eur(s.allocated_budget) + '</td>' +
      '<td class="' + netCls + '">' + eur(s.net_profit) + '</td>' +
      '<td class="' + roiCls + '">' + (s.roi_30d || 0).toFixed(2) + 'x</td>' +
      '</tr>';
  }).join('');

  // Costs table
  const costs = d.costs_by_agent || [];
  $('costs-body').innerHTML = costs.map(c =>
    '<tr><td>' + c.agent_name + '</td>' +
    '<td>' + (c.calls || 0) + '</td>' +
    '<td>' + eur(c.total_cost) + '</td></tr>'
  ).join('');

  // Activity log
  const actions = d.recent_actions || [];
  $('activity-log').innerHTML = actions.map(a => {
    const t = (a.created_at || '').split('T')[1] || '';
    const time = t.substring(0, 8);
    return '<div class="log-entry">' +
      '<span class="log-time">' + time + '</span>' +
      '<span class="log-agent">' + (a.agent_name || '') + '</span>' +
      '<span class="log-action">' + (a.action || '') + '</span>' +
      '<span class="log-details">' + (a.details || '').substring(0, 100) + '</span>' +
      '</div>';
  }).join('');

  // Subtitle
  $('subtitle').textContent = 'Last update: ' + new Date().toLocaleTimeString();
}

// Clock
setInterval(() => {
  $('clock').textContent = new Date().toLocaleTimeString();
}, 1000);

// SSE connection
let retries = 0;
function connect() {
  const es = new EventSource('/events');
  es.onmessage = e => {
    retries = 0;
    $('live-dot').className = 'status-dot live';
    $('status-text').textContent = 'Live';
    try { update(JSON.parse(e.data)); } catch(err) { console.error(err); }
  };
  es.onerror = () => {
    $('live-dot').className = 'status-dot dead';
    $('status-text').textContent = 'Reconnecting...';
    es.close();
    retries++;
    setTimeout(connect, Math.min(retries * 2000, 30000));
  };
}

// Initial fetch + SSE
fetch('/api/data').then(r => r.json()).then(update).catch(() => {});
connect();
</script>
</body>
</html>"""


async def run_dashboard(config: Config, port: int = 8421):
    """Run the dashboard server (blocking)."""
    db = Database()
    server = DashboardServer(config, db, port=port)
    await server.start()
    print(f"\n  monAI Dashboard: http://localhost:{port}")
    print(f"  Press Ctrl+C to stop\n")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await server.stop()
