import argparse
import asyncio
import logging
import os
import signal
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

from bybit_api_manager import BybitAPIManager
from market_scanner import MarketScanner, Signal
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
    "MAX_POSITIONS": 3,
    "POSITION_PCT": 0.10,
    "SL_PCT": 0.40,
    "SCAN_INTERVAL": 3.0,
    "MIN_SCORE": 0.60,
    "MIN_RR": 3.0,
    "DB_PATH": "patterns.db",
    "CAPITAL_FILE": "capital_inicial.json",
    "WATCHLIST": [
    
        "H/USDT:USDT",
        "MEGA/USDT:USDT",
        "BE/USDT:USDT",
        "QNTX/USDT:USDT",
        "AIO/USDT:USDT",
        "CLO/USDT:USDT",
        "JTO/USDT:USDT",
        "ESPORTS/USDT:USDT",
        "SKYAI1/USDT:USDT",
        "LAB/USDT:USDT",
        "PARTI/USDT:USDT",
    ],
}


class UnicoBot:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.mode = BotMode[config["MODE"]]
        self.api = BybitAPIManager(
            api_key=config["API_KEY"],
            api_secret=config["API_SECRET"],
            sandbox=config["SANDBOX"],
        )
        self.rm = RiskManager(
            api_manager=self.api,
            mode=self.mode,
            db_path=config["DB_PATH"],
            max_positions=config["MAX_POSITIONS"],
        )
        self.scanner = MarketScanner(
            api_manager=self.api,
            watchlist=config["WATCHLIST"],
            scan_interval=config["SCAN_INTERVAL"],
            min_score=config["MIN_SCORE"],
            min_rr=config["MIN_RR"],
            position_pct=config["POSITION_PCT"],
            db_path=config["DB_PATH"],
        )
        self.executor = OrderExecutor(api_manager=self.api, mode=self.mode)
        self.running = False
        self.stats = {
            "cycles": 0,
            "signals": 0,
            "opened": 0,
            "closed": 0,
            "reversals": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    async def initialize(self) -> None:
        logger.info(
            "\n" + "=" * 60 + "\n"
            + " 🚀 ÚNICO STRATEGY v2.0 — Inicializando\n"
            + f" Modo: {self.mode.value}\n"
            + f" Sandbox: {self.config['SANDBOX']}\n"
            + f" Scanner: {'ON' if self.config['SCANNER_ENABLED'] else 'PAUSE'}\n"
            + f" Watchlist: {len(self.config['WATCHLIST'])} símbolos\n"
            + f" Intervalo: {self.config['SCAN_INTERVAL']}s\n"
            + f" Máximo posiciones: {self.config['MAX_POSITIONS']}\n"
            + "=" * 60
        )

        if self.mode == BotMode.DRY_RUN:
            logger.info("Modo DRY_RUN. Se simula el balance y los datos de mercado.")
            self.rm.set_initial_balance(10000.0)
        else:
            balance = await self.api.fetch_balance()
            self.rm.set_initial_balance(balance["total"])
            logger.info(f"Balance inicial: ${balance['total']:.2f}")

    async def run(self) -> None:
        await self.initialize()
        self.running = True
        logger.info("[Bot] Loop principal iniciado.")

        while self.running:
            try:
                await self._cycle()
            except Exception as exc:
                logger.exception(f"[Bot] Error en ciclo: {exc}")
                await asyncio.sleep(2)

        await self.shutdown()

    async def _cycle(self) -> None:
        self.stats["cycles"] += 1

        capital = await self.rm.update_capital()
        stopped, reason = self.rm.kill_switch.check(capital.total_balance)
        if stopped:
            logger.critical(f"[Bot] Kill Switch activo: {reason}")
            self.running = False
            return

        if self.config["SCANNER_ENABLED"] and len(self.rm.positions) < self.config["MAX_POSITIONS"]:
            signals = await self.scanner.scan_all()
            self.stats["signals"] += len(signals)
            for signal in signals:
                await self._process_signal(signal)
        else:
            logger.debug("[Bot] Scanner en pausa o máximo de posiciones alcanzado.")

        if self.rm.positions:
            dfs = {}
            for symbol in list(self.rm.positions.keys()):
                dfs[symbol] = await self.api.fetch_ohlcv(symbol, timeframe="15m", limit=100)
            closes = await self.rm.monitor_positions(dfs)
            for symbol, (should_close, reason, notes) in closes.items():
                if not should_close:
                    continue
                pos = self.rm.positions.get(symbol)
                if not pos:
                    continue
                close_side = "sell" if pos.side == "LONG" else "buy"
                result = await self.executor.close_position(
                    symbol=symbol,
                    side=close_side,
                    contracts=pos.contracts,
                    current_price=pos.current_price,
                    reason=reason,
                )
                if result is None:
                    continue

                close_result = await self.rm.close_position(symbol, reason, pos.current_price)
                self.stats["closed"] += 1
                if reason == CloseReason.REVERSE:
                    self.stats["reversals"] += 1
                    # Abrir reverso inmediatamente si se detecta
                    reverse_side = "sell" if pos.side == "LONG" else "buy"
                    reverse_order = await self.executor.open_position(
                        symbol=symbol,
                        side=reverse_side,
                        position_size=pos,
                        stop_loss=pos.stop_loss,
                        take_profit=pos.take_profit,
                    )
                    if reverse_order:
                        self.rm.register_position(
                            order_id=reverse_order["id"],
                            position_size=pos,
                            pattern_id=pos.pattern_id,
                        )
                        logger.info(f"[Bot] Reverso abierto {symbol} {reverse_side}")

        self._log_status(capital)
        await asyncio.sleep(self.config["SCAN_INTERVAL"])

    async def _process_signal(self, signal: Signal) -> None:
        logger.info(
            f"[Bot] Señal {signal.signal_type.value} {signal.symbol} | "
            f"Score {signal.score:.2f} | R:R {signal.risk_reward:.1f}"
        )

        position_size = await self.rm.evaluate_entry(
            signal=signal,
            df=signal.df,
            sl_structural=signal.stop_loss,
            tp_structural=signal.take_profit,
        )
        if not position_size:
            logger.debug(f"[Bot] Señal descartada por RiskManager: {signal.symbol}")
            return

        open_side = "buy" if signal.signal_type.is_long() else "sell"
        order = await self.executor.open_position(
            symbol=signal.symbol,
            side=open_side,
            position_size=position_size,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
        )
        if not order:
            return

        self.rm.register_position(
            order_id=order["id"],
            position_size=position_size,
            pattern_id=signal.pattern_id,
        )
        self.stats["opened"] += 1
        logger.info(f"[Bot] Posición abierta {signal.symbol} {open_side}")

    def _log_status(self, capital: Any) -> None:
        logger.info(
            f"[Bot] Ciclo {self.stats['cycles']} | "
            f"Balance: ${capital.total_balance:.2f} | "
            f"Posiciones: {len(self.rm.positions)} | "
            f"Signals: {self.stats['signals']} | "
            f"Open: {self.stats['opened']} | "
            f"Closed: {self.stats['closed']}"
        )

    def _handle_shutdown(self, signum: int, frame: Any) -> None:
        logger.info(f"[Bot] Señal {signum} recibida, cerrando...")
        self.running = False

    async def shutdown(self) -> None:
        logger.info("[Bot] Deteniendo...")
        if self.api:
            await self.api.close()
        logger.info(
            f"[Bot] Estadísticas finales: ciclos={self.stats['cycles']}, "
            f"señales={self.stats['signals']}, abiertas={self.stats['opened']}, cerradas={self.stats['closed']}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ÚNICO STRATEGY Bot")
    parser.add_argument("--status", action="store_true", help="Muestra el estado del bot")
    parser.add_argument("--init-db", action="store_true", help="Inicializa la base de datos de patrones")
    parser.add_argument("--dry-run", action="store_true", help="Forzar modo DRY_RUN")
    parser.add_argument("--live", action="store_true", help="Forzar modo LIVE")
    return parser.parse_args()


def mostrar_estado() -> None:
    if not os.path.exists(CONFIG["DB_PATH"]):
        print("\n⚠️  La base de datos de patrones no existe. Ejecuta --init-db.")
        return
    print("\n" + "=" * 60)
    print("  ÚNICO STRATEGY — Estado")
    print("=" * 60)
    print(f"Modo: {CONFIG['MODE']}")
    print(f"Sandbox: {CONFIG['SANDBOX']}")
    print(f"Watchlist: {', '.join(CONFIG['WATCHLIST'])}")
    print(f"DB: {CONFIG['DB_PATH']}")
    print("=" * 60)


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
        mostrar_estado()
        return

    if CONFIG["MODE"] == "LIVE" and (not CONFIG["API_KEY"] or not CONFIG["API_SECRET"]):
        print("\n❌ Falta BYBIT_API_KEY o BYBIT_API_SECRET en el entorno.")
        sys.exit(1)

    bot = UnicoBot(CONFIG)
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("[Bot] Interrupción manual.")
    except Exception as exc:
        logger.exception(f"[Bot] Error crítico: {exc}")
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    print("""
╔═══════════════════════════════════════════════╗
║ ÚNICO STRATEGY v2.0 — Bot                    ║
║ Futuros Perpetuos USDT-M — Bybit             ║
╠═══════════════════════════════════════════════╣
║ Comandos:                                    ║
║ python main.py --init-db     → inicializar DB║
║ python main.py --status      → ver estado    ║
║ python main.py --dry-run     → modo de prueba║
║ python main.py --live        → modo real     ║
╚═══════════════════════════════════════════════╝
""")
    asyncio.run(main())
