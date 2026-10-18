"""Las 4 flechas de Karla: cada escenario dispara la flecha correcta y nada más."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

import scenarios as SC
import strategy as S
from conftest import random_walk
from rejection_analyzer import RejectionAnalyzer

P = S.StrategyParams(setups=(S.ROJA, S.AMARILLA, S.AZUL))   # flechas v9 (las de v10 tienen sus pruebas)
AN = RejectionAnalyzer(db_path=None)


def last(df, p=P, **kw):
    res, frame = S.evaluate_last_closed(df, symbol="X/USDT:USDT", p=p, analyzer=AN, log_rejection=False, **kw)
    return res, frame


@pytest.mark.parametrize("seed", range(4))
def test_roja_after_climax_lower_high_at_upper_band(seed):
    res, frame = last(SC.scenario_roja(seed=seed))
    sig = res.entry
    assert sig is not None and sig.setup == S.ROJA and sig.side == S.SHORT
    assert sig.ref_price is not None and frame.a["high"][-1] < sig.ref_price      # máximo más bajo
    assert sig.stop_loss > frame.a["high"][-1]
    assert sig.score >= P.min_score and any("Techo previo" in r for r in sig.reasons)


@pytest.mark.parametrize("seed", range(5))
def test_no_roja_without_climax(seed):
    res, _ = last(SC.scenario_roja(seed=seed, climax=False))
    assert res.entry is None or res.entry.setup != S.ROJA


def test_higher_high_is_not_roja_but_top_warning():
    res, _ = last(SC.scenario_roja(seed=0, lower_high=False))
    assert res.entry is None
    assert [e.kind for e in res.events] == [S.TOPE]


@pytest.mark.parametrize("seed", range(4))
def test_amarilla_rally_to_resistance_in_downtrend(seed):
    res, frame = last(SC.scenario_amarilla(seed=seed))
    sig = res.entry
    assert sig is not None and sig.setup == S.AMARILLA and sig.side == S.SHORT
    assert int(frame.a["fase"][-1]) <= 0
    assert any("banda superior" in r or "EMA" in r for r in sig.reasons)


@pytest.mark.parametrize("seed", range(4))
def test_verde_is_a_warning_not_an_entry(seed):
    res, _ = last(SC.scenario_verde(seed=seed))
    assert res.entry is None
    assert [e.kind for e in res.events] == [S.VERDE]
    assert res.events[0].closes_side == S.SHORT and res.events[0].exhaustion >= P.event_min_exhaustion


def test_verde_entry_only_when_enabled():
    res, _ = last(SC.scenario_verde(seed=0), p=replace(P, entry_on_green=True))
    assert res.entry is not None and res.entry.setup == S.VERDE and res.entry.side == S.LONG


@pytest.mark.parametrize("seed", range(4))
def test_azul_after_verde_higher_low(seed):
    res, frame = last(SC.scenario_azul_tras_verde(seed=seed))
    sig = res.entry
    assert sig is not None and sig.setup == S.AZUL and sig.variant == "tras_verde" and sig.side == S.LONG
    assert frame.a["low"][-1] > sig.ref_price                                     # mínimo más alto
    assert sig.stop_loss < frame.a["low"][-1]


def test_no_azul_after_verde_if_bounce_never_closed_above_ema55():
    res, _ = last(SC.scenario_azul_tras_verde(seed=0, bounce=0.002))
    assert res.entry is None or res.entry.variant != "tras_verde"


@pytest.mark.parametrize("seed", range(4))
def test_azul_pullback_in_uptrend(seed):
    res, frame = last(SC.scenario_azul_retroceso(seed=seed))
    sig = res.entry
    assert sig is not None and sig.setup == S.AZUL and sig.variant == "retroceso"
    assert int(frame.a["fase"][-1]) >= 0


def test_tope_warning_and_optional_entry():
    res, _ = last(SC.scenario_tope(seed=0))
    assert res.entry is None and [e.kind for e in res.events] == [S.TOPE]
    res, _ = last(SC.scenario_tope(seed=0), p=replace(P, entry_on_top=True))
    assert res.entry is not None and res.entry.setup == S.TOPE and res.entry.side == S.SHORT


def test_disabled_setup_is_discarded():
    res, _ = last(SC.scenario_amarilla(seed=0), p=replace(P, setups=(S.ROJA, S.AZUL)))
    assert res.entry is None or res.entry.setup != S.AMARILLA


def test_cycle_no_resell_right_after_confirmed_bottom():
    df = SC.scenario_amarilla(seed=0)
    frame = S.build_frame(df, P)
    i = len(frame) - 1
    state = S.ScanState(last_long_reversal=i - 10)
    res = S.evaluate_bar(frame, i, symbol="X", analyzer=AN, state=state)
    assert res.entry is None and any("AZUL tras la verde" in d.reason for d in res.discarded)
    res = S.evaluate_bar(frame, i, symbol="X", analyzer=AN, state=S.ScanState(last_long_reversal=i - P.swing_bars - 1))
    assert res.entry is not None and res.entry.setup == S.AMARILLA


def test_cycle_no_pullback_buy_right_after_confirmed_top():
    df = SC.scenario_azul_retroceso(seed=0)
    frame = S.build_frame(df, P)
    i = len(frame) - 1
    res = S.evaluate_bar(frame, i, symbol="X", analyzer=AN, state=S.ScanState(last_short_reversal=i - 5))
    assert res.entry is None and any("ROJA" in d.reason for d in res.discarded)


def test_htf_filter_blocks_pullbacks_against_1h():
    df = SC.scenario_azul_retroceso(seed=0)
    frame = S.build_frame(df, replace(P, htf_filter="tendencia"))
    i = len(frame) - 1
    blocked = S.evaluate_bar(frame, i, symbol="X", analyzer=AN, htf_fase=-1)
    assert blocked.entry is None and any("en contra" in d.reason for d in blocked.discarded)
    allowed = S.evaluate_bar(frame, i, symbol="X", analyzer=AN, htf_fase=0)
    assert allowed.entry is not None
    # Las reversiones (roja / azul tras verde) no dependen del filtro de 1h
    frame_r = S.build_frame(SC.scenario_roja(seed=0), replace(P, htf_filter="tendencia"))
    assert S.evaluate_bar(frame_r, len(frame_r) - 1, symbol="X", analyzer=AN, htf_fase=1).entry is not None


def test_stop_is_beyond_the_wick_by_at_least_one_atr():
    res, frame = last(SC.scenario_roja(seed=1))
    sig = res.entry
    extreme = float(frame.a["high"][-P.stop_lookback:].max())
    assert sig.stop_loss >= extreme + max(P.stop_buffer * extreme, P.stop_atr * float(frame.a["atr"][-1])) - 1e-9


def test_candle_patterns_and_phase():
    df = pd.DataFrame({
        "open": [10.0, 10.0, 10.5, 10.0],
        "high": [10.6, 11.2, 10.6, 10.1],
        "low": [9.9, 9.95, 9.9, 9.2],
        "close": [10.5, 10.05, 9.95, 10.05],
        "volume": [1.0] * 4,
    }, index=pd.date_range("2026-01-01", periods=4, freq="15min", tz="UTC"))
    f = S.build_features(df, replace(P, min_range_atr=0.0, pivot_bars=1))
    assert bool(f["pat_bear"].iloc[1]) and f["pat_name"].iloc[1] == "estrella fugaz"
    assert bool(f["pat_bear"].iloc[2]) and f["pat_name"].iloc[2] == "envolvente bajista"
    assert bool(f["pat_bull"].iloc[3]) and f["pat_name"].iloc[3] == "martillo"


def test_no_look_ahead_live_equals_backtest():
    """Lo que ve el bot en vivo en la vela i es idéntico a lo que ve el backtest en esa vela."""
    found = 0
    for seed in range(3):
        df = random_walk(700, seed=seed, drift=0.0008 if seed % 2 else -0.0008, vol=0.008, freq="15min")
        full = S.build_frame(df, P)
        history = S.scan_history(full, symbol="X", analyzer=AN, keep_discarded=True)
        for i in range(len(df) - 260, len(df)):
            live, _ = S.evaluate_last_closed(df.iloc[: i + 1], symbol="X", p=P, analyzer=AN, log_rejection=False)
            back = history.get(i, S.BarResult())
            assert (live.entry is None) == (back.entry is None), i
            if live.entry is not None:
                found += 1
                assert live.entry.setup_key == back.entry.setup_key
                assert live.entry.score == pytest.approx(back.entry.score, abs=1e-9)
                assert live.entry.stop_loss == pytest.approx(back.entry.stop_loss, rel=1e-9)
            assert [e.kind for e in live.events] == [e.kind for e in back.events]
    assert found > 0


def test_signal_serialization():
    res, _ = last(SC.scenario_roja(seed=0))
    data = res.entry.to_dict()
    assert data["setup_key"] == "ROJA" and isinstance(data["candle_close_time"], str)
    assert res.entry.setup_code == S.SETUP_CODES["ROJA"]


def test_fibonacci_levels_from_the_last_big_leg():
    """Bajada de 100 a 80 y rebote: el 61,8 % (92,36) es resistencia; tras una subida serían soportes."""
    closes = list(np.linspace(100, 80, 40)) + list(np.linspace(80, 91.5, 20))
    o = [closes[0]] + closes[:-1]
    df = pd.DataFrame({"open": o, "high": [max(a, b) * 1.001 for a, b in zip(o, closes)],
                       "low": [min(a, b) * 0.999 for a, b in zip(o, closes)], "close": closes, "volume": 1.0},
                      index=pd.date_range("2026-10-01", periods=60, freq="15min", tz="UTC"))
    last = pd.DataFrame({"open": [91.5], "high": [92.4], "low": [91.0], "close": [91.2], "volume": [1.0]},
                        index=[df.index[-1] + pd.Timedelta(minutes=15)])
    df = pd.concat([df, last])
    p = replace(P, fib_bars=60, fib_min_range_atr=1.0, fib_levels=(0.382, 0.5, 0.618, 0.786))
    f = S.build_features(df, p)
    row = f.iloc[-1]
    assert row["fib_up"] == 0 and row["fib_hi"] > 99 and row["fib_lo"] < 81
    assert bool(row["fib_res"]) and row["fib_res_lvl"] == pytest.approx(0.618)
    assert bool(row["res_touch"]) and bool(row["roja_touch"])
    frame = S.Frame(f, p)
    assert any("Fibonacci 61.8%" in t for t in S._touched(frame, len(f) - 1, S.SHORT))


def test_fibonacci_uses_only_past_candles():
    df = random_walk(700, seed=4, vol=0.008, freq="15min")
    pf = replace(P, fib_levels=(0.382, 0.618))
    full = S.build_features(df, pf)
    part = S.build_features(df.iloc[:500], pf)
    for col in ("fib_hi", "fib_lo", "fib_up", "fib_sup", "fib_res"):
        assert np.array_equal(full[col].iloc[:500].to_numpy(), part[col].to_numpy(), equal_nan=True), col


def test_fibonacci_is_off_by_default():
    f = S.build_features(random_walk(700, seed=4, vol=0.008, freq="15min"), P)
    assert not f["fib_sup"].any() and not f["fib_res"].any()


# ----------------------------------------------------------------------
# v10: interés / desinterés con las EMAs (morada 55, azul 144, amarilla 233) y salida en la zona
# ----------------------------------------------------------------------
P10 = S.StrategyParams()          # por defecto: DESINTERES e INTERES


@pytest.mark.parametrize("seed", [0, 1, 3, 4])
def test_desinteres_sell_when_price_cannot_hold_the_purple(seed):
    res, frame = last(SC.scenario_desinteres(seed=seed), p=P10)
    sig = res.entry
    assert sig is not None and sig.setup == S.DESINTERES and sig.side == S.SHORT and sig.variant == "morada"
    a = frame.a
    assert a["close"][-1] < a["ema_fast"][-1] and a["close"][-1] < a["ema_mid"][-1]
    assert a["high"][-1] >= a["ema_fast"][-1] * (1 - P10.touch_tol)            # la tocó por debajo
    assert sig.target is not None and sig.target < sig.entry_ref
    assert (sig.entry_ref - sig.target) >= P10.min_target_r * (sig.stop_loss - sig.entry_ref) - 1e-9
    assert any("Zona de salida" in r for r in sig.reasons)


@pytest.mark.parametrize("seed", range(5))
def test_interes_buy_when_price_holds_the_purple(seed):
    res, frame = last(SC.scenario_interes(seed=seed), p=P10)
    sig = res.entry
    assert sig is not None and sig.setup == S.INTERES and sig.side == S.LONG
    assert sig.target is not None and sig.target > sig.entry_ref
    assert (sig.target - sig.entry_ref) >= P10.min_target_r * (sig.entry_ref - sig.stop_loss) - 1e-9


def test_no_trade_without_a_zone_far_enough():
    res, _ = last(SC.scenario_desinteres(seed=0), p=replace(P10, min_target_r=50.0))
    assert res.entry is None and any("sin zona" in d.reason for d in res.discarded)


def test_no_trade_when_emas_are_tangled():
    res, _ = last(SC.scenario_interes(seed=0), p=replace(P10, ema_min_spread=0.5))
    assert res.entry is None


def test_zone_plan_closes_everything_at_the_target():
    from risk_manager import RiskParams, plan_position, replan_from_fill
    rp = RiskParams()
    plan, why = plan_position("X", S.SHORT, 100.0, 103.0, 10_000.0, rp, max_leverage=20, target=92.0)
    assert plan is not None, why
    assert plan.tp1_price == 92.0 and plan.tp1_qty == plan.contracts and plan.tp2_qty == 0 and plan.be_trigger == 0
    again = replan_from_fill(plan, 99.8, plan.contracts, rp)
    assert again.tp1_price == 92.0 and again.tp1_qty == plan.contracts
    assert plan_position("X", S.SHORT, 100.0, 103.0, 10_000.0, rp, target=101.0)[0] is None   # ya pasó la zona


def test_no_look_ahead_with_zones():
    found = 0
    for seed in range(3):
        df = random_walk(700, seed=seed + 10, drift=0.0008 if seed % 2 else -0.0008, vol=0.008, freq="15min")
        history = S.scan_history(S.build_frame(df, P10), symbol="X", analyzer=AN, keep_discarded=True)
        for i in range(len(df) - 200, len(df)):
            live, _ = S.evaluate_last_closed(df.iloc[: i + 1], symbol="X", p=P10, analyzer=AN, log_rejection=False)
            back = history.get(i, S.BarResult())
            assert (live.entry is None) == (back.entry is None), i
            if live.entry is not None:
                found += 1
                assert live.entry.target == pytest.approx(back.entry.target)
    assert found > 0
