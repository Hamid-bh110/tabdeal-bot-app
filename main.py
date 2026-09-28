# -*- coding: utf-8 -*-
"""
ربات معامله‌گر تبدیل — نسخه اپ اندروید (Kivy)
"""

import threading
import time
import hmac
import hashlib
from datetime import datetime, timedelta

import requests

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.togglebutton import ToggleButton
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.clock import Clock
from kivy.core.window import Window

RISK_LEVELS = {
    "کم":     {"risk_pct": 0.005, "stop_loss_pct": 0.02, "take_profit_pct": 0.04},
    "متوسط":  {"risk_pct": 0.01,  "stop_loss_pct": 0.03, "take_profit_pct": 0.06},
    "زیاد":   {"risk_pct": 0.02,  "stop_loss_pct": 0.05, "take_profit_pct": 0.10},
}
DEFAULT_RISK_LEVEL = "کم"

BASE_URL = "https://api1.tabdeal.org"
PUBLIC_PREFIX = "/r/api/v1"
PRIVATE_PREFIX = "/api/v1"

MAX_OPEN_POSITIONS = 5
MAX_DAILY_LOSS_PCT = 0.05
MIN_TRADE_VALUE = 10
MAX_CAPITAL_ALLOCATION_PCT = 0.8
MIN_PRICE_SAMPLES_TO_TRADE = 50
MAX_SYMBOLS_PER_SCAN = 20
CANDLE_LOOKBACK = 200
SCAN_INTERVAL_SECONDS = 60
COOLDOWN_AFTER_LOSS_MINUTES = 30
MAX_TRADES_PER_DAY = 20
QUOTE_SUFFIX = "USDT"


def sma(values, period):
    result = [None] * len(values)
    for i in range(period - 1, len(values)):
        result[i] = sum(values[i - period + 1:i + 1]) / period
    return result


def ema(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    multiplier = 2 / (period + 1)
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    for i in range(period, len(values)):
        result[i] = (values[i] - result[i - 1]) * multiplier + result[i - 1]
    return result


def rsi(values, period=14):
    result = [50.0] * len(values)
    if len(values) < period + 1:
        return result
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def calc(ag, al):
        if al == 0:
            return 100.0
        return 100 - (100 / (1 + ag / al))

    result[period] = calc(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result[i + 1] = calc(avg_gain, avg_loss)
    return result


def macd(values, fast=12, slow=26, signal=9):
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line = [None] * len(values)
    for i in range(len(values)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]
    valid_start = next((i for i, v in enumerate(macd_line) if v is not None), None)
    signal_line = [None] * len(values)
    if valid_start is not None:
        valid_macd = [v for v in macd_line if v is not None]
        sig_valid = ema(valid_macd, signal)
        for offset, val in enumerate(sig_valid):
            signal_line[valid_start + offset] = val
    return macd_line, signal_line


def generate_signal(closes):
    if len(closes) < 55:
        return {"action": "HOLD", "score": 0, "reason": "داده ناکافی"}
    rsi_vals = rsi(closes, 14)
    macd_line, signal_line = macd(closes)
    sma_trend = sma(closes, 50)

    last_close = closes[-1]
    last_rsi, prev_rsi = rsi_vals[-1], rsi_vals[-2]
    last_macd, prev_macd = macd_line[-1], macd_line[-2]
    last_sig, prev_sig = signal_line[-1], signal_line[-2]
    last_sma = sma_trend[-1]

    if last_sma is None or last_macd is None or prev_sig is None or prev_macd is None or last_sig is None:
        return {"action": "HOLD", "score": 0, "reason": "داده ناکافی برای اندیکاتور"}

    score = 0
    reasons = []
    uptrend = last_close > last_sma
    if uptrend:
        score += 1
        reasons.append("روند صعودی")
    if prev_rsi < 35 <= last_rsi:
        score += 1
        reasons.append("خروج RSI از اشباع فروش")
    if prev_macd <= prev_sig and last_macd > last_sig:
        score += 1
        reasons.append("کراس صعودی MACD")

    if last_rsi >= 70:
        return {"action": "HOLD", "score": 0, "reason": "RSI در اشباع خرید"}

    if score >= 2 and uptrend:
        return {"action": "BUY", "score": score, "reason": " + ".join(reasons), "price": last_close}
    return {"action": "HOLD", "score": score, "reason": "شرایط کافی نیست"}


class TabdealClient:
    def __init__(self, api_key="", api_secret="", dry_run=True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.dry_run = dry_run
        self.session = requests.Session()
        if api_key:
            self.session.headers.update({"X-MBX-APIKEY": api_key})

    def _get(self, path, params=None):
        try:
            resp = self.session.get(f"{BASE_URL}{path}", params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def get_all_symbols(self, quote_suffix):
        data = self._get(f"{PUBLIC_PREFIX}/exchangeInfo")
        if not data:
            return []
        market_list = data if isinstance(data, list) else data.get("symbols", [])
        return [s["symbol"] for s in market_list
                if s.get("status") == "TRADING" and s["symbol"].endswith(quote_suffix)]

    def get_recent_trades(self, symbol, limit=50):
        return self._get(f"{PUBLIC_PREFIX}/trades", params={"symbol": symbol, "limit": limit}) or []

    def get_price(self, symbol):
        trades = self.get_recent_trades(symbol, limit=1)
        return float(trades[0]["price"]) if trades else None

    def _signed_request(self, method, path, params):
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        query = "&".join(f"{k}={v}" for k, v in params.items())
        sig = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        if self.dry_run:
            return {"status": "SIMULATED", "params": params}
        try:
            resp = self.session.request(method, f"{BASE_URL}{path}", params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def get_balance(self, asset):
        if self.dry_run:
            return 100.0
        data = self._signed_request("GET", f"{PRIVATE_PREFIX}/account", {})
        if not data:
            return 0.0
        for b in data.get("balances", []):
            if b["asset"] == asset:
                return float(b["free"])
        return 0.0

    def place_market_order(self, symbol, side, quantity):
        return self._signed_request("POST", f"{PRIVATE_PREFIX}/order", {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": quantity,
        })

    def place_stop_limit_order(self, symbol, side, quantity, stop_price, limit_price):
        return self._signed_request("POST", f"{PRIVATE_PREFIX}/order", {
            "symbol": symbol, "side": side, "type": "STOP_LOSS_LIMIT",
            "quantity": quantity, "stopPrice": stop_price, "price": limit_price,
            "timeInForce": "GTC",
        })


class RiskManager:
    def __init__(self):
        self.daily_loss = 0.0
        self.daily_trades = 0
        self.day_start = datetime.now().date()
        self.cooldowns = {}
        self.halted = False

    def _reset_if_new_day(self):
        if datetime.now().date() != self.day_start:
            self.day_start = datetime.now().date()
            self.daily_loss = 0.0
            self.daily_trades = 0
            self.halted = False

    def can_trade(self, symbol, open_count):
        self._reset_if_new_day()
        if self.halted or open_count >= MAX_OPEN_POSITIONS:
            return False
        if self.daily_trades >= MAX_TRADES_PER_DAY:
            return False
        cd = self.cooldowns.get(symbol)
        if cd and datetime.now() < cd:
            return False
        return True

    def position_size(self, balance, entry_price, risk_cfg):
        risk_amount = balance * risk_cfg["risk_pct"]
        position_value = min(risk_amount / risk_cfg["stop_loss_pct"], balance * MAX_CAPITAL_ALLOCATION_PCT)
        if position_value < MIN_TRADE_VALUE:
            return 0.0
        return position_value / entry_price

    def record_result(self, symbol, pnl):
        self._reset_if_new_day()
        self.daily_trades += 1
        if pnl < 0:
            self.daily_loss += abs(pnl)
            self.cooldowns[symbol] = datetime.now() + timedelta(minutes=COOLDOWN_AFTER_LOSS_MINUTES)

    def check_halt(self, balance):
        if self.daily_loss >= balance * MAX_DAILY_LOSS_PCT:
            self.halted = True


class RiskSelector(BoxLayout):
    def __init__(self, on_change, **kwargs):
        super().__init__(orientation="horizontal", spacing=8, size_hint_y=None, height=48, **kwargs)
        self.on_change = on_change
        self.buttons = {}
        for level in RISK_LEVELS:
            btn = ToggleButton(text=level, group="risk_level",
                               state="down" if level == DEFAULT_RISK_LEVEL else "normal")
            btn.bind(on_press=self._make_handler(level))
            self.buttons[level] = btn
            self.add_widget(btn)

    def _make_handler(self, level):
        def handler(instance):
            if instance.state == "down":
                self.on_change(level)
        return handler


class BotUI(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", padding=12, spacing=8, **kwargs)

        self.risk_level = DEFAULT_RISK_LEVEL
        self.running = False
        self.client = TabdealClient(dry_run=True)
        self.risk_manager = RiskManager()
        self.positions = {}
        self.price_history = {}
        self.log_lines = []

        self.add_widget(Label(text="Tabdeal Bot", size_hint_y=None, height=36, font_size=20))
        self.add_widget(Label(text="Risk level:", size_hint_y=None, height=24))
        self.risk_selector = RiskSelector(on_change=self.set_risk_level)
        self.add_widget(self.risk_selector)

        self.risk_info_label = Label(text=self._risk_info_text(), size_hint_y=None, height=24)
        self.add_widget(self.risk_info_label)

        self.status_label = Label(text="Ready (DRY RUN)", size_hint_y=None, height=48)
        self.add_widget(self.status_label)

        self.start_btn = Button(text="START", size_hint_y=None, height=56)
        self.start_btn.bind(on_press=self.toggle_running)
        self.add_widget(self.start_btn)

        scroll = ScrollView()
        self.log_label = Label(text="", size_hint_y=None, valign="top", halign="left", font_size=12)
        self.log_label.bind(texture_size=lambda inst, val: setattr(self.log_label, "height", val[1]))
        self.log_label.bind(width=lambda inst, val: setattr(self.log_label, "text_size", (val, None)))
        scroll.add_widget(self.log_label)
        self.add_widget(scroll)

        Clock.schedule_interval(self.refresh_ui, 2)

    def _risk_info_text(self):
        cfg = RISK_LEVELS[self.risk_level]
        return "risk %.1f%% | SL %.0f%% | TP %.0f%%" % (
            cfg["risk_pct"] * 100, cfg["stop_loss_pct"] * 100, cfg["take_profit_pct"] * 100)

    def set_risk_level(self, level):
        self.risk_level = level
        self.risk_info_label.text = self._risk_info_text()
        self.log("Risk level changed: %s" % level)

    def log(self, text):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_lines.append("[%s] %s" % (ts, text))
        self.log_lines = self.log_lines[-100:]
        text_now = "\n".join(self.log_lines)
        Clock.schedule_once(lambda dt: setattr(self.log_label, "text", text_now))

    def toggle_running(self, instance):
        self.running = not self.running
        if self.running:
            self.start_btn.text = "STOP"
            self.log("Bot started (risk: %s)" % self.risk_level)
            threading.Thread(target=self.bot_loop, daemon=True).start()
        else:
            self.start_btn.text = "START"
            self.log("Bot stopped.")

    def refresh_ui(self, dt):
        mode = "DRY RUN" if self.client.dry_run else "LIVE"
        self.status_label.text = "%s | positions: %d | loss today: %.2f" % (
            mode, len(self.positions), self.risk_manager.daily_loss)

    def bot_loop(self):
        while self.running:
            try:
                self.scan_and_trade()
            except Exception as e:
                self.log("Error: %s" % e)
            time.sleep(SCAN_INTERVAL_SECONDS)

    def scan_and_trade(self):
        risk_cfg = RISK_LEVELS[self.risk_level]

        for symbol in list(self.positions.keys()):
            price = self.client.get_price(symbol)
            if price is None:
                continue
            pos = self.positions[symbol]
            if price <= pos["sl"] or price >= pos["tp"]:
                pnl = (price - pos["entry"]) * pos["qty"]
                self.client.place_market_order(symbol, "SELL", pos["qty"])
                self.risk_manager.record_result(symbol, pnl)
                self.log("Closed %s PnL: %.4f" % (symbol, pnl))
                del self.positions[symbol]

        if not self.risk_manager.can_trade("_", len(self.positions)):
            return

        balance = self.client.get_balance("USDT")
        self.risk_manager.check_halt(balance)

        symbols = self.client.get_all_symbols(QUOTE_SUFFIX)[:MAX_SYMBOLS_PER_SCAN]
        if not symbols:
            self.log("No symbols received (check internet).")
            return

        best = None
        best_symbol = None
        for symbol in symbols:
            price = self.client.get_price(symbol)
            if price is None:
                continue
            hist = self.price_history.setdefault(symbol, [])
            hist.append(price)
            if len(hist) > CANDLE_LOOKBACK:
                del hist[:-CANDLE_LOOKBACK]

            if symbol in self.positions or not self.risk_manager.can_trade(symbol, len(self.positions)):
                continue
            if len(hist) < MIN_PRICE_SAMPLES_TO_TRADE:
                continue

            signal = generate_signal(hist)
            if signal["action"] == "BUY" and (best is None or signal["score"] > best["score"]):
                best, best_symbol = signal, symbol

        self.log("Scanned %d symbols. Risk: %s" % (len(symbols), self.risk_level))

        if best_symbol:
            entry = best["price"]
            qty = self.risk_manager.position_size(balance, entry, risk_cfg)
            if qty <= 0:
                return
            self.client.place_market_order(best_symbol, "BUY", qty)
            sl = entry * (1 - risk_cfg["stop_loss_pct"])
            tp = entry * (1 + risk_cfg["take_profit_pct"])
            self.client.place_stop_limit_order(best_symbol, "SELL", qty, sl, sl * 0.995)
            self.positions[best_symbol] = {"entry": entry, "qty": qty, "sl": sl, "tp": tp}
            self.log("BUY %s @ %.6f SL=%.6f TP=%.6f" % (best_symbol, entry, sl, tp))


class TabdealBotApp(App):
    def build(self):
        Window.clearcolor = (0.06, 0.07, 0.09, 1)
        return BotUI()


if __name__ == "__main__":
    TabdealBotApp().run()
