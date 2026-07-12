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

# ─── CONFIGURACIÓN DE LOGGING ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("UNICO")

# ─── CONFIGURACIÓN PRINCIPAL ───
CONFIG: Dict[str, Any] = {
    # ─── CREDENCIALES (USANDO os.getenv PARA SEGURIDAD) ───
    "API_KEY": os.getenv("BYBIT_API_KEY", ""),
    "API_SECRET": os.getenv("BYBIT_API_SECRET", ""),
    "SANDBOX": os.getenv("BYBIT_SANDBOX", "false").lower() == "true",
    "MODE": os.getenv("BOT_MODE", "DRY_RUN"),
    
    # ─── ESTRATEGIA ───
    "SCANNER_ENABLED": True,
    "SCAN_INTERVAL": float(os.getenv("SCAN_INTERVAL", "10.0")),
    "MIN_SCORE": float(os.getenv("MIN_SCORE", "0.40")),   # ✅ CAMBIO: Bajado de 0.60 a 0.40 (menos estricto)
    "MIN_RR": float(os.getenv("MIN_RR", "3.0")),
    
    # ─── MÚLTIPLES TIMEFRAMES ───
    "TIMEFRAMES": ["15m", "3m"],
    
    # ─── GESTIÓN DE RIESGO ───
    "MAX_POSITIONS": int(os.getenv("MAX_POSITIONS", "3")),
    "POSITION_PCT": float(os.getenv("POSITION_PCT", "0.50")), # ✅ CAMBIO: Subido de 0.10 a 0.50 (50% del capital para cubrir el mínimo de Bybit)
    "SL_PCT": float(os.getenv("SL_PCT", "0.40")),
    "TP_MULTIPLE": float(os.getenv("TP_MULTIPLE", "5.0")),
    "LEVERAGE": int(os.getenv("LEVERAGE", "10")),
    "COOLDOWN_MINUTES": int(os.getenv("COOLDOWN_MINUTES", "15")),
    "MAX_ENTRIES_DAILY": int(os.getenv("MAX_ENTRIES_DAILY", "3")),
    
    # ─── APRENDIZAJE ───
    "LEARNING_ENABLED": os.getenv("LEARNING_ENABLED", "true").lower() == "true",
    
    # ─── BASE DE DATOS ───
    "DB_PATH": os.getenv("DB_PATH", "patterns.db"),
    "CAPITAL_FILE": os.getenv("CAPITAL_FILE", "capital_inicial.json"),
    
    # ─── WATCHLIST (AMPLIADA) ───
    "WATCHLIST": [
        # TOP 10
        "BTCUSDT", "ETHUSDT", "SOLUSDT",
        "BNBUSDT", "XRPUSDT", "DOGEUSDT",
        "ADAUSDT", "LINKUSDT", "AVAXUSDT",
        "DOTUSDT",
        
        # ALTCOINS
        "ATOMUSDT", "NEARUSDT", "ARBUSDT",
        "OPUSDT", "APTUSDT", "SUIUSDT",
        "RNDRUSDT", "FETUSDT", "AGIXUSDT",
        "OCEANUSDT", "POLUSDT",
        
        # ACTIVOS DE TUS IMÁGENES
        "DEXEUSDT", "BRUSDT", "LIGHTUSDT",
        "RESOLVUSDT", "OPGUSDT", "VELVETUSDT",
        "BEATUSDT", "TSTBSCUSDT", "POPCATUSDT",
        "HEIUSDT", "ALLOUSDT", "ABUSD",
        
        # MEMECOINS
        "PEPEUSDT", "WIFUSDT", "BONKUSDT",
        "SHIBUSDT",
        
        # DEFI
        "AAVEUSDT", "MKRUSDT", "COMPUSDT",
        "CRVUSDT", "LDOUSDT",
        
        # GAMING
        "SANDUSDT", "MANAUSDT", "AXSUSDT",
        "GALAUSDT", "BEAMUSDT", "CHZUSDT",
        
        # LAYER 1
        "VETUSDT", "HBARUSDT", "ALGOUSDT",
        "STXUSDT", "EGLDUSDT", "FTMUSDT",
    ],
}


class UnicoBot:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.mode = BotMode[config["MODE"]]
        
        # ─── VALIDACIÓN DE CREDENCIALES ───
        if self.mode == BotMode.LIVE:
            if not config["API_KEY"] or not config["API_SECRET"]:
                logger.critical("❌ BYBIT_API_KEY o BYBIT_API_SECRET no están configuradas")
                raise ValueError("Faltan credenciales de Bybit")
        
        # ─── API MANAGER ───
        self.api = BybitAPIManager(
            api_key=config["API_KEY"],
            api_secret=config["API_SECRET"],
            sandbox=config["SANDBOX"],
        )
        
        # ─── RISK MANAGER ───
        self.rm = RiskManager(
            api_manager=self.api,
            mode=self.mode,
            db_path=config["DB_PATH"],
            max_positions=config["MAX_POSITIONS"],
            position_pct=config.get("POSITION_PCT", 0.50), # Usa el nuevo 0.50
            sl_pct=config.get("SL_PCT", 0.40),
            tp_multiple=config.get("TP_MULTIPLE", 5.0),
            leverage=config.get("LEVERAGE", 10),
            cooldown_minutes=config.get("COOLDOWN_MINUTES", 15),
            max_entries_daily=config.get("MAX_ENTRIES_DAILY", 3),
        )
        
        # ─── SCANNER ───
        self.scanner = MarketScanner(
            api_manager=self.api,
            watchlist=config["WATCHLIST"],
            scan_interval=config["SCAN_INTERVAL"],
            min_score=config["MIN_SCORE"], # Usa el nuevo 0.40
            min_rr=config["MIN_RR"],
            position_pct=config["POSITION_PCT"],
            db_path=config["DB_PATH"],
            signal_cooldown_seconds=60,
            timeframes=config.get("TIMEFRAMES", ["15m", "3m"]),
        )
        
        # ─── ORDER EXECUTOR ───
        self.executor = OrderExecutor(api_manager=self.api, mode=self.mode)
        
        # ─── ESTADO ───
        self.running = False
        self.stats = {
            "cycles": 0,
            "signals": 0,
            "opened": 0,
            "closed": 0,
            "reversals": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        
        # ─── SEÑALES ───
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    async def initialize(self) -> None:
        """Inicializa el bot y muestra configuración."""
        logger.info(
            "\n" + "=" * 60 + "\n"
            + " 🚀 ÚNICO STRATEGY v4.0 — M15 + M3\n"
            + f" Modo: {self.mode.value}\n"
            + f" Sandbox: {self.config['SANDBOX']}\n"
            + f" Scanner: {'ON' if self.config['SCANNER_ENABLED'] else 'PAUSE'}\n"
            + f" Watchlist: {len(self.config['WATCHLIST'])} símbolos\n"
            + f" Timeframes: {self.config.get('TIMEFRAMES', ['15m', '3m'])}\n"
            + f" Intervalo: {self.config['SCAN_INTERVAL']}s\n"
            + f" Máximo posiciones: {self.config['MAX_POSITIONS']}\n"
            + f" Posición: {self.config['POSITION_PCT']*100:.0f}% capital\n" # Mostrará 50%
            + f" SL: {self.config['SL_PCT']*100:.0f}% de posición\n"
            + f" TP: {self.config['TP_MULTIPLE']}x riesgo\n"
            + f" Cooldown: {self.config['COOLDOWN_MINUTES']}min\n"
            + f" Límite diario: {self.config['MAX_ENTRIES_DAILY']}\n"
            + "=" * 60
        )

        # ─── BALANCE INICIAL ───
        if self.mode == BotMode.DRY_RUN:
            logger.info("🔬 Modo DRY_RUN - Simulando balance de $10,000 USDT")
            self.rm.set_initial_balance(10000.0)
        else:
            try:
                balance = await self.api.fetch_balance()
                self.rm.set_initial_balance(balance["total"])
                logger.info(f"💰 Balance inicial: {balance['total']:.2f} USDT")
            except Exception as e:
                logger.error(f"❌ Error obteniendo balance: {e}")
                raise

    async def run(self) -> None:
        """Loop principal del bot."""
        await self.initialize()
        self.running = True
        logger.info("✅ Bot iniciado. Loop principal corriendo...")

        while self.running:
            try:
                await self._cycle()
            except Exception as exc:
                logger.exception(f"❌ Error en ciclo: {exc}")
                await asyncio.sleep(5)

        await self.shutdown()

    async def _cycle(self) -> None:
        """Ciclo principal de escaneo y monitoreo."""
        self.stats["cycles"] += 1

        # ── CAPITAL Y KILL SWITCH ──
        capital = await self.rm.update_capital()
        stopped, reason = self.rm.kill_switch.check(capital.total_balance)
        if stopped:
            logger.critical(f"🛑 Kill Switch activo: {reason}")
            self.running = False
            return

        # ── SCANNER ──
        if self.config["SCANNER_ENABLED"] and len(self.rm.positions) < self.config["MAX_POSITIONS"]:
            signals = await self.scanner.scan_all()
            self.stats["signals"] += len(signals)
            for signal in signals:
                await self._process_signal(signal)
        else:
            logger.warning("⏸️ Scanner en pausa o máximo de posiciones alcanzado.")

        # ── MONITOREO DE POSICIONES ──
        if self.rm.positions:
            dfs = {}
            for symbol in list(self.rm.positions.keys()):
                pos = self.rm.positions[symbol]
                tf = getattr(pos, 'timeframe', '15m')
                dfs[symbol] = await self.api.fetch_ohlcv(symbol, timeframe=tf, limit=100)
            
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
                            signal_type=pos.signal_type,
                            arrow_color=pos.arrow_color,
                            score=pos.score,
                        )
                        logger.info(f"🔄 Reverso abierto {symbol} {reverse_side}")

        # ── STATUS ──
        self._log_status(capital)
        await asyncio.sleep(self.config["SCAN_INTERVAL"])

    async def _process_signal(self, signal: Signal) -> None:
        """Procesa una señal detectada por el scanner."""
        logger.info(
            f"📊 Señal {signal.signal_type.value} {signal.symbol} | "
            f"Score {signal.score:.2f} | Timeframe: {signal.timeframe}"
        )

        position_size = await self.rm.evaluate_entry(
            signal=signal,
            df=signal.df,
            sl_structural=signal.stop_loss,
            tp_structural=signal.take_profit,
        )
        
        if not position_size:
            logger.warning(f"⏭️ Señal descartada por RiskManager: {signal.symbol}")
            return

        open_side = "buy" if signal.signal_type.is_long() else "sell"
        order = await self.executor.open_position(
            symbol=signal.symbol,
            side=open_side,
            position_size=position_size,
            stop_loss=position_size.stop_loss,
            take_profit=position_size.take_profit,
            leverage=position_size.leverage,
        )
        
        if not order:
            logger.error(f"❌ Error abriendo posición en {signal.symbol}")
            return

        self.rm.register_position(
            order_id=order["id"],
            position_size=position_size,
            pattern_id=signal.pattern_id,
            signal_type=signal.signal_type.value,
            arrow_color=None,
            score=signal.score,
        )
        self.stats["opened"] += 1
        logger.info(
            f"✅ Posición abierta {signal.symbol} {open_side} {signal.timeframe} | "
            f"SL: {position_size.stop_loss:.4f} TP: {position_size.take_profit:.4f}"
        )

    def _log_status(self, capital: Any) -> None:
        """Log del estado actual."""
        positions_info = []
        for sym, pos in self.rm.positions.items():
            positions_info.append(f"{sym}({pos.timeframe})")
        
        logger.info(
            f"📊 Ciclo {self.stats['cycles']} | "
            f"Balance: {capital.total_balance:.2f} USDT | "
            f"Posiciones: {len(self.rm.positions)} {positions_info} | "
            f"Señales: {self.stats['signals']} | "
            f"Abiertas: {self.stats['opened']} | "
            f"Cerradas: {self.stats['closed']}"
        )

    def _handle_shutdown(self, signum: int, frame: Any) -> None:
        """Manejo de señales de cierre."""
        logger.info(f"🛑 Señal {signum} recibida, cerrando...")
        self.running = False

    async def shutdown(self) -> None:
        """Cierre limpio del bot."""
        logger.info("🛑 Deteniendo bot...")
        if self.api:
            await self.api.close()
        logger.info(
            f"📊 Estadísticas finales: ciclos={self.stats['cycles']}, "
            f"señales={self.stats['signals']}, abiertas={self.stats['opened']}, "
            f"cerradas={self.stats['closed']}, reversos={self.stats['reversals']}"
        )


# ─────────────────────────────────────────────
#  FUNCIONES AUXILIARES
# ─────────────────────────────────────────────
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
    print("  🧠 ÚNICO STRATEGY v4.0 — M15 + M3")
    print("=" * 60)
    print(f"  Modo: {CONFIG['MODE']}")
    print(f"  Sandbox: {CONFIG['SANDBOX']}")
    print(f"  Watchlist: {len(CONFIG['WATCHLIST'])} símbolos")
    print(f"  Timeframes: {CONFIG.get('TIMEFRAMES', ['15m', '3m'])}")
    print(f"  DB: {CONFIG['DB_PATH']}")
    print(f"  SL: {CONFIG['SL_PCT']*100:.0f}% de posición")
    print(f"  TP: {CONFIG['TP_MULTIPLE']}x riesgo")
    print("=" * 60)


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
async def main() -> None:
    args = parse_args()
    
    # ─── SOBREESCRIBIR MODO ───
    if args.dry_run:
        CONFIG["MODE"] = "DRY_RUN"
    if args.live:
        CONFIG["MODE"] = "LIVE"

    # ─── INICIALIZAR DB ───
    if args.init_db:
        with sqlite3.connect(CONFIG['DB_PATH']) as conn:
            seed_patterns.init_db(conn)
            seed_patterns.insert_patterns(conn, seed_patterns.PATTERNS)
            seed_patterns.print_summary(conn)
        return

    # ─── MOSTRAR ESTADO ───
    if args.status:
        mostrar_estado()
        return

    # ─── VALIDAR CREDENCIALES ───
    if CONFIG["MODE"] == "LIVE":
        if not CONFIG["API_KEY"] or not CONFIG["API_SECRET"]:
            print("\n❌ ERROR: Faltan credenciales de Bybit")
            print("   Asegúrate de configurar estas variables en Railway:")
            print("   • BYBIT_API_KEY")
            print("   • BYBIT_API_SECRET")
            print("\n   También puedes usar el modo DRY_RUN para pruebas:")
            print("   python main.py --dry-run\n")
            sys.exit(1)

    # ─── INICIAR BOT ───
    bot = UnicoBot(CONFIG)
    
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("⏹️ Interrupción manual.")
    except Exception as exc:
        logger.exception(f"💥 Error crítico: {exc}")
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    print("""
╔═══════════════════════════════════════════════════════════════╗
║            🧠 ÚNICO STRATEGY v4.0 — M15 + M3               ║
║         Futuros Perpetuos USDT-M — Bybit                    ║
╠═══════════════════════════════════════════════════════════════╣
║  TIMEFRAMES:                                                 ║
║  📊 M15: SL 4% | TP 20% | Leverage 10x | R:R 5:1           ║
║  ⚡ M3:  SL 4% | TP 20% | Leverage 10x | R:R 5:1           ║
╠═══════════════════════════════════════════════════════════════╣
║  COMANDOS:                                                   ║
║  python main.py --init-db     → Inicializar base de datos   ║
║  python main.py --status      → Ver estado del bot          ║
║  python main.py --dry-run     → Modo simulación (seguro)    ║
║  python main.py --live        → Modo real (¡cuidado!)       ║
╠═══════════════════════════════════════════════════════════════╣
║  🛡️  SL: 4% del capital  |  🚀 TP: 20% del capital         ║
║  📊 Posición: 50%        |  ⏱️  Cooldown: 15min            ║
║  🔒 Límite diario: 3     |  🧠 Aprendizaje: ACTIVADO       ║
╚═══════════════════════════════════════════════════════════════╝
""")
    asyncio.run(main())
