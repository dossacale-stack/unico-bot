"""
setup_memory.py — Memoria de resultados por flecha y por patrón WASHI (v9)
=========================================================================
Antes de entrar, el bot pregunta: "¿esta flecha (o este patrón) ha ganado antes?".

Qué se guarda: por (moneda, código, temporalidad) y también en conjunto ("UNIVERSAL"):
  - código 1001 ROJA, 1002 AMARILLA, 1003 AZUL en retroceso, 1004 AZUL tras la verde,
    1005 VERDE y 1006 TOPE (si se activan como entrada) → strategy.SETUP_CODES
  - código = id del patrón WASHI parecido (seed_patterns), si lo hay
  - el resultado en R (múltiplos del riesgo): así el tamaño de la cuenta no distorsiona el promedio.

Cuándo bloquea (corrige la v8):
  La v8 bloqueaba PARA SIEMPRE un patrón con 5 operaciones y promedio negativo. Con salidas a 2R/5R
  es normal acertar menos de la mitad, así que 5 u 8 operaciones con promedio negativo pasan a menudo
  por pura mala racha; y una vez bloqueado no llegaban operaciones nuevas para desbloquearlo.
  Ahora:
    - Hacen falta SETUP_MIN_SAMPLES operaciones (20 por defecto).
    - Solo se bloquea si el promedio sigue siendo negativo aun sumándole un error estándar
      (promedio + desviación / √n < 0): es perdedor de verdad, no una racha.
    - Mientras está bloqueada, el bot sigue esas señales "en sombra" (qué habría pasado) y esos
      resultados también cuentan: si la flecha vuelve a funcionar, se desbloquea sola.
    - SETUP_MIN_HIT_RATE (0 = desactivado) bloquea además si el % de aciertos suavizado es menor.
Se consulta primero la moneda; si tiene menos muestras que el mínimo, se usa el dato UNIVERSAL.
El backtest usa la misma clase en memoria (sin tocar la base de datos real).
"""
from __future__ import annotations

import logging
import math
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("SetupMemory")

UNIVERSAL = "UNIVERSAL"

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS setup_stats (
    symbol       TEXT NOT NULL,
    code         INTEGER NOT NULL,
    timeframe    TEXT NOT NULL,
    name         TEXT,
    n            INTEGER DEFAULT 0,
    wins         INTEGER DEFAULT 0,
    sum_r        REAL DEFAULT 0,
    sum_r2       REAL DEFAULT 0,
    shadow_n     INTEGER DEFAULT 0,
    best_r       REAL,
    worst_r      REAL,
    last_updated TEXT,
    PRIMARY KEY (symbol, code, timeframe)
)
"""


def stats_from_row(n: int, wins: int, sum_r: float, sum_r2: float) -> Dict[str, float]:
    mean = sum_r / n if n else 0.0
    var = max(0.0, (sum_r2 - n * mean * mean) / (n - 1)) if n > 1 else 0.0
    std = math.sqrt(var)
    se = std / math.sqrt(n) if n else 0.0
    hit = (wins + 2) / (n + 4)          # suavizado: como si empezara con 2 ganadas y 2 perdidas
    return {"r_medio": mean, "desviacion": std, "error": se, "hit_rate": hit}


class SetupMemory:
    def __init__(self, db_path: str = "unico.db", min_samples: int = 20, min_hit_rate: float = 0.0,
                 conn: Optional[sqlite3.Connection] = None):
        self.min_samples = max(1, int(min_samples))
        self.min_hit_rate = float(min_hit_rate)
        self._lock = threading.RLock()
        self.conn = conn or sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        with self._lock:
            self.conn.execute(TABLE_SQL)
            self.conn.commit()

    @classmethod
    def in_memory(cls, min_samples: int = 20, min_hit_rate: float = 0.0) -> "SetupMemory":
        return cls(conn=sqlite3.connect(":memory:", check_same_thread=False),
                   min_samples=min_samples, min_hit_rate=min_hit_rate)

    # ------------------------------------------------------------------
    def _row(self, symbol: str, code: int, timeframe: str) -> Optional[Tuple]:
        with self._lock:
            return self.conn.execute(
                "SELECT n, wins, sum_r, sum_r2, shadow_n FROM setup_stats WHERE symbol = ? AND code = ? AND timeframe = ?",
                (symbol, int(code), timeframe),
            ).fetchone()

    def expectancy(self, symbol: str, code: int, timeframe: str) -> Dict:
        """Resultado histórico de la flecha/patrón y si se permite operarla."""
        row = self._row(symbol, code, timeframe)
        source = symbol
        if row is None or row[0] < self.min_samples:
            universal = self._row(UNIVERSAL, code, timeframe)
            if universal is not None and (row is None or universal[0] > row[0]):
                row, source = universal, UNIVERSAL
        if row is None or row[0] == 0:
            return {"muestras": 0, "sombra": 0, "hit_rate": 0.5, "r_medio": 0.0, "error": 0.0,
                    "permitido": True, "razon": "sin historial", "fuente": source}
        n, wins, sum_r, sum_r2, shadow_n = row
        s = stats_from_row(n, wins, sum_r, sum_r2)
        base = {"muestras": n, "sombra": shadow_n, "hit_rate": round(s["hit_rate"], 4),
                "r_medio": round(s["r_medio"], 4), "error": round(s["error"], 4), "fuente": source}
        if n < self.min_samples:
            return {**base, "permitido": True, "razon": f"pocas muestras ({n} de {self.min_samples})"}
        if s["r_medio"] + s["error"] < 0:
            return {**base, "permitido": False,
                    "razon": f"pierde de forma consistente ({s['r_medio']:+.2f}R de media en {n} operaciones)"}
        if self.min_hit_rate > 0 and s["hit_rate"] < self.min_hit_rate:
            return {**base, "permitido": False,
                    "razon": f"% de aciertos bajo ({s['hit_rate']:.0%} con {n} operaciones)"}
        return {**base, "permitido": True, "razon": f"OK ({s['r_medio']:+.2f}R de media en {n} operaciones)"}

    # Compatibilidad con la v8
    get_setup_expectancy = expectancy

    def check(self, symbol: str, timeframe: str, keys: Iterable[Tuple[Optional[int], str]]) -> Dict:
        """Revisa varias claves (flecha y patrón). Bloquea si alguna está bloqueada."""
        detail = []
        for code, label in keys:
            if code is None:
                continue
            exp = self.expectancy(symbol, int(code), timeframe)
            detail.append({"codigo": int(code), "nombre": label, **exp})
            if not exp["permitido"]:
                return {"permitido": False, "razon": f"{label}: {exp['razon']}", "detalle": detail}
        return {"permitido": True, "razon": "; ".join(f"{d['nombre']}: {d['razon']}" for d in detail),
                "detalle": detail}

    # ------------------------------------------------------------------
    def _record_one(self, symbol: str, code: int, timeframe: str, r: float, name: str, shadow: bool) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO setup_stats (symbol, code, timeframe, name, n, wins, sum_r, sum_r2, shadow_n, best_r, "
            "worst_r, last_updated) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, code, timeframe) DO UPDATE SET "
            "n = n + 1, wins = wins + excluded.wins, sum_r = sum_r + excluded.sum_r, "
            "sum_r2 = sum_r2 + excluded.sum_r2, shadow_n = shadow_n + excluded.shadow_n, "
            "best_r = MAX(COALESCE(best_r, excluded.best_r), excluded.best_r), "
            "worst_r = MIN(COALESCE(worst_r, excluded.worst_r), excluded.worst_r), "
            "name = COALESCE(excluded.name, name), last_updated = excluded.last_updated",
            (symbol, int(code), timeframe, name or None, 1 if r > 0 else 0, float(r), float(r) * float(r),
             1 if shadow else 0, float(r), float(r), now),
        )

    def record(self, symbol: str, code: int, timeframe: str, r: float, name: str = "", shadow: bool = False) -> None:
        """Registra el resultado (en R) de una operación cerrada, en la moneda y en UNIVERSAL."""
        if r is None or not math.isfinite(float(r)):
            return
        with self._lock:
            self._record_one(symbol, code, timeframe, r, name, shadow)
            if symbol != UNIVERSAL:
                self._record_one(UNIVERSAL, code, timeframe, r, name, shadow)
            self.conn.commit()
        tag = " (sombra)" if shadow else ""
        logger.info(f"[Memoria] {symbol} {name or code} @ {timeframe}: {float(r):+.2f}R{tag}")

    def summary(self, symbol: str = UNIVERSAL, timeframe: Optional[str] = None) -> List[Dict]:
        """Tabla para el panel: resultado acumulado de cada flecha/patrón."""
        sql = "SELECT * FROM setup_stats WHERE symbol = ?"
        params: List = [symbol]
        if timeframe:
            sql += " AND timeframe = ?"
            params.append(timeframe)
        with self._lock:
            cur = self.conn.execute(sql + " ORDER BY code", params)
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for row in rows:
            s = stats_from_row(row["n"], row["wins"], row["sum_r"], row["sum_r2"])
            row.update({k: round(v, 4) for k, v in s.items()})
            row["bloqueada"] = not self.expectancy(symbol, row["code"], row["timeframe"])["permitido"]
        return rows
