"""
ApexTrader — E*TRADE Client
Alpaca-compatible adapter over the E*TRADE OAuth REST API.

Implements the same interface consumed by EnhancedExecutor so the executor
needs zero broker-specific changes.  All Alpaca-SDK object shapes are mirrored
via lightweight dataclasses.

E*TRADE API reference: https://apisb.etrade.com/docs/api/

Authentication flow
-------------------
E*TRADE uses OAuth 1.0a with a two-step verifier redirect:
  1. Request a request-token  →  redirect user to authorise URL
  2. User pastes verifier code →  exchange for access-token

For automated / headless operation the access token is cached in
.etrade_token_cache.json and refreshed each market day.  If the cache is
stale (new trading session), the code raises ETradeAuthRequired; the caller
must call `client.authorize()` interactively (or serve the URL via web hook).

Required env vars
-----------------
  ETRADE_CONSUMER_KEY     – OAuth consumer key
  ETRADE_CONSUMER_SECRET  – OAuth consumer secret
  ETRADE_ACCOUNT_ID       – Numeric account ID (string)
  ETRADE_SANDBOX          – "true" for sandbox / paper (default "false")
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests
from requests_oauthlib import OAuth1Session

log = logging.getLogger("ApexTrader")

_LIVE_BASE    = "https://api.etrade.com/v1"
_SANDBOX_BASE = "https://apisb.etrade.com/v1"
_TOKEN_CACHE  = Path(__file__).parent.parent / ".etrade_token_cache.json"

_REQUEST_TOKEN_URL = "https://api.etrade.com/oauth/request_token"
_ACCESS_TOKEN_URL  = "https://api.etrade.com/oauth/access_token"
_AUTHORIZE_URL     = "https://us.etrade.com/e/t/etws/authorize"
_SB_REQUEST_TOKEN  = "https://apisb.etrade.com/oauth/request_token"
_SB_ACCESS_TOKEN   = "https://apisb.etrade.com/oauth/access_token"
_SB_AUTHORIZE_URL  = "https://us.etrade.com/e/t/etws/authorize"   # same portal


# ── Alpaca-compatible shape objects ───────────────────────────────────────────

@dataclass
class _Position:
    symbol:          str
    qty:             str          # signed: positive=long, negative=short
    qty_available:   str          # same in E*TRADE (no pending legs)
    avg_entry_price: str
    current_price:   str
    unrealized_pl:   str
    unrealized_plpc: str          # as decimal e.g. 0.032


@dataclass
class _Order:
    id:             str
    symbol:         str
    side:           str           # "buy" or "sell"
    status:         str           # "new", "partially_filled", "filled", "cancelled"
    qty:            str
    filled_qty:     str
    submitted_at:   datetime.datetime


@dataclass
class _Quote:
    bid_price: float
    ask_price: float


@dataclass
class _Asset:
    symbol:   str
    status:   str     # "active" or "inactive"
    tradable: bool


@dataclass
class _Account:
    equity:         float
    buying_power:   float
    daytrade_count: int
    pattern_day_trader: bool


# ── Order request dataclasses (mirrors alpaca-py request objects) ─────────────

@dataclass
class MarketOrderRequest:
    symbol:         str
    qty:            int
    side:           str    # "buy" or "sell"
    time_in_force:  str    # "day" or "gtc"


@dataclass
class LimitOrderRequest:
    symbol:         str
    qty:            int
    side:           str
    time_in_force:  str
    limit_price:    float
    extended_hours: bool = False


@dataclass
class TrailingStopOrderRequest:
    symbol:         str
    qty:            int
    side:           str
    time_in_force:  str
    trail_percent:  float
    type:           str = "trailing_stop"   # unused — kept for interface parity


# ── Auth helper ───────────────────────────────────────────────────────────────

class ETradeAuthRequired(Exception):
    """Raised when OAuth tokens are missing or expired and manual auth is needed."""
    def __init__(self, url: str):
        self.url = url
        super().__init__(f"E*TRADE authorization required. Visit: {url}")


# ── Main client ───────────────────────────────────────────────────────────────

class ETradeClient:
    """
    Alpaca-interface-compatible E*TRADE REST client.

    Usage:
        client = ETradeClient(consumer_key, consumer_secret, account_id, sandbox)
        # First run (interactive):
        client.authorize()          # prints URL, prompts for verifier
        # Subsequent runs:
        client.ensure_authenticated()
    """

    def __init__(
        self,
        consumer_key:    str,
        consumer_secret: str,
        account_id:      str,
        sandbox:         bool = False,
    ):
        self._consumer_key    = consumer_key
        self._consumer_secret = consumer_secret
        self._account_id      = account_id
        self._sandbox         = sandbox
        self._base            = _SANDBOX_BASE if sandbox else _LIVE_BASE
        self._req_token_url   = _SB_REQUEST_TOKEN if sandbox else _REQUEST_TOKEN_URL
        self._acc_token_url   = _SB_ACCESS_TOKEN  if sandbox else _ACCESS_TOKEN_URL
        self._auth_url        = _SB_AUTHORIZE_URL if sandbox else _AUTHORIZE_URL

        self._access_token:  Optional[str] = None
        self._access_secret: Optional[str] = None
        self._session:       Optional[OAuth1Session] = None

        self._load_token_cache()

    # ── Authentication ─────────────────────────────────────────────────────

    def _load_token_cache(self) -> None:
        """Load cached access token from disk (survives restarts within same session)."""
        try:
            if _TOKEN_CACHE.exists():
                data = json.loads(_TOKEN_CACHE.read_text())
                today = datetime.date.today().isoformat()
                if data.get("date") == today:
                    self._access_token  = data["access_token"]
                    self._access_secret = data["access_secret"]
                    self._build_session()
                    log.info("E*TRADE: loaded cached OAuth token for today")
        except Exception as e:
            log.warning(f"E*TRADE token cache load failed: {e}")

    def _save_token_cache(self) -> None:
        try:
            _TOKEN_CACHE.write_text(json.dumps({
                "date":          datetime.date.today().isoformat(),
                "access_token":  self._access_token,
                "access_secret": self._access_secret,
            }))
        except Exception as e:
            log.warning(f"E*TRADE token cache save failed: {e}")

    def _build_session(self) -> None:
        self._session = OAuth1Session(
            self._consumer_key,
            client_secret        = self._consumer_secret,
            resource_owner_key   = self._access_token,
            resource_owner_secret= self._access_secret,
        )

    def authorize(self) -> None:
        """
        Interactive OAuth 1.0a flow.  Call once per trading day.
        Prints the authorisation URL, waits for the verifier code, then
        exchanges it for an access token and caches it.
        """
        req_sess = OAuth1Session(
            self._consumer_key,
            client_secret = self._consumer_secret,
            callback_uri  = "oob",
        )
        fetch_response = req_sess.fetch_request_token(self._req_token_url)
        req_token  = fetch_response["oauth_token"]
        req_secret = fetch_response["oauth_token_secret"]

        auth_url = f"{self._auth_url}?key={self._consumer_key}&token={req_token}"
        print(f"\nE*TRADE Authorization required.\nVisit: {auth_url}\n")
        verifier = input("Enter the verifier code: ").strip()

        acc_sess = OAuth1Session(
            self._consumer_key,
            client_secret         = self._consumer_secret,
            resource_owner_key    = req_token,
            resource_owner_secret = req_secret,
            verifier              = verifier,
        )
        acc_response = acc_sess.fetch_access_token(self._acc_token_url)
        self._access_token  = acc_response["oauth_token"]
        self._access_secret = acc_response["oauth_token_secret"]
        self._build_session()
        self._save_token_cache()
        log.info("E*TRADE: OAuth authorization successful")

    def ensure_authenticated(self) -> None:
        """Call at startup. Raises ETradeAuthRequired if interactive auth is needed."""
        if self._session is None:
            req_sess = OAuth1Session(
                self._consumer_key,
                client_secret = self._consumer_secret,
                callback_uri  = "oob",
            )
            try:
                fetch_response = req_sess.fetch_request_token(self._req_token_url)
                req_token = fetch_response["oauth_token"]
                auth_url = f"{self._auth_url}?key={self._consumer_key}&token={req_token}"
                raise ETradeAuthRequired(auth_url)
            except ETradeAuthRequired:
                raise
            except Exception as e:
                raise ETradeAuthRequired("(could not generate URL)") from e

    def _get(self, path: str, params: Optional[Dict] = None) -> Dict:
        url = f"{self._base}{path}"
        resp = self._session.get(url, params=params, headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: Dict) -> Dict:
        url = f"{self._base}{path}"
        resp = self._session.post(
            url,
            json    = payload,
            headers = {"Accept": "application/json", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> Dict:
        url = f"{self._base}{path}"
        resp = self._session.delete(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()

    # ── Account ───────────────────────────────────────────────────────────

    def get_account(self) -> _Account:
        """Return account snapshot — mirrors alpaca TradingClient.get_account()."""
        data = self._get(f"/accounts/{self._account_id}/balance")
        bal  = data["BalanceResponse"]["Computed"]
        return _Account(
            equity         = float(bal.get("RealizedPnl", 0)) + float(bal.get("cashBuyingPower", 0)),
            buying_power   = float(bal.get("cashBuyingPower", bal.get("marginBuyingPower", 0))),
            daytrade_count = int(bal.get("dayTradesRemaining",  3)),
            pattern_day_trader = bool(bal.get("patternDayTrader", False)),
        )

    # ── Positions ─────────────────────────────────────────────────────────

    def get_all_positions(self) -> List[_Position]:
        """Return all open positions — mirrors alpaca TradingClient.get_all_positions()."""
        try:
            data = self._get(f"/accounts/{self._account_id}/portfolio")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 204:
                return []   # no positions
            raise

        positions = []
        for account_portfolio in data.get("PortfolioResponse", {}).get("AccountPortfolio", []):
            for pos in account_portfolio.get("Position", []):
                qty_sign = float(pos["quantity"])
                if pos.get("positionType", "LONG") == "SHORT":
                    qty_sign = -abs(qty_sign)
                qty_avail = float(pos.get("quantityAvailable", pos["quantity"]))
                positions.append(_Position(
                    symbol          = pos["Product"]["symbol"],
                    qty             = str(qty_sign),
                    qty_available   = str(qty_avail),
                    avg_entry_price = str(pos.get("costPerShare", pos.get("pricePaid", 0))),
                    current_price   = str(pos.get("currentPrice", 0)),
                    unrealized_pl   = str(pos.get("totalGain", 0)),
                    unrealized_plpc = str(pos.get("totalGainPct", 0) / 100.0),
                ))
        return positions

    # ── Orders ────────────────────────────────────────────────────────────

    def _et_status(self, raw: str) -> str:
        """Map E*TRADE order status → Alpaca-style status string."""
        mapping = {
            "OPEN":              "new",
            "PARTIALLY_FILLED":  "partially_filled",
            "EXECUTED":          "filled",
            "CANCELLED":         "cancelled",
            "CANCEL_REQUESTED":  "cancelled",
            "EXPIRED":           "cancelled",
            "REJECTED":         "cancelled",
        }
        return mapping.get(raw.upper(), raw.lower())

    def _order_from_raw(self, raw: Dict) -> _Order:
        detail  = raw.get("OrderDetail", [{}])[0]
        instr   = detail.get("Instrument", [{}])[0]
        side_raw = instr.get("orderAction", "BUY").upper()
        side    = "buy" if side_raw in ("BUY", "BUY_TO_COVER") else "sell"
        placed  = raw.get("placedTime", 0)
        placed_dt = (
            datetime.datetime.fromtimestamp(placed / 1000, tz=datetime.timezone.utc)
            if placed else datetime.datetime.now(datetime.timezone.utc)
        )
        return _Order(
            id           = str(raw.get("orderId", "")),
            symbol       = instr.get("Product", {}).get("symbol", ""),
            side         = side,
            status       = self._et_status(raw.get("status", "OPEN")),
            qty          = str(instr.get("orderedQuantity", 0)),
            filled_qty   = str(instr.get("filledQuantity", 0)),
            submitted_at = placed_dt,
        )

    def get_orders(self, status: str = "OPEN") -> List[_Order]:
        """Return list of orders — mirrors alpaca TradingClient.get_orders()."""
        try:
            data = self._get(
                f"/accounts/{self._account_id}/orders",
                params={"status": status, "count": 100},
            )
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 204:
                return []
            raise
        orders_raw = (
            data.get("OrdersResponse", {}).get("Order", [])
        )
        return [self._order_from_raw(o) for o in orders_raw]

    def get_order_by_id(self, order_id: str) -> _Order:
        """Return single order by ID — mirrors alpaca TradingClient.get_order_by_id()."""
        data = self._get(
            f"/accounts/{self._account_id}/orders",
            params={"orderIds": order_id},
        )
        orders_raw = data.get("OrdersResponse", {}).get("Order", [])
        if not orders_raw:
            raise ValueError(f"Order {order_id} not found")
        return self._order_from_raw(orders_raw[0])

    def cancel_order_by_id(self, order_id: str) -> None:
        """Cancel an order — mirrors alpaca TradingClient.cancel_order_by_id()."""
        self._delete(f"/accounts/{self._account_id}/orders/{order_id}")

    def submit_order(self, request) -> _Order:
        """
        Submit a market, limit, or trailing-stop order.
        Accepts MarketOrderRequest, LimitOrderRequest, or TrailingStopOrderRequest.
        Returns an _Order with .id set.
        """
        side_raw = request.side.upper() if isinstance(request.side, str) else str(request.side.value).upper()
        # Map Alpaca side enum values
        if hasattr(request.side, "value"):
            side_raw = request.side.value.upper()

        # E*TRADE action codes
        if side_raw in ("BUY", "BUY_TO_COVER"):
            action = "BUY"
        elif side_raw == "SELL":
            action = "SELL"
        elif side_raw == "SELL_SHORT":
            action = "SELL_SHORT"
        else:
            # Infer from request type: cover shorts require BUY_TO_COVER
            action = side_raw

        tif_raw = (
            request.time_in_force.value.upper()
            if hasattr(request.time_in_force, "value")
            else str(request.time_in_force).upper()
        )
        tif_map = {"DAY": "GOOD_FOR_DAY", "GTC": "GOOD_TILL_CANCEL", "IOC": "IMMEDIATE_OR_CANCEL"}
        tif = tif_map.get(tif_raw, "GOOD_FOR_DAY")

        instrument = {
            "Product":      {"securityType": "EQ", "symbol": request.symbol},
            "orderAction":  action,
            "quantityType": "QUANTITY",
            "quantity":     int(request.qty),
        }

        if isinstance(request, MarketOrderRequest):
            order_type = "MARKET"
            price_type = {}
        elif isinstance(request, LimitOrderRequest):
            order_type = "LIMIT"
            price_type = {"limitPrice": float(request.limit_price)}
        elif isinstance(request, TrailingStopOrderRequest):
            order_type = "TRAILING_STOP_PRCT"
            price_type = {"stopPrice": float(request.trail_percent)}
        else:
            raise TypeError(f"Unsupported order request type: {type(request)}")

        payload = {
            "PlaceOrderRequest": {
                "allOrNone":   "false",
                "priceType":   order_type,
                "orderTerm":   tif,
                "marketSession": "REGULAR",
                "stopPrice":   "",
                "Instrument":  [instrument],
                **price_type,
            }
        }

        data = self._post(f"/accounts/{self._account_id}/orders/place", payload)
        placed = data.get("PlaceOrderResponse", {})
        order_id = str(placed.get("orderId", placed.get("OrderIds", [{}])[0].get("orderId", "0")))

        return _Order(
            id           = order_id,
            symbol       = request.symbol,
            side         = action.lower(),
            status       = "new",
            qty          = str(int(request.qty)),
            filled_qty   = "0",
            submitted_at = datetime.datetime.now(datetime.timezone.utc),
        )

    # ── Quotes ────────────────────────────────────────────────────────────

    def get_latest_quote(self, symbol: str) -> _Quote:
        """Return latest bid/ask — mirrors alpaca DataClient.get_latest_quote()."""
        data = self._get(
            "/market/quote/" + symbol,
            params={"detailFlag": "INTRADAY"},
        )
        q = data.get("QuoteResponse", {}).get("QuoteData", [{}])[0].get("All", {})
        return _Quote(
            bid_price = float(q.get("bid", q.get("lastTrade", 0))),
            ask_price = float(q.get("ask", q.get("lastTrade", 0))),
        )

    # ── Assets ────────────────────────────────────────────────────────────

    def get_asset(self, symbol: str) -> _Asset:
        """
        Check if a symbol is tradable — mirrors alpaca TradingClient.get_asset().
        E*TRADE has no direct asset-status endpoint so we probe a quote request.
        """
        try:
            self.get_latest_quote(symbol)
            return _Asset(symbol=symbol, status="active", tradable=True)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (400, 404):
                return _Asset(symbol=symbol, status="inactive", tradable=False)
            raise

    # ── Close position ────────────────────────────────────────────────────

    def close_position(self, symbol: str) -> _Order:
        """
        Market-close an open position (long or short).
        Mirrors alpaca TradingClient.close_position().
        """
        positions = {p.symbol: p for p in self.get_all_positions()}
        if symbol not in positions:
            raise ValueError(f"No open position for {symbol}")

        pos = positions[symbol]
        qty = abs(int(float(pos.qty)))
        is_short = float(pos.qty) < 0
        side = "BUY" if is_short else "SELL"    # cover short or sell long

        req = MarketOrderRequest(
            symbol        = symbol,
            qty           = qty,
            side          = side,
            time_in_force = "day",
        )
        return self.submit_order(req)
