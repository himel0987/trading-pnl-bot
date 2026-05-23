import os
import asyncio
import logging
from datetime import datetime
from telegram import Bot
from binance.client import Client as BinanceClient
from pybit.unified_trading import HTTP as BybitClient

# ── Logging Setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ── Environment Variables ──────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
BINANCE_API_KEY  = os.environ["BINANCE_API_KEY"]
BINANCE_SECRET   = os.environ["BINANCE_SECRET"]
BYBIT_API_KEY    = os.environ["BYBIT_API_KEY"]
BYBIT_SECRET     = os.environ["BYBIT_SECRET"]

# ── Clients ────────────────────────────────────────────────────────────────────
binance_client = BinanceClient(BINANCE_API_KEY, BINANCE_SECRET)
bybit_client   = BybitClient(api_key=BYBIT_API_KEY, api_secret=BYBIT_SECRET)
bot            = Bot(token=TELEGRAM_TOKEN)

# ── State Tracking ─────────────────────────────────────────────────────────────
prev_binance_spot_trades    = {}
prev_binance_futures_trades = {}
prev_bybit_spot_trades      = {}
prev_bybit_futures_trades   = {}

# ── SKIP LIST: coins to ignore in spot scanning ────────────────────────────────
SKIP_ASSETS = {"USDT", "BUSD", "USDC", "TUSD", "BNB", "DAI", "FDUSD"}


# ══════════════════════════════════════════════════════════════════════════════
# DATA FETCHERS
# ══════════════════════════════════════════════════════════════════════════════

async def get_binance_spot_trades():
    """
    FIX: Removed broken get_my_trades(symbol="") call.
    Now reads account balances and checks only coins you actually hold.
    Also removed the 300s sleep-inside-function bug.
    """
    trades = {}
    try:
        account_info = binance_client.get_account()
        balances = [
            b for b in account_info["balances"]
            if float(b["free"]) > 0 or float(b["locked"]) > 0
        ]
        logger.info(f"Binance Spot: scanning {len(balances)} balance(s)")

        for b in balances:
            asset = b["asset"]
            if asset in SKIP_ASSETS:
                continue
            symbol = f"{asset}USDT"
            try:
                orders = binance_client.get_all_orders(symbol=symbol, limit=5)
                for o in orders:
                    if o["status"] == "FILLED":
                        trades[o["orderId"]] = o
                await asyncio.sleep(0.2)   # gentle rate-limit delay
            except Exception as e:
                # Symbol may not exist as USDT pair — skip silently
                logger.debug(f"Binance Spot skip {symbol}: {e}")

    except Exception as e:
        logger.error(f"Binance Spot account fetch error: {e}")
        # FIX: No more 300s sleep here — let the main loop handle retries
    return trades


def get_binance_futures_trades():
    trades = {}
    try:
        positions = binance_client.futures_position_information()
        for p in positions:
            if float(p.get("positionAmt", 0)) != 0:
                trades[p["symbol"]] = p
        logger.info(f"Binance Futures: {len(trades)} active position(s)")
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
        logger.info(f"Bybit Spot: {len(trades)} filled order(s)")
    except Exception as e:
        logger.error(f"Bybit Spot error: {e}")
    return trades


def get_bybit_futures_trades():
    trades = {}
    try:
        result = bybit_client.get_positions(category="linear", settleCoin="USDT")
        for p in result["result"]["list"]:
            trades[p["symbol"]] = p
        logger.info(f"Bybit Futures: {len(trades)} position(s) fetched")
    except Exception as e:
        logger.error(f"Bybit Futures error: {e}")
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM SENDER
# ══════════════════════════════════════════════════════════════════════════════

async def send_pnl(exchange, market, symbol, pnl, reason="TP/SL"):
    emoji = "🟢" if pnl >= 0 else "🔴"
    sign  = "+" if pnl >= 0 else ""
    now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    msg = (
        f"{emoji} *{exchange} {market}*\n"
        f"📌 Pair   : `{symbol}`\n"
        f"📊 PNL    : `{sign}{pnl:.2f}%`\n"
        f"🔔 Reason : {reason}\n"
        f"🕐 Time   : {now}"
    )
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")
        logger.info(f"Sent PNL alert: {exchange} {market} {symbol} {sign}{pnl:.2f}%")
    except Exception as e:
        logger.error(f"Telegram send error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# CHECKERS
# ══════════════════════════════════════════════════════════════════════════════

async def check_binance_spot():
    global prev_binance_spot_trades
    current = await get_binance_spot_trades()
    for oid, order in current.items():
        if oid not in prev_binance_spot_trades and order.get("side") == "SELL":
            try:
                price = float(order["price"]) if float(order.get("price", 0)) > 0 else float(order.get("cummulativeQuoteQty", 0)) / float(order.get("executedQty", 1))
                fills = binance_client.get_my_trades(symbol=order["symbol"], limit=10)
                buy_fills = [f for f in fills if f["isBuyer"]]   # FIX: was checking wrong field
                if buy_fills and price > 0:
                    avg_buy = sum(float(f["price"]) for f in buy_fills) / len(buy_fills)
                    pnl = ((price - avg_buy) / avg_buy) * 100
                    await send_pnl("Binance", "SPOT", order["symbol"], pnl, "Closed")
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.debug(f"Binance spot PNL calc error: {e}")
    prev_binance_spot_trades = current


async def check_binance_futures():
    global prev_binance_futures_trades
    current = get_binance_futures_trades()
    for sym, pos in current.items():
        prev = prev_binance_futures_trades.get(sym)
        if prev:
            prev_amt = float(prev.get("positionAmt", 0))
            curr_amt = float(pos.get("positionAmt", 0))
            if prev_amt != 0 and curr_amt == 0:
                entry = float(prev.get("entryPrice", 0))
                mark  = float(prev.get("markPrice", 0))
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
                qty       = float(order.get("cumExecQty", 0))
                avg_price = float(order.get("avgPrice", 0))
                last_price = float(order.get("price", 0))
                if qty > 0 and avg_price > 0 and last_price > 0:
                    pnl = ((last_price - avg_price) / avg_price) * 100
                    await send_pnl("Bybit", "SPOT", order["symbol"], pnl, "Closed")
            except Exception as e:
                logger.debug(f"Bybit spot PNL calc error: {e}")
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
                entry = float(prev.get("avgPrice", 0))
                mark  = float(prev.get("markPrice", 0))
                side  = prev.get("side", "Buy")
                if entry > 0:
                    pnl = ((mark - entry) / entry * 100) if side == "Buy" else ((entry - mark) / entry * 100)
                    await send_pnl("Bybit", "FUTURES", sym, pnl, "TP/SL Hit")
    prev_bybit_futures_trades = current


# ══════════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    logger.info("Trading PNL Bot started!")
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text="🚀 *Trading PNL Bot চালু হয়েছে!*\nঅ্যাকাউন্ট বেইজড মনিটরিং শুরু...",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Startup Telegram message failed: {e}")

    cycle = 0
    while True:
        cycle += 1
        logger.info(f"── Cycle #{cycle} started ──")
        try:
            await check_binance_spot()
            await asyncio.sleep(2)

            await check_binance_futures()
            await asyncio.sleep(2)

            await check_bybit_spot()
            await asyncio.sleep(2)

            await check_bybit_futures()

        except Exception as e:
            logger.error(f"Cycle #{cycle} error: {e}")

        logger.info(f"── Cycle #{cycle} done — sleeping 30s ──")
        await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
