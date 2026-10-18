"""
rejection_analyzer.py — Análisis contextual de velas de rechazo
================================================================
Implementa los 5 contextos clave + scoring ponderado + pipeline ML.

Contextos:
1. key_level    — Nivel clave (EMAs, extremos, bandas de Bollinger)
2. trend        — Tendencia (pila de EMAs)
3. volume       — Volumen alto vs media
4. confluence   — Coincidencia de indicadores
5. structure    — Estructura previa coherente

Cada contexto devuelve 0.0-1.0. El score final es la media ponderada.
Si existe un modelo ML entrenado, se combina 50/50 con el heurístico.

v9 — dos lecturas según la flecha (parámetro `context`):
  "continuacion" (amarilla y azul en retroceso): el rechazo vale más si el precio toca una EMA
      (21/55/144/233, medida con la mecha) y la tendencia va a favor de la operación.
  "reversion" (roja, azul tras la verde, verde y tope): el rechazo vale más si el precio está
      estirado lejos de la EMA55, toca la banda y la tendencia previa va EN CONTRA de la operación
      (es la que se está agotando).
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("RejectionAnalyzer")

CONTEXTS = ("continuacion", "reversion")


class RejectionAnalyzer:
    WEIGHTS = {
        "key_level": 0.30,
        "trend": 0.25,
        "confluence": 0.20,
        "volume": 0.15,
        "structure": 0.10,
    }

    MIN_SCORE = 0.40
    MIN_WICK_BODY_RATIO = 1.2
    BAND_TOUCH_TOL = 0.003
    ML_MODEL_PATH = "models/rejection_model.json"

    def __init__(self, db_path: Optional[str] = "unico.db", min_score: Optional[float] = None):
        """db_path=None desactiva el registro en base de datos (lo usa el backtest)."""
        self.db_path = db_path
        if min_score is not None:
            self.MIN_SCORE = float(min_score)
        self._ml_model: Optional[Dict] = None
        if self.db_path:
            self._init_tables()
        self._try_load_ml_model()

    def _init_tables(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rejection_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT,
                    direction TEXT NOT NULL,
                    score REAL NOT NULL,
                    contexts TEXT NOT NULL,
                    features TEXT NOT NULL,
                    outcome TEXT,
                    pnl_usd REAL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(rejection_events)")}
            if "context" not in columns:
                conn.execute("ALTER TABLE rejection_events ADD COLUMN context TEXT")
            conn.commit()

    def _try_load_ml_model(self):
        if os.path.exists(self.ML_MODEL_PATH):
            try:
                with open(self.ML_MODEL_PATH, "r") as f:
                    self._ml_model = json.load(f)
                logger.info(f"[RejectionAnalyzer] Modelo ML cargado: {self.ML_MODEL_PATH}")
            except Exception as e:
                logger.warning(f"[RejectionAnalyzer] Error cargando ML: {e}")
                self._ml_model = None
        else:
            logger.info("[RejectionAnalyzer] Sin modelo ML. Solo heurística ponderada.")

    def analyze(self, df: pd.DataFrame, direction: str, symbol: str = "?", log: bool = True,
                context: str = "continuacion") -> Dict:
        if df is None or len(df) < 30:
            return self._empty_result()

        direction = direction.upper()
        if direction not in ("BULLISH", "BEARISH"):
            return self._empty_result()
        if context not in CONTEXTS:
            context = "continuacion"

        row = df.iloc[-1]

        features = self._extract_features(df, row, direction)
        contexts = self._evaluate_contexts(df, row, features, direction, context)

        heuristic_score = sum(contexts[k] * self.WEIGHTS[k] for k in self.WEIGHTS)

        score = heuristic_score
        if self._ml_model is not None:
            ml_score = self._predict_ml(features)
            if ml_score is not None:
                score = 0.5 * heuristic_score + 0.5 * ml_score

        wick_ok = features["wick_body_ratio"] >= self.MIN_WICK_BODY_RATIO
        is_rejection = wick_ok and score >= self.MIN_SCORE

        event_id = None
        if log and self.db_path:
            event_id = self._log_event(symbol, direction, score, contexts, features, context)

        contexts_met = [k for k, v in contexts.items() if v >= 0.5]

        return {
            "event_id": event_id,
            "is_rejection": is_rejection,
            "score": round(score, 3),
            "heuristic_score": round(heuristic_score, 3),
            "contexts": {k: round(v, 3) for k, v in contexts.items()},
            "contexts_met": contexts_met,
            "context": context,
            "features": features,
        }

    def _extract_features(self, df, row, direction) -> Dict:
        o = float(row["open"])
        c = float(row["close"])
        h = float(row["high"])
        l = float(row["low"])

        body = abs(c - o) or (h - l) * 0.1
        total_range = h - l or 1e-9

        if direction == "BULLISH":
            wick = min(o, c) - l
        else:
            wick = h - max(o, c)

        wick_body_ratio = wick / body if body > 0 else 0.0
        wick_range_ratio = wick / total_range
        body_range_ratio = body / total_range

        ema21 = float(row.get("ema21", c))
        ema_fast = float(row.get("ema_fast", c))
        ema_mid = float(row.get("ema_mid", c))
        ema_slow = float(row.get("ema_slow", c))
        price = c

        distance_to_ema55 = abs(price - ema_fast) / ema_fast if ema_fast > 0 else 1.0
        distance_to_bb_mid = abs(price - float(row.get("bb_mid", c))) / c if c > 0 else 1.0

        # v9: distancia de la MECHA a la EMA más cercana (el toque se hace con la mecha)
        extreme = l if direction == "BULLISH" else h
        emas = [e for e in (ema21, ema_fast, ema_mid, ema_slow) if np.isfinite(e) and e > 0]
        nearest_ema_dist = min(abs(extreme - e) / c for e in emas) if emas and c > 0 else 1.0
        if ema_fast > 0:
            stretch_ema55 = (ema_fast - l) / ema_fast if direction == "BULLISH" else (h - ema_fast) / ema_fast
        else:
            stretch_ema55 = 0.0

        bb_position = 0.5
        band_touch = 0.0
        try:
            bb_u = float(row["bb_upper"])
            bb_l = float(row["bb_lower"])
            if bb_u > bb_l:
                bb_position = (c - bb_l) / (bb_u - bb_l)
            if direction == "BULLISH" and l <= bb_l * (1 + self.BAND_TOUCH_TOL):
                band_touch = 1.0
            elif direction == "BEARISH" and h >= bb_u * (1 - self.BAND_TOUCH_TOL):
                band_touch = 1.0
        except Exception:
            pass

        vol = float(row["volume"]) if "volume" in row else 0.0
        vol_avg = float(row.get("vol_avg", vol)) if "vol_avg" in row else vol
        volume_ratio = vol / vol_avg if vol_avg > 0 else 1.0

        recent_20 = df.tail(20)
        recent_low = float(recent_20["low"].min())
        recent_high = float(recent_20["high"].max())

        if direction == "BULLISH":
            distance_to_extreme = abs(l - recent_low) / recent_low if recent_low > 0 else 1.0
        else:
            distance_to_extreme = abs(recent_high - h) / recent_high if recent_high > 0 else 1.0

        closes_10 = df["close"].tail(10).values
        if len(closes_10) >= 2 and closes_10[0] > 0:
            structure_change = (closes_10[-1] - closes_10[0]) / closes_10[0]
        else:
            structure_change = 0.0

        atr = float(row["atr"]) if "atr" in row and not pd.isna(row["atr"]) else 0.0
        atr_pct = atr / c if c > 0 else 0.0

        return {
            "body_size": float(body),
            "wick_size": float(wick),
            "total_range": float(total_range),
            "wick_body_ratio": float(wick_body_ratio),
            "wick_range_ratio": float(wick_range_ratio),
            "body_range_ratio": float(body_range_ratio),
            "distance_to_ema55": float(distance_to_ema55),
            "distance_to_bb_mid": float(distance_to_bb_mid),
            "nearest_ema_dist": float(nearest_ema_dist),
            "stretch_ema55": float(stretch_ema55),
            "band_touch": float(band_touch),
            "bb_position": float(bb_position),
            "volume_ratio": float(volume_ratio),
            "distance_to_extreme": float(distance_to_extreme),
            "structure_change": float(structure_change),
            "atr_pct": float(atr_pct),
        }

    def _evaluate_contexts(self, df, row, features, direction, context="continuacion") -> Dict:
        reversion = context == "reversion"
        return {
            "key_level": self._ctx_key_level(features, direction, reversion),
            "trend": self._ctx_trend(row, direction, reversion),
            "confluence": self._ctx_confluence(features, direction, reversion),
            "volume": self._ctx_volume(features),
            "structure": self._ctx_structure(features, direction),
        }

    def _ctx_key_level(self, features, direction, reversion=False) -> float:
        score = 0.0
        ext = features["distance_to_extreme"]
        if reversion:
            # Estirado lejos de la EMA55 (clímax) y cerca del extremo reciente (vale un máximo más bajo)
            stretch = features["stretch_ema55"]
            if stretch >= 0.04:
                score += 0.4
            elif stretch >= 0.02:
                score += 0.2
            if ext < 0.015:
                score += 0.4
            elif ext < 0.04:
                score += 0.2
        else:
            near = features["nearest_ema_dist"]
            if near < 0.005:
                score += 0.4
            elif near < 0.015:
                score += 0.2
            if ext < 0.005:
                score += 0.4
            elif ext < 0.015:
                score += 0.2

        if features["band_touch"] >= 1.0:
            score += 0.2
        elif direction == "BULLISH" and features["bb_position"] < 0.15:
            score += 0.2
        elif direction == "BEARISH" and features["bb_position"] > 0.85:
            score += 0.2

        return min(score, 1.0)

    @staticmethod
    def _stack_score(ef, em, es, bullish: bool) -> float:
        if bullish:
            if ef > em and em > es:
                return 1.0
            if ef > em:
                return 0.6
            if em > es:
                return 0.3
            return 0.0
        if ef < em and em < es:
            return 1.0
        if ef < em:
            return 0.6
        if em < es:
            return 0.3
        return 0.0

    def _ctx_trend(self, row, direction, reversion=False) -> float:
        try:
            ef = float(row["ema_fast"])
            em = float(row["ema_mid"])
            es = float(row["ema_slow"])
        except Exception:
            return 0.0
        bullish = direction == "BULLISH"
        if reversion:
            # En una reversión cuenta la tendencia que se agota (la contraria a la operación)
            bullish = not bullish
        return self._stack_score(ef, em, es, bullish)

    def _ctx_confluence(self, features, direction, reversion=False) -> float:
        score = 0.0
        ext = features["distance_to_extreme"]
        vr = features["volume_ratio"]
        if reversion:
            if features["stretch_ema55"] >= 0.03 and features["band_touch"] >= 1.0:
                score += 0.5
            if ext < 0.02 and vr >= 1.4:
                score += 0.3
            change = features["structure_change"]
            if (direction == "BEARISH" and change >= 0.03) or (direction == "BULLISH" and change <= -0.03):
                score += 0.2
            return min(score, 1.0)

        near = features["nearest_ema_dist"]
        if near < 0.015 and ext < 0.01:
            score += 0.5

        if near < 0.015:
            if features["band_touch"] >= 1.0:
                score += 0.3
            elif direction == "BULLISH" and features["bb_position"] < 0.2:
                score += 0.3
            elif direction == "BEARISH" and features["bb_position"] > 0.8:
                score += 0.3

        if vr >= 1.4 and ext < 0.01:
            score += 0.2

        return min(score, 1.0)

    def _ctx_volume(self, features) -> float:
        vr = features["volume_ratio"]
        if vr >= 1.8:
            return 1.0
        if vr >= 1.4:
            return 0.7
        if vr >= 1.1:
            return 0.4
        if vr >= 0.8:
            return 0.2
        return 0.0

    def _ctx_structure(self, features, direction) -> float:
        change = features["structure_change"]
        if direction == "BULLISH":
            if change <= -0.02:
                return 1.0
            if change <= -0.01:
                return 0.6
            if change <= 0:
                return 0.3
            return 0.0
        else:
            if change >= 0.02:
                return 1.0
            if change >= 0.01:
                return 0.6
            if change >= 0:
                return 0.3
            return 0.0

    def _predict_ml(self, features) -> Optional[float]:
        if self._ml_model is None:
            return None
        try:
            weights = self._ml_model.get("weights", {})
            bias = self._ml_model.get("bias", 0.0)
            z = bias
            for k, w in weights.items():
                z += w * features.get(k, 0.0)
            z = max(min(z, 30), -30)  # evita overflow
            return float(1.0 / (1.0 + np.exp(-z)))
        except Exception as e:
            logger.warning(f"[RejectionAnalyzer] Error en ML: {e}")
            return None

    def _log_event(self, symbol, direction, score, contexts, features, context="continuacion") -> Optional[int]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute("""
                    INSERT INTO rejection_events
                    (timestamp, symbol, direction, score, contexts, features, context)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    datetime.now(timezone.utc).isoformat(),
                    symbol,
                    direction,
                    float(score),
                    json.dumps(contexts),
                    json.dumps(features),
                    context,
                ))
                conn.commit()
                return int(cur.lastrowid)
        except Exception as e:
            logger.warning(f"[RejectionAnalyzer] Error registrando evento: {e}")
            return None

    def record_outcome(self, event_id: int, outcome: str, pnl_usd: float):
        """Guarda el resultado real de la operación (WIN/LOSS) para entrenar el modelo ML."""
        if not self.db_path or not event_id:
            return
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE rejection_events SET outcome = ?, pnl_usd = ? WHERE id = ?",
                    (outcome, float(pnl_usd), int(event_id)),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"[RejectionAnalyzer] Error record_outcome: {e}")

    def _empty_result(self) -> Dict:
        return {
            "event_id": None,
            "is_rejection": False,
            "score": 0.0,
            "heuristic_score": 0.0,
            "contexts": {},
            "contexts_met": [],
            "context": None,
            "features": {},
        }
