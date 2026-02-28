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

# --- CONFIGURATION ---
# It is better to set these in Railway "Variables" tab, 
# but I am leaving your defaults here so it works immediately.
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

# --- LOGIC FUNCTIONS ---

def calculate_atr() -> float:
    candles = list(state.candles)
    if len(candles) < ATR_PERIOD + 1:
        return state.current_candle.close * 0.005 if state.current_candle else 500.0
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
    return (
        f"\n----------------------\n"
        f"{arr} Setup 1:{RR_RATIO} RR\n"
        f"Entry : ${r['entry']:,.0f}\n"
        f"SL    : ${r['sl']:,.0f} (-{r['risk_pct']:.2f}%)\n"
        f"TP    : ${r['tp']:,.0f} (+{r['reward_pct']:.2f}%)\n"
        f"Risk  : ${r['sl_dist']:,.0f} | Reward: ${r['tp_dist']:,.0f}"
    )

async def send_alert(alert_type: str, message: str):
    now = time.time()
    last = state.last_alert.get(alert_type, 0)
    if now - last < ALERT_COOLDOWN_SEC:
        return
    state.last_alert[alert_type] = now
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message)
        log.info(f"Alert sent: {alert_type}")
    except Exception as e:
        log.error(f"Telegram error: {e}")

def analyse_orderbook():
    bids, asks = state.orderbook["bids"], state.orderbook["asks"]
    price = state.current_candle.close if state.current_candle else 0
    if not bids or not asks or price == 0: return
    
    near_bids = {p: v for p, v in bids.items() if price * 0.995 <= p <= price}
    near_asks = {p: v for p, v in asks.items() if price <= p <= price * 1.005}
    bid_vol, ask_vol = sum(near_bids.values()), sum(near_asks.values())

    if ask_vol > 0 and bid_vol / (ask_vol + 1e-9) >= OB_IMBALANCE_RATIO:
        big = [(p, v) for p, v in near_bids.items() if v >= OB_WALL_MIN_BTC]
        if big:
            biggest = max(big, key=lambda x: x[1])
            msg = (f"BID WALL DETECTED\nPrice: ${price:,.0f}\nWall: ${biggest[0]:,.0f} ({biggest[1]:.1f} BTC)" + rr_block(price, "long"))
            asyncio.create_task(send_alert("BID_WALL", msg))

    if bid_vol > 0 and ask_vol / (bid_vol + 1e-9) >= OB_IMBALANCE_RATIO:
        big = [(p, v) for p, v in near_asks.items() if v >= OB_WALL_MIN_BTC]
        if big:
            biggest = max(big, key=lambda x: x[1])
            msg = (f"ASK WALL DETECTED\nPrice: ${price:,.0f}\nWall: ${biggest[0]:,.0f} ({biggest[1]:.1f} BTC)" + rr_block(price, "short"))
            asyncio.create_task(send_alert("ASK_WALL", msg))

# --- COMMAND HANDLERS ---

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = state.current_candle
    price = c.close if c else 0
    await update.message.reply_text(f"BTC/USD Live\nPrice: ${price:,.0f}\nPOC: ${state.poc:,.0f}\nCVD: {state.cvd:,.0f} BTC")

async def main():
    # Load snapshot first
    url = f"{BINANCE_REST}/depth?symbol={SYMBOL.upper()}&limit=500"
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            data = await r.json()
            state.orderbook["bids"] = {float(p): float(q) for p, q in data["bids"]}
            state.orderbook["asks"] = {float(p): float(q) for p, q in data["asks"]}

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("status", cmd_status))
    
    # Start Telegram
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # Start Binance Streams
    streams = f"{SYMBOL}@kline_{TF}/{SYMBOL}@depth@100ms"
    ws_url = BINANCE_WS_BASE + streams
    
    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                log.info("Connected to Binance")
                async for raw in ws:
                    msg = json.loads(raw)
                    stream, data = msg.get("stream", ""), msg.get("data", {})
                    if "kline" in stream:
                        k = data["k"]
                        c = Candle(float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"]), float(k["V"]), closed=k["x"])
                        state.current_candle = c
                        if c.closed: state.candles.append(c)
                    elif "depth" in stream:
                        for b in data.get("b", []): 
                            p, q = float(b[0]), float(b[1])
                            if q == 0: state.orderbook["bids"].pop(p, None)
                            else: state.orderbook["bids"][p] = q
                        analyse_orderbook()
        except Exception as e:
            log.error(f"Error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
