import os
import asyncio
import logging
import json
import aiohttp
import websockets
from dataclasses import dataclass, field
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- 1. CONFIGURATION (Updated for US Servers) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8664798073:AAEwX0DnpjTccGW3IUMWi-hLEMUuclffzl4")
CHAT_ID = os.getenv("CHAT_ID", "6389282895")
SYMBOL = "btcusdt"
BINANCE_WS_BASE = "wss://stream.binance.us:9443/stream?streams="
BINANCE_REST = "https://api.binance.us/api/v3"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

@dataclass
class BotState:
    current_price: float = 0.0
    orderbook: dict = field(default_factory=lambda: {"bids": {}, "asks": {}})

state = BotState()

# --- 2. TELEGRAM COMMANDS ---
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 Bot is LIVE on Binance US!\nUse /status to check price.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price_text = f"${state.current_price:,.2f}" if state.current_price > 0 else "Loading..."
    await update.message.reply_text(f"📊 BTC/USD Price: {price_text}")

# --- 3. BINANCE DATA STREAM ---
async def run_binance_logic():
    url = f"{BINANCE_WS_BASE}{SYMBOL}@kline_15m"
    while True:
        try:
            async with websockets.connect(url) as ws:
                log.info("✅ Connected to Binance US")
                async for message in ws:
                    msg = json.loads(message)
                    data = msg.get("data", {})
                    if "k" in data:
                        state.current_price = float(data["k"]["c"])
        except Exception as e:
            log.error(f"❌ Connection Error: {e}. Retrying...")
            await asyncio.sleep(5)

# --- 4. MAIN ENTRY POINT ---
async def main():
    # drop_pending_updates=True fixes the "Conflict" error in your logs
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))

    await app.initialize()
    await app.start()
    
    log.info("🤖 Bot logic initialized.")

    # Runs Telegram and Binance at the same time
    await asyncio.gather(
        app.updater.start_polling(drop_pending_updates=True),
        run_binance_logic()
    )

if __name__ == "__main__":
    asyncio.run(main())
