from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal

Action = Literal["BUY", "SELL", "HOLD"]


@dataclass
class Signal:
    action: Action
    reason: str
    price: float = 0.0
    atr: float = 0.0
    snapshot: dict = field(default_factory=dict)  # indicator values, for logging / AI review
    filtered: bool = False  # True = a setup appeared but a filter blocked it


@dataclass
class Position:
    key: str               # "market:symbol"
    symbol: str
    market: str
    qty: float
    avg_price: float       # native currency (KRW, or USD for kis_us)
    stop: float
    target: float
    opened_at: str
    entry_bar: str = ""
    entry_reason: str = ""
    initial_stop: float = 0.0   # stop at entry — defines 1R of risk
    highest: float = 0.0        # highest price seen since entry (for the trailing stop)
    entry_atr: float = 0.0      # ATR at entry (trailing distance is measured in these)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Fill:
    ok: bool
    qty: float = 0.0
    price: float = 0.0     # native currency
    fee_krw: float = 0.0
    order_id: str = ""
    message: str = ""
