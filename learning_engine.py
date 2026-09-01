# learning_engine.py - Motor de Aprendizaje WASHI UNIFICADO
# ============================================================
# REEMPLAZA COMPLETAMENTE al antiguo learning_engine.py
# Integra TODA la funcionalidad de WASHI APRENDE
# 
# CARACTERÍSTICAS:
#   - Aprende de TODA la historia del mercado
#   - Identifica niveles históricos (soportes/resistencias)
#   - Detecta trampas de mercado (fakeouts)
#   - Analiza comportamiento en niveles
#   - Registra y aprende de cada operación
#   - Genera pesos dinámicos para símbolos y patrones
#   - Predice oportunidades futuras

import logging
import sqlite3
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
import pandas as pd
import numpy as np

logger = logging.getLogger("LearningEngine")

# ═══════════════════════════════════════════════════════════════
#  DATACLASSES PARA APRENDIZAJE
# ═══════════════════════════════════════════════════════════════

@dataclass
class NivelHistorico:
    """Nivel importante en la historia del precio"""
    precio: float
    tipo: str  # "MAXIMO", "MINIMO", "SOPORTE", "RESISTENCIA", "ZONA"
    fuerza: int  # 0-100
    veces_tocado: int
    volumen_promedio: float
    comportamiento: str  # "ACUMULACION", "DISTRIBUCION", "CONSOLIDACION"
    fecha_primer_touch: datetime
    fecha_ultimo_touch: datetime
    confianza: float = 0.5

@dataclass
class TrampaDeMercado:
    """Trampa de mercado identificada"""
    precio_trampa: float
    tipo: str  # "FAKEOUT", "LIQUIDEZ", "SHAKEOUT"
    fecha: datetime
    volumen: float
    direccion_falsa: str  # "LONG_FALSO", "SHORT_FALSO"
    direccion_real: str   # "REAL_LONG", "REAL_SHORT"
    confianza: float

@dataclass
class TradeRecord:
    """Registro completo de una operación"""
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
    nivel_historico_used: Optional[float] = None
    trampa_detectada: bool = False

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


class ModoAprendizaje(Enum):
    """Modos de aprendizaje del motor"""
    PASIVO = "PASIVO"          # Solo registra, no recomienda
    ACTIVO = "ACTIVO"          # Recomienda y ajusta pesos
    AUTONOMO = "AUTONOMO"      # Toma decisiones automáticas


# ═══════════════════════════════════════════════════════════════
#  LEARNING ENGINE - VERSIÓN WASHI
# ═══════════════════════════════════════════════════════════════

class LearningEngine:
    """
    Motor de Aprendizaje WASHI UNIFICADO.
    Aprende de TODA la historia del mercado y de cada operación.
    """
    
    def __init__(
        self,
        db_path: str = "patterns.db",
        modo: ModoAprendizaje = ModoAprendizaje.ACTIVO,
        max_historia_velas: int = 1500,
    ):
        self.db_path = db_path
        self.modo = modo
        self.max_historia_velas = max_historia_velas
        
        # Estructuras de aprendizaje
        self.niveles_historicos: Dict[str, List[NivelHistorico]] = {}
        self.trampas: Dict[str, List[TrampaDeMercado]] = {}
        self.patrones_aprendidos: Dict[str, Dict] = {}
        self.historial_trades: Dict[str, List[TradeRecord]] = {}
        self.memorias: Dict[str, Dict] = {}
        
        # Pesos aprendidos
        self._symbol_weights: Dict[str, float] = {}
        self._pattern_weights: Dict[int, float] = {}
        self._hour_weights: Dict[int, float] = {}
        self._weekday_weights: Dict[int, float] = {}
        
        # Cache para decisión rápida
        self._decision_cache: Dict[str, Dict] = {}
        self._cache_time: Dict[str, float] = {}
        self._cache_ttl = 60  # segundos
        
        # Inicializar tablas
        self._init_tables()
        
        # Cargar datos persistidos
        self._load_from_db()
        
        logger.info(f"🧠 LEARNING ENGINE WASHI UNIFICADO")
        logger.info(f"   Modo: {self.modo.value}")
        logger.info(f"   Símbolos aprendidos: {len(self.memorias)}")
        logger.info(f"   Niveles totales: {sum(len(v) for v in self.niveles_historicos.values())}")
        logger.info(f"   Trampas totales: {sum(len(v) for v in self.trampas.values())}")
        logger.info(f"   Trades registrados: {sum(len(v) for v in self.historial_trades.values())}")

    # ═══════════════════════════════════════════════════════════
    # 1. INICIALIZACIÓN DE BASE DE DATOS
    # ═══════════════════════════════════════════════════════════

    def _init_tables(self):
        """Inicializa las tablas de aprendizaje WASHI"""
        with sqlite3.connect(self.db_path) as conn:
            # Tabla de niveles históricos (WASHI)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS washi_niveles (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol              TEXT NOT NULL,
                    precio              REAL NOT NULL,
                    tipo                TEXT NOT NULL,
                    fuerza              INTEGER DEFAULT 50,
                    veces_tocado        INTEGER DEFAULT 0,
                    volumen_promedio    REAL DEFAULT 0,
                    comportamiento      TEXT,
                    fecha_primer_touch  TEXT,
                    fecha_ultimo_touch  TEXT,
                    confianza           REAL DEFAULT 0.5,
                    created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, precio, tipo)
                )
            """)
            
            # Tabla de trampas de mercado (WASHI)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS washi_trampas (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol              TEXT NOT NULL,
                    precio_trampa       REAL NOT NULL,
                    tipo                TEXT NOT NULL,
                    fecha               TEXT NOT NULL,
                    volumen             REAL NOT NULL,
                    direccion_falsa     TEXT NOT NULL,
                    direccion_real      TEXT NOT NULL,
                    confianza           REAL DEFAULT 0.7,
                    created_at          TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Tabla de trades WASHI
            conn.execute("""
                CREATE TABLE IF NOT EXISTS washi_trades (
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
                    nivel_historico_used REAL,
                    trampa_detectada    BOOLEAN DEFAULT 0,
                    created_at          TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Tabla de pesos aprendidos
            conn.execute("""
                CREATE TABLE IF NOT EXISTS washi_pesos (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol              TEXT,
                    pattern_id          INTEGER,
                    tipo                TEXT NOT NULL,
                    valor               REAL DEFAULT 1.0,
                    total_trades        INTEGER DEFAULT 0,
                    wins                INTEGER DEFAULT 0,
                    losses              INTEGER DEFAULT 0,
                    last_updated        TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, pattern_id, tipo)
                )
            """)
            
            conn.commit()
            logger.debug("[LearningEngine] Tablas WASHI inicializadas")

    # ═══════════════════════════════════════════════════════════
    # 2. APRENDER DE UN SÍMBOLO (TODA LA HISTORIA)
    # ═══════════════════════════════════════════════════════════

    async def aprender_simbolo(self, symbol: str, df: pd.DataFrame, api_manager=None) -> bool:
        """
        El motor WASHI aprende de TODA la historia de un símbolo.
        """
        if df is None or len(df) < 100:
            logger.warning(f"[LearningEngine] Datos insuficientes para {symbol} ({len(df) if df is not None else 0} velas)")
            return False
        
        logger.info(f"📚 WASHI estudiando {len(df)} velas de {symbol}...")
        
        try:
            # 1. Construir memoria
            memoria = self._construir_memoria(symbol, df)
            self.memorias[symbol] = memoria
            
            # 2. Identificar niveles históricos
            niveles = self._identificar_niveles_historicos(symbol, df)
            self.niveles_historicos[symbol] = niveles
            
            # 3. Detectar trampas
            trampas = self._detectar_trampas(symbol, df)
            self.trampas[symbol] = trampas
            
            # 4. Aprender patrones
            self._aprender_patrones(symbol, df)
            
            # 5. Guardar en DB
            self._guardar_niveles(symbol, niveles)
            self._guardar_trampas(symbol, trampas)
            
            logger.info(f"✅ WASHI aprendió {symbol}:")
            logger.info(f"   Niveles: {len(niveles)} | Trampas: {len(trampas)}")
            logger.info(f"   Rango histórico: {memoria['precio_minimo']:.4f} - {memoria['precio_maximo']:.4f}")
            
            return True
            
        except Exception as e:
            logger.error(f"[LearningEngine] Error aprendiendo {symbol}: {e}")
            return False

    def _construir_memoria(self, symbol: str, df: pd.DataFrame) -> Dict:
        """Construye la memoria del símbolo"""
        return {
            'symbol': symbol,
            'precio_maximo': float(df['high'].max()),
            'precio_minimo': float(df['low'].min()),
            'precio_actual': float(df['close'].iloc[-1]),
            'volumen_maximo': float(df['volume'].max()),
            'volumen_promedio': float(df['volume'].mean()),
            'rango_historico': float(df['high'].max() - df['low'].min()),
            'volatilidad_historica': float(df['close'].pct_change().std() * 100),
            'fecha_inicio': df.index[0].isoformat() if hasattr(df.index[0], 'isoformat') else str(df.index[0]),
            'fecha_fin': df.index[-1].isoformat() if hasattr(df.index[-1], 'isoformat') else str(df.index[-1]),
            'total_velas': len(df)
        }

    # ═══════════════════════════════════════════════════════════
    # 3. IDENTIFICAR NIVELES HISTÓRICOS
    # ═══════════════════════════════════════════════════════════

    def _identificar_niveles_historicos(self, symbol: str, df: pd.DataFrame) -> List[NivelHistorico]:
        """Identifica niveles importantes en la historia"""
        niveles = []
        highs = df['high'].values
        lows = df['low'].values
        window = 50
        
        for i in range(window, len(df) - window):
            # Máximo local → Resistencia
            if highs[i] == max(highs[i-window:i+window]):
                nivel = NivelHistorico(
                    precio=float(highs[i]),
                    tipo="RESISTENCIA",
                    fuerza=self._calcular_fuerza_nivel(df, float(highs[i])),
                    veces_tocado=self._contar_toques(df, float(highs[i]), 'high'),
                    volumen_promedio=self._volumen_en_nivel(df, float(highs[i])),
                    comportamiento=self._analizar_comportamiento_nivel(df, float(highs[i])),
                    fecha_primer_touch=df.index[i-window],
                    fecha_ultimo_touch=df.index[i],
                    confianza=self._calcular_confianza_nivel(df, float(highs[i]))
                )
                niveles.append(nivel)
            
            # Mínimo local → Soporte
            if lows[i] == min(lows[i-window:i+window]):
                nivel = NivelHistorico(
                    precio=float(lows[i]),
                    tipo="SOPORTE",
                    fuerza=self._calcular_fuerza_nivel(df, float(lows[i])),
                    veces_tocado=self._contar_toques(df, float(lows[i]), 'low'),
                    volumen_promedio=self._volumen_en_nivel(df, float(lows[i])),
                    comportamiento=self._analizar_comportamiento_nivel(df, float(lows[i])),
                    fecha_primer_touch=df.index[i-window],
                    fecha_ultimo_touch=df.index[i],
                    confianza=self._calcular_confianza_nivel(df, float(lows[i]))
                )
                niveles.append(nivel)
        
        # Ordenar por fuerza
        niveles.sort(key=lambda x: x.fuerza, reverse=True)
        
        # Guardar solo los más fuertes (top 50)
        return niveles[:50]

    def _calcular_fuerza_nivel(self, df: pd.DataFrame, precio: float) -> int:
        """Calcula fuerza de un nivel (0-100)"""
        fuerza = 50
        toques = self._contar_toques(df, precio, 'both')
        fuerza += min(toques * 5, 25)
        volumen = self._volumen_en_nivel(df, precio)
        if volumen > df['volume'].mean() * 1.5:
            fuerza += 15
        elif volumen > df['volume'].mean() * 1.2:
            fuerza += 8
        return min(fuerza, 100)

    def _contar_toques(self, df: pd.DataFrame, precio: float, tipo: str) -> int:
        """Cuenta cuántas veces tocó este nivel"""
        margen = precio * 0.005
        if tipo == 'high':
            return int(sum(1 for h in df['high'] if abs(h - precio) < margen))
        elif tipo == 'low':
            return int(sum(1 for l in df['low'] if abs(l - precio) < margen))
        else:
            return int(sum(1 for c in df['close'] if abs(c - precio) < margen))

    def _volumen_en_nivel(self, df: pd.DataFrame, precio: float) -> float:
        """Volumen promedio en este nivel de precio"""
        margen = precio * 0.01
        mask = abs(df['close'] - precio) < margen
        return float(df.loc[mask, 'volume'].mean()) if mask.sum() > 0 else 0

    def _analizar_comportamiento_nivel(self, df: pd.DataFrame, precio: float) -> str:
        """Analiza comportamiento en este nivel"""
        margen = precio * 0.01
        cercanas = df[abs(df['close'] - precio) < margen]
        if len(cercanas) < 10:
            return "NEUTRAL"
        
        cambio = (cercanas['close'].iloc[-1] - cercanas['close'].iloc[0]) / cercanas['close'].iloc[0]
        volumen_ratio = cercanas['volume'].mean() / df['volume'].mean()
        
        if cambio > 0.03 and volumen_ratio > 1.2:
            return "ACUMULACION"
        elif cambio < -0.03 and volumen_ratio > 1.2:
            return "DISTRIBUCION"
        elif abs(cambio) < 0.01 and volumen_ratio < 0.8:
            return "CONSOLIDACION"
        return "NEUTRAL"

    def _calcular_confianza_nivel(self, df: pd.DataFrame, precio: float) -> float:
        """Confianza en el nivel"""
        confianza = 0.5
        toques = self._contar_toques(df, precio, 'both')
        confianza += min(toques * 0.05, 0.3)
        volumen = self._volumen_en_nivel(df, precio)
        if volumen > df['volume'].mean() * 1.5:
            confianza += 0.2
        return min(confianza, 1.0)

    # ═══════════════════════════════════════════════════════════
    # 4. DETECTAR TRAMPAS DE MERCADO
    # ═══════════════════════════════════════════════════════════

    def _detectar_trampas(self, symbol: str, df: pd.DataFrame) -> List[TrampaDeMercado]:
        """Detecta trampas de mercado"""
        trampas = []
        
        if len(df) < 200:
            return trampas
        
        for i in range(100, len(df) - 100):
            # Fakeout alcista
            if self._es_fakeout_alcista(df, i):
                trampas.append(TrampaDeMercado(
                    precio_trampa=float(df['high'].iloc[i]),
                    tipo="FAKEOUT",
                    fecha=df.index[i],
                    volumen=float(df['volume'].iloc[i]),
                    direccion_falsa="LONG_FALSO",
                    direccion_real="REAL_SHORT",
                    confianza=0.7
                ))
            
            # Fakeout bajista
            if self._es_fakeout_bajista(df, i):
                trampas.append(TrampaDeMercado(
                    precio_trampa=float(df['low'].iloc[i]),
                    tipo="FAKEOUT",
                    fecha=df.index[i],
                    volumen=float(df['volume'].iloc[i]),
                    direccion_falsa="SHORT_FALSO",
                    direccion_real="REAL_LONG",
                    confianza=0.7
                ))
        
        return trampas

    def _es_fakeout_alcista(self, df: pd.DataFrame, idx: int) -> bool:
        """Detecta falso breakout al alza"""
        if idx < 20 or idx > len(df) - 2:
            return False
        
        resistencia = df['high'].iloc[idx-20:idx].max()
        if df['close'].iloc[idx] <= resistencia * 1.01:
            return False
        if df['close'].iloc[idx+1] > df['close'].iloc[idx]:
            return False
        
        volumen_promedio = df['volume'].iloc[idx-20:idx].mean()
        if df['volume'].iloc[idx] < volumen_promedio * 1.5:
            return False
        return True

    def _es_fakeout_bajista(self, df: pd.DataFrame, idx: int) -> bool:
        """Detecta falso breakout a la baja"""
        if idx < 20 or idx > len(df) - 2:
            return False
        
        soporte = df['low'].iloc[idx-20:idx].min()
        if df['close'].iloc[idx] >= soporte * 0.99:
            return False
        if df['close'].iloc[idx+1] < df['close'].iloc[idx]:
            return False
        
        volumen_promedio = df['volume'].iloc[idx-20:idx].mean()
        if df['volume'].iloc[idx] < volumen_promedio * 1.5:
            return False
        return True

    def _aprender_patrones(self, symbol: str, df: pd.DataFrame):
        """Aprende patrones de comportamiento"""
        self.patrones_aprendidos[symbol] = {
            'acumulacion': [],
            'distribucion': [],
            'consolidacion': [],
            'breakout_real': [],
            'breakout_falso': [],
        }
        
        for nivel in self.niveles_historicos.get(symbol, [])[:20]:
            detalle = self._analizar_detalle_nivel(df, nivel.precio)
            if detalle:
                key = detalle['tipo']
                if key in self.patrones_aprendidos[symbol]:
                    self.patrones_aprendidos[symbol][key].append(detalle)

    def _analizar_detalle_nivel(self, df: pd.DataFrame, precio: float) -> Optional[Dict]:
        """Analiza detalle de comportamiento en un nivel"""
        if len(df) < 100:
            return None
        
        idx_cercano = (df['close'] - precio).abs().idxmin()
        idx_pos = df.index.get_loc(idx_cercano)
        
        if idx_pos < 50 or idx_pos > len(df) - 50:
            return None
        
        antes = df.iloc[idx_pos-50:idx_pos]
        despues = df.iloc[idx_pos:idx_pos+50]
        
        if antes.empty or despues.empty:
            return None
        
        precio_antes = antes['close'].iloc[-1]
        precio_despues = despues['close'].iloc[-1]
        cambio = (precio_despues - precio_antes) / precio_antes if precio_antes > 0 else 0
        
        vol_antes = antes['volume'].mean()
        vol_despues = despues['volume'].mean()
        vol_ratio = vol_despues / vol_antes if vol_antes > 0 else 0
        
        if cambio > 0.05 and vol_ratio > 1.2:
            return {'tipo': 'acumulacion', 'cambio': float(cambio), 'vol_ratio': float(vol_ratio), 'confianza': min(cambio * 10 + vol_ratio * 0.5, 1.0)}
        elif cambio < -0.05 and vol_ratio > 1.2:
            return {'tipo': 'distribucion', 'cambio': float(cambio), 'vol_ratio': float(vol_ratio), 'confianza': min(abs(cambio) * 10 + vol_ratio * 0.5, 1.0)}
        elif abs(cambio) < 0.01 and vol_ratio < 0.8:
            return {'tipo': 'consolidacion', 'cambio': float(cambio), 'vol_ratio': float(vol_ratio), 'confianza': 0.5}
        return None

    # ═══════════════════════════════════════════════════════════
    # 5. DECISIÓN WASHI (EL CORAZÓN DEL APRENDIZAJE)
    # ═══════════════════════════════════════════════════════════

    def decidir(self, symbol: str, precio_actual: float, df_reciente: pd.DataFrame) -> Dict:
        """
        Toma decisión basada en TODO lo que aprendió.
        Esta es la función principal que usa el bot.
        """
        # Verificar caché
        if symbol in self._decision_cache:
            if time.time() - self._cache_time.get(symbol, 0) < self._cache_ttl:
                return self._decision_cache[symbol]
        
        # 1. Posición en la historia
        posicion = self._posicion_en_historia(symbol, precio_actual)
        
        # 2. Niveles cercanos
        niveles_cerca = self._niveles_cerca(symbol, precio_actual)
        
        # 3. Trampas cercanas
        trampa_cerca = self._trampa_cerca(symbol, precio_actual)
        
        # 4. Patrón cercano
        patron_cercano = self._patron_cercano(symbol, df_reciente)
        
        # 5. Pesos aprendidos
        peso_simbolo = self.get_symbol_weight(symbol)
        
        # ⚠️ PRIORIDAD 1: TRAMPAS
        if trampa_cerca['hay_trampa']:
            decision = {
                'accion': 'ESPERAR',
                'razon': f"⚠️ TRAMPA DETECTADA: {trampa_cerca['direccion_falsa']}",
                'confianza': 0,
                'nivel': 'PELIGRO',
                'detalle': trampa_cerca,
                'peso_simbolo': peso_simbolo
            }
            self._decision_cache[symbol] = decision
            self._cache_time[symbol] = time.time()
            return decision
        
        # 🟢 PRIORIDAD 2: SOPORTE HISTÓRICO → COMPRA
        if posicion['zona'] == 'ZONA_BAJA':
            decision = {
                'accion': 'BUSCAR_COMPRA',
                'razon': f"✅ PRECIO EN SOPORTE HISTÓRICO ({posicion['porcentaje']:.1f}% del rango)",
                'confianza': 0.7 * peso_simbolo,
                'nivel': 'OPORTUNIDAD',
                'detalle': posicion,
                'peso_simbolo': peso_simbolo
            }
            self._decision_cache[symbol] = decision
            self._cache_time[symbol] = time.time()
            return decision
        
        # 🔴 PRIORIDAD 3: RESISTENCIA HISTÓRICA → VENTA
        if posicion['zona'] == 'ZONA_ALTA':
            decision = {
                'accion': 'BUSCAR_VENTA',
                'razon': f"✅ PRECIO EN RESISTENCIA HISTÓRICA ({posicion['porcentaje']:.1f}% del rango)",
                'confianza': 0.7 * peso_simbolo,
                'nivel': 'OPORTUNIDAD',
                'detalle': posicion,
                'peso_simbolo': peso_simbolo
            }
            self._decision_cache[symbol] = decision
            self._cache_time[symbol] = time.time()
            return decision
        
        # 🟡 PRIORIDAD 4: NIVELES CERCANOS FUERTES
        for nivel in niveles_cerca[:3]:
            if nivel['tipo'] == 'SOPORTE' and nivel['fuerza'] > 60:
                decision = {
                    'accion': 'BUSCAR_COMPRA',
                    'razon': f"✅ SOPORTE FUERTE en {nivel['precio']:.4f} (fuerza: {nivel['fuerza']}%)",
                    'confianza': (nivel.get('confianza', 0.5) * 0.8) * peso_simbolo,
                    'nivel': 'SOPORTE',
                    'detalle': nivel,
                    'peso_simbolo': peso_simbolo
                }
                self._decision_cache[symbol] = decision
                self._cache_time[symbol] = time.time()
                return decision
            
            if nivel['tipo'] == 'RESISTENCIA' and nivel['fuerza'] > 60:
                decision = {
                    'accion': 'BUSCAR_VENTA',
                    'razon': f"✅ RESISTENCIA FUERTE en {nivel['precio']:.4f} (fuerza: {nivel['fuerza']}%)",
                    'confianza': (nivel.get('confianza', 0.5) * 0.8) * peso_simbolo,
                    'nivel': 'RESISTENCIA',
                    'detalle': nivel,
                    'peso_simbolo': peso_simbolo
                }
                self._decision_cache[symbol] = decision
                self._cache_time[symbol] = time.time()
                return decision
        
        # 🔵 PRIORIDAD 5: PATRONES DE ACUMULACIÓN/DISTRIBUCIÓN
        if patron_cercano['hay_patron']:
            if patron_cercano['tipo'] == 'acumulacion' and patron_cercano['confianza'] > 0.6:
                decision = {
                    'accion': 'BUSCAR_COMPRA',
                    'razon': f"✅ Patrón de ACUMULACIÓN detectado",
                    'confianza': patron_cercano['confianza'] * peso_simbolo,
                    'nivel': 'PATRON',
                    'detalle': patron_cercano,
                    'peso_simbolo': peso_simbolo
                }
                self._decision_cache[symbol] = decision
                self._cache_time[symbol] = time.time()
                return decision
            elif patron_cercano['tipo'] == 'distribucion' and patron_cercano['confianza'] > 0.6:
                decision = {
                    'accion': 'BUSCAR_VENTA',
                    'razon': f"✅ Patrón de DISTRIBUCIÓN detectado",
                    'confianza': patron_cercano['confianza'] * peso_simbolo,
                    'nivel': 'PATRON',
                    'detalle': patron_cercano,
                    'peso_simbolo': peso_simbolo
                }
                self._decision_cache[symbol] = decision
                self._cache_time[symbol] = time.time()
                return decision
        
        # ⚪ SIN SEÑAL CLARA
        decision = {
            'accion': 'ESPERAR',
            'razon': '🔍 No hay señal clara',
            'confianza': 0.2 * peso_simbolo,
            'nivel': 'NEUTRAL',
            'detalle': {
                'posicion': posicion,
                'niveles_cerca': len(niveles_cerca),
                'trampa_cerca': trampa_cerca['hay_trampa']
            },
            'peso_simbolo': peso_simbolo
        }
        self._decision_cache[symbol] = decision
        self._cache_time[symbol] = time.time()
        return decision

    def _posicion_en_historia(self, symbol: str, precio: float) -> Dict:
        """Dónde está el precio en la historia"""
        memoria = self.memorias.get(symbol, {})
        if not memoria:
            return {'zona': 'MEDIA', 'porcentaje': 50}
        
        precio_min = memoria.get('precio_minimo', precio * 0.5)
        precio_max = memoria.get('precio_maximo', precio * 1.5)
        rango = precio_max - precio_min
        
        if rango == 0:
            return {'zona': 'MEDIA', 'porcentaje': 50}
        
        porcentaje = (precio - precio_min) / rango * 100
        
        if porcentaje > 80:
            zona = "ZONA_ALTA (resistencia histórica)"
        elif porcentaje > 60:
            zona = "ZONA_MEDIA_ALTA"
        elif porcentaje > 40:
            zona = "ZONA_MEDIA"
        elif porcentaje > 20:
            zona = "ZONA_MEDIA_BAJA"
        else:
            zona = "ZONA_BAJA (soporte histórico)"
        
        return {
            'zona': zona,
            'porcentaje': porcentaje,
            'riesgo': 'ALTO' if porcentaje > 80 else 'BAJO' if porcentaje < 20 else 'MEDIO'
        }

    def _niveles_cerca(self, symbol: str, precio: float) -> List[Dict]:
        """Niveles importantes cerca del precio"""
        cercanos = []
        for nivel in self.niveles_historicos.get(symbol, [])[:30]:
            distancia = abs(precio - nivel.precio) / precio * 100 if precio > 0 else 100
            if distancia < 3:
                cercanos.append({
                    'precio': nivel.precio,
                    'tipo': nivel.tipo,
                    'fuerza': nivel.fuerza,
                    'distancia': distancia,
                    'confianza': nivel.confianza,
                    'comportamiento': nivel.comportamiento,
                    'veces_tocado': nivel.veces_tocado
                })
        return sorted(cercanos, key=lambda x: x['distancia'])

    def _trampa_cerca(self, symbol: str, precio: float) -> Dict:
        """Trampa de mercado cerca del precio"""
        for trampa in self.trampas.get(symbol, [])[-50:]:
            distancia = abs(precio - trampa.precio_trampa) / precio * 100 if precio > 0 else 100
            if distancia < 1.5:
                return {
                    'hay_trampa': True,
                    'tipo': trampa.tipo,
                    'precio': trampa.precio_trampa,
                    'direccion_falsa': trampa.direccion_falsa,
                    'direccion_real': trampa.direccion_real,
                    'confianza': trampa.confianza,
                    'distancia': distancia
                }
        return {'hay_trampa': False}

    def _patron_cercano(self, symbol: str, df_reciente: pd.DataFrame) -> Dict:
        """Busca patrón similar en la historia reciente"""
        if len(df_reciente) < 20:
            return {'hay_patron': False, 'tipo': 'NINGUNO', 'confianza': 0}
        
        precio = df_reciente['close'].iloc[-1]
        precio_hace_20 = df_reciente['close'].iloc[-20]
        cambio = (precio - precio_hace_20) / precio_hace_20 if precio_hace_20 > 0 else 0
        
        volumen_promedio = df_reciente['volume'].tail(20).mean()
        volumen_actual = df_reciente['volume'].iloc[-1]
        vol_ratio = volumen_actual / volumen_promedio if volumen_promedio > 0 else 0
        
        # Verificar si hay patrones aprendidos para este símbolo
        patrones_simbolo = self.patrones_aprendidos.get(symbol, {})
        if cambio > 0.02 and vol_ratio > 1.2:
            return {
                'hay_patron': True,
                'tipo': 'acumulacion',
                'confianza': min(0.5 + (cambio * 10), 0.9)
            }
        elif cambio < -0.02 and vol_ratio > 1.2:
            return {
                'hay_patron': True,
                'tipo': 'distribucion',
                'confianza': min(0.5 + (abs(cambio) * 10), 0.9)
            }
        
        return {'hay_patron': False, 'tipo': 'NINGUNO', 'confianza': 0}

    # ═══════════════════════════════════════════════════════════
    # 6. REGISTRAR TRADE (APRENDIZAJE CONTINUO)
    # ═══════════════════════════════════════════════════════════

    def register_trade(self, trade: TradeRecord) -> int:
        """
        Registra un trade para aprendizaje continuo.
        Esta es la función que el bot llama después de cerrar una operación.
        """
        # Guardar en memoria
        if trade.symbol not in self.historial_trades:
            self.historial_trades[trade.symbol] = []
        self.historial_trades[trade.symbol].append(trade)
        
        # Guardar en DB
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO washi_trades (
                    symbol, side, entry_price, exit_price, contracts,
                    leverage, position_usd, pnl_usd, pnl_percent,
                    entry_time, exit_time, duration_minutes, exit_reason,
                    pattern_id, pattern_score, signal_type, arrow_color,
                    ema_config, bb_config, volume_ratio, was_st_asset,
                    nivel_historico_used, trampa_detectada
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.symbol, trade.side, trade.entry_price, trade.exit_price,
                trade.contracts, trade.leverage, trade.position_usd,
                trade.pnl_usd, trade.pnl_percent,
                trade.entry_time.isoformat(), trade.exit_time.isoformat(),
                trade.duration_minutes, trade.exit_reason,
                trade.pattern_id, trade.pattern_score,
                trade.signal_type, trade.arrow_color,
                trade.ema_config, trade.bb_config,
                trade.volume_ratio, 1 if trade.was_st_asset else 0,
                trade.nivel_historico_used, 1 if trade.trampa_detectada else 0
            ))
            trade_id = cursor.lastrowid
            conn.commit()
        
        # Actualizar pesos
        self._actualizar_pesos(trade)
        
        logger.info(f"[LearningEngine] 📊 Trade registrado: {trade.symbol} {trade.side} | "
                   f"PnL: ${trade.pnl_usd:.2f} | {trade.exit_reason}")
        
        return trade_id

    def _actualizar_pesos(self, trade: TradeRecord):
        """Actualiza pesos aprendidos basado en el trade"""
        # Peso del símbolo
        if trade.symbol not in self._symbol_weights:
            self._symbol_weights[trade.symbol] = 1.0
        
        if trade.is_win:
            self._symbol_weights[trade.symbol] = min(2.0, self._symbol_weights[trade.symbol] * 1.05)
        else:
            self._symbol_weights[trade.symbol] = max(0.3, self._symbol_weights[trade.symbol] * 0.95)
        
        # Peso del patrón
        if trade.pattern_id:
            if trade.pattern_id not in self._pattern_weights:
                self._pattern_weights[trade.pattern_id] = 1.0
            
            if trade.is_win:
                self._pattern_weights[trade.pattern_id] = min(2.0, self._pattern_weights[trade.pattern_id] * 1.1)
            else:
                self._pattern_weights[trade.pattern_id] = max(0.3, self._pattern_weights[trade.pattern_id] * 0.9)
        
        # Guardar pesos en DB
        with sqlite3.connect(self.db_path) as conn:
            # Símbolo
            conn.execute("""
                INSERT INTO washi_pesos (symbol, tipo, valor, total_trades, wins, losses, last_updated)
                VALUES (?, 'symbol_weight', ?, 1, ?, ?, ?)
                ON CONFLICT(symbol, pattern_id, tipo) DO UPDATE SET
                    valor = ?,
                    total_trades = total_trades + 1,
                    wins = wins + ?,
                    losses = losses + ?,
                    last_updated = ?
            """, (
                trade.symbol,
                self._symbol_weights[trade.symbol],
                1 if trade.is_win else 0,
                1 if trade.is_loss else 0,
                datetime.now(timezone.utc).isoformat(),
                self._symbol_weights[trade.symbol],
                1 if trade.is_win else 0,
                1 if trade.is_loss else 0,
                datetime.now(timezone.utc).isoformat()
            ))
            
            # Patrón
            if trade.pattern_id:
                conn.execute("""
                    INSERT INTO washi_pesos (pattern_id, tipo, valor, total_trades, wins, losses, last_updated)
                    VALUES (?, 'pattern_weight', ?, 1, ?, ?, ?)
                    ON CONFLICT(symbol, pattern_id, tipo) DO UPDATE SET
                        valor = ?,
                        total_trades = total_trades + 1,
                        wins = wins + ?,
                        losses = losses + ?,
                        last_updated = ?
                """, (
                    trade.pattern_id,
                    self._pattern_weights[trade.pattern_id],
                    1 if trade.is_win else 0,
                    1 if trade.is_loss else 0,
                    datetime.now(timezone.utc).isoformat(),
                    self._pattern_weights[trade.pattern_id],
                    1 if trade.is_win else 0,
                    1 if trade.is_loss else 0,
                    datetime.now(timezone.utc).isoformat()
                ))
            
            conn.commit()

    # ═══════════════════════════════════════════════════════════
    # 7. OBTENER PESOS APRENDIDOS
    # ═══════════════════════════════════════════════════════════

    def get_symbol_weight(self, symbol: str) -> float:
        """Obtiene el peso aprendido de un símbolo"""
        return self._symbol_weights.get(symbol, 1.0)

    def get_pattern_weight(self, pattern_id: int) -> float:
        """Obtiene el peso aprendido de un patrón"""
        return self._pattern_weights.get(pattern_id, 1.0)

    def should_trade_symbol(self, symbol: str) -> Tuple[bool, str]:
        """Evalúa si se debe operar un símbolo basado en aprendizaje"""
        peso = self.get_symbol_weight(symbol)
        
        # Verificar historial
        trades = self.historial_trades.get(symbol, [])
        if len(trades) >= 10:
            wins = sum(1 for t in trades if t.is_win)
            win_rate = wins / len(trades)
            
            if win_rate < 0.35:
                return False, f"Win rate muy bajo: {win_rate*100:.1f}%"
            
            total_pnl = sum(t.pnl_usd for t in trades)
            if total_pnl < 0:
                return False, f"PnL total negativo: ${total_pnl:.2f}"
        
        if peso < 0.4:
            return False, f"Peso muy bajo: {peso:.2f}"
        
        return True, "Aprobado por aprendizaje"

    # ═══════════════════════════════════════════════════════════
    # 8. REPORTES Y EXPORTACIÓN
    # ═══════════════════════════════════════════════════════════

    def get_report(self, symbol: Optional[str] = None) -> Dict:
        """Genera reporte de aprendizaje"""
        if symbol:
            return self._get_report_simbolo(symbol)
        
        report = {
            'total_symbols': len(self.memorias),
            'total_niveles': sum(len(v) for v in self.niveles_historicos.values()),
            'total_trampas': sum(len(v) for v in self.trampas.values()),
            'total_trades': sum(len(v) for v in self.historial_trades.values()),
            'symbols': {}
        }
        
        for sym in self.memorias.keys():
            report['symbols'][sym] = self._get_report_simbolo(sym)
        
        return report

    def _get_report_simbolo(self, symbol: str) -> Dict:
        """Reporte de un símbolo específico"""
        trades = self.historial_trades.get(symbol, [])
        wins = sum(1 for t in trades if t.is_win)
        losses = sum(1 for t in trades if t.is_loss)
        total_pnl = sum(t.pnl_usd for t in trades)
        
        return {
            'memoria': self.memorias.get(symbol, {}),
            'niveles': len(self.niveles_historicos.get(symbol, [])),
            'trampas': len(self.trampas.get(symbol, [])),
            'trades': len(trades),
            'wins': wins,
            'losses': losses,
            'win_rate': wins / len(trades) if trades else 0,
            'total_pnl': total_pnl,
            'avg_pnl': total_pnl / len(trades) if trades else 0,
            'peso': self.get_symbol_weight(symbol),
            'debe_operar': self.should_trade_symbol(symbol)[0]
        }

    # ═══════════════════════════════════════════════════════════
    # 9. GUARDAR Y CARGAR DESDE DB
    # ═══════════════════════════════════════════════════════════

    def _guardar_niveles(self, symbol: str, niveles: List[NivelHistorico]):
        """Guarda niveles en DB"""
        with sqlite3.connect(self.db_path) as conn:
            for nivel in niveles:
                conn.execute("""
                    INSERT OR REPLACE INTO washi_niveles (
                        symbol, precio, tipo, fuerza, veces_tocado,
                        volumen_promedio, comportamiento, fecha_primer_touch,
                        fecha_ultimo_touch, confianza
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    symbol, nivel.precio, nivel.tipo, nivel.fuerza,
                    nivel.veces_tocado, nivel.volumen_promedio,
                    nivel.comportamiento,
                    nivel.fecha_primer_touch.isoformat(),
                    nivel.fecha_ultimo_touch.isoformat(),
                    nivel.confianza
                ))
            conn.commit()

    def _guardar_trampas(self, symbol: str, trampas: List[TrampaDeMercado]):
        """Guarda trampas en DB"""
        with sqlite3.connect(self.db_path) as conn:
            for trampa in trampas:
                conn.execute("""
                    INSERT OR REPLACE INTO washi_trampas (
                        symbol, precio_trampa, tipo, fecha,
                        volumen, direccion_falsa, direccion_real, confianza
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    symbol, trampa.precio_trampa, trampa.tipo,
                    trampa.fecha.isoformat(), trampa.volumen,
                    trampa.direccion_falsa, trampa.direccion_real,
                    trampa.confianza
                ))
            conn.commit()

    def _load_from_db(self):
        """Carga datos persistidos de la DB"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                # Cargar niveles
                rows = conn.execute("SELECT * FROM washi_niveles").fetchall()
                for row in rows:
                    symbol = row['symbol']
                    if symbol not in self.niveles_historicos:
                        self.niveles_historicos[symbol] = []
                    
                    nivel = NivelHistorico(
                        precio=row['precio'],
                        tipo=row['tipo'],
                        fuerza=row['fuerza'],
                        veces_tocado=row['veces_tocado'],
                        volumen_promedio=row['volumen_promedio'],
                        comportamiento=row['comportamiento'] or "NEUTRAL",
                        fecha_primer_touch=datetime.fromisoformat(row['fecha_primer_touch']),
                        fecha_ultimo_touch=datetime.fromisoformat(row['fecha_ultimo_touch']),
                        confianza=row['confianza']
                    )
                    self.niveles_historicos[symbol].append(nivel)
                
                # Cargar trampas
                rows = conn.execute("SELECT * FROM washi_trampas").fetchall()
                for row in rows:
                    symbol = row['symbol']
                    if symbol not in self.trampas:
                        self.trampas[symbol] = []
                    
                    trampa = TrampaDeMercado(
                        precio_trampa=row['precio_trampa'],
                        tipo=row['tipo'],
                        fecha=datetime.fromisoformat(row['fecha']),
                        volumen=row['volumen'],
                        direccion_falsa=row['direccion_falsa'],
                        direccion_real=row['direccion_real'],
                        confianza=row['confianza']
                    )
                    self.trampas[symbol].append(trampa)
                
                # Cargar pesos
                rows = conn.execute("SELECT * FROM washi_pesos WHERE tipo = 'symbol_weight'").fetchall()
                for row in rows:
                    if row['symbol']:
                        self._symbol_weights[row['symbol']] = row['valor']
                
                rows = conn.execute("SELECT * FROM washi_pesos WHERE tipo = 'pattern_weight'").fetchall()
                for row in rows:
                    if row['pattern_id']:
                        self._pattern_weights[row['pattern_id']] = row['valor']
                
                logger.debug(f"[LearningEngine] Cargados {len(self.niveles_historicos)} símbolos con niveles")
                logger.debug(f"[LearningEngine] Cargados {len(self.trampas)} símbolos con trampas")
                
        except Exception as e:
            logger.warning(f"[LearningEngine] Error cargando datos: {e}")

    # ═══════════════════════════════════════════════════════════
    # 10. EXPORTAR APRENDIZAJE A JSON
    # ═══════════════════════════════════════════════════════════

    def export_learning_data(self, filepath: str = "washi_learning_data.json") -> Dict:
        """Exporta todos los datos de aprendizaje a JSON"""
        report = self.get_report()
        
        # Convertir objetos a serializables
        report['timestamp'] = datetime.now(timezone.utc).isoformat()
        report['modo'] = self.modo.value
        
        with open(filepath, 'w') as f:
            json.dump(report, f, indent=2, default=str, ensure_ascii=False)
        
        logger.info(f"[LearningEngine] 📊 Datos exportados a {filepath}")
        return report


# ═══════════════════════════════════════════════════════════════
#  CLI PARA MONITOREAR APRENDIZAJE
# ═══════════════════════════════════════════════════════════════

def main():
    """CLI para monitorear el aprendizaje WASHI"""
    engine = LearningEngine()
    
    print("\n" + "=" * 70)
    print("🧠 LEARNING ENGINE WASHI - MONITOR")
    print("=" * 70)
    
    while True:
        print("\n📋 COMANDOS DISPONIBLES:")
        print("  1. Ver reporte completo")
        print("  2. Ver símbolos aprendidos")
        print("  3. Ver niveles de un símbolo")
        print("  4. Ver trampas de un símbolo")
        print("  5. Ver pesos aprendidos")
        print("  6. Exportar aprendizaje a JSON")
        print("  7. Salir")
        
        choice = input("\nSelecciona una opción (1-7): ").strip()
        
        if choice == "1":
            report = engine.get_report()
            print(f"\n📊 REPORTE GLOBAL:")
            print(f"   Símbolos: {report['total_symbols']}")
            print(f"   Niveles totales: {report['total_niveles']}")
            print(f"   Trampas totales: {report['total_trampas']}")
            print(f"   Trades totales: {report['total_trades']}")
            
            for sym, data in report['symbols'].items():
                print(f"\n   📌 {sym}:")
                print(f"      Niveles: {data['niveles']}")
                print(f"      Trampas: {data['trampas']}")
                print(f"      Trades: {data['trades']} | Win Rate: {data['win_rate']*100:.1f}%")
                print(f"      Peso: {data['peso']:.2f} | Debe operar: {'✅' if data['debe_operar'] else '❌'}")
        
        elif choice == "2":
            print("\n📋 SÍMBOLOS APRENDIDOS:")
            for sym in engine.memorias.keys():
                memoria = engine.memorias[sym]
                print(f"   {sym}: {memoria.get('total_velas', 0)} velas | "
                     f"${memoria.get('precio_minimo', 0):.4f} - ${memoria.get('precio_maximo', 0):.4f}")
        
        elif choice == "3":
            symbol = input("Símbolo: ").strip().upper()
            niveles = engine.niveles_historicos.get(symbol, [])
            print(f"\n📊 NIVELES DE {symbol}:")
            for n in niveles[:10]:
                print(f"   {n.tipo}: ${n.precio:.4f} | Fuerza: {n.fuerza}% | "
                     f"Tocado: {n.veces_tocado}x | Conf: {n.confianza:.1%}")
        
        elif choice == "4":
            symbol = input("Símbolo: ").strip().upper()
            trampas = engine.trampas.get(symbol, [])
            print(f"\n🕵️ TRAMPAS DE {symbol}:")
            for t in trampas[-10:]:
                print(f"   {t.tipo}: ${t.precio_trampa:.4f} | {t.direccion_falsa} → {t.direccion_real}")
        
        elif choice == "5":
            print("\n⚖️ PESOS APRENDIDOS:")
            print("   Símbolos:")
            for sym, peso in engine._symbol_weights.items():
                print(f"      {sym}: {peso:.2f}")
            print("   Patrones:")
            for pid, peso in list(engine._pattern_weights.items())[:10]:
                print(f"      ID {pid}: {peso:.2f}")
        
        elif choice == "6":
            filename = f"washi_learning_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            engine.export_learning_data(filename)
        
        elif choice == "7":
            print("👋 ¡Hasta luego!")
            break
        else:
            print("❌ Opción inválida.")


if __name__ == "__main__":
    main()
