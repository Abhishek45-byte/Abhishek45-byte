"""Pricing and risk rules. The AI never decides these - this code does."""
import datetime as dt
import math
import os
from dataclasses import dataclass
from pathlib import Path
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


HOLIDAYS_FILE = Path(os.getenv("HOLIDAYS_FILE", Path(__file__).with_name("holidays.txt")))


def load_holidays(path: Path = HOLIDAYS_FILE) -> dict[dt.date, str]:
    """Read 'YYYY-MM-DD  name' lines. Blank lines and # comments are ignored."""
    holidays = {}
    if not path.exists():
        return holidays
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        day, _, name = line.partition(" ")
        try:
            holidays[dt.date.fromisoformat(day)] = name.strip() or "Exchange holiday"
        except ValueError:
            raise ValueError(f"{path.name} line {n}: bad date {day!r}, expected YYYY-MM-DD") from None
    return holidays


HOLIDAYS = load_holidays()


def holiday_name(day: dt.date, holidays: dict | None = None) -> str | None:
    return (HOLIDAYS if holidays is None else holidays).get(day)


def holidays_known(year: int, holidays: dict | None = None) -> bool:
    """False if holidays.txt has no dates for this year (the list probably needs updating)."""
    return any(d.year == year for d in (HOLIDAYS if holidays is None else holidays))


def market_open(now: dt.datetime | None = None, holidays: dict | None = None) -> bool:
    """Mon-Fri 09:15-15:30 IST, excluding the exchange holidays in holidays.txt."""
    now = now.astimezone(IST) if now else dt.datetime.now(IST)
    t = now.time()
    if now.weekday() >= 5 or holiday_name(now.date(), holidays):
        return False
    return dt.time(9, 15) <= t <= dt.time(15, 30)


def market_warnings(now: dt.datetime | None = None, holidays: dict | None = None) -> list[str]:
    """Non-blocking warnings to show the user, e.g. a missing holiday list for this year."""
    now = now.astimezone(IST) if now else dt.datetime.now(IST)
    if not holidays_known(now.year, holidays):
        return [f"No exchange holidays listed for {now.year} in {HOLIDAYS_FILE.name}: holidays are NOT being "
                "checked. Add this year's NSE holiday list."]
    return []


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
        now_ist = now.astimezone(IST) if now else dt.datetime.now(IST)
        name = holiday_name(now_ist.date())
        if name:
            problems.append(f"Market is closed today for an exchange holiday ({name}).")
        else:
            problems.append("Market is closed (09:15-15:30 IST, Mon-Fri).")
    return problems
