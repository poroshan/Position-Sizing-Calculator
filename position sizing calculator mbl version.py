"""
Position Sizing Calculator - Mobile Dashboard v3.2
By Roshan Pokhrel
Supports: EURUSD, USDJPY, XAUUSD + NEPSE + High-Impact News + Session Panel
Run:  python app.py
Open: http://localhost:5000  (also works on phone via local network)
"""

from flask import Flask, render_template_string, request, jsonify
import math
import requests
from datetime import datetime, timezone, timedelta
import time as _time         # standard-library time — no Streamlit, no external cache lib

app = Flask(__name__)

# ─────────────────────────────────────────────────────────
# INSTRUMENTS
# ─────────────────────────────────────────────────────────
INSTRUMENTS = {
    "EURUSD": {"pip_size": 0.0001, "pip_value": 10.0,  "lot_unit": 100000, "min_lot": 0.01, "lot_step": 0.01, "swap_long": -4.5,  "swap_short":  1.2},
    "USDJPY": {"pip_size": 0.01,   "pip_value":  9.0,  "lot_unit": 100000, "min_lot": 0.01, "lot_step": 0.01, "swap_long":  1.8,  "swap_short": -3.2},
    # pip_value for Gold: pip_size 0.1 × 100 oz = $1.00 per pip per lot
    "XAUUSD": {"pip_size": 0.1,    "pip_value": 1.0,  "lot_unit": 100,    "min_lot": 0.01, "lot_step": 0.01, "swap_long": -12.0, "swap_short": -8.0},
}

NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# ─────────────────────────────────────────────────────────
# SIMPLE IN-PROCESS CACHE  (replaces @st.cache_data)
# Stores (result, expiry_timestamp). Thread-safe enough for
# a single-user local Flask server; add threading.Lock if
# you ever deploy to a multi-worker production server.
# ─────────────────────────────────────────────────────────
_news_cache = {"value": None, "expires": 0.0}   # TTL: 1 hour
_rate_cache = {"value": None, "expires": 0.0}   # TTL: 60 seconds

NEWS_TTL  = 86400   # 24 hour 
RATE_TTL  = 60     # 60 seconds


# ─────────────────────────────────────────────────────────
# NEWS FETCHER
# ─────────────────────────────────────────────────────────
def get_next_high_news():
    """
    Returns the next HIGH-impact news event (all currencies) in NPT.
    Result is cached for 1 hour to avoid hammering ForexFactory.
    Falls back to the cached value if the fetch fails mid-TTL.
    """
    now_ts = _time.monotonic()
    if _news_cache["value"] is not None and now_ts < _news_cache["expires"]:
        return _news_cache["value"]   # serve from cache

    try:
        r = requests.get(
              NEWS_URL,
              headers={
                  "User-Agent": "Mozilla/5.0",
                  "Accept": "application/json",
                  "Referer": "https://www.forexfactory.com/"
              },
              timeout=20
)
        r.raise_for_status()
        data = r.json()

        if not isinstance(data, list):
            raise ValueError("Unexpected news format")

        now_utc = datetime.now(timezone.utc)
        high = []

        # Only show news that moves EURUSD, USDJPY, or XAUUSD (Gold)
        ALLOWED_CURRENCIES = {"USD", "EUR", "JPY"}

        for n in data:
            if n.get("impact") != "High":
                continue
            # Skip currencies irrelevant to our instruments
            if str(n.get("country", "")).upper() not in ALLOWED_CURRENCIES:
                continue
            try:
                t = datetime.fromisoformat(str(n["date"]).replace("Z", "+00:00"))
                if t > now_utc:
                    mins = int((t - now_utc).total_seconds() / 60)
                    hrs  = mins // 60
                    rem  = mins % 60
                    # Format: "2 hr 15 min", "45 min", "1 hr 0 min"
                    if hrs > 0:
                        countdown = f"{hrs} hr {rem} min"
                    else:
                        countdown = f"{rem} min"
                    npt  = t.astimezone(timezone(timedelta(hours=5, minutes=45)))
                    high.append({
                        "currency": n["country"],
                        "title":    n["title"],
                        "forecast": n.get("forecast", "-"),
                        "previous": n.get("previous", "-"),
                        "minutes":  mins,
                        "countdown": countdown,
                        "date":     npt.strftime("%d %b %Y"),
                        "time":     npt.strftime("%I:%M %p"),
                        "timezone": "NPT (UTC+5:45)",
                    })
            except Exception:
                continue

        if not high:
            result = {"active": False}
        else:
            high.sort(key=lambda x: x["minutes"])
            nxt = high[0]
            nxt["lock"] = nxt["minutes"] <= 30
            result = nxt

    except requests.exceptions.HTTPError as e:
        # Handle 429 Too Many Requests
        if e.response is not None and e.response.status_code == 429:
            print("Rate limited — serving cached data")

            if _news_cache["value"] is not None:
                return _news_cache["value"]

            result = {
                "active": False,
                "error": "Rate limit reached (429)"
            }

        else:
            if _news_cache["value"] is not None:
                return _news_cache["value"]

            result = {
                "active": False,
                "error": f"HTTP error: {e}"
            }

    except Exception as e:
        if _news_cache["value"] is not None:
            return _news_cache["value"]

        result = {
            "active": False,
            "error": f"News fetch failed: {e}"
        }

    _news_cache["value"]   = result
    _news_cache["expires"] = now_ts + NEWS_TTL
    return result


# ─────────────────────────────────────────────────────────
# MARKET SESSION HELPER  (pure Python, no Streamlit)
# ─────────────────────────────────────────────────────────
def get_market_session():
    """
    Returns (sessions_list, overlap_text) in NPT.
    Each entry in sessions_list is a dict: {name, open, label}
    """
    npt_now      = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=45)))
    now_minutes  = npt_now.hour * 60 + npt_now.minute
    offset       = 5 * 60 + 45   # NPT offset in minutes

    if npt_now.weekday() >= 5:   # Saturday=5, Sunday=6
        sessions = [
            {"name": "Sydney",   "open": False, "label": "🔴 Sydney   CLOSED"},
            {"name": "Tokyo",    "open": False, "label": "🔴 Tokyo    CLOSED"},
            {"name": "London",   "open": False, "label": "🔴 London   CLOSED"},
            {"name": "New York", "open": False, "label": "🔴 New York CLOSED"},
        ]
        return sessions, ""

    # GMT open/close hours → convert to NPT minutes
    gmt_sessions = {
        "Sydney":   (21 * 60,  6 * 60),
        "Tokyo":    ( 0 * 60,  9 * 60),
        "London":   ( 8 * 60, 17 * 60),
        "New York": (13 * 60, 22 * 60),
    }

    sessions = []
    active_names = []

    for name, (gmt_s, gmt_e) in gmt_sessions.items():
        s_npt = (gmt_s + offset) % 1440
        e_npt = (gmt_e + offset) % 1440
        if s_npt < e_npt:
            is_open = s_npt <= now_minutes < e_npt
        else:
            is_open = now_minutes >= s_npt or now_minutes < e_npt

        if is_open:
            active_names.append(name)
            label = f"🟢 {name} OPEN"
        else:
            label = f"🔴 {name} CLOSED"

        sessions.append({"name": name, "open": is_open, "label": label})

    overlap = ""
    if "London" in active_names and "New York" in active_names:
        overlap = "London + New York"
    #elif "Sydney" in active_names and "Tokyo" in active_names:
        #overlap = "Sydney + Tokyo"

    return sessions, overlap


# ─────────────────────────────────────────────────────────
# CALCULATORS
# ─────────────────────────────────────────────────────────
def calc_forex(data):
    sym         = data["symbol"]
    inst        = INSTRUMENTS[sym]
    balance     = float(data["balance"])
    risk_pct    = float(data["risk_pct"])
    sl_pips     = float(data["sl_pips"])
    rr_ratio    = float(data.get("rr", 2) or 2)
    leverage    = float(data.get("leverage", 100) or 100)
    usdjpy_rate = float(data.get("usdjpy_rate", 160) or 160)
    swap_days   = int(data.get("swap_days", 0) or 0)
    trade_dir   = data.get("direction", "long")

    pip_val = inst["pip_value"]
    if sym == "USDJPY":
        pip_val = (inst["pip_size"] * inst["lot_unit"]) / usdjpy_rate
    if sl_pips <= 0:
        return {"error": "SL pips must be greater than 0"}

    risk_amount     = balance * risk_pct / 100.0
    lot_size        = risk_amount / (sl_pips * pip_val)
    lot_size        = max(inst["min_lot"], lot_size)
    lot_size        = math.floor(lot_size / inst["lot_step"]) * inst["lot_step"]
    lot_size        = round(lot_size, 2)
    tp_pips         = sl_pips * rr_ratio
    reward_amount   = lot_size * tp_pips * pip_val
    margin_required = (lot_size * inst["lot_unit"]) / leverage
    swap_rate       = inst["swap_long"] if trade_dir == "long" else inst["swap_short"]
    swap_night_usd  = swap_rate * pip_val * lot_size
    actual_risk     = lot_size * sl_pips * pip_val

    return {
        "lot_size":             round(lot_size, 2),
        "sl_pips":              round(sl_pips, 1),
        "tp_pips":              round(tp_pips, 1),
        "risk_amount":          round(actual_risk, 2),
        "risk_pct":             round((actual_risk / balance) * 100, 3),
        "reward_amount":        round(reward_amount, 2),
        "rr_ratio":             round(rr_ratio, 2),
        "margin_required":      round(margin_required, 2),
        "pip_value":            round(pip_val, 4),
        "swap_per_night_pips":  round(swap_rate, 2),
        "swap_per_night_usd":   round(swap_night_usd, 2),
        "swap_total_pips":      round(swap_rate * swap_days, 2),
        "swap_total_usd":       round(swap_night_usd * swap_days, 2),
        "swap_days":            swap_days,
    }


def calc_nepse(data):
    balance       = float(data["balance"])
    risk_pct      = float(data["risk_pct"])
    sl_pts        = float(data["sl_pips"])
    rr_ratio      = float(data.get("rr", 2) or 2)
    brokerage_pct = float(data.get("brokerage", 0.4) or 0.4)
    entry_price   = float(data.get("entry_price", 800) or 800)

    if sl_pts <= 0:
        return {"error": "SL must be greater than 0"}

    risk_amount    = balance * risk_pct / 100.0
    shares         = max(1, math.floor(risk_amount / sl_pts))
    investment     = shares * entry_price
    brokerage_buy  = investment * brokerage_pct / 100.0
    sebon_fee      = investment * 0.00015
    dp_fee         = 25.0
    total_fees     = brokerage_buy + sebon_fee + dp_fee
    tp_pts         = sl_pts * rr_ratio
    sell_val       = (entry_price + tp_pts) * shares
    brokerage_sell = sell_val * brokerage_pct / 100.0
    net_reward     = shares * tp_pts - total_fees - brokerage_sell
    actual_risk    = shares * sl_pts + total_fees

    return {
        "shares":        shares,
        "sl_points":     round(sl_pts, 2),
        "tp_points":     round(tp_pts, 2),
        "risk_amount":   round(actual_risk, 2),
        "risk_pct":      round((actual_risk / balance) * 100, 3),
        "reward_amount": round(net_reward, 2),
        "rr_ratio":      round(net_reward / actual_risk if actual_risk else 0, 2),
        "investment":    round(investment, 2),
        "brokerage_buy": round(brokerage_buy, 2),
        "sebon_fee":     round(sebon_fee, 2),
        "dp_fee":        dp_fee,
        "total_fees":    round(total_fees, 2),
    }


# ─────────────────────────────────────────────────────────
# HTML TEMPLATE
# ─────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0b0d13">
<title>Position Sizing | Roshan Pokhrel</title>
<style>
:root{
  --bg:#0b0d13;--surface:#12151e;--card:#171b27;--border:#1e2536;
  --accent:#3b82f6;--green:#10b981;--amber:#f59e0b;--red:#ef4444;
  --text:#e2e8f0;--muted:#64748b;
  --mono:'JetBrains Mono','Courier New',monospace;
  --sans:'Inter','Segoe UI',system-ui,sans-serif;
  --radius:14px;--touch:52px;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}
html{font-size:16px;}
body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;padding-bottom:env(safe-area-inset-bottom);}

/* ── HEADER ── */
.header{background:var(--surface);border-bottom:1px solid var(--border);padding:14px 16px 12px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:200;padding-top:calc(14px + env(safe-area-inset-top));}
.logo{font-family:var(--mono);font-weight:800;font-size:13px;color:var(--accent);letter-spacing:-0.3px;line-height:1;}
.logo span{color:var(--text);}
.logo-sub{font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:3px;letter-spacing:0.3px;}
.live-dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--green);margin-right:5px;animation:blink 1.5s infinite;vertical-align:middle;}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.author-badge{text-align:right;font-family:var(--mono);font-size:10px;color:var(--muted);line-height:1.5;}
.author-badge strong{color:var(--accent);font-size:11px;display:block;}

/* ── NEWS + SESSION SHELL ── */
.news-shell{display:flex;gap:12px;align-items:stretch;margin:14px 16px 0;flex-wrap:wrap;}
.news-card{flex:1.2;min-width:260px;background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px 16px;}
.news-card h3{font-family:var(--mono);font-size:12px;font-weight:700;color:var(--text);margin:0 0 8px;letter-spacing:0.3px;}
.news-body{font-family:var(--mono);font-size:13px;color:var(--muted);}
.news-currency{font-size:16px;color:var(--red);font-weight:700;}
.news-title{margin-top:6px;font-size:14px;color:var(--text);}
.news-row{margin-top:8px;color:var(--text);}
.news-tz{color:var(--muted);margin-top:4px;}
.news-countdown{margin-top:8px;color:var(--accent);font-weight:700;}
.news-stat{margin-top:4px;color:var(--text);}
.news-warn{margin-top:12px;color:var(--amber);font-weight:700;background:#1a1000;border:1px solid #2d1f00;border-radius:8px;padding:8px 10px;}

/* ── SESSION PANEL ── */
.session-panel{flex:0.8;min-width:200px;background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px 16px;}
.session-title{font-family:var(--mono);font-size:12px;font-weight:700;color:var(--text);margin-bottom:10px;}
.session-list{display:grid;gap:6px;}
.session-item{font-family:var(--mono);font-size:12px;padding:7px 10px;border-radius:8px;background:#0d1017;border:1px solid var(--border);}
.session-item.open{color:var(--green);border-color:rgba(16,185,129,.3);}
.session-item.closed{color:var(--muted);}
.session-overlap{margin-top:10px;padding:8px 10px;border-radius:8px;background:#1a1000;border:1px solid #2d1f00;color:var(--amber);font-family:var(--mono);font-size:12px;font-weight:700;}

/* ── TAB BAR ── */
.tabs{display:flex;overflow-x:auto;background:var(--surface);border-bottom:1px solid var(--border);scrollbar-width:none;-ms-overflow-style:none;padding:0 4px;margin-top:14px;}
.tabs::-webkit-scrollbar{display:none;}
.tab{flex:0 0 auto;padding:12px 20px;font-family:var(--mono);font-size:12px;font-weight:600;color:var(--muted);cursor:pointer;white-space:nowrap;border-bottom:2px solid transparent;transition:color .2s,border-color .2s;letter-spacing:0.5px;}
.tab.active{color:var(--accent);border-bottom-color:var(--accent);}
.tab.tab-gold.active{color:var(--amber);border-bottom-color:var(--amber);}
.tab.tab-np.active{color:var(--green);border-bottom-color:var(--green);}

/* ── PAGE ── */
.page{display:none;padding:16px;}
.page.active{display:block;}

/* ── SECTION LABEL ── */
.sec-label{font-family:var(--mono);font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:1.2px;margin:18px 0 8px;display:flex;align-items:center;gap:8px;}
.sec-label::after{content:'';flex:1;height:1px;background:var(--border);}

/* ── FIELD ── */
.field{margin-bottom:14px;}
.field label{display:block;font-size:12px;font-weight:700;color:#60a5fa;text-transform:uppercase;letter-spacing:0.8px;margin-bottom:6px;text-shadow:0 0 8px rgba(96,165,250,.5);}
.field input,.field select{width:100%;height:var(--touch);background:#0d1017;border:1.5px solid var(--border);border-radius:10px;color:var(--text);font-family:var(--mono);font-size:16px;padding:0 14px;outline:none;transition:border-color .2s,box-shadow .2s;-webkit-appearance:none;appearance:none;}
.field input:focus,.field select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(59,130,246,.15);}
.field select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%2364748b' d='M6 8L1 3h10z'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 14px center;padding-right:36px;}
.hint{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:5px;padding-left:2px;}
.hint.live{color:var(--green);}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;}

/* ── RISK SYNC ── */
.risk-sync{background:#0d1017;border:1.5px solid var(--border);border-radius:10px;padding:12px 14px;margin-bottom:14px;}
.risk-sync-row{display:flex;justify-content:space-between;align-items:center;font-family:var(--mono);font-size:13px;}
.risk-sync-row + .risk-sync-row{margin-top:8px;padding-top:8px;border-top:1px solid var(--border);}
.risk-key{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px;}
.risk-val{color:var(--text);font-weight:700;font-size:14px;}
.risk-val.pos{color:var(--green);}

/* ── METER ── */
.meter{margin:16px 0;}
.meter-top{display:flex;justify-content:space-between;align-items:center;font-family:var(--mono);font-size:11px;color:var(--muted);margin-bottom:6px;}
.meter-pct{font-size:13px;font-weight:700;}
.meter-bg{background:#0d1017;border:1px solid var(--border);border-radius:6px;height:8px;overflow:hidden;}
.meter-fill{height:100%;border-radius:6px;transition:width .4s,background .3s;}

/* ── TOGGLE ── */
.toggle-row{display:flex;align-items:center;gap:12px;background:#0d1017;border:1.5px solid var(--border);border-radius:10px;padding:14px;margin-bottom:14px;}
.toggle-label{font-family:var(--mono);font-size:13px;font-weight:700;color:var(--text);flex:1;}
.toggle-label small{display:block;font-size:10px;color:var(--muted);font-weight:400;margin-top:2px;}
.switch{position:relative;width:52px;height:28px;flex-shrink:0;}
.switch input{opacity:0;width:0;height:0;}
.slider{position:absolute;inset:0;background:#374151;border-radius:28px;cursor:pointer;transition:background .3s;}
.slider:before{content:'';position:absolute;width:22px;height:22px;left:3px;top:3px;background:#fff;border-radius:50%;transition:transform .3s;}
.switch input:checked + .slider{background:var(--green);}
.switch input:checked + .slider:before{transform:translateX(24px);}
.swap-section{display:none;}
.swap-section.open{display:block;}

/* ── BUTTON ── */
.calc-btn{width:100%;height:56px;background:var(--accent);color:#fff;border:none;border-radius:12px;font-family:var(--mono);font-size:14px;font-weight:700;letter-spacing:1px;cursor:pointer;margin:16px 0 12px;transition:filter .2s,transform .1s;display:flex;align-items:center;justify-content:center;gap:8px;}
.calc-btn:active{transform:scale(.97);}
.calc-btn:hover{filter:brightness(1.12);}
.calc-btn.gold{background:var(--amber);}
.calc-btn.np{background:var(--green);}

/* ── ERROR ── */
.err-box{background:#1f0a0a;border:1.5px solid var(--red);border-radius:10px;padding:12px 14px;color:var(--red);font-family:var(--mono);font-size:12px;margin-bottom:12px;display:none;}
.err-box.show{display:block;}

/* ── RESULT CARD ── */
.result-card{background:#0d1017;border:1.5px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-bottom:20px;display:none;}
.result-card.show{display:block;}
.lot-hero{padding:24px 16px 20px;text-align:center;border-bottom:1px solid var(--border);}
.lot-number{font-family:var(--mono);font-size:52px;font-weight:900;letter-spacing:-3px;line-height:1;background:linear-gradient(135deg,var(--accent),#60a5fa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
.lot-number.gold-num{background:linear-gradient(135deg,var(--amber),#fcd34d);-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
.lot-number.np-num{background:linear-gradient(135deg,var(--green),#34d399);-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
.lot-unit{font-family:var(--mono);font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-top:6px;}
.res-grid{display:grid;grid-template-columns:1fr 1fr;}
.res-cell{padding:14px 16px;border-right:1px solid var(--border);border-bottom:1px solid var(--border);}
.res-cell:nth-child(even){border-right:none;}
.res-label{font-family:var(--mono);font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px;}
.res-val{font-family:var(--mono);font-size:16px;font-weight:700;}
.res-val.g{color:var(--green)}.res-val.b{color:var(--accent)}.res-val.a{color:var(--amber)}.res-val.r{color:var(--red)}.res-val.w{color:var(--text)}
.rr-badge{display:inline-block;padding:3px 12px;border-radius:20px;font-family:var(--mono);font-size:13px;font-weight:700;}
.rr-g{background:#071a10;color:var(--green);}.rr-a{background:#1a0e00;color:var(--amber);}.rr-r{background:#1a0505;color:var(--red);}

/* ── SWAP TABLE ── */
.swap-result{border-top:1px solid var(--border);}
.swap-result-head{background:var(--surface);padding:10px 16px;font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;display:grid;grid-template-columns:2fr 1fr 1fr;}
.swap-result-row{display:grid;grid-template-columns:2fr 1fr 1fr;padding:12px 16px;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:13px;}
.swap-result-row:last-child{border-bottom:none;}
.sc{color:var(--muted);}.sp{text-align:right;}.su{text-align:right;}
.neg{color:var(--red);}.pos{color:var(--green);}

/* ── FEES TABLE ── */
.fees-table{border-top:1px solid var(--border);}
.fees-head{background:var(--surface);padding:10px 16px;font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;}
.fees-row{display:flex;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:13px;}
.fees-row:last-child{border-bottom:none;}
.fees-row.total{background:var(--surface);font-weight:700;}
.fn{color:var(--muted);}.fv{color:var(--text);}

/* ── FOOTER ── */
.footer{text-align:center;padding:20px 16px 32px;border-top:1px solid var(--border);font-family:var(--mono);font-size:11px;color:var(--muted);line-height:2;}
.footer strong{color:var(--accent);font-size:13px;}
::-webkit-scrollbar{width:4px;}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px;}
</style>
</head>
<body>

<!-- HEADER -->
<div class="header">
  <div>
    <div class="logo">Position <span>Sizing</span></div>
    <div class="logo-sub"><span class="live-dot"></span>Forex &bull; Gold &bull; NEPSE</div>
  </div>
  <div class="author-badge">
    <strong>Roshan Pokhrel</strong>
    Design by RP &bull; v3.2
  </div>
</div>

<!-- NEWS + SESSION PANEL -->
<div class="news-shell">
  <div class="news-card">
    <h3>📌 Next High Impact News</h3>
    <div class="news-body" id="newsBox">Loading...</div>
  </div>
  <div class="session-panel">
    <div class="session-title">🕒 Forex Sessions (NPT)</div>
    <div class="session-list" id="sessionList">Loading...</div>
    <div id="overlapBox"></div>
  </div>
</div>

<!-- TAB BAR -->
<div class="tabs">
  <div class="tab active"   onclick="switchTab('eu')"  id="tab_eu">EUR/USD</div>
  <div class="tab"          onclick="switchTab('uj')"  id="tab_uj">USD/JPY</div>
  <div class="tab tab-gold" onclick="switchTab('xau')" id="tab_xau">XAU/USD</div>
  <div class="tab tab-np"   onclick="switchTab('np')"  id="tab_np">NEPSE</div>
</div>

<!-- ══════════════ EURUSD PAGE ══════════════ -->
<div class="page active" id="page_eu">
  <div class="sec-label">Account & Risk</div>
  <div class="field"><label>Account Balance (USD)</label><input type="number" id="eu_bal" value="5000" inputmode="decimal" step="100" min="1" oninput="syncRisk('eu')"></div>
  <div class="row2">
    <div class="field"><label>Risk %</label><input type="number" id="eu_rpct" value="1" inputmode="decimal" step="0.1" min="0.1" max="20" oninput="syncRisk('eu')"></div>
    <div class="field"><label>Risk $ (USD)</label><input type="number" id="eu_rusd" value="50" inputmode="decimal" step="1" min="1" oninput="syncRiskMoney('eu')"></div>
  </div>
  <div class="risk-sync"><div class="risk-sync-row"><span class="risk-key">Risk Amount</span><span class="risk-val pos" id="eu_sync_usd">$50.00</span></div><div class="risk-sync-row"><span class="risk-key">Risk Percent</span><span class="risk-val" id="eu_sync_pct">1.00% of balance</span></div></div>
  <div class="sec-label">Trade Setup</div>
  <div class="row3">
    <div class="field"><label>SL Pips</label><input type="number" id="eu_sl" value="10" inputmode="decimal" step="0.5" min="1"></div>
    <div class="field"><label>RR Ratio</label><input type="number" id="eu_rr" value="2" inputmode="decimal" step="0.5" min="0.5"><div class="hint">TP = SL x RR</div></div>
    <div class="field"><label>Leverage</label><select id="eu_lev"><option value="500">1:500</option><option value="200">1:200</option><option value="100" selected>1:100</option><option value="50">1:50</option><option value="30">1:30</option></select></div>
  </div>
  <div class="meter"><div class="meter-top"><span>Risk Level</span><span class="meter-pct" id="eu_mpct">1.00%</span></div><div class="meter-bg"><div class="meter-fill" id="eu_mbar" style="width:10%"></div></div></div>
  <div class="sec-label">Overnight Swap</div>
  <div class="toggle-row"><div class="toggle-label">Holding Overnight?<small>Toggle ON if trade passes midnight</small></div><label class="switch"><input type="checkbox" id="eu_overnight" onchange="toggleSwap('eu')"><span class="slider"></span></label></div>
  <div class="swap-section" id="eu_swap">
    <div class="row2"><div class="field"><label>Direction</label><select id="eu_dir"><option value="long">Long (Buy)</option><option value="short">Short (Sell)</option></select></div><div class="field"><label>Hold Days</label><input type="number" id="eu_days" value="1" inputmode="numeric" step="1" min="1"></div></div>
    <div class="row2"><div class="field"><label>Long Swap/night</label><input type="number" id="eu_swl" value="-4.5" inputmode="decimal" step="0.1"><div class="hint">pips per night</div></div><div class="field"><label>Short Swap/night</label><input type="number" id="eu_sws" value="1.2" inputmode="decimal" step="0.1"><div class="hint">pips per night</div></div></div>
  </div>
  <button class="calc-btn" onclick="calcFX('eu','EURUSD')">CALCULATE EUR/USD</button>
  <div class="err-box" id="eu_err"></div>
  <div class="result-card" id="eu_res">
    <div class="lot-hero"><div class="lot-number" id="eu_lots">0.00</div><div class="lot-unit">Standard Lots</div></div>
    <div class="res-grid">
      <div class="res-cell"><div class="res-label">Lot Size</div><div class="res-val b" id="eu_rlot">-</div></div>
      <div class="res-cell"><div class="res-label">Pip Value/Lot</div><div class="res-val w" id="eu_rpv">-</div></div>
      <div class="res-cell"><div class="res-label">SL Distance</div><div class="res-val r" id="eu_rsl">-</div></div>
      <div class="res-cell"><div class="res-label">TP Distance</div><div class="res-val g" id="eu_rtp">-</div></div>
      <div class="res-cell"><div class="res-label">Risk Amount</div><div class="res-val r" id="eu_rrisk">-</div></div>
      <div class="res-cell"><div class="res-label">Reward Amount</div><div class="res-val g" id="eu_rrew">-</div></div>
      <div class="res-cell"><div class="res-label">Risk / Reward</div><div class="res-val" id="eu_rrr">-</div></div>
      <div class="res-cell"><div class="res-label">Actual Risk %</div><div class="res-val w" id="eu_rrpct">-</div></div>
      <div class="res-cell" style="grid-column:1/-1"><div class="res-label">Margin Required</div><div class="res-val a" id="eu_rmarg">-</div></div>
    </div>
    <div class="swap-result" id="eu_swres" style="display:none">
      <div class="swap-result-head"><span>Swap</span><span style="text-align:right">PIPS</span><span style="text-align:right">USD</span></div>
      <div class="swap-result-row"><span class="sc">Per Night</span><span class="sp" id="eu_sw1p">-</span><span class="su" id="eu_sw1u">-</span></div>
      <div class="swap-result-row"><span class="sc" id="eu_swlbl">Total</span><span class="sp" id="eu_sw2p">-</span><span class="su" id="eu_sw2u">-</span></div>
    </div>
  </div>
</div>

<!-- ══════════════ USDJPY PAGE ══════════════ -->
<div class="page" id="page_uj">
  <div class="sec-label">Account & Risk</div>
  <div class="field"><label>Account Balance (USD)</label><input type="number" id="uj_bal" value="5000" inputmode="decimal" step="100" oninput="syncRisk('uj')"></div>
  <div class="row2"><div class="field"><label>Risk %</label><input type="number" id="uj_rpct" value="1" inputmode="decimal" step="0.1" oninput="syncRisk('uj')"></div><div class="field"><label>Risk $ (USD)</label><input type="number" id="uj_rusd" value="50" inputmode="decimal" step="1" oninput="syncRiskMoney('uj')"></div></div>
  <div class="risk-sync"><div class="risk-sync-row"><span class="risk-key">Risk Amount</span><span class="risk-val pos" id="uj_sync_usd">$50.00</span></div><div class="risk-sync-row"><span class="risk-key">Risk Percent</span><span class="risk-val" id="uj_sync_pct">1.00% of balance</span></div></div>
  <div class="sec-label">Trade Setup</div>
  <div class="row3"><div class="field"><label>SL Pips</label><input type="number" id="uj_sl" value="10" inputmode="decimal" step="0.5" min="1"></div><div class="field"><label>RR Ratio</label><input type="number" id="uj_rr" value="2" inputmode="decimal" step="0.5" min="0.5"></div><div class="field"><label>Leverage</label><select id="uj_lev"><option value="500">1:500</option><option value="200">1:200</option><option value="100" selected>1:100</option><option value="50">1:50</option><option value="30">1:30</option></select></div></div>
  <div class="field"><label>USD/JPY Live Rate</label><input type="number" id="uj_rate" value="160" readonly><div class="hint" id="uj_rate_hint">Loading live rate...</div></div>
  <div class="meter"><div class="meter-top"><span>Risk Level</span><span class="meter-pct" id="uj_mpct">1.00%</span></div><div class="meter-bg"><div class="meter-fill" id="uj_mbar" style="width:10%"></div></div></div>
  <div class="sec-label">Overnight Swap</div>
  <div class="toggle-row"><div class="toggle-label">Holding Overnight?<small>Toggle ON if trade passes midnight</small></div><label class="switch"><input type="checkbox" id="uj_overnight" onchange="toggleSwap('uj')"><span class="slider"></span></label></div>
  <div class="swap-section" id="uj_swap">
    <div class="row2"><div class="field"><label>Direction</label><select id="uj_dir"><option value="long">Long (Buy)</option><option value="short">Short (Sell)</option></select></div><div class="field"><label>Hold Days</label><input type="number" id="uj_days" value="1" inputmode="numeric" step="1" min="1"></div></div>
    <div class="row2"><div class="field"><label>Long Swap/night</label><input type="number" id="uj_swl" value="1.8" inputmode="decimal" step="0.1"><div class="hint">pips/night</div></div><div class="field"><label>Short Swap/night</label><input type="number" id="uj_sws" value="-3.2" inputmode="decimal" step="0.1"><div class="hint">pips/night</div></div></div>
  </div>
  <button class="calc-btn" onclick="calcFX('uj','USDJPY')">CALCULATE USD/JPY</button>
  <div class="err-box" id="uj_err"></div>
  <div class="result-card" id="uj_res">
    <div class="lot-hero"><div class="lot-number" id="uj_lots">0.00</div><div class="lot-unit">Standard Lots</div></div>
    <div class="res-grid">
      <div class="res-cell"><div class="res-label">Lot Size</div><div class="res-val b" id="uj_rlot">-</div></div><div class="res-cell"><div class="res-label">Pip Value/Lot</div><div class="res-val w" id="uj_rpv">-</div></div>
      <div class="res-cell"><div class="res-label">SL Distance</div><div class="res-val r" id="uj_rsl">-</div></div><div class="res-cell"><div class="res-label">TP Distance</div><div class="res-val g" id="uj_rtp">-</div></div>
      <div class="res-cell"><div class="res-label">Risk Amount</div><div class="res-val r" id="uj_rrisk">-</div></div><div class="res-cell"><div class="res-label">Reward Amount</div><div class="res-val g" id="uj_rrew">-</div></div>
      <div class="res-cell"><div class="res-label">Risk / Reward</div><div class="res-val" id="uj_rrr">-</div></div><div class="res-cell"><div class="res-label">Actual Risk %</div><div class="res-val w" id="uj_rrpct">-</div></div>
      <div class="res-cell" style="grid-column:1/-1"><div class="res-label">Margin Required</div><div class="res-val a" id="uj_rmarg">-</div></div>
    </div>
    <div class="swap-result" id="uj_swres" style="display:none">
      <div class="swap-result-head"><span>Swap</span><span style="text-align:right">PIPS</span><span style="text-align:right">USD</span></div>
      <div class="swap-result-row"><span class="sc">Per Night</span><span class="sp" id="uj_sw1p">-</span><span class="su" id="uj_sw1u">-</span></div>
      <div class="swap-result-row"><span class="sc" id="uj_swlbl">Total</span><span class="sp" id="uj_sw2p">-</span><span class="su" id="uj_sw2u">-</span></div>
    </div>
  </div>
</div>

<!-- ══════════════ XAUUSD PAGE ══════════════ -->
<div class="page" id="page_xau">
  <div class="sec-label">Account & Risk</div>
  <div class="field"><label>Account Balance (USD)</label><input type="number" id="xau_bal" value="5000" inputmode="decimal" step="100" oninput="syncRisk('xau')"></div>
  <div class="row2"><div class="field"><label>Risk %</label><input type="number" id="xau_rpct" value="0.5" inputmode="decimal" step="0.1" oninput="syncRisk('xau')"></div><div class="field"><label>Risk $ (USD)</label><input type="number" id="xau_rusd" value="25" inputmode="decimal" step="1" oninput="syncRiskMoney('xau')"></div></div>
  <div class="risk-sync"><div class="risk-sync-row"><span class="risk-key">Risk Amount</span><span class="risk-val pos" id="xau_sync_usd">$25.00</span></div><div class="risk-sync-row"><span class="risk-key">Risk Percent</span><span class="risk-val" id="xau_sync_pct">0.50% of balance</span></div></div>
  <div class="sec-label">Trade Setup</div>
  <div class="row3">
    <div class="field"><label>SL Pips</label><input type="number" id="xau_sl" value="830" inputmode="decimal" step="1" min="1"><div class="hint">pip=$0.10 | $10/pip/lot</div></div>
    <div class="field"><label>RR Ratio</label><input type="number" id="xau_rr" value="2" inputmode="decimal" step="0.5" min="0.5"></div>
    <div class="field"><label>Leverage</label><select id="xau_lev"><option value="500">1:500</option><option value="200">1:200</option><option value="100" selected>1:100</option><option value="50">1:50</option><option value="20">1:20</option></select></div>
  </div>
  <div class="meter"><div class="meter-top"><span>Risk Level</span><span class="meter-pct" id="xau_mpct">0.50%</span></div><div class="meter-bg"><div class="meter-fill" id="xau_mbar" style="width:5%"></div></div></div>
  <div class="sec-label">Overnight Swap</div>
  <div class="toggle-row"><div class="toggle-label">Holding Overnight?<small>Toggle ON if trade passes midnight</small></div><label class="switch"><input type="checkbox" id="xau_overnight" onchange="toggleSwap('xau')"><span class="slider"></span></label></div>
  <div class="swap-section" id="xau_swap">
    <div class="row2"><div class="field"><label>Direction</label><select id="xau_dir"><option value="long">Long (Buy)</option><option value="short">Short (Sell)</option></select></div><div class="field"><label>Hold Days</label><input type="number" id="xau_days" value="1" inputmode="numeric" step="1" min="1"></div></div>
    <div class="row2"><div class="field"><label>Long Swap/night</label><input type="number" id="xau_swl" value="-12.0" inputmode="decimal" step="0.1"><div class="hint">pips/night</div></div><div class="field"><label>Short Swap/night</label><input type="number" id="xau_sws" value="-8.0" inputmode="decimal" step="0.1"><div class="hint">pips/night</div></div></div>
  </div>
  <button class="calc-btn gold" onclick="calcFX('xau','XAUUSD')">CALCULATE XAU/USD GOLD</button>
  <div class="err-box" id="xau_err"></div>
  <div class="result-card" id="xau_res">
    <div class="lot-hero" style="background:linear-gradient(135deg,#150d00 0%,#17181e 100%)"><div class="lot-number gold-num" id="xau_lots">0.00</div><div class="lot-unit">Gold Lots (100 oz each)</div></div>
    <div class="res-grid">
      <div class="res-cell"><div class="res-label">Lot Size</div><div class="res-val a" id="xau_rlot">-</div></div><div class="res-cell"><div class="res-label">Pip Value/Lot</div><div class="res-val w" id="xau_rpv">-</div></div>
      <div class="res-cell"><div class="res-label">SL Distance</div><div class="res-val r" id="xau_rsl">-</div></div><div class="res-cell"><div class="res-label">TP Distance</div><div class="res-val g" id="xau_rtp">-</div></div>
      <div class="res-cell"><div class="res-label">Risk Amount</div><div class="res-val r" id="xau_rrisk">-</div></div><div class="res-cell"><div class="res-label">Reward Amount</div><div class="res-val g" id="xau_rrew">-</div></div>
      <div class="res-cell"><div class="res-label">Risk / Reward</div><div class="res-val" id="xau_rrr">-</div></div><div class="res-cell"><div class="res-label">Actual Risk %</div><div class="res-val w" id="xau_rrpct">-</div></div>
      <div class="res-cell" style="grid-column:1/-1"><div class="res-label">Margin Required</div><div class="res-val a" id="xau_rmarg">-</div></div>
    </div>
    <div class="swap-result" id="xau_swres" style="display:none">
      <div class="swap-result-head"><span>Swap</span><span style="text-align:right">PIPS</span><span style="text-align:right">USD</span></div>
      <div class="swap-result-row"><span class="sc">Per Night</span><span class="sp" id="xau_sw1p">-</span><span class="su" id="xau_sw1u">-</span></div>
      <div class="swap-result-row"><span class="sc" id="xau_swlbl">Total</span><span class="sp" id="xau_sw2p">-</span><span class="su" id="xau_sw2u">-</span></div>
    </div>
  </div>
</div>

<!-- ══════════════ NEPSE PAGE ══════════════ -->
<div class="page" id="page_np">
  <div class="sec-label">Capital & Risk</div>
  <div class="field"><label>Capital (NPR)</label><input type="number" id="np_bal" value="1000000" inputmode="decimal" step="10000" oninput="syncRisk('np')"></div>
  <div class="row2"><div class="field"><label>Risk %</label><input type="number" id="np_rpct" value="2" inputmode="decimal" step="0.1" oninput="syncRisk('np')"></div><div class="field"><label>Risk (NPR)</label><input type="number" id="np_rusd" value="20000" inputmode="decimal" step="100" oninput="syncRiskMoney('np')"></div></div>
  <div class="risk-sync"><div class="risk-sync-row"><span class="risk-key">Risk Amount</span><span class="risk-val pos" id="np_sync_usd">NPR 20,000</span></div><div class="risk-sync-row"><span class="risk-key">Risk Percent</span><span class="risk-val" id="np_sync_pct">2.00% of capital</span></div></div>
  <div class="sec-label">Trade Setup</div>
  <div class="row3">
    <div class="field"><label>Entry Price</label><input type="number" id="np_entry" value="800" inputmode="decimal" step="1" min="1"><div class="hint">NPR per share</div></div>
    <div class="field"><label>SL per Share</label><input type="number" id="np_sl" value="30" inputmode="decimal" step="0.5" min="1"><div class="hint">NPR loss/share</div></div>
    <div class="field"><label>RR Ratio</label><input type="number" id="np_rr" value="2" inputmode="decimal" step="0.5" min="0.5"><div class="hint">TP = SL x RR</div></div>
  </div>
  <div class="field"><label>Brokerage Rate</label><select id="np_brok"><option value="0.36">0.36% (up to NPR 50K)</option><option value="0.33">0.33% (50K–5L)</option><option value="0.31">0.31% (5L–20L)</option><option value="0.27">0.27% (20L–1CR)</option><option value="0.24">0.24% (above 1CR)</option><option value="0.4" selected>0.40% (Custom)</option></select></div>
  <div class="meter"><div class="meter-top"><span>Risk Level</span><span class="meter-pct" id="np_mpct">2.00%</span></div><div class="meter-bg"><div class="meter-fill" id="np_mbar" style="width:20%;background:var(--green)"></div></div></div>
  <button class="calc-btn np" onclick="calcNP()">CALCULATE NEPSE POSITION</button>
  <div class="err-box" id="np_err"></div>
  <div class="result-card" id="np_res">
    <div class="lot-hero" style="background:linear-gradient(135deg,#061409 0%,#17181e 100%)"><div class="lot-number np-num" id="np_shares">0</div><div class="lot-unit">Shares to Buy</div></div>
    <div class="res-grid">
      <div class="res-cell"><div class="res-label">SL per Share</div><div class="res-val r" id="np_rsl">-</div></div><div class="res-cell"><div class="res-label">TP per Share</div><div class="res-val g" id="np_rtp">-</div></div>
      <div class="res-cell"><div class="res-label">Risk Amount</div><div class="res-val r" id="np_rrisk">-</div></div><div class="res-cell"><div class="res-label">Net Reward</div><div class="res-val g" id="np_rrew">-</div></div>
      <div class="res-cell"><div class="res-label">Risk / Reward</div><div class="res-val" id="np_rrr">-</div></div><div class="res-cell"><div class="res-label">Actual Risk %</div><div class="res-val w" id="np_rrpct">-</div></div>
      <div class="res-cell" style="grid-column:1/-1"><div class="res-label">Investment Required</div><div class="res-val b" id="np_rinv">-</div></div>
    </div>
    <div class="fees-table">
      <div class="fees-head">Fee Breakdown (Buy Side)</div>
      <div class="fees-row"><span class="fn">Brokerage</span><span class="fv" id="np_fbrok">-</span></div>
      <div class="fees-row"><span class="fn">SEBON Fee (0.015%)</span><span class="fv" id="np_fsebon">-</span></div>
      <div class="fees-row"><span class="fn">DP Charge</span><span class="fv">NPR 25.00</span></div>
      <div class="fees-row total"><span class="fn">Total Buy Cost</span><span class="fv" id="np_ftotal">-</span></div>
    </div>
  </div>
</div>

<!-- FOOTER -->
<div class="footer">
  <strong>Design by Roshan Pokhrel</strong><br>
  Position Sizing Calculator v3.2 &bull; EUR/USD &bull; USD/JPY &bull; XAU/USD &bull; NEPSE<br>
  <span style="font-size:10px;color:#374151">Educational use only. Not financial advice.</span>
</div>

<script>
// ── TAB SWITCHER ──
function switchTab(p){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.page').forEach(pg=>pg.classList.remove('active'));
  document.getElementById('tab_'+p).classList.add('active');
  document.getElementById('page_'+p).classList.add('active');
  window.scrollTo(0,0);
}

// ── OVERNIGHT TOGGLE ──
function toggleSwap(p){
  document.getElementById(p+'_swap').classList.toggle('open', document.getElementById(p+'_overnight').checked);
}

// ── RISK SYNC ──
function syncRisk(p){
  const bal=parseFloat(gv(p+'_bal'))||0, pct=parseFloat(gv(p+'_rpct'))||0;
  const amt=bal*pct/100, isNP=p==='np', curr=isNP?'NPR ':'$';
  sv(p+'_rusd', amt.toFixed(2));
  sd(p+'_sync_usd', curr+fmt2(amt));
  sd(p+'_sync_pct', pct.toFixed(2)+'% of '+(isNP?'capital':'balance'));
  meter(p,pct);
}
function syncRiskMoney(p){
  const bal=parseFloat(gv(p+'_bal'))||1, amt=parseFloat(gv(p+'_rusd'))||0;
  const pct=(amt/bal)*100, isNP=p==='np', curr=isNP?'NPR ':'$';
  sv(p+'_rpct', pct.toFixed(3));
  sd(p+'_sync_usd', curr+fmt2(amt));
  sd(p+'_sync_pct', pct.toFixed(2)+'% of '+(isNP?'capital':'balance'));
  meter(p,pct);
}
function meter(p,pct){
  const bar=document.getElementById(p+'_mbar'), lbl=document.getElementById(p+'_mpct');
  bar.style.width=Math.min(pct*10,100)+'%';
  bar.style.background=pct<=1?'var(--green)':pct<=2?'var(--accent)':pct<=3?'var(--amber)':'var(--red)';
  lbl.textContent=pct.toFixed(2)+'%';
}

// ── HELPERS ──
function gv(id){return document.getElementById(id)?.value||'';}
function sv(id,v){const e=document.getElementById(id);if(e)e.value=v;}
function sd(id,v){const e=document.getElementById(id);if(e)e.textContent=v;}
function fmt2(n){return parseFloat(n).toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2});}
function fmtN(n,d=2){return parseFloat(n).toLocaleString('en',{minimumFractionDigits:d,maximumFractionDigits:d});}
function scls(n){return parseFloat(n)<0?'neg':'pos';}
function rrHtml(r){const v=parseFloat(r);if(v<=0)return '<span class="rr-badge rr-r">N/A</span>';const c=v>=2?'rr-g':v>=1?'rr-a':'rr-r';return `<span class="rr-badge ${c}">1 : ${v.toFixed(2)}</span>`;}
function showErr(p,m){const e=document.getElementById(p+'_err');e.textContent=m;e.classList.add('show');document.getElementById(p+'_res').classList.remove('show');}
function hideErr(p){document.getElementById(p+'_err').classList.remove('show');}

// ── FOREX CALC ──
async function calcFX(p,sym){
  hideErr(p);
  const overnight=document.getElementById(p+'_overnight')?.checked??false;
  const dir=gv(p+'_dir')||'long', days=parseInt(gv(p+'_days'))||1;
  const data={symbol:sym,balance:gv(p+'_bal'),risk_pct:gv(p+'_rpct'),sl_pips:gv(p+'_sl'),rr:gv(p+'_rr'),leverage:gv(p+'_lev'),direction:dir,swap_days:overnight?days:0,usdjpy_rate:p==='uj'?gv('uj_rate'):150,swap_long:overnight?gv(p+'_swl'):0,swap_short:overnight?gv(p+'_sws'):0};
  try{
    const r=await fetch('/calc_forex',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const d=await r.json();
    if(d.error){showErr(p,d.error);return;}
    document.getElementById(p+'_lots').textContent=fmtN(d.lot_size);
    sd(p+'_rlot',d.lot_size+' lots');sd(p+'_rpv','$'+fmt2(d.pip_value)+'/pip');
    sd(p+'_rsl',d.sl_pips+' pips');sd(p+'_rtp',d.tp_pips+' pips');
    sd(p+'_rrisk','-$'+fmt2(d.risk_amount));sd(p+'_rrew','+$'+fmt2(d.reward_amount));
    document.getElementById(p+'_rrr').innerHTML=rrHtml(d.rr_ratio);
    sd(p+'_rrpct',d.risk_pct+'%');sd(p+'_rmarg','$'+fmt2(d.margin_required));
    const swBox=document.getElementById(p+'_swres');
    if(overnight&&d.swap_days>0){
      swBox.style.display='block';
      const s1p=document.getElementById(p+'_sw1p'),s1u=document.getElementById(p+'_sw1u'),s2p=document.getElementById(p+'_sw2p'),s2u=document.getElementById(p+'_sw2u');
      s1p.textContent=d.swap_per_night_pips;s1p.className='sp '+scls(d.swap_per_night_pips);
      s1u.textContent='$'+fmt2(d.swap_per_night_usd);s1u.className='su '+scls(d.swap_per_night_usd);
      s2p.textContent=d.swap_total_pips;s2p.className='sp '+scls(d.swap_total_pips);
      s2u.textContent='$'+fmt2(d.swap_total_usd);s2u.className='su '+scls(d.swap_total_usd);
      sd(p+'_swlbl','Total ('+days+' day'+(days>1?'s':'')+')');
    }else{swBox.style.display='none';}
    document.getElementById(p+'_res').classList.add('show');
    document.getElementById(p+'_res').scrollIntoView({behavior:'smooth',block:'nearest'});
  }catch(e){showErr(p,'Error: '+e.message);}
}

// ── NEPSE CALC ──
async function calcNP(){
  hideErr('np');
  const data={balance:gv('np_bal'),risk_pct:gv('np_rpct'),sl_pips:gv('np_sl'),rr:gv('np_rr'),entry_price:gv('np_entry'),brokerage:gv('np_brok')};
  try{
    const r=await fetch('/calc_nepse',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const d=await r.json();
    if(d.error){showErr('np',d.error);return;}
    document.getElementById('np_shares').textContent=fmtN(d.shares,0);
    sd('np_rsl','NPR '+fmt2(d.sl_points));sd('np_rtp','NPR '+fmt2(d.tp_points));
    sd('np_rrisk','-NPR '+fmt2(d.risk_amount));sd('np_rrew','+NPR '+fmt2(d.reward_amount));
    document.getElementById('np_rrr').innerHTML=rrHtml(d.rr_ratio);
    sd('np_rrpct',d.risk_pct+'%');sd('np_rinv','NPR '+fmt2(d.investment));
    sd('np_fbrok','NPR '+fmt2(d.brokerage_buy));sd('np_fsebon','NPR '+fmt2(d.sebon_fee));sd('np_ftotal','NPR '+fmt2(d.total_fees));
    document.getElementById('np_res').classList.add('show');
    document.getElementById('np_res').scrollIntoView({behavior:'smooth',block:'nearest'});
  }catch(e){showErr('np','Error: '+e.message);}
}

// ── LIVE USDJPY RATE ──
async function loadRate(){
  try{
    const r=await fetch('/usdjpy');const d=await r.json();
    if(d.rate){sv('uj_rate',parseFloat(d.rate).toFixed(3));const h=document.getElementById('uj_rate_hint');if(h){h.textContent='Live rate loaded';h.className='hint live';}}
  }catch(e){console.warn('Rate fetch failed');}
}

// ── NEWS ──
function newsCardHtml(d){
  let warn='';
  if(d.lock) warn='<div class="news-warn">⚠ News in &lt;30 min — reduce risk to 0.5%</div>';
  return '<div class="news-currency">🔴 '+d.currency+'</div>'+
         '<div class="news-title">'+d.title+'</div>'+
         '<div class="news-row">📅 '+d.date+'</div>'+
         '<div class="news-row">🕒 '+d.time+'</div>'+
         '<div class="news-tz">'+d.timezone+'</div>'+
         '<div class="news-countdown">⏳ Starts in '+d.countdown+'</div>'+
         '<div class="news-stat">Forecast: '+d.forecast+'</div>'+
         '<div class="news-stat">Previous: '+d.previous+'</div>'+warn;
}
async function loadNews(){
  try{
    const r=await fetch('/news');
    const d=await r.json();

    if(d.error){
      document.getElementById('newsBox').innerHTML=
        '<span style="color:red">'+d.error+'</span>';
      return;
    }

    document.getElementById('newsBox').innerHTML=
      d.currency ? newsCardHtml(d)
      : 'No upcoming high-impact news.';
  }
  catch(e){
    document.getElementById('newsBox').innerHTML=
      'News fetch failed: '+e.message;
  }
}

// ── SESSIONS ──
async function loadSessions(){
  try{
    const r=await fetch('/sessions');const d=await r.json();
    const list=document.getElementById('sessionList');
    list.innerHTML=d.sessions.map(s=>`<div class="session-item ${s.open?'open':'closed'}">${s.label}</div>`).join('');
    const ob=document.getElementById('overlapBox');
    ob.innerHTML=d.overlap?`<div class="session-overlap">⚡ High Volatility · ${d.overlap}</div>`:'';
  }catch(e){}
}

// ── INIT ──
['eu','uj','xau'].forEach(p=>{document.getElementById(p+'_bal').addEventListener('input',()=>syncRisk(p));});
document.getElementById('np_bal').addEventListener('input',()=>syncRisk('np'));
syncRisk('eu');syncRisk('uj');syncRisk('xau');syncRisk('np');
loadRate();loadNews();loadSessions();
setInterval(loadRate,60000);setInterval(loadNews,3600000);setInterval(loadSessions,60000);
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/calc_forex", methods=["POST"])
def api_forex():
    data = request.get_json()
    sym  = data.get("symbol", "EURUSD").upper()
    inst = dict(INSTRUMENTS.get(sym, INSTRUMENTS["EURUSD"]))
    if data.get("swap_long"):  inst["swap_long"]  = float(data["swap_long"])
    if data.get("swap_short"): inst["swap_short"] = float(data["swap_short"])
    INSTRUMENTS[sym].update(inst)
    return jsonify(calc_forex(data))

@app.route("/calc_nepse", methods=["POST"])
def api_nepse():
    return jsonify(calc_nepse(request.get_json()))

@app.route("/usdjpy")
def usdjpy():
    now_ts = _time.monotonic()
    if _rate_cache["value"] is not None and now_ts < _rate_cache["expires"]:
        return jsonify({"rate": _rate_cache["value"]})
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=8)
        rate = r.json()["rates"]["JPY"]
        _rate_cache["value"]   = rate
        _rate_cache["expires"] = now_ts + RATE_TTL
        return jsonify({"rate": rate})
    except Exception:
        return jsonify({"rate": _rate_cache["value"] or 160.0})

@app.route("/news")
def news():
    return jsonify(get_next_high_news())

@app.route("/sessions")
def sessions():
    sess_list, overlap = get_market_session()
    return jsonify({"sessions": sess_list, "overlap": overlap})


if __name__ == "__main__":
    print("=" * 52)
    print("  Position Sizing Calculator v3.2")
    print("  Design by Roshan Pokhrel")
    print("  Forex + NEPSE + News + Sessions")
    print("=" * 52)
    print("  Desktop: http://localhost:5000")
    print("  Mobile:  http://<your-ip>:5000")
    print("  Stop:    Ctrl+C")
    print("=" * 52)
    app.run(debug=True, port=5000, host="0.0.0.0")
