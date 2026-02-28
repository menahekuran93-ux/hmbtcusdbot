with open("clean_bot.py", "w") as f:
    f.write("print('Bot is running')")
import os
import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
import aiohttp
import websockets
import json
import numpy as np
import telegram
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8664798073:AAEwX0DnpjTccGW3IUMWi-hLEMUuclffzl4")
CHAT_ID = os.getenv("CHAT_ID", "6389282895")
SYMBOL = "btcusdt"
TF = "15m"
BINANCE_WS_BASE = "wss://stream.binance.com:9443/stream?streams="
BINANCE_REST = "https://api.binance.com/api/v3"
RR_RATIO = 4
SL_ATR_MULT = 1.0
ATR_PERIOD = 14
OB_IMBALANCE_RATIO = 3.0
OB_WALL_MIN_BTC = 50.0
ICEBERG_REFRESH_RATE = 0.85
HVN_PERCENTILE = 75
LVN_PERCENTILE = 25
POC_SWEEP_TICKS = 3
CVD_DIVERGENCE_BARS = 5
ALERT_COOLDOWN_SEC = 300

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

@dataclass
class Candle:
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    buy_vol: float = 0.0
    sell_vol: float = 0.0
    closed: bool = False

@dataclass
class BotState:
    candles: deque = field(default_factory=lambda: deque(maxlen=200))
    current_candle: Optional[Candle] = None
    orderbook: dict = field(default_factory=lambda: {"bids": {}, "asks": {}})
    ob_snapshot_time: float = 0.0
    ob_prev_snapshot: dict = field(default_factory=dict)
    cvd: float = 0.0
    cvd_history: deque = field(default_factory=lambda: deque(maxlen=100))
    price_history: deque = field(default_factory=lambda: deque(maxlen=100))
    last_alert: dict = field(default_factory=dict)
    volume_profile: dict = field(default_factory=dict)
    poc: float = 0.0
    hvn_levels: list = field(default_factory=list)
    lvn_levels: list = field(default_factory=list)
    tick_size: float = 1.0
    atr: float = 0.0

state = BotState()
bot = telegram.Bot(token=TELEGRAM_TOKEN)

def calculate_atr() -> float:
    candles = list(state.candles)
    if len(candles) < ATR_PERIOD + 1:
        if state.current_candle:
            return state.current_candle.close * 0.005
        return 500.0
    true_ranges = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i-1].close
        true_ranges.append(max(h - l, abs(h - pc), abs(l - pc)))
    return float(np.mean(true_ranges[-ATR_PERIOD:]))

def calc_rr(entry: float, direction: str) -> dict:
    atr = state.atr if state.atr > 0 else calculate_atr()
    sl_distance = atr * SL_ATR_MULT
    tp_distance = sl_distance * RR_RATIO
    if direction == "long":
        sl, tp = entry - sl_distance, entry + tp_distance
    else:
        sl, tp = entry + sl_distance, entry - tp_distance
    return {
        "entry": entry, "sl": sl, "tp": tp,
        "sl_dist": sl_distance, "tp_dist": tp_distance,
        "risk_pct": (sl_distance / entry) * 100,
        "reward_pct": (tp_distance / entry) * 100,
    }

def rr_block(entry: float, direction: str) -> str:
    r = calc_rr(entry, direction)
    arr = "LONG" if direction == "long" else "SHORT"
    emoji = "Green" if direction == "long" else "Red"
    return (
        "\n----------------------\n"
        + arr + " Setup 1:" + str(RR_RATIO) + " RR\n"
        + "Entry : $" + f"{r['entry']:,.0f}" + "\n"
        + "SL    : $" + f"{r['sl']:,.0f}" + " (-" + f"{r['risk_pct']:.2f}" + "%)\n"
        + "TP    : $" + f"{r['tp']:,.0f}" + " (+" + f"{r['reward_pct']:.2f}" + "%)\n"
        + "Risk  : $" + f"{r['sl_dist']:,.0f}" + " | Reward: $" + f"{r['tp_dist']:,.0f}"
    )

async def send_alert(alert_type: str, message: str):
    now = time.time()
    last = state.last_alert.get(alert_type, 0)
    if now - last < ALERT_COOLDOWN_SEC:
        return
    state.last_alert[alert_type] = now
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message)
        log.info("Alert sent: " + alert_type)
    except Exception as e:
        log.error("Telegram error: " + str(e))

def analyse_orderbook():
    bids = state.orderbook["bids"]
    asks = state.orderbook["asks"]
    price = state.current_candle.close if state.current_candle else 0
    if not bids or not asks or price == 0:
        return
    near_bids = {p: v for p, v in bids.items() if price * 0.995 <= p <= price}
    near_asks = {p: v for p, v in asks.items() if price <= p <= price * 1.005}
    bid_vol = sum(near_bids.values())
    ask_vol = sum(near_asks.values())

    if ask_vol > 0 and bid_vol / (ask_vol + 1e-9) >= OB_IMBALANCE_RATIO:
        big = [(p, v) for p, v in near_bids.items() if v >= OB_WALL_MIN_BTC]
        if big:
            biggest = max(big, key=lambda x: x[1])
            msg = ("BID WALL DETECTED - BTC/USD\n"
                   "Price: $" + f"{price:,.0f}" + "\n"
                   "Wall: $" + f"{biggest[0]:,.0f}" + " -> " + f"{biggest[1]:.1f}" + " BTC\n"
                   "OB: " + f"{bid_vol/ask_vol:.1f}" + "x bid-heavy\n"
                   "Institutional support zone"
                   + rr_block(price, "long"))
            asyncio.create_task(send_alert("BID_WALL", msg))

    if bid_vol > 0 and ask_vol / (bid_vol + 1e-9) >= OB_IMBALANCE_RATIO:
        big = [(p, v) for p, v in near_asks.items() if v >= OB_WALL_MIN_BTC]
        if big:
            biggest = max(big, key=lambda x: x[1])
            msg = ("ASK WALL DETECTED - BTC/USD\n"
                   "Price: $" + f"{price:,.0f}" + "\n"
                   "Wall: $" + f"{biggest[0]:,.0f}" + " -> " + f"{biggest[1]:.1f}" + " BTC\n"
                   "OB: " + f"{ask_vol/bid_vol:.1f}" + "x ask-heavy\n"
                   "Institutional resistance zone"
                   + rr_block(price, "short"))
            asyncio.create_task(send_alert("ASK_WALL", msg))

    now = time.time()
    if state.ob_prev_snapshot and (now - state.ob_snapshot_time) < 3:
        for side in ["bids", "asks"]:
            direction = "long" if side == "bids" else "short"
            curr = state.orderbook[side]
            prev = state.ob_prev_snapshot.get(side, {})
            for pl, qty in curr.items():
                prev_qty = prev.get(pl, 0)
                if prev_qty > OB_WALL_MIN_BTC and qty >= prev_qty * ICEBERG_REFRESH_RATE:
                    msg = ("ICEBERG ORDER - BTC/USD\n"
                           "Side: " + ("BID" if side == "bids" else "ASK") + "\n"
                           "Level: $" + f"{pl:,.0f}" + " refreshed " + f"{qty:.1f}" + " BTC\n"
                           "Hidden institutional order detected"
                           + rr_block(price, direction))
                    asyncio.create_task(send_alert("ICEBERG_" + side.upper(), msg))

    state.ob_prev_snapshot = {"bids": dict(bids), "asks": dict(asks)}
    state.ob_snapshot_time = now

def update_volume_profile(c: Candle):
    if not c or c.volume == 0:
        return
    if c.high == c.low:
        pb = round(c.close / 100) * 100
        state.volume_profile[pb] = state.volume_profile.get(pb, 0) + c.volume
        return
    steps = max(1, int((c.high - c.low) / state.tick_size))
    vps = c.volume / steps
    ss = (c.high - c.low) / steps
    for i in range(steps):
        pb = round((c.low + i * ss) / 100) * 100
        state.volume_profile[pb] = state.volume_profile.get(pb, 0) + vps

def compute_poc_hvn_lvn():
    vp = state.volume_profile
    if len(vp) < 5:
        return
    levels = sorted(vp.keys())
    arr = np.array([vp[l] for l in levels])
    state.poc = levels[int(np.argmax(arr))]
    state.hvn_levels = [levels[i] for i, v in enumerate(arr) if v >= np.percentile(arr, HVN_PERCENTILE)]
    state.lvn_levels = [levels[i] for i, v in enumerate(arr) if v <= np.percentile(arr, LVN_PERCENTILE)]
    state.atr = calculate_atr()

def check_poc_sweep(price: float):
    if state.poc == 0:
        return
    dist = abs(price - state.poc)
    if dist <= POC_SWEEP_TICKS * state.tick_size:
        direction = "short" if price >= state.poc else "long"
        msg = ("POC SWEEP - BTC/USD\n"
               "Price: $" + f"{price:,.0f}" + " -> POC @ $" + f"{state.poc:,.0f}" + "\n"
               "High-volume node - expect strong reaction"
               + rr_block(price, direction))
        asyncio.create_task(send_alert("POC_SWEEP", msg))

def check_lvn_entry(price: float):
    for lvn in state.lvn_levels:
        if abs(price - lvn) <= POC_SWEEP_TICKS * 2 * state.tick_size:
            direction = "long" if price > lvn else "short"
            msg = ("LVN SNIPE ZONE - BTC/USD\n"
                   "Price: $" + f"{price:,.0f}" + " entering LVN @ $" + f"{lvn:,.0f}" + "\n"
                   "Low Volume Node - fast travel / snipe entry"
                   + rr_block(price, direction))
            asyncio.create_task(send_alert("LVN_ENTRY", msg))
            break

def check_delta_divergence():
    if len(state.cvd_history) < CVD_DIVERGENCE_BARS + 1 or len(state.price_history) < CVD_DIVERGENCE_BARS + 1:
        return
    prices = list(state.price_history)[-CVD_DIVERGENCE_BARS:]
    cvds = list(state.cvd_history)[-CVD_DIVERGENCE_BARS:]
    pt = prices[-1] - prices[0]
    ct = cvds[-1] - cvds[0]
    entry = prices[-1]
    if pt > 0 and ct < -5000:
        msg = ("BEARISH DELTA DIVERGENCE - BTC/USD\n"
               "Price: +" + f"{pt:,.0f}" + " over " + str(CVD_DIVERGENCE_BARS) + " bars\n"
               "CVD: " + f"{ct:,.0f}" + " BTC (sellers absorbing)\n"
               "Price up but sell delta rising - distribution"
               + rr_block(entry, "short"))
        asyncio.create_task(send_alert("CVD_BEAR_DIV", msg))
    elif pt < 0 and ct > 5000:
        msg = ("BULLISH DELTA DIVERGENCE - BTC/USD\n"
               "Price: " + f"{pt:,.0f}" + " over " + str(CVD_DIVERGENCE_BARS) + " bars\n"
               "CVD: +" + f"{ct:,.0f}" + " BTC (buyers absorbing)\n"
               "Price down but buy delta rising - accumulation"
               + rr_block(entry, "long"))
        asyncio.create_task(send_alert("CVD_BULL_DIV", msg))

def process_kline(data: dict):
    k = data["k"]
    c = Candle(
        open=float(k["o"]), high=float(k["h"]),
        low=float(k["l"]), close=float(k["c"]),
        volume=float(k["v"]), buy_vol=float(k["V"]),
        closed=k["x"]
    )
    c.sell_vol = c.volume - c.buy_vol
    state.current_candle = c
    state.cvd += (c.buy_vol - c.sell_vol)
    if c.closed:
        state.candles.append(c)
        state.cvd_history.append(state.cvd)
        state.price_history.append(c.close)
        update_volume_profile(c)
        compute_poc_hvn_lvn()
        check_delta_divergence()
    check_poc_sweep(c.close)
    check_lvn_entry(c.close)

def process_depth(data: dict):
    for bid in data.get("b", []):
        p, q = float(bid[0]), float(bid[1])
        if q == 0:
            state.orderbook["bids"].pop(p, None)
        else:
            state.orderbook["bids"][p] = q
    for ask in data.get("a", []):
        p, q = float(ask[0]), float(ask[1])
        if q == 0:
            state.orderbook["asks"].pop(p, None)
        else:
            state.orderbook["asks"][p] = q
    analyse_orderbook()

async def fetch_ob_snapshot():
    url = BINANCE_REST + "/depth?symbol=" + SYMBOL.upper() + "&limit=500"
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            data = await r.json()
    state.orderbook["bids"] = {float(p): float(q) for p, q in data["bids"]}
    state.orderbook["asks"] = {float(p): float(q) for p, q in data["asks"]}
    log.info("Order book snapshot loaded.")

async def run_streams():
    streams = SYMBOL + "@kline_" + TF + "/" + SYMBOL + "@depth@100ms"
    url = BINANCE_WS_BASE + streams
    await fetch_ob_snapshot()
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                log.info("WebSocket connected")
                await send_alert("BOT_START",
                    "BTC/USD Institutional Bot LIVE\n"
                    "Exchange: Binance | TF: " + TF + "\n"
                    "RR: 1:" + str(RR_RATIO) + " on every alert\n"
                    "Watching: OB Walls, Iceberg, Volume Profile, CVD Delta"
                )
                async for raw in ws:
                    msg = json.loads(raw)
                    stream = msg.get("stream", "")
                    data = msg.get("data", {})
                    if "kline" in stream:
                        process_kline(data)
                    elif "depth" in stream:
                        process_depth(data)
        except Exception as e:
            log.error("WS error: " + str(e) + " reconnecting in 5s")
            await asyncio.sleep(5)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = state.current_candle
    price = c.close if c else 0
    bv = sum(state.orderbook["bids"].values())
    av = sum(state.orderbook["asks"].values())
    atr = state.atr if state.atr > 0 else calculate_atr()
    await update.message.reply_text(
        "BTC/USD Live Status\n"
        "Price   : $" + f"{price:,.0f}" + "\n"
        "ATR(14) : $" + f"{atr:,.0f}" + "\n"
        "POC     : $" + f"{state.poc:,.0f}" + "\n"
        "CVD     : " + f"{state.cvd:,.0f}" + " BTC\n"
        "OB Ratio: " + f"{bv/(av+1e-9):.2f}" + "x bid/ask"
    )

async def cmd_ob(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bids = sorted(state.orderbook["bids"].items(), reverse=True)[:5]
    asks = sorted(state.orderbook["asks"].items())[:5]
    lines = ["Order Book - BTC/USD\n", "ASKS:"]
    for p, v in reversed(asks):
        lines.append("  $" + f"{p:>10,.0f}" + " -> " + f"{v:.2f}" + " BTC")
    lines.append("---------------------")
    lines.append("BIDS:")
    for p, v in bids:
        lines.append("  $" + f"{p:>10,.0f}" + " -> " + f"{v:.2f}" + " BTC")
    await update.message.reply_text("\n".join(lines))

async def cmd_vp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    hvn = ", ".join("$" + f"{x:,.0f}" for x in state.hvn_levels[-5:])
    lvn = ", ".join("$" + f"{x:,.0f}" for x in state.lvn_levels[:5])
    await update.message.reply_text(
        "Volume Profile - BTC/USD\n"
        "POC: $" + f"{state.poc:,.0f}" + "\n"
        "HVN: " + hvn + "\n"
        "LVN: " + lvn
    )

async def cmd_rr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = state.current_candle
    price = c.close if c else 0
    if price == 0:
        await update.message.reply_text("No price yet. Try again shortly.")
        return
    args = ctx.args
    direction = (args[0].lower() if args else "long")
    if direction not in ("long", "short"):
        direction = "long"
    r = calc_rr(price, direction)
    await update.message.reply_text(
        "Manual RR - 1:" + str(RR_RATIO) + "\n"
        "Direction: " + direction.upper() + "\n"
        "Entry: $" + f"{r['entry']:,.0f}" + "\n"
        "SL   : $" + f"{r['sl']:,.0f}" + " (-" + f"{r['risk_pct']:.2f}" + "%)\n"
        "TP   : $" + f"{r['tp']:,.0f}" + " (+" + f"{r['reward_pct']:.2f}" + "%)\n"
        "ATR  : $" + f"{state.atr:,.0f}"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "BTC/USD Institutional Bot\n\n"
        "/status     - Price, ATR, POC, CVD\n"
        "/ob         - Top 5 bids and asks\n"
        "/vp         - Volume profile\n"
        "/rr long    - RR calc for long\n"
        "/rr short   - RR calc for short\n"
        "/help       - This menu\n\n"
        "All alerts include 1:" + str(RR_RATIO) + " RR levels"
    )

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ob", cmd_ob))
    app.add_handler(CommandHandler("vp", cmd_vp))
    app.add_handler(CommandHandler("rr", cmd_rr))
    app.add_handler(CommandHandler("help", cmd_help))
    async with app:
        await app.start()
        await app.updater.start_polling()
        await run_streams()
        await app.updater.stop()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
ENDOFFILE

import py_compile
try:
    py_compile.compile('clean_bot.py')
    print("SUCCESS")
except:
    print("ERROR")

# Check for any smart quotes
"python3 -c "
content = open('/home/claude/clean_bot.py', 'rb').read()
bad = [b'\xe2\x80\x9c', b'\xe2\x80\x9d', b'\xe2\x80\x98', b'\xe2\x80\x99']
found = any(b in content for b in bad)
print('Smart quotes found:', found)

