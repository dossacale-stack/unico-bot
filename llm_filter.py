"""
llm_filter.py — Filtro de confirmación con OpenRouter
=======================================================
Se consulta SOLO cuando el score está en zona gris (0.20 - 0.40).
Por encima de 0.40, se aprueba directo sin consultar.
Por debajo de 0.20, se descarta sin consultar.

Modelo: openrouter/free (router automático que elige el mejor modelo
gratuito disponible en cada momento). Esto evita que el bot se rompa
cuando OpenRouter retire un modelo específico.
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
from typing import Dict, Optional

import httpx

logger = logging.getLogger("LLMFilter")


class LLMFilter:
    API_URL = "https://openrouter.ai/api/v1/chat/completions"
    MODEL = "openrouter/free"
    SCORE_MIN = 0.20
    SCORE_MAX = 0.40
    TIMEOUT = 15.0

    def __init__(self, db_path: str = "patterns.db"):
        self.db_path = db_path
        self.api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        self.enabled = bool(self.api_key)

        self._mem_cache: Dict[str, Dict] = {}
        self._cache_ttl = 300  # 5 min

        self._init_table()

        if self.enabled:
            logger.info(f"[LLMFilter] ✅ Activo | Proveedor: OpenRouter | Modelo: {self.MODEL}")
        else:
            logger.warning("[LLMFilter] ⚠️ Sin OPENROUTER_API_KEY — filtro desactivado (todo aprueba)")

    def _init_table(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_cache (
                    hash       TEXT PRIMARY KEY,
                    response   TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            conn.commit()

    def _hash_prompt(self, prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    def _get_cached(self, prompt_hash: str) -> Optional[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT response FROM llm_cache WHERE hash = ?",
                (prompt_hash,)
            ).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return None
        return None

    def _save_cache(self, prompt_hash: str, response: Dict):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_cache (hash, response, created_at) VALUES (?, ?, ?)",
                (prompt_hash, json.dumps(response), time.time())
            )
            conn.commit()

    async def _call_llm(self, prompt: str) -> Optional[Dict]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/dosacale/unico-bot",
            "X-Title": "Unico Bot",
        }
        payload = {
            "model": self.MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Eres un analista de trading experto en futuros de criptomonedas. "
                        "Responde SIEMPRE en JSON válido, sin texto adicional. "
                        "Formato exacto: {\"decision\":\"GO\"|\"NO_GO\",\"reason\":\"...\",\"confidence\":0.0-1.0}"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }

        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                r = await client.post(self.API_URL, headers=headers, json=payload)
                r.raise_for_status()
                data = r.json()
                content = data["choices"][0]["message"]["content"]

                # Limpiar posibles backticks de markdown en la respuesta
                content = content.strip()
                if content.startswith("```"):
                    content = content.strip("`")
                    if content.startswith("json"):
                        content = content[4:].strip()

                return json.loads(content)
        except httpx.HTTPStatusError as e:
            logger.warning(f"[LLMFilter] HTTP {e.response.status_code} de OpenRouter: {e.response.text[:200]}")
            return None
        except json.JSONDecodeError as e:
            logger.warning(f"[LLMFilter] Respuesta no-JSON del modelo: {e}")
            return None
        except Exception as e:
            logger.warning(f"[LLMFilter] Error OpenRouter: {e}")
            return None

    async def should_execute(self, signal) -> Dict:
        """
        Evalúa si la señal debe ejecutarse.
        Retorna: {"allow": bool, "reason": str, "source": "score_alto"|"score_bajo"|"llm"|"degradado"}
        """
        score = getattr(signal, "score", 0.0)

        if score >= self.SCORE_MAX:
            return {
                "allow": True,
                "reason": f"Score alto ({score:.2f}) — aprueba sin LLM",
                "source": "score_alto",
            }

        if score < self.SCORE_MIN:
            return {
                "allow": False,
                "reason": f"Score bajo ({score:.2f}) — descarta",
                "source": "score_bajo",
            }

        if not self.enabled:
            return {
                "allow": True,
                "reason": "LLM desactivado (sin API key) — aprueba por defecto",
                "source": "degradado",
            }

        prompt = self._build_prompt(signal)
        prompt_hash = self._hash_prompt(prompt)

        cached = self._mem_cache.get(prompt_hash)
        if cached and (time.time() - cached["_ts"]) < self._cache_ttl:
            return cached["result"]

        db_cached = self._get_cached(prompt_hash)
        if db_cached:
            result = {
                "allow": db_cached.get("decision") == "GO",
                "reason": f"LLM (caché): {db_cached.get('reason', '')}",
                "source": "llm",
            }
            self._mem_cache[prompt_hash] = {"result": result, "_ts": time.time()}
            return result

        llm_response = await self._call_llm(prompt)
        if not llm_response:
            return {
                "allow": True,
                "reason": "LLM falló — aprueba por defecto",
                "source": "degradado",
            }

        self._save_cache(prompt_hash, llm_response)
        result = {
            "allow": llm_response.get("decision") == "GO",
            "reason": f"LLM: {llm_response.get('reason', '')}",
            "source": "llm",
        }
        self._mem_cache[prompt_hash] = {"result": result, "_ts": time.time()}
        return result

    def _build_prompt(self, signal) -> str:
        symbol = getattr(signal, "symbol", "?")
        signal_type = getattr(signal, "signal_type", None)
        signal_type_str = signal_type.value if hasattr(signal_type, "value") else str(signal_type)
        score = getattr(signal, "score", 0.0)
        rr = getattr(signal, "risk_reward", 0.0)
        reason = getattr(signal, "entry_reason", "")

        return (
            f"SEÑAL: {signal_type_str} en {symbol}\n"
            f"Score de agotamiento: {score:.2f} (zona gris)\n"
            f"Risk/Reward: {rr:.2f}\n"
            f"Razones del scanner: {reason}\n\n"
            f"¿Debo ejecutar esta operación? Responde SOLO con JSON: "
            f'{{"decision":"GO" o "NO_GO","reason":"explicación breve","confidence":0.0-1.0}}'
        )
