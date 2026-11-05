"""Brokers: PaperBroker (default, fake money) and KiteBroker (Zerodha, real money)."""
import itertools
import os


class PriceError(Exception):
    pass


# ---------- price sources ----------
class YahooPrices:
    """Free, ~15 min delayed. NSE/BSE cash stocks only (no F&O). Paper mode only."""

    def ltp(self, exchange, symbol):
        import yfinance as yf
        suffix = {"NSE": ".NS", "BSE": ".BO"}.get(exchange.upper())
        if not suffix:
            raise PriceError("Yahoo has no F&O prices. Add Kite keys for NFO/BFO.")
        hist = yf.Ticker(symbol.upper() + suffix).history(period="1d", interval="1m")
        if hist.empty:
            raise PriceError(f"No price found for {exchange}:{symbol}.")
        return float(hist["Close"].iloc[-1])


class KiteBroker:
    """Zerodha Kite Connect. Needs KITE_API_KEY and a fresh KITE_ACCESS_TOKEN (daily login)."""
    name = "LIVE (Zerodha)"

    def __init__(self):
        from kiteconnect import KiteConnect
        self.kite = KiteConnect(api_key=os.environ["KITE_API_KEY"])
        self.kite.set_access_token(os.environ["KITE_ACCESS_TOKEN"])

    def ltp(self, exchange, symbol):
        key = f"{exchange.upper()}:{symbol.upper()}"
        data = self.kite.ltp([key]).get(key)
        if not data or not data.get("last_price"):
            raise PriceError(f"No live price for {key}.")
        return float(data["last_price"])

    def positions(self):
        return [
            {k: p[k] for k in ("exchange", "tradingsymbol", "quantity", "average_price", "last_price", "pnl", "product")}
            for p in self.kite.positions()["net"]
        ]

    def holdings(self):
        """Demat holdings (delivery stock from earlier days). t1_quantity = bought, not yet settled."""
        keys = ("exchange", "tradingsymbol", "quantity", "t1_quantity", "average_price", "last_price", "pnl")
        return [{k: h.get(k) for k in keys} for h in self.kite.holdings()]

    def day_pnl(self):
        return sum(p["pnl"] for p in self.kite.positions()["net"])

    def funds(self):
        eq = self.kite.margins()["equity"]
        return {"available_cash": eq["available"]["live_balance"], "used_margin": eq["utilised"]["debits"]}

    def orders(self):
        keys = ("order_id", "status", "exchange", "tradingsymbol", "transaction_type", "quantity",
                "price", "filled_quantity", "average_price", "status_message")
        return [{k: o.get(k) for k in keys} for o in self.kite.orders()]

    def place_limit(self, exchange, symbol, side, quantity, price, product):
        k = self.kite
        return k.place_order(
            variety=k.VARIETY_REGULAR, exchange=exchange.upper(), tradingsymbol=symbol.upper(),
            transaction_type=side, quantity=int(quantity), product=product,
            order_type=k.ORDER_TYPE_LIMIT, price=price, validity=k.VALIDITY_DAY,
        )

    def cancel(self, order_id):
        return self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=order_id)


class PaperBroker:
    """Simulated account. Uses Kite prices if keys exist, otherwise Yahoo (delayed)."""
    name = "PAPER (fake money)"

    def __init__(self, cash=float(os.getenv("PAPER_CASH", 100000)), price_source=None):
        self.cash = cash
        self.prices = price_source or self._default_prices()
        self.pos = {}       # key -> {"qty", "avg"}
        self.realized = 0.0
        self._orders = []
        self._ids = itertools.count(1)

    @staticmethod
    def _default_prices():
        if os.getenv("KITE_API_KEY") and os.getenv("KITE_ACCESS_TOKEN"):
            return KiteBroker()
        return YahooPrices()

    def ltp(self, exchange, symbol):
        return self.prices.ltp(exchange, symbol)

    def _fill(self, key, side, qty, price):
        p = self.pos.setdefault(key, {"qty": 0, "avg": 0.0})
        signed = qty if side == "BUY" else -qty
        if p["qty"] == 0 or (p["qty"] > 0) == (signed > 0):  # open / add
            total = p["qty"] + signed
            p["avg"] = (p["avg"] * abs(p["qty"]) + price * qty) / abs(total)
            p["qty"] = total
        else:  # reduce / close / flip
            closing = min(qty, abs(p["qty"]))
            direction = 1 if p["qty"] > 0 else -1
            self.realized += (price - p["avg"]) * closing * direction
            p["qty"] += signed
            if p["qty"] == 0:
                p["avg"] = 0.0
            elif (p["qty"] > 0) != (direction > 0):
                p["avg"] = price
        self.cash -= signed * price

    def place_limit(self, exchange, symbol, side, quantity, price, product):
        ltp = self.ltp(exchange, symbol)
        oid = f"PAPER-{next(self._ids)}"
        crosses = price >= ltp if side == "BUY" else price <= ltp
        order = {"order_id": oid, "exchange": exchange, "tradingsymbol": symbol, "transaction_type": side,
                 "quantity": quantity, "price": price, "status": "OPEN", "filled_quantity": 0, "average_price": 0}
        if crosses:  # fills at the market price, like a real marketable limit
            self._fill(f"{exchange}:{symbol}", side, quantity, ltp)
            order.update(status="COMPLETE", filled_quantity=quantity, average_price=ltp)
        self._orders.append(order)
        return oid

    def cancel(self, order_id):
        for o in self._orders:
            if o["order_id"] == order_id and o["status"] == "OPEN":
                o["status"] = "CANCELLED"
                return order_id
        raise ValueError(f"No open order {order_id}.")

    def orders(self):
        return list(self._orders)

    def positions(self):
        out = []
        for key, p in self.pos.items():
            if p["qty"] == 0:
                continue
            ex, sym = key.split(":", 1)
            try:
                last = self.ltp(ex, sym)
            except PriceError:
                last = p["avg"]
            out.append({"exchange": ex, "tradingsymbol": sym, "quantity": p["qty"], "average_price": round(p["avg"], 2),
                        "last_price": last, "pnl": round((last - p["avg"]) * p["qty"], 2), "product": "PAPER"})
        return out

    def holdings(self):
        return []  # paper trades all live in positions

    def day_pnl(self):
        return self.realized + sum(p["pnl"] for p in self.positions())

    def funds(self):
        return {"available_cash": round(self.cash, 2), "realized_pnl": round(self.realized, 2)}


def make_broker():
    if os.getenv("TRADING_MODE", "paper").lower() == "live":
        return KiteBroker()
    return PaperBroker()
