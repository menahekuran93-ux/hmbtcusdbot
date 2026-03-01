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
# CONFIGURATION
# ==============================
TELEGRAM_TOKEN = "8664798073:AAESFLVg-b2eLYWOQ0xQ6pVdfz-RvJV54J8"
CHAT_ID = "6389282895"

SYMBOL = "btcusdt"
TIMEFRAME = "15m"

BINANCE_WS = (
    f"wss://stream.binance.com:9443/stream?"
    f"streams={SYMBOL}@depth@100ms/"
    f"{SYMBOL}@aggTrade/"
    f"{SYMBOL}@kline_{TIMEFRAME}"
)

RR_RATIO = 4
ALERT_COOLDOWN = 300

# Institutional thresholds
MIN_WALL_SIZE = 20.0  
WALL_PERSIST_SECONDS = 12
CVD_Z_THRESHOLD = 2.0
ABSORPTION_DELTA_THRESHOLD = 150
CONFLUENCE_THRESHOLD = 2 # Adjusted for better strike rate with 20BTC sensitivity

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
log = logging.getLogger("institutional_bot")

# ==============================
# STATE
# ==============================
@dataclass
class BotState:
    orderbook: dict = field(default_factory=lambda: {"bids": {}, "asks": {}})
    wall_tracker: dict = field(default_factory=dict) # Key: price, Value: {start, side, size}
    cvd: float = 0.0
    cvd_history: deque = field(default_factory=lambda: deque(maxlen=200))
    price_history: deque = field(default_factory=lambda: deque(maxlen=200))
    delta_history: deque = field(default_factory=lambda: deque(maxlen=200))
    last_price: float = 0.0
    last_alert_time: float = 0.0

state = BotState()
bot = Bot(token=TELEGRAM_TOKEN)

# ==============================
# ENGINES
# ==============================

def update_orderbook(data):
    now = time.time()
    # Handle Bids and Asks
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
                        state.wall_tracker[price] = {"start": now, "side": side_key, "size": size}
                    else:
                        state.wall_tracker[price]["size"] = size

    # Remove walls that dropped below threshold
    for p in list(state.wall_tracker.keys()):
        side = state.wall_tracker[p]["side"]
        if state.orderbook[side].get(p, 0) < MIN_WALL_SIZE:
            del state.wall_tracker[p]

def get_confluence():
    now = time.time()
    score = 0
    active_side = None
    wall_ref_price = None

    # 1. Wall Persistence & Direction
    for p, info in state.wall_tracker.items():
        if now - info["start"] >= WALL_PERSIST_SECONDS:
            score += 1
            active_side = "long" if info["side"] == "bids" else "short"
            wall_ref_price = p
            break # Take the first significant wall

    # 2. CVD Z-Score
    if len(state.cvd_history) > 30:
        arr = np.array(state.cvd_history)
        z = (arr[-1] - arr.mean()) / (arr.std() + 1e-9)
        if (z > CVD_Z_THRESHOLD and active_side == "long") or (z < -CVD_Z_THRESHOLD and active_side == "short"):
            score += 1

    # 3. Absorption (Price stall on high delta)
    if len(state.delta_history) > 20:
        recent_delta = sum(list(state.delta_history)[-20:])
        recent_prices = list(state.price_history)[-20:]
        price_range = max(recent_prices) - min(recent_prices)
        if abs(recent_delta) > ABSORPTION_DELTA_THRESHOLD and price_range < 20:
            score += 1

    return score, active_side, wall_ref_price

def build_dynamic_rr(entry, direction, wall_price):
    # Dynamic SL: Place 0.05% behind the institutional wall
    buffer = entry * 0.0005 
    if direction == "long":
        sl = wall_price - buffer if wall_price else entry * 0.995
        risk = entry - sl
        tp = entry + (risk * RR_RATIO)
    else:
        sl = wall_price + buffer if wall_price else entry * 1.005
        risk = sl - entry
        tp = entry - (risk * RR_RATIO)

    return (
        f"\n🎯 Direction: {direction.upper()}"
        f"\n💵 Entry: ${entry:,.2f}"
        f"\n🛡️ SL (Behind Wall): ${sl:,.2f}"
        f"\n💰 TP: ${tp:,.2f}"
        f"\n⚖️ RR: 1:{RR_RATIO}"
    )

async def evaluate_and_alert():
    now = time.time()
    if now - state.last_alert_time < ALERT_COOLDOWN:
        return

    score, direction, wall_price = get_confluence()

    if score >= CONFLUENCE_THRESHOLD and direction:
        message = (
            "🏛 **INSTITUTIONAL CONFLUENCE**\n"
            f"Confidence Score: {score}/3\n"
            "Signals: Wall + CVD + Absorption"
        )
        message += build_dynamic_rr(state.last_price, direction, wall_price)

        await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown")
        state.last_alert_time = now
        log.info(f"Alert sent: {direction} at {state.last_price}")

# ==============================
# MAIN LOOP
# ==============================

async def stream():
    async with websockets.connect(BINANCE_WS) as ws:
        log.info("✅ Connected to Binance Institutional Feed")
        async for message in ws:
            data = json.loads(message)
            stream_name = data["stream"]
            payload = data["data"]

            if "depth" in stream_name:
                update_orderbook(payload)
            elif "aggTrade" in stream_name:
                # Update Trade/CVD Data
                p, q = float(payload["p"]), float(payload["q"])
                delta = -q if payload["m"] else q
                state.cvd += delta
                state.last_price = p
                state.delta_history.append(delta)
                state.price_history.append(p)
                state.cvd_history.append(state.cvd)
                
                # Check for setups
                await evaluate_and_alert()

async def main():
    while True:
        try:
            await stream()
        except Exception as e:
            log.error(f"Stream interrupted: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
