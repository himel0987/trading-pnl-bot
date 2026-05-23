import os
import asyncio
import logging
from datetime import datetime
from telegram import Bot
from binance.client import Client as BinanceClient
from pybit.unified_trading import HTTP as BybitClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
BINANCE_API_KEY  = os.environ["BINANCE_API_KEY"]
BINANCE_SECRET   = os.environ["BINANCE_SECRET"]
BYBIT_API_KEY    = os.environ["BYBIT_API_KEY"]
BYBIT_SECRET     = os.environ["BYBIT_SECRET"]

bot            = Bot(token=TELEGRAM_TOKEN)
binance_client = BinanceClient(BINANCE_API_KEY, BINANCE_SECRET)
bybit_client   = BybitClient(api_key=BYBIT_API_KEY, api_secret=BYBIT_SECRET)

prev_binance_spot_trades    = {}
prev_binance_futures_trades = {}
prev_bybit_spot_trades      = {}
prev_bybit_futures_trades   = {}

def get_binance_spot_trades():
    trades = {}
    try:
        tickers = binance_client.get_all_tickers()
        usdt_pairs = [t["symbol"] for t in tickers if t["symbol"].endswith("USDT")]
        for symbol in usdt_pairs:
            try:
                orders = binance_client.get_all_orders(symbol=symbol, limit=5)
                for o in orders:
                    if o["status"] == "FILLED":
                        trades[o["orderId"]] = o
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Binance Spot error: {e}")
    return trades

def get_binance_futures_trades():
    trades = {}
    try:
        positions = binance_client.futures_position_information()
        for p in positions:
            trades[p["symbol"]] = p
    except Exception as e:
        logger.error(f"Binance Futures error: {e}")
    return trades

def get_bybit_spot_trades():
    trades = {}
    try:
        result = bybit_client.get_order_history(category="spot", limit=50)
        for o in result["result"]["list"]:
            if o["orderStatus"] == "Filled":
                trades[o["orderId"]] = o
    except Exception as e:
        logger.error(f"Bybit Spot error: {e}")
    return trades

def get_bybit_futures_trades():
    trades = {}
    try:
        result = bybit_client.get_positions(category="linear", settleCoin="USDT")
        for p in result["result"]["list"]:
            trades[p["symbol"]] = p
    except Exception as e:
        logger.error(f"Bybit Futures error: {e}")
    return trades

EMOJI = {"profit": "🟢", "loss": "🔴"}

async def send_pnl(exchange, market, symbol, pnl, reason="TP/SL"):
    emoji = EMOJI["profit"] if pnl >= 0 else EMOJI["loss"]
    sign  = "+" if pnl >= 0 else ""
    now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    msg = (
        f"{emoji} *{exchange} {market}*\n"
        f"📌 Pair   : `{symbol}`\n"
        f"📊 PNL    : `{sign}{pnl:.2f}%`\n"
        f"🔔 Reason : {reason}\n"
        f"🕐 Time   : {now}"
    )
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")

async def check_binance_spot():
    global prev_binance_spot_trades
    current = get_binance_spot_trades()
    for oid, order in current.items():
        if oid not in prev_binance_spot_trades and order["side"] == "SELL":
            try:
                price = float(order["price"])
                fills = binance_client.get_my_trades(symbol=order["symbol"], limit=10)
                buy_trades = [f for f in fills if not f["isBuyer"]]
                if buy_trades and price > 0:
                    avg_buy = sum(float(f["price"]) for f in buy_trades) / len(buy_trades)
                    pnl = ((price - avg_buy) / avg_buy) * 100
                    await send_pnl("Binance", "SPOT", order["symbol"], pnl, "Closed")
            except Exception:
                pass
    prev_binance_spot_trades = current

async def check_binance_futures():
    global prev_binance_futures_trades
    current = get_binance_futures_trades()
    for sym, pos in current.items():
        prev = prev_binance_futures_trades.get(sym)
        if prev:
            prev_amt = float(prev["positionAmt"])
            curr_amt = float(pos["positionAmt"])
            if prev_amt != 0 and curr_amt == 0:
                entry = float(prev["entryPrice"])
                mark  = float(prev["markPrice"])
                if entry > 0:
                    pnl = ((mark - entry) / entry * 100) if prev_amt > 0 else ((entry - mark) / entry * 100)
                    await send_pnl("Binance", "FUTURES", sym, pnl, "TP/SL Hit")
    prev_binance_futures_trades = current

async def check_bybit_spot():
    global prev_bybit_spot_trades
    current = get_bybit_spot_trades()
    for oid, order in current.items():
        if oid not in prev_bybit_spot_trades:
            try:
                qty   = float(order.get("cumExecQty", 0))
                value = float(order.get("cumExecValue", 0))
                if qty > 0 and value > 0:
                    avg_price = value / qty
                    pnl = ((avg_price - float(order["avgPrice"])) / float(order["avgPrice"])) * 100
                    await send_pnl("Bybit", "SPOT", order["symbol"], pnl, "Closed")
            except Exception:
                pass
    prev_bybit_spot_trades = current

async def check_bybit_futures():
    global prev_bybit_futures_trades
    current = get_bybit_futures_trades()
    for sym, pos in current.items():
        prev = prev_bybit_futures_trades.get(sym)
        if prev:
            prev_size = float(prev.get("size", 0))
            curr_size = float(pos.get("size", 0))
            if prev_size != 0 and curr_size == 0:
                entry = float(prev["avgPrice"])
                mark  = float(prev["markPrice"])
                side  = prev["side"]
                if entry > 0:
                    pnl = ((mark - entry) / entry * 100) if side == "Buy" else ((entry - mark) / entry * 100)
                    await send_pnl("Bybit", "FUTURES", sym, pnl, "TP/SL Hit")
    prev_bybit_futures_trades = current

async def main():
    logger.info("Trading PNL Bot started!")
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="✅ *Trading PNL Bot চালু হয়েছে!*\nBinance ও Bybit মনিটরিং শুরু...",
        parse_mode="Markdown"
    )
    while True:
        try:
            await check_binance_spot()
            await asyncio.sleep(3)
            await check_binance_futures()
            await asyncio.sleep(3)
            await check_bybit_spot()
            await asyncio.sleep(3)
            await check_bybit_futures()
        except Exception as e:
            logger.error(f"Loop error: {e}")
        await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
