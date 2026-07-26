#!/usr/bin/env python3
"""
GridrunnerAItrader — AI-powered trading assistant for Solana.
Single-file server with embedded dashboard.
"""

import json, os, time, threading, math
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import requests
except ImportError:
    requests = None

# ── Configuration ─────────────────────────────────────────────────────────────
PORT         = int(os.environ.get("PORT", 10000))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Global State ──────────────────────────────────────────────────────────────
state = {
    "running":       False,
    "strategy":      "ai_trader",
    "pair":          "SOL/USDC",
    "price":         0.0,
    "balance":       10000.0,
    "pnl":           0.0,
    "daily_loss":    0.0,
    "max_daily_loss": 500.0,
    "max_position":  1000.0,
    "emergency_stop": False,
    "paper_trading":  True,
    "trades":        [],
    "ai_analysis":   {},
    "opportunities": [],
    "price_history": [],
    "log":           [],
    "trades_list":   [],
}
_state_lock = threading.Lock()

def log(msg, level="INFO"):
    entry = f"[{time.strftime('%H:%M:%S')}] [{level}] {msg}"
    state["log"].append(entry)
    if len(state["log"]) > 100: state["log"] = state["log"][-100:]
    print(entry)

# ── Price Feed ────────────────────────────────────────────────────────────────
def get_price(pair="SOL/USDC"):
    if not requests: return 0.0
    try:
        token = pair.split("/")[0].lower()
        ids = {"sol":"solana","btc":"bitcoin","eth":"ethereum","bnb":"binancecoin"}
        cid = ids.get(token, token)
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
            params={"ids":cid,"vs_currencies":"usd"}, timeout=5)
        return float(r.json().get(cid,{}).get("usd",0))
    except Exception: return 0.0

# ── Technical Indicators ─────────────────────────────────────────────────────
def calc_rsi(prices, period=14):
    if len(prices) < period+1: return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i-1]
        gains.append(d if d>0 else 0); losses.append(abs(d) if d<0 else 0)
    avg_g = sum(gains[-period:])/period; avg_l = sum(losses[-period:])/period
    if avg_l == 0: return 100.0
    return 100 - (100/(1 + avg_g/avg_l))

def calc_macd(prices, fast=12, slow=26, sig=9):
    if len(prices) < slow+sig: return None,None,None
    def ema(d, p): 
        k=2/(p+1); r=[d[0]]
        for i in range(1,len(d)): r.append(d[i]*k+r[-1]*(1-k))
        return r
    ef=ema(prices,fast); es=ema(prices,slow)
    ml=[ef[i]-es[i] for i in range(len(prices))]
    sl=ema(ml[-sig*2:],sig)
    return ml[-1], sl[-1], ml[-1]-sl[-1]

# ── AI Analysis ──────────────────────────────────────────────────────────────
def analyze_market(pair):
    prices = [p["value"] for p in state["price_history"][-50:]]
    if len(prices) < 20: return {"error":"Not enough price data"}
    rsi = calc_rsi(prices, 14)
    macd, msig, mhist = calc_macd(prices)
    recent = prices[-10:]; older = prices[-20:-10]
    vol_trend = "increasing" if sum(abs(p-prices[-11]) for p in recent) > sum(abs(p-prices[-21]) for p in older) else "decreasing"
    sma = sum(prices[-5:])/5 if len(prices)>=5 else prices[-1]
    lma = sum(prices[-20:])/20 if len(prices)>=20 else prices[-1]
    momentum = "up" if sma>lma else "down"
    trend = "up" if len(prices)>=10 and prices[-1]>prices[-10] else "down"
    signal="hold"; conf=0.5
    if rsi is not None:
        if rsi<30: signal="buy"; conf=0.7+(30-rsi)/100
        elif rsi>70: signal="sell"; conf=0.7+(rsi-70)/100
    if mhist is not None:
        if mhist>0 and signal=="buy": conf=min(conf+0.1,0.95)
        elif mhist<0 and signal=="sell": conf=min(conf+0.1,0.95)
        elif mhist>0: signal="buy"; conf=0.55
        elif mhist<0: signal="sell"; conf=0.55
    return {"rsi":round(rsi,2) if rsi else None,"macd":round(macd,4) if macd else None,
        "macd_signal":round(msig,4) if msig else None,"volume":vol_trend,
        "momentum":momentum,"trend":trend,"signal":signal,
        "confidence":round(conf,2),"timestamp":time.strftime("%H:%M:%S")}

def ai_narrative(indicators):
    s = indicators.get("signal","hold"); c = indicators.get("confidence",0)
    if s=="buy" and c>0.65: return f"Strong buy — RSI oversold, MACD bullish. Confidence: {c*100:.0f}%"
    if s=="sell" and c>0.65: return f"Strong sell — RSI overbought, MACD bearish. Confidence: {c*100:.0f}%"
    if s=="buy": return f"Moderate buy — indicators lean bullish. Confidence: {c*100:.0f}%"
    if s=="sell": return f"Moderate sell — indicators lean bearish. Confidence: {c*100:.0f}%"
    return "Market neutral — no strong directional signal."

# ── Trade Execution ──────────────────────────────────────────────────────────
def execute_trade(signal, pair, amount):
    with _state_lock:
        if state["emergency_stop"]: log("Trade blocked — emergency stop","WARN"); return False
        if state["daily_loss"]>=state["max_daily_loss"]: log("Daily loss limit reached","WARN"); return False
        if amount>state["max_position"]: log(f"Size ${amount:.0f}>{state['max_position']:.0f}","WARN"); return False
        price = state["price"] or get_price(pair)
        if price<=0: log("No price data","WARN"); return False
        trade = {"time":time.strftime("%H:%M:%S"),"pair":pair,"action":signal,
            "price":round(price,4),"amount":round(amount,2),"pnl":None}
        if signal=="sell":
            buys = [t for t in state["trades"] if t["action"]=="buy" and t["pnl"] is None]
            if buys:
                b=buys[0]; pnl=(price-b["price"])*amount
                trade["pnl"]=round(pnl,2); b["pnl"]=0
                state["pnl"]+=pnl
                if pnl<0: state["daily_loss"]+=abs(pnl)
        state["trades"].append(trade)
        if len(state["trades"])>200: state["trades"]=state["trades"][-200:]
        state["trades_list"]=[{"time":t["time"],"action":t["action"],"price":t["price"],
            "amount":t["amount"],"pnl":t.get("pnl"),"pair":t["pair"]} for t in state["trades"][-50:]]
        log(f"TRADE: {signal.upper()} {pair} ${amount:.2f} @ ${price:.4f}"+(f" PnL:${trade['pnl']:.2f}" if trade.get("pnl") else ""))
        return True

# ── Opportunity Detection ────────────────────────────────────────────────────
def detect_opportunities(ind, pair):
    opps=[]; s=ind.get("signal","hold"); c=ind.get("confidence",0); rsi=ind.get("rsi")
    if s=="buy" and c>0.6: opps.append({"pair":pair,"direction":"BUY","confidence":c,
        "reason":f"RSI={rsi}, MACD bullish","suggested_size":round(state["max_position"]*0.3,2)})
    elif s=="sell" and c>0.6: opps.append({"pair":pair,"direction":"SELL","confidence":c,
        "reason":f"RSI={rsi}, MACD bearish","suggested_size":round(state["max_position"]*0.3,2)})
    if rsi and rsi<25: opps.append({"pair":pair,"direction":"BUY","confidence":0.8,
        "reason":f"Extreme oversold RSI={rsi}","suggested_size":round(state["max_position"]*0.5,2)})
    elif rsi and rsi>80: opps.append({"pair":pair,"direction":"SELL","confidence":0.8,
        "reason":f"Extreme overbought RSI={rsi}","suggested_size":round(state["max_position"]*0.5,2)})
    return opps

# ── Telegram ─────────────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not requests: return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id":TELEGRAM_CHAT_ID,"text":f"\U0001f916 GridrunnerAI\n{msg}","parse_mode":"HTML"}, timeout=5)
        return r.status_code==200
    except Exception as e: log(f"TG fail: {e}","WARN"); return False

# ── Background Loop ──────────────────────────────────────────────────────────
def ai_loop():
    while True:
        if state["running"]:
            try:
                pair=state["pair"]; price=get_price(pair)
                if price>0:
                    state["price"]=price
                    state["price_history"].append({"time":int(time.time()),"value":price})
                    if len(state["price_history"])>200: state["price_history"]=state["price_history"][-200:]
                ind=analyze_market(pair)
                state["ai_analysis"]={"indicators":ind,"narrative":ai_narrative(ind),"timestamp":time.strftime("%H:%M:%S")}
                state["opportunities"]=detect_opportunities(ind,pair)
                if ind.get("confidence",0)>0.75 and ind.get("signal") in ("buy","sell"):
                    amt=state["max_position"]*0.25
                    if execute_trade(ind["signal"],pair,amt):
                        send_telegram(f"<b>{ind['signal'].upper()}</b> {pair}\nPrice: ${price:.4f}\nAmount: ${amt:.2f}\nConfidence: {ind['confidence']*100:.0f}%")
            except Exception as e: log(f"AI loop: {e}","WARN")
        time.sleep(30 if state["running"] else 5)

# ── Dashboard HTML ───────────────────────────────────────────────────────────
DASHBOARD = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>GridrunnerAItrader</title>
<style>
:root{--bg:#0a0a1a;--card:#111122;--border:#1a1a2e;--text:#e0e0e0;--dim:#888;--accent:#14b8a6;--red:#ff6b6b;--green:#00ff9d;--yellow:#ffd43b}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:20px;max-width:1200px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:16px}
h1{font-size:22px;color:var(--accent);margin-bottom:4px}
.sub{font-size:13px;color:var(--dim);margin-bottom:20px}
.dot{width:8px;height:8px;border-radius:50%;background:#333;display:inline-block}
.dot.on{background:var(--accent);box-shadow:0 0 8px var(--accent)}
.btn{padding:8px 16px;border:1px solid var(--border);border-radius:6px;cursor:pointer;font-size:13px;color:var(--text);background:var(--card)}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.primary{background:var(--accent);color:#000;border-color:var(--accent)}
.btn.danger{border-color:var(--red);color:var(--red)}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:700px){.grid-2{grid-template-columns:1fr}}
.signal-buy{color:var(--green);font-weight:700}
.signal-sell{color:var(--red);font-weight:700}
.signal-hold{color:var(--yellow);font-weight:700}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid var(--border)}
th{color:var(--dim);font-weight:600}
.opp-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)}
.conf-bar{height:4px;border-radius:2px;margin-top:4px}
input,select{background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px 10px;border-radius:6px;font-size:13px;width:100%}
#paper-btn.paper-on{color:var(--yellow);border-color:var(--yellow);background:var(--yellow)18}
#paper-btn.paper-off{color:var(--red);border-color:var(--red);background:var(--red)18}
label{font-size:12px;color:var(--dim);display:block;margin-bottom:4px}
</style></head><body>
<div style="display:flex;justify-content:space-between;align-items:center">
<div><h1>\U0001f916 GridrunnerAItrader</h1><div class="sub"><span class="dot" id="dot"></span> <span id="status-text">Stopped</span></div><button class="btn" onclick="toggleConfig()" style="font-size:18px;padding:4px 8px" title="Toggle Config">\u2699</button></div>
<div><button class="btn primary" id="start-btn" onclick="startBot()">\u25b6 Start</button>
<button class="btn danger" id="stop-btn" onclick="stopBot()" style="display:none">\u23f9 Stop</button>
<button class="btn" id="paper-btn" onclick="togglePaper()" style="font-size:12px">\U0001f4cb PAPER</button></div>
</div>
<div class="grid-2">
<div class="card"><h3 style="color:var(--accent);margin-bottom:12px">\U0001f9e0 AI Analysis</h3>
<div id="ai-narrative" style="font-size:14px;margin-bottom:12px;line-height:1.5">Waiting for data...</div>
<div id="ai-indicators" style="font-size:12px;color:var(--dim)"></div></div>
<div class="card"><h3 style="color:var(--yellow);margin-bottom:12px">\u26a1 Trade Opportunities</h3>
<div id="opportunities"><div style="color:var(--dim);font-size:13px">No opportunities detected</div></div></div>
</div>
<div class="card"><h3 style="color:var(--dim);margin-bottom:12px">\U0001f4ca Trade History</h3>
<table><thead><tr><th>Time</th><th>Pair</th><th>Action</th><th>Price</th><th>Amount</th><th>P&amp;L</th></tr></thead>
<tbody id="trades-body"><tr><td colspan="6" style="color:var(--dim)">No trades yet</td></tr></tbody></table></div>
<div class="card" id="config-card" style="display:none"><h3 style="color:var(--dim);margin-bottom:12px;cursor:pointer" onclick="toggleConfig()">\u2699 Configuration <span id="gear-icon" style="font-size:14px">\u25b6</span></h3>
<div class="grid-2">
<div><label>Trading Pair</label><select id="cfg-pair" onchange="updateCfg()"><option>SOL/USDC</option><option>BTC/USDC</option><option>ETH/USDC</option></select></div>
<div><label>Max Position (USD)</label><input id="cfg-maxpos" type="number" value="1000" onchange="updateCfg()"></div>
<div><label>Max Daily Loss (USD)</label><input id="cfg-maxloss" type="number" value="500" onchange="updateCfg()"></div>
<div><label>Telegram Token</label><input id="cfg-tg-token" type="text" placeholder="optional" onchange="updateCfg()"></div>
<div><label>Telegram Chat ID</label><input id="cfg-tg-chat" type="text" placeholder="optional" onchange="updateCfg()"></div>
</div><button class="btn" onclick="saveConfig()" style="margin-top:12px">\U0001f4be Save Config</button></div>
<script>
var API_SECRET="{API_SECRET}";
function apiFetch(u,o){o=o||{};o.headers=o.headers||{};if(API_SECRET)o.headers["X-API-Secret"]=API_SECRET;return fetch(u,o)}
function refresh(){apiFetch("/state").then(function(r){return r.json()}).then(function(d){
var on=d.running;
document.getElementById("dot").className="dot"+(on?" on":"");
document.getElementById("status-text").textContent=on?"Running \u2014 AI Trader on "+(d.pair||"SOL/USDC"):"Stopped";
document.getElementById("start-btn").style.display=on?"none":"inline-block";
document.getElementById("stop-btn").style.display=on?"inline-block":"none";
updatePaperBtn(d.paper_trading);
var ai=d.ai_analysis||{},ind=ai.indicators||{};
if(ind.signal){var cls="signal-"+ind.signal;
document.getElementById("ai-narrative").innerHTML="<span class=\""+cls+"\">"+ind.signal.toUpperCase()+"</span> "+(ai.narrative||"")+" <span style=\"color:var(--dim);font-size:11px\">Confidence: "+(ind.confidence*100).toFixed(0)+"%</span>";
document.getElementById("ai-indicators").innerHTML="RSI: "+(ind.rsi!=null?ind.rsi.toFixed(1):"--")+" | MACD: "+(ind.macd_signal!=null?ind.macd_signal.toFixed(4):"--")+" | Volume: "+(ind.volume||"--")+" | Trend: "+(ind.trend||"--")}
else{document.getElementById("ai-narrative").textContent="Waiting for market data...";document.getElementById("ai-indicators").textContent=""}
var opps=d.opportunities||[];
if(opps.length){var h="";opps.forEach(function(o){var c=o.direction==="BUY"?"var(--green)":"var(--red)";
h+="<div class=\"opp-row\"><div><strong style=\"color:"+c+"\">"+o.direction+"</strong> "+o.pair+"<div style=\"font-size:11px;color:var(--dim)\">"+o.reason+"</div></div><div style=\"text-align:right\"><div>$"+o.suggested_size.toFixed(2)+"</div><div class=\"conf-bar\" style=\"width:"+(o.confidence*100)+"px;background:"+c+"\"></div><div style=\"font-size:10px;color:var(--dim)\">"+(o.confidence*100).toFixed(0)+"%</div></div></div>"});
document.getElementById("opportunities").innerHTML=h}
else document.getElementById("opportunities").innerHTML="<div style=\"color:var(--dim);font-size:13px\">No opportunities detected</div>";
var trades=d.trades_list||[];
if(trades.length){var th="";trades.slice().reverse().forEach(function(t){var cls=t.action==="buy"?"signal-buy":"signal-sell";
th+="<tr><td>"+t.time+"</td><td>"+(t.pair||"--")+"</td><td class=\""+cls+"\">"+t.action.toUpperCase()+"</td><td>$"+(t.price||0).toFixed(4)+"</td><td>$"+(t.amount||0).toFixed(2)+"</td><td style=\"color:"+(t.pnl>0?"var(--green)":t.pnl<0?"var(--red)":"var(--dim)")+"\">"+(t.pnl!=null?"$"+t.pnl.toFixed(2):"--")+"</td></tr>"});
document.getElementById("trades-body").innerHTML=th}
}).catch(function(e){console.log("Refresh:",e)})}
function startBot(){apiFetch("/start",{method:"POST"}).then(function(){refresh()})}
function stopBot(){apiFetch("/stop",{method:"POST"}).then(function(){refresh()})}
function updateCfg(){}
function saveConfig(){var c={pair:document.getElementById("cfg-pair").value,max_pos:parseFloat(document.getElementById("cfg-maxpos").value)||1000,max_daily_loss:parseFloat(document.getElementById("cfg-maxloss").value)||500};
apiFetch("/config",{method:"POST",body:JSON.stringify(c)}).then(function(){alert("Config saved")})}
function togglePaper(){apiFetch("/toggle_paper",{method:"POST"}).then(function(r){return r.json()}).then(function(d){
var btn=document.getElementById("paper-btn");
if(d.paper_trading){btn.textContent="\U0001f4cb PAPER";btn.className="btn paper-on"}
else{btn.textContent="\U0001f534 LIVE";btn.className="btn paper-off"}})}
function toggleConfig(){var c=document.getElementById("config-card");c.style.display=c.style.display==="none"?"block":"none"}
function updatePaperBtn(pt){var b=document.getElementById("paper-btn");b.textContent=pt?"\U0001f4cb PAPER":"\U0001f534 LIVE";b.className="btn "+(pt?"paper-on":"paper-off")}
setInterval(refresh,3000);refresh();
</script></body></html>"""

# ── HTTP Server ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    API_SECRET = os.environ.get("API_SECRET","")
    def log_message(self, format, *args): pass
    def _check_auth(self):
        if self.API_SECRET and self.headers.get("X-API-Secret","")!=self.API_SECRET:
            return False
        return True
    def respond(self, code, ct, body):
        self.send_response(code); self.send_header("Content-Type",ct)
        self.send_header("Access-Control-Allow-Origin","*"); self.end_headers()
        self.wfile.write(body if isinstance(body,bytes) else body.encode())
    def do_GET(self):
        p = urlparse(self.path).path
        if not self._check_auth():
            self.respond(401,"text/plain",b"Unauthorized")
            return
        if p in ("/","/dashboard"):
            self.respond(200,"text/html; charset=utf-8",DASHBOARD.replace("{API_SECRET}",self.API_SECRET).encode())
        elif p=="/state":
            with _state_lock:
                self.respond(200,"application/json",json.dumps({
                    "running":state["running"],"strategy":state["strategy"],"pair":state["pair"],
                    "price":state["price"],"balance":state["balance"],"pnl":state["pnl"],
                    "daily_loss":state["daily_loss"],"ai_analysis":state["ai_analysis"],
                    "opportunities":state["opportunities"],"trades_list":state["trades_list"],
                    "max_position":state["max_position"],"max_daily_loss":state["max_daily_loss"],
                    "emergency_stop":state["emergency_stop"],"paper_trading":state["paper_trading"]}).encode())
        elif p=="/debug":
            with _state_lock: self.respond(200,"application/json",json.dumps(state,default=str).encode())
        else: self.respond(404,"text/plain",b"Not found")
    def do_POST(self):
        p = urlparse(self.path).path
        cl = int(self.headers.get("Content-Length",0))
        body = self.rfile.read(cl) if cl>0 else b"{}"
        if not self._check_auth():
            self.respond(401,"text/plain",b"Unauthorized")
            return
        if p=="/start":
            if not state["running"]:
                state["running"]=True; log("Bot started — AI Trader on "+state["pair"])
                send_telegram("\U0001f680 <b>Bot Started</b>\nPair: "+state["pair"]+"\nStrategy: AI Trader")
            self.respond(200,"application/json",b'{"ok":true}')
        elif p=="/stop":
            state["running"]=False; log("Bot stopped"); send_telegram("\u23f9 <b>Bot Stopped</b>")
            self.respond(200,"application/json",b'{"ok":true}')
        elif p=="/config":
            try:
                data=json.loads(body)
                with _state_lock:
                    if "pair" in data: state["pair"]=data["pair"]
                    if "max_pos" in data: state["max_position"]=float(data["max_pos"])
                    if "max_daily_loss" in data: state["max_daily_loss"]=float(data["max_daily_loss"])
                log(f"Config updated: {data}"); self.respond(200,"application/json",b'{"ok":true}')
            except Exception as e: self.respond(400,"application/json",json.dumps({"error":str(e)}).encode())
        elif p=="/trade":
            try:
                data=json.loads(body)
                ok=execute_trade(data.get("signal","buy"),data.get("pair",state["pair"]),float(data.get("amount",100)))
                self.respond(200,"application/json",json.dumps({"ok":ok}).encode())
            except Exception as e: self.respond(400,"application/json",json.dumps({"error":str(e)}).encode())
        elif p=="/toggle_paper":
            with _state_lock:
                state["paper_trading"] = not state.get("paper_trading", True)
            self.respond(200,"application/json",json.dumps({"paper_trading":state["paper_trading"]}).encode())
        elif p=="/emergency_stop":
            state["emergency_stop"]=True; state["running"]=False
            log("EMERGENCY STOP","WARN"); send_telegram("\U0001f6d1 <b>EMERGENCY STOP</b> activated")
            self.respond(200,"application/json",b'{"ok":true}')
        else: self.respond(404,"text/plain",b"Not found")

# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n  GridrunnerAItrader — http://0.0.0.0:{PORT}")
    print(f"  AI Trading Bot | Dashboard + API\n")
    threading.Thread(target=ai_loop, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try: server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down..."); server.shutdown()
