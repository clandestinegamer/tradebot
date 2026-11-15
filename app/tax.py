"""Tax export: one row per closed trade with cost basis and gain in both KRW and USD.

USD figures use the ECB USD/KRW reference rate recorded on the day of each buy and sell
(the IRS asks for the exchange rate on the date of each transaction). This is a record-keeping aid,
not tax advice — have a tax professional review it, especially for Korean filing rules.
"""
from __future__ import annotations

import csv
import io

COLUMNS = ["symbol", "market", "quantity", "date_acquired", "date_sold", "currency",
           "cost_basis_native", "proceeds_native", "fees_krw",
           "usd_krw_on_buy", "usd_krw_on_sell",
           "cost_basis_krw", "proceeds_krw", "gain_krw",
           "cost_basis_usd", "proceeds_usd", "gain_usd", "holding_days", "mode"]


def closed_trades(trades: list[dict], year: int | None = None) -> list[dict]:
    rows = []
    last_buy: dict[str, dict] = {}
    for t in sorted(trades, key=lambda x: x["id"]):
        if t["side"] == "BUY":
            last_buy[t["key"]] = t
            continue
        if year and not str(t["ts"]).startswith(str(year)):
            continue
        b = last_buy.get(t["key"], {})
        usd_native = t["market"] == "kis_us"
        fx_buy = b.get("fx_usdkrw") or t.get("fx_usdkrw") or 0
        fx_sell = t.get("fx_usdkrw") or fx_buy
        qty = t["qty"]
        buy_px = b.get("price") or 0
        buy_fee = (b.get("fee_krw") or 0) * (min(1.0, qty / b["qty"]) if b.get("qty") else 1.0)
        cost_native = qty * buy_px
        proceeds_native = qty * t["price"]
        if usd_native:
            cost_krw, proceeds_krw = cost_native * fx_buy, proceeds_native * fx_sell
            cost_usd, proceeds_usd = cost_native, proceeds_native
        else:
            cost_krw, proceeds_krw = cost_native, proceeds_native
            cost_usd = cost_native / fx_buy if fx_buy else 0
            proceeds_usd = proceeds_native / fx_sell if fx_sell else 0
        fees_krw = buy_fee + (t.get("fee_krw") or 0)
        fees_usd_buy = buy_fee / fx_buy if fx_buy else 0
        fees_usd_sell = (t.get("fee_krw") or 0) / fx_sell if fx_sell else 0
        acquired = (t.get("opened_at") or b.get("ts") or "")[:10]
        sold = str(t["ts"])[:10]
        try:
            from datetime import date
            days = (date.fromisoformat(sold) - date.fromisoformat(acquired)).days
        except ValueError:
            days = ""
        rows.append({
            "symbol": t["symbol"], "market": t["market"], "quantity": qty, "date_acquired": acquired,
            "date_sold": sold, "currency": "USD" if usd_native else "KRW",
            "cost_basis_native": round(cost_native, 4), "proceeds_native": round(proceeds_native, 4),
            "fees_krw": round(fees_krw), "usd_krw_on_buy": round(fx_buy, 2), "usd_krw_on_sell": round(fx_sell, 2),
            "cost_basis_krw": round(cost_krw + buy_fee), "proceeds_krw": round(proceeds_krw - (t.get("fee_krw") or 0)),
            "gain_krw": round(proceeds_krw - cost_krw - fees_krw),
            "cost_basis_usd": round(cost_usd + fees_usd_buy, 2),
            "proceeds_usd": round(proceeds_usd - fees_usd_sell, 2),
            "gain_usd": round(proceeds_usd - cost_usd - fees_usd_buy - fees_usd_sell, 2),
            "holding_days": days, "mode": t.get("mode", ""),
        })
    return rows


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def harvest_review(engine) -> dict:
    """Year-to-date realized gains and open positions currently at a loss (tax-loss harvesting candidates)."""
    from datetime import datetime, timezone
    year = str(datetime.now(timezone.utc).year)
    rows = closed_trades(engine.store.all_trades(), int(year))
    fx_now = engine._usdkrw()
    cands = []
    for p in engine.store.positions():
        px = engine.last_prices.get(p.key, p.avg_price)
        fx = engine._fx_for(p.market)
        un_krw = (px - p.avg_price) * p.qty * fx
        if un_krw >= 0:
            continue
        un_usd = (px - p.avg_price) * p.qty if p.market == "kis_us" else un_krw / fx_now
        try:
            held = (datetime.now(timezone.utc) - datetime.fromisoformat(p.opened_at)).days
        except ValueError:
            held = None
        cands.append({"key": p.key, "symbol": p.symbol, "kind": p.key.split(":")[0] if ":" in p.key and p.key.split(":")[0] in ("core", "dca", "grid", "park") else "swing",
                      "unrealized_krw": round(un_krw), "unrealized_usd": round(un_usd, 2), "held_days": held})
    cands.sort(key=lambda c: c["unrealized_krw"])
    return {"year": int(year), "realized_ytd_krw": sum(r["gain_krw"] for r in rows),
            "realized_ytd_usd": round(sum(r["gain_usd"] for r in rows), 2), "closed_trades": len(rows),
            "candidates": cands,
            "note": "Not tax advice. Selling a loser before Dec 31 can offset gains; US wash-sale rules may "
                    "disallow the loss if you buy the same security back within 30 days. Korea and the US tax "
                    "these differently — check with a tax professional who handles both."}
