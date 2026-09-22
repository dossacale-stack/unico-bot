"""
Backtest HISTORICO COMPLETO con cache.
- Primera corrida: descarga todo (~20-30 min)
- Siguientes corridas: carga desde cache (segundos)
Uso: python backtest.py
"""
import asyncio
import logging
import os
import time
from datetime import datetime
from typing import List, Dict

import numpy as np
import pandas as pd
from pybit.unified_trading import HTTP

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Backtest")


# ═══════════════════════════════════════════════════════════
# CONFIGURACION
# ═══════════════════════════════════════════════════════════
CONFIG = {
    "SYMBOLS": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
                "ADAUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT", "MATICUSDT"],
    "TIMEFRAME": "15",
    "INITIAL_BALANCE": 10000,
    "POSITION_PCT": 0.30,
    "SL_PCT": 0.15,
    "TP1_MULTIPLE": 2.0,
    "TP2_MULTIPLE": 5.0,
    "LEVERAGE": 10,
    "MIN_SCORE": 0.15,
    "MIN_RR": 0.8,
    "USE_BTC_REGIME_FILTER": True,
    "BTC_REGIME_THRESHOLD": 0.01,
    "MIN_CANDLES_REQUIRED": 500,
    "CACHE_DIR": "data_cache",       # Carpeta de cache
    "USE_CACHE": True,                # Usar cache si existe
    "CACHE_MAX_AGE_HOURS": 24,        # Regenerar si tiene mas de X horas
}


# ═══════════════════════════════════════════════════════════
# CACHE
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
    age_hours = (time.time() - os.path.getmtime(path)) / 3600
    if age_hours > CONFIG["CACHE_MAX_AGE_HOURS"]:
        logger.info(f"  Cache de {symbol} tiene {age_hours:.1f}h, regenerando...")
        return False
    return True


def load_from_cache(symbol: str, interval: str) -> pd.DataFrame:
    path = get_cache_path(symbol, interval)
    df = pd.read_csv(path, index_col="datetime", parse_dates=True)
    return df


def save_to_cache(symbol: str, interval: str, df: pd.DataFrame):
    path = get_cache_path(symbol, interval)
    df.to_csv(path)
    size_mb = os.path.getsize(path) / (1024 * 1024)
    logger.info(f"  {symbol}: guardado en cache ({size_mb:.1f} MB)")


# ═══════════════════════════════════════════════════════════
# DESCARGA
# ═══════════════════════════════════════════════════════════
def fetch_all_history(symbol: str, interval: str) -> pd.DataFrame:
    if cache_is_valid(symbol, interval):
        logger.info(f"  {symbol}: cargando desde cache...")
        df = load_from_cache(symbol, interval)
        logger.info(f"  {symbol}: {len(df)} velas desde cache")
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

    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"])
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
# INDICADORES
# ═══════════════════════════════════════════════════════════
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    df["rsi"] = 100 - (100 / (1 + rs))
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema55"] = df["close"].ewm(span=55, adjust=False).mean()
    df["bb_mid"] = df["close"].rolling(20).mean()
    df["bb_std"] = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - 2 * df["bb_std"]
    df["tr"] = np.maximum(
        df["high"] - df["low"],
        np.maximum(abs(df["high"] - df["close"].shift(1)), abs(df["low"] - df["close"].shift(1)))
    )
    df["atr"] = df["tr"].rolling(14).mean()
    df["vol_avg"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_avg"].replace(0, 1e-9)
    return df


# ═══════════════════════════════════════════════════════════
# SEÑALES
# ═══════════════════════════════════════════════════════════
def generate_signals(df: pd.DataFrame, min_score: float, min_rr: float) -> List[Dict]:
    signals = []
    for i in range(60, len(df) - 1):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        if pd.isna(row["rsi"]) or pd.isna(row["atr"]):
            continue

        price = float(row["close"])
        rsi = float(row["rsi"])
        atr = float(row["atr"])
        ema21 = float(row["ema21"])
        ema55 = float(row["ema55"])
        bb_upper = float(row["bb_upper"])
        bb_lower = float(row["bb_lower"])
        vol_ratio = float(row["vol_ratio"])

        score = 0.0
        side = None

        if rsi < 35 and price <= bb_lower * 1.01:
            side = "LONG"; score += 0.4
        elif rsi > 65 and price >= bb_upper * 0.99:
            side = "SHORT"; score += 0.4
        elif float(prev["ema21"]) <= float(prev["ema55"]) and ema21 > ema55:
            side = "LONG"; score += 0.35
        elif float(prev["ema21"]) >= float(prev["ema55"]) and ema21 < ema55:
            side = "SHORT"; score += 0.35

        if side is None:
            continue

        if vol_ratio > 1.3:
            score += 0.2
        atr_pct = atr / price if price > 0 else 0
        if 0.002 < atr_pct < 0.05:
            score += 0.1

        if score < min_score:
            continue

        if side == "LONG":
            sl = price - (atr * 1.5); tp = price + (atr * 3.0)
        else:
            sl = price + (atr * 1.5); tp = price - (atr * 3.0)

        risk = abs(price - sl)
        reward = abs(tp - price)
        rr = reward / risk if risk > 0 else 0
        if rr < min_rr:
            continue

        signals.append({
            "index": i,
            "datetime": df.index[i],
            "side": side,
            "price": price,
            "sl": sl,
            "tp": tp,
            "score": score,
            "rr": rr,
        })
    return signals


# ═══════════════════════════════════════════════════════════
# SIMULACION
# ═══════════════════════════════════════════════════════════
def simulate_trade(df: pd.DataFrame, signal: Dict, balance: float, config: Dict) -> Dict:
    entry_idx = signal["index"]
    entry_price = signal["price"]
    side = signal["side"]
    sl_price = signal["sl"]
    tp_price = signal["tp"]

    position_usd = balance * config["POSITION_PCT"]
    leverage = config["LEVERAGE"]

    for j in range(entry_idx + 1, min(entry_idx + 96, len(df))):
        candle = df.iloc[j]
        high = float(candle["high"])
        low = float(candle["low"])

        if side == "LONG":
            if low <= sl_price:
                exit_price = sl_price; exit_reason = "SL"; break
            if high >= tp_price:
                exit_price = tp_price; exit_reason = "TP"; break
        else:
            if high >= sl_price:
                exit_price = sl_price; exit_reason = "SL"; break
            if low <= tp_price:
                exit_price = tp_price; exit_reason = "TP"; break
    else:
        exit_price = float(df.iloc[min(entry_idx + 96, len(df) - 1)]["close"])
        exit_reason = "TIMEOUT"

    if side == "LONG":
        pnl_pct = (exit_price - entry_price) / entry_price
    else:
        pnl_pct = (entry_price - exit_price) / entry_price

    pnl_usd = position_usd * pnl_pct * leverage

    return {
        "entry_time": signal["datetime"],
        "weekday": signal["datetime"].weekday(),
        "hour": signal["datetime"].hour,
        "side": side,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct * 100,
        "is_win": pnl_usd > 0,
        "score": signal["score"],
    }


def detect_btc_regime(btc_df: pd.DataFrame, idx: int, threshold: float, window: int = 4) -> str:
    if idx < window:
        return "FLAT"
    current = float(btc_df.iloc[idx]["close"])
    past = float(btc_df.iloc[idx - window]["close"])
    change = (current - past) / past
    if change > threshold:
        return "UP"
    elif change < -threshold:
        return "DOWN"
    return "FLAT"


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
async def run_backtest():
    config = CONFIG
    logger.info("=" * 70)
    logger.info(f"BACKTEST HISTORICO | {config['TIMEFRAME']}m | {len(config['SYMBOLS'])} simbolos")
    logger.info(f"Cache: {'ON' if config['USE_CACHE'] else 'OFF'} | Dir: {config['CACHE_DIR']}")
    logger.info("=" * 70)

    btc_df = None
    if config["USE_BTC_REGIME_FILTER"]:
        btc_df = fetch_all_history("BTCUSDT", config["TIMEFRAME"])
        if not btc_df.empty:
            logger.info(f"BTC: {len(btc_df)} velas | {btc_df.index[0].date()} a {btc_df.index[-1].date()}")

    all_trades = []

    for symbol in config["SYMBOLS"]:
        logger.info(f"\n>> Procesando {symbol}...")
        df = fetch_all_history(symbol, config["TIMEFRAME"])
        if df.empty or len(df) < config["MIN_CANDLES_REQUIRED"]:
            logger.warning(f"  {symbol}: datos insuficientes")
            continue

        logger.info(f"  {symbol}: {len(df)} velas | {df.index[0].date()} a {df.index[-1].date()}")
        df = add_indicators(df)
        signals = generate_signals(df, config["MIN_SCORE"], config["MIN_RR"])
        logger.info(f"  {symbol}: {len(signals)} señales")

        btc_aligned = None
        if btc_df is not None and not btc_df.empty:
            btc_aligned = btc_df.reindex(df.index, method="ffill")

        balance = config["INITIAL_BALANCE"]
        for sig in signals:
            if btc_aligned is not None and config["USE_BTC_REGIME_FILTER"]:
                try:
                    idx_pos = df.index.get_loc(sig["datetime"])
                except KeyError:
                    continue
                regime = detect_btc_regime(btc_aligned, idx_pos, config["BTC_REGIME_THRESHOLD"])
                if regime == "UP" and sig["side"] == "SHORT":
                    continue
                if regime == "DOWN" and sig["side"] == "LONG":
                    continue

            trade = simulate_trade(df, sig, balance, config)
            trade["symbol"] = symbol
            balance += trade["pnl_usd"]
            all_trades.append(trade)

    print("\n" + "=" * 70)
    print("REPORTE FINAL - HISTORIA COMPLETA")
    print("=" * 70)

    if not all_trades:
        print("No se genero ningun trade.")
        return

    df_trades = pd.DataFrame(all_trades)
    total = len(df_trades)
    wins = df_trades[df_trades["is_win"]]
    losses = df_trades[~df_trades["is_win"]]

    total_pnl = df_trades["pnl_usd"].sum()
    win_rate = len(wins) / total * 100
    gross_profit = wins["pnl_usd"].sum() if len(wins) > 0 else 0
    gross_loss = abs(losses["pnl_usd"].sum()) if len(losses) > 0 else 1
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0
    final_balance = config["INITIAL_BALANCE"] + total_pnl
    roi_pct = total_pnl / config["INITIAL_BALANCE"] * 100

    print(f"\nBalance inicial: ${config['INITIAL_BALANCE']:,.2f}")
    print(f"Balance final:   ${final_balance:,.2f}")
    print(f"PnL total:       ${total_pnl:+,.2f} ({roi_pct:+.2f}%)")
    print(f"\nTotal trades: {total}")
    print(f"Wins:  {len(wins)} ({win_rate:.1f}%)")
    print(f"Losses: {len(losses)} ({100 - win_rate:.1f}%)")
    print(f"Profit Factor: {profit_factor:.2f}")
    print(f"Esperanza por trade: ${total_pnl / total:+,.2f}")

    print("\n--- Por simbolo ---")
    for symbol in config["SYMBOLS"]:
        s = df_trades[df_trades["symbol"] == symbol]
        if len(s) == 0: continue
        print(f"  {symbol}: {len(s)} trades, WR {len(s[s['is_win']])/len(s)*100:.0f}%, PnL ${s['pnl_usd'].sum():+,.2f}")

    print("\n--- Por lado ---")
    for side in ["LONG", "SHORT"]:
        s = df_trades[df_trades["side"] == side]
        if len(s) == 0: continue
        print(f"  {side}: {len(s)} trades, WR {len(s[s['is_win']])/len(s)*100:.0f}%, PnL ${s['pnl_usd'].sum():+,.2f}")

    print("\n--- Por dia de la semana ---")
    day_names = ["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"]
    for d in range(7):
        s = df_trades[df_trades["weekday"] == d]
        if len(s) == 0: continue
        print(f"  {day_names[d]}: {len(s)} trades, WR {len(s[s['is_win']])/len(s)*100:.0f}%, PnL ${s['pnl_usd'].sum():+,.2f}")

    print("\n--- Por hora del dia (UTC) ---")
    for h in range(0, 24, 4):
        s = df_trades[(df_trades["hour"] >= h) & (df_trades["hour"] < h + 4)]
        if len(s) == 0: continue
        print(f"  {h:02d}:00-{h+3:02d}:59: {len(s)} trades, WR {len(s[s['is_win']])/len(s)*100:.0f}%, PnL ${s['pnl_usd'].sum():+,.2f}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    asyncio.run(run_backtest())
