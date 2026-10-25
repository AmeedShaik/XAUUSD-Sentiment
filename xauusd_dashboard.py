import re
import io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf

try:
    import MetaTrader5 as mt5  # optional: spot gold straight from your broker
except Exception:
    mt5 = None

# ----------------------------------------------------
# PAGE CONFIG
# ----------------------------------------------------
st.set_page_config(page_title="XAUUSD Macro & Sentiment Dashboard", page_icon="🪙", layout="wide")
st.title("🪙 XAUUSD 6-Pillar Macro & Sentiment Dashboard")
st.caption(
    "Pillars: DXY · Yields (real if available) · Retail positioning · VIX · Geopolitics (each -1..+1) · "
    "WTI crude (weighted 0.5, so total -5.5..+5.5) · plus Event Risk as a confidence flag."
)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# ----------------------------------------------------
# SIDEBAR
# ----------------------------------------------------
with st.sidebar:
    st.header("Settings")
    use_mt5 = st.checkbox("Use MT5 spot XAUUSD price", value=False, disabled=mt5 is None,
                          help="Requires the MetaTrader5 package and a running terminal.")
    mt5_symbol = st.text_input("MT5 symbol", "XAUUSD")
    st.markdown("**Manual retail override** (if the scrape is unavailable)")
    manual_long = st.number_input("Retail % long (0 = off)", 0.0, 100.0, 0.0, 1.0)
    st.markdown("**Thresholds**")
    dxy_full = st.number_input("DXY % move for full score", 0.1, 2.0, 0.5, 0.05)
    yld_full = st.number_input("Yield bps move for full score", 1.0, 30.0, 8.0, 1.0)
    vix_floor = st.number_input("VIX level where fear bid starts", 10.0, 30.0, 18.0, 1.0)
    vix_full = st.number_input("VIX level for full score", 15.0, 50.0, 28.0, 1.0)
    wti_full = st.number_input("WTI % move for full score", 0.5, 10.0, 2.0, 0.5)
    event_window_h = st.number_input("Event-risk window (hours)", 1, 72, 24, 1)
    if st.button("Refresh now"):
        st.cache_data.clear()


# ----------------------------------------------------
# DATA FETCHERS (each returns None / empty on failure; nothing is faked)
# ----------------------------------------------------
def _last_two(series: pd.Series):
    s = series.dropna()
    if len(s) < 2:
        return None
    return float(s.iloc[-1]), float(s.iloc[-2])


@st.cache_data(ttl=60)
def fetch_ticker(symbol: str):
    """Latest price (intraday if possible) vs previous daily close."""
    try:
        daily = yf.Ticker(symbol).history(period="10d", interval="1d")["Close"]
        pair = _last_two(daily)
        if pair is None:
            return None
        last, prev = pair
        # try for a fresher intraday print; day-over-day change still uses daily closes
        try:
            intra = yf.Ticker(symbol).history(period="1d", interval="5m")["Close"].dropna()
            if len(intra):
                last_px = float(intra.iloc[-1])
                prev_close = float(daily.dropna().iloc[-2]) if len(daily.dropna()) >= 2 else prev
                # if the last daily bar is today's partial bar, prev close is iloc[-2]; else iloc[-1]
                last, prev = last_px, prev_close
        except Exception:
            pass
        chg = last - prev
        return {"current": last, "prev": prev, "change": chg, "pct": chg / prev * 100}
    except Exception:
        return None


@st.cache_data(ttl=60)
def fetch_mt5_gold(symbol: str):
    if mt5 is None or not mt5.initialize():
        return None
    try:
        tick = mt5.symbol_info_tick(symbol)
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 3)
        if tick is None or rates is None or len(rates) < 2:
            return None
        last = (tick.bid + tick.ask) / 2
        prev = float(rates[-2]["close"])
        return {"current": last, "prev": prev, "change": last - prev, "pct": (last - prev) / prev * 100}
    except Exception:
        return None


@st.cache_data(ttl=3600)
def fetch_real_yield():
    """10Y TIPS real yield from FRED (no API key needed). Returns level and bps change."""
    try:
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10", headers=UA, timeout=8)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        vals = pd.to_numeric(df.iloc[:, 1], errors="coerce").dropna()
        if len(vals) < 2:
            return None
        cur, prev = float(vals.iloc[-1]), float(vals.iloc[-2])
        return {"current": cur, "bps": (cur - prev) * 100, "asof": str(df.iloc[vals.index[-1], 0])}
    except Exception:
        return None


@st.cache_data(ttl=300)
def fetch_retail_sentiment():
    """Scrape Myfxbook outlook. Returns None if it can't be parsed (never a placeholder)."""
    try:
        resp = requests.get("https://www.myfxbook.com/community/outlook/XAUUSD", headers=UA, timeout=8)
        resp.raise_for_status()
        text = re.sub(r"\s+", " ", resp.text)
        # Tolerant patterns: look for Long/Short percentages near the words
        m_long = re.search(r"Long[^0-9]{0,80}?(\d{1,3})\s*%", text, re.I)
        m_short = re.search(r"Short[^0-9]{0,80}?(\d{1,3})\s*%", text, re.I)
        if not (m_long and m_short):
            return None
        lp, sp = float(m_long.group(1)), float(m_short.group(1))
        if not (90 <= lp + sp <= 110):  # sanity check
            return None
        return {"long_pct": lp, "short_pct": sp, "source": "Myfxbook"}
    except Exception:
        return None


@st.cache_data(ttl=600)
def fetch_economic_calendar():
    """USD High/Medium events for the week, upcoming only, in UTC."""
    cols = ["when_utc", "title", "impact", "forecast", "previous"]
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers=UA, timeout=8)
        r.raise_for_status()
        events = r.json()
        if not isinstance(events, list):
            return pd.DataFrame(columns=cols), "Calendar feed returned unexpected data (possibly rate-limited)."
        rows = [e for e in events if e.get("country") == "USD" and e.get("impact") in ("High", "Medium")]
        if not rows:
            return pd.DataFrame(columns=cols), None
        df = pd.DataFrame(rows)
        df["when_utc"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
        df = df.dropna(subset=["when_utc"]).sort_values("when_utc")
        for c in ("forecast", "previous"):
            if c not in df:
                df[c] = ""
        return df[cols].reset_index(drop=True), None
    except Exception as e:
        return pd.DataFrame(columns=cols), f"Calendar unavailable: {type(e).__name__}"


@st.cache_data(ttl=600)
def fetch_gold_oil_ratio():
    """Gold/WTI ratio and its 60-day z-score (context only)."""
    try:
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
        corr20 = float(roll.iloc[-1]) if len(roll) else None
        t60 = rets.tail(60)
        corr60 = float(t60["g"].corr(t60["o"])) if len(t60) >= 30 else None
        return {"ratio": float(ratio.iloc[-1]), "z": z, "corr20": corr20, "corr60": corr60, "roll": roll}
    except Exception:
        return None


ESC_RE = re.compile(r"\b(?:wars?|missiles?|strikes?|attacks?|invasion|invad\w*|escalat\w*|sanction\w*|tensions?|"
                    r"troops|drones?|airstrikes?|nuclear|blockade|retaliat\w*|conflicts?)\b", re.I)
DE_RE = re.compile(r"\b(?:ceasefire|truce|peace (?:deal|talks)|de-escalat\w*|withdraw\w*|agreement reached)\b", re.I)


@st.cache_data(ttl=600)
def fetch_geopolitics():
    """Headline keyword scan (Google News RSS, last 24h). Crude but transparent."""
    q = "geopolitical OR war OR missile OR sanctions OR ceasefire"
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(q)}+when:1d&hl=en-US&gl=US&ceid=US:en"
    try:
        r = requests.get(url, headers=UA, timeout=8)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        titles = [(i.findtext("title") or "") for i in root.iter("item")][:60]
        if not titles:
            return None
        de = sum(bool(DE_RE.search(t)) for t in titles)
        esc = sum(bool(ESC_RE.search(t)) and not DE_RE.search(t) for t in titles)
        return {"esc": esc, "de": de, "n": len(titles), "headlines": titles[:8]}
    except Exception:
        return None


# ----------------------------------------------------
# LOAD DATA
# ----------------------------------------------------
gold = (fetch_mt5_gold(mt5_symbol) if use_mt5 else None) or fetch_ticker("GC=F")
gold_src = "MT5 spot" if (use_mt5 and gold and fetch_mt5_gold(mt5_symbol)) else "GC=F futures (not spot)"
dxy = fetch_ticker("DX-Y.NYB")
us10y = fetch_ticker("^TNX")
vix = fetch_ticker("^VIX")
real_y = fetch_real_yield()
retail = fetch_retail_sentiment()
if manual_long > 0:
    retail = {"long_pct": manual_long, "short_pct": 100 - manual_long, "source": "manual"}
cal_df, cal_err = fetch_economic_calendar()
geo = fetch_geopolitics()
wti = fetch_ticker("CL=F")
ratio_d = fetch_gold_oil_ratio()

now_utc = pd.Timestamp.now(tz="UTC")
upcoming = cal_df[cal_df["when_utc"] >= now_utc] if not cal_df.empty else cal_df


def clip(x, lo=-1.0, hi=1.0):
    return float(np.clip(x, lo, hi))


# ----------------------------------------------------
# SCORING ENGINE (continuous, -1..+1 per pillar; None = unavailable -> 0 and flagged)
# ----------------------------------------------------
pillars = []  # (name, score or None, note)

# 1. DXY
if dxy:
    s = clip(-dxy["pct"] / dxy_full)
    pillars.append(("US Dollar (DXY)", s, f"{dxy['pct']:+.2f}% day-over-day"))
else:
    pillars.append(("US Dollar (DXY)", None, "data unavailable"))

# 2. Yields: prefer real yield, fall back to nominal 10Y (move in bps)
if real_y:
    s = clip(-real_y["bps"] / yld_full)
    pillars.append(("Real 10Y yield (TIPS)", s, f"{real_y['current']:.2f}% ({real_y['bps']:+.1f} bps, as of {real_y['asof']})"))
elif us10y:
    bps = us10y["change"] * 100  # ^TNX is quoted directly in percent
    s = clip(-bps / yld_full) * 0.7  # nominal is a weaker proxy -> down-weighted
    pillars.append(("Nominal 10Y yield (proxy)", s, f"{us10y['current']:.2f}% ({bps:+.1f} bps)"))
else:
    pillars.append(("Yields", None, "data unavailable"))

# 3. Retail contrarian: linear between 35% and 65%
if retail:
    s = clip((50 - retail["long_pct"]) / 15)
    pillars.append(("Retail positioning (contrarian)", s,
                    f"{retail['long_pct']:.0f}% long / {retail['short_pct']:.0f}% short ({retail['source']})"))
else:
    pillars.append(("Retail positioning (contrarian)", None, "scrape unavailable - use manual override"))

# 4. VIX safe-haven bid (only positive; calm markets are neutral, not bearish)
if vix:
    s = clip((vix["current"] - vix_floor) / max(vix_full - vix_floor, 1), 0, 1)
    pillars.append(("Fear gauge (VIX)", s, f"VIX {vix['current']:.2f}"))
else:
    pillars.append(("Fear gauge (VIX)", None, "data unavailable"))

# 5. Geopolitics (headline keyword balance, last 24h)
if geo:
    net = geo["esc"] - geo["de"]
    s = clip(net / 12)
    pillars.append(("Geopolitics (headlines)", s,
                    f"{geo['esc']} escalation vs {geo['de']} de-escalation of {geo['n']} headlines"))
else:
    pillars.append(("Geopolitics (headlines)", None, "news feed unavailable"))

# 6. WTI crude: inflation / risk impulse. Down-weighted to 0.5 because the oil-gold link is unstable
#    (oil up can mean inflation hedge demand, but also higher yields and a firmer dollar).
MAX_SCORE = 5.5
if wti:
    s = clip(wti["pct"] / wti_full) * 0.5
    note = f"WTI ${wti['current']:.2f} ({wti['pct']:+.2f}%)"
    if ratio_d:
        note += f" · gold/oil {ratio_d['ratio']:.1f} (z {ratio_d['z']:+.1f})"
    pillars.append(("Oil (WTI) inflation/risk impulse", s, note))
else:
    pillars.append(("Oil (WTI) inflation/risk impulse", None, "data unavailable"))

live = [p for p in pillars if p[1] is not None]
score = sum(p[1] for p in live)
coverage = f"{len(live)}/{len(pillars)} scored pillars live"

# 6. Event risk (flag, not directional)
risk_events = pd.DataFrame()
if not upcoming.empty:
    horizon = now_utc + pd.Timedelta(hours=int(event_window_h))
    risk_events = upcoming[(upcoming["when_utc"] <= horizon) & (upcoming["impact"] == "High")]
event_risk = len(risk_events) > 0

# ----------------------------------------------------
# UI
# ----------------------------------------------------
c1, c2, c3, c4, c5 = st.columns(5)


def metric(col, label, d, fmt, inverse=False):
    with col:
        if d:
            st.metric(label, fmt.format(d["current"]), f"{d['pct']:+.2f}%",
                      delta_color="inverse" if inverse else "normal")
        else:
            st.metric(label, "n/a")


metric(c1, f"Gold ({gold_src})", gold, "${:,.2f}")
metric(c2, "US Dollar (DXY)", dxy, "{:.2f}", inverse=True)
metric(c3, "US 10Y Yield", us10y, "{:.2f}%", inverse=True)
metric(c4, "CBOE VIX", vix, "{:.2f}")
metric(c5, "WTI Crude", wti, "${:.2f}")
if ratio_d:
    st.caption(
        f"Gold/WTI ratio: {ratio_d['ratio']:.1f} (60-day z-score {ratio_d['z']:+.1f}). "
        "A high z means gold is rich vs oil; context only, not scored."
    )
    if ratio_d.get("corr20") is not None:
        k1, k2 = st.columns([1, 2])
        with k1:
            st.metric("Gold-WTI corr (20d)", f"{ratio_d['corr20']:+.2f}")
            if ratio_d.get("corr60") is not None:
                st.metric("Gold-WTI corr (60d)", f"{ratio_d['corr60']:+.2f}")
            st.caption("Daily-return correlation. Near 0 means the oil pillar carries little information right now.")
        with k2:
            roll = ratio_d["roll"]
            cfig = go.Figure(go.Scatter(x=roll.index, y=roll.values, mode="lines", line=dict(color="#ffd600")))
            cfig.add_hline(y=0, line_dash="dot", line_color="gray")
            cfig.update_layout(height=200, margin=dict(l=10, r=10, t=30, b=10),
                               title="Rolling 20-day correlation: gold vs WTI returns",
                               yaxis=dict(range=[-1, 1]))
            st.plotly_chart(cfig, use_container_width=True)

st.markdown("---")
left, right = st.columns(2)

with left:
    st.subheader("Composite Score")
    bar_color = "#00e676" if score > 0.5 else ("#ff5252" if score < -0.5 else "#ffd600")
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=round(score, 2),
        title={"text": f"Bias (-{MAX_SCORE} Bearish to +{MAX_SCORE} Bullish)", "font": {"size": 18}},
        gauge={
            "axis": {"range": [-MAX_SCORE, MAX_SCORE]},
            "bar": {"color": bar_color},
            "steps": [
                {"range": [-MAX_SCORE, -1.5], "color": "rgba(255,82,82,0.25)"},
                {"range": [-1.5, 1.5], "color": "rgba(255,214,0,0.25)"},
                {"range": [1.5, MAX_SCORE], "color": "rgba(0,230,118,0.25)"},
            ],
        },
    ))
    fig.update_layout(height=260, margin=dict(l=20, r=20, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)
    st.caption(coverage + ". Unavailable pillars count as 0, not as a guess.")

    if event_risk:
        nxt = risk_events.iloc[0]
        st.warning(f"⚠️ Event risk: {nxt['title']} at {nxt['when_utc']:%a %H:%M} UTC "
                   f"(+{len(risk_events) - 1} more high-impact within {event_window_h}h). "
                   "Treat the bias with lower confidence and expect spread/volatility.")
    else:
        st.success(f"No high-impact USD events in the next {event_window_h}h.")

    st.markdown("**Pillar breakdown**")
    tbl = pd.DataFrame(
        [{"Pillar": n, "Score": ("n/a" if s is None else f"{s:+.2f}"), "Detail": note} for n, s, note in pillars]
    )
    st.dataframe(tbl, use_container_width=True, hide_index=True)

with right:
    st.subheader("Retail Positioning")
    if retail:
        st.write(f"**Long:** {retail['long_pct']:.0f}% | **Short:** {retail['short_pct']:.0f}%  ({retail['source']})")
        st.progress(int(min(max(retail["long_pct"], 0), 100)))
    else:
        st.info("Retail data unavailable (Myfxbook scrape failed). Enter a value in the sidebar to override.")

    st.markdown("---")
    st.subheader("Upcoming High/Medium-Impact USD Events")
    if cal_err:
        st.warning(cal_err)
    if not upcoming.empty:
        show = upcoming.head(8).copy()
        show["when_utc"] = show["when_utc"].dt.strftime("%a %d %b %H:%M UTC")
        st.dataframe(show, use_container_width=True, hide_index=True)
    elif not cal_err:
        st.info("No high/medium-impact USD releases remaining this week.")

    st.markdown("---")
    st.subheader("Geopolitical headlines (24h)")
    if geo:
        for h in geo["headlines"][:6]:
            st.write("• " + h)
    else:
        st.info("News feed unavailable.")

st.markdown("---")
st.caption(
    f"Updated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Headline scoring is a crude keyword count; "
    "treat the geopolitics pillar as context, not signal. Not financial advice."
)
