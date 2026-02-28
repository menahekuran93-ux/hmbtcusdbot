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
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# --- 1. CONFIGURATION (US-FIXED) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8664798073:AAESFLVg-b2eLYWOQ0xQ6pVdfz-RvJV54J8")
CHAT_ID = os.getenv("CHAT_ID", "6389282895")
SYMBOL = "btcusdt"
TF = "15m"
# Using .us to bypass the 451 block in California/Virginia
BINANCE_WS_BASE = "wss://stream.binance.us:9443/stream?streams="
BINANCE_REST = "https://api.binance.us/api/v3"

# Institutional Params
RR_RATIO = 4
OB_WALL_MIN_BTC = 50.0
ICEBERG_REFRESH_RATE = 0.85
CVD_DIVERGENCE_BARS = 5
ALERT_COOLDOWN_SEC = 300

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

@dataclass
class Candle:
    open: float = 0.0; high: float = 0.0; low: float = 0.0; close: float = 0.0
    volume: float = 0.0; buy_vol: float = 0.0; sell_vol: float = 0.0; closed: bool = False

@dataclass
class BotState:
    candles: deque = field(default_factory=lambda: deque(maxlen=200))
    current_candle: Optional[Candle] = None
    orderbook: dict = field(default_factory=lambda: {"bids": {}, "asks": {}})
    ob_prev_snapshot: dict = field(default_factory=dict)
    ob_snapshot_time: float = 0.0
    cvd: float = 0.0
    cvd_history: deque = field(default_factory=lambda: deque(maxlen=100))
    price_history: deque = field(default_factory=lambda: deque(maxlen=100))
    last_alert: dict = field(default_factory=dict)
    poc: float = 0.0
    atr: float = 0.0

state = BotState()

# --- 2. INSTITUTIONAL LOGIC ---

def rr_block(entry: float, direction: str) -> str:
    # Simplified RR for the alert block
    risk = entry * 0.01 
    reward = risk * RR_RATIO
    sl = entry - risk if direction == "long" else entry + risk
    tp = entry + reward if direction == "long" else entry - reward
    return (f"\n--- {direction.upper()} SETUP (1:{RR_RATIO} RR) ---\n"
            f"Entry: ${entry:,.0f}\nSL: ${sl:,.0f}\nTP: ${tp:,.0f}")

async def send_alert(alert_type: str, message: str, context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    if now - state.last_alert.get(alert_type, 0) < ALERT_COOLDOWN_SEC: return
    state.last_alert[alert_type] = now
    await context.bot.send_message(chat_id=CHAT_ID, text=message)

def analyse_orderbook(context):
    price = state.current_candle.close if state.current_candle else 0
    if not price: return
    
    # Big Wall Detection
    for side, sign in [("bids", "long"), ("asks", "short")]:
        walls = [v for p, v in state.orderbook[side].items() if v >= OB_WALL_MIN_BTC]
        if walls:
            msg = f"🏛️ INSTITUTIONAL {side.upper()} WALL\nPrice: ${price:,.2f}\n" + rr_block(price, sign)
            asyncio.create_task(send_alert(f"{side}_wall", msg, context))

def process_kline(data: dict, context):
    k = data["k"]
    c = Candle(open=float(k["o"]), high=float(k["h"]), low=float(k["l"]), 
               close=float(k["c"]), volume=float(k["v"]), buy_vol=float(k["V"]), closed=k["x"])
    c.sell_vol = c.volume - c.buy_vol
    state.current_candle = c
    state.cvd += (c.buy_vol - c.sell_vol)
    
    if c.closed:
        state.cvd_history.append(state.cvd)
        state.price_history.append(c.close)
        # Check Divergence
        if len(state.cvd_history) >= CVD_DIVERGENCE_BARS:
            cvd_diff = state.cvd_history[-1] - state.cvd_history[0]
            price_diff = state.price_history[-1] - state.price_history[0]
            if price_diff < 0 and cvd_diff > 5000:
                msg = "⚠️ BULLISH CVD ABSORPTION\nPrice down, but Banks are buying." + rr_block(c.close, "long")
                asyncio.create_task(send_alert("CVD_DIV", msg, context))

# --- 3. CORE ENGINE ---

async def run_streams(app):
    url = f"{BINANCE_WS_BASE}{SYMBOL}@kline_{TF}/{SYMBOL}@depth@100ms"
    while True:
        try:
            async with websockets.connect(url) as ws:
                log.info("✅ Institutional Stream Connected")
                async for raw in ws:
                    msg = json.loads(raw)
                    data, stream = msg.get("data", {}), msg.get("stream", "")
                    if "kline" in stream:
                        process_kline(data, app)
                    elif "depth" in stream:
                        for b in data.get("b", []): state.orderbook["bids"][float(b[0])] = float(b[1])
                        for a in data.get("a", []): state.orderbook["asks"][float(a[0])] = float(a[1])
                        analyse_orderbook(app)
        except Exception as e:
            log.error(f"Stream Error: {e}")
            await asyncio.sleep(5)

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Commands
    app.add_handler(CommandHandler("status", lambda u, c: u.message.reply_text(f"Price: ${state.current_candle.close:,.2f}")))

    await app.initialize()
    await app.start()
    
    log.info("🤖 Institutional Bot Ready.")
    
    await asyncio.gather(
        app.updater.start_polling(drop_pending_updates=True),
        run_streams(app)
    )

if __name__ == "__main__":
    asyncio.run(main())
