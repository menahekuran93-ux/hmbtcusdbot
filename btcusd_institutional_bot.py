import os
import asyncio
import json
import time
import logging
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import websockets
from telegram import Bot

# ==============================
# 1. CONFIGURATION
# ==============================
# Using the credentials you provided
TELEGRAM_TOKEN = "8664798073:AAESFLVg-b2eLYWOQ0xQ6pVdfz-RvJV54J8"
CHAT_ID = "6389282895"

SYMBOL = "btcusdt"
TIMEFRAME = "15m"

# Binance.US endpoint to bypass Railway's regional IP blocks (Error 451)
BINANCE_WS = (
    f"wss://stream.binance.us:9443/stream?"
    f"streams={SYMBOL}@depth@100ms/"
    f"{SYMBOL}@aggTrade/"
    f"{SYMBOL}@kline_{TIMEFRAME}"
)

# Institutional Logic Parameters
RR_RATIO = 4
ALERT_COOLDOWN = 300
MIN_WALL_SIZE = 20.0  
WALL_PERSIST_SECONDS = 12
CVD_Z_THRESHOLD = 2.0
ABSORPTION_DELTA_THRESHOLD = 150
CONFLUENCE_THRESHOLD = 2 

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("institutional_bot")

# ==============================
# 2. STATE MANAGEMENT
# ==============================
@dataclass
class BotState:
    orderbook: dict = field(default_factory=lambda: {"bids": {}, "asks": {}})
    wall_tracker: dict = field(default_factory=dict) 
    cvd: float = 0.0
    cvd_history: deque = field(default_factory=lambda: deque(maxlen=200))
    price_history: deque = field(default_factory=lambda: deque(maxlen=200))
    delta_history: deque = field(default_factory=lambda: deque(maxlen=200))
    last_price: float = 0.0
    last_alert_time: float = 0.0

state = BotState()

# ==============================
# 3. ANALYSIS ENGINES
# ==============================

def update_orderbook(data):
    """Tracks limit orders and persistent institutional walls."""
    now = time.time()
    for side_code, side_key in [("b", "bids"), ("a", "asks")]:
        for update in data.get(side_code, []):
            price, size = float(update[0]), float(update[1])
            if size == 0:
                state.orderbook[side_key].pop(price, None)
                state.wall_tracker.pop(price, None)
            else:
                state.orderbook[side_key][price] = size
                if size >= MIN_WALL_SIZE:
                    if price not in state.wall_tracker:
                        state.wall_tracker[price] = {"start": now, "side": side_key}

    # Clean up walls that dropped below size
    for p in list(state.wall_tracker.keys()):
        side = state.wall_tracker[p]["side"]
        if state.orderbook[side].get(p, 0) < MIN_WALL_SIZE:
            del state.wall_tracker[p]

def get_confluence():
    """Calculates if multiple institutional signals align."""
    now = time.time()
    score = 0
    direction = None
    wall_price = None

    # Signal 1: Persistent Wall
    for p, info in state.wall_tracker.items():
        if now - info["start"] >= WALL_PERSIST_SECONDS:
            score += 1
            direction = "long" if info["side"] == "bids" else "short"
            wall_price = p
            break 

    # Signal 2: CVD Z-Score (Institutional Momentum)
    if len(state.cvd_history) > 30:
        arr = np.array(state.cvd_history)
        z = (arr[-1] - arr.mean()) / (arr.std() + 1e-9)
        if (z > CVD_Z_THRESHOLD and direction == "long") or \
           (z < -CVD_Z_THRESHOLD and direction == "short"):
            score += 1

    # Signal 3: Price Absorption
    if len(state.delta_history) > 20:
        recent_delta = sum(list(state.delta_history)[-20:])
        recent_prices = list(state.price_history)[-20:]
        price_range = max(recent_prices) - min(recent_prices)
        if abs(recent_delta) > ABSORPTION_DELTA_THRESHOLD and price_range < 20:
            score += 1

    return score, direction, wall_price

def build_alert_text(entry, direction, wall_price, score):
    """Formats the Telegram message with Dynamic SL/TP."""
    buffer = entry * 0.0005 
    if direction == "long":
        sl = (wall_price - buffer) if wall_price else (entry * 0.995)
        tp = entry + ((entry - sl) * RR_RATIO)
    else:
        sl = (wall_price + buffer) if wall_price else (entry * 1.005)
        tp = entry - ((sl - entry) * RR_RATIO)

    return (
        "🏛 **INSTITUTIONAL CONFLUENCE DETECTED**\n"
        f"Confidence Score: {score}/3\n\n"
        f"🎯 **Direction:** {direction.upper()}\n"
        f"💵 **Entry:** ${entry:,.2f}\n"
        f"🛡️ **SL (Wall Shield):** ${sl:,.2f}\n"
        f"💰 **TP (Target):** ${tp:,.2f}\n"
        f"⚖️ **RR Ratio:** 1:{RR_RATIO}"
    )

# ==============================
# 4. EXECUTION & STREAMING
# ==============================

async def stream(bot_instance):
    """Main WebSocket loop for data ingestion."""
    async with websockets.connect(BINANCE_WS) as ws:
        log.info("✅ Connected to Binance.US Institutional Feed")
        async for message in ws:
            data = json.loads(message)
            stream_name = data.get("stream", "")
            payload = data.get("data", {})

            if "depth" in stream_name:
                update_orderbook(payload)
            elif "aggTrade" in stream_name:
                p, q = float(payload["p"]), float(payload["q"])
                delta = -q if payload["m"] else q
                
                state.last_price = p
                state.cvd += delta
                state.delta_history.append(delta)
                state.price_history.append(p)
                state.cvd_history.append(state.cvd)
                
                # Check for alerts
                now = time.time()
                if now - state.last_alert_time > ALERT_COOLDOWN:
                    score, direction, wall_p = get_confluence()
                    if score >= CONFLUENCE_THRESHOLD and direction:
                        msg = build_alert_text(p, direction, wall_p, score)
                        try:
                            await bot_instance.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
                            state.last_alert_time = now
                            log.info(f"🚀 Alert Sent: {direction.upper()} at {p}")
                        except Exception as e:
                            log.error(f"Telegram Send Error: {e}")

async def main():
    """Initialize bot session and maintain connection."""
    async with Bot(token=TELEGRAM_TOKEN) as bot_instance:
        log.info("🤖 Bot Session Initialized.")
        while True:
            try:
                await stream(bot_instance)
            except Exception as e:
                log.error(f"❌ Connection Error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
