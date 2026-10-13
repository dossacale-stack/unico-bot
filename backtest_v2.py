"""
backtest_v2.py — Backtest HISTÓRICO real de la estrategia UNICO
================================================================
Reemplaza a Backrest.py.

CAMBIOS CLAVE:
- Usa EXACTAMENTE la misma lógica que scanner_v2.py (sin RSI, sin EMA 21, sin BB(20,2)).
- Solo EMAs 55/144/233 + acción del precio + agotamiento + volumen.
- Sistema de caché robusto (CSV por símbolo).
- Nuevo position sizing: margen 15%, pérdida máx 40% del margen, apalancamiento dinámico (tope 20x).
- SL estructural (recent_high/recent_low), no fijo.
- Gestión de salida: TP1 (2x), TP2 (5x), break-even (3x), trailing por ATR tras TP1.

Uso:
    python backtest_v2.py
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from pybit.unified_trading import HTTP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("BacktestV2")


# ═══════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════
CONFIG = {
    "SYMBOLS": [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
        "ADAUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT", "MATICUSDT",
    ],
    "TIMEFRAME": "60",  # 1h en formato Bybit
    "TIMEFRAME_LABEL": "1h",

    "INITIAL_BALANCE": 10000.0,
    "MARGIN_PCT": 0.15,          # 15% del balance como margen
    "MAX_LOSS_PCT_OF_MARGIN": 0.40,  # 40% del margen como pérdida máxima
    "LEVERAGE_CAP": 20,          # Tope de apalancamiento

    # Indicadores (idénticos a scanner_v2.py)
    "EMA_FAST": 55,
    "EMA_MID": 144,
    "EMA_SLOW": 233,
    "BB_PERIOD": 21,
    "BB_STD": 2.0,
    "ATR_PERIOD": 14,

    # Filtros de señal (idénticos a scanner_v2.py)
    "MIN_DISTANCE_PCT": 0.01,
    "MIN_EXHAUSTION_SCORE": 0.20,
    "REJECTION_WICK_RATIO": 1.0,
    "MIN_RR": 0.8,

    # Gestión de salida
    "TP1_MULTIPLE": 2.0,
    "TP2_MULTIPLE": 5.0,
    "BREAK_EVEN_AT_R": 3.0,
    "TRAILING_ATR_MULT": 2.5,

    # Datos
    "MIN_CANDLES_REQUIRED": 500,
    "MAX_HOLD_CANDLES": 200,   # Máx velas en posición (timeout)
    "CACHE_DIR": "data_cache",
    "USE_CACHE": True,
    "CACHE_MAX_AGE_HOURS": 24,
}


# ═══════════════════════════════════════════════════════════
# CACHÉ
# ═══════════════════════════════════════════════════════════
def get_cache_path(symbol: str, interval: str) -> str:
    os.makedirs(CONFIG["CACHE_DIR"], exist_ok=True)
    return os.path.join(CONFIG["CACHE_DIR"], f"{symbol}_{interval}m.csv")


def cache_is_valid(symbol: str, interval: str) -> bool:
    if not CONFIG["USE_CACHE"]:
        return False
    path = get_cache_path(symbol, interval)
    if not os.path.exists(path):
        return False
    age_h = (time.time() - os.path.getmtime(path)) / 3600
    if age_h > CONFIG["CACHE_MAX_AGE_HOURS"]:
        logger.info(f"  {symbol}: caché con {age_h:.1f}h, regenerando...")
        return False
    return True


def load_from_cache(symbol: str, interval: str) -> pd.DataFrame:
    path = get_cache_path(symbol, interval)
    df = pd.read_csv(path, index_col="datetime", parse_dates=True)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def save_to_cache(symbol: str, interval: str, df: pd.DataFrame):
    path = get_cache_path(symbol, interval)
    df.to_csv(path)
    size_mb = os.path.getsize(path) / (1024 * 1024)
    logger.info(f"  {symbol}: caché guardada ({size_mb:.1f} MB)")


# ═══════════════════════════════════════════════════════════
# DESCARGA DE HISTORIAL COMPLETO
# ═══════════════════════════════════════════════════════════
def fetch_all_history(symbol: str, interval: str) -> pd.DataFrame:
    if cache_is_valid(symbol, interval):
        logger.info(f"  {symbol}: cargando de caché...")
        df = load_from_cache(symbol, interval)
        logger.info(f"  {symbol}: {len(df)} velas desde caché")
        return df

    session = HTTP(testnet=False)
    all_candles = []
    cursor_end = int(datetime.now().timestamp() * 1000)
    interval_ms = int(interval) * 60 * 1000

    logger.info(f"  {symbol}: descargando historia completa...")
    batch_num = 0
    earliest_ts = None
    stall_count = 0

    while True:
        batch_num += 1
        try:
            resp = session.get_kline(
                category="linear",
                symbol=symbol,
                interval=interval,
                end=cursor_end,
                limit=1000,
            )
            batch = resp["result"]["list"]
            if not batch:
                break

            all_candles.extend(batch)
            new_earliest = int(batch[-1][0])

            if earliest_ts is not None and new_earliest >= earliest_ts:
                stall_count += 1
                if stall_count > 3:
                    break
            else:
                stall_count = 0
            earliest_ts = new_earliest

            if batch_num % 10 == 0:
                dt = pd.to_datetime(new_earliest, unit="ms", utc=True)
                logger.info(f"  {symbol}: batch {batch_num} | {len(all_candles)} velas | hasta {dt.date()}")

            if len(batch) < 1000:
                break

            cursor_end = new_earliest - interval_ms
            if cursor_end < 1514764800000:
                break

            time.sleep(0.15)

        except Exception as e:
            logger.error(f"  Error {symbol} batch {batch_num}: {e}")
            time.sleep(2)
            if batch_num > 5:
                break
            continue

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(
        all_candles,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
    )
    df["datetime"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms", utc=True)
    df.set_index("datetime", inplace=True)
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="first")]
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df = df[["open", "high", "low", "close", "volume"]]

    save_to_cache(symbol, interval, df)
    return df


# ═══════════════════════════════════════════════════════════
# INDICADORES (idénticos a scanner_v2.py)
# ═══════════════════════════════════════════════════════════
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=CONFIG["EMA_FAST"], adjust=False).mean()
    df["ema_mid"] = df["close"].ewm(span=CONFIG["EMA_MID"], adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=CONFIG["EMA_SLOW"], adjust=False).mean()

    df["bb_mid"] = df["close"].rolling(CONFIG["BB_PERIOD"]).mean()
    df["bb_std"] = df["close"].rolling(CONFIG["BB_PERIOD"]).std()
    df["bb_upper"] = df["bb_mid"] + CONFIG["BB_STD"] * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - CONFIG["BB_STD"] * df["bb_std"]
    df["bb_bandwidth"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

    df["tr"] = np.maximum(
        df["high"] - df["low"],
        np.maximum(
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"] - df["close"].shift(1)).abs(),
        ),
    )
    df["atr"] = df["tr"].rolling(CONFIG["ATR_PERIOD"]).mean()

    df["vol_avg"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_avg"].replace(0, 1e-9)
    return df


# ═══════════════════════════════════════════════════════════
# LÓGICA DE SEÑAL (idéntica a scanner_v2.py)
# ═══════════════════════════════════════════════════════════
def detect_ema_stack(row: pd.Series, price: float) -> str:
    ef = float(row["ema_fast"])
    em = float(row["ema_mid"])
    es = float(row["ema_slow"])
    if ef > em and em > es and price > ef:
        return "BULLISH"
    if ef < em and em < es and price < ef:
        return "BEARISH"
    return "TANGLED"


def exhaustion_score(df: pd.DataFrame, idx: int, direction: str) -> Dict:
    row = df.iloc[idx]
    price = float(row["close"])
    ema_fast = float(row["ema_fast"])

    distance_pct = (price - ema_fast) / ema_fast if ema_fast > 0 else 0
    if direction == "UP":
        dist_score = min(max(distance_pct - CONFIG["MIN_DISTANCE_PCT"], 0) / 0.08, 1.0)
    else:
        dist_score = min(max(-distance_pct - CONFIG["MIN_DISTANCE_PCT"], 0) / 0.08, 1.0)

    closes = df["close"].values
    consecutive = 0
    for i in range(idx - 1, max(idx - 20, 0), -1):
        if direction == "UP" and closes[i] > closes[i - 1]:
            consecutive += 1
        elif direction == "DOWN" and closes[i] < closes[i - 1]:
            consecutive += 1
        else:
            break
    consec_score = min(consecutive / 8.0, 1.0)

    bw_now = float(row["bb_bandwidth"])
    bw_10 = float(df["bb_bandwidth"].iloc[idx - 10]) if idx >= 10 else bw_now
    bb_expansion = (bw_now - bw_10) / bw_10 if bw_10 > 0 else 0.0
    bb_score = min(max(bb_expansion, 0) / 0.30, 1.0)

    threshold_pct = 0.015 if direction == "UP" else -0.015
    start = max(0, idx - 30)
    candles_in_zone = 0
    for j in range(start, idx):
        r = df.iloc[j]
        p = float(r["close"])
        ef = float(r["ema_fast"])
        if ef <= 0:
            continue
        d = (p - ef) / ef
        if direction == "UP" and d > threshold_pct:
            candles_in_zone += 1
        elif direction == "DOWN" and d < threshold_pct:
            candles_in_zone += 1
    time_score = min(candles_in_zone / 15.0, 1.0)

    score = (
        dist_score * 0.35
        + consec_score * 0.20
        + bb_score * 0.25
        + time_score * 0.20
    )

    return {
        "score": score,
        "distance_pct": distance_pct,
        "consecutive": consecutive,
        "bb_expansion": bb_expansion,
        "candles_in_zone": candles_in_zone,
    }


def detect_rejection_bullish(row: pd.Series) -> bool:
    o, c, h, l = float(row["open"]), float(row["close"]), float(row["high"]), float(row["low"])
    body = abs(c - o) or (h - l) * 0.1
    lower_wick = min(o, c) - l
    return lower_wick > body * CONFIG["REJECTION_WICK_RATIO"] and c >= o * 0.995


def detect_rejection_bearish(row: pd.Series) -> bool:
    o, c, h, l = float(row["open"]), float(row["close"]), float(row["high"]), float(row["low"])
    body = abs(c - o) or (h - l) * 0.1
    upper_wick = h - max(o, c)
    return upper_wick > body * CONFIG["REJECTION_WICK_RATIO"] and c <= o * 1.005


def analyze_signal(df: pd.DataFrame, idx: int) -> Optional[Dict]:
    if idx < 2 or idx >= len(df) - 1:
        return None

    last = df.iloc[idx]
    prev = df.iloc[idx - 1]

    if pd.isna(last["ema_slow"]) or pd.isna(last["bb_mid"]):
        return None

    price = float(last["close"])
    bb_upper = float(last["bb_upper"])
    bb_lower = float(last["bb_lower"])
    stack = detect_ema_stack(last, price)

    if stack == "TANGLED":
        return None

    if stack == "BEARISH":
        ex = exhaustion_score(df, idx, "UP")
        if ex["score"] < CONFIG["MIN_EXHAUSTION_SCORE"]:
            return None
        if not (detect_rejection_bearish(last) or detect_rejection_bearish(prev)):
            return None

        recent_high = float(df["high"].iloc[idx - 9:idx + 1].max())
        stop_loss = recent_high * 1.003
        take_profit = bb_lower
        risk = abs(price - stop_loss)
        reward = abs(take_profit - price)
        if risk <= 0:
            return None
        rr = reward / risk
        if rr < CONFIG["MIN_RR"]:
            return None

        return {
            "side": "SHORT",
            "price": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "rr": rr,
            "score": ex["score"],
            "atr": float(last["atr"]) if not pd.isna(last["atr"]) else 0.0,
            "exhaustion": ex,
        }

    if stack == "BULLISH":
        ex = exhaustion_score(df, idx, "DOWN")
        if ex["score"] < CONFIG["MIN_EXHAUSTION_SCORE"]:
            return None
        if not (detect_rejection_bullish(last) or detect_rejection_bullish(prev)):
            return None

        recent_low = float(df["low"].iloc[idx - 9:idx + 1].min())
        stop_loss = recent_low * 0.997
        take_profit = bb_upper
        risk = abs(price - stop_loss)
        reward = abs(take_profit - price)
        if risk <= 0:
            return None
        rr = reward / risk
        if rr < CONFIG["MIN_RR"]:
            return None

        return {
            "side": "LONG",
            "price": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "rr": rr,
            "score": ex["score"],
            "atr": float(last["atr"]) if not pd.isna(last["atr"]) else 0.0,
            "exhaustion": ex,
        }

    return None


# ═══════════════════════════════════════════════════════════
# POSITION SIZING
# ═══════════════════════════════════════════════════════════
def calculate_position(
    balance: float,
    entry: float,
    stop_loss: float,
    leverage: int,
) -> Dict:
    """
    Margen = 15% del balance
    Pérdida máx = 40% del margen
    Leverage = dinámico, tope 20x
    SL estructural (recent_high/low) → ajusta tamaño si es necesario
    """
    margin = balance * CONFIG["MARGIN_PCT"]
    max_loss_usd = margin * CONFIG["MAX_LOSS_PCT_OF_MARGIN"]

    sl_pct = abs(entry - stop_loss) / entry if entry > 0 else 0.01
    if sl_pct <= 0:
        return {"contracts": 0, "notional": 0, "margin": margin, "max_loss": 0}

    # Notional según riesgo permitido
    notional_from_risk = max_loss_usd / sl_pct
    # Notional máximo por margen y apalancamiento
    notional_from_margin = margin * leverage
    # El menor de los dos
    notional = min(notional_from_risk, notional_from_margin)
    contracts = notional / entry

    return {
        "contracts": contracts,
        "notional": notional,
        "margin": margin,
        "max_loss": max_loss_usd,
        "sl_pct": sl_pct,
        "leverage_used": notional / margin if margin > 0 else 0,
    }


# ═══════════════════════════════════════════════════════════
# SIMULACIÓN DE TRADE
# ═══════════════════════════════════════════════════════════
def simulate_trade(
    df: pd.DataFrame,
    entry_idx: int,
    signal: Dict,
    balance: float,
    leverage: int,
) -> Dict:
    entry = signal["price"]
    side = signal["side"]
    sl = signal["stop_loss"]
    atr_entry = signal["atr"]

    # Calcular TP1, TP2, break-even
    risk_dist = abs(entry - sl)
    if side == "LONG":
        tp1 = entry + risk_dist * CONFIG["TP1_MULTIPLE"]
        tp2 = entry + risk_dist * CONFIG["TP2_MULTIPLE"]
        be_trigger = entry + risk_dist * CONFIG["BREAK_EVEN_AT_R"]
    else:
        tp1 = entry - risk_dist * CONFIG["TP1_MULTIPLE"]
        tp2 = entry - risk_dist * CONFIG["TP2_MULTIPLE"]
        be_trigger = entry - risk_dist * CONFIG["BREAK_EVEN_AT_R"]

    pos = calculate_position(balance, entry, sl, leverage)
    if pos["contracts"] <= 0:
        return None

    contracts = pos["contracts"]
    remaining = contracts
    realized_pnl = 0.0
    highest = entry
    lowest = entry
    current_sl = sl
    tp1_hit = False
    tp2_hit = False
    break_even_set = False
    exit_reason = "TIMEOUT"
    exit_price = entry

    for j in range(entry_idx + 1, min(entry_idx + CONFIG["MAX_HOLD_CANDLES"], len(df))):
        candle = df.iloc[j]
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        atr_now = float(candle["atr"]) if not pd.isna(candle["atr"]) else atr_entry

        if side == "LONG":
            highest = max(highest, high)
        else:
            lowest = min(lowest, low)

        # Break-even
        if not break_even_set:
            if side == "LONG" and high >= be_trigger:
                current_sl = entry * 1.001
                break_even_set = True
            elif side == "SHORT" and low <= be_trigger:
                current_sl = entry * 0.999
                break_even_set = True

        # SL
        if side == "LONG" and low <= current_sl:
            pnl = (current_sl - entry) / entry * remaining * entry
            realized_pnl += pnl
            exit_price = current_sl
            exit_reason = "SL" if not break_even_set else "BREAK_EVEN"
            remaining = 0
            break
        elif side == "SHORT" and high >= current_sl:
            pnl = (entry - current_sl) / entry * remaining * entry
            realized_pnl += pnl
            exit_price = current_sl
            exit_reason = "SL" if not break_even_set else "BREAK_EVEN"
            remaining = 0
            break

        # TP1 (25% parcial)
        if not tp1_hit:
            if (side == "LONG" and high >= tp1) or (side == "SHORT" and low <= tp1):
                portion = contracts * 0.25
                pnl = (tp1 - entry) / entry * portion * entry if side == "LONG" else (entry - tp1) / entry * portion * entry
                realized_pnl += pnl
                remaining -= portion
                tp1_hit = True

        # TP2 (25% parcial)
        if tp1_hit and not tp2_hit:
            if (side == "LONG" and high >= tp2) or (side == "SHORT" and low <= tp2):
                portion = contracts * 0.25
                pnl = (tp2 - entry) / entry * portion * entry if side == "LONG" else (entry - tp2) / entry * portion * entry
                realized_pnl += pnl
                remaining -= portion
                tp2_hit = True

        # Trailing por ATR (después de TP1)
        if tp1_hit and atr_now > 0:
            callback = atr_now * CONFIG["TRAILING_ATR_MULT"]
            if side == "LONG":
                new_sl = highest - callback
                if new_sl > current_sl:
                    current_sl = new_sl
            else:
                new_sl = lowest + callback
                if new_sl < current_sl:
                    current_sl = new_sl

    # Cerrar remanente al final
    if remaining > 0:
        final_candle = df.iloc[min(entry_idx + CONFIG["MAX_HOLD_CANDLES"], len(df) - 1)]
        exit_price = float(final_candle["close"])
        if side == "LONG":
            pnl = (exit_price - entry) / entry * remaining * entry
        else:
            pnl = (entry - exit_price) / entry * remaining * entry
        realized_pnl += pnl
        if exit_reason == "TIMEOUT":
            exit_reason = "TIMEOUT"

    # PnL en USDT (la porción ya está en notional × %)
    # Convertir a PnL real con apalancamiento implícito
    # El notional ya incluye el apalancamiento del margen
    pnl_pct_price = (
        (exit_price - entry) / entry if side == "LONG" else (entry - exit_price) / entry
    )
    pnl_usd = realized_pnl  # Ya está calculado en notional

    return {
        "entry_time": df.index[entry_idx],
        "exit_time": df.index[min(entry_idx + CONFIG["MAX_HOLD_CANDLES"], len(df) - 1)],
        "side": side,
        "entry": entry,
        "exit": exit_price,
        "sl_initial": sl,
        "tp1": tp1,
        "tp2": tp2,
        "score": signal["score"],
        "rr": signal["rr"],
        "contracts": contracts,
        "notional": pos["notional"],
        "margin": pos["margin"],
        "pnl_usd": pnl_usd,
        "pnl_pct_of_balance": pnl_usd / balance * 100 if balance > 0 else 0,
        "exit_reason": exit_reason,
        "tp1_hit": tp1_hit,
        "tp2_hit": tp2_hit,
        "break_even": break_even_set,
        "is_win": pnl_usd > 0,
        "weekday": df.index[entry_idx].weekday(),
        "hour": df.index[entry_idx].hour,
    }


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
async def run_backtest():
    config = CONFIG
    logger.info("=" * 70)
    logger.info(f"BACKTEST V2 | {config['TIMEFRAME_LABEL']} | {len(config['SYMBOLS'])} símbolos")
    logger.info(f"Margin: {config['MARGIN_PCT']*100:.0f}% | Max loss: {config['MAX_LOSS_PCT_OF_MARGIN']*100:.0f}% del margen")
    logger.info(f"Leverage cap: {config['LEVERAGE_CAP']}x | Cache: {'ON' if config['USE_CACHE'] else 'OFF'}")
    logger.info("=" * 70)

    all_trades = []

    for symbol in config["SYMBOLS"]:
        logger.info(f"\n>> Procesando {symbol}...")
        df = fetch_all_history(symbol, config["TIMEFRAME"])
        if df.empty or len(df) < config["MIN_CANDLES_REQUIRED"]:
            logger.warning(f"  {symbol}: datos insuficientes")
            continue

        logger.info(f"  {symbol}: {len(df)} velas | {df.index[0].date()} a {df.index[-1].date()}")
        df = add_indicators(df)
        df = df.dropna(subset=["ema_slow", "bb_mid", "atr"])

        # Simular balance evolutivo
        balance = config["INITIAL_BALANCE"]
        # Asumir apalancamiento = 20x (tope). En real sería dinámico.
        leverage = config["LEVERAGE_CAP"]

        i = 250  # Empezar después de tener indicadores
        while i < len(df) - 1:
            sig = analyze_signal(df, i)
            if sig is None:
                i += 1
                continue

            trade = simulate_trade(df, i, sig, balance, leverage)
            if trade is None:
                i += 1
                continue

            trade["symbol"] = symbol
            balance += trade["pnl_usd"]
            all_trades.append(trade)

            # Avanzar índice: saltar velas que estén dentro del trade
            # (para no solapar operaciones en el mismo símbolo)
            hold_candles = min(config["MAX_HOLD_CANDLES"], len(df) - i - 1)
            i += max(hold_candles, 1)

        logger.info(f"  {symbol}: {len([t for t in all_trades if t['symbol'] == symbol])} trades")

    # ═══════════════════════════════════════════════════════
    # REPORTE FINAL
    # ═══════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("REPORTE FINAL — BACKTEST V2 (EMAs 55/144/233 + Acción del Precio)")
    print("=" * 70)

    if not all_trades:
        print("No se generó ningún trade.")
        return

    df_t = pd.DataFrame(all_trades)
    total = len(df_t)
    wins = df_t[df_t["is_win"]]
    losses = df_t[~df_t["is_win"]]

    total_pnl = df_t["pnl_usd"].sum()
    win_rate = len(wins) / total * 100
    gross_profit = wins["pnl_usd"].sum() if len(wins) > 0 else 0
    gross_loss = abs(losses["pnl_usd"].sum()) if len(losses) > 0 else 1
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0
    final_balance = config["INITIAL_BALANCE"] + total_pnl
    roi = total_pnl / config["INITIAL_BALANCE"] * 100
    avg_win = wins["pnl_usd"].mean() if len(wins) > 0 else 0
    avg_loss = losses["pnl_usd"].mean() if len(losses) > 0 else 0

    print(f"\nBalance inicial: ${config['INITIAL_BALANCE']:,.2f}")
    print(f"Balance final:   ${final_balance:,.2f}")
    print(f"PnL total:       ${total_pnl:+,.2f} ({roi:+.2f}%)")
    print(f"\nTotal trades: {total}")
    print(f"Wins:  {len(wins)} ({win_rate:.1f}%) | Avg: ${avg_win:+,.2f}")
    print(f"Losses: {len(losses)} ({100 - win_rate:.1f}%) | Avg: ${avg_loss:+,.2f}")
    print(f"Profit Factor: {profit_factor:.2f}")
    print(f"Esperanza por trade: ${total_pnl / total:+,.2f}")

    print("\n--- Por símbolo ---")
    for symbol in config["SYMBOLS"]:
        s = df_t[df_t["symbol"] == symbol]
        if len(s) == 0:
            continue
        wr = len(s[s["is_win"]]) / len(s) * 100
        print(f"  {symbol}: {len(s):3d} trades | WR {wr:5.1f}% | PnL ${s['pnl_usd'].sum():+9.2f}")

    print("\n--- Por lado ---")
    for side in ["LONG", "SHORT"]:
        s = df_t[df_t["side"] == side]
        if len(s) == 0:
            continue
        wr = len(s[s["is_win"]]) / len(s) * 100
        print(f"  {side}: {len(s):3d} trades | WR {wr:5.1f}% | PnL ${s['pnl_usd'].sum():+9.2f}")

    print("\n--- Por razón de salida ---")
    for reason in df_t["exit_reason"].unique():
        s = df_t[df_t["exit_reason"] == reason]
        print(f"  {reason}: {len(s):3d} trades | PnL ${s['pnl_usd'].sum():+9.2f}")

    print("\n--- Por día de la semana ---")
    day_names = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    for d in range(7):
        s = df_t[df_t["weekday"] == d]
        if len(s) == 0:
            continue
        wr = len(s[s["is_win"]]) / len(s) * 100
        print(f"  {day_names[d]:10s}: {len(s):3d} trades | WR {wr:5.1f}% | PnL ${s['pnl_usd'].sum():+9.2f}")

    print("\n--- Por hora (UTC) ---")
    for h in range(0, 24, 4):
        s = df_t[(df_t["hour"] >= h) & (df_t["hour"] < h + 4)]
        if len(s) == 0:
            continue
        wr = len(s[s["is_win"]]) / len(s) * 100
        print(f"  {h:02d}:00-{h+3:02d}:59: {len(s):3d} trades | WR {wr:5.1f}% | PnL ${s['pnl_usd'].sum():+9.2f}")

    print("\n--- Por rango de score ---")
    bins = [(0.20, 0.30), (0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 1.00)]
    for lo, hi in bins:
        s = df_t[(df_t["score"] >= lo) & (df_t["score"] < hi)]
        if len(s) == 0:
            continue
        wr = len(s[s["is_win"]]) / len(s) * 100
        print(f"  Score {lo:.2f}-{hi:.2f}: {len(s):3d} trades | WR {wr:5.1f}% | PnL ${s['pnl_usd'].sum():+9.2f}")

    print("\n" + "=" * 70)
    print("💾 Guardando trades en backtest_trades.csv")
    df_t.to_csv("backtest_trades.csv", index=False)
    print("   Usa ese CSV para el análisis de patrones de vela (pattern_learner).")


if __name__ == "__main__":
    asyncio.run(run_backtest())
