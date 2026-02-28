“””
BTC/USD Institutional Trading Alert Bot
Exchange : Binance (WebSocket + REST)
Timeframe : 15m
Mechanics : Order Book Imbalance | Volume Profile (POC/HVN/LVN) |
Iceberg Detection | Delta Divergence (CVD vs Price)
R:R        : 1:4 (auto calculated on every alert using ATR)
“””

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

# ─────────────────────────────────────────────

# CONFIG

# ─────────────────────────────────────────────

TELEGRAM_TOKEN   = os.getenv(“TELEGRAM_TOKEN”, “8664798073:AAFoqCuvekYgDwrns7XyJdWjHGq7C05KxnA”)
CHAT_ID          = os.getenv(“CHAT_ID”,        “6389282895”)
SYMBOL           = “btcusdt”
TF               = “15m”
BINANCE_WS_BASE  = “wss://stream.binance.com:9443/stream?streams=”
BINANCE_REST     = “https://api.binance.com/api/v3”

# ── R:R Settings ────────────────────────────

RR_RATIO             = 4       # 1:4 risk to reward
SL_ATR_MULT          = 1.0     # SL = 1x ATR from entry
ATR_PERIOD           = 14

# ── Detection Thresholds ─────────────────────

OB_IMBALANCE_RATIO   = 3.0
OB_WALL_MIN_BTC      = 50.0
ICEBERG_REFRESH_RATE = 0.85
HVN_PERCENTILE       = 75
LVN_PERCENTILE       = 25
POC_SWEEP_TICKS      = 3
CVD_DIVERGENCE_BARS  = 5
ALERT_COOLDOWN_SEC   = 300

logging.basicConfig(level=logging.INFO, format=”%(asctime)s [%(levelname)s] %(message)s”)
log = logging.getLogger(**name**)

# ─────────────────────────────────────────────

# STATE

# ─────────────────────────────────────────────

@dataclass
class Candle:
open: float = 0.0
high: float = 0.0
low:  float = 0.0
close: float = 0.0
volume: float = 0.0
buy_vol: float = 0.0
sell_vol: float = 0.0
closed: bool = False

@dataclass
class BotState:
candles: deque          = field(default_factory=lambda: deque(maxlen=200))
current_candle: Optional[Candle] = None
orderbook: dict         = field(default_factory=lambda: {“bids”: {}, “asks”: {}})
ob_snapshot_time: float = 0.0
ob_prev_snapshot: dict  = field(default_factory=dict)
cvd: float              = 0.0
cvd_history: deque      = field(default_factory=lambda: deque(maxlen=100))
price_history: deque    = field(default_factory=lambda: deque(maxlen=100))
last_alert: dict        = field(default_factory=dict)
volume_profile: dict    = field(default_factory=dict)
poc: float              = 0.0
hvn_levels: list        = field(default_factory=list)
lvn_levels: list        = field(default_factory=list)
tick_size: float        = 1.0
atr: float              = 0.0

state = BotState()
bot   = telegram.Bot(token=TELEGRAM_TOKEN)

# ─────────────────────────────────────────────

# ATR

# ─────────────────────────────────────────────

def calculate_atr() -> float:
candles = list(state.candles)
if len(candles) < ATR_PERIOD + 1:
if state.current_candle:
return state.current_candle.close * 0.005  # fallback 0.5%
return 500.0
true_ranges = []
for i in range(1, len(candles)):
h, l, pc = candles[i].high, candles[i].low, candles[i-1].close
true_ranges.append(max(h - l, abs(h - pc), abs(l - pc)))
return float(np.mean(true_ranges[-ATR_PERIOD:]))

# ─────────────────────────────────────────────

# R:R CALCULATOR

# ─────────────────────────────────────────────

def calc_rr(entry: float, direction: str) -> dict:
atr         = state.atr if state.atr > 0 else calculate_atr()
sl_distance = atr * SL_ATR_MULT
tp_distance = sl_distance * RR_RATIO

```
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
```

def rr_block(entry: float, direction: str) -> str:
r   = calc_rr(entry, direction)
arr = “▲ LONG” if direction == “long” else “▼ SHORT”
return (
f”\n{‘─’*22}\n”
f”*{‘🟢’ if direction == ‘long’ else ‘🔴’} {arr} — 1:{RR_RATIO} R:R*\n”
f”Entry  : `${r['entry']:>10,.0f}`\n”
f”SL     : `${r['sl']:>10,.0f}`  *(−{r[‘risk_pct’]:.2f}%)*\n”
f”TP     : `${r['tp']:>10,.0f}`  *(+{r[‘reward_pct’]:.2f}%)*\n”
f”Risk   : `${r['sl_dist']:,.0f}` | Reward: `${r['tp_dist']:,.0f}`”
)

# ─────────────────────────────────────────────

# TELEGRAM SENDER

# ─────────────────────────────────────────────

async def send_alert(alert_type: str, message: str):
now  = time.time()
last = state.last_alert.get(alert_type, 0)
if now - last < ALERT_COOLDOWN_SEC:
return
state.last_alert[alert_type] = now
try:
await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode=“Markdown”)
log.info(f”Alert [{alert_type}] sent”)
except Exception as e:
log.error(f”Telegram error: {e}”)

# ─────────────────────────────────────────────

# ORDER BOOK ANALYSIS

# ─────────────────────────────────────────────

def analyse_orderbook():
bids  = state.orderbook[“bids”]
asks  = state.orderbook[“asks”]
price = state.current_candle.close if state.current_candle else 0
if not bids or not asks or price == 0:
return

```
near_bids = {p: v for p, v in bids.items() if price * 0.995 <= p <= price}
near_asks = {p: v for p, v in asks.items() if price <= p <= price * 1.005}
bid_vol   = sum(near_bids.values())
ask_vol   = sum(near_asks.values())

# Bid Wall → LONG
if ask_vol > 0 and bid_vol / (ask_vol + 1e-9) >= OB_IMBALANCE_RATIO:
    big = [(p, v) for p, v in near_bids.items() if v >= OB_WALL_MIN_BTC]
    if big:
        biggest = max(big, key=lambda x: x[1])
        asyncio.create_task(send_alert("BID_WALL",
            f"🟢 *BID WALL DETECTED* — BTC/USD\n"
            f"Price : `${price:,.0f}`\n"
            f"Wall  : `${biggest[0]:,.0f}` → `{biggest[1]:.1f} BTC`\n"
            f"OB    : `{bid_vol/ask_vol:.1f}x` bid-heavy\n"
            f"_Institutional support / accumulation zone_"
            + rr_block(price, "long")
        ))

# Ask Wall → SHORT
if bid_vol > 0 and ask_vol / (bid_vol + 1e-9) >= OB_IMBALANCE_RATIO:
    big = [(p, v) for p, v in near_asks.items() if v >= OB_WALL_MIN_BTC]
    if big:
        biggest = max(big, key=lambda x: x[1])
        asyncio.create_task(send_alert("ASK_WALL",
            f"🔴 *ASK WALL DETECTED* — BTC/USD\n"
            f"Price : `${price:,.0f}`\n"
            f"Wall  : `${biggest[0]:,.0f}` → `{biggest[1]:.1f} BTC`\n"
            f"OB    : `{ask_vol/bid_vol:.1f}x` ask-heavy\n"
            f"_Institutional resistance / distribution zone_"
            + rr_block(price, "short")
        ))

# Iceberg Detection
now = time.time()
if state.ob_prev_snapshot and (now - state.ob_snapshot_time) < 3:
    for side in ["bids", "asks"]:
        direction = "long" if side == "bids" else "short"
        curr = state.orderbook[side]
        prev = state.ob_prev_snapshot.get(side, {})
        for pl, qty in curr.items():
            prev_qty = prev.get(pl, 0)
            if prev_qty > OB_WALL_MIN_BTC and qty >= prev_qty * ICEBERG_REFRESH_RATE:
                asyncio.create_task(send_alert(f"ICEBERG_{side.upper()}",
                    f"🧊 *ICEBERG ORDER* — BTC/USD\n"
                    f"Side  : `{'BID' if side == 'bids' else 'ASK'}`\n"
                    f"Level : `${pl:,.0f}` refreshed `{qty:.1f} BTC`\n"
                    f"Prev  : `{prev_qty:.1f}` → Now: `{qty:.1f}` BTC\n"
                    f"_Hidden institutional order absorbing flow_"
                    + rr_block(price, direction)
                ))

state.ob_prev_snapshot = {"bids": dict(bids), "asks": dict(asks)}
state.ob_snapshot_time = now
```

# ─────────────────────────────────────────────

# VOLUME PROFILE

# ─────────────────────────────────────────────

def update_volume_profile(c: Candle):
if not c or c.volume == 0:
return
if c.high == c.low:
pb = round(c.close / 100) * 100
state.volume_profile[pb] = state.volume_profile.get(pb, 0) + c.volume
return
steps = max(1, int((c.high - c.low) / state.tick_size))
vps   = c.volume / steps
ss    = (c.high - c.low) / steps
for i in range(steps):
pb = round((c.low + i * ss) / 100) * 100
state.volume_profile[pb] = state.volume_profile.get(pb, 0) + vps

def compute_poc_hvn_lvn():
vp = state.volume_profile
if len(vp) < 5:
return
levels  = sorted(vp.keys())
arr     = np.array([vp[l] for l in levels])
state.poc        = levels[int(np.argmax(arr))]
state.hvn_levels = [levels[i] for i, v in enumerate(arr) if v >= np.percentile(arr, HVN_PERCENTILE)]
state.lvn_levels = [levels[i] for i, v in enumerate(arr) if v <= np.percentile(arr, LVN_PERCENTILE)]
state.atr        = calculate_atr()

def check_poc_sweep(price: float):
if state.poc == 0:
return
dist = abs(price - state.poc)
if dist <= POC_SWEEP_TICKS * state.tick_size:
direction = “short” if price >= state.poc else “long”
asyncio.create_task(send_alert(“POC_SWEEP”,
f”🎯 *POC SWEEP* — BTC/USD\n”
f”Price : `${price:,.0f}` → POC @ `${state.poc:,.0f}`\n”
f”Dist  : `${dist:.0f}` from Point of Control\n”
f”*Highest volume node — expect strong reaction*”
+ rr_block(price, direction)
))

def check_lvn_entry(price: float):
for lvn in state.lvn_levels:
if abs(price - lvn) <= POC_SWEEP_TICKS * 2 * state.tick_size:
direction = “long” if price > lvn else “short”
asyncio.create_task(send_alert(“LVN_ENTRY”,
f”⚡ *LVN SNIPE ZONE* — BTC/USD\n”
f”Price : `${price:,.0f}` entering LVN @ `${lvn:,.0f}`\n”
f”*Low Volume Node = fast travel / snipe entry*”
+ rr_block(price, direction)
))
break

# ─────────────────────────────────────────────

# CVD DELTA DIVERGENCE

# ─────────────────────────────────────────────

def check_delta_divergence():
if len(state.cvd_history) < CVD_DIVERGENCE_BARS + 1 or   
len(state.price_history) < CVD_DIVERGENCE_BARS + 1:
return
prices = list(state.price_history)[-CVD_DIVERGENCE_BARS:]
cvds   = list(state.cvd_history)[-CVD_DIVERGENCE_BARS:]
pt     = prices[-1] - prices[0]
ct     = cvds[-1] - cvds[0]
entry  = prices[-1]

```
if pt > 0 and ct < -5000:
    asyncio.create_task(send_alert("CVD_BEAR_DIV",
        f"📉 *BEARISH DELTA DIVERGENCE* — BTC/USD\n"
        f"Price : `+${pt:,.0f}` over {CVD_DIVERGENCE_BARS} bars\n"
        f"CVD   : `{ct:,.0f}` BTC _(sellers absorbing)_\n"
        f"_Price up but sell delta rising — distribution_"
        + rr_block(entry, "short")
    ))
elif pt < 0 and ct > 5000:
    asyncio.create_task(send_alert("CVD_BULL_DIV",
        f"📈 *BULLISH DELTA DIVERGENCE* — BTC/USD\n"
        f"Price : `${pt:,.0f}` over {CVD_DIVERGENCE_BARS} bars\n"
        f"CVD   : `+{ct:,.0f}` BTC _(buyers absorbing)_\n"
        f"_Price down but buy delta rising — accumulation_"
        + rr_block(entry, "long")
    ))
```

# ─────────────────────────────────────────────

# CANDLE PROCESSING

# ─────────────────────────────────────────────

def process_kline(data: dict):
k = data[“k”]
c = Candle(
open=float(k[“o”]), high=float(k[“h”]),
low=float(k[“l”]),  close=float(k[“c”]),
volume=float(k[“v”]), buy_vol=float(k[“V”]),
closed=k[“x”]
)
c.sell_vol = c.volume - c.buy_vol
state.current_candle = c
state.cvd += (c.buy_vol - c.sell_vol)

```
if c.closed:
    state.candles.append(c)
    state.cvd_history.append(state.cvd)
    state.price_history.append(c.close)
    update_volume_profile(c)
    compute_poc_hvn_lvn()
    check_delta_divergence()

check_poc_sweep(c.close)
check_lvn_entry(c.close)
```

# ─────────────────────────────────────────────

# DEPTH PROCESSING

# ─────────────────────────────────────────────

def process_depth(data: dict):
for bid in data.get(“b”, []):
p, q = float(bid[0]), float(bid[1])
if q == 0: state.orderbook[“bids”].pop(p, None)
else:      state.orderbook[“bids”][p] = q
for ask in data.get(“a”, []):
p, q = float(ask[0]), float(ask[1])
if q == 0: state.orderbook[“asks”].pop(p, None)
else:      state.orderbook[“asks”][p] = q
analyse_orderbook()

# ─────────────────────────────────────────────

# SNAPSHOT

# ─────────────────────────────────────────────

async def fetch_ob_snapshot():
url = f”{BINANCE_REST}/depth?symbol={SYMBOL.upper()}&limit=500”
async with aiohttp.ClientSession() as s:
async with s.get(url) as r:
data = await r.json()
state.orderbook[“bids”] = {float(p): float(q) for p, q in data[“bids”]}
state.orderbook[“asks”] = {float(p): float(q) for p, q in data[“asks”]}
log.info(“Order book snapshot loaded.”)

# ─────────────────────────────────────────────

# WEBSOCKET

# ─────────────────────────────────────────────

async def run_streams():
streams = f”{SYMBOL}@kline_{TF}/{SYMBOL}@depth@100ms”
url     = BINANCE_WS_BASE + streams
await fetch_ob_snapshot()
while True:
try:
async with websockets.connect(url, ping_interval=20) as ws:
log.info(“WebSocket connected”)
await send_alert(“BOT_START”,
f”🚀 *BTC/USD Institutional Bot LIVE*\n”
f”Exchange : Binance | TF: {TF}\n”
f”R:R      : 1:{RR_RATIO} on every alert\n”
f”SL basis : ATR({ATR_PERIOD})\n”
f”Watching : OB Walls · Iceberg · Volume Profile · CVD Delta”
)
async for raw in ws:
msg    = json.loads(raw)
stream = msg.get(“stream”, “”)
data   = msg.get(“data”, {})
if “kline” in stream:   process_kline(data)
elif “depth” in stream: process_depth(data)
except Exception as e:
log.error(f”WS error: {e} — reconnecting in 5s”)
await asyncio.sleep(5)

# ─────────────────────────────────────────────

# COMMANDS

# ─────────────────────────────────────────────

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
c     = state.current_candle
price = c.close if c else 0
bv    = sum(state.orderbook[“bids”].values())
av    = sum(state.orderbook[“asks”].values())
atr   = state.atr if state.atr > 0 else calculate_atr()
await update.message.reply_text(
f”📊 *BTC/USD Live Status*\n”
f”Price    : `${price:,.0f}`\n”
f”ATR(14)  : `${atr:,.0f}`\n”
f”POC      : `${state.poc:,.0f}`\n”
f”CVD      : `{state.cvd:,.0f}` BTC\n”
f”OB Ratio : `{bv/(av+1e-9):.2f}x` bid/ask\n”
f”HVN      : `{[f'${x:,.0f}' for x in state.hvn_levels[-3:]]}`\n”
f”LVN      : `{[f'${x:,.0f}' for x in state.lvn_levels[:3]]}`”,
parse_mode=“Markdown”
)

async def cmd_ob(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
bids  = sorted(state.orderbook[“bids”].items(), reverse=True)[:5]
asks  = sorted(state.orderbook[“asks”].items())[:5]
lines = [“📖 *Order Book — BTC/USD*\n”, “*ASKS:*”]
for p, v in reversed(asks):
lines.append(f”  `${p:>10,.0f}` → `{v:.2f} BTC`”)
lines.append(“─────────────────────”)
lines.append(”*BIDS:*”)
for p, v in bids:
lines.append(f”  `${p:>10,.0f}` → `{v:.2f} BTC`”)
await update.message.reply_text(”\n”.join(lines), parse_mode=“Markdown”)

async def cmd_vp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(
f”📈 *Volume Profile — BTC/USD*\n”
f”POC : `${state.poc:,.0f}`\n”
f”HVN : `{', '.join(f'${x:,.0f}' for x in state.hvn_levels[-5:])}`\n”
f”LVN : `{', '.join(f'${x:,.0f}' for x in state.lvn_levels[:5])}`”,
parse_mode=“Markdown”
)

async def cmd_rr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
c     = state.current_candle
price = c.close if c else 0
if price == 0:
await update.message.reply_text(“No price yet. Try again shortly.”)
return
args      = ctx.args
direction = (args[0].lower() if args else “long”)
if direction not in (“long”, “short”):
direction = “long”
r = calc_rr(price, direction)
await update.message.reply_text(
f”📐 *Manual R:R — 1:{RR_RATIO}*\n”
f”Direction : `{'LONG ▲' if direction == 'long' else 'SHORT ▼'}`\n”
f”Entry     : `${r['entry']:,.0f}`\n”
f”SL        : `${r['sl']:,.0f}`  *(−{r[‘risk_pct’]:.2f}%)*\n”
f”TP        : `${r['tp']:,.0f}`  *(+{r[‘reward_pct’]:.2f}%)*\n”
f”ATR(14)   : `${state.atr:,.0f}`”,
parse_mode=“Markdown”
)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(
f”🤖 *BTC/USD Institutional Bot*\n\n”
f”/status      — Live price, ATR, POC, CVD, OB\n”
f”/ob          — Top 5 bids & asks\n”
f”/vp          — Volume profile (POC / HVN / LVN)\n”
f”/rr long     — Manual R:R for long\n”
f”/rr short    — Manual R:R for short\n”
f”/help        — This menu\n\n”
f”*All alerts include 1:{RR_RATIO} R:R levels (SL = ATR×{SL_ATR_MULT})*”,
parse_mode=“Markdown”
)

# ─────────────────────────────────────────────

# MAIN

# ─────────────────────────────────────────────

async def main():
app = Application.builder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler(“status”, cmd_status))
app.add_handler(CommandHandler(“ob”,     cmd_ob))
app.add_handler(CommandHandler(“vp”,     cmd_vp))
app.add_handler(CommandHandler(“rr”,     cmd_rr))
app.add_handler(CommandHandler(“help”,   cmd_help))
async with app:
await app.start()
await app.updater.start_polling()
await run_streams()
await app.updater.stop()
await app.stop()

if **name** == “**main**”:
asyncio.run(main())
