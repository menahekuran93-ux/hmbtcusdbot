import os
import asyncio
import logging
import time
import json
import aiohttp
import websockets
import numpy as np
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- 1. CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8664798073:AAEwX0DnpjTccGW3IUMWi-hLEMUuclffzl4")
CHAT_ID = os.getenv("CHAT_ID", "6389282895")
SYMBOL = "btcusdt"
TF = "15m"
BINANCE_WS_BASE = "wss://stream.binance.com:9443/stream?streams="
BINANCE_REST = "https://api.binance.com/api/v3"

# Strategy Settings
RR_RATIO = 4
OB_IMBALANCE_RATIO = 3.0
OB_WALL_MIN_BTC = 50.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

@dataclass
class Candle:
    close: float = 0.0
    volume: float = 0.0
    closed: bool = False

@dataclass
class BotState:
    current_price: float = 0.0
    orderbook: dict = field(default_factory=lambda: {"bids": {}, "asks": {}})
    last_alert_time: float = 0.0

state = BotState()

# --- 2. TRADING LOGIC ---

def analyse_orderbook():
    price = state.current_price
    if price == 0: return
    
    bids, asks = state.orderbook["bids"], state.orderbook["asks"]
    bid_vol = sum(v for p, v in bids.items() if p >= price * 0.999)
    ask_vol = sum(v for p, v in asks.items() if p <= price * 1.001)

    # Simple imbalance check for logs
    if ask_vol > 0 and bid_vol / ask_vol > OB_IMBALANCE_RATIO:
        log.info(f"Market heavy on BIDS: {bid_vol/ask_vol:.2f}x")

# --- 3. TELEGRAM COMMANDS ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 BTC/USD Institutional Bot is LIVE!\nUse /status to check market.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (f"📊 **BTC/USD Status**\n"
           f"Price: ${state.current_price:,.2f}\n"
           f"Bids Tracked: {len(state.orderbook['bids'])}\n"
           f"Asks Tracked: {len(state.orderbook['asks'])}")
    await update.message.reply_text(msg, parse_mode="Markdown")

# --- 4. DATA STREAMS ---

async def run_binance_logic():
    """Handles WebSocket connection to Binance"""
    url = f"{BINANCE_WS_BASE}{SYMBOL}@kline_{TF}/{SYMBOL}@depth@100ms"
    while True:
        try:
            async with websockets.connect(url) as ws:
                log.info("✅ Connected to Binance WebSocket")
                async for message in ws:
                    msg = json.loads(message)
                    data = msg.get("data", {})
                    stream = msg.get("stream", "")

                    if "kline" in stream:
                        state.current_price = float(data["k"]["c"])
                    
                    elif "depth" in stream:
                        # Update local orderbook
                        for b in data.get("b", []):
                            p, q = float(b[0]), float(b[1])
                            if q == 0: state.orderbook["bids"].pop(p, None)
                            else: state.orderbook["bids"][p] = q
                        for a in data.get("a", []):
                            p, q = float(a[0]), float(a[1])
                            if q == 0: state.orderbook["asks"].pop(p, None)
                            else: state.orderbook["asks"][p] = q
                        analyse_orderbook()
        except Exception as e:
            log.error(f"❌ Binance WS Error: {e}. Reconnecting...")
            await asyncio.sleep(5)

# --- 5. MAIN EXECUTION ---

async def main():
    # Setup Telegram Application
    # drop_pending_updates=True clears old messages so the bot doesn't spam you on start
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))

    # Initialize and start Telegram
    await app.initialize()
    await app.start()
    
    log.info("🤖 Bot logic initialized. Starting streams...")

    # Run BOTH the Telegram listener and Binance logic at the same time
    await asyncio.gather(
        app.updater.start_polling(drop_pending_updates=True),
        run_binance_logic()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
