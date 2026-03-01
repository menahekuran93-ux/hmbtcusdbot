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
TIMEFRAME = "15m"

# Binance.US endpoint (essential for Railway/US server bypass)
BINANCE_WS = (
    f"wss://stream.binance.us:9443/stream?"
    f"streams={SYMBOL}@depth@100ms/"
    f"{SYMBOL}@aggTrade/"
    f"{SYMBOL}@kline_{TIMEFRAME}"
)

# Logic Constants
RR_RATIO = 4
ALERT_COOLDOWN = 300
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

state = BotState()

# ... (Analysis logic remains same as previous version) ...

def build_alert_text(entry, direction, wall_price, score):
    buffer = entry * 0.0005 
    if direction == "long":
        sl = (wall_price - buffer) if wall_price else (entry * 0.995)
        tp = entry + ((entry - sl) * RR_RATIO)
    else:
        sl = (wall_price + buffer) if wall_price else (entry * 1.005)
        tp = entry - ((sl - entry) * RR_RATIO)

    return (
        "🏛 **INSTITUTIONAL SIGNAL**\n"
        f"Score: {score}/3\n\n"
        f"🎯 **Dir:** {direction.upper()}\n"
        f"💵 **Entry:** ${entry:,.2f}\n"
        f"🛡️ **SL:** ${sl:,.2f}\n"
        f"💰 **TP:** ${tp:,.2f}"
    )

async def stream(bot_instance):
    async with websockets.connect(BINANCE_WS) as ws:
        log.info("✅ Connected to Binance.US")
        async for message in ws:
            data = json.loads(message)
            payload = data.get("data", {})
            
            # Simple logic to trigger based on walls and CVD
            # (Detailed wall/CVD logic from previous blocks goes here)
            
            # Placeholder check for alerts
            now = time.time()
            if now - state.last_alert_time > ALERT_COOLDOWN:
                # Mockup of confluence check
                if True: # Replace with actual score check
                    try:
                        await bot_instance.send_message(chat_id=CHAT_ID, text="🔍 Scanning for Institutional Walls...")
                        state.last_alert_time = now
                    except:
                        pass

async def main():
    async with Bot(token=TELEGRAM_TOKEN) as bot_instance:
        log.info("🤖 Bot Ready.")
        try:
            await bot_instance.send_message(chat_id=CHAT_ID, text="🚀 **Bot Online!**\nWaiting for 20 BTC walls...")
        except Exception as e:
            log.warning("Could not send startup message. Did you click 'START' in the bot chat?")

        while True:
            try:
                await stream(bot_instance)
            except Exception as e:
                log.error(f"Reconnecting: {e}")
                await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
