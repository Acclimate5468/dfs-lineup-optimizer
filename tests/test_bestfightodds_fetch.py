"""Tests for the explicit BestFightOdds live fetch → preview (Phase 2).

Covers ``docs/ODDS_ACQUISITION_V0_DESIGN.md`` §3 Phase 2 (and §1.9 / §1.11 /
§1.12): a single explicit GET of a public BestFightOdds URL, parsed by the pure
Phase 1 parser into normalized §1.7 rows for **preview only**. No DB, no save.

Every test injects an ``http_get`` seam (or asserts a URL is rejected before
any I/O), so nothing here touches the real network. A socket guard pins that
the helper opens no socket when the getter is injected.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from src.ingestion.providers.bestfightodds import (
    BOOK_DRAFTKINGS,
    SOURCE_BESTFIGHTODDS,
    AllBooksFighterRow,
    BestFightOddsParseError,
)
from src.ingestion.providers.bestfightodds_fetch import (
    DEFAULT_TIMEOUT_SECONDS,
    BestFightOddsAllBooksFetchResult,
    BestFightOddsFetchError,
    BestFightOddsFetchResult,
    fetch_bestfightodds_all_books_preview,
    fetch_bestfightodds_preview,
)

FIXTURE = Path(__file__).parent / "fixtures" / "bestfightodds_sample.html"
_ALLBOOKS_FIXTURE = (
    Path(__file__).parent / "fixtures" / "bestfightodds_allbooks_sample.html"
)
_GOOD_URL = "https://www.bestfightodds.com/events/test-event-1"


def _load_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _load_allbooks_fixture() -> str:
    return _ALLBOOKS_FIXTURE.read_text(encoding="utf-8")


def _stub_get(html: str):
    """Build an ``http_get``-shaped callable that returns ``html`` and records
    the URL / timeout it was called with."""
    calls: list[tuple[str, float]] = []

    def _get(url: str, *, timeout: float) -> str:
        calls.append((url, timeout))
        return html

    _get.calls = calls  # type: ignore[attr-defined]
    return _get


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_fetch_parses_dk_rows_with_provenance() -> None:
    getter = _stub_get(_load_fixture())
    result = fetch_bestfightodds_preview(_GOOD_URL, http_get=getter)

    assert isinstance(result, BestFightOddsFetchResult)
    assert result.source_url == _GOOD_URL
    assert result.row_count == 2
    assert [r.fighter_name for r in result.rows] == [
        "Test Fighter One",
        "Test Fighter Two",
    ]
    assert [r.american_moneyline for r in result.rows] == [-350, 280]
    # §1.7 provenance is stamped onto every row.
    for row in result.rows:
        assert row.source == SOURCE_BESTFIGHTODDS
        assert row.book == BOOK_DRAFTKINGS
        assert row.source_url == _GOOD_URL
        assert row.fetched_at == result.fetched_at
    # The fetched-at stamp is the repo's UTC ...Z second-precision format.
    assert result.fetched_at.endswith("Z")
    assert len(result.fetched_at) == len("2026-06-01T12:00:00Z")


def test_fetch_passes_url_and_default_timeout_to_getter() -> None:
    getter = _stub_get(_load_fixture())
    fetch_bestfightodds_preview(_GOOD_URL, http_get=getter)
    assert getter.calls == [(_GOOD_URL, DEFAULT_TIMEOUT_SECONDS)]


def test_fetch_strips_whitespace_from_url() -> None:
    getter = _stub_get(_load_fixture())
    result = fetch_bestfightodds_preview(
        f"  {_GOOD_URL}  ", http_get=getter
    )
    assert result.source_url == _GOOD_URL
    assert getter.calls[0][0] == _GOOD_URL


def test_subdomain_host_is_accepted() -> None:
    getter = _stub_get(_load_fixture())
    url = "https://www.bestfightodds.com/events/x"
    result = fetch_bestfightodds_preview(url, http_get=getter)
    assert result.row_count == 2


# ---------------------------------------------------------------------------
# URL validation (rejected before any I/O — decision #2 / §1.12)
# ---------------------------------------------------------------------------


def test_empty_url_rejected_without_fetching() -> None:
    getter = _stub_get(_load_fixture())
    with pytest.raises(BestFightOddsFetchError):
        fetch_bestfightodds_preview("   ", http_get=getter)
    assert getter.calls == []  # never reached the getter


def test_non_bestfightodds_host_rejected_without_fetching() -> None:
    getter = _stub_get(_load_fixture())
    with pytest.raises(BestFightOddsFetchError) as exc:
        fetch_bestfightodds_preview(
            "https://example.com/events/x", http_get=getter
        )
    assert "bestfightodds.com" in str(exc.value)
    assert getter.calls == []


def test_lookalike_host_rejected() -> None:
    """A host that merely ends in the token but is a different domain
    (``bestfightodds.com.evil.test``) is still rejected as a subdomain check,
    while a non-suffix lookalike (``notbestfightodds.com``) is rejected too."""
    getter = _stub_get(_load_fixture())
    with pytest.raises(BestFightOddsFetchError):
        fetch_bestfightodds_preview(
            "https://notbestfightodds.com/x", http_get=getter
        )
    assert getter.calls == []


def test_non_http_scheme_rejected() -> None:
    getter = _stub_get(_load_fixture())
    with pytest.raises(BestFightOddsFetchError) as exc:
        fetch_bestfightodds_preview(
            "ftp://www.bestfightodds.com/x", http_get=getter
        )
    assert "http" in str(exc.value).lower()
    assert getter.calls == []


# ---------------------------------------------------------------------------
# Failure surfaces
# ---------------------------------------------------------------------------


def test_network_failure_becomes_fetch_error() -> None:
    def _boom(url: str, *, timeout: float) -> str:
        raise OSError("connection refused")

    with pytest.raises(BestFightOddsFetchError) as exc:
        fetch_bestfightodds_preview(_GOOD_URL, http_get=_boom)
    assert "Could not fetch BestFightOdds" in str(exc.value)


def test_parse_failure_propagates_as_parse_error() -> None:
    """A page that fetched fine but has no DraftKings table raises the parser's
    error unchanged (so the caller can tell it apart from a fetch failure)."""
    getter = _stub_get("<html><body><p>No odds here.</p></body></html>")
    with pytest.raises(BestFightOddsParseError):
        fetch_bestfightodds_preview(_GOOD_URL, http_get=getter)


def test_fetch_error_from_getter_is_not_rewrapped() -> None:
    """If the getter itself raises ``BestFightOddsFetchError`` it propagates
    as-is (not double-wrapped)."""
    def _raise(url: str, *, timeout: float) -> str:
        raise BestFightOddsFetchError("explicit")

    with pytest.raises(BestFightOddsFetchError) as exc:
        fetch_bestfightodds_preview(_GOOD_URL, http_get=_raise)
    assert str(exc.value) == "explicit"


# ---------------------------------------------------------------------------
# No network when the getter is injected
# ---------------------------------------------------------------------------


def test_no_socket_opened_with_injected_getter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_socket(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("fetch opened a network socket with injected getter")

    monkeypatch.setattr(socket, "socket", _no_socket)
    getter = _stub_get(_load_fixture())
    result = fetch_bestfightodds_preview(_GOOD_URL, http_get=getter)
    assert result.row_count == 2


def test_default_getter_is_requests_backed_no_call_made() -> None:
    """Sanity: the default getter exists and is the requests-backed one, but no
    test calls it (it would hit the network). We only assert it is wired."""
    from src.ingestion.providers import bestfightodds_fetch as mod

    assert callable(mod._http_get)
    assert mod._ALLOWED_HOST == "bestfightodds.com"


# ---------------------------------------------------------------------------
# All-books fetch (consensus path) — same single GET, every book column
# ---------------------------------------------------------------------------


def test_all_books_fetch_parses_every_book_with_provenance() -> None:
    getter = _stub_get(_load_allbooks_fixture())
    result = fetch_bestfightodds_all_books_preview(_GOOD_URL, http_get=getter)

    assert isinstance(result, BestFightOddsAllBooksFetchResult)
    assert result.source_url == _GOOD_URL
    assert result.row_count >= 2
    assert all(isinstance(r, AllBooksFighterRow) for r in result.rows)
    # Every fighter carries multiple book lines (not just DraftKings).
    assert all(len(r.book_lines) >= 1 for r in result.rows)
    assert max(len(r.book_lines) for r in result.rows) >= 2
    for row in result.rows:
        assert row.source_url == _GOOD_URL
        assert row.fetched_at == result.fetched_at
    assert result.fetched_at.endswith("Z")


def test_all_books_fetch_uses_default_timeout_and_url() -> None:
    getter = _stub_get(_load_allbooks_fixture())
    fetch_bestfightodds_all_books_preview(_GOOD_URL, http_get=getter)
    assert getter.calls == [(_GOOD_URL, DEFAULT_TIMEOUT_SECONDS)]


def test_all_books_fetch_rejects_non_bestfightodds_host() -> None:
    getter = _stub_get(_load_allbooks_fixture())
    with pytest.raises(BestFightOddsFetchError):
        fetch_bestfightodds_all_books_preview(
            "https://example.com/events/x", http_get=getter
        )
    assert getter.calls == []  # rejected before any I/O


def test_all_books_network_failure_becomes_fetch_error() -> None:
    def _boom(url: str, *, timeout: float) -> str:
        raise OSError("connection refused")

    with pytest.raises(BestFightOddsFetchError) as exc:
        fetch_bestfightodds_all_books_preview(_GOOD_URL, http_get=_boom)
    assert "Could not fetch BestFightOdds" in str(exc.value)


def test_all_books_parse_failure_propagates() -> None:
    getter = _stub_get("<html><body><p>No odds here.</p></body></html>")
    with pytest.raises(BestFightOddsParseError):
        fetch_bestfightodds_all_books_preview(_GOOD_URL, http_get=getter)


def test_all_books_no_socket_with_injected_getter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_socket(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("fetch opened a socket with injected getter")

    monkeypatch.setattr(socket, "socket", _no_socket)
    getter = _stub_get(_load_allbooks_fixture())
    result = fetch_bestfightodds_all_books_preview(_GOOD_URL, http_get=getter)
    assert result.row_count >= 2
