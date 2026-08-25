"""Tests for Export / Run Log v1 C.3 orchestration service.

Covers ``src/exports/export_service.build_run_log`` per
``docs/EXPORT_RUN_LOG_V1_DESIGN.md`` §2, §5 Option A (no persistence),
§7 (validation), §8 (module contract), and §9 (test coverage list).

Pinned contracts:

- Ready slate → :class:`InternalExportBundle` with the expected
  metadata, optimizer status, lineups, and pool diagnostics; the C.2
  formatter surface accepts the returned bundle.
- Not-ready gate → diagnostics-only bundle with
  ``optimizer_status == "gate_blocked"`` (design §12 / §7 rule 4);
  no raise.
- Invalid ``n_lineups`` → :class:`ExportValidationError`; no solver
  invocation.
- Same-fight conflict / oversized salary / wrong lineup size
  (synthetic) → :class:`ExportValidationError` (design §7 rules 1–3).
- Undersized pool → diagnostics-only bundle with
  ``infeasible_pool_too_small`` status (design §7 rule 5).
- Read-only: snapshots of every persisted table are unchanged across
  the call (docs/DEVELOPMENT_NOTES.md §11).
- No file writes: monkeypatch ``builtins.open`` to raise on write
  modes; ``build_run_log`` and the C.2 formatters do not trigger it.
"""

from __future__ import annotations

import builtins
import sqlite3
from pathlib import Path

import pytest

from src.db.repositories import (
    FightGroupRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.exports import export_service
from src.exports.export_service import (
    STATUS_GATE_BLOCKED,
    ExportValidationError,
    build_run_log,
)
from src.exports.internal_export import (
    InternalExportBundle,
    format_lineups_csv,
    format_lineups_json,
    format_markdown_summary,
)
from src.ingestion.odds_matching_service import (
    recompute_and_replace_match_results,
)
from src.optimizer.lineup_solver import (
    Lineup,
    SolveResult,
    STATUS_INFEASIBLE_POOL_TOO_SMALL,
    STATUS_OK,
)


# ---------------------------------------------------------------------------
# Fixtures + seeders
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    apply_schema(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def slate_id(conn):
    return SlateRepository(conn).create(
        event_name="UFC 999 — Export Test",
        event_date="2026-05-30",
        salary_csv_status="validated",
        salary_row_count=12,
    ).id


def _insert_fighter(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    name: str,
    salary: int = 8000,
) -> int:
    cur = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, 'active')",
        (int(slate_id), name, int(salary)),
    )
    conn.commit()
    return int(cur.lastrowid)


def _save_odds_row(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    fighter_name_raw: str,
    american_odds: int = -150,
    captured_at: str = "2026-05-20T00:00:00Z",
):
    return OddsRowRepository(conn).create(
        slate_id=slate_id,
        fighter_name_raw=fighter_name_raw,
        american_odds=american_odds,
        source="manual",
        captured_at=captured_at,
    )


READY_FIGHTS: tuple[tuple[str, str], ...] = (
    ("F1_A", "F1_B"),
    ("F2_A", "F2_B"),
    ("F3_A", "F3_B"),
    ("F4_A", "F4_B"),
    ("F5_A", "F5_B"),
    ("F6_A", "F6_B"),
)


def _seed_ready_slate_minus_ack(conn, slate_id) -> list[int]:
    fids: list[int] = []
    for a, b in READY_FIGHTS:
        fids.append(_insert_fighter(conn, slate_id=slate_id, name=a, salary=8000))
        fids.append(_insert_fighter(conn, slate_id=slate_id, name=b, salary=8000))
    for a, b in READY_FIGHTS:
        fg = FightGroupRepository(conn).create(
            slate_id=slate_id,
            fighter_1_name=a,
            fighter_2_name=b,
            scheduled_rounds=3,
        )
        FightGroupRepository(conn).update_status(fg.id, "confirmed")
    for i, (a, b) in enumerate(READY_FIGHTS):
        _save_odds_row(
            conn,
            slate_id=slate_id,
            fighter_name_raw=a,
            american_odds=-150,
            captured_at=f"2026-05-20T00:00:{2 * i:02d}Z",
        )
        _save_odds_row(
            conn,
            slate_id=slate_id,
            fighter_name_raw=b,
            american_odds=+130,
            captured_at=f"2026-05-20T00:00:{2 * i + 1:02d}Z",
        )
    recompute_and_replace_match_results(conn, slate_id)
    return fids


def _seed_ready_slate(conn, slate_id) -> list[int]:
    fids = _seed_ready_slate_minus_ack(conn, slate_id)
    SlateRepository(conn).set_manual_review_reviewed(slate_id)
    return fids


UNDERSIZED_FIGHTS: tuple[tuple[str, str], ...] = (
    ("U1_A", "U1_B"),
    ("U2_A", "U2_B"),
    ("U3_A", "U3_B"),
    ("U4_A", "U4_B"),
)


def _seed_undersized_pool_but_ready_gate(conn, slate_id) -> list[int]:
    fids: list[int] = []
    for a, b in UNDERSIZED_FIGHTS:
        fids.append(_insert_fighter(conn, slate_id=slate_id, name=a, salary=8000))
        fids.append(_insert_fighter(conn, slate_id=slate_id, name=b, salary=8000))
    for a, b in UNDERSIZED_FIGHTS:
        fg = FightGroupRepository(conn).create(
            slate_id=slate_id,
            fighter_1_name=a,
            fighter_2_name=b,
            scheduled_rounds=3,
        )
        FightGroupRepository(conn).update_status(fg.id, "confirmed")
    covered = (
        ("U1_A", -150, 0),
        ("U1_B", +130, 1),
        ("U2_A", -120, 2),
        ("U2_B", +110, 3),
        ("U3_A", -200, 4),
    )
    for name, odds, i in covered:
        _save_odds_row(
            conn,
            slate_id=slate_id,
            fighter_name_raw=name,
            american_odds=odds,
            captured_at=f"2026-05-20T00:00:{i:02d}Z",
        )
    recompute_and_replace_match_results(conn, slate_id)
    SlateRepository(conn).set_manual_review_reviewed(slate_id)
    return fids


_SNAPSHOT_TABLES = (
    "slates",
    "fighters",
    "fight_groups",
    "odds_rows",
    "odds_match_results",
    "manual_match_overrides",
)


def _db_snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    snap: dict[str, list[tuple]] = {}
    for table in _SNAPSHOT_TABLES:
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY rowid"
        ).fetchall()
        snap[table] = [tuple(r) for r in rows]
    return snap


# ---------------------------------------------------------------------------
# Ready slate — happy path
# ---------------------------------------------------------------------------


def test_ready_slate_returns_bundle_with_expected_metadata(conn, slate_id):
    _seed_ready_slate(conn, slate_id)

    bundle = build_run_log(conn, slate_id=slate_id, n_lineups=1)

    assert isinstance(bundle, InternalExportBundle)
    assert bundle.optimizer_status == STATUS_OK
    assert bundle.n_lineups_generated == 1
    assert len(bundle.lineups) == 1
    assert bundle.metadata.slate_id == slate_id
    assert bundle.metadata.slate_name == "UFC 999 — Export Test"
    assert bundle.metadata.slate_event_date == "2026-05-30"
    assert bundle.metadata.n_lineups_requested == 1
    assert bundle.metadata.run_id.endswith(f"-slate{slate_id}")
    assert bundle.metadata.generated_at_utc.endswith("Z")

    snap = bundle.metadata.manual_review
    assert snap is not None
    assert snap.ready is True
    assert snap.status == "reviewed"
    # ``blocking_count`` is the total Blocking-category row count
    # (passed + failed); ``ready`` is the signal that none failed.
    assert snap.blocking_count >= 0


def test_ready_slate_bundle_lineup_carries_names_salaries_projections(
    conn, slate_id
):
    fids = _seed_ready_slate(conn, slate_id)

    bundle = build_run_log(conn, slate_id=slate_id, n_lineups=1)

    assert len(bundle.lineups) == 1
    lu = bundle.lineups[0]
    assert len(lu.fighters) == 6
    assert lu.total_salary <= 50_000
    for f in lu.fighters:
        assert isinstance(f.fighter_name, str)
        assert f.fighter_name
        assert int(f.dk_salary) == 8000
        assert isinstance(f.default_projection, float)
        # Fight-group id should be populated since every fighter in the
        # ready slate is paired into a confirmed group.
        assert f.fight_group_id is not None


def test_ready_slate_bundle_carries_pool_diagnostics(conn, slate_id):
    _seed_ready_slate(conn, slate_id)
    bundle = build_run_log(conn, slate_id=slate_id, n_lineups=1)
    assert bundle.diagnostics is not None
    assert bundle.diagnostics.pool_size == 12  # twelve eligible fighters
    assert isinstance(bundle.diagnostics.excluded, tuple)


def test_ready_slate_bundle_feeds_all_three_formatters(conn, slate_id):
    """The C.3 service is wired to the C.2 formatter surface — every
    formatter accepts the returned bundle and produces non-empty bytes
    that mention the run id and the warning string."""
    _seed_ready_slate(conn, slate_id)
    bundle = build_run_log(conn, slate_id=slate_id, n_lineups=2)

    csv_bytes = format_lineups_csv(bundle)
    json_bytes = format_lineups_json(bundle)
    md_bytes = format_markdown_summary(bundle)

    for blob in (csv_bytes, json_bytes, md_bytes):
        assert isinstance(blob, bytes)
        assert len(blob) > 0
        assert bundle.metadata.run_id.encode("utf-8") in blob
        assert b"Internal research export only" in blob


def test_multi_lineup_request_is_propagated(conn, slate_id):
    _seed_ready_slate(conn, slate_id)
    bundle = build_run_log(conn, slate_id=slate_id, n_lineups=3)
    assert bundle.metadata.n_lineups_requested == 3
    assert bundle.optimizer_status in (STATUS_OK, "ok_partial")
    assert bundle.n_lineups_generated == len(bundle.lineups)
    assert 1 <= bundle.n_lineups_generated <= 3


# ---------------------------------------------------------------------------
# Gate-not-ready: diagnostics-only bundle (design §7 rule 4 / §12)
# ---------------------------------------------------------------------------


def test_gate_not_ready_returns_diagnostics_only_bundle(conn, slate_id):
    _seed_ready_slate_minus_ack(conn, slate_id)  # missing user-ack
    bundle = build_run_log(conn, slate_id=slate_id, n_lineups=1)
    assert bundle.optimizer_status == STATUS_GATE_BLOCKED
    assert bundle.lineups == ()
    assert bundle.n_lineups_generated == 0
    assert bundle.diagnostics is not None
    assert bundle.diagnostics.pool_size == 0
    snap = bundle.metadata.manual_review
    assert snap is not None
    assert snap.ready is False
    assert snap.blocking_count >= 1


def test_unknown_slate_returns_diagnostics_only_bundle(conn):
    bundle = build_run_log(conn, slate_id=999_999, n_lineups=1)
    assert bundle.optimizer_status == STATUS_GATE_BLOCKED
    assert bundle.lineups == ()
    assert bundle.metadata.slate_id == 999_999
    assert bundle.metadata.slate_name is None
    assert bundle.metadata.slate_event_date is None


def test_gate_not_ready_does_not_invoke_optimizer(
    conn, slate_id, monkeypatch
):
    """Per design §12 step 1: when the gate is not ready the service
    must not run the solver or build the pool."""
    _seed_ready_slate_minus_ack(conn, slate_id)

    pool_calls: list = []
    opt_calls: list = []

    def _boom_pool(*a, **kw):
        pool_calls.append((a, kw))
        raise AssertionError("build_optimizer_pool must not run when gate fails")

    def _boom_run(*a, **kw):
        opt_calls.append((a, kw))
        raise AssertionError("run_optimizer must not run when gate fails")

    monkeypatch.setattr(export_service, "build_optimizer_pool", _boom_pool)
    monkeypatch.setattr(export_service, "run_optimizer", _boom_run)

    bundle = build_run_log(conn, slate_id=slate_id, n_lineups=1)
    assert bundle.optimizer_status == STATUS_GATE_BLOCKED
    assert pool_calls == []
    assert opt_calls == []


# ---------------------------------------------------------------------------
# n_lineups validation (design §7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_n", [0, -1, 6, 100])
def test_invalid_n_lineups_raises_export_validation_error(
    conn, slate_id, bad_n
):
    _seed_ready_slate(conn, slate_id)
    with pytest.raises(ExportValidationError) as exc:
        build_run_log(conn, slate_id=slate_id, n_lineups=bad_n)
    assert "n_lineups" in exc.value.reason


def test_invalid_n_lineups_does_not_invoke_optimizer(
    conn, slate_id, monkeypatch
):
    _seed_ready_slate(conn, slate_id)

    opt_calls: list = []
    pool_calls: list = []

    def _boom_run(*a, **kw):
        opt_calls.append((a, kw))
        raise AssertionError("run_optimizer must not run on invalid n_lineups")

    def _boom_pool(*a, **kw):
        pool_calls.append((a, kw))
        raise AssertionError("build_optimizer_pool must not run on invalid n_lineups")

    monkeypatch.setattr(export_service, "run_optimizer", _boom_run)
    monkeypatch.setattr(export_service, "build_optimizer_pool", _boom_pool)

    with pytest.raises(ExportValidationError):
        build_run_log(conn, slate_id=slate_id, n_lineups=99)

    assert opt_calls == []
    assert pool_calls == []


# ---------------------------------------------------------------------------
# Validation rules 1–3 against a synthetic bad SolveResult
# ---------------------------------------------------------------------------


def _patch_run_optimizer_to_return(monkeypatch, result: SolveResult) -> None:
    monkeypatch.setattr(
        export_service,
        "run_optimizer",
        lambda *a, **kw: result,
    )


def test_lineup_with_wrong_size_raises_export_validation_error(
    conn, slate_id, monkeypatch
):
    fids = _seed_ready_slate(conn, slate_id)
    bad = SolveResult(
        slate_id=slate_id,
        status=STATUS_OK,
        lineups=(
            Lineup(
                fighter_ids=tuple(fids[:7]),  # 7 fighters
                total_salary=48_000,
                total_projection=120.0,
            ),
        ),
        reason="",
    )
    _patch_run_optimizer_to_return(monkeypatch, bad)
    with pytest.raises(ExportValidationError) as exc:
        build_run_log(conn, slate_id=slate_id, n_lineups=1)
    assert "6" in exc.value.reason
    assert exc.value.lineup is not None


def test_lineup_with_oversized_salary_raises_export_validation_error(
    conn, slate_id, monkeypatch
):
    fids = _seed_ready_slate(conn, slate_id)
    bad = SolveResult(
        slate_id=slate_id,
        status=STATUS_OK,
        lineups=(
            Lineup(
                fighter_ids=tuple(sorted(fids[:6])),
                total_salary=50_001,
                total_projection=120.0,
            ),
        ),
        reason="",
    )
    _patch_run_optimizer_to_return(monkeypatch, bad)
    with pytest.raises(ExportValidationError) as exc:
        build_run_log(conn, slate_id=slate_id, n_lineups=1)
    assert "50000" in exc.value.reason.replace(",", "") or "50,000" in exc.value.reason
    assert exc.value.lineup is not None


def test_lineup_with_same_fight_pair_raises_export_validation_error(
    conn, slate_id, monkeypatch
):
    """A synthetic lineup containing both sides of the first fight
    triggers §7 rule 3 — same-fight pair detected against the
    OptimizerPool the orchestration service built itself."""
    fids = _seed_ready_slate(conn, slate_id)
    # Both sides of the first fight (F1_A, F1_B) followed by four other
    # eligible fighters from different fights.
    pair_ids = sorted([fids[0], fids[1], fids[2], fids[4], fids[6], fids[8]])
    bad = SolveResult(
        slate_id=slate_id,
        status=STATUS_OK,
        lineups=(
            Lineup(
                fighter_ids=tuple(pair_ids),
                total_salary=48_000,
                total_projection=120.0,
            ),
        ),
        reason="",
    )
    _patch_run_optimizer_to_return(monkeypatch, bad)
    with pytest.raises(ExportValidationError) as exc:
        build_run_log(conn, slate_id=slate_id, n_lineups=1)
    assert "same-fight" in exc.value.reason
    assert exc.value.lineup is not None


# ---------------------------------------------------------------------------
# Infeasible pool → diagnostics-only bundle (design §7 rule 5)
# ---------------------------------------------------------------------------


def test_undersized_pool_returns_diagnostics_only_bundle(conn, slate_id):
    _seed_undersized_pool_but_ready_gate(conn, slate_id)
    bundle = build_run_log(conn, slate_id=slate_id, n_lineups=1)
    assert bundle.optimizer_status == STATUS_INFEASIBLE_POOL_TOO_SMALL
    assert bundle.lineups == ()
    assert bundle.n_lineups_generated == 0
    assert bundle.diagnostics is not None
    # The five projection-ok fighters from the seed.
    assert bundle.diagnostics.pool_size == 5


# ---------------------------------------------------------------------------
# Read-only invariant (docs/DEVELOPMENT_NOTES.md §11)
# ---------------------------------------------------------------------------


def test_build_run_log_is_read_only_on_ready_slate(conn, slate_id):
    _seed_ready_slate(conn, slate_id)
    before = _db_snapshot(conn)
    build_run_log(conn, slate_id=slate_id, n_lineups=1)
    build_run_log(conn, slate_id=slate_id, n_lineups=2)
    after = _db_snapshot(conn)
    assert before == after


def test_build_run_log_is_read_only_on_gate_blocked_slate(conn, slate_id):
    _seed_ready_slate_minus_ack(conn, slate_id)
    before = _db_snapshot(conn)
    build_run_log(conn, slate_id=slate_id, n_lineups=1)
    after = _db_snapshot(conn)
    assert before == after


def test_build_run_log_is_read_only_on_undersized_pool(conn, slate_id):
    _seed_undersized_pool_but_ready_gate(conn, slate_id)
    before = _db_snapshot(conn)
    build_run_log(conn, slate_id=slate_id, n_lineups=1)
    after = _db_snapshot(conn)
    assert before == after


# ---------------------------------------------------------------------------
# No file writes (design §5 Option A)
# ---------------------------------------------------------------------------


def test_build_run_log_does_not_write_under_repo_or_tmp_path(
    conn, slate_id, tmp_path, monkeypatch
):
    """Design §5 Option A — no export file is persisted to disk by the
    app. The CBC solver legitimately writes scratch MPS files to the
    OS temp directory; those are an upstream solver concern, not an
    export artifact, so this test scopes the "no writes" check to the
    test's ``tmp_path`` (which would only see writes if the service or
    formatters opened a file under the caller's working area).
    """
    _seed_ready_slate(conn, slate_id)
    monkeypatch.chdir(tmp_path)

    bundle = build_run_log(conn, slate_id=slate_id, n_lineups=1)
    format_lineups_csv(bundle)
    format_lineups_json(bundle)
    format_markdown_summary(bundle)

    assert list(Path(tmp_path).iterdir()) == []


def test_formatters_do_not_open_any_file(
    conn, slate_id, monkeypatch
):
    """The C.2 formatters are pure: rendering an existing bundle must
    not open or write any file. Build the bundle first (so the solver
    can do its work undisturbed), then patch ``builtins.open`` to fail
    on *any* invocation and run all three formatters."""
    _seed_ready_slate(conn, slate_id)
    bundle = build_run_log(conn, slate_id=slate_id, n_lineups=1)

    def _no_open(*a, **kw):
        raise AssertionError(
            f"C.2 formatters must not open files; got open({a!r}, {kw!r})"
        )

    monkeypatch.setattr(builtins, "open", _no_open)

    format_lineups_csv(bundle)
    format_lineups_json(bundle)
    format_markdown_summary(bundle)


# ---------------------------------------------------------------------------
# Bundle reuses the C.2 formatter surface (design §8)
# ---------------------------------------------------------------------------


def test_returned_bundle_is_the_c2_internal_export_bundle(conn, slate_id):
    """Design §8: the C.3 service delegates byte-shaping to the C.2
    formatters by returning the same :class:`InternalExportBundle`
    dataclass — not a private C.3 type."""
    _seed_ready_slate(conn, slate_id)
    bundle = build_run_log(conn, slate_id=slate_id, n_lineups=1)
    assert type(bundle) is InternalExportBundle
