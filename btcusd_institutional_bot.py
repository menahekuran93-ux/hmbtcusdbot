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
BINANCE_WS = f"wss://stream.binance.us:9443/stream?streams={SYMBOL}@depth@100ms/{SYMBOL}@aggTrade"

# Timing & Logic
HEARTBEAT_INTERVAL = 10800 
MIN_WALL_SIZE = 20.0  

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("institutional_bot")

@dataclass
class BotState:
    orderbook: dict = field(default_factory=lambda: {"bids": {}, "asks": {}})
    wall_tracker: dict = field(default_factory=dict) 
    last_price: float = 0.0
    last_heartbeat_time: float = time.time()
    last_update_id: int = 0  # To track seen messages

state = BotState()

# ==============================
# 2. THE "LISTENER" (New Feature)
# ==============================
async def listen_for_commands(bot_instance):
    """Checks Telegram for /status commands."""
    while True:
        try:
            updates = await bot_instance.get_updates(offset=state.last_update_id + 1, timeout=10)
            for u in updates:
                state.last_update_id = u.update_id
                if u.message and str(u.message.chat_id) == CHAT_ID:
                    text = u.message.text
                    
                    if text == "/status":
                        # Find the biggest wall currently on the books
                        all_walls = {**state.orderbook["bids"], **state.orderbook["asks"]}
                        big_wall = max(all_walls.values()) if all_walls else 0
                        
                        resp = (
                            f"📊 **Current Status**\n"
                            f"BTC Price: ${state.last_price:,.2f}\n"
                            f"Largest Wall: {big_wall:.2f} BTC\n"
                            f"System: Active ✅"
                        )
                        await bot_instance.send_message(chat_id=CHAT_ID, text=resp, parse_mode="Markdown")
        except Exception as e:
            log.error(f"Listener Error: {e}")
        await asyncio.sleep(2)

# ==============================
# 3. ANALYSIS & STREAMING
# ==============================
def update_orderbook(data):
    for side_code, side_key in [("b", "bids"), ("a", "asks")]:
        for update in data.get(side_code, []):
            p, s = float(update[0]), float(update[1])
            if s == 0: state.orderbook[side_key].pop(p, None)
            else: state.orderbook[side_key][p] = s

async def stream_data(bot_instance):
    async with websockets.connect(BINANCE_WS) as ws:
        log.info("✅ Connected to Binance.US")
        async for message in ws:
            data = json.loads(message)
            payload = data.get("data", {})
            stream_name = data.get("stream", "")

            if "depth" in stream_name:
                update_orderbook(payload)
            elif "aggTrade" in stream_name:
                state.last_price = float(payload["p"])
                
                # Check 3-hour heartbeat
                now = time.time()
                if now - state.last_heartbeat_time > HEARTBEAT_INTERVAL:
                    await bot_instance.send_message(
                        chat_id=CHAT_ID, 
                        text=f"🕒 **3-Hour Update**\nPrice: ${state.last_price:,.2f}",
                        disable_notification=True
                    )
                    state.last_heartbeat_time = now

async def main():
    async with Bot(token=TELEGRAM_TOKEN) as bot_instance:
        log.info("🤖 Bot Ready and Listening.")
        # Run the Data Stream and the Message Listener at the same time!
        await asyncio.gather(
            stream_data(bot_instance),
            listen_for_commands(bot_instance)
        )

if __name__ == "__main__":
    asyncio.run(main())
