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
TELEGRAM_TOKEN = "8664798073:AAESFLVg-b2eLYWOQ0xQ6pVdfz-RvJV54J8"
CHAT_ID = "6389282895"

SYMBOL = "btcusdt"
# Using Binance.US to stay connected on Railway
BINANCE_WS = f"wss://stream.binance.us:9443/stream?streams={SYMBOL}@depth@100ms/{SYMBOL}@aggTrade"

# Timing Settings
RR_RATIO = 4
ALERT_COOLDOWN = 300 
HEARTBEAT_INTERVAL = 10800  # 3 Hours

# Institutional Logic
MIN_WALL_SIZE = 20.0  
WALL_PERSIST_SECONDS = 12
CONFLUENCE_THRESHOLD = 2 

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("institutional_bot")

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
    last_heartbeat_time: float = time.time()

state = BotState()

# ==============================
# 2. ANALYSIS LOGIC
# ==============================

def update_orderbook(data):
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
    
    for p in list(state.wall_tracker.keys()):
        side = state.wall_tracker[p]["side"]
        if state.orderbook[side].get(p, 0) < MIN_WALL_SIZE:
            del state.wall_tracker[p]

def get_confluence():
    now = time.time()
    score = 0
    direction, wall_price = None, None
    for p, info in state.wall_tracker.items():
        if now - info["start"] >= WALL_PERSIST_SECONDS:
            score += 1
            direction = "long" if info["side"] == "bids" else "short"
            wall_price = p
            break 
    if len(state.cvd_history) > 30:
        arr = np.array(state.cvd_history)
        z = (arr[-1] - arr.mean()) / (arr.std() + 1e-9)
        if (z > 2.0 and direction == "long") or (z < -2.0 and direction == "short"):
            score += 1
    return score, direction, wall_price

# ==============================
# 3. STREAMING & NOTIFICATIONS
# ==============================

async def stream(bot_instance):
    async with websockets.connect(BINANCE_WS) as ws:
        log.info("✅ Connected to Binance.US")
        async for message in ws:
            data = json.loads(message)
            payload = data.get("data", {})
            stream_name = data.get("stream", "")

            if "depth" in stream_name:
                update_orderbook(payload)
            elif "aggTrade" in stream_name:
                p, q = float(payload["p"]), float(payload["q"])
                delta = -q if payload["m"] else q
                state.last_price = p
                state.cvd += delta
                state.cvd_history.append(state.cvd)
                state.price_history.append(p)

                now = time.time()

                # --- 🕒 SILENT HEARTBEAT (3 Hours) ---
                if now - state.last_heartbeat_time > HEARTBEAT_INTERVAL:
                    try:
                        heartbeat_msg = (
                            f"🕒 **3-Hour Status Update**\n"
                            f"Current Price: ${p:,.2f}\n"
                            f"Status: Monitoring for 20 BTC Walls..."
                        )
                        await bot_instance.send_message(
                            chat_id=CHAT_ID, 
                            text=heartbeat_msg,
                            disable_notification=True, # NO SOUND
                            parse_mode="Markdown"
                        )
                        state.last_heartbeat_time = now
                        log.info("Heartbeat sent.")
                    except: pass

                # --- 🏛 LOUD TRADE SIGNAL ---
                if now - state.last_alert_time > ALERT_COOLDOWN:
                    score, direction, wall_p = get_confluence()
                    if score >= CONFLUENCE_THRESHOLD and direction:
                        msg = f"🏛 **INSTITUTIONAL SIGNAL**\nDir: {direction.upper()} at ${p:,.2f}"
                        try:
                            await bot_instance.send_message(
                                chat_id=CHAT_ID, 
                                text=msg, 
                                disable_notification=False # LOUD SOUND
                            )
                            state.last_alert_time = now
                            log.info(f"Signal alert sent: {direction}")
                        except: pass

async def main():
    async with Bot(token=TELEGRAM_TOKEN) as bot_instance:
        log.info("🤖 Bot Ready.")
        # Startup message (Silent)
        try:
            await bot_instance.send_message(
                chat_id=CHAT_ID, 
                text="🚀 **Bot Online!**\nNow monitoring every 3 hours silently.",
                disable_notification=True
            )
        except: pass

        while True:
            try:
                await stream(bot_instance)
            except Exception as e:
                log.error(f"Reconnecting: {e}")
                await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
