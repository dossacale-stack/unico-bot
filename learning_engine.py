# washi_aprende.py - WASHI que aprende de toda la historia

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Any
from collections import defaultdict
import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("WashiAprende")

@dataclass
class NivelHistorico:
    precio: float
    tipo: str
    fuerza: int
    veces_tocado: int
    volumen_promedio: float
    comportamiento: str
    fecha_primer_touch: datetime
    fecha_ultimo_touch: datetime
    confianza: float = 0.5

@dataclass
class TrampaDeMercado:
    precio_trampa: float
    tipo: str
    fecha: datetime
    volumen: float
    direccion_falsa: str
    direccion_real: str
    confianza: float

class WashiAprende:
    def __init__(self, simbolo: str, df_completo: pd.DataFrame):
        self.simbolo = simbolo
        self.df = df_completo
        self.niveles_historicos: List[NivelHistorico] = []
        self.trampas: List[TrampaDeMercado] = []
        self.patrones_aprendidos: Dict[str, Any] = {}
        self.memoria = self._construir_memoria()
        self._aprender_de_toda_la_historia()
        
        logger.info(f"🧠 WASHI ha aprendido de {len(self.df)} velas")
        logger.info(f"   Niveles: {len(self.niveles_historicos)} | Trampas: {len(self.trampas)}")

    def _construir_memoria(self) -> Dict:
        precios = self.df['close'].values
        highs = self.df['high'].values
        lows = self.df['low'].values
        volumes = self.df['volume'].values
        
        return {
            'precio_maximo_historico': highs.max(),
            'precio_minimo_historico': lows.min(),
            'precio_actual': precios[-1],
            'volumen_maximo': volumes.max(),
            'volumen_promedio': volumes.mean(),
            'rango_historico': highs.max() - lows.min(),
            'volatilidad_historica': self._calcular_volatilidad(),
        }

    def _aprender_de_toda_la_historia(self):
        self._identificar_niveles_historicos()
        self._detectar_trampas()
        logger.info("✅ WASHI ha completado su aprendizaje")

    def _identificar_niveles_historicos(self):
        highs = self.df['high'].values
        lows = self.df['low'].values
        window = 50
        
        for i in range(window, len(self.df) - window):
            if highs[i] == max(highs[i-window:i+window]):
                nivel = NivelHistorico(
                    precio=highs[i],
                    tipo="RESISTENCIA",
                    fuerza=self._calcular_fuerza(highs[i]),
                    veces_tocado=self._contar_toques(highs[i], 'high'),
                    volumen_promedio=self._volumen_en_nivel(highs[i]),
                    comportamiento="DISTRIBUCION",
                    fecha_primer_touch=self.df.index[i-window],
                    fecha_ultimo_touch=self.df.index[i]
                )
                self.niveles_historicos.append(nivel)
            
            if lows[i] == min(lows[i-window:i+window]):
                nivel = NivelHistorico(
                    precio=lows[i],
                    tipo="SOPORTE",
                    fuerza=self._calcular_fuerza(lows[i]),
                    veces_tocado=self._contar_toques(lows[i], 'low'),
                    volumen_promedio=self._volumen_en_nivel(lows[i]),
                    comportamiento="ACUMULACION",
                    fecha_primer_touch=self.df.index[i-window],
                    fecha_ultimo_touch=self.df.index[i]
                )
                self.niveles_historicos.append(nivel)

    def _detectar_trampas(self):
        for i in range(100, len(self.df) - 100):
            if self._es_fakeout_alcista(i):
                self.trampas.append(TrampaDeMercado(
                    precio_trampa=self.df['high'].iloc[i],
                    tipo="FAKEOUT",
                    fecha=self.df.index[i],
                    volumen=self.df['volume'].iloc[i],
                    direccion_falsa="LONG_FALSO",
                    direccion_real="REAL_SHORT",
                    confianza=0.7
                ))
            if self._es_fakeout_bajista(i):
                self.trampas.append(TrampaDeMercado(
                    precio_trampa=self.df['low'].iloc[i],
                    tipo="FAKEOUT",
                    fecha=self.df.index[i],
                    volumen=self.df['volume'].iloc[i],
                    direccion_falsa="SHORT_FALSO",
                    direccion_real="REAL_LONG",
                    confianza=0.7
                ))

    def _es_fakeout_alcista(self, idx: int) -> bool:
        resistencia = self.df['high'].iloc[idx-20:idx].max()
        if self.df['close'].iloc[idx] <= resistencia * 1.01:
            return False
        if self.df['close'].iloc[idx+1] > self.df['close'].iloc[idx]:
            return False
        volumen_promedio = self.df['volume'].iloc[idx-20:idx].mean()
        if self.df['volume'].iloc[idx] < volumen_promedio * 1.5:
            return False
        return True

    def _es_fakeout_bajista(self, idx: int) -> bool:
        soporte = self.df['low'].iloc[idx-20:idx].min()
        if self.df['close'].iloc[idx] >= soporte * 0.99:
            return False
        if self.df['close'].iloc[idx+1] < self.df['close'].iloc[idx]:
            return False
        volumen_promedio = self.df['volume'].iloc[idx-20:idx].mean()
        if self.df['volume'].iloc[idx] < volumen_promedio * 1.5:
            return False
        return True

    def _calcular_fuerza(self, precio: float) -> int:
        fuerza = 50
        toques = self._contar_toques(precio, 'both')
        fuerza += min(toques * 5, 25)
        volumen = self._volumen_en_nivel(precio)
        if volumen > self.df['volume'].mean() * 1.5:
            fuerza += 15
        return min(fuerza, 100)

    def _contar_toques(self, precio: float, tipo: str) -> int:
        margen = precio * 0.005
        if tipo == 'high':
            return sum(1 for h in self.df['high'] if abs(h - precio) < margen)
        elif tipo == 'low':
            return sum(1 for l in self.df['low'] if abs(l - precio) < margen)
        else:
            return sum(1 for c in self.df['close'] if abs(c - precio) < margen)

    def _volumen_en_nivel(self, precio: float) -> float:
        margen = precio * 0.01
        mask = abs(self.df['close'] - precio) < margen
        return self.df.loc[mask, 'volume'].mean() if mask.sum() > 0 else 0

    def _calcular_volatilidad(self) -> float:
        returns = self.df['close'].pct_change()
        return returns.std() * 100

    def decidir(self, precio_actual: float, df_reciente: pd.DataFrame) -> Dict:
        posicion = self._posicion_en_historia(precio_actual)
        niveles_cerca = self._niveles_cerca(precio_actual)
        trampa_cerca = self._trampa_cerca(precio_actual)
        
        if trampa_cerca['hay_trampa']:
            return {
                'accion': 'ESPERAR',
                'razon': f"⚠️ TRAMPA DETECTADA: {trampa_cerca['direccion_falsa']}",
                'confianza': 0
            }
        
        if posicion['zona'] == 'ZONA_BAJA':
            return {
                'accion': 'BUSCAR_COMPRA',
                'razon': f"✅ PRECIO EN SOPORTE HISTÓRICO ({posicion['porcentaje']:.1f}%)",
                'confianza': 0.7
            }
        
        if posicion['zona'] == 'ZONA_ALTA':
            return {
                'accion': 'BUSCAR_VENTA',
                'razon': f"✅ PRECIO EN RESISTENCIA HISTÓRICA ({posicion['porcentaje']:.1f}%)",
                'confianza': 0.7
            }
        
        for nivel in niveles_cerca[:3]:
            if nivel['tipo'] == 'SOPORTE' and nivel['fuerza'] > 60:
                return {
                    'accion': 'BUSCAR_COMPRA',
                    'razon': f"✅ SOPORTE FUERTE en {nivel['precio']:.4f}",
                    'confianza': 0.6
                }
            if nivel['tipo'] == 'RESISTENCIA' and nivel['fuerza'] > 60:
                return {
                    'accion': 'BUSCAR_VENTA',
                    'razon': f"✅ RESISTENCIA FUERTE en {nivel['precio']:.4f}",
                    'confianza': 0.6
                }
        
        return {
            'accion': 'ESPERAR',
            'razon': '🔍 Sin señal clara',
            'confianza': 0.2
        }

    def _posicion_en_historia(self, precio: float) -> Dict:
        precio_min = self.memoria['precio_minimo_historico']
        precio_max = self.memoria['precio_maximo_historico']
        rango = precio_max - precio_min
        if rango == 0:
            return {'zona': 'MEDIA', 'porcentaje': 50}
        porcentaje = (precio - precio_min) / rango * 100
        zona = "ZONA_ALTA" if porcentaje > 80 else "ZONA_BAJA" if porcentaje < 20 else "ZONA_MEDIA"
        return {'zona': zona, 'porcentaje': porcentaje}

    def _niveles_cerca(self, precio: float) -> List[Dict]:
        cercanos = []
        for nivel in self.niveles_historicos[:30]:
            distancia = abs(precio - nivel.precio) / precio * 100
            if distancia < 3:
                cercanos.append({
                    'precio': nivel.precio,
                    'tipo': nivel.tipo,
                    'fuerza': nivel.fuerza,
                    'distancia': distancia,
                    'confianza': nivel.confianza
                })
        return sorted(cercanos, key=lambda x: x['distancia'])

    def _trampa_cerca(self, precio: float) -> Dict:
        for trampa in self.trampas[-50:]:
            distancia = abs(precio - trampa.precio_trampa) / precio * 100
            if distancia < 1.5:
                return {
                    'hay_trampa': True,
                    'tipo': trampa.tipo,
                    'direccion_falsa': trampa.direccion_falsa,
                    'confianza': trampa.confianza
                }
        return {'hay_trampa': False}
