import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from engine.data import alpaca_news


class AlpacaNewsTests(unittest.TestCase):
    def test_positive_catalyst_is_scored_and_cached(self):
        published = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)
        article = SimpleNamespace(
            headline="Company announces major contract win",
            summary="New customer agreement expands revenue.",
            created_at=published,
            updated_at=published,
        )
        response = SimpleNamespace(data={"TEST": [article]})
        client = SimpleNamespace(get_news=lambda request: response)

        alpaca_news._CACHE.clear()
        with patch.object(alpaca_news, "API_KEY", "key"), patch.object(
            alpaca_news, "API_SECRET", "secret"
        ), patch("alpaca.data.historical.news.NewsClient", return_value=client):
            result = alpaca_news.analyze_symbol_news("TEST")

        self.assertEqual(result.direction, "positive")
        self.assertEqual(result.catalyst, "contract_win")
        self.assertGreater(result.score, 0)
        self.assertEqual(result.article_count, 1)

    def test_dilution_news_is_negative(self):
        published = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
        article = SimpleNamespace(
            headline="Company announces direct offering",
            summary="",
            created_at=published,
            updated_at=published,
        )
        response = SimpleNamespace(data={"TEST": [article]})
        client = SimpleNamespace(get_news=lambda request: response)

        alpaca_news._CACHE.clear()
        with patch.object(alpaca_news, "API_KEY", "key"), patch.object(
            alpaca_news, "API_SECRET", "secret"
        ), patch("alpaca.data.historical.news.NewsClient", return_value=client):
            result = alpaca_news.analyze_symbol_news("TEST")

        self.assertEqual(result.direction, "negative")
        self.assertIn("dilution", result.risk_flags)
        self.assertLess(result.score, 0)


if __name__ == "__main__":
    unittest.main()
