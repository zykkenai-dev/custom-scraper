"""SerpApi fallback budget and quota protection."""

from config.settings import ScraperSettings
from sources.search import SearchClient


class Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class Session:
    def __init__(self, remaining=250):
        self.remaining = remaining
        self.searches = 0

    def get(self, url, **_kwargs):
        if url.endswith("account.json"):
            return Response({"plan_searches_left": self.remaining})
        self.searches += 1
        return Response({
            "organic_results": [
                {"link": "https://acmerealty.example", "title": "Acme Realty"},
            ],
        })


def _client(*, reserve=25, max_per_run=4, remaining=250):
    settings = ScraperSettings(
        serpapi_key="test-key",
        serpapi_reserve=reserve,
        serpapi_max_per_run=max_per_run,
    )
    client = SearchClient(settings)
    client._session = Session(remaining)
    return client


def test_search_allowed_above_reserve():
    client = _client(remaining=26)
    assert client.search("real estate")
    assert client._session.searches == 1


def test_search_stops_at_monthly_reserve():
    client = _client(remaining=25)
    assert client.search("real estate") == []
    assert client._session.searches == 0


def test_search_stops_at_per_run_budget():
    client = _client(max_per_run=1)
    assert client.search("first")
    assert client.search("second") == []
    assert client._session.searches == 1


def test_failed_quota_check_preserves_credits():
    client = _client()
    client._session = Session()
    client._session.get = lambda *_args, **_kwargs: Response({}, 503)
    assert client.search("real estate") == []
