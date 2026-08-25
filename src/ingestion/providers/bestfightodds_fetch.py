"""Explicit live BestFightOdds fetch → preview (Odds Acquisition v0 Phase 2).

Realizes ``docs/ODDS_ACQUISITION_V0_DESIGN.md`` §3 Phase 2 (and decisions
§1.9 / §1.11 / §1.12): a single, **explicit, user-triggered** HTTP GET of a
public BestFightOdds event page, parsed by the pure Phase 1 parser
(:mod:`src.ingestion.providers.bestfightodds`) into the normalized §1.7
moneyline rows, returned for **preview only**.

Hard boundaries (design §1.9 / §1.11 / §1.12; ``docs/DEVELOPMENT_NOTES.md`` §3):

  - **Fetch is separate from save.** This module performs *no* DB write, no
    match recompute, and no UI work. Saving previewed rows is Phase 3.
  - **Explicit trigger only.** Nothing here runs on import; a caller invokes
    :func:`fetch_bestfightodds_preview` from a button handler only. There is
    no background job, no page-load fetch, no retry loop, no crawl.
  - **One Green source.** Only ``bestfightodds.com`` hosts are fetched
    (decision #2); any other host is rejected before a connection is opened.
  - **Plain static HTML, public URL.** A single GET with a normal timeout and
    a polite identifying User-Agent. No login, no cookies, no CAPTCHA / proxy
    / paywall handling, no headless browser, no premium / keyed scraping.

The parser stays pure (it imports no network library); this module owns the
only network I/O in the acquisition path. Fetch failures raise
:class:`BestFightOddsFetchError`; a successful fetch whose HTML cannot be
parsed re-raises the parser's
:class:`~src.ingestion.providers.bestfightodds.BestFightOddsParseError`
unchanged, so a caller can tell "could not reach the page" apart from
"reached it, but it had no DraftKings odds".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from src.ingestion.providers.bestfightodds import (
    AcquiredMoneylineRow,
    AllBooksFighterRow,
    parse_bestfightodds_all_books,
    parse_bestfightodds_html,
)

# A normal, non-aggressive timeout for one on-demand GET (design §1.12 —
# low-volume, explicit). Connect + read share the same budget here.
DEFAULT_TIMEOUT_SECONDS = 15.0

# Polite, identifying User-Agent. v0 fetches public static HTML only; this is
# not an attempt to evade detection (the opposite — it identifies the tool).
_USER_AGENT = "DraftKingsLineupLab/0.1 (UFC research; +local single-user)"

# Decision #2 / §1.12: the only Green public fetcher is BestFightOdds. A host
# must be ``bestfightodds.com`` or a subdomain of it; anything else is refused
# before any connection is opened (no second fetcher, no open redirect target).
_ALLOWED_HOST = "bestfightodds.com"


class BestFightOddsFetchError(RuntimeError):
    """Raised when the live BestFightOdds fetch itself fails.

    Distinct from
    :class:`~src.ingestion.providers.bestfightodds.BestFightOddsParseError`
    (which means the page was fetched but had no parseable DraftKings odds):
    this covers a rejected URL, a network error, a timeout, or a non-2xx HTTP
    response — the page never produced usable HTML.
    """


@dataclass(frozen=True)
class BestFightOddsFetchResult:
    """The outcome of one explicit live fetch + parse (preview only).

    ``rows`` are the normalized §1.7 moneyline rows the Phase 1 parser produced
    (each already carrying ``source_url`` / ``fetched_at`` provenance).
    ``source_url`` and ``fetched_at`` are restated here for the caller's
    convenience; ``row_count`` is ``len(rows)``. Nothing in this result has
    been persisted.
    """

    rows: list[AcquiredMoneylineRow]
    source_url: str
    fetched_at: str

    @property
    def row_count(self) -> int:
        return len(self.rows)


def _utc_now_z() -> str:
    """Current UTC time as the repo's ``...Z`` second-precision stamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_bestfightodds_url(url: str) -> str:
    """Validate + return a public BestFightOdds ``http(s)`` URL.

    Rejects (raising :class:`BestFightOddsFetchError`) anything that is not an
    ``http`` / ``https`` URL whose host is ``bestfightodds.com`` or a subdomain
    of it. This keeps Phase 2 to the single approved Green source (decision #2)
    and prevents the fetch button from being pointed at an arbitrary host.
    """
    cleaned = (url or "").strip()
    if not cleaned:
        raise BestFightOddsFetchError("Enter a BestFightOdds event URL to fetch.")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise BestFightOddsFetchError(
            "BestFightOdds URL must start with http:// or https:// "
            f"(got {parsed.scheme or 'no scheme'!r})."
        )
    host = (parsed.hostname or "").lower()
    if not (host == _ALLOWED_HOST or host.endswith("." + _ALLOWED_HOST)):
        raise BestFightOddsFetchError(
            "Phase 2 fetches BestFightOdds only — the URL host must be "
            f"{_ALLOWED_HOST} (got {host or 'no host'!r})."
        )
    return cleaned


def _http_get(url: str, *, timeout: float) -> str:
    """Perform one public GET and return the response body text.

    A single ``requests.get`` with a polite User-Agent and a normal timeout;
    a non-2xx response raises. No cookies, no auth, no redirects beyond what a
    plain GET follows, no retries.
    """
    response = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT},
    )
    response.raise_for_status()
    return response.text


def fetch_bestfightodds_preview(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    http_get=None,
) -> BestFightOddsFetchResult:
    """Fetch a public BestFightOdds event page and parse it for **preview**.

    Validates ``url`` as a public BestFightOdds ``http(s)`` URL (decision #2),
    performs **one** explicit GET, and feeds the HTML to the pure Phase 1
    parser, stamping every row with ``source_url`` and the UTC ``fetched_at``.
    Returns a :class:`BestFightOddsFetchResult`; **nothing is saved** (Phase 3).

    ``http_get`` is an injection seam for tests (a callable
    ``(url, *, timeout) -> str``); production uses :func:`_http_get`. It keeps
    the unit tests off the network entirely.

    Raises:
        BestFightOddsFetchError: the URL was rejected, or the GET failed
            (network error, timeout, or non-2xx response) — no usable HTML.
        BestFightOddsParseError: the page was fetched but exposed no parseable
            DraftKings moneyline rows (re-raised from the parser unchanged).
    """
    cleaned = _validate_bestfightodds_url(url)
    getter = http_get if http_get is not None else _http_get
    fetched_at = _utc_now_z()
    try:
        html = getter(cleaned, timeout=timeout)
    except BestFightOddsFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface any network failure uniformly
        raise BestFightOddsFetchError(
            f"Could not fetch BestFightOdds page: {exc}"
        ) from exc

    # Parser errors (BestFightOddsParseError) intentionally propagate unchanged
    # so the caller can distinguish "fetched but unparseable" from "fetch
    # failed". source_url / fetched_at are recorded on every row as §1.7
    # provenance.
    rows = parse_bestfightodds_html(
        html, source_url=cleaned, fetched_at=fetched_at
    )
    return BestFightOddsFetchResult(
        rows=rows, source_url=cleaned, fetched_at=fetched_at
    )


@dataclass(frozen=True)
class BestFightOddsAllBooksFetchResult:
    """The outcome of one explicit all-books fetch + parse (consensus preview).

    ``rows`` are the per-fighter all-book lines the consensus path blends
    (:func:`~src.ingestion.providers.bestfightodds.parse_bestfightodds_all_books`),
    each carrying ``source_url`` / ``fetched_at`` provenance. The consensus
    sibling of :class:`BestFightOddsFetchResult` (which keeps only the
    DraftKings column). Nothing here is persisted.
    """

    rows: list[AllBooksFighterRow]
    source_url: str
    fetched_at: str

    @property
    def row_count(self) -> int:
        return len(self.rows)


def fetch_bestfightodds_all_books_preview(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    http_get=None,
) -> BestFightOddsAllBooksFetchResult:
    """Fetch a public BestFightOdds event page and parse **every** book column.

    The multi-book consensus sibling of :func:`fetch_bestfightodds_preview`:
    the same single explicit GET, the same host/scheme guard and ``http_get``
    test seam, but it runs
    :func:`~src.ingestion.providers.bestfightodds.parse_bestfightodds_all_books`
    so the consensus blend sees every book the page renders (not just
    DraftKings). Returns a :class:`BestFightOddsAllBooksFetchResult`; **nothing
    is saved** (persistence is ``consensus_save.save_consensus_to_slate``).

    Raises:
        BestFightOddsFetchError: the URL was rejected, or the GET failed
            (network error, timeout, or non-2xx response).
        BestFightOddsParseError: the page was fetched but exposed no parseable
            book columns (re-raised from the parser unchanged).
    """
    cleaned = _validate_bestfightodds_url(url)
    getter = http_get if http_get is not None else _http_get
    fetched_at = _utc_now_z()
    try:
        html = getter(cleaned, timeout=timeout)
    except BestFightOddsFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface any network failure uniformly
        raise BestFightOddsFetchError(
            f"Could not fetch BestFightOdds page: {exc}"
        ) from exc

    rows = parse_bestfightodds_all_books(
        html, source_url=cleaned, fetched_at=fetched_at
    )
    return BestFightOddsAllBooksFetchResult(
        rows=rows, source_url=cleaned, fetched_at=fetched_at
    )
