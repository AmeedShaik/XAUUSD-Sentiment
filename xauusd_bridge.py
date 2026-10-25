"""
XAUUSD macro bridge for xauusd_macro_dashboard.html

Run:   pip install flask yfinance pandas requests
       python xauusd_bridge.py
Then paste  http://localhost:5000/api/macro  into the dashboard's bridge field.

Every field is null when its source fails, so the dashboard shows n/a
instead of a made-up number.
"""
import io
import os
import re
import time
import threading
import xml.etree.ElementTree as ET

import pandas as pd
import requests
import yfinance as yf
from flask import Flask, jsonify, send_from_directory

app = Flask(__name__)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

ESC_RE = re.compile(r"\b(?:wars?|missiles?|strikes?|attacks?|invasion|invad\w*|escalat\w*|sanction\w*|tensions?|"
                    r"troops|drones?|airstrikes?|nuclear|blockade|retaliat\w*|conflicts?)\b", re.I)
DE_RE = re.compile(r"\b(?:ceasefire|truce|peace (?:deal|talks)|de-escalat\w*|withdraw\w*|agreement reached)\b", re.I)


def count_geo(titles):
    de = sum(bool(DE_RE.search(t)) for t in titles)
    esc = sum(bool(ESC_RE.search(t)) and not DE_RE.search(t) for t in titles)
    return esc, de

# ---------- tiny TTL cache (seconds) ----------
_cache, _lock = {}, threading.Lock()


def cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    try:
        val = fn()
    except Exception:
        val = None
    with _lock:
        _cache[key] = (time.time(), val)
    return val


# ---------- fetchers ----------
def day_change(symbol):
    """(last, prev_close) from daily closes; None on failure."""
    s = yf.Ticker(symbol).history(period="10d", interval="1d")["Close"].dropna()
    if len(s) < 2:
        return None
    return float(s.iloc[-1]), float(s.iloc[-2])


def pct(symbol):
    p = day_change(symbol)
    return None if p is None else (p[0] - p[1]) / p[1] * 100


def last_level(symbol):
    p = day_change(symbol)
    return None if p is None else p[0]


def yield_info():
    """Prefer 10Y TIPS real yield (FRED); fall back to nominal ^TNX. Returns (bps, is_real)."""
    try:
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10", headers=UA, timeout=8)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        v = pd.to_numeric(df.iloc[:, 1], errors="coerce").dropna()
        if len(v) >= 2:
            return round(float(v.iloc[-1] - v.iloc[-2]) * 100, 1), True
    except Exception:
        pass
    p = day_change("^TNX")
    return (None, False) if p is None else (round((p[0] - p[1]) * 100, 1), False)


def retail_long():
    r = requests.get("https://www.myfxbook.com/community/outlook/XAUUSD", headers=UA, timeout=8)
    r.raise_for_status()
    text = re.sub(r"\s+", " ", r.text)
    ml = re.search(r"Long[^0-9]{0,80}?(\d{1,3})\s*%", text, re.I)
    ms = re.search(r"Short[^0-9]{0,80}?(\d{1,3})\s*%", text, re.I)
    if not (ml and ms):
        return None
    lp, sp = float(ml.group(1)), float(ms.group(1))
    return lp if 90 <= lp + sp <= 110 else None


def geopolitics():
    q = "geopolitical OR war OR missile OR sanctions OR ceasefire"
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(q)}+when:1d&hl=en-US&gl=US&ceid=US:en"
    r = requests.get(url, headers=UA, timeout=8)
    r.raise_for_status()
    titles = [(i.findtext("title") or "").lower() for i in ET.fromstring(r.content).iter("item")][:60]
    if not titles:
        return None
    return count_geo(titles)


def event_risk(hours=24):
    r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers=UA, timeout=8)
    r.raise_for_status()
    ev = r.json()
    if not isinstance(ev, list):
        return None
    now = pd.Timestamp.now(tz="UTC")
    end = now + pd.Timedelta(hours=hours)
    for e in ev:
        if e.get("country") == "USD" and e.get("impact") == "High":
            t = pd.to_datetime(e.get("date"), utc=True, errors="coerce")
            if pd.notna(t) and now <= t <= end:
                return True
    return False


def gold_wti_corr20():
    g = yf.Ticker("GC=F").history(period="6mo", interval="1d")["Close"]
    o = yf.Ticker("CL=F").history(period="6mo", interval="1d")["Close"]
    df = pd.concat([g, o], axis=1, keys=["g", "o"]).dropna()
    df = df[df["o"] > 0]
    if len(df) < 30:
        return None
    rets = df.pct_change().dropna()
    roll = rets["g"].rolling(20).corr(rets["o"]).dropna()
    return round(float(roll.iloc[-1]), 2) if len(roll) else None


def quote(symbol):
    p = day_change(symbol)
    if p is None:
        return None
    return {"current": round(p[0], 4), "prev": round(p[1], 4), "pct": round((p[0] - p[1]) / p[1] * 100, 2)}


def gold_quote():
    """MT5 spot if available, otherwise GC=F futures."""
    try:
        import MetaTrader5 as mt5
        if mt5.initialize():
            t = mt5.symbol_info_tick("XAUUSD")
            r = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_D1, 0, 3)
            if t is not None and r is not None and len(r) >= 2:
                last, prev = (t.bid + t.ask) / 2, float(r[-2]["close"])
                return {"current": round(last, 2), "prev": prev, "pct": round((last - prev) / prev * 100, 2), "src": "MT5 spot"}
    except Exception:
        pass
    q = quote("GC=F")
    if q:
        q["src"] = "GC=F futures (not spot)"
    return q


def real_yield_full():
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10", headers=UA, timeout=8)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    v = pd.to_numeric(df.iloc[:, 1], errors="coerce").dropna()
    if len(v) < 2:
        return None
    return {"current": round(float(v.iloc[-1]), 3), "bps": round(float(v.iloc[-1] - v.iloc[-2]) * 100, 1),
            "asof": str(df.iloc[v.index[-1], 0])}


def corr_full():
    g = yf.Ticker("GC=F").history(period="6mo", interval="1d")["Close"]
    o = yf.Ticker("CL=F").history(period="6mo", interval="1d")["Close"]
    df = pd.concat([g, o], axis=1, keys=["g", "o"]).dropna()
    df = df[df["o"] > 0]
    if len(df) < 30:
        return None
    ratio = df["g"] / df["o"]
    win = ratio.tail(60)
    z = float((ratio.iloc[-1] - win.mean()) / win.std()) if win.std() > 0 else 0.0
    rets = df.pct_change().dropna()
    roll = rets["g"].rolling(20).corr(rets["o"]).dropna()
    t60 = rets.tail(60)
    return {"ratio": round(float(ratio.iloc[-1]), 2), "z": round(z, 2),
            "corr20": round(float(roll.iloc[-1]), 2) if len(roll) else None,
            "corr60": round(float(t60["g"].corr(t60["o"])), 2) if len(t60) >= 30 else None,
            "series": [[str(d.date()), round(float(v), 3)] for d, v in roll.items()]}


def events_list():
    r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers=UA, timeout=8)
    r.raise_for_status()
    ev = r.json()
    if not isinstance(ev, list):
        return None
    now = pd.Timestamp.now(tz="UTC")
    out = []
    for e in ev:
        if e.get("country") == "USD" and e.get("impact") in ("High", "Medium"):
            t = pd.to_datetime(e.get("date"), utc=True, errors="coerce")
            if pd.notna(t) and t >= now:
                out.append({"t": t.isoformat(), "title": e.get("title"), "impact": e.get("impact"),
                            "forecast": e.get("forecast") or "", "previous": e.get("previous") or ""})
    out.sort(key=lambda x: x["t"])
    return out[:12]


def geo_full():
    q = "geopolitical OR war OR missile OR sanctions OR ceasefire"
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(q)}+when:1d&hl=en-US&gl=US&ceid=US:en"
    r = requests.get(url, headers=UA, timeout=8)
    r.raise_for_status()
    titles = [(i.findtext("title") or "") for i in ET.fromstring(r.content).iter("item")][:60]
    if not titles:
        return None
    esc, de = count_geo(titles)
    return {"esc": esc, "de": de, "n": len(titles), "headlines": titles[:8]}


def tech_one(symbol):
    df = yf.Ticker(symbol).history(period="8mo", interval="1d").dropna()
    if len(df) < 60:
        return None
    c, h, l = df["Close"], df["High"], df["Low"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return {"price": round(float(c.iloc[-1]), 2), "ma20": round(float(c.rolling(20).mean().iloc[-1]), 2),
            "ma50": round(float(c.rolling(50).mean().iloc[-1]), 2), "atr": round(float(tr.rolling(14).mean().iloc[-1]), 2)}


def tech_full():
    return {"gold": tech_one("GC=F"), "wti": tech_one("CL=F")}


def rnd(x, n=2):
    return None if x is None else round(float(x), n)


# ---------- route ----------
def build_data():
    yb = cached("yield", 600, yield_info) or (None, False)
    geo = cached("geo", 600, geopolitics)
    return {
        "dxy_pct": rnd(cached("dxy", 60, lambda: pct("DX-Y.NYB"))),
        "yield_bps": yb[0],
        "yield_is_real": yb[1],
        "retail_long": cached("retail", 300, retail_long),
        "vix": rnd(cached("vix", 60, lambda: last_level("^VIX"))),
        "wti_pct": rnd(cached("wti", 60, lambda: pct("CL=F"))),
        "geo_esc": geo[0] if geo else None,
        "geo_de": geo[1] if geo else None,
        "corr20": cached("corr", 600, gold_wti_corr20),
        "event_risk": cached("event", 600, event_risk),
        "fetched_utc": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.route("/api/macro")
def macro():
    resp = jsonify(build_data())
    resp.headers["Access-Control-Allow-Origin"] = "*"  # lets the local HTML file call this
    return resp


@app.route("/download")
def download():
    """Same JSON, delivered as a file: http://127.0.0.1:5000/download"""
    resp = jsonify(build_data())
    resp.headers["Content-Disposition"] = "attachment; filename=xauusd_macro.json"
    return resp


@app.route("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "xauusd_live.html")


@app.route("/api/dashboard")
def dashboard():
    t10 = cached("t10", 60, lambda: quote("^TNX"))
    if t10:
        t10["bps"] = round((t10["current"] - t10["prev"]) * 100, 1)
    data = {
        "gold": cached("gold", 60, gold_quote),
        "dxy": cached("dxy_q", 60, lambda: quote("DX-Y.NYB")),
        "us10y": t10,
        "vix": cached("vix_q", 60, lambda: quote("^VIX")),
        "wti": cached("wti_q", 60, lambda: quote("CL=F")),
        "real_yield": cached("ry", 3600, real_yield_full),
        "retail_long": cached("retail", 300, retail_long),
        "geo": cached("geofull", 600, geo_full),
        "corr": cached("corrf", 600, corr_full),
        "events": cached("events", 600, events_list),
        "tech": cached("tech", 300, tech_full),
        "fetched_utc": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S"),
    }
    resp = jsonify(data)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
