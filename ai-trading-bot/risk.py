"""Pricing and risk rules. The AI never decides these - this code does."""
import datetime as dt
import math
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
TICK = 0.05


def buffer_for(exchange: str, symbol: str) -> float:
    """Slippage buffer so a LIMIT order behaves like a capped market order."""
    exchange, symbol = exchange.upper(), symbol.upper()
    if exchange in ("NFO", "BFO"):
        if symbol.endswith(("CE", "PE")):
            return 0.05  # options
        if symbol.endswith("FUT"):
            return 0.01  # futures
    if exchange in ("NSE", "BSE"):
        return 0.01  # cash equity
    raise ValueError(f"No buffer rule for {exchange}:{symbol}. Not supported.")


def limit_price(ltp: float, side: str, buffer: float) -> float:
    """BUY rounds UP, SELL rounds DOWN on a 0.05 tick, so the buffer is never lost."""
    if ltp is None or ltp <= 0:
        raise ValueError("No valid live price - refusing to price the order.")
    if side == "BUY":
        ticks = math.ceil(round(ltp * (1 + buffer) / TICK, 6))
    else:
        ticks = max(1, math.floor(round(ltp * (1 - buffer) / TICK, 6)))
    return round(ticks * TICK, 2)


def market_open(now: dt.datetime | None = None) -> bool:
    """Mon-Fri 09:15-15:30 IST. Exchange holidays are NOT checked."""
    now = now or dt.datetime.now(IST)
    t = now.time()
    return now.weekday() < 5 and dt.time(9, 15) <= t <= dt.time(15, 30)


@dataclass
class RiskLimits:
    max_order_value: float = float(os.getenv("MAX_ORDER_VALUE", 25000))
    max_quantity: int = int(os.getenv("MAX_QUANTITY", 500))
    daily_loss_limit: float = float(os.getenv("DAILY_LOSS_LIMIT", 5000))
    allowed_exchanges: tuple = ("NSE", "BSE", "NFO", "BFO")
    require_market_open: bool = os.getenv("REQUIRE_MARKET_OPEN", "1") == "1"


def reduces_position(side: str, quantity: int, open_qty: int) -> bool:
    """True if the order only shrinks or closes an existing position (never opens or flips one)."""
    if side.upper() == "SELL":
        return 0 < quantity <= open_qty
    return 0 < quantity <= -open_qty


def check_order(exchange, quantity, price, day_pnl, limits: RiskLimits, now=None,
                is_exit: bool = False) -> list[str]:
    """Return a list of reasons to BLOCK. Empty list = OK.

    is_exit=True means the order only reduces an existing position. Exits skip the size and
    daily-loss limits so you can always get out of a trade; the other checks still apply.
    """
    problems = []
    if exchange.upper() not in limits.allowed_exchanges:
        problems.append(f"Exchange {exchange} not allowed.")
    if quantity <= 0:
        problems.append("Quantity must be positive.")
    if not is_exit:
        if quantity > limits.max_quantity:
            problems.append(f"Quantity {quantity} > max {limits.max_quantity}.")
        value = quantity * price
        if value > limits.max_order_value:
            problems.append(f"Order value Rs {value:,.0f} > max Rs {limits.max_order_value:,.0f}.")
        if day_pnl <= -limits.daily_loss_limit:
            problems.append(f"Daily loss limit hit (P&L Rs {day_pnl:,.0f}). No new trades today; exits are still allowed.")
    if limits.require_market_open and not market_open(now):
        problems.append("Market is closed (09:15-15:30 IST, Mon-Fri).")
    return problems
