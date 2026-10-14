"""
rejection_analyzer.py — Análisis contextual de velas de rechazo
================================================================
Implementa los 5 contextos clave + scoring ponderado + pipeline ML.

Contextos:
1. key_level    — Nivel clave (EMA55, extremos, BB)
2. trend        — Tendencia a favor (pila EMAs)
3. volume       — Volumen alto vs media
4. confluence   — Coincidencia de indicadores
5. structure    — Estructura previa coherente

Cada contexto devuelve 0.0-1.0. El score final es la media ponderada.
Si existe un modelo ML entrenado, se combina 50/50 con el heurístico.
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
    ML_MODEL_PATH = "models/rejection_model.json"

    def __init__(self, db_path: str = "patterns.db"):
        self.db_path = db_path
        self._ml_model: Optional[Dict] = None
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

    def analyze(self, df: pd.DataFrame, direction: str, symbol: str = "?") -> Dict:
        if df is None or len(df) < 30:
            return self._empty_result()

        direction = direction.upper()
        if direction not in ("BULLISH", "BEARISH"):
            return self._empty_result()

        row = df.iloc[-1]

        features = self._extract_features(df, row, direction)
        contexts = self._evaluate_contexts(df, row, features, direction)

        heuristic_score = sum(contexts[k] * self.WEIGHTS[k] for k in self.WEIGHTS)

        score = heuristic_score
        if self._ml_model is not None:
            ml_score = self._predict_ml(features)
            if ml_score is not None:
                score = 0.5 * heuristic_score + 0.5 * ml_score

        wick_ok = features["wick_body_ratio"] >= self.MIN_WICK_BODY_RATIO
        is_rejection = wick_ok and score >= self.MIN_SCORE

        self._log_event(symbol, direction, score, contexts, features)

        contexts_met = [k for k, v in contexts.items() if v >= 0.5]

        return {
            "is_rejection": is_rejection,
            "score": round(score, 3),
            "heuristic_score": round(heuristic_score, 3),
            "contexts": {k: round(v, 3) for k, v in contexts.items()},
            "contexts_met": contexts_met,
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

        wick_body_ratio = wick / body
        wick_range_ratio = wick / total_range
        body_range_ratio = body / total_range

        ema_fast = float(row.get("ema_fast", c))
        ema_mid = float(row.get("ema_mid", c))
        ema_slow = float(row.get("ema_slow", c))
        price = c

        distance_to_ema55 = abs(price - ema_fast) / ema_fast if ema_fast > 0 else 1.0
        distance_to_bb_mid = abs(price - float(row.get("bb_mid", c))) / c if c > 0 else 1.0

        bb_position = 0.5
        try:
            bb_u = float(row["bb_upper"])
            bb_l = float(row["bb_lower"])
            if bb_u > bb_l:
                bb_position = (c - bb_l) / (bb_u - bb_l)
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
            "bb_position": float(bb_position),
            "volume_ratio": float(volume_ratio),
            "distance_to_extreme": float(distance_to_extreme),
            "structure_change": float(structure_change),
            "atr_pct": float(atr_pct),
        }

    def _evaluate_contexts(self, df, row, features, direction) -> Dict:
        return {
            "key_level": self._ctx_key_level(features, direction),
            "trend": self._ctx_trend(row, direction),
            "confluence": self._ctx_confluence(features, direction),
            "volume": self._ctx_volume(features),
            "structure": self._ctx_structure(features, direction),
        }

    def _ctx_key_level(self, features, direction) -> float:
        score = 0.0
        if features["distance_to_ema55"] < 0.01:
            score += 0.4
        elif features["distance_to_ema55"] < 0.02:
            score += 0.2

        if features["distance_to_extreme"] < 0.005:
            score += 0.4
        elif features["distance_to_extreme"] < 0.015:
            score += 0.2

        if direction == "BULLISH" and features["bb_position"] < 0.15:
            score += 0.2
        elif direction == "BEARISH" and features["bb_position"] > 0.85:
            score += 0.2

        return min(score, 1.0)

    def _ctx_trend(self, row, direction) -> float:
        try:
            ef = float(row["ema_fast"])
            em = float(row["ema_mid"])
            es = float(row["ema_slow"])
        except Exception:
            return 0.0

        if direction == "BULLISH":
            if ef > em and em > es:
                return 1.0
            if ef > em:
                return 0.6
            if em > es:
                return 0.3
            return 0.0
        else:
            if ef < em and em < es:
                return 1.0
            if ef < em:
                return 0.6
            if em < es:
                return 0.3
            return 0.0

    def _ctx_confluence(self, features, direction) -> float:
        score = 0.0
        if features["distance_to_ema55"] < 0.015 and features["distance_to_extreme"] < 0.01:
            score += 0.5

        if features["distance_to_ema55"] < 0.015:
            if direction == "BULLISH" and features["bb_position"] < 0.2:
                score += 0.3
            elif direction == "BEARISH" and features["bb_position"] > 0.8:
                score += 0.3

        if features["volume_ratio"] >= 1.4 and features["distance_to_extreme"] < 0.01:
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

    def _log_event(self, symbol, direction, score, contexts, features):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO rejection_events
                    (timestamp, symbol, direction, score, contexts, features)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    datetime.now(timezone.utc).isoformat(),
                    symbol,
                    direction,
                    float(score),
                    json.dumps(contexts),
                    json.dumps(features),
                ))
                conn.commit()
        except Exception as e:
            logger.debug(f"[RejectionAnalyzer] Error logging: {e}")

    def record_outcome(self, event_id: int, outcome: str, pnl_usd: float):
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
            "is_rejection": False,
            "score": 0.0,
            "heuristic_score": 0.0,
            "contexts": {},
            "contexts_met": [],
            "features": {},
        }
