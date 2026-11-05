# AI Trading Chatbot (India)

Chat in plain English: "What's Reliance at?", "Buy 10 TCS intraday", "Show my P&L", "Close my Infosys position".

## Setup
```
pip install -r requirements.txt
cp .env.example .env      # add your ANTHROPIC_API_KEY
python bot.py
```
Starts in **paper mode** (fake Rs 1,00,000). Without Kite keys, paper prices come from Yahoo (~15 min delayed, cash stocks only).

Paper mode acts like a cash account: buys need free cash (open buy orders reserve theirs), CNC can only sell shares you hold, CNC is for NSE/BSE and NRML for F&O, and a limit that doesn't cross rests as OPEN and fills when the price reaches it (checked whenever you look at orders, positions or funds). It does not model MIS/F&O margin, auto square-off or charges.

## Going live (Zerodha)
1. Get a Kite Connect app at developers.kite.trade (paid API).
2. Each morning, log in and put the access token in `.env` as `KITE_ACCESS_TOKEN`.
3. Set `TRADING_MODE=live`.

## Safety built in
- Every order shows a ticket and needs you to type `yes`. The AI cannot bypass this.
- No market orders: LIMIT at LTP ± buffer (1% stocks/futures, 5% options), rounded away from the market on the 0.05 tick.
- Blocks orders over `MAX_ORDER_VALUE` / `MAX_QUANTITY`, after `DAILY_LOSS_LIMIT` is hit, and outside 09:15–15:30 IST or on an exchange holiday.
- Exits are never trapped: an order that only reduces or closes an open position skips the size and daily-loss limits. For CNC sells of NSE/BSE stock this counts your demat holdings too. An order that would flip you from long to short (or back) counts as a new trade.
- Exchange holidays live in `holidays.txt` (2026 included). NSE publishes each new year's list in December: add it there. If the current year has no dates, the bot warns at startup and on every ticket that holidays aren't being checked. Special sessions such as Sunday Muhurat trading are treated as closed.
- Everything is logged to `trades.log`.

AI suggestions are not financial advice. Test in paper mode for a few weeks before going live, and start small.
