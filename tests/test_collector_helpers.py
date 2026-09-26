"""Tests for sources.collector pure helper functions (no network)."""

from types import SimpleNamespace

import pytest

from sources.collector import (
    SearchSource,
    _candidate_score,
    _dedupe_urls,
    _discovery_queries,
    _extract_domain,
    _normalize_candidate_url,
    _ordered_contact_urls,
    _results_relevant,
)


class TestExtractDomain:
    def test_bare_host(self):
        assert _extract_domain("https://www.AcmeRealty.com/contact") == "acmerealty.com"

    def test_no_www(self):
        assert _extract_domain("https://acmerealty.com/x") == "acmerealty.com"

    def test_invalid_returns_none(self):
        assert _extract_domain("not a url") is None


class TestNormalizeCandidateUrl:
    def test_content_page_to_root(self):
        assert (
            _normalize_candidate_url("https://acme.com/blog/7-email-templates")
            == "https://acme.com/"
        )

    def test_homepage_unchanged(self):
        assert _normalize_candidate_url("https://acme.com/") == "https://acme.com/"

    def test_other_paths_unchanged(self):
        assert (
            _normalize_candidate_url("https://acme.com/branches/north/")
            == "https://acme.com/branches/north/"
        )


class TestCandidateScore:
    def test_scope_keyword_bonus(self, real_estate_niche):
        score = _candidate_score("https://acmerealty.com", "Real Estate Brokerage", real_estate_niche)
        assert score > 0

    def test_homepage_bonus(self, real_estate_niche):
        home = _candidate_score("https://acme.com/", "", real_estate_niche)
        deep = _candidate_score("https://acme.com/a/b/c/d/e", "", real_estate_niche)
        assert home > deep

    def test_junk_title_heavy_penalty(self, real_estate_niche):
        assert _candidate_score("https://acme.com", "Sign in to your account", real_estate_niche) < 0

    def test_denied_brand_min_score(self, real_estate_niche):
        assert _candidate_score("https://acme.com", "Microsoft Dynamics", real_estate_niche) == -100


class TestResultsRelevant:
    def test_relevant_results_pass(self, real_estate_niche):
        items = [
            {"url": "https://a.com", "title": "Real estate brokerage"},
            {"url": "https://b.com", "title": "Property developer"},
            {"url": "https://c.com", "title": "A football club"},
        ]
        assert _results_relevant(items, real_estate_niche)

    def test_off_topic_results_fail(self, real_estate_niche):
        items = [
            {"url": "https://a.com", "title": "Football club"},
            {"url": "https://b.com", "title": "Dictionary entry"},
        ]
        assert not _results_relevant(items, real_estate_niche)

    def test_no_niche_accepts(self, real_estate_niche):
        assert _results_relevant([], real_estate_niche) is True


class TestDedupeUrls:
    def test_dedupe_dicts_preserving_order(self):
        items = [
            {"url": "https://a.com", "title": "A"},
            {"url": "https://a.com", "title": "A dup"},
            {"url": "https://b.com", "title": "B"},
        ]
        out = _dedupe_urls(items)
        assert [i["url"] for i in out] == ["https://a.com", "https://b.com"]

    def test_dedupe_strings(self):
        assert _dedupe_urls(["https://a.com", "https://a.com"]) == ["https://a.com"]


class TestDiscoveryQueries:
    def test_small_target_uses_base_queries_only(self, real_estate_niche):
        assert _discovery_queries(real_estate_niche, 6, "us") == real_estate_niche.search_queries

    def test_large_target_expands_to_volume_budget(self, real_estate_niche):
        queries = _discovery_queries(real_estate_niche, 100, "us")
        assert len(queries) == 50
        assert queries[:4] == real_estate_niche.search_queries
        assert any('"New York NY"' in query for query in queries)
        assert len(set(queries)) == len(queries)

    def test_expansion_is_capped(self, real_estate_niche):
        assert len(_discovery_queries(real_estate_niche, 500, "us")) == 60


class TestOrderedContactUrls:
    def test_discovered_first_then_guessed(self):
        urls = _ordered_contact_urls(
            "https://acme.com", ["/about", "/contact-us"]
        )
        assert urls[0] == "https://acme.com/about"
        assert urls[1] == "https://acme.com/contact-us"
        assert "https://acme.com/contact" in urls

    def test_dedupes_and_skips_base(self):
        urls = _ordered_contact_urls("https://acme.com/", ["/about", "/", "/about"])
        assert urls.count("https://acme.com/about") == 1
        assert "https://acme.com/" not in urls

    def test_handles_relative_paths(self):
        urls = _ordered_contact_urls("https://acme.com/site/index.html", ["contact.html"])
        assert urls[0] == "https://acme.com/site/contact.html"

    def test_rejects_cross_site_contact_links(self):
        urls = _ordered_contact_urls("https://acme.com", ["//evil.test/contact", "https://evil.test/contact"])
        assert all("evil.test" not in url for url in urls)


def test_no_discovery_is_reported_as_failure(real_estate_niche):
    source = SearchSource.__new__(SearchSource)
    source.settings = SimpleNamespace(delay_between_requests=0)
    source._discover = lambda *_args, **_kwargs: []
    with pytest.raises(RuntimeError, match="No search results"):
        source.collect(real_estate_niche)


def test_free_search_is_used_before_serpapi(real_estate_niche):
    source = SearchSource.__new__(SearchSource)
    source.settings = SimpleNamespace(cache_search=False)
    source._load_cached = lambda _query: []
    source._save_cache = lambda _query, _urls: None

    class Engine:
        available = True

        def __init__(self, results):
            self.results = results
            self.calls = 0

        def search(self, *_args, **_kwargs):
            self.calls += 1
            return self.results

    free = Engine([{"url": "https://acmerealty.example", "title": "Acme real estate brokerage"}])
    paid = Engine([{"url": "https://paid.example", "title": "Paid real estate result"}])
    source.free_engines = [free]
    source.search = paid

    results = source._discover("real estate", niche=real_estate_niche)
    assert results[0]["url"] == "https://acmerealty.example"
    assert free.calls == 1
    assert paid.calls == 0


def test_serpapi_is_last_resort(real_estate_niche):
    source = SearchSource.__new__(SearchSource)
    source.settings = SimpleNamespace(cache_search=False)
    source._load_cached = lambda _query: []
    source._save_cache = lambda _query, _urls: None

    class Engine:
        available = True

        def __init__(self, results):
            self.results = results
            self.calls = 0

        def search(self, *_args, **_kwargs):
            self.calls += 1
            return self.results

    free = Engine([])
    paid = Engine([{"url": "https://acmerealty.example", "title": "Acme real estate brokerage"}])
    source.free_engines = [free]
    source.search = paid

    results = source._discover("real estate", niche=real_estate_niche)
    assert results[0]["url"] == "https://acmerealty.example"
    assert free.calls == 1
    assert paid.calls == 1


def test_js_shell_uses_rendered_text_for_scope(real_estate_niche):
    source = SearchSource.__new__(SearchSource)
    source.settings = SimpleNamespace(free_js_render=True, infer_emails=False)
    source.enrich = False

    class Response:
        url = "https://acme.test/"
        status_code = 200
        headers = {"Content-Type": "text/html"}
        text = "<html><head><title>Acme Realty</title></head><body>Enable JavaScript</body></html>"

    ctx = SimpleNamespace(
        session=SimpleNamespace(get=lambda _url: Response()),
        renderer=SimpleNamespace(render_free=lambda _url: "Acme is a real estate brokerage. Email hello@acme.test"),
    )
    contact, name, final_url = source._process_url("https://acme.test/", real_estate_niche, ctx=ctx)
    assert contact.emails == ["hello@acme.test"]
    assert name == "Acme Realty"
    assert final_url == "https://acme.test/"
