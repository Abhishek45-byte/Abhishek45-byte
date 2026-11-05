# AI Trading Chatbot (India)

Chat in plain English: "What's Reliance at?", "Buy 10 TCS intraday", "Show my P&L", "Close my Infosys position".

## Setup
```
pip install -r requirements.txt
cp .env.example .env      # add your ANTHROPIC_API_KEY
python bot.py
```
Starts in **paper mode** (fake Rs 1,00,000). Without Kite keys, paper prices come from Yahoo (~15 min delayed, cash stocks only).

## Going live (Zerodha)
1. Get a Kite Connect app at developers.kite.trade (paid API).
2. Each morning, log in and put the access token in `.env` as `KITE_ACCESS_TOKEN`.
3. Set `TRADING_MODE=live`.

## Safety built in
- Every order shows a ticket and needs you to type `yes`. The AI cannot bypass this.
- No market orders: LIMIT at LTP ± buffer (1% stocks/futures, 5% options), rounded away from the market on the 0.05 tick.
- Blocks orders over `MAX_ORDER_VALUE` / `MAX_QUANTITY`, after `DAILY_LOSS_LIMIT` is hit, and outside 09:15–15:30 IST (exchange holidays not checked).
- Exits are never trapped: an order that only reduces or closes an open position skips the size and daily-loss limits. An order that would flip you from long to short (or back) counts as a new trade.
- Everything is logged to `trades.log`.

AI suggestions are not financial advice. Test in paper mode for a few weeks before going live, and start small.
