"""
APEX Pro v5 — Production Bot
════════════════════════════════════════════════════════════════
Strategy: SATS Long-Only (Self-Aware Trend System)
Signal:   Adaptive Supertrend flip up + TQI ≥ 0.6
Coins:    SOL, XRP, DOGE, ADA, AVAX
Direction: Long only
Timeframe: 1h candles
SL:        1.5x ATR below pivot low
TP:        1R (1x risk distance)
Risk:      5% of balance per trade
Max pos:   2 concurrent
Daily DD:  -10% → pause 24h

Backtested: 75% WR | +3.08% exp | 4.05x PF | -9.8% DD
            $110 → $156 over 90 days

Deploy: Railway (FastAPI + uvicorn)
Env vars needed:
  BYBIT_API_KEY
  BYBIT_API_SECRET
  SUPABASE_URL      (optional — for trade logging)
  SUPABASE_KEY      (optional)
  PORT              (Railway sets this automatically)
════════════════════════════════════════════════════════════════
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn, os, time, asyncio, hmac, hashlib, json, math
import httpx

app = FastAPI()

# ── CONFIG ───────────────────────────────────────────────────
API_KEY    = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BASE       = "https://api.bybit.com"
SB_URL     = os.getenv("SUPABASE_URL", "")
SB_KEY     = os.getenv("SUPABASE_KEY", "")

COINS      = ["SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT"]
MAX_POS    = 2        # max concurrent positions
RISK_PCT   = 5.0      # % of balance per trade
DAILY_DD_LIMIT = 10.0 # pause if daily drawdown exceeds this %
SCAN_INTERVAL  = 60   # seconds between scans (1 min — faster score updates)

# ── SATS Parameters ───────────────────────────────────────────
ATR_LEN        = 14
BASE_MULT      = 2.8
ER_LEN         = 20
SL_ATR_MULT    = 1.5
ATR_BASELINE   = 100
ADAPT_STRENGTH = 0.5
TQI_MIN        = 0.60
TP_R           = 1.0
TQI_ER_W       = 0.35
TQI_VOL_W      = 0.20
TQI_STRUCT_W   = 0.25
TQI_MOM_W      = 0.20
TQI_STRUCT_LEN = 20
TQI_MOM_LEN    = 10
QUAL_STRENGTH  = 0.4
QUAL_CURVE     = 1.5
USE_ASYM       = True
ASYM_STRENGTH  = 0.5
PIVOT_LEN      = 3
CANDLES_NEEDED = 200  # minimum candles to calculate indicators

# ── STATE ────────────────────────────────────────────────────
state = {
    "status":        "starting",
    "balance":       0.0,
    "balance_start": 0.0,   # balance at day start for DD tracking
    "balance_day":   0.0,
    "dd_paused":     False,
    "dd_pause_until":0,
    "positions":     {},     # symbol → position dict
    "trades":        [],     # last 100 closed trades
    "wins":          0,
    "losses":        0,
    "pnl":           0.0,
    "scan_log":      [],     # last 20 scan events
    "last_scan":     0,
    "start_time":    int(time.time()),
    "errors":        [],
    "manual_paused": False,  # user-controlled pause from dashboard
}

# ── BYBIT API ────────────────────────────────────────────────

def _sign(params: dict) -> dict:
    params["api_key"]     = API_KEY
    params["timestamp"]   = str(int(time.time() * 1000))
    params["recv_window"] = "5000"
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    params["sign"] = hmac.new(
        API_SECRET.encode(), query.encode(), hashlib.sha256
    ).hexdigest()
    return params

async def _get(path, params={}, signed=False):
    if signed:
        params = _sign(dict(params))
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(BASE + path, params=params)
        return r.json()

async def _post(path, body):
    body = _sign(dict(body))
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(BASE + path, json=body)
        return r.json()

async def get_balance() -> float:
    try:
        r = await _get("/v5/account/wallet-balance",
                       {"accountType": "UNIFIED"}, signed=True)
        if r.get("retCode") == 0:
            acct = r["result"]["list"][0]
            bal  = float(acct.get("totalAvailableBalance") or 0)
            if bal > 0:
                return bal
            for c in acct.get("coin", []):
                if c["coin"] == "USDT":
                    val = float(c.get("walletBalance") or 0)
                    if val > 0:
                        return val
    except Exception as e:
        _log_error(f"get_balance: {e}")
    return 0.0

async def get_klines(symbol, interval="60", limit=220):
    try:
        r = await _get("/v5/market/kline", {
            "category": "linear",
            "symbol":   symbol,
            "interval": interval,
            "limit":    str(limit),
        })
        if r.get("retCode") == 0:
            return list(reversed(r["result"]["list"]))
    except Exception as e:
        _log_error(f"get_klines {symbol}: {e}")
    return []

async def get_ticker(symbol):
    try:
        r = await _get("/v5/market/tickers",
                       {"category": "linear", "symbol": symbol})
        items = r.get("result", {}).get("list", [])
        return items[0] if items else {}
    except:
        return {}

async def get_instrument(symbol):
    """Get qty step and min order qty."""
    try:
        r = await _get("/v5/market/instruments-info",
                       {"category": "linear", "symbol": symbol})
        info = r.get("result", {}).get("list", [])
        if info:
            lot = info[0].get("lotSizeFilter", {})
            return float(lot.get("qtyStep", "0.001")), \
                   float(lot.get("minOrderQty", "0.001"))
    except:
        pass
    return 0.001, 0.001

async def place_order(symbol, side, qty, sl_price):
    return await _post("/v5/order/create", {
        "category":   "linear",
        "symbol":     symbol,
        "side":       "Buy" if side == "long" else "Sell",
        "orderType":  "Market",
        "qty":        str(qty),
        "stopLoss":   str(round(sl_price, 6)),
        "positionIdx":"0",
    })

async def close_order(symbol, qty, side):
    return await _post("/v5/order/create", {
        "category":   "linear",
        "symbol":     symbol,
        "side":       "Sell" if side == "long" else "Buy",
        "orderType":  "Market",
        "qty":        str(qty),
        "reduceOnly": True,
        "positionIdx":"0",
    })

async def check_open_position(symbol):
    """Check if we actually have an open position on Bybit."""
    try:
        r = await _get("/v5/position/list",
                       {"category": "linear", "symbol": symbol},
                       signed=True)
        for p in r.get("result", {}).get("list", []):
            if p.get("symbol") == symbol and float(p.get("size", 0)) > 0:
                return True
        return False
    except:
        return True  # assume open on error to be safe

def round_qty(qty, step):
    if step <= 0:
        return round(qty, 3)
    dec = max(0, -int(math.floor(math.log10(step))))
    return round(math.floor(qty / step) * step, dec)

# ── INDICATORS ────────────────────────────────────────────────

def _calc_sma(values, period):
    result = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(sum(values[:i+1]) / (i+1))
        else:
            result.append(sum(values[i-period+1:i+1]) / period)
    return result

def _calc_stdev(values, period):
    result = []
    for i in range(len(values)):
        w = values[max(0, i-period+1):i+1]
        if len(w) < 2:
            result.append(0.0); continue
        mean = sum(w) / len(w)
        result.append(math.sqrt(sum((x-mean)**2 for x in w) / len(w)))
    return result

def _calc_atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        h  = float(candles[i][2])
        l  = float(candles[i][3])
        pc = float(candles[i-1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return [0.0] * len(candles)
    atr = [trs[0]] * period
    for i in range(period, len(trs)):
        atr.append((atr[-1] * (period-1) + trs[i]) / period)
    return [0.0] + atr

def _calc_er(closes, period):
    result = [0.0] * len(closes)
    for i in range(period, len(closes)):
        net  = abs(closes[i] - closes[i-period])
        path = sum(abs(closes[j] - closes[j-1])
                   for j in range(i-period+1, i+1))
        result[i] = net / path if path > 0 else 0.0
    return result

def _calc_vol_z(volumes, period=20):
    sma = _calc_sma(volumes, period)
    std = _calc_stdev(volumes, period)
    return [(volumes[i]-sma[i])/std[i] if std[i] > 0 else 0.0
            for i in range(len(volumes))]

def _clamp(v, lo, hi):
    return max(lo, min(hi, v))

def _map_clamp(v, il, ih, ol, oh):
    t = _clamp((v-il)/(ih-il) if (ih-il) != 0 else 0, 0, 1)
    return ol + t * (oh - ol)

def _calc_tqi(candles, er_vals, vol_z_vals):
    n      = len(candles)
    closes = [float(c[4]) for c in candles]
    highs  = [float(c[2]) for c in candles]
    lows   = [float(c[3]) for c in candles]
    tqi    = [0.5] * n
    for i in range(max(TQI_STRUCT_LEN, TQI_MOM_LEN), n):
        tqi_er  = _clamp(er_vals[i], 0, 1)
        tqi_vol = _clamp(_map_clamp(vol_z_vals[i], -1.0, 2.0, 0.0, 1.0), 0, 1)
        sh = max(highs[i-TQI_STRUCT_LEN:i+1])
        sl = min(lows[i-TQI_STRUCT_LEN:i+1])
        sr = sh - sl
        pp = (closes[i]-sl)/sr if sr > 0 else 0.5
        tqi_struct = _clamp(abs(pp-0.5)*2.0, 0, 1)
        wc = closes[i] - closes[i-TQI_MOM_LEN]
        aligned = sum(
            1 for k in range(TQI_MOM_LEN)
            if (wc > 0 and closes[i-k] > closes[i-k-1]) or
               (wc < 0 and closes[i-k] < closes[i-k-1])
        )
        tqi_mom = aligned / TQI_MOM_LEN
        w = TQI_ER_W + TQI_VOL_W + TQI_STRUCT_W + TQI_MOM_W
        tqi[i] = _clamp(
            (tqi_er*TQI_ER_W + tqi_vol*TQI_VOL_W +
             tqi_struct*TQI_STRUCT_W + tqi_mom*TQI_MOM_W) / w,
            0, 1
        )
    return tqi

def _calc_supertrend(candles, atr_vals, er_vals, tqi_vals):
    n      = len(candles)
    closes = [float(c[4]) for c in candles]
    SMOOTH = 0.15
    direction  = [1] * n
    upper_band = [0.0] * n
    lower_band = [0.0] * n
    active_sm  = BASE_MULT
    passive_sm = BASE_MULT

    for i in range(ATR_LEN, n):
        er  = er_vals[i]
        tqi = tqi_vals[i]
        atr = atr_vals[i]
        if atr == 0:
            direction[i] = direction[i-1]; continue

        leg  = 1.0 + ADAPT_STRENGTH * (0.5 - er)
        qd   = (1.0 - tqi) ** QUAL_CURVE
        tqim = 1.0 - QUAL_STRENGTH + QUAL_STRENGTH * (0.6 + 0.8 * qd)
        sym  = BASE_MULT * leg * tqim

        if USE_ASYM:
            a_r = sym * (1.0 - ASYM_STRENGTH * tqi * 0.3)
            p_r = sym * (1.0 + ASYM_STRENGTH * tqi * 0.4)
        else:
            a_r = p_r = sym

        active_sm  = active_sm  * (1-SMOOTH) + a_r * SMOOTH
        passive_sm = passive_sm * (1-SMOOTH) + p_r * SMOOTH

        eff_atr = atr * (0.5 + 0.5 * er)
        pd = direction[i-1]
        lm = active_sm  if pd ==  1 else passive_sm
        um = passive_sm if pd ==  1 else active_sm

        src   = closes[i]
        lb_r  = src - lm * eff_atr
        ub_r  = src + um * eff_atr

        if i == ATR_LEN:
            lower_band[i] = lb_r
            upper_band[i] = ub_r
        else:
            lower_band[i] = lb_r if (lb_r > lower_band[i-1] or
                closes[i-1] < lower_band[i-1]) else lower_band[i-1]
            upper_band[i] = ub_r if (ub_r < upper_band[i-1] or
                closes[i-1] > upper_band[i-1]) else upper_band[i-1]

        if i == ATR_LEN:
            direction[i] = -1 if closes[i] > upper_band[i] else 1
        else:
            if direction[i-1] == 1:
                direction[i] = -1 if closes[i] > upper_band[i] else 1
            else:
                direction[i] =  1 if closes[i] < lower_band[i] else -1

    return direction

def _calc_pivots(highs, lows, pl):
    n   = len(highs)
    plo = [None] * n
    for i in range(pl, n - pl):
        if lows[i] == min(lows[i-pl:i+pl+1]):
            plo[i] = lows[i]
    return plo

# ── SATS SIGNAL ───────────────────────────────────────────────

def compute_sats_signal(candles):
    """
    Returns dict with signal info or None if no signal.
    Signal = Supertrend flips from bearish to bullish AND TQI ≥ 0.6
    """
    if len(candles) < CANDLES_NEEDED:
        return None

    closes = [float(c[4]) for c in candles]
    highs  = [float(c[2]) for c in candles]
    lows   = [float(c[3]) for c in candles]
    vols   = [float(c[5]) for c in candles]

    atr_vals = _calc_atr(candles, ATR_LEN)
    er_vals  = _calc_er(closes, ER_LEN)
    vol_z    = _calc_vol_z(vols, 20)
    tqi_vals = _calc_tqi(candles, er_vals, vol_z)
    st_dir   = _calc_supertrend(candles, atr_vals, er_vals, tqi_vals)
    pivot_lo = _calc_pivots(highs, lows, PIVOT_LEN)

    i = len(candles) - 1  # current bar

    # Check for Supertrend flip up on current bar
    flip_up = st_dir[i] == -1 and st_dir[i-1] == 1

    if not flip_up:
        return None

    tqi = tqi_vals[i]
    if tqi < TQI_MIN:
        return None

    # Find last pivot low for SL
    last_pl = None
    for j in range(i-1, max(0, i-50), -1):
        if pivot_lo[j] is not None:
            last_pl = pivot_lo[j]
            break

    atr   = atr_vals[i]
    price = closes[i]

    sl_base = last_pl if last_pl else lows[i]
    sl      = min(sl_base - SL_ATR_MULT * atr,
                  price   - SL_ATR_MULT * atr)
    risk    = price - sl
    if risk <= 0:
        return None

    tp = price + risk * TP_R

    return {
        "price":   round(price, 6),
        "sl":      round(sl, 6),
        "tp":      round(tp, 6),
        "risk":    round(risk, 6),
        "atr":     round(atr, 6),
        "tqi":     round(tqi, 3),
        "sl_pct":  round(risk / price * 100, 3),
    }

# ── POSITION SIZING ───────────────────────────────────────────

def calc_position_size(balance, sl_pct, price, step, min_qty):
    """Risk 5% of balance. Position = risk_amount / sl_distance."""
    risk_amount  = balance * (RISK_PCT / 100)
    sl_distance  = sl_pct / 100  # as decimal
    if sl_distance <= 0:
        return 0
    position_usd = risk_amount / sl_distance
    qty          = position_usd / price
    qty          = round_qty(qty, step)
    return max(qty, min_qty)

# ── DAILY DRAWDOWN PROTECTION ─────────────────────────────────

def check_daily_dd():
    """Reset day balance at midnight, pause if DD > 10%."""
    now = int(time.time())

    # Reset daily balance at start of each UTC day
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().toordinal()
    last  = datetime.fromtimestamp(
        state["balance_day"], tz=timezone.utc
    ).date().toordinal() if state["balance_day"] > 0 else 0

    if today > last:
        state["balance_start"] = state["balance"]
        state["balance_day"]   = now

    # Check if paused
    if state["dd_paused"]:
        if now >= state["dd_pause_until"]:
            state["dd_paused"] = False
            _log_scan("DD pause lifted — resuming trading")
        else:
            return False  # still paused

    # Check current DD
    if state["balance_start"] > 0:
        dd = (state["balance"] - state["balance_start"]) / \
              state["balance_start"] * 100
        if dd <= -DAILY_DD_LIMIT:
            state["dd_paused"]     = True
            state["dd_pause_until"]= now + 24 * 3600
            _log_scan(f"Daily DD limit hit ({dd:.1f}%) — pausing 24h")
            return False

    return True

# ── HELPERS ───────────────────────────────────────────────────

def _log_scan(msg):
    state["scan_log"].insert(0, {
        "time": int(time.time()),
        "msg":  msg,
    })
    state["scan_log"] = state["scan_log"][:25]
    print(msg)

def _log_error(msg):
    state["errors"].insert(0, {
        "time": int(time.time()),
        "msg":  msg,
    })
    state["errors"] = state["errors"][:10]
    print(f"ERROR: {msg}")

def _log_scan_detail(symbol, tqi, reason, er=None, st=None, flip=False):
    """Log per-coin scan result with scores to scan_detail list."""
    entry = {
        "time":   int(time.time()),
        "symbol": symbol,
        "tqi":    tqi,
        "er":     er,
        "st":     st,
        "flip":   flip,
        "reason": reason,
    }
    state["scan_detail"].insert(0, entry)
    state["scan_detail"] = state["scan_detail"][:30]

# ── SUPABASE LOGGING ─────────────────────────────────────────

async def sb_save(trade, result):
    if not SB_URL or not SB_KEY:
        return
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            await c.post(
                f"{SB_URL}/rest/v1/trades",
                json={
                    "symbol":     trade.get("symbol"),
                    "direction":  "long",
                    "result":     result,
                    "entry":      trade.get("entry"),
                    "exit_price": trade.get("exit_price"),
                    "pnl":        trade.get("pnl"),
                    "pnl_pct":    trade.get("pnl_pct"),
                    "tqi":        trade.get("tqi"),
                    "exit_type":  trade.get("exit_type"),
                },
                headers={
                    "apikey":        SB_KEY,
                    "Authorization": f"Bearer {SB_KEY}",
                    "Content-Type":  "application/json",
                    "Prefer":        "return=minimal",
                }
            )
    except:
        pass

async def sb_load():
    if not SB_URL or not SB_KEY:
        return
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(
                f"{SB_URL}/rest/v1/trades?select=result,pnl&order=created_at.asc",
                headers={
                    "apikey":        SB_KEY,
                    "Authorization": f"Bearer {SB_KEY}",
                }
            )
            trades = r.json()
            if isinstance(trades, list):
                state["wins"]   = sum(1 for t in trades
                                      if t.get("result","").lower() == "win")
                state["losses"] = sum(1 for t in trades
                                      if t.get("result","").lower() == "loss")
                state["pnl"]    = round(
                    sum(float(t.get("pnl", 0)) for t in trades), 4
                )
                print(f"Restored: W={state['wins']} L={state['losses']} "
                      f"PnL=${state['pnl']:.2f}")
    except Exception as e:
        print(f"Supabase load err: {e}")

# ── CORE SCAN LOGIC ───────────────────────────────────────────

async def scan_symbol(symbol):
    """
    Scan one symbol for SATS signal.
    Returns True if a trade was entered.
    """
    # Fetch 1h candles first so we always get scores
    candles = await get_klines(symbol, "60", 220)
    if len(candles) < CANDLES_NEEDED:
        _log_scan_detail(symbol, None, "NO DATA")
        return False

    # Always compute TQI to show scores in scan log
    closes   = [float(c[4]) for c in candles]
    highs    = [float(c[2]) for c in candles]
    lows     = [float(c[3]) for c in candles]
    vols     = [float(c[5]) for c in candles]
    atr_vals = _calc_atr(candles, ATR_LEN)
    er_vals  = _calc_er(closes, ER_LEN)
    vol_z    = _calc_vol_z(vols, 20)
    tqi_vals = _calc_tqi(candles, er_vals, vol_z)
    st_dir   = _calc_supertrend(candles, atr_vals, er_vals, tqi_vals)
    i        = len(candles) - 1
    tqi      = round(tqi_vals[i], 3)
    er       = round(er_vals[i], 3)
    flip_up  = st_dir[i] == -1 and st_dir[i-1] == 1
    st_state = "BULL" if st_dir[i] == -1 else "BEAR"

    # Skip if already in position (but still log the score)
    if symbol in state["positions"]:
        _log_scan_detail(symbol, tqi, "IN TRADE", er=er, st=st_state, flip=flip_up)
        return False

    # Compute SATS signal
    signal = compute_sats_signal(candles)
    if signal is None:
        reason = "TQI LOW" if flip_up else "NO FLIP"
        _log_scan_detail(symbol, tqi, reason, er=er, st=st_state, flip=flip_up)
        return False

    # Log signal found
    _log_scan_detail(symbol, signal["tqi"], "SIGNAL ✅",
                     er=er, st="BULL", flip=True)

    # Check daily DD protection
    if not check_daily_dd():
        return False

    # Check max positions
    if len(state["positions"]) >= MAX_POS:
        return False

    # Get current balance
    if state["balance"] < 10:
        return False

    # Get instrument specs
    step, min_qty = await get_instrument(symbol)

    # Size the position
    qty = calc_position_size(
        state["balance"],
        signal["sl_pct"],
        signal["price"],
        step,
        min_qty,
    )

    if qty < min_qty:
        _log_scan(f"  SKIP {symbol}: qty {qty} < min {min_qty}")
        return False

    # Place order
    resp = await place_order(symbol, "long", qty, signal["sl"])
    if not resp or resp.get("retCode") != 0:
        _log_error(f"ORDER FAIL {symbol}: {resp}")
        return False

    # Record position
    state["positions"][symbol] = {
        "symbol":    symbol,
        "side":      "long",
        "entry":     signal["price"],
        "sl":        signal["sl"],
        "tp":        signal["tp"],
        "qty":       qty,
        "sl_pct":    signal["sl_pct"],
        "tqi":       signal["tqi"],
        "atr":       signal["atr"],
        "open_time": int(time.time()),
        "peak_pnl":  0.0,
    }

    _log_scan(
        f"  ✅ ENTERED {symbol} LONG @ {signal['price']} "
        f"| SL={signal['sl']} | TP={signal['tp']} "
        f"| TQI={signal['tqi']} | Qty={qty}"
    )
    await sb_save(state["positions"][symbol], "open")
    return True

async def monitor_position(symbol, pos):
    """
    Monitor an open position.
    Check if TP or SL hit via live ticker.
    """
    ticker = await get_ticker(symbol)
    if not ticker:
        return

    price = float(ticker.get("lastPrice", pos["entry"]))
    pnl_pct = (price - pos["entry"]) / pos["entry"] * 100

    # Update peak
    if pnl_pct > pos.get("peak_pnl", 0):
        pos["peak_pnl"] = round(pnl_pct, 3)

    # Check if still open on exchange
    still_open = await check_open_position(symbol)
    if not still_open:
        # SL was hit by exchange
        pnl_usd = pos["qty"] * pos["entry"] * (pnl_pct / 100)
        result  = "win" if pnl_pct > 0 else "loss"
        await close_trade(pos, price, result, "ExchangeClose")
        return

    # Check TP hit
    if price >= pos["tp"]:
        r = await close_order(symbol, pos["qty"], "long")
        if r and r.get("retCode") == 0:
            pnl_pct = (pos["tp"] - pos["entry"]) / pos["entry"] * 100
            await close_trade(pos, pos["tp"], "win", "TP1")
        return

    # Check SL hit (belt-and-suspenders — exchange should handle this)
    if price <= pos["sl"]:
        r = await close_order(symbol, pos["qty"], "long")
        if r and r.get("retCode") == 0:
            pnl_pct = (pos["sl"] - pos["entry"]) / pos["entry"] * 100
            await close_trade(pos, pos["sl"], "loss", "SL")
        return

    print(f"  HOLD {symbol} | Price={price} | "
          f"PnL={pnl_pct:+.2f}% | Peak={pos.get('peak_pnl',0):+.2f}%")

async def close_trade(pos, exit_price, result, exit_type):
    symbol   = pos["symbol"]
    entry    = pos["entry"]
    pnl_pct  = (exit_price - entry) / entry * 100
    pnl_usd  = pos["qty"] * entry * (pnl_pct / 100)

    trade = {
        **pos,
        "exit_price": round(exit_price, 6),
        "pnl_pct":    round(pnl_pct, 3),
        "pnl":        round(pnl_usd, 4),
        "result":     result,
        "exit_type":  exit_type,
        "close_time": int(time.time()),
    }

    state["trades"].append(trade)
    state["trades"] = state["trades"][-100:]
    state["positions"].pop(symbol, None)
    state["pnl"] += pnl_usd

    if result == "win":
        state["wins"] += 1
    else:
        state["losses"] += 1

    _log_scan(
        f"  {'✅ WIN' if result=='win' else '❌ LOSS'} "
        f"{symbol} | {pnl_pct:+.2f}% | ${pnl_usd:+.2f} | {exit_type}"
    )
    await sb_save(trade, result)

# ── TOGGLE ENDPOINT ─────────────────────────────────────────

@app.post("/api/reset_dd")
async def reset_dd():
    """Manually clear the DD pause — use if timer gets stuck."""
    state["dd_paused"]      = False
    state["dd_pause_until"] = 0
    state["balance_start"]  = state["balance"]
    _log_scan("DD pause manually cleared via dashboard")
    return {"ok": True, "msg": "DD pause cleared"}

@app.post("/api/toggle")
async def toggle_bot():
    state["manual_paused"] = not state["manual_paused"]
    status = "paused" if state["manual_paused"] else "running"
    state["status"] = status
    _log_scan(f"Bot manually {'PAUSED' if state['manual_paused'] else 'RESUMED'} via dashboard")
    return {"paused": state["manual_paused"], "status": status}

# ── MAIN BOT LOOP ────────────────────────────────────────────

async def bot_loop():
    print("APEX Pro v5 starting...")
    # Ensure all state keys exist (safe for hot-reloads)
    state.setdefault("manual_paused", False)
    state.setdefault("scan_detail", [])
    await sb_load()

    state["balance"]       = await get_balance()
    state["balance_start"] = state["balance"]
    state["balance_day"]   = int(time.time())
    state["status"]        = "running"

    print(f"Balance: ${state['balance']:.2f}")

    while True:
        try:
            state["last_scan"] = int(time.time())

            # Refresh balance
            bal = await get_balance()
            if bal > 0:
                state["balance"] = bal

            # Monitor existing positions
            if state["positions"]:
                tasks = [
                    monitor_position(sym, pos)
                    for sym, pos in list(state["positions"].items())
                ]
                await asyncio.gather(*tasks)

            # Always check DD timer — this is what lifts the pause
            check_daily_dd()

            # Scan for new signals
            slots = MAX_POS - len(state["positions"])
            if slots > 0 and not state["dd_paused"] and not state["manual_paused"]:
                _log_scan(
                    f"Scanning {len(COINS)} coins | "
                    f"Balance=${state['balance']:.2f} | "
                    f"Positions={len(state['positions'])}/{MAX_POS}"
                )
                for symbol in COINS:
                    if len(state["positions"]) >= MAX_POS:
                        break
                    try:
                        entered = await scan_symbol(symbol)
                        if not entered:
                            pass  # normal — most scans find nothing
                    except Exception as e:
                        _log_error(f"scan {symbol}: {e}")
                    await asyncio.sleep(1)  # rate limit between coins

        except Exception as e:
            _log_error(f"bot_loop: {e}")

        await asyncio.sleep(SCAN_INTERVAL)

# ── STARTUP ───────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    asyncio.create_task(bot_loop())

# ── API ENDPOINTS ─────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {
        "ok":        True,
        "status":    state["status"],
        "balance":   state["balance"],
        "positions": len(state["positions"]),
        "uptime":    int(time.time()) - state["start_time"],
        "dd_paused": state["dd_paused"],
    }

@app.get("/api/status")
async def api_status():
    # Live P&L for open positions
    pos_data = {}
    for sym, pos in state["positions"].items():
        try:
            tk = await asyncio.wait_for(get_ticker(sym), timeout=3)
            lp = float(tk.get("lastPrice", pos["entry"]))
            pnl = (lp - pos["entry"]) / pos["entry"] * 100
            pos_data[sym] = {**pos, "live_price": round(lp, 6),
                             "live_pnl": round(pnl, 3)}
        except:
            pos_data[sym] = {**pos, "live_price": pos["entry"],
                             "live_pnl": 0.0}

    total = state["wins"] + state["losses"]
    return JSONResponse({
        "status":    state["status"],
        "balance":   state["balance"],
        "wins":      state["wins"],
        "losses":    state["losses"],
        "win_rate":  round(state["wins"]/total*100, 1) if total > 0 else 0,
        "pnl":       round(state["pnl"], 2),
        "positions": pos_data,
        "trades":    state["trades"][-30:],
        "scan_log":  state["scan_log"][:15],
        "last_scan": state["last_scan"],
        "dd_paused": state["dd_paused"],
        "errors":    state["errors"][:5],
        "now":       int(time.time()),
    })

@app.get("/api/debug/balance")
async def debug_balance():
    results = {}
    for acct in ["UNIFIED", "CONTRACT"]:
        try:
            r = await _get("/v5/account/wallet-balance",
                           {"accountType": acct}, signed=True)
            results[acct] = r
        except Exception as e:
            results[acct] = {"error": str(e)}
    return JSONResponse(results)

# ── DASHBOARD ─────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    total = state["wins"] + state["losses"]
    wr    = f"{state['wins']/total*100:.0f}%" if total > 0 else "—"
    pnl   = state["pnl"]
    bal   = state["balance"]

    trades = state["trades"]
    avg_win  = (sum(t["pnl"] for t in trades if t.get("result")=="win") /
                max(state["wins"],1))
    avg_loss = (sum(t["pnl"] for t in trades if t.get("result")=="loss") /
                max(state["losses"],1))
    age = int(time.time()) - state["last_scan"] if state["last_scan"] > 0 else "?"

    def pnl_col(v): return "#22c55e" if v >= 0 else "#ef4444"
    def fmt(v, d=2): return f"{v:.{d}f}"

    # Positions HTML
    pos_html = ""
    if state["positions"]:
        for sym, pos in state["positions"].items():
            try:
                tk = await asyncio.wait_for(get_ticker(sym), timeout=2)
                lp = float(tk.get("lastPrice", pos["entry"]))
            except:
                lp = pos["entry"]
            pnl_pct = (lp - pos["entry"]) / pos["entry"] * 100
            dur = int((time.time() - pos["open_time"]) / 60)
            col = pnl_col(pnl_pct)
            pos_html += f"""
            <div class="card pos-card">
              <div class="row">
                <b>{sym}</b>
                <span class="badge long">LONG</span>
                <span class="score-badge">TQI {pos['tqi']}</span>
              </div>
              <div class="row mt8">
                <span>Entry <b>${fmt(pos['entry'],4)}</b></span>
                <span>Now <b style="color:{col}">${fmt(lp,4)}</b></span>
                <span>P&L <b style="color:{col}">{pnl_pct:+.2f}%</b></span>
              </div>
              <div class="row mt4">
                <span>SL <b style="color:#ef4444">${fmt(pos['sl'],4)}</b></span>
                <span>TP <b style="color:#22c55e">${fmt(pos['tp'],4)}</b></span>
                <span style="color:#888">{dur}m open</span>
              </div>
            </div>"""
    else:
        pos_html = '<div class="empty">No open positions — scanning...</div>'

    # Scan log HTML
    log_html = ""
    for s in state["scan_log"][:12]:
        age_s   = int(time.time()) - s.get("time", int(time.time()))
        age_str = f"{age_s//60}m" if age_s >= 60 else f"{age_s}s"
        msg     = s.get("msg","")
        col     = "#22c55e" if "ENTERED" in msg or "WIN" in msg else \
                  "#ef4444" if "LOSS" in msg or "ERROR" in msg else "#888"
        log_html += f"""
        <div class="log-row">
          <span style="font-size:12px;color:{col}">{msg}</span>
          <span class="age">{age_str} ago</span>
        </div>"""
    if not log_html:
        log_html = '<div class="empty">Scanning...</div>'

    # Trades HTML
    trades_html = ""
    for t in reversed(state["trades"][-12:]):
        p   = t.get("pnl", 0)
        col = pnl_col(p)
        res = t.get("result","")
        trades_html += f"""
        <div class="log-row">
          <span class="sym">{t.get('symbol','').replace('USDT','')}</span>
          <span style="font-size:11px;color:#888">{t.get('exit_type','')}</span>
          <span style="color:{col};font-weight:700;margin-left:auto">
            {'+' if p>=0 else ''}${fmt(abs(p))}</span>
          <span class="tag tag-{'signal' if res=='win' else 'blocked'}">
            {res.upper()}</span>
        </div>"""
    if not trades_html:
        trades_html = '<div class="empty">No trades yet</div>'

    dd_warning = ""
    # Pre-compute toggle values (avoid nested quotes in f-string)
    is_paused     = state["manual_paused"]
    btn_class     = "toggle-btn btn-pause" if not is_paused else "toggle-btn btn-resume"
    btn_label     = "⏸  PAUSE BOT" if not is_paused else "▶  RESUME BOT"
    if state["dd_paused"]:
        resume = state["dd_pause_until"] - int(time.time())
        dd_warning = f'<div class="dd-warning">⚠️ Daily DD limit hit — paused {resume//3600:.0f}h {(resume%3600)//60:.0f}m</div>'
    if state["manual_paused"]:
        dd_warning += '<div class="manual-warning">⏸ Bot manually paused — click RESUME to restart scanning</div>'

    # Build scan detail log HTML
    scan_detail_html = ""
    for s in state.get("scan_detail", [])[:15]:
        age_s   = int(time.time()) - s.get("time", int(time.time()))
        age_str = f"{age_s//60}m" if age_s >= 60 else f"{age_s}s"
        sym     = s.get("symbol","").replace("USDT","")
        tqi     = s.get("tqi")
        er      = s.get("er")
        reason  = s.get("reason","")
        st      = s.get("st","")
        flip    = s.get("flip", False)
        tqi_str = f"{tqi:.3f}" if tqi is not None else "—"
        er_str  = f"{er:.3f}" if er is not None else "—"
        if "SIGNAL" in reason:
            row_col = "#16a34a"; bg = "#f0fdf4"
        elif flip:
            row_col = "#ca8a04"; bg = "#fefce8"
        else:
            row_col = "#888"; bg = "transparent"
        tqi_col = "#16a34a" if tqi and tqi >= 0.6 else "#ef4444" if tqi else "#888"
        scan_detail_html += f'''
        <div class="scan-row" style="background:{bg}">
          <span class="scan-sym">{sym}</span>
          <span class="scan-st" style="color:{'#16a34a' if st=='BULL' else '#ef4444'};font-size:10px">{st}</span>
          <span class="scan-tqi">TQI <b style="color:{tqi_col}">{tqi_str}</b></span>
          <span class="scan-er" style="color:#888;font-size:10px">ER {er_str}</span>
          <span class="scan-reason" style="color:{row_col};font-weight:600;font-size:11px">{reason}</span>
          <span class="age">{age_str}</span>
        </div>'''
    if not scan_detail_html:
        scan_detail_html = '<div class="empty">Scanning...</div>'

    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>APEX Pro v5</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#f5f2ed;color:#1a1a1a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:16px;max-width:480px;margin:0 auto}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}}
.logo{{font-size:20px;font-weight:700;letter-spacing:-.5px}}
.logo span{{color:#16a34a}}
.logo small{{font-size:11px;color:#888;font-weight:400;margin-left:6px}}
.status{{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:600;color:#16a34a}}
.dot{{width:7px;height:7px;background:#16a34a;border-radius:50%;animation:pulse 1.5s infinite}}
.dd-warning{{background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:10px 14px;margin-bottom:12px;font-size:13px;color:#92400e}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}}
.card{{background:#fff;border-radius:12px;padding:14px;border:1px solid #e5e5e5}}
.card-label{{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#888;margin-bottom:6px}}
.card-val{{font-size:22px;font-weight:700;font-family:'Courier New',monospace}}
.card-sub{{font-size:11px;color:#888;margin-top:3px}}
.sec-title{{font-size:10px;text-transform:uppercase;letter-spacing:.12em;color:#888;margin:20px 0 10px;display:flex;align-items:center;gap:8px}}
.sec-title::after{{content:'';flex:1;height:1px;background:#e5e5e5}}
.card.pos-card{{border-left:3px solid #16a34a;padding:12px}}
.row{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:13px}}
.mt8{{margin-top:8px}}.mt4{{margin-top:4px}}
.badge{{font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px}}
.long{{background:rgba(22,163,74,.1);color:#16a34a}}
.score-badge{{font-size:11px;color:#ca8a04;margin-left:auto}}
.tag{{font-size:10px;font-weight:600;padding:2px 8px;border-radius:20px}}
.tag-blocked{{background:#fee2e2;color:#dc2626}}
.tag-signal{{background:#dcfce7;color:#16a34a}}
.log-row{{display:flex;align-items:center;gap:8px;padding:9px 0;border-bottom:1px solid #f0f0f0;flex-wrap:wrap}}
.log-row:last-child{{border-bottom:none}}
.sym{{font-weight:700;font-size:13px;min-width:60px}}
.age{{font-size:11px;color:#aaa;margin-left:auto}}
.empty{{text-align:center;padding:20px;color:#aaa;font-size:13px}}
.sys-row{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0f0;font-size:13px}}
.sys-row:last-child{{border-bottom:none}}
.sys-row span:first-child{{color:#888}}
.sys-row span:last-child{{font-weight:600}}
.refresh{{text-align:center;margin-top:20px;font-size:11px;color:#aaa}}
</style>
</head><body>
<div class="header">
  <div class="logo">APEX <span>Pro</span><small>v5</small></div>
  <div class="status"><span class="dot"></span>{state["status"].upper()}</div>
</div>
{dd_warning}
<button class="{btn_class}" onclick="fetch('/api/toggle',{{method:'POST'}}).then(()=>location.reload())">{btn_label}</button>
<div class="grid">
  <div class="card">
    <div class="card-label">Total P&L</div>
    <div class="card-val" style="color:{pnl_col(pnl)}">{'+' if pnl>=0 else ''}${fmt(pnl)}</div>
    <div class="card-sub">{total} trades closed</div>
  </div>
  <div class="card">
    <div class="card-label">Win Rate</div>
    <div class="card-val" style="color:#ca8a04">{wr}</div>
    <div class="card-sub">{state['wins']}W / {state['losses']}L</div>
  </div>
  <div class="card">
    <div class="card-label">Avg Win</div>
    <div class="card-val" style="color:#22c55e">+${fmt(avg_win)}</div>
    <div class="card-sub">per trade</div>
  </div>
  <div class="card">
    <div class="card-label">Avg Loss</div>
    <div class="card-val" style="color:#ef4444">-${fmt(abs(avg_loss))}</div>
    <div class="card-sub">per trade</div>
  </div>
</div>
<div class="card">
  <div class="sys-row"><span>Balance</span><span>${fmt(bal)} USDT</span></div>
  <div class="sys-row"><span>Open Positions</span><span>{len(state['positions'])} / {MAX_POS}</span></div>
  <div class="sys-row"><span>Coins</span><span>{', '.join(c.replace('USDT','') for c in COINS)}</span></div>
  <div class="sys-row"><span>Strategy</span><span>SATS Long-Only | TQI≥{TQI_MIN}</span></div>
  <div class="sys-row"><span>Risk/Trade</span><span>{RISK_PCT}% | SL {SL_ATR_MULT}x ATR</span></div>
  <div class="sys-row"><span>Last Scan</span><span>{age}s ago</span></div>
  <div class="sys-row"><span>DD Protection</span>
    <span style="color:{'#ef4444' if state['dd_paused'] else '#16a34a'}">
      {'PAUSED' if state['dd_paused'] else f'Active ({DAILY_DD_LIMIT}% limit)'}</span></div>
</div>
<div class="sec-title">Open Positions</div>
{pos_html}
<div class="sec-title">Scan Log — Per Coin Scores</div>
<div class="card">{scan_detail_html}</div>
<div class="sec-title">Activity Log</div>
<div class="card">{log_html}</div>
<div class="sec-title">Recent Trades</div>
<div class="card">{trades_html}</div>
<div class="refresh">Auto-refreshes every 30s</div>
<meta http-equiv="refresh" content="30">
</body></html>"""

    return HTMLResponse(html)

# ── ENTRY POINT ───────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
    )
