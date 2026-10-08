# schemas.py
from pydantic import BaseModel, Field
from typing import List, Literal

class ScoutOutput(BaseModel):
    symbol: str
    market_worth_researching: bool
    reason_to_research: str
    confidence: float

class NewsOutput(BaseModel):
    summary: str
    positive_events: List[str]
    negative_events: List[str]
    unverified_claims: List[str]
    sources: List[str]

class SentimentOutput(BaseModel):
    sentiment: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    score: int = Field(ge=0, le=100)
    main_reasons: List[str]
    possible_hype: bool

class ChartsOutput(BaseModel):
    trend: Literal["BULLISH", "BEARISH", "SIDEWAYS"]
    support_levels: List[float]
    resistance_levels: List[float]
    volume_summary: str
    setup_quality: Literal["HIGH", "MEDIUM", "LOW"]

class RiskOutput(BaseModel):
    status: Literal["approve", "reduce", "reject"]
    maximum_simulated_size: float
    risk_flags: List[str]
    reason: str

class DecisionOutput(BaseModel):
    decision: Literal["BUY_SIMULATION_ONLY", "WAIT", "SKIP"]
    reasons: List[str]
    conflicting_signals: List[str]
    human_review_required: bool = True
