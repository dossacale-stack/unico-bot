"""
llm_filter.py — Segunda opinión con IA para cada flecha (v9)
=============================================================
Qué hace:
  - Recibe la flecha que vio el bot (ROJA, AMARILLA, AZUL…) con sus motivos, el plan de la operación,
    la fase de 1 hora, los máximos/mínimos desde el listado, el historial de esa flecha y las velas recientes
    con EMAs 21/55/144/233 y Bandas de Bollinger.
  - Responde GO / NO_GO con su confianza y el motivo, en español.

¿Cuándo se consulta?
  En la prueba con velas reales el puntaje NO separó las buenas de las malas, así que por defecto
  (LLM_AUTO_APPROVE_SCORE=1.0) se consulta TODA flecha que pase las reglas. Si algún día el puntaje
  demuestra servir, se puede bajar ese número para aprobar sin consultar las de puntaje alto.

Proveedores (LLM_PROVIDER): anthropic (Claude), openrouter, groq. "auto" elige el que tenga clave.
  Claude: ANTHROPIC_API_KEY (consola de Claude), modelo por defecto claude-haiku-5-5.

Si el modelo falla (caída, tiempo de espera, respuesta no válida):
  LLM_ON_ERROR=allow → el bot sigue con sus reglas (por defecto).
  LLM_ON_ERROR=block → no opera esa señal.
Todo queda registrado en la tabla signals (decisión, confianza, motivo, latencia) y las señales
vetadas se siguen en "modo sombra" para medir si el filtro realmente ayuda.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

import httpx
import pandas as pd

from config import Config

logger = logging.getLogger("LLMFilter")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SYSTEM_PROMPT = (
    "Eres el asistente de riesgo de una trader de futuros perpetuos de criptomonedas (Bybit, velas de 15 minutos). "
    "Opera interés y desinterés con las EMAs exponenciales 55 (morada), 144 (azul) y 233 (amarilla): "
    "DESINTERÉS = el precio pierde la morada y la azul, vuelve a acercarse por debajo y no logra cerrar encima "
    "(venta de menor riesgo hasta la zona del suelo previo); INTERÉS = sobre la morada y la azul, retrocede a una "
    "de ellas y se sostiene (compra hasta la zona del techo previo). Se cierra todo en la zona. "
    "También tiene flechas antiguas sobre EMAs y Bandas de Bollinger (21, 2): "
    "ROJA = venta de menor riesgo después de un techo de clímax (máximo más bajo en la banda superior); "
    "AMARILLA = reventa donde muere el interés comprador (subida que se rechaza en la banda superior o en la "
    "EMA144/233 con tendencia bajista); VERDE = agotamiento de las ventas (aviso para cerrar cortos); "
    "AZUL = compra de menor riesgo (mínimo más alto después de la verde, o retroceso a un soporte con tendencia "
    "alcista). Recibes una flecha detectada por el bot con datos reales del mercado. Tu trabajo es vetar las que "
    "NO parecen una entrada de menor riesgo: estructura confusa, vela de rechazo débil, precio ya alejado del "
    "cierre de la flecha, contexto de 1 hora claramente en contra o niveles históricos que la frenan. "
    "Usa solo los datos del mensaje; no inventes nada. "
    "Responde SOLO con JSON válido, sin texto adicional, con este formato exacto: "
    '{"decision":"GO" o "NO_GO","confidence":número entre 0 y 1,"reason":"motivo breve en español"}'
)


@dataclass
class LLMDecision:
    allow: bool
    source: str                 # score_alto | llm | error | desactivado
    decision: Optional[str] = None
    confidence: Optional[float] = None
    reason: str = ""
    latency_ms: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def parse_json_response(text: str) -> Dict[str, Any]:
    """Extrae el JSON aunque venga envuelto en ``` o con texto alrededor."""
    if not text:
        raise ValueError("respuesta vacía")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", cleaned)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError(f"sin JSON en la respuesta: {text[:120]!r}")
    data = json.loads(match.group(0))
    decision = str(data.get("decision", "")).strip().upper().replace("-", "_").replace(" ", "_")
    if decision not in ("GO", "NO_GO"):
        raise ValueError(f"decisión inválida: {data.get('decision')!r}")
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {"decision": decision, "confidence": max(0.0, min(1.0, confidence)),
            "reason": str(data.get("reason", ""))[:500]}


def _fmt(value: Any, digits: int = 6) -> str:
    try:
        return f"{float(value):.{digits}g}"
    except (TypeError, ValueError):
        return str(value)


def build_prompt(signal, context: Dict[str, Any]) -> str:
    """Texto que se envía al modelo (datos objetivos, en español)."""
    side_txt = "COMPRA (LONG)" if signal.side == "LONG" else "VENTA (SHORT)"
    label = getattr(signal, "label", "") or signal.side
    lines = [
        f"FLECHA {label} → {side_txt} en {signal.symbol} (velas de {signal.timeframe}; contexto en {signal.trend_timeframe})",
        f"Vela de la flecha: cierre {signal.candle_close_time.isoformat()} a {_fmt(signal.entry_ref)} | "
        f"precio actual {_fmt(context.get('price'))}",
        f"Puntaje {signal.score:.2f} = 50% contexto ({getattr(signal, 'contexto', 0):.2f}) + 50% rechazo "
        f"({getattr(signal, 'rechazo', 0):.2f}) | mínimo {context.get('min_score')}",
    ]
    reasons = list(getattr(signal, "reasons", None) or [])
    if reasons:
        lines.append("Lo que vio el bot:")
        lines += [f"  - {r}" for r in reasons]
    if signal.components:
        lines.append(f"Detalle: {json.dumps(signal.components, ensure_ascii=False, default=str)}")
    rejection = signal.rejection or {}
    lines.append(f"Rechazo: puntaje {rejection.get('score')} | contextos cumplidos: {rejection.get('contexts_met')}")
    lines.append(f"Stop: {_fmt(signal.stop_loss)} (a {signal.sl_distance_pct * 100:.2f}% del cierre de la flecha)")
    plan = context.get("plan") or {}
    if plan:
        lines.append(
            f"Plan: entrada {_fmt(plan.get('entry_price'))} | TP1 {_fmt(plan.get('tp1_price'))} (2R) | "
            f"TP2 {_fmt(plan.get('tp2_price'))} (5R) | break-even a 3R | apalancamiento {plan.get('leverage')}x | "
            f"comisiones ida y vuelta = {plan.get('fee_to_risk', 0) * 100:.0f}% del riesgo"
        )
    if context.get("funding_rate") is not None:
        lines.append(f"Funding actual: {context['funding_rate'] * 100:.4f}% por periodo")
    htf = context.get("htf") or {}
    if htf:
        lines.append(f"Velas de 1 hora: fase {htf.get('fase')} | EMAs {htf.get('pila')} | precio a "
                     f"{htf.get('distancia_ema55_pct')}% de la EMA55 | posición en las bandas {htf.get('posicion_bandas_pct')}%")
    hist = context.get("hist")
    if hist:
        lines.append(
            f"Historial en Bybit desde {hist.get('listing_date')}: ATH {_fmt(hist.get('ath'))} ({hist.get('pct_from_ath')}%), "
            f"ATL {_fmt(hist.get('atl'))} (+{hist.get('pct_from_atl')}%), posición en el rango {hist.get('range_position_pct')}%"
        )
    if context.get("memoria"):
        lines.append(f"Resultados anteriores de esta flecha/patrón: {context['memoria']}")
    if signal.pattern_name:
        lines.append(f"Patrón WASHI parecido: {signal.pattern_name}")
    candles: Optional[pd.DataFrame] = context.get("candles")
    if candles is not None and not candles.empty:
        lines.append("Últimas velas (hora UTC | apertura | máximo | mínimo | cierre | volumen/promedio):")
        for ts, row in candles.tail(24).iterrows():
            ratio = row.get("vol_ratio")
            ratio_txt = f"{ratio:.2f}" if ratio is not None and pd.notna(ratio) else "-"
            lines.append(f"{ts.strftime('%m-%d %H:%M')} | {_fmt(row['open'])} | {_fmt(row['high'])} | "
                         f"{_fmt(row['low'])} | {_fmt(row['close'])} | {ratio_txt}")
        last = candles.iloc[-1]
        levels = []
        for key, label_ in (("ema21", "EMA21"), ("ema_fast", "EMA55"), ("ema_mid", "EMA144"), ("ema_slow", "EMA233"),
                            ("bb_upper", "Banda superior"), ("bb_lower", "Banda inferior")):
            if key in candles.columns and pd.notna(last[key]):
                levels.append(f"{label_} {_fmt(last[key])}")
        if levels:
            lines.append("Niveles en la vela de la flecha: " + " | ".join(levels))
    lines.append('¿Se ejecuta? Responde solo con el JSON {"decision","confidence","reason"}.')
    return "\n".join(lines)


class LLMFilter:
    def __init__(self, cfg: Config, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.cfg = cfg
        self.provider = cfg.llm_provider
        self.model = cfg.llm_model
        self.transport = transport
        if self.enabled:
            logger.info(f"[LLM] Activo: {self.provider} / {self.model}")
        else:
            logger.info("[LLM] Desactivado (sin proveedor o sin clave): las señales se deciden por reglas.")

    @property
    def enabled(self) -> bool:
        return self.provider != "none" and bool(self.cfg.llm_api_key)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.cfg.llm_timeout, transport=self.transport)

    async def _call_anthropic(self, prompt: str) -> str:
        headers = {
            "x-api-key": self.cfg.llm_api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        body = {"model": self.model, "max_tokens": 300, "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}]}
        async with self._client() as client:
            resp = await client.post(ANTHROPIC_URL, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        return "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")

    async def _call_openai_compatible(self, url: str, prompt: str) -> str:
        headers = {"Authorization": f"Bearer {self.cfg.llm_api_key}", "Content-Type": "application/json"}
        if self.provider == "openrouter":
            headers.update({"HTTP-Referer": "https://github.com/dossacale-stack/unico-bot", "X-Title": "Unico Bot"})
        body = {"model": self.model, "temperature": 0.1, "max_tokens": 300,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]}
        async with self._client() as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def _ask(self, prompt: str) -> str:
        if self.provider == "anthropic":
            return await self._call_anthropic(prompt)
        if self.provider == "openrouter":
            return await self._call_openai_compatible(OPENROUTER_URL, prompt)
        if self.provider == "groq":
            return await self._call_openai_compatible(GROQ_URL, prompt)
        raise RuntimeError(f"Proveedor LLM no soportado: {self.provider}")

    async def evaluate(self, signal, context: Dict[str, Any]) -> LLMDecision:
        if signal.score >= self.cfg.llm_auto_approve_score:
            return LLMDecision(True, "score_alto", reason=f"Puntaje alto ({signal.score:.2f}): aprobada sin consultar")
        if not self.enabled:
            return LLMDecision(True, "desactivado", reason="LLM desactivado: decide la regla")

        prompt = build_prompt(signal, context)
        last_error: Optional[Exception] = None
        start = time.perf_counter()
        for attempt in range(2):
            try:
                text = await self._ask(prompt)
                parsed = parse_json_response(text)
                latency = int((time.perf_counter() - start) * 1000)
                allow = parsed["decision"] == "GO" and parsed["confidence"] >= self.cfg.llm_min_confidence
                return LLMDecision(allow, "llm", parsed["decision"], parsed["confidence"], parsed["reason"],
                                   latency, self.provider, self.model)
            except Exception as exc:
                last_error = exc
                logger.warning(f"[LLM] Intento {attempt + 1} fallido para {signal.symbol}: {exc}")
        latency = int((time.perf_counter() - start) * 1000)
        allow = self.cfg.llm_on_error == "allow"
        action = "se sigue con las reglas" if allow else "no se opera"
        return LLMDecision(allow, "error", None, None, f"Error del LLM ({last_error}); {action}",
                           latency, self.provider, self.model)
