"""Schwab OAuth token manager and market data API client."""

import os
import json
import time
import base64
import requests
import logging
from typing import Dict, Optional
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("ApexTrader")

# OAuth endpoints
SCHWAB_OAUTH_URL = "https://api.schwabapi.com/v1/oauth/token"
SCHWAB_MARKET_DATA_URL = "https://api.schwabapi.com/marketdata/v1"

# Token cache file
TOKEN_CACHE_FILE = ".schwab_token_cache.json"


class SchwabOAuthClient:
    """Manages Schwab OAuth token refresh and API authentication."""
    
    def __init__(self, client_id: Optional[str] = None, client_secret: Optional[str] = None):
        self.client_id = client_id or os.environ.get("SCHWAB_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("SCHWAB_CLIENT_SECRET")
        
        if not self.client_id or not self.client_secret:
            raise ValueError("Schwab SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET required in .env")
        
        self.access_token: Optional[str] = None
        self.token_expiry: Optional[float] = None
        self._load_cached_token()
    
    def _load_cached_token(self) -> None:
        """Load token from cache file if it exists and is not expired."""
        if os.path.exists(TOKEN_CACHE_FILE):
            try:
                with open(TOKEN_CACHE_FILE, "r") as f:
                    cache = json.load(f)
                    self.access_token = cache.get("access_token")
                    self.token_expiry = cache.get("token_expiry")
                    
                    # Check if token is still valid
                    if self.token_expiry and time.time() < self.token_expiry:
                        log.debug("Schwab: Loaded cached access token")
                        return
            except Exception as e:
                log.debug(f"Schwab: Failed to load cached token: {e}")
        
        self.access_token = None
        self.token_expiry = None
    
    def _save_cached_token(self) -> None:
        """Save token to cache file."""
        try:
            with open(TOKEN_CACHE_FILE, "w") as f:
                json.dump({
                    "access_token": self.access_token,
                    "token_expiry": self.token_expiry
                }, f)
        except Exception as e:
            log.debug(f"Schwab: Failed to cache token: {e}")
    
    def get_access_token(self) -> str:
        """Get valid access token, refreshing if needed."""
        # Check if current token is valid
        if self.access_token and self.token_expiry and time.time() < self.token_expiry - 60:
            return self.access_token
        
        # Need to refresh
        log.info("Schwab: Refreshing access token...")
        self._refresh_token()
        return self.access_token
    
    def _refresh_token(self) -> None:
        """Request new access token using client credentials (OAuth2 Client Credentials flow)."""
        try:
            # Base64 encode credentials
            credentials = f"{self.client_id}:{self.client_secret}"
            encoded = base64.b64encode(credentials.encode()).decode()
            
            response = requests.post(
                SCHWAB_OAUTH_URL,
                headers={
                    "Authorization": f"Basic {encoded}",
                    "Content-Type": "application/x-www-form-urlencoded"
                },
                data={"grant_type": "client_credentials"},
                timeout=10
            )
            response.raise_for_status()
            
            data = response.json()
            self.access_token = data.get("access_token")
            expires_in = int(data.get("expires_in", 1800))  # Default 30 minutes, ensure int
            self.token_expiry = time.time() + expires_in
            
            self._save_cached_token()
            log.info(f"Schwab: Access token refreshed (expires in {expires_in}s)")
        
        except requests.exceptions.RequestException as e:
            log.error(f"Schwab: OAuth token refresh failed: {e}")
            raise
    
    def get_headers(self) -> Dict[str, str]:
        """Get headers for API requests with authorization."""
        token = self.get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }


class SchwabMarketDataClient:
    """Schwab market data API client for quotes, candles, options chains."""
    
    def __init__(self, oauth_client: Optional[SchwabOAuthClient] = None):
        if oauth_client is None:
            oauth_client = SchwabOAuthClient()
        self.oauth = oauth_client
        self.session = requests.Session()
        
        # Configure connection pooling: increase pool size from default 10 to 25.
        # Do NOT put 502/503/504 in the urllib3 status_forcelist — the manual retry
        # loops in get_option_chains / get_candles already handle those codes with
        # meaningful backoff.  Having both layers active multiplies the attempt count
        # by up to 4×, causing the bot to block for 60–100 seconds per symbol.
        adapter = HTTPAdapter(
            pool_connections=25,   # Max connections to pool per host
            pool_maxsize=25,       # Max number of connections to save in pool
            max_retries=Retry(
                total=2,               # Only retry on connection-level failures (not HTTP 5xx)
                backoff_factor=1.0,
                status_forcelist=[],   # 502/503/504 handled by the manual loops below
                raise_on_status=False  # Don't raise on 4xx (auth/validation errors)
            )
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
    
    def get_quote(self, symbol: str) -> Optional[Dict]:
        """Get current quote for symbol."""
        try:
            url = f"{SCHWAB_MARKET_DATA_URL}/quotes/{symbol}"
            response = self.session.get(
                url,
                headers=self.oauth.get_headers(),
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            # Schwab can return 404 for valid/newly-listed symbols. Use Alpaca
            # market data as a quote fallback so one provider does not suppress
            # squeeze confirmation for the entire symbol.
            try:
                from alpaca.data.historical import StockHistoricalDataClient
                from alpaca.data.requests import StockLatestQuoteRequest
                from engine.config import API_KEY, API_SECRET

                alpaca_client = StockHistoricalDataClient(API_KEY, API_SECRET)
                quotes = alpaca_client.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=symbol)
                )
                quote = quotes[symbol]
                log.info(f"Schwab: quote unavailable for {symbol}; Alpaca fallback used")
                return {
                    symbol: {
                        "bidPrice": float(getattr(quote, "bid_price", 0) or 0),
                        "askPrice": float(getattr(quote, "ask_price", 0) or 0),
                        "lastPrice": float(getattr(quote, "ask_price", 0) or getattr(quote, "bid_price", 0) or 0),
                        "source": "alpaca_fallback",
                    }
                }
            except Exception as fallback_error:
                log.warning(
                    f"Schwab: Failed to get quote for {symbol}: {e}; "
                    f"Alpaca fallback failed: {fallback_error}"
                )
                return None
    
    def get_candles(self, symbol: str, period_type: str = "day", period: int = 5, 
                   frequency_type: str = "minute", frequency: int = 15,
                   start_ms: int = None, end_ms: int = None) -> Optional[Dict]:
        """
        Get candles (OHLCV bars) for symbol with exponential backoff on transient failures.
        
        Args:
            symbol: Stock symbol
            period_type: "day", "month", "year", "ytd" (ignored when start_ms/end_ms given)
            period: Number of periods to fetch (ignored when start_ms/end_ms given)
            frequency_type: "minute", "daily", "weekly", "monthly"
            frequency: 1, 5, 10, 15, 30 for minute; 1 for daily/weekly/monthly
            start_ms: Optional start of range, epoch milliseconds. When provided
                (with end_ms), a date range is sent instead of periodType/period —
                required for multi-day minute-frequency history (Schwab rejects
                periodType=day&period>1 with frequencyType=minute&frequency=1).
            end_ms: Optional end of range, epoch milliseconds.
        """
        max_retries = 3
        backoff_times = [3.0, 8.0, 15.0]  # Longer waits so the API has time to recover
        timeout_values = [15, 20, 25]      # Shorter first timeout — 502s return quickly
        
        for attempt in range(max_retries):
            try:
                if start_ms is not None and end_ms is not None:
                    # Date-range mode: do NOT send periodType/period — including both
                    # causes HTTP 400.
                    params = {
                        "frequencyType": frequency_type,
                        "frequency": frequency,
                        "symbol": symbol,
                        "startDate": start_ms,
                        "endDate": end_ms,
                    }
                else:
                    params = {
                        "periodType": period_type,
                        "period": period,
                        "frequencyType": frequency_type,
                        "frequency": frequency,
                        "symbol": symbol
                    }
                url = f"{SCHWAB_MARKET_DATA_URL}/pricehistory"
                
                response = self.session.get(
                    url,
                    headers=self.oauth.get_headers(),
                    params=params,
                    timeout=timeout_values[attempt]
                )
                
                # 502, 503, 504 are transient — retry with backoff
                if response.status_code in (502, 503, 504):
                    if attempt < max_retries - 1:
                        wait_time = backoff_times[attempt]
                        log.debug(
                            f"Schwab: {symbol} candles returned {response.status_code} "
                            f"— retrying in {wait_time:.1f}s"
                        )
                        time.sleep(wait_time)
                        continue
                    else:
                        log.warning(f"Schwab: {symbol} candles failed after {max_retries} attempts")
                        return None
                
                response.raise_for_status()
                return response.json()
            
            except requests.exceptions.HTTPError as e:
                # 4xx client errors (bad request, forbidden, not found) are not transient
                # Skip them without retrying
                if 400 <= e.response.status_code < 500:
                    log.debug(f"Schwab: {symbol} candles — {e.response.status_code} (skipping, not retryable)")
                    return None
                # Other errors (5xx, connection) might be transient — retry if not last attempt
                if attempt < max_retries - 1:
                    wait_time = backoff_times[attempt]
                    log.debug(f"Schwab: {symbol} candles retry in {wait_time:.1f}s")
                    time.sleep(wait_time)
                else:
                    log.warning(f"Schwab: Failed to get candles for {symbol}: {e}")
                    return None
            
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    wait_time = backoff_times[attempt]
                    log.debug(f"Schwab: {symbol} candles retry in {wait_time:.1f}s")
                    time.sleep(wait_time)
                else:
                    log.warning(f"Schwab: Failed to get candles for {symbol}: {e}")
                    return None
    
    def get_option_chains(self, symbol: str, contract_type: str = "ALL") -> Optional[Dict]:
        """
        Get options chains for symbol with exponential backoff retry on transient failures.
        
        Args:
            symbol: Stock symbol
            contract_type: "CALL", "PUT", "ALL"
        
        Returns:
            Options chain data or None on persistent failure
        """
        max_retries = 3
        backoff_times = [3.0, 8.0, 15.0]  # Longer waits so the API has time to recover
        timeout_values = [15, 20, 25]      # Shorter first timeout — 502s return quickly
        
        for attempt in range(max_retries):
            try:
                params = {
                    "symbol": symbol,
                    "contractType": contract_type
                }
                url = f"{SCHWAB_MARKET_DATA_URL}/chains"
                
                response = self.session.get(
                    url,
                    headers=self.oauth.get_headers(),
                    params=params,
                    timeout=timeout_values[attempt]
                )
                
                # 502, 503, 504 are transient — retry with backoff
                if response.status_code in (502, 503, 504):
                    if attempt < max_retries - 1:
                        wait_time = backoff_times[attempt]
                        log.warning(
                            f"Schwab: {symbol} chain returned {response.status_code} "
                            f"— retrying in {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})"
                        )
                        time.sleep(wait_time)
                        continue
                    else:
                        log.warning(
                            f"Schwab: {symbol} chain failed with {response.status_code} "
                            f"after {max_retries} attempts — giving up"
                        )
                        return None
                
                # All other status codes: use standard raise_for_status
                response.raise_for_status()
                return response.json()
            
            except requests.exceptions.HTTPError as e:
                # 4xx client errors (bad request, forbidden, not found) are not transient
                # Skip them without retrying
                if 400 <= e.response.status_code < 500:
                    log.debug(f"Schwab: {symbol} chain — {e.response.status_code} (skipping, not retryable)")
                    return None
                # Other errors (5xx, connection) might be transient — retry if not last attempt
                if attempt < max_retries - 1:
                    wait_time = backoff_times[attempt]
                    log.warning(
                        f"Schwab: {symbol} chain request error: {e} "
                        f"— retrying in {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(wait_time)
                else:
                    log.warning(f"Schwab: Failed to get options chains for {symbol}: {e}")
                    return None
            
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    wait_time = backoff_times[attempt]
                    log.warning(
                        f"Schwab: {symbol} chain request error: {e} "
                        f"— retrying in {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(wait_time)
                else:
                    log.warning(f"Schwab: Failed to get options chains for {symbol}: {e}")
                    return None


# Singleton instances
_oauth_client: Optional[SchwabOAuthClient] = None
_market_data_client: Optional[SchwabMarketDataClient] = None


def get_schwab_oauth_client() -> SchwabOAuthClient:
    """Get or create Schwab OAuth client singleton."""
    global _oauth_client
    if _oauth_client is None:
        _oauth_client = SchwabOAuthClient()
    return _oauth_client


def get_schwab_market_data_client() -> SchwabMarketDataClient:
    """Get or create Schwab market data client singleton."""
    global _market_data_client
    if _market_data_client is None:
        oauth = get_schwab_oauth_client()
        _market_data_client = SchwabMarketDataClient(oauth)
    return _market_data_client
