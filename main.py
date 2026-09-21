import argparse
import asyncio
import logging
import os
import signal
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

from pybit.unified_trading import HTTP

from bybit_api_manager import BybitAPIManager
from strategy_scanner import MarketScanner, Signal
from order_executor import OrderExecutor
from risk_manager import BotMode, CloseReason, RiskManager
import seed_patterns

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("UNICO")

CONFIG: Dict[str, Any] = {
    "API_KEY": os.getenv("BYBIT_API_KEY", ""),
    "API_SECRET": os.getenv("BYBIT_API_SECRET", ""),
    "SANDBOX": os.getenv("BYBIT_SANDBOX", "false").lower() == "true",
    "MODE": os.getenv("BOT_MODE", "DRY_RUN"),

    "SCANNER_ENABLED": True,
    "SCAN_INTERVAL": float(os.getenv("SCAN_INTERVAL", "20.0")),
    "MIN_SCORE": float(os.getenv("MIN_SCORE", "0.15")),
    "MIN_RR": float(os.getenv("MIN_RR", "0.8")),

    "TIMEFRAMES": ["15m", "3m"],

    "MAX_POSITIONS": int(os.getenv("MAX_POSITIONS", "3")),
    "POSITION_PCT": float(os.getenv("POSITION_PCT", "0.30")),
    "SL_PCT": float(os.getenv("SL_PCT", "0.15")),
    "TP_MULTIPLE": float(os.getenv("TP_MULTIPLE", "5.0")),
    "LEVERAGE": int(os.getenv("LEVERAGE", "10")),
    "COOLDOWN_MINUTES": int(os.getenv("COOLDOWN_MINUTES", "5")),
    "MAX_ENTRIES_DAILY": int(os.getenv("MAX_ENTRIES_DAILY", "20")),

    "LEARNING_ENABLED": os.getenv("LEARNING_ENABLED", "true").lower() == "true",

    "DB_PATH": os.getenv("DB_PATH", "patterns.db"),
    "CAPITAL_FILE": os.getenv("CAPITAL_FILE", "capital_inicial.json"),

    "WATCHLIST": [],

    "TRAILING_ACTIVATED": os.getenv("TRAILING_ACTIVATED", "true").lower() == "true",
    "TRAILING_CALLBACK_PCT": float(os.getenv("TRAILING_CALLBACK_PCT", "0.005")),
}

FALLBACK_WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT", "MATICUSDT"
]


class TrailingStopManager:
    def __init__(self, api_manager, callback_atr_mult: float = 2.5, is_active: bool = True):
        self.api = api_manager
        self.callback_atr_mult = callback_atr_mult
        self.is_active = is_active
        self._tracked_positions = {}

    async def manage(self, symbol: str, side: str, atr: float = 0.0) -> None:
        if not self.is_active or atr <= 0:
            return
        try:
            response = await self.api.get_positions(symbol=symbol)
            if isinstance(response, dict) and "list" in response:
                positions = response["list"]
            elif isinstance(response, list):
                positions = response
            else:
                return
            if not positions:
                return
            pos = positions[0]
            if float(pos.get("size", 0)) == 0:
                return
            current_price = float(pos.get("markPrice", 0))
            current_sl = float(pos.get("stopLoss", 0))
            entry_price = float(pos.get("avgPrice", 0))
            callback = atr * self.callback_atr_mult
            if side == "LONG":
                best = self._tracked_positions.get(symbol, entry_price)
                if current_price > best:
                    best = current_price
                    self._tracked_positions[symbol] = best
                new_sl = best - callback
                if new_sl > current_sl and new_sl > entry_price:
                    await self.api.place_order(symbol=symbol, side="buy", order_type="limit",
                                               amount=0, price=None, stop_loss=new_sl,
                                               take_profit=None, reduce_only=False)
            elif side == "SHORT":
                best = self._tracked_positions.get(symbol, entry_price)
                if current_price < best:
                    best = current_price
                    self._tracked_positions[symbol] = best
                new_sl = best + callback
                if new_sl < current_sl and new_sl < entry_price:
                    await self.api.place_order(symbol=symbol, side="sell", order_type="limit",
                                               amount=0, price=None, stop_loss=new_sl,
                                               take_profit=None, reduce_only=False)
        except Exception as e:
            logger.error(f"[TrailingStop] Error {symbol}: {e}")


class UnicoBot:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.mode = BotMode[config["MODE"]]

        if self.mode == BotMode.LIVE:
            if not config["API_KEY"] or not config["API_SECRET"]:
                logger.critical("Faltan credenciales Bybit")
                raise ValueError("Faltan BYBIT_API_KEY o BYBIT_API_SECRET")

        self.api = BybitAPIManager(
            api_key=config["API_KEY"],
            api_secret=config["API_SECRET"],
            sandbox=config["SANDBOX"]
        )
        self.rm = RiskManager(
            api_manager=self.api,
            mode=self.mode,
            db_path=config["DB_PATH"],
            max_positions=config["MAX_POSITIONS"],
            position_pct=config.get("POSITION_PCT", 0.30),
            sl_pct=config.get("SL_PCT", 0.15),
            tp_multiple=config.get("TP_MULTIPLE", 5.0),
            leverage=config.get("LEVERAGE", 10),
            cooldown_minutes=config.get("COOLDOWN_MINUTES", 5),
            max_entries_daily=config.get("MAX_ENTRIES_DAILY", 20)
        )
        self.scanner = MarketScanner(
            api_manager=self.api,
            watchlist=[],
            scan_interval=config["SCAN_INTERVAL"],
            min_score=config["MIN_SCORE"],
            min_rr=config["MIN_RR"],
            position_pct=config["POSITION_PCT"],
            db_path=config["DB_PATH"],
            signal_cooldown_seconds=60,
            timeframes=config.get("TIMEFRAMES", ["15m", "3m"])
        )
        self.executor = OrderExecutor(api_manager=self.api, mode=self.mode)
        self.trailing_stop = TrailingStopManager(
            api_manager=self.api,
            callback_atr_mult=2.5,
            is_active=config.get("TRAILING_ACTIVATED", True)
        )

        self.running = False
        self.stats = {
            "cycles": 0, "signals": 0, "opened": 0, "closed": 0,
            "tp1_hits": 0, "tp2_hits": 0, "break_even_saves": 0,
            "started_at": datetime.now(timezone.utc).isoformat()
        }

        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    async def generate_dynamic_watchlist(self) -> List[str]:
        def to_ccxt_symbol(bybit_symbol: str) -> str:
            if bybit_symbol.endswith('USDT'):
                return f"{bybit_symbol[:-4]}/USDT:USDT"
            return bybit_symbol
        try:
            session = HTTP(testnet=False)
            response = session.get_tickers(category="linear")
            tickers = response["result"]["list"]
            sorted_24h = sorted(tickers, key=lambda x: float(x.get("price24hPcnt", 0)), reverse=True)
            top_24h = [to_ccxt_symbol(t["symbol"]) for t in sorted_24h[:15]]
            sorted_1h = sorted(tickers, key=lambda x: float(x.get("price1hPcnt", 0)), reverse=True)
            top_1h = [to_ccxt_symbol(t["symbol"]) for t in sorted_1h[:15]]
            final_watchlist = list(set(top_24h + top_1h))
            if not final_watchlist:
                return [to_ccxt_symbol(s) for s in FALLBACK_WATCHLIST]
            return final_watchlist
        except Exception as e:
            logger.error(f"Error watchlist: {e}")
            return [to_ccxt_symbol(s) for s in FALLBACK_WATCHLIST]

    async def initialize(self) -> None:
        logger.info("=" * 60)
        logger.info("UNICO STRATEGY v6.0 - ARRANCANDO")
        logger.info(f"MODO: {self.mode.value}")
        logger.info("=" * 60)
        self.config["WATCHLIST"] = await self.generate_dynamic_watchlist()
        self.scanner.watchlist = self.config["WATCHLIST"]
        logger.info(f"Watchlist: {len(self.config['WATCHLIST'])} simbolos")

        if self.mode == BotMode.DRY_RUN:
            self.rm.set_initial_balance(10000.0)
        else:
            try:
                balance = await self.api.fetch_balance()
                self.rm.set_initial_balance(balance["total"])
                logger.info(f"Balance REAL: {balance['total']:.2f} USDT")
            except Exception as e:
                logger.error(f"Error balance: {e}")
                raise

    async def run(self) -> None:
        await self.initialize()
        self.running = True
        while self.running:
            try:
                await self._cycle()
            except Exception as exc:
                logger.exception(f"Error en ciclo: {exc}")
                await asyncio.sleep(5)
        await self.shutdown()

    async def _cycle(self) -> None:
        self.stats["cycles"] += 1
        capital = await self.rm.update_capital()
        stopped, reason = self.rm.kill_switch.check(capital.total_balance)
        if stopped:
            logger.critical(f"Kill Switch: {reason}")
            self.running = False
            return

        # FIX: Solo bloquea por saldo bajo en LIVE. En DRY_RUN el scanner siempre corre.
        if self.mode == BotMode.LIVE:
            min_available = capital.total_balance * 0.05
            if capital.available < min_available:
                logger.warning(f"Saldo bajo: {capital.available:.2f} < {min_available:.2f}")

        if self.config["SCANNER_ENABLED"] and len(self.rm.positions) < self.config["MAX_POSITIONS"]:
            if self.stats["cycles"] % 15 == 0:
                self.config["WATCHLIST"] = await self.generate_dynamic_watchlist()
                self.scanner.watchlist = self.config["WATCHLIST"]

            signals = await self.scanner.scan_all()
            self.stats["signals"] += len(signals)

            if signals:
                signals.sort(key=lambda s: s.score, reverse=True)
                available_slots = self.config["MAX_POSITIONS"] - len(self.rm.positions)
                for sig in signals[:available_slots]:
                    await self._process_signal(sig)

        if self.rm.positions:
            dfs = {}
            for symbol in list(self.rm.positions.keys()):
                pos = self.rm.positions[symbol]
                tf = getattr(pos, 'timeframe', '15m')
                try:
                    dfs[symbol] = await asyncio.wait_for(
                        self.api.fetch_ohlcv(symbol, timeframe=tf, limit=100),
                        timeout=15.0
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout OHLCV {symbol}")
                    continue

            closes = await self.rm.monitor_positions(dfs)
            for symbol, (should_close, reason, notes, partial_pct) in closes.items():
                if not should_close:
                    continue
                pos = self.rm.positions.get(symbol)
                if not pos:
                    continue
                close_side = "sell" if pos.side.value == "LONG" else "buy"
                live_pos = await self.executor.check_position_exists(symbol)
                if not live_pos:
                    await self.rm.close_position(symbol, CloseReason.MANUAL, pos.current_price, 1.0)
                    continue
                contracts_to_close = pos.original_contracts * partial_pct if partial_pct < 1.0 else pos.contracts
                result = await self.executor.close_position(
                    symbol=symbol, side=close_side, contracts=contracts_to_close,
                    current_price=pos.current_price, reason=reason
                )
                if result is None or result.get("is_ghost"):
                    await self.rm.close_position(symbol, CloseReason.MANUAL, pos.current_price, 1.0)
                    continue
                await self.rm.close_position(symbol, reason, pos.current_price, partial_pct)
                if reason == CloseReason.TAKE_PROFIT_1:
                    self.stats["tp1_hits"] += 1
                elif reason == CloseReason.TAKE_PROFIT_2:
                    self.stats["tp2_hits"] += 1

        self._log_status(capital)
        await asyncio.sleep(self.config["SCAN_INTERVAL"])

    async def _process_signal(self, signal: Signal) -> None:
        try:
            df = await asyncio.wait_for(
                self.api.fetch_ohlcv(signal.symbol, timeframe=signal.timeframe, limit=100),
                timeout=15.0
            )
        except asyncio.TimeoutError:
            logger.warning(f"Timeout al procesar {signal.symbol}")
            return

        position_size = await self.rm.evaluate_entry(signal=signal, df=df)
        if not position_size:
            return

        open_side = "buy" if signal.signal_type.is_long() else "sell"
        order = await self.executor.open_position(
            symbol=signal.symbol, side=open_side, position_size=position_size,
            stop_loss=position_size.stop_loss, take_profit=position_size.take_profit,
            leverage=position_size.leverage
        )
        if not order:
            logger.error(f"Error abriendo {signal.symbol}")
            return

        atr = 0.0
        if df is not None and len(df) > 14:
            from risk_manager import PositionCalculator
            df_prep = PositionCalculator._prepare_structural_df(df)
            if not df_prep.empty:
                atr = float(df_prep["atr"].iloc[-1])

        self.rm.register_position(
            order_id=order["id"], position_size=position_size,
            pattern_id=signal.pattern_id, signal_type=signal.signal_type.value,
            arrow_color=None, score=signal.score, atr=atr
        )
        self.stats["opened"] += 1
        logger.info(f"ABIERTA {signal.symbol} {open_side}")

    def _log_status(self, capital: Any) -> None:
        logger.info(
            f"Ciclo {self.stats['cycles']} | Balance: {capital.total_balance:.2f} | "
            f"Pos: {len(self.rm.positions)} | Senales: {self.stats['signals']} | "
            f"TP1: {self.stats['tp1_hits']} | TP2: {self.stats['tp2_hits']}"
        )

    def _handle_shutdown(self, signum: int, frame: Any) -> None:
        logger.info(f"Senal {signum} recibida.")
        self.running = False

    async def shutdown(self) -> None:
        logger.info("Deteniendo bot...")
        if self.api:
            await self.api.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UNICO STRATEGY Bot")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--init-db", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if args.dry_run:
        CONFIG["MODE"] = "DRY_RUN"
    if args.live:
        CONFIG["MODE"] = "LIVE"

    if args.init_db:
        with sqlite3.connect(CONFIG['DB_PATH']) as conn:
            seed_patterns.init_db(conn)
            seed_patterns.insert_patterns(conn, seed_patterns.PATTERNS)
            seed_patterns.print_summary(conn)
        return
    if args.status:
        return
    if CONFIG["MODE"] == "LIVE":
        if not CONFIG["API_KEY"] or not CONFIG["API_SECRET"]:
            sys.exit(1)

    bot = UnicoBot(CONFIG)
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Interrupcion manual.")
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    print("UNICO STRATEGY v6.0 - INICIANDO")
    asyncio.run(main())
