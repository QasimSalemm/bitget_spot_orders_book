# app.py
"""
Streamlit web UI for Bitget SPOT orderbook (Top N by QUANTITY).
Run: streamlit run app.py
"""

import threading
import time
import json
from decimal import Decimal, InvalidOperation, getcontext, ROUND_HALF_UP
import requests
from websocket import WebSocketApp
import pandas as pd
import streamlit as st
from threading import Lock, Event
import ssl
from pathlib import Path

    
def Bitget_Spot_Orderbook():
    st.set_page_config(
    page_title="Bitget spot order-book deep analyze",
    page_icon="images/bitget_spot_orderbook.png",
    layout="wide"
)

def local_css(file_name):
    try:
        # Use pathlib to robustly find the file relative to this script
        css_file = Path(__file__).parent / file_name
        with open(css_file) as f:
            st.markdown(f'<style>{f.read()}</style>', unsafe_allow_html=True)
    except FileNotFoundError:
        st.warning(f"CSS file not found: {file_name}")
    except Exception as e:
        pass # Ignore other errors to avoid crashing

local_css("styles/style.css")

st.title("Bitget Spot Orderbook Live Tracker — Real-Time Crypto Market Viewer")
st.write("Monitor live cryptocurrency orders-book with the Bitget Spot Orderbook Viewer, a real-time Streamlit app that connects directly to Bitget’s WebSocket and REST APIs.")
st.divider()


# decimal precision
getcontext().prec = 28
getcontext().rounding = ROUND_HALF_UP

# -------- CONFIG --------
WS_URL = "wss://ws.bitget.com/v2/ws/public"
REST_ORDERBOOK_URL = "https://api.bitget.com/api/v2/spot/market/orderbook"
REST_TICKER_URL = "https://api.bitget.com/api/v2/spot/market/tickers"
DEFAULT_UI_REFRESH = 1000    # ms for streamlit auto-refresh (default shown in UI)
TICKER_POLL = 0.6            # REST ticker poll interval (s)
# formatting
PRICE_DIGITS = 6
QTY_DIGITS = 4
VALUE_DIGITS = 2
STAR_AFTER_PRICE = True
# ------------------------

session = requests.Session()
session.headers.update({"User-Agent": "bitget-top-n/1.0"})

# helpers
def D(x):
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)

def fmt_decimal(d: Decimal, ndig=8, fixed=False):
    if not isinstance(d, Decimal):
        d = D(d)
    if fixed:
        q = Decimal(1).scaleb(-ndig)
        try:
            dd = d.quantize(q)
        except Exception:
            dd = d
        s = format(dd, "f")
        if "." not in s:
            s = s + "." + "0" * ndig
        else:
            intp, frac = s.split(".", 1)
            frac = frac.ljust(ndig, "0")[:ndig]
            s = intp + "." + frac
        return s
    else:
        s = format(d.normalize(), "f")
        if "." in s:
            intp, frac = s.split(".", 1)
            frac = frac.rstrip("0")
            return intp + (("." + frac) if frac else "")
        return s

# Manager maintains shared state and background threads
class BitgetManager:
    def __init__(self):
        self._lock = Lock()
        self._stop = Event()
        self.bids = {}       # Decimal price -> Decimal qty
        self.asks = {}
        self.last_price_str = ""
        self.prev_price_str = ""
        self._ws_app = None
        self._ws_thread = None
        self._ticker_thread = None
        self._updater_thread = None
        self._latest_df = pd.DataFrame()
        self.symbol = "BTCUSDT"
        self.top_n = 5
        self.running = False
        self.last_update_time = time.time()

    def _clear_books(self):
        with self._lock:
            self.bids.clear()
            self.asks.clear()
    

    def init_snapshot_and_price(self, sym):
        try:
            r = session.get(REST_ORDERBOOK_URL, params={"symbol": sym, "type": "step0", "limit": 150}, timeout=6)
            js = r.json() if r.status_code == 200 else {}
            data = js.get("data") or {}
            raw_bids = data.get("bids") or []
            raw_asks = data.get("asks") or []
            with self._lock:
                self.bids.clear(); self.asks.clear()
                for item in raw_bids:
                    if not item or len(item) < 2: continue
                    p, q = item[0], item[1]
                    self.bids[D(p)] = D(q)
                for item in raw_asks:
                    if not item or len(item) < 2: continue
                    p, q = item[0], item[1]
                    self.asks[D(p)] = D(q)
        except Exception as e:
            print("init_snapshot orderbook failed:", e)

        try:
            r = session.get(REST_TICKER_URL, params={"symbol": sym}, timeout=6)
            js = r.json() if r.status_code == 200 else {}
            data = js.get("data")
            if isinstance(data, list) and data:
                entry = data[0]
                p = entry.get("lastPr") or entry.get("last") or entry.get("price")
                if p is not None:
                    with self._lock:
                        self.prev_price_str = self.last_price_str
                        self.last_price_str = str(p)
        except Exception as e:
            print("init_snapshot ticker failed:", e)

    def apply_books(self, blocks):
        with self._lock:
            for block in blocks:
                if not isinstance(block, dict): continue
                for it in block.get("bids", []):
                    try:
                        p, q = D(it[0]), D(it[1])
                    except Exception:
                        continue
                    if q == 0:
                        self.bids.pop(p, None)
                    else:
                        self.bids[p] = q
                for it in block.get("asks", []):
                    try:
                        p, q = D(it[0]), D(it[1])
                    except Exception:
                        continue
                    if q == 0:
                        self.asks.pop(p, None)
                    else:
                        self.asks[p] = q
            self.last_update_time = time.time()

    # def apply_trades(self, blocks):
    #     with self._lock:
    #         for block in blocks:
    #             if isinstance(block, list):
    #                 for t in block:
    #                     if not isinstance(t, dict): continue
    #                     p = t.get("p") or t.get("price") or t.get("last")
    #                     if p is not None:
    #                         self.prev_price_str = self.last_price_str
    #                         self.last_price_str = str(p)
    #             elif isinstance(block, dict):
    #                 p = block.get("p") or block.get("price") or block.get("last")
    #                 if p is not None:
    #                     self.prev_price_str = self.last_price_str
    #                     self.last_price_str = str(p)

    # --- websocket callbacks ---
    def _on_open(self, ws):
        try:
            sub = {"op":"subscribe","args":[
                {"instType":"SPOT","channel":"books","instId":self.symbol},
                {"instType":"SPOT","channel":"trades","instId":self.symbol}
            ]}
            ws.send(json.dumps(sub))
        except Exception as e:
            print("on_open send failed:", e)

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("event") or msg.get("code"):
            return
        arg = msg.get("arg") or {}
        channel = arg.get("channel") or msg.get("channel") or ""
        data = msg.get("data")
        if data is None:
            return
        blocks = data if isinstance(data, list) else [data]
        if any(isinstance(b, dict) and (b.get("bids") or b.get("asks")) for b in blocks):
            self.apply_books(blocks)
        if any(isinstance(b, dict) and any(k in b for k in ("p","price","last")) for b in blocks) or ("trade" in channel):
            self.apply_trades(blocks)

    def _on_error(self, ws, err):
        print("WS error:", err)

    def _on_close(self, ws, code, reason):
        print("WS closed:", code, reason)

    def _ws_runloop(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._ws_app = WebSocketApp(WS_URL,
                                           header={"User-Agent": "bitget-top-n/1.0"},
                                           on_open=self._on_open,
                                           on_message=self._on_message,
                                           on_error=self._on_error,
                                           on_close=self._on_close)
                self._ws_app.run_forever(ping_interval=20, ping_timeout=10, sslopt={"cert_reqs": ssl.CERT_NONE})
            except Exception as e:
                if self._stop.is_set(): break
                print("ws_runloop exception, reconnecting in", backoff, e)
                time.sleep(backoff)
                backoff = min(10, backoff * 1.5)
            else:
                if self._stop.is_set(): break
                time.sleep(0.5)





    def _ticker_poll_loop(self):
        while not self._stop.is_set():
            try:
                r = session.get(REST_TICKER_URL, params={"symbol": self.symbol}, timeout=5)
                if r.status_code == 200:
                    js = r.json()
                    data = js.get("data")
                    if isinstance(data, list) and data:
                        entry = data[0]
                        p = entry.get("lastPr") or entry.get("last") or entry.get("price")
                        if p is not None:
                            with self._lock:
                                if str(p) != self.last_price_str:
                                    self.prev_price_str = self.last_price_str
                                    self.last_price_str = str(p)
            except Exception:
                pass
            for _ in range(int(TICKER_POLL * 10)):
                if self._stop.is_set(): return
                time.sleep(0.1)

    def _updater_loop(self):
        """
        Background updater loop for BitgetManager.
        Context-safe, efficient, and adaptive.
        """

        # --- Prevent Streamlit context usage in this thread ---
        try:
            import streamlit.runtime.scriptrunner.script_run_context as stc  # type: ignore
            stc.get_script_run_ctx = lambda *a, **kw: None
        except Exception:
            pass  # Ignore if Streamlit version doesn’t have this module

        # --- Normal loop ---
        last_snapshot = None
        idle_loops = 0

        while not self._stop.is_set():
            try:
                df = self.build_dataframe(self.top_n)
                if not df.equals(last_snapshot):
                    with self._lock:
                        self._latest_df = df
                    last_snapshot = df
                    idle_loops = 0
                else:
                    idle_loops += 1

                time.sleep(0.5 + min(idle_loops * 0.1, 1.5))

            except Exception as e:
                print(f"[Updater Error] {e}")
                time.sleep(1.0)

    def start(self):
        if self.running:
            return
        # clear previous
        self._stop.clear()
        self.init_snapshot_and_price(self.symbol)
        # threads
        self._ws_thread = threading.Thread(target=self._ws_runloop, daemon=True)
        self._ws_thread.start()
        self._ticker_thread = threading.Thread(target=self._ticker_poll_loop, daemon=True)
        self._ticker_thread.start()
        self._updater_thread = threading.Thread(target=self._updater_loop, daemon=True)
        self._updater_thread.start()
        self.running = True

    def stop(self):
        if not self.running:
            return
        self._stop.set()
        try:
            if self._ws_app:
                self._ws_app.close()
        except Exception:
            pass
        # join attempts (short)
        if self._ws_thread:
            self._ws_thread.join(timeout=1)
        if self._ticker_thread:
            self._ticker_thread.join(timeout=1)
        if self._updater_thread:
            self._updater_thread.join(timeout=1)
        self.running = False

    def restart(self, symbol=None, top_n=None):
        # stop, set params, clear, and start again
        self.stop()
        if symbol:
            self.symbol = symbol
        if top_n:
            self.top_n = top_n
        self._clear_books()
        # small delay to ensure ws closed
        time.sleep(0.1)
        self.start()
    def get_total_values(self):
        """
        Calculate total buy and sell value across the full orderbook.
        Returns tuple: (total_buy_value, total_sell_value)
        """
        with self._lock:
            try:
                total_buy = sum(p * q for p, q in self.bids.items())
                total_sell = sum(p * q for p, q in self.asks.items())
                return total_buy, total_sell
            except Exception as e:
                print("Error calculating totals:", e)
                return Decimal(0), Decimal(0)

    def get_metrics(self):
        with self._lock:
            # Spread
            best_bid = max(self.bids.keys()) if self.bids else Decimal(0)
            best_ask = min(self.asks.keys()) if self.asks else Decimal(0)
            spread = Decimal(0)
            spread_pct = Decimal(0)
            if best_bid > 0 and best_ask > 0:
                spread = best_ask - best_bid
                spread_pct = (spread / best_ask) * 100

            # Imbalance (Volume)
            # Simple imbalance: Buy Vol / (Buy Vol + Sell Vol)
            total_buy_vol = sum(self.bids.values())
            total_sell_vol = sum(self.asks.values())
            imbalance = 0.5
            if (total_buy_vol + total_sell_vol) > 0:
                imbalance = float(total_buy_vol / (total_buy_vol + total_sell_vol))
            
            # Latency (approx)
            latency = time.time() - self.last_update_time
            
            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": spread,
                "spread_pct": spread_pct,
                "imbalance": imbalance,
                "latency": latency
            }

    def get_support_resistance(self, count=3):
        """
        Identify top 'count' price levels with highest quantity for Bids (Support) and Asks (Resistance).
        Returns: {
            "supports": [(price, qty), ...],
            "resistances": [(price, qty), ...]
        }
        """
        with self._lock:
            # Sort by Quantity descending
            top_bids = sorted(self.bids.items(), key=lambda x: x[1], reverse=True)[:count]
            top_asks = sorted(self.asks.items(), key=lambda x: x[1], reverse=True)[:count]
            
            # Sort the results by Price for display logic if needed, 
            # BUT usually for S/R we want to see the strongest levels regardless of distance.
            # Keeping them sorted by strength (Quantity) is better for "Real Support/Resist".
            return {
                "supports": top_bids,
                "resistances": top_asks
            }

    def build_dataframe(self, TOP_N):
        with self._lock:
            top_asks = sorted(self.asks.items(), key=lambda x: x[1], reverse=True)[:TOP_N]
            top_bids = sorted(self.bids.items(), key=lambda x: x[1], reverse=True)[:TOP_N]
            cur_price = self.last_price_str or "N/A"
            prev_price = self.prev_price_str or ""

        nearest_ask_idx = None
        nearest_bid_idx = None
        try:
            if cur_price != "N/A":
                cur = Decimal(str(cur_price))
                if top_asks:
                    best_idx = 0
                    best_dist = abs(cur - top_asks[0][0])
                    for i, (p, q) in enumerate(top_asks):
                        d = abs(cur - p)
                        if d < best_dist:
                            best_dist = d
                            best_idx = i
                    nearest_ask_idx = best_idx
                if top_bids:
                    best_idx = 0
                    best_dist = abs(cur - top_bids[0][0])
                    for i, (p, q) in enumerate(top_bids):
                        d = abs(cur - p)
                        if d < best_dist:
                            best_dist = d
                            best_idx = i
                    nearest_bid_idx = best_idx
        except Exception:
            nearest_ask_idx = nearest_bid_idx = None

        rows = max(len(top_asks), len(top_bids))
        rows_list = []
        for i in range(rows):
            # SELL side
            sell_qty = sell_price = sell_val = ""
            if i < len(top_asks):
                p_a, q_a = top_asks[i]
                icon = "★" if (nearest_ask_idx is not None and i == nearest_ask_idx) else ""
                sell_qty = fmt_decimal(q_a, QTY_DIGITS, fixed=True)
                price_str = fmt_decimal(p_a, PRICE_DIGITS, fixed=True)
                if STAR_AFTER_PRICE:
                    sell_price = price_str + (" " + icon if icon else "")
                else:
                    sell_price = (icon + " " if icon else "") + price_str
                sv = (p_a * q_a) if (p_a and q_a) else Decimal(0)
                sell_val = fmt_decimal(sv, VALUE_DIGITS, fixed=True)
            # BUY side
            buy_qty = buy_price = buy_val = ""
            if i < len(top_bids):
                p_b, q_b = top_bids[i]
                icon = "★" if (nearest_bid_idx is not None and i == nearest_bid_idx) else ""
                buy_qty = fmt_decimal(q_b, QTY_DIGITS, fixed=True)
                price_str = fmt_decimal(p_b, PRICE_DIGITS, fixed=True)
                if STAR_AFTER_PRICE:
                    buy_price = price_str + (" " + icon if icon else "")
                else:
                    buy_price = (icon + " " if icon else "") + price_str
                bv = (p_b * q_b) if (p_b and q_b) else Decimal(0)
                buy_val = fmt_decimal(bv, VALUE_DIGITS, fixed=True)

            rows_list.append({
                "SELL QTY": sell_qty,
                "SELL PRICE": sell_price,
                "SELL VALUE": sell_val,
                "BUY VALUE": buy_val,
                "BUY PRICE": buy_price,
                "BUY QTY": buy_qty
            })

        df = pd.DataFrame(rows_list)
        # Additional meta fields as dataframe attributes
        try:
            # price change icon
            icon = "●"
            if self.prev_price_str and self.last_price_str and self.prev_price_str != "":
                pprev = Decimal(str(self.prev_price_str))
                pcur = Decimal(str(self.last_price_str))
                if pcur > pprev:
                    icon = "▲"
                elif pcur < pprev:
                    icon = "▼"
            df.attrs["current_price"] = self.last_price_str or "N/A"
            df.attrs["price_icon"] = icon
        except Exception:
            df.attrs["current_price"] = self.last_price_str or "N/A"
            df.attrs["price_icon"] = "●"

        return df

    @property
    def latest_df(self):
        with self._lock:
            return self._latest_df.copy()

# SINGLETON manager for the Streamlit app
if "manager" not in st.session_state:
    st.session_state.manager = BitgetManager()

mgr: BitgetManager = st.session_state.manager

# Sidebar controls
with st.sidebar:
    st.header("⚙️ Settings")

    symbol = st.text_input(
        "Symbol (e.g. BTCUSDT)",
        value=mgr.symbol
    ).strip().upper()

    top_n = st.slider(
        "Top N (Orderbook depth)",
        min_value=1,
        max_value=200,
        value=mgr.top_n,
        step=1
    )

    refresh_ms = st.slider(
        "Auto refresh (milliseconds)",
        min_value=200,
        max_value=10000,
        value=DEFAULT_UI_REFRESH,
        step=100
    )
    time_frame = st.selectbox("Select Time-frame", {
        "1 Minute": "1m",
        "2 Minute": "2m",
        "3 Minute": "3m",
        "5 Minutes": "5m",
        "15 Minutes": "15m",
        "1 Hour": "60m",
        "4 Hours": "240m",
        "1 Day": "1d"
    })

    # --- Buttons stacked vertically ---
    if st.button("Start" if not mgr.running else "Restart"):
        mgr.restart(symbol=symbol, top_n=int(top_n))

    if st.button("Stop"):
        mgr.stop()

    if st.button("Clear"):
        mgr._clear_books()

# col1, col2, col3 = st.columns([3, 1, 1])

# with col1:
#     symbol = st.text_input("Symbol (e.g. BTCUSDT)", value=mgr.symbol).strip().upper()
# with col2:
#     top_n = st.number_input("Top N", min_value=1, max_value=50, value=mgr.top_n, step=1)
# with col3:
#     refresh_ms = st.number_input("Auto refresh (ms)", min_value=200, max_value=10000, value=DEFAULT_UI_REFRESH, step=100)

# c1, c2, c3 = st.columns(3)
# with c1:
#     if st.button("Start" if not mgr.running else "Restart"):
#         mgr.restart(symbol=symbol, top_n=int(top_n))
# with c2:
#     if st.button("Stop"):
#         mgr.stop()
# with c3:
#     if st.button("Clear books"):
#         mgr._clear_books()

# show status
st.write("**Running :** ", "Yes" if mgr.running else "No")
st.write("**Symbol :** ", mgr.symbol)
st.write("**Top :** ", mgr.top_n)
# st.write("")
# status_col1, status_col2, status_col3 = st.columns([2, 2, 6])
# status_col1.metric("Running", "Yes" if mgr.running else "No")
# status_col2.metric("Symbol", mgr.symbol)
# status_col3.metric("Top N", mgr.top_n)

# Auto-update logic is handled at the end of the script to ensure full page render first
pass

# Table + current price
df = mgr.latest_df
cur_price = df.attrs.get("current_price", mgr.last_price_str or "N/A") if hasattr(df, "attrs") else (mgr.last_price_str or "N/A")
price_icon = df.attrs.get("price_icon", "●") if hasattr(df, "attrs") else "●"

st.subheader(f"Current Price: {cur_price} {price_icon}")

# --- Metrics Row ---
metrics = mgr.get_metrics()
m1, m2, m3, m4 = st.columns(4)
m1.metric("Best Bid", f"{metrics['best_bid']:,.2f}")
m2.metric("Best Ask", f"{metrics['best_ask']:,.2f}")
m3.metric("Spread", f"{metrics['spread']:.2f} ({metrics['spread_pct']:.3f}%)")
# Imbalance formatted as Buy Pressue %
m4.metric("Orderbook Imbalance (Buy side)", f"{metrics['imbalance']*100:.1f}%", 
          delta=f"{(metrics['imbalance']-0.5)*100:.1f}%" if metrics['imbalance'] != 0.5 else None)

st.caption(f"Last update: {metrics['latency']:.2f}s ago")

# --- Support & Resistance (Highest Liquidity) ---
sr_levels = mgr.get_support_resistance(count=3)

st.markdown("---")
st.subheader("🧱 Market Depth Analysis")

# Use metrics for the absolute strongest levels
best_support = sr_levels["supports"][0] if sr_levels["supports"] else (0, 0)
best_resistance = sr_levels["resistances"][0] if sr_levels["resistances"] else (0, 0)

col_sr1, col_sr2 = st.columns(2)
col_sr1.metric("Strongest Support (Bid Wall)", f"{best_support[0]}", f"{best_support[1]} Qty", delta_color="normal")
col_sr2.metric("Strongest Resistance (Sell Wall)", f"{best_resistance[0]}", f"-{best_resistance[1]} Qty", delta_color="inverse")

# Show the rest in a small expander if desired, or just below
with st.expander("View Top 3 Support & Resistance Levels"):
    sc1, sc2 = st.columns(2)
    with sc1:
        st.markdown("**Support Levels**")
        st.dataframe(pd.DataFrame(sr_levels["supports"], columns=["Price", "Qty"]), hide_index=True, use_container_width=True)
    with sc2:
        st.markdown("**Resistance Levels**")
        st.dataframe(pd.DataFrame(sr_levels["resistances"], columns=["Price", "Qty"]), hide_index=True, use_container_width=True)

st.markdown("---")
st.markdown("**Orderbook (Top by QTY)**")

if df is None or df.empty:
    st.info("No data yet — click Start to begin streaming.")
else:
    # --- Pure Streamlit Styling with Pandas Styler ---
    def color_coding(s):
        if s.name == "SELL PRICE":
            return ['color: #ff4b4b'] * len(s) # Streamlit Red
        elif s.name == "BUY PRICE":
            return ['color: #09ab3b'] * len(s) # Streamlit Green
        return [''] * len(s)

    # Apply Styler
    styler = df.style.apply(color_coding, axis=0)
    
    # Display
    st.dataframe(
        styler,
        use_container_width=True,
        height=(35 * (len(df) + 1)) if len(df) < 20 else 600,
        column_config={
            "SELL VALUE": st.column_config.NumberColumn("SELL VALUE (USDT)"),
            "BUY VALUE": st.column_config.NumberColumn("BUY VALUE (USDT)")
        }
    )

    # --- Totals across FULL orderbook ---
    try:
        total_buy, total_sell = mgr.get_total_values()
        st.markdown("### Total Market Depth (All Orders)")
        c1, c2 = st.columns(2)
        c1.metric("Total Sell Value", f"{total_sell:,.2f}")
        c2.metric("Total Buy Value", f"{total_buy:,.2f}")
    except Exception as e:
        st.warning(f"Error calculating orderbook totals: {e}")


    # Build TradingView iframe
    html_code = f"""
    <iframe
      src="https://s.tradingview.com/widgetembed/?frameElementId=tradingview_123
      &symbol=BITGET:{symbol}
      &interval={time_frame}
      &hidesidetoolbar=1
      &theme=dark
      &style=1
      &timezone=Etc%2FUTC"
      width="100%"
      height="600"
      frameborder="0"
    ></iframe>
    """

    st.components.v1.html(html_code, height=600)

st.markdown("---")
st.caption("Notes: background threads perform WebSocket + REST polling. Change symbol and Top N via inputs, then click Start/Restart.")

if mgr.running:
    time.sleep(refresh_ms / 1000)
    try:
        st.rerun()
    except AttributeError:
        st.experimental_rerun()
if __name__ == "__main__":
    Bitget_Spot_Orderbook()