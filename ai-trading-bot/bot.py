"""AI trading chatbot for Indian markets (NSE/BSE/NFO/BFO).

The AI can look up prices/positions and PROPOSE orders. Placing an order always
needs you to type 'yes' in the terminal - the AI cannot skip that step.
Run:  python bot.py
"""
import datetime as dt
import json
import os

from dotenv import load_dotenv

load_dotenv()

import anthropic  # noqa: E402

from broker import PriceError, make_broker  # noqa: E402
from risk import IST, RiskLimits, buffer_for, check_order, limit_price, market_warnings, reduces_position  # noqa: E402

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
LOG_FILE = "trades.log"

SYSTEM = """You are a trading assistant for Indian markets (NSE, BSE, NFO, BFO), talking to the account owner.
- Use tools for every price, position and fund figure. Never invent numbers.
- Symbols are exchange trading symbols, e.g. NSE:RELIANCE, NFO:NIFTY25NOV24000CE, NFO:BANKNIFTY25NOVFUT.
- To trade, call propose_order. The program then computes a buffered LIMIT price, runs risk checks and asks
  the user to confirm. Only say an order was placed if the tool result says status PLACED.
- Never promise profits. When giving a view, state the risk and where a stop-loss would go.
- Be brief. Use rupees (Rs)."""

TOOLS = [
    {"name": "get_price", "description": "Last traded price of one instrument.",
     "input_schema": {"type": "object", "properties": {
         "exchange": {"type": "string", "enum": ["NSE", "BSE", "NFO", "BFO"]},
         "symbol": {"type": "string"}}, "required": ["exchange", "symbol"]}},
    {"name": "get_positions", "description": "Open positions with P&L.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_funds", "description": "Available cash / margin.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_orders", "description": "Today's orders and their status.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "propose_order",
     "description": "Propose a BUY or SELL. The program prices it as a buffered LIMIT, risk-checks it and asks the user to confirm.",
     "input_schema": {"type": "object", "properties": {
         "exchange": {"type": "string", "enum": ["NSE", "BSE", "NFO", "BFO"]},
         "symbol": {"type": "string"},
         "side": {"type": "string", "enum": ["BUY", "SELL"]},
         "quantity": {"type": "integer", "description": "Shares, or lots x lot size for F&O."},
         "product": {"type": "string", "enum": ["MIS", "CNC", "NRML"], "description": "MIS intraday, CNC delivery, NRML F&O carry."},
         "reason": {"type": "string", "description": "One line why, shown to the user."}},
         "required": ["exchange", "symbol", "side", "quantity", "product", "reason"]}},
    {"name": "cancel_order", "description": "Cancel an open order (user confirms).",
     "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}},
]


def log(event: dict):
    event["time"] = dt.datetime.now(IST).isoformat(timespec="seconds")
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")


def ask_user(prompt: str) -> bool:
    return input(prompt).strip().lower() == "yes"


class TradingBot:
    def __init__(self, broker=None, client=None, confirm=ask_user, limits=None):
        self.broker = broker or make_broker()
        self.client = client or anthropic.Anthropic()
        self.confirm = confirm
        self.limits = limits or RiskLimits()
        self.history = []

    # ----- tools -----
    def open_quantity(self, exchange, symbol, product):
        """Net quantity you hold in this instrument and product. + long, - short.

        For CNC (delivery) on NSE/BSE this includes demat holdings, and a share counts on either
        exchange: holdings can be sold on NSE or BSE. Today's sells of holdings show up as a negative
        CNC position, so holdings + positions = what is still left to sell.
        """
        cash_delivery = product == "CNC" and exchange in ("NSE", "BSE")

        def same(ex, sym):
            if sym.upper() != symbol:
                return False
            return ex.upper() in ("NSE", "BSE") if cash_delivery else ex.upper() == exchange

        total = 0
        for p in self.broker.positions():
            if same(p["exchange"], p["tradingsymbol"]) and p.get("product") == product:
                total += int(p["quantity"])
        if cash_delivery:
            for h in self.broker.holdings():
                if same(h["exchange"], h["tradingsymbol"]):
                    total += int(h["quantity"]) + int(h.get("t1_quantity") or 0)
        return total

    def propose_order(self, exchange, symbol, side, quantity, product, reason):
        exchange, symbol, side = exchange.upper(), symbol.upper(), side.upper()
        ltp = self.broker.ltp(exchange, symbol)
        price = limit_price(ltp, side, buffer_for(exchange, symbol))
        is_exit = reduces_position(side, quantity, self.open_quantity(exchange, symbol, product))
        problems = check_order(exchange, quantity, price, self.broker.day_pnl(), self.limits, is_exit=is_exit)
        if hasattr(self.broker, "precheck"):  # paper account rules (cash, no CNC shorts), checked before the ticket
            problems += self.broker.precheck(exchange, symbol, side, quantity, price, product)
        if problems:
            log({"event": "blocked", "symbol": symbol, "side": side, "qty": quantity, "why": problems})
            return {"status": "BLOCKED", "reasons": problems}
        print(f"\n  ┌─ ORDER TICKET [{self.broker.name}]")
        print(f"  │ {side} {quantity} x {exchange}:{symbol}  ({product}){'  [EXIT]' if is_exit else ''}")
        print(f"  │ LTP Rs {ltp:,.2f}  ->  LIMIT Rs {price:,.2f}   value ~Rs {price * quantity:,.0f}")
        print(f"  │ Reason: {reason}")
        for w in market_warnings():
            print(f"  │ WARNING: {w}")
        print("  └─")
        if not self.confirm("  Type 'yes' to place, anything else to cancel: "):
            log({"event": "declined", "symbol": symbol, "side": side, "qty": quantity})
            return {"status": "DECLINED_BY_USER"}
        oid = self.broker.place_limit(exchange, symbol, side, quantity, price, product)
        log({"event": "placed", "order_id": oid, "symbol": f"{exchange}:{symbol}", "side": side,
             "qty": quantity, "exit": is_exit, "ltp": ltp, "limit": price, "mode": self.broker.name})
        status = next((o for o in self.broker.orders() if str(o["order_id"]) == str(oid)), {})
        return {"status": "PLACED", "order_id": oid, "ltp": ltp, "limit_price": price,
                "broker_status": status.get("status"),
                "note": "OPEN means the limit is resting and has not filled yet." if status.get("status") == "OPEN" else ""}

    def cancel_order(self, order_id):
        if not self.confirm(f"  Cancel order {order_id}? Type 'yes': "):
            return {"status": "DECLINED_BY_USER"}
        self.broker.cancel(order_id)
        log({"event": "cancelled", "order_id": order_id})
        return {"status": "CANCELLED", "order_id": order_id}

    def run_tool(self, name, args):
        try:
            if name == "get_price":
                return {"ltp": self.broker.ltp(args["exchange"], args["symbol"])}
            if name == "get_positions":
                return self.broker.positions()
            if name == "get_funds":
                return self.broker.funds()
            if name == "get_orders":
                return self.broker.orders()
            if name == "propose_order":
                return self.propose_order(**args)
            if name == "cancel_order":
                return self.cancel_order(**args)
            return {"error": f"unknown tool {name}"}
        except (PriceError, ValueError, KeyError) as e:
            return {"error": str(e)}
        except Exception as e:  # broker/network errors go back to the AI to explain
            return {"error": f"{type(e).__name__}: {e}"}

    # ----- chat loop -----
    def chat(self, user_text: str) -> str:
        self.history.append({"role": "user", "content": user_text})
        while True:
            resp = self.client.messages.create(model=MODEL, max_tokens=1500, system=SYSTEM,
                                               tools=TOOLS, messages=self.history)
            self.history.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                return "".join(b.text for b in resp.content if b.type == "text")
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    out = self.run_tool(block.name, block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps(out, default=str)})
            self.history.append({"role": "user", "content": results})


def main():
    bot = TradingBot()
    print(f"AI Trading Bot - mode: {bot.broker.name}. Type 'quit' to exit.\n")
    if "LIVE" in bot.broker.name:
        print("!! LIVE MODE: confirmed orders use REAL money. !!\n")
    for w in market_warnings():
        print(f"WARNING: {w}\n")
    while True:
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in ("quit", "exit"):
            break
        if text:
            print(f"\nBot: {bot.chat(text)}\n")


if __name__ == "__main__":
    main()
