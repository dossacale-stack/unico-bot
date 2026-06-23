"""
LearningEngine — Motor de Aprendizaje Automático del Bot
========================================================
ÚNICO STRATEGY v3.0 — El bot aprende de cada operación

CARACTERÍSTICAS:
  - Registra cada operación con todos sus detalles
  - Analiza patrones ganadores vs perdedores
  - Ajusta pesos de patrones según rendimiento
  - Aprende mejores horarios para operar
  - Identifica activos rentables vs no rentables
  - Genera recomendaciones automáticas
  - Exporta reportes de aprendizaje
"""

import logging
import sqlite3
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger("LearningEngine")


@dataclass
class TradeRecord:
    """Registro completo de una operación."""
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    contracts: float
    leverage: int
    position_usd: float
    pnl_usd: float
    pnl_percent: float
    entry_time: datetime
    exit_time: datetime
    exit_reason: str
    pattern_id: Optional[int] = None
    pattern_score: Optional[float] = None
    signal_type: Optional[str] = None
    arrow_color: Optional[str] = None
    ema_config: Optional[str] = None
    bb_config: Optional[str] = None
    volume_ratio: Optional[float] = None
    was_st_asset: bool = False

    @property
    def duration_minutes(self) -> float:
        delta = self.exit_time - self.entry_time
        return delta.total_seconds() / 60

    @property
    def is_win(self) -> bool:
        return self.pnl_usd > 0

    @property
    def is_loss(self) -> bool:
        return self.pnl_usd < 0

    @property
    def hour(self) -> int:
        return self.entry_time.hour

    @property
    def weekday(self) -> int:
        return self.entry_time.weekday()


class LearningEngine:
    """
    Motor de aprendizaje del bot.
    Analiza operaciones pasadas para mejorar decisiones futuras.
    """

    def __init__(self, db_path: str = "patterns.db"):
        self.db_path = db_path
        self._init_tables()

    def _init_tables(self):
        """Inicializa las tablas de aprendizaje."""
        with sqlite3.connect(self.db_path) as conn:
            # Tabla de operaciones
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol              TEXT NOT NULL,
                    side                TEXT NOT NULL,
                    entry_price         REAL NOT NULL,
                    exit_price          REAL NOT NULL,
                    contracts           REAL NOT NULL,
                    leverage            INTEGER NOT NULL,
                    position_usd        REAL NOT NULL,
                    pnl_usd             REAL NOT NULL,
                    pnl_percent         REAL NOT NULL,
                    entry_time          TEXT NOT NULL,
                    exit_time           TEXT NOT NULL,
                    duration_minutes    REAL NOT NULL,
                    exit_reason         TEXT NOT NULL,
                    pattern_id          INTEGER,
                    pattern_score       REAL,
                    signal_type         TEXT,
                    arrow_color         TEXT,
                    ema_config          TEXT,
                    bb_config           TEXT,
                    volume_ratio        REAL,
                    was_st_asset        BOOLEAN DEFAULT 0,
                    created_at          TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Tabla de rendimiento por patrón
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pattern_performance (
                    pattern_id          INTEGER PRIMARY KEY,
                    pattern_name        TEXT,
                    total_trades        INTEGER DEFAULT 0,
                    wins                INTEGER DEFAULT 0,
                    losses              INTEGER DEFAULT 0,
                    total_pnl_usd       REAL DEFAULT 0.0,
                    avg_pnl_usd         REAL DEFAULT 0.0,
                    win_rate            REAL DEFAULT 0.0,
                    avg_duration_min    REAL DEFAULT 0.0,
                    best_trade          REAL DEFAULT 0.0,
                    worst_trade         REAL DEFAULT 0.0,
                    last_updated        TEXT DEFAULT CURRENT_TIMESTAMP,
                    weight              REAL DEFAULT 1.0
                )
            """)

            # Tabla de rendimiento por símbolo
            conn.execute("""
                CREATE TABLE IF NOT EXISTS symbol_performance (
                    symbol              TEXT PRIMARY KEY,
                    total_trades        INTEGER DEFAULT 0,
                    wins                INTEGER DEFAULT 0,
                    losses              INTEGER DEFAULT 0,
                    total_pnl_usd       REAL DEFAULT 0.0,
                    win_rate            REAL DEFAULT 0.0,
                    avg_pnl_usd         REAL DEFAULT 0.0,
                    best_trade          REAL DEFAULT 0.0,
                    worst_trade         REAL DEFAULT 0.0,
                    last_traded         TEXT,
                    weight              REAL DEFAULT 1.0,
                    is_active           BOOLEAN DEFAULT 1
                )
            """)

            # Tabla de condiciones aprendidas
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learned_conditions (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    condition_type      TEXT NOT NULL,
                    condition_value     TEXT NOT NULL,
                    win_rate            REAL DEFAULT 0.0,
                    total_trades        INTEGER DEFAULT 0,
                    confidence          REAL DEFAULT 0.0,
                    last_updated        TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(condition_type, condition_value)
                )
            """)

            # Tabla de recomendaciones
            conn.execute("""
                CREATE TABLE IF NOT EXISTS recommendations (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    recommendation_type TEXT NOT NULL,
                    symbol              TEXT,
                    pattern_id          INTEGER,
                    reason              TEXT,
                    created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
                    expires_at          TEXT,
                    is_active           BOOLEAN DEFAULT 1
                )
            """)

            # Tabla de estadísticas diarias (para tracking de mejora)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_stats (
                    date                TEXT PRIMARY KEY,
                    total_trades        INTEGER DEFAULT 0,
                    wins                INTEGER DEFAULT 0,
                    losses              INTEGER DEFAULT 0,
                    total_pnl_usd       REAL DEFAULT 0.0,
                    win_rate            REAL DEFAULT 0.0,
                    best_pattern_id     INTEGER,
                    worst_pattern_id    INTEGER,
                    created_at          TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()
            logger.info("[LearningEngine] Tablas de aprendizaje inicializadas")

    def register_trade(self, trade: TradeRecord) -> int:
        """
        Registra una operación completada.
        Retorna el ID de la operación.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO trades (
                    symbol, side, entry_price, exit_price, contracts,
                    leverage, position_usd, pnl_usd, pnl_percent,
                    entry_time, exit_time, duration_minutes, exit_reason,
                    pattern_id, pattern_score, signal_type, arrow_color,
                    ema_config, bb_config, volume_ratio, was_st_asset
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.symbol, trade.side, trade.entry_price, trade.exit_price,
                trade.contracts, trade.leverage, trade.position_usd,
                trade.pnl_usd, trade.pnl_percent,
                trade.entry_time.isoformat(), trade.exit_time.isoformat(),
                trade.duration_minutes, trade.exit_reason,
                trade.pattern_id, trade.pattern_score,
                trade.signal_type, trade.arrow_color,
                trade.ema_config, trade.bb_config,
                trade.volume_ratio, 1 if trade.was_st_asset else 0
            ))
            trade_id = cursor.lastrowid
            conn.commit()

            # Actualizar estadísticas
            self._update_pattern_stats(trade)
            self._update_symbol_stats(trade)
            self._update_learned_conditions(trade)
            self._update_daily_stats(trade)

            # Generar recomendaciones si hay suficiente data
            if trade.pattern_id:
                self._check_and_generate_recommendations(trade)

            logger.info(
                f"[LearningEngine] 📊 Operación registrada: {trade.symbol} "
                f"{trade.side} | PnL: ${trade.pnl_usd:.2f} | {trade.exit_reason}"
            )

            return trade_id

    def _update_pattern_stats(self, trade: TradeRecord):
        """Actualiza el rendimiento del patrón usado."""
        if not trade.pattern_id:
            return

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT total_trades, wins, losses, total_pnl_usd,
                       best_trade, worst_trade, weight
                FROM pattern_performance WHERE pattern_id = ?
            """, (trade.pattern_id,))
            row = cursor.fetchone()

            if row:
                total, wins, losses, total_pnl, best, worst, current_weight = row
                total += 1
                if trade.is_win:
                    wins += 1
                    best = max(best, trade.pnl_usd)
                else:
                    losses += 1
                    worst = min(worst, trade.pnl_usd)
                total_pnl += trade.pnl_usd
                avg_pnl = total_pnl / total
                win_rate = wins / total if total > 0 else 0.0
                avg_duration = ((row[6] or 0) * (total - 1) + trade.duration_minutes) / total

                # Calcular nuevo peso basado en rendimiento
                weight = current_weight
                if total >= 5:
                    if win_rate > 0.6 and avg_pnl > 0:
                        weight = min(2.0, weight * (1 + (win_rate - 0.5) * 0.5))
                    elif win_rate < 0.4 or avg_pnl < 0:
                        weight = max(0.3, weight * (1 - (0.5 - win_rate) * 0.5))
                    # Limitar el peso
                    weight = max(0.1, min(2.0, weight))

                conn.execute("""
                    UPDATE pattern_performance SET
                        total_trades = ?, wins = ?, losses = ?,
                        total_pnl_usd = ?, avg_pnl_usd = ?,
                        win_rate = ?, avg_duration_min = ?,
                        best_trade = ?, worst_trade = ?,
                        weight = ?, last_updated = ?
                    WHERE pattern_id = ?
                """, (total, wins, losses, total_pnl, avg_pnl,
                      win_rate, avg_duration, best, worst,
                      weight, datetime.now(timezone.utc).isoformat(),
                      trade.pattern_id))
            else:
                conn.execute("""
                    INSERT INTO pattern_performance (
                        pattern_id, total_trades, wins, losses,
                        total_pnl_usd, avg_pnl_usd, win_rate,
                        avg_duration_min, best_trade, worst_trade,
                        weight, last_updated
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?)
                """, (
                    trade.pattern_id,
                    1 if trade.is_win else 0,
                    1 if trade.is_loss else 0,
                    trade.pnl_usd,
                    trade.pnl_usd,
                    1.0 if trade.is_win else 0.0,
                    trade.duration_minutes,
                    trade.pnl_usd if trade.is_win else 0.0,
                    trade.pnl_usd if trade.is_loss else 0.0,
                    datetime.now(timezone.utc).isoformat()
                ))

            conn.commit()

    def _update_symbol_stats(self, trade: TradeRecord):
        """Actualiza el rendimiento del símbolo."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT total_trades, wins, losses, total_pnl_usd,
                       best_trade, worst_trade, weight
                FROM symbol_performance WHERE symbol = ?
            """, (trade.symbol,))
            row = cursor.fetchone()

            if row:
                total, wins, losses, total_pnl, best, worst, current_weight = row
                total += 1
                if trade.is_win:
                    wins += 1
                    best = max(best, trade.pnl_usd)
                else:
                    losses += 1
                    worst = min(worst, trade.pnl_usd)
                total_pnl += trade.pnl_usd
                avg_pnl = total_pnl / total
                win_rate = wins / total if total > 0 else 0.0

                # Calcular peso del símbolo
                weight = current_weight
                if total >= 5:
                    if win_rate > 0.55 and avg_pnl > 0:
                        weight = min(2.0, weight * (1 + (win_rate - 0.5) * 0.5))
                    elif win_rate < 0.4 or avg_pnl < 0:
                        weight = max(0.3, weight * (1 - (0.5 - win_rate) * 0.5))

                conn.execute("""
                    UPDATE symbol_performance SET
                        total_trades = ?, wins = ?, losses = ?,
                        total_pnl_usd = ?, avg_pnl_usd = ?,
                        win_rate = ?, best_trade = ?,
                        worst_trade = ?, weight = ?,
                        last_traded = ?
                    WHERE symbol = ?
                """, (total, wins, losses, total_pnl, avg_pnl,
                      win_rate, best, worst, weight,
                      datetime.now(timezone.utc).isoformat(),
                      trade.symbol))
            else:
                conn.execute("""
                    INSERT INTO symbol_performance (
                        symbol, total_trades, wins, losses,
                        total_pnl_usd, avg_pnl_usd, win_rate,
                        best_trade, worst_trade, weight, last_traded
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 1.0, ?)
                """, (
                    trade.symbol,
                    1 if trade.is_win else 0,
                    1 if trade.is_loss else 0,
                    trade.pnl_usd,
                    trade.pnl_usd,
                    1.0 if trade.is_win else 0.0,
                    trade.pnl_usd if trade.is_win else 0.0,
                    trade.pnl_usd if trade.is_loss else 0.0,
                    datetime.now(timezone.utc).isoformat()
                ))

            conn.commit()

    def _update_learned_conditions(self, trade: TradeRecord):
        """Actualiza condiciones aprendidas."""
        with sqlite3.connect(self.db_path) as conn:
            # Aprender sobre el mejor horario para operar
            hour = trade.hour
            conn.execute("""
                INSERT INTO learned_conditions (
                    condition_type, condition_value, win_rate,
                    total_trades, confidence, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(condition_type, condition_value) DO UPDATE SET
                    win_rate = ((win_rate * total_trades) + ?) / (total_trades + 1),
                    total_trades = total_trades + 1,
                    confidence = min(1.0, (total_trades + 1) / 20),
                    last_updated = ?
            """, (
                f"hour_{hour}",
                f"entry_time_{hour}h",
                1.0 if trade.is_win else 0.0,
                1,
                0.05,
                datetime.now(timezone.utc).isoformat(),
                1.0 if trade.is_win else 0.0,
                datetime.now(timezone.utc).isoformat()
            ))

            # Aprender sobre el día de la semana
            weekday = trade.weekday
            conn.execute("""
                INSERT INTO learned_conditions (
                    condition_type, condition_value, win_rate,
                    total_trades, confidence, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(condition_type, condition_value) DO UPDATE SET
                    win_rate = ((win_rate * total_trades) + ?) / (total_trades + 1),
                    total_trades = total_trades + 1,
                    confidence = min(1.0, (total_trades + 1) / 20),
                    last_updated = ?
            """, (
                f"weekday_{weekday}",
                f"entry_day_{weekday}",
                1.0 if trade.is_win else 0.0,
                1,
                0.05,
                datetime.now(timezone.utc).isoformat(),
                1.0 if trade.is_win else 0.0,
                datetime.now(timezone.utc).isoformat()
            ))

            # Aprender sobre el exit_reason (qué funciona mejor)
            conn.execute("""
                INSERT INTO learned_conditions (
                    condition_type, condition_value, win_rate,
                    total_trades, confidence, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(condition_type, condition_value) DO UPDATE SET
                    win_rate = ((win_rate * total_trades) + ?) / (total_trades + 1),
                    total_trades = total_trades + 1,
                    confidence = min(1.0, (total_trades + 1) / 20),
                    last_updated = ?
            """, (
                f"exit_reason_{trade.exit_reason}",
                f"exit_{trade.exit_reason}",
                1.0 if trade.is_win else 0.0,
                1,
                0.05,
                datetime.now(timezone.utc).isoformat(),
                1.0 if trade.is_win else 0.0,
                datetime.now(timezone.utc).isoformat()
            ))

            conn.commit()

    def _update_daily_stats(self, trade: TradeRecord):
        """Actualiza estadísticas diarias."""
        today = trade.entry_time.date().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO daily_stats (date, total_trades, wins, losses, total_pnl_usd)
                VALUES (?, 1, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    total_trades = total_trades + 1,
                    wins = wins + ?,
                    losses = losses + ?,
                    total_pnl_usd = total_pnl_usd + ?,
                    win_rate = (wins + ?) / (total_trades + 1)
            """, (
                today,
                1 if trade.is_win else 0,
                1 if trade.is_loss else 0,
                trade.pnl_usd,
                1 if trade.is_win else 0,
                1 if trade.is_loss else 0,
                trade.pnl_usd,
                1 if trade.is_win else 0
            ))
            conn.commit()

    def _check_and_generate_recommendations(self, trade: TradeRecord):
        """Genera recomendaciones basadas en aprendizaje."""
        if not trade.pattern_id:
            return

        with sqlite3.connect(self.db_path) as conn:
            # Verificar si el patrón está funcionando mal
            cursor = conn.execute("""
                SELECT total_trades, win_rate, total_pnl_usd
                FROM pattern_performance WHERE pattern_id = ?
            """, (trade.pattern_id,))
            row = cursor.fetchone()

            if row:
                total, win_rate, total_pnl = row
                if total >= 10 and win_rate < 0.35 and total_pnl < 0:
                    # Generar recomendación de evitar este patrón
                    conn.execute("""
                        INSERT INTO recommendations (
                            recommendation_type, pattern_id, reason, expires_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT DO NOTHING
                    """, (
                        "AVOID_PATTERN",
                        trade.pattern_id,
                        f"Win rate muy bajo ({win_rate*100:.1f}%) con {total} operaciones",
                        (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
                    ))
                    logger.info(f"[LearningEngine] ⚠️ Recomendación generada: evitar patrón {trade.pattern_id}")

            # Verificar si el símbolo está funcionando mal
            cursor = conn.execute("""
                SELECT total_trades, win_rate, total_pnl_usd
                FROM symbol_performance WHERE symbol = ?
            """, (trade.symbol,))
            row = cursor.fetchone()

            if row:
                total, win_rate, total_pnl = row
                if total >= 10 and win_rate < 0.3 and total_pnl < 0:
                    conn.execute("""
                        INSERT INTO recommendations (
                            recommendation_type, symbol, reason, expires_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT DO NOTHING
                    """, (
                        "AVOID_SYMBOL",
                        trade.symbol,
                        f"Win rate muy bajo ({win_rate*100:.1f}%) con {total} operaciones",
                        (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
                    ))
                    logger.info(f"[LearningEngine] ⚠️ Recomendación generada: evitar {trade.symbol}")

            conn.commit()

    def get_pattern_weight(self, pattern_id: int) -> float:
        """Obtiene el peso aprendido de un patrón."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT weight FROM pattern_performance
                WHERE pattern_id = ?
            """, (pattern_id,))
            row = cursor.fetchone()
            return float(row[0]) if row else 1.0

    def get_symbol_weight(self, symbol: str) -> float:
        """Obtiene el peso aprendido de un símbolo."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT weight FROM symbol_performance
                WHERE symbol = ?
            """, (symbol,))
            row = cursor.fetchone()
            return float(row[0]) if row else 1.0

    def should_trade_symbol(self, symbol: str) -> Tuple[bool, str]:
        """
        Evalúa si se debe operar un símbolo basado en aprendizaje.
        Retorna (debe_operar, razón).
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT total_trades, win_rate, avg_pnl_usd, weight
                FROM symbol_performance WHERE symbol = ?
            """, (symbol,))
            row = cursor.fetchone()

            if not row:
                return True, "Sin historial - permitir"

            total, win_rate, avg_pnl, weight = row

            # Si tiene más de 10 operaciones y win_rate < 35% → evitar
            if total >= 10 and win_rate < 0.35:
                return False, f"Win rate muy bajo: {win_rate*100:.1f}%"

            # Si tiene más de 10 operaciones y PnL promedio negativo → evitar
            if total >= 10 and avg_pnl < 0:
                return False, f"PnL promedio negativo: ${avg_pnl:.2f}"

            # Si el peso es muy bajo → evitar
            if weight < 0.4:
                return False, f"Peso muy bajo: {weight:.2f}"

            return True, "Aprobado por aprendizaje"

    def get_best_patterns(self, limit: int = 10) -> List[Dict]:
        """Obtiene los patrones con mejor rendimiento."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM pattern_performance
                WHERE total_trades >= 5
                ORDER BY win_rate DESC, total_pnl_usd DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_worst_patterns(self, limit: int = 10) -> List[Dict]:
        """Obtiene los patrones con peor rendimiento."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM pattern_performance
                WHERE total_trades >= 5
                ORDER BY win_rate ASC, total_pnl_usd ASC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_best_symbols(self, limit: int = 10) -> List[Dict]:
        """Obtiene los símbolos con mejor rendimiento."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM symbol_performance
                WHERE total_trades >= 5 AND win_rate > 0.5
                ORDER BY win_rate DESC, total_pnl_usd DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_learning_report(self) -> Dict:
        """Genera un reporte de aprendizaje completo."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            report = {
                "total_trades": 0,
                "total_wins": 0,
                "total_losses": 0,
                "total_pnl": 0.0,
                "overall_win_rate": 0.0,
                "best_patterns": [],
                "worst_patterns": [],
                "best_symbols": [],
                "worst_symbols": [],
                "best_hours": [],
                "best_exit_reasons": [],
                "recommendations": [],
                "daily_trend": [],
                "learning_progress": "El bot está aprendiendo..."
            }

            # Estadísticas generales
            cursor = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) as losses,
                    SUM(pnl_usd) as total_pnl
                FROM trades
            """)
            row = cursor.fetchone()
            if row:
                report["total_trades"] = row["total"] or 0
                report["total_wins"] = row["wins"] or 0
                report["total_losses"] = row["losses"] or 0
                report["total_pnl"] = row["total_pnl"] or 0.0
                if row["total"] and row["total"] > 0:
                    report["overall_win_rate"] = row["wins"] / row["total"]

            # Mejores patrones
            report["best_patterns"] = self.get_best_patterns(5)

            # Peores patrones
            report["worst_patterns"] = self.get_worst_patterns(5)

            # Mejores símbolos
            report["best_symbols"] = self.get_best_symbols(5)

            # Peores símbolos
            cursor = conn.execute("""
                SELECT * FROM symbol_performance
                WHERE total_trades >= 5 AND win_rate < 0.4
                ORDER BY win_rate ASC, total_pnl_usd ASC
                LIMIT 5
            """)
            report["worst_symbols"] = [dict(row) for row in cursor.fetchall()]

            # Mejores horas
            cursor = conn.execute("""
                SELECT condition_value, win_rate, total_trades, confidence
                FROM learned_conditions
                WHERE condition_type LIKE 'hour_%' AND total_trades >= 3
                ORDER BY win_rate DESC
                LIMIT 5
            """)
            report["best_hours"] = [dict(row) for row in cursor.fetchall()]

            # Mejores exit_reasons
            cursor = conn.execute("""
                SELECT condition_value, win_rate, total_trades, confidence
                FROM learned_conditions
                WHERE condition_type LIKE 'exit_reason_%' AND total_trades >= 3
                ORDER BY win_rate DESC
                LIMIT 5
            """)
            report["best_exit_reasons"] = [dict(row) for row in cursor.fetchall()]

            # Recomendaciones
            for symbol, should_trade, reason in self.get_recommendations():
                report["recommendations"].append({
                    "symbol": symbol,
                    "should_trade": should_trade,
                    "reason": reason
                })

            # Tendencia diaria (últimos 7 días)
            cursor = conn.execute("""
                SELECT date, total_trades, wins, losses, total_pnl_usd, win_rate
                FROM daily_stats
                ORDER BY date DESC
                LIMIT 7
            """)
            report["daily_trend"] = [dict(row) for row in cursor.fetchall()]

            # Mensaje de progreso
            if report["total_trades"] < 10:
                report["learning_progress"] = "🔬 Aprendiendo... (necesita más operaciones)"
            elif report["overall_win_rate"] > 0.55:
                report["learning_progress"] = f"🧠 El bot está mejorando! Win Rate: {report['overall_win_rate']*100:.1f}%"
            else:
                report["learning_progress"] = "📈 El bot está aprendiendo de sus errores..."

            return report

    def get_recommendations(self) -> List[Tuple[str, bool, str]]:
        """Genera recomendaciones basadas en aprendizaje."""
        recommendations = []

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT DISTINCT symbol FROM symbol_performance
                WHERE total_trades >= 5
            """)
            symbols = [row[0] for row in cursor.fetchall()]

            for symbol in symbols:
                should, reason = self.should_trade_symbol(symbol)
                recommendations.append((symbol, should, reason))

            return recommendations

    def export_learning_data(self, filepath: str = "learning_data.json"):
        """Exporta todos los datos de aprendizaje a JSON."""
        report = self.get_learning_report()

        # Añadir operaciones detalladas
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM trades ORDER BY id DESC LIMIT 100
            """)
            report["recent_trades"] = [dict(row) for row in cursor.fetchall()]

        with open(filepath, "w") as f:
            json.dump(report, f, indent=2, default=str)

        logger.info(f"[LearningEngine] 📊 Datos exportados a {filepath}")
        return report

    def print_learning_report(self):
        """Imprime un reporte de aprendizaje formateado."""
        report = self.get_learning_report()

        print("\n" + "=" * 70)
        print("🧠 REPORTE DE APRENDIZAJE DEL BOT")
        print("=" * 70)
        print(f"📈 {report['learning_progress']}")
        print("=" * 70)

        print(f"\n📊 ESTADÍSTICAS GENERALES:")
        print(f"  Total operaciones: {report['total_trades']}")
        print(f"  Victorias: {report['total_wins']}")
        print(f"  Derrotas: {report['total_losses']}")
        print(f"  Win Rate: {report['overall_win_rate']*100:.1f}%")
        print(f"  PnL Total: ${report['total_pnl']:.2f}")

        if report['best_patterns']:
            print(f"\n🏆 MEJORES PATRONES:")
            for p in report['best_patterns']:
                print(f"  ID:{p['pattern_id']} | Win Rate: {p['win_rate']*100:.1f}% | "
                      f"Operaciones: {p['total_trades']} | Peso: {p['weight']:.2f} | "
                      f"PnL: ${p['total_pnl_usd']:.2f}")

        if report['worst_patterns']:
            print(f"\n⚠️ PEORES PATRONES (a evitar):")
            for p in report['worst_patterns']:
                print(f"  ID:{p['pattern_id']} | Win Rate: {p['win_rate']*100:.1f}% | "
                      f"Operaciones: {p['total_trades']} | Peso: {p['weight']:.2f} | "
                      f"PnL: ${p['total_pnl_usd']:.2f}")

        if report['best_symbols']:
            print(f"\n💰 MEJORES SÍMBOLOS:")
            for s in report['best_symbols']:
                print(f"  {s['symbol']} | Win Rate: {s['win_rate']*100:.1f}% | "
                      f"PnL: ${s['total_pnl_usd']:.2f} | Peso: {s['weight']:.2f}")

        if report['best_hours']:
            print(f"\n🕐 MEJORES HORARIOS:")
            for h in report['best_hours']:
                hour = h['condition_value'].replace('entry_time_', '').replace('h', '')
                print(f"  {hour}:00hs | Win Rate: {h['win_rate']*100:.1f}% | "
                      f"Operaciones: {h['total_trades']} | Confianza: {h['confidence']:.2f}")

        if report['best_exit_reasons']:
            print(f"\n🎯 MEJORES RAZONES DE SALIDA:")
            for e in report['best_exit_reasons']:
                reason = e['condition_value'].replace('exit_', '')
                print(f"  {reason} | Win Rate: {e['win_rate']*100:.1f}% | "
                      f"Operaciones: {e['total_trades']}")

        if report['recommendations']:
            print(f"\n📋 RECOMENDACIONES:")
            for r in report['recommendations']:
                status = "✅ RECOMENDADO" if r['should_trade'] else "❌ EVITAR"
                print(f"  {r['symbol']}: {status} - {r['reason']}")

        if report['daily_trend']:
            print(f"\n📈 TENDENCIA DIARIA (últimos 7 días):")
            for d in report['daily_trend']:
                print(f"  {d['date']} | Trades: {d['total_trades']} | "
                      f"Win Rate: {d['win_rate']*100:.1f}% | PnL: ${d['total_pnl_usd']:.2f}")

        print("\n" + "=" * 70)


# ─────────────────────────────────────────────
#  CLI PARA MONITOREAR APRENDIZAJE
# ─────────────────────────────────────────────
def main():
    engine = LearningEngine()

    print("\n" + "=" * 70)
    print("🧠 SISTEMA DE APRENDIZAJE DEL BOT — ÚNICO STRATEGY")
    print("=" * 70)

    while True:
        print("\n📋 COMANDOS DISPONIBLES:")
        print("  1. Ver reporte completo de aprendizaje")
        print("  2. Ver mejores patrones")
        print("  3. Ver peores patrones")
        print("  4. Ver mejores símbolos")
        print("  5. Ver recomendaciones")
        print("  6. Exportar aprendizaje a JSON")
        print("  7. Salir")

        choice = input("\nSelecciona una opción (1-7): ").strip()

        if choice == "1":
            engine.print_learning_report()
        elif choice == "2":
            print("\n🏆 MEJORES PATRONES:")
            for p in engine.get_best_patterns():
                print(f"  ID:{p['pattern_id']} | Win Rate: {p['win_rate']*100:.1f}% | "
                      f"Operaciones: {p['total_trades']} | Peso: {p['weight']:.2f}")
        elif choice == "3":
            print("\n⚠️ PEORES PATRONES:")
            for p in engine.get_worst_patterns():
                print(f"  ID:{p['pattern_id']} | Win Rate: {p['win_rate']*100:.1f}% | "
                      f"Operaciones: {p['total_trades']} | Peso: {p['weight']:.2f}")
        elif choice == "4":
            print("\n💰 MEJORES SÍMBOLOS:")
            for s in engine.get_best_symbols():
                print(f"  {s['symbol']} | Win Rate: {s['win_rate']*100:.1f}% | "
                      f"PnL: ${s['total_pnl_usd']:.2f} | Peso: {s['weight']:.2f}")
        elif choice == "5":
            print("\n📋 RECOMENDACIONES:")
            for symbol, should, reason in engine.get_recommendations():
                status = "✅ RECOMENDADO" if should else "❌ EVITAR"
                print(f"  {symbol}: {status} - {reason}")
        elif choice == "6":
            filename = f"learning_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            engine.export_learning_data(filename)
            print(f"✅ Reporte exportado a {filename}")
        elif choice == "7":
            print("👋 ¡Hasta luego!")
            break
        else:
            print("❌ Opción inválida. Intenta de nuevo.")


if __name__ == "__main__":
    main()