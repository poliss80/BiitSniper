"""
ApexTrader - Broker Factory
Selects the appropriate broker client.
Supports Alpaca (stocks + options) and E*TRADE (stocks only).
"""

import logging
import os

log = logging.getLogger("ApexTrader")


class BrokerFactory:
    """Factory for creating broker clients."""

    @staticmethod
    def create_stock_client(broker: str = "alpaca"):
        """
        Create a stock trading client.

        Args:
            broker: 'alpaca' or 'etrade'
        """
        broker = broker.lower()

        if broker == "alpaca":
            from engine.config import PAPER, ALPACA_BASE_URL, API_KEY, API_SECRET
            from alpaca.trading.client import TradingClient

            api_key    = API_KEY
            api_secret = API_SECRET
            paper      = PAPER
            base_url   = ALPACA_BASE_URL

            if not api_key or not api_secret:
                raise ValueError("Alpaca credentials not found in environment")

            log.debug(f"Using Alpaca for stock trading (paper={paper}, base_url={base_url})")
            return TradingClient(api_key, api_secret, paper=paper)

        elif broker == "etrade":
            from engine.broker.etrade_client import ETradeClient

            consumer_key    = os.getenv("ETRADE_CONSUMER_KEY")
            consumer_secret = os.getenv("ETRADE_CONSUMER_SECRET")
            account_id      = os.getenv("ETRADE_ACCOUNT_ID")
            sandbox         = os.getenv("ETRADE_SANDBOX", "false").lower() == "true"

            if not consumer_key or not consumer_secret or not account_id:
                raise ValueError("E*TRADE credentials not found in environment")

            log.debug("Using E*TRADE for stock trading")
            return ETradeClient(consumer_key, consumer_secret, account_id, sandbox)

        else:
            raise ValueError(f"Unknown broker: {broker}")

    @staticmethod
    def create_options_client():
        """
        Create an options trading client.
        Currently only Alpaca supports options.
        """
        from engine.config import PAPER, ALPACA_BASE_URL, API_KEY, API_SECRET
        from alpaca.trading.client import TradingClient

        api_key    = API_KEY
        api_secret = API_SECRET
        paper      = PAPER

        if not api_key or not api_secret:
            raise ValueError("Alpaca credentials not found in environment")

        log.info("Using Alpaca for options trading")
        return TradingClient(api_key, api_secret, paper=paper)

    @staticmethod
    def get_broker_type(client) -> str:
        """Determine broker type from client instance."""
        class_name = client.__class__.__name__

        if "Alpaca" in class_name or "TradingClient" in class_name:
            return "alpaca"
        elif "ETrade" in class_name:
            return "etrade"
        else:
            return "unknown"
