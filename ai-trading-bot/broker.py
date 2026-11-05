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
    """Simulated account. Uses Kite prices if keys exist, otherwise Yahoo (delayed).

    Behaves like a cash account with Zerodha's product rules:
    - BUYs need enough free cash (cash minus what open BUY orders have reserved).
    - CNC (delivery) can't go short: you can only sell CNC shares you hold.
    - CNC is for NSE/BSE only, NRML for NFO/BFO only; MIS works everywhere.
    - A LIMIT that doesn't cross stays OPEN and fills later, once the price reaches it.
    Not modelled: margin/leverage for MIS and F&O shorts, auto square-off, charges.
    """
    name = "PAPER (fake money)"

    def __init__(self, cash=float(os.getenv("PAPER_CASH", 100000)), price_source=None):
        self.cash = cash
        self.prices = price_source or self._default_prices()
        self.pos = {}       # "EXCHANGE:SYMBOL:PRODUCT" -> {"qty", "avg"}
        self.realized = 0.0
        self._orders = []
        self._ids = itertools.count(1)

    @staticmethod
    def _default_prices():
        if os.getenv("KITE_API_KEY") and os.getenv("KITE_ACCESS_TOKEN"):
            return KiteBroker()
        return YahooPrices()

    @staticmethod
    def _key(exchange, symbol, product):
        return f"{exchange}:{symbol}:{product}"

    def _position_key(self, exchange, symbol, product):
        """Delivery shares are one holding whether bought on NSE or BSE, so a CNC trade on either
        exchange books against the existing CNC row for that stock."""
        if product == "CNC" and exchange in ("NSE", "BSE"):
            for ex in ("NSE", "BSE"):
                k = self._key(ex, symbol, "CNC")
                if self.pos.get(k, {"qty": 0})["qty"] != 0:
                    return k
        return self._key(exchange, symbol, product)

    def ltp(self, exchange, symbol):
        return self.prices.ltp(exchange, symbol)

    # ----- bookkeeping -----
    def _open_orders(self):
        return [o for o in self._orders if o["status"] == "OPEN"]

    def _reserved_cash(self):
        return sum(o["price"] * o["quantity"] for o in self._open_orders() if o["transaction_type"] == "BUY")

    def free_cash(self):
        return self.cash - self._reserved_cash()

    def _sellable_cnc(self, exchange, symbol):
        """CNC shares held (on NSE or BSE) minus those already tied up in open CNC SELL orders."""
        held = sum(max(0, self.pos.get(self._key(ex, symbol, "CNC"), {"qty": 0})["qty"]) for ex in ("NSE", "BSE"))
        pending = sum(o["quantity"] for o in self._open_orders()
                      if o["product"] == "CNC" and o["transaction_type"] == "SELL" and o["tradingsymbol"] == symbol)
        return held - pending

    def precheck(self, exchange, symbol, side, quantity, price, product):
        """Reasons the paper account would reject this order. Empty list = OK."""
        exchange, symbol, side = exchange.upper(), symbol.upper(), side.upper()
        problems = []
        if product == "CNC" and exchange not in ("NSE", "BSE"):
            problems.append("CNC (delivery) is for NSE/BSE stocks. Use NRML or MIS for F&O.")
        if product == "NRML" and exchange not in ("NFO", "BFO"):
            problems.append("NRML is for F&O. Use CNC (delivery) or MIS (intraday) for stocks.")
        if side == "BUY":
            cost = price * quantity
            if cost > self.free_cash():
                problems.append(f"Not enough paper cash: need Rs {cost:,.0f} at the limit price, "
                                f"have Rs {self.free_cash():,.0f} free.")
        elif product == "CNC":
            sellable = self._sellable_cnc(exchange, symbol)
            if quantity > sellable:
                problems.append(f"Can't sell {quantity} {symbol} for delivery: you hold {sellable} sellable "
                                "CNC shares. Delivery can't go short; use MIS to short intraday.")
        return problems

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

    def _try_fill(self, order, ltp):
        """Fill an OPEN order if the price has reached its limit. Fills at the LTP, never worse than the limit."""
        side, limit = order["transaction_type"], order["price"]
        if not (ltp <= limit if side == "BUY" else ltp >= limit):
            return
        self._fill(self._position_key(order["exchange"], order["tradingsymbol"], order["product"]),
                   side, order["quantity"], ltp)
        order.update(status="COMPLETE", filled_quantity=order["quantity"], average_price=ltp)

    def refresh(self):
        """Check every OPEN order against the latest price and fill the ones that are now marketable."""
        for o in self._open_orders():
            try:
                self._try_fill(o, self.ltp(o["exchange"], o["tradingsymbol"]))
            except PriceError:
                pass

    # ----- broker interface -----
    def place_limit(self, exchange, symbol, side, quantity, price, product):
        exchange, symbol, side = exchange.upper(), symbol.upper(), side.upper()
        problems = self.precheck(exchange, symbol, side, quantity, price, product)
        if problems:
            raise ValueError("Paper order rejected: " + " ".join(problems))
        ltp = self.ltp(exchange, symbol)
        oid = f"PAPER-{next(self._ids)}"
        order = {"order_id": oid, "exchange": exchange, "tradingsymbol": symbol, "transaction_type": side,
                 "quantity": quantity, "price": price, "product": product, "status": "OPEN",
                 "filled_quantity": 0, "average_price": 0}
        self._orders.append(order)
        self._try_fill(order, ltp)  # marketable limits fill now; others rest until the price gets there
        return oid

    def cancel(self, order_id):
        self.refresh()  # don't cancel something that has already filled
        for o in self._orders:
            if o["order_id"] == order_id and o["status"] == "OPEN":
                o["status"] = "CANCELLED"
                return order_id
        raise ValueError(f"No open order {order_id}.")

    def orders(self):
        self.refresh()
        return list(self._orders)

    def positions(self):
        self.refresh()
        out = []
        for key, p in self.pos.items():
            if p["qty"] == 0:
                continue
            ex, sym, product = key.split(":", 2)
            try:
                last = self.ltp(ex, sym)
            except PriceError:
                last = p["avg"]
            out.append({"exchange": ex, "tradingsymbol": sym, "quantity": p["qty"], "average_price": round(p["avg"], 2),
                        "last_price": last, "pnl": round((last - p["avg"]) * p["qty"], 2), "product": product})
        return out

    def holdings(self):
        return []  # paper CNC shares stay in positions

    def day_pnl(self):
        return self.realized + sum(p["pnl"] for p in self.positions())

    def funds(self):
        self.refresh()
        return {"available_cash": round(self.free_cash(), 2), "reserved_for_open_buys": round(self._reserved_cash(), 2),
                "realized_pnl": round(self.realized, 2)}


def make_broker():
    if os.getenv("TRADING_MODE", "paper").lower() == "live":
        return KiteBroker()
    return PaperBroker()
