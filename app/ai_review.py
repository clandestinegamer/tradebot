"""Claude reviews each BUY the rules propose. It can approve or veto — never start a trade itself."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .config import AICfg

SYSTEM = """You are the risk reviewer for an automated trading bot. A rules-based strategy has proposed a BUY.
Your only job is to APPROVE or REJECT that single trade. You cannot change its size or suggest other trades.

Reject when there is a concrete reason the setup is likely to fail or the risk is abnormal, for example:
- earnings, a major economic release, or a token unlock within ~2 days
- fresh serious negative news (fraud, hack, delisting, lawsuit, guidance cut, trading halt)
- the move looks like a one-off spike on news that is already reversing
- exchange-specific problems (deposits/withdrawals suspended, warning designation 투자유의/투자경고)
Approve when the technical setup is reasonable and you find no such red flag. Do not reject merely because
markets are uncertain — that is always true. Be brief and factual; do not invent news.

Answer with ONLY a JSON object:
{"decision": "approve" | "reject", "confidence": 0.0-1.0, "reason": "<one or two sentences>"}"""


@dataclass
class Review:
    decision: str          # approve | reject | skipped | error
    confidence: float
    reason: str

    @property
    def approved(self) -> bool:
        return self.decision in ("approve", "skipped")


def _parse(text: str) -> dict:
    m = re.findall(r"\{[^{}]*\"decision\"[^{}]*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON decision in reply: {text[:200]}")
    return json.loads(m[-1])


class AIReviewer:
    def __init__(self, cfg: AICfg, api_key: str, client=None):
        self.cfg = cfg
        self.client = client
        if client is None and cfg.enabled and api_key:
            import anthropic
            self.client = anthropic.Anthropic(api_key=api_key)

    @property
    def active(self) -> bool:
        return self.cfg.enabled and self.client is not None

    def review(self, *, symbol: str, market: str, price: float, reason: str, snapshot: dict,
               recent_closes: list[float], stop: float, target: float, portfolio: dict) -> Review:
        if not self.cfg.enabled:
            return Review("skipped", 1.0, "AI review disabled in config")
        if self.client is None:
            return self._fail("ANTHROPIC_API_KEY is not set")

        venue = {"upbit": "Upbit (Korean crypto exchange, KRW pairs)", "kis_kr": "KRX (Korean stock)",
                 "kis_us": "US stock exchange"}[market]
        prompt = (
            f"Proposed BUY: {symbol} on {venue}\n"
            f"Price: {price:,.4f} | stop-loss: {stop:,.4f} | take-profit: {target:,.4f}\n"
            f"Rule that fired: {reason}\n"
            f"Indicators: {json.dumps(snapshot)}\n"
            f"Last {len(recent_closes)} closes (oldest→newest): {[round(c, 4) for c in recent_closes]}\n"
            f"Portfolio: {json.dumps(portfolio)}\n"
            + ("Search for recent news about this asset before deciding.\n" if self.cfg.web_search else "")
        )
        kwargs = dict(model=self.cfg.model, max_tokens=1024, system=SYSTEM,
                      messages=[{"role": "user", "content": prompt}])
        try:
            try:
                if self.cfg.web_search:
                    kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
                msg = self.client.messages.create(**kwargs)
            except Exception:
                if "tools" not in kwargs:
                    raise
                kwargs.pop("tools")  # retry without web search if the tool isn't available
                msg = self.client.messages.create(**kwargs)
            text = "".join(getattr(b, "text", "") for b in msg.content)
            d = _parse(text)
            decision = str(d.get("decision", "")).lower()
            conf = float(d.get("confidence", 0))
            why = str(d.get("reason", ""))[:500]
            if decision == "approve" and conf < self.cfg.min_confidence:
                return Review("reject", conf, f"low-confidence approval ({conf:.2f}): {why}")
            if decision not in ("approve", "reject"):
                return self._fail(f"unexpected decision '{decision}'")
            return Review(decision, conf, why)
        except Exception as e:  # network, auth, parse errors
            return self._fail(f"{type(e).__name__}: {e}")

    def _fail(self, why: str) -> Review:
        if self.cfg.fail_open:
            return Review("skipped", 0.0, f"AI unavailable, proceeding (fail_open): {why}")
        return Review("error", 0.0, f"AI unavailable, buy skipped: {why}")
