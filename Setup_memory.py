"""
SetupMemory — Memoria de expectativa por setup.
================================================
Guarda hit rate y expectancy por (símbolo + patrón + timeframe).
Antes de entrar, el bot consulta: "este setup, ¿ganó antes en este par?"
Si perdió sistemáticamente, no entra.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Dict

logger = logging.getLogger("SetupMemory")


class SetupMemory:
    def __init__(self, db_path: str = "patterns.db"):
        self.db_path = db_path
        self._init_table()
        self._cache: Dict[str, Dict] = {}
        self._cache_time: Dict[str, float] = {}
        self._cache_ttl = 120  # 2 min

    def _init_table(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS setup_memory (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol          TEXT NOT NULL,
                    pattern_id      INTEGER NOT NULL,
                    timeframe       TEXT NOT NULL,
                    wins            INTEGER DEFAULT 0,
                    losses          INTEGER DEFAULT 0,
                    total_pnl       REAL DEFAULT 0,
                    avg_pnl         REAL DEFAULT 0,
                    hit_rate        REAL DEFAULT 0,
                    best_pnl        REAL DEFAULT 0,
                    worst_pnl       REAL DEFAULT 0,
                    last_updated    TEXT,
                    UNIQUE(symbol, pattern_id, timeframe)
                )
            """)
            conn.commit()

    def get_setup_expectancy(self, symbol: str, pattern_id: int, timeframe: str) -> Dict:
        """Consulta la expectativa histórica del setup."""
        import time
        key = f"{symbol}|{pattern_id}|{timeframe}"

        if key in self._cache and (time.time() - self._cache_time.get(key, 0)) < self._cache_ttl:
            return self._cache[key]

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT wins, losses, avg_pnl, total_pnl, hit_rate
                FROM setup_memory
                WHERE symbol = ? AND pattern_id = ? AND timeframe = ?
            """, (symbol, pattern_id, timeframe)).fetchone()

        if not row:
            result = {
                "muestras": 0,
                "hit_rate": 0.5,
                "expectancy": 0.0,
                "permitido": True,
                "razon": "SETUP_NUEVO"
            }
        else:
            wins, losses, avg_pnl, total_pnl, hit_rate_db = row
            muestras = wins + losses

            # Suavizado bayesiano: prior 2 wins / 2 losses
            alpha = 2
            hit_rate = (wins + alpha) / (muestras + 2 * alpha)

            if muestras < 5:
                result = {
                    "muestras": muestras,
                    "hit_rate": hit_rate,
                    "expectancy": avg_pnl,
                    "permitido": True,
                    "razon": f"POCAS_MUESTRAS ({muestras})"
                }
            elif hit_rate < 0.35:
                result = {
                    "muestras": muestras,
                    "hit_rate": hit_rate,
                    "expectancy": avg_pnl,
                    "permitido": False,
                    "razon": f"HIT_RATE_BAJO ({hit_rate:.1%} con {muestras} muestras)"
                }
            elif avg_pnl < 0:
                result = {
                    "muestras": muestras,
                    "hit_rate": hit_rate,
                    "expectancy": avg_pnl,
                    "permitido": False,
                    "razon": f"EXPECTANCY_NEGATIVA (${avg_pnl:.2f})"
                }
            else:
                result = {
                    "muestras": muestras,
                    "hit_rate": hit_rate,
                    "expectancy": avg_pnl,
                    "permitido": True,
                    "razon": f"OK (hit {hit_rate:.1%}, {muestras} muestras, avg ${avg_pnl:.2f})"
                }

        self._cache[key] = result
        self._cache_time[key] = time.time()
        return result

    def update(self, symbol: str, pattern_id: int, timeframe: str, pnl_usd: float):
        """Registra resultado del trade cerrado."""
        now = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT wins, losses, total_pnl, best_pnl, worst_pnl
                FROM setup_memory
                WHERE symbol = ? AND pattern_id = ? AND timeframe = ?
            """, (symbol, pattern_id, timeframe)).fetchone()

            if row:
                wins, losses, total_pnl, best_pnl, worst_pnl = row
                if pnl_usd > 0:
                    wins += 1
                else:
                    losses += 1
                total_pnl += pnl_usd
                best_pnl = max(best_pnl, pnl_usd)
                worst_pnl = min(worst_pnl, pnl_usd)
                muestras = wins + losses
                avg_pnl = total_pnl / muestras if muestras > 0 else 0
                hit_rate = wins / muestras if muestras > 0 else 0

                conn.execute("""
                    UPDATE setup_memory SET
                        wins = ?, losses = ?, total_pnl = ?,
                        avg_pnl = ?, hit_rate = ?, best_pnl = ?,
                        worst_pnl = ?, last_updated = ?
                    WHERE symbol = ? AND pattern_id = ? AND timeframe = ?
                """, (wins, losses, total_pnl, avg_pnl, hit_rate,
                      best_pnl, worst_pnl, now,
                      symbol, pattern_id, timeframe))
            else:
                wins = 1 if pnl_usd > 0 else 0
                losses = 1 if pnl_usd <= 0 else 0
                conn.execute("""
                    INSERT INTO setup_memory
                    (symbol, pattern_id, timeframe, wins, losses,
                     total_pnl, avg_pnl, hit_rate, best_pnl, worst_pnl, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (symbol, pattern_id, timeframe, wins, losses,
                      pnl_usd, pnl_usd, 1.0 if wins else 0.0,
                      pnl_usd, pnl_usd, now))

            conn.commit()

        # Invalidar caché
        key = f"{symbol}|{pattern_id}|{timeframe}"
        self._cache.pop(key, None)
        self._cache_time.pop(key, None)

        logger.info(f"[SetupMemory] 📝 {symbol} patrón {pattern_id} @ {timeframe}: ${pnl_usd:+.2f}")

    def get_top_setups(self, min_muestras: int = 5, limit: int = 20):
        """Devuelve los mejores setups históricos."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM setup_memory
                WHERE (wins + losses) >= ?
                ORDER BY avg_pnl DESC
                LIMIT ?
            """, (min_muestras, limit)).fetchall()
            return [dict(r) for r in rows]
