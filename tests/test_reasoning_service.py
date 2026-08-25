"""Tests for the B6 read-only reasoning-context assembler.

Covers ``assemble_reasoning_context`` in
``src/exports/reasoning_service.py`` per
``docs/TWO_STEP_BUILDER_PRODUCTION_DESIGN.md`` §8.1 (the B6 deliverable)
and §7.6 / §7.7 (read-only). The assembler is the read-only bridge from
the optimizer / export bundle to the pure ``build_lineup_reasoning``
generator. It must:

- carry already-computed facts through verbatim (implied win probability,
  salary, projection, scheduled rounds, fight group, projection status);
- re-derive only the value-gap / five-round bonuses via the pure formula
  helpers (``docs/DEVELOPMENT_NOTES.md`` §4) — no new math;
- surface excluded fighters and the gate's active Warning / Blocking flags
  (design §8.2), with the gate's own wording;
- never write, recompute, or fabricate a fact the services did not supply.

The pure generator's own line-rendering rules are pinned separately in
``tests/test_lineup_reasoning.py``; here we exercise the DB-backed
assembly that feeds it.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.config.constants import LINEUP_SIZE, SALARY_CAP
from src.db.repositories import (
    FightGroupRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.exports.export_service import build_run_log
from src.exports.internal_export import (
    BundleFighter,
    BundleLineup,
    ExportDiagnostics,
    ExportRunMetadata,
    InternalExportBundle,
)
from src.exports.lineup_reasoning import (
    KIND_EXCLUSION_OR_WARNING,
    KIND_FIVE_ROUND_CONTEXT,
    KIND_VALUE_DRIVER,
    build_lineup_reasoning,
)
from src.exports.reasoning_service import assemble_reasoning_context
from src.ingestion.odds_matching_service import (
    recompute_and_replace_match_results,
)
from src.projections.value_bonus import five_round_bonus, value_gap_bonus
from src.slate.manual_review import (
    CATEGORY_BLOCKING,
    CATEGORY_WARNING,
    STATUS_FAIL,
)
from src.slate.manual_review_service import evaluate_manual_review

# Outcome / hype vocabulary the generator must never author (design §8.3),
# mirroring tests/test_lineup_reasoning.py.
_BANNED = ("lock", "guarantee", "guaranteed", "will win", "finish", "itd")


# ---------------------------------------------------------------------------
# Fixtures + seeding helpers (mirroring tests/test_optimizer_service.py)
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


def _insert_fighter(conn, *, slate_id, name, salary):
    cur = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, 'active')",
        (int(slate_id), name, int(salary)),
    )
    conn.commit()
    return int(cur.lastrowid)


def _save_odds_row(conn, *, slate_id, name, american_odds, seq):
    return OddsRowRepository(conn).create(
        slate_id=slate_id,
        fighter_name_raw=name,
        american_odds=american_odds,
        source="manual",
        captured_at=f"2026-05-20T00:00:{seq:02d}Z",
    )


def _seed_ready_slate_with_main_event(conn) -> int:
    """Six confirmed fights / twelve auto-matched active fighters, reviewed.

    Fight 1 is the main event (``scheduled_rounds=5``). ``F1_A`` is priced
    cheap-but-live (7,500 @ -160) so it clears the +8 value-gap tier *and*
    carries the +7 five-round bonus — the single highest projection, so the
    solver always selects it. Every other A-side favourite (8,000 @ -150)
    clears the +5 tier; B-side dogs clear none. Twelve fighters across six
    distinct fights is the smallest setup that fills a 6-fighter lineup
    under the one-per-fight constraint, and 47,500 total fits the 50k cap.
    """
    sid = SlateRepository(conn).create(
        event_name="UFC Main Event",
        salary_csv_status="validated",
        salary_row_count=12,
    ).id

    fights = [(f"F{i}_A", f"F{i}_B") for i in range(1, 7)]
    _insert_fighter(conn, slate_id=sid, name="F1_A", salary=7500)
    _insert_fighter(conn, slate_id=sid, name="F1_B", salary=8000)
    for a, b in fights[1:]:
        _insert_fighter(conn, slate_id=sid, name=a, salary=8000)
        _insert_fighter(conn, slate_id=sid, name=b, salary=8000)

    for idx, (a, b) in enumerate(fights):
        rounds = 5 if idx == 0 else 3
        fg = FightGroupRepository(conn).create(
            slate_id=sid,
            fighter_1_name=a,
            fighter_2_name=b,
            scheduled_rounds=rounds,
        )
        FightGroupRepository(conn).update_status(fg.id, "confirmed")

    seq = 0
    for idx, (a, b) in enumerate(fights):
        fav_odds = -160 if idx == 0 else -150
        _save_odds_row(conn, slate_id=sid, name=a, american_odds=fav_odds, seq=seq)
        seq += 1
        _save_odds_row(conn, slate_id=sid, name=b, american_odds=+130, seq=seq)
        seq += 1

    recompute_and_replace_match_results(conn, sid)
    SlateRepository(conn).set_manual_review_reviewed(sid)
    return sid


def _seed_ready_slate_one_uncovered(conn) -> int:
    """Seven confirmed fights; every fighter has odds except ``F7_B``.

    13 of 14 matched (7% uncovered, below the 50% Blocking threshold), and
    ``F7_B`` falls to ``missing_inputs`` (no odds) so it is filtered out of
    the pool with a stored reason. The gate stays ready (warnings only), the
    pool is still 13 (≥ 6), so a lineup generates *and* there is an excluded
    fighter to carry through.
    """
    sid = SlateRepository(conn).create(
        event_name="UFC One Uncovered",
        salary_csv_status="validated",
        salary_row_count=14,
    ).id
    fights = [(f"G{i}_A", f"G{i}_B") for i in range(1, 8)]
    for a, b in fights:
        _insert_fighter(conn, slate_id=sid, name=a, salary=8000)
        _insert_fighter(conn, slate_id=sid, name=b, salary=8000)
    for a, b in fights:
        fg = FightGroupRepository(conn).create(
            slate_id=sid,
            fighter_1_name=a,
            fighter_2_name=b,
            scheduled_rounds=3,
        )
        FightGroupRepository(conn).update_status(fg.id, "confirmed")

    seq = 0
    for a, b in fights:
        _save_odds_row(conn, slate_id=sid, name=a, american_odds=-150, seq=seq)
        seq += 1
        if b != "G7_B":  # leave one fighter without odds
            _save_odds_row(conn, slate_id=sid, name=b, american_odds=+130, seq=seq)
            seq += 1

    recompute_and_replace_match_results(conn, sid)
    SlateRepository(conn).set_manual_review_reviewed(sid)
    return sid


def _seed_blocked_no_odds(conn) -> int:
    """Salary + confirmed groups but zero odds → ``odds_unmatched_active``
    fails (100% uncovered) → the gate is not green → ``build_run_log``
    returns the diagnostics-only ``gate_blocked`` bundle (no lineups)."""
    sid = SlateRepository(conn).create(
        event_name="UFC Blocked",
        salary_csv_status="validated",
        salary_row_count=12,
    ).id
    fights = [(f"B{i}_A", f"B{i}_B") for i in range(1, 7)]
    for a, b in fights:
        _insert_fighter(conn, slate_id=sid, name=a, salary=8000)
        _insert_fighter(conn, slate_id=sid, name=b, salary=8000)
    for a, b in fights:
        fg = FightGroupRepository(conn).create(
            slate_id=sid,
            fighter_1_name=a,
            fighter_2_name=b,
            scheduled_rounds=3,
        )
        FightGroupRepository(conn).update_status(fg.id, "confirmed")
    return sid


def _db_snapshot(conn) -> dict[str, list[tuple]]:
    tables = (
        "slates",
        "fighters",
        "fight_groups",
        "odds_rows",
        "odds_match_results",
        "manual_match_overrides",
    )
    snap: dict[str, list[tuple]] = {}
    for table in tables:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        snap[table] = [tuple(r) for r in rows]
    return snap


# ---------------------------------------------------------------------------
# Per-fighter facts + lineup totals + cap / roster
# ---------------------------------------------------------------------------


def test_context_carries_lineup_totals_and_cap(conn):
    sid = _seed_ready_slate_with_main_event(conn)
    bundle = build_run_log(conn, slate_id=sid, n_lineups=1)
    ctx = assemble_reasoning_context(conn, slate_id=sid, bundle=bundle)

    assert ctx.salary_cap == SALARY_CAP
    assert ctx.roster_size == LINEUP_SIZE
    assert len(ctx.lineups) == 1

    lineup = ctx.lineups[0]
    assert lineup.lineup_index == 1
    assert len(lineup.fighters) == LINEUP_SIZE
    # Totals are the solver's own per-lineup totals, carried verbatim.
    assert lineup.total_salary == bundle.lineups[0].total_salary
    assert lineup.total_projection == pytest.approx(
        bundle.lineups[0].total_projection
    )
    # The no-same-fight constraint holds, so every fighter is from a
    # distinct fight group (the fact the generator's constraint line reads).
    group_ids = [f.fight_group_id for f in lineup.fighters]
    assert all(gid is not None for gid in group_ids)
    assert len(set(group_ids)) == LINEUP_SIZE


def test_context_carries_per_fighter_facts_verbatim(conn):
    sid = _seed_ready_slate_with_main_event(conn)
    bundle = build_run_log(conn, slate_id=sid, n_lineups=1)
    ctx = assemble_reasoning_context(conn, slate_id=sid, bundle=bundle)

    by_name = {f.name: f for f in ctx.lineups[0].fighters}
    # F1_A is the top projection (value + five-round) → always selected.
    assert "F1_A" in by_name
    f1a = by_name["F1_A"]
    assert f1a.salary == 7500
    assert f1a.projection_status == "ok"
    assert f1a.fight_group_id is not None
    assert f1a.implied_win_probability is not None
    assert f1a.scheduled_rounds == 5

    # Bonuses are re-derived only via the pure formula helpers (no new math).
    assert f1a.value_gap_bonus == value_gap_bonus(
        f1a.salary, f1a.implied_win_probability
    )
    assert f1a.value_gap_bonus > 0  # clears a value-gap tier
    assert f1a.five_round_bonus == five_round_bonus(f1a.scheduled_rounds)
    assert f1a.five_round_bonus == 7.0

    # Every selected (status-ok) fighter carries the full fact set, and the
    # bonus fields always equal the pure-helper output for their inputs.
    for f in ctx.lineups[0].fighters:
        assert f.projection_status == "ok"
        assert f.implied_win_probability is not None
        assert f.scheduled_rounds in (3, 5)
        assert f.fight_group_id is not None
        assert f.value_gap_bonus == value_gap_bonus(
            f.salary, f.implied_win_probability
        )
        assert f.five_round_bonus == five_round_bonus(f.scheduled_rounds)


def test_generator_emits_five_round_and_value_from_assembled_context(conn):
    sid = _seed_ready_slate_with_main_event(conn)
    bundle = build_run_log(conn, slate_id=sid, n_lineups=1)
    ctx = assemble_reasoning_context(conn, slate_id=sid, bundle=bundle)
    result = build_lineup_reasoning(ctx)

    five = [i for i in result.items if i.kind == KIND_FIVE_ROUND_CONTEXT]
    assert any("F1_A" in i.fighter_names for i in five)
    values = [i for i in result.items if i.kind == KIND_VALUE_DRIVER]
    assert any("F1_A" in i.fighter_names for i in values)

    # The generator's own authored lines never assert an outcome (§8.3).
    authored = [
        i for i in result.items if i.kind != KIND_EXCLUSION_OR_WARNING
    ]
    blob = " ".join(i.text for i in authored).lower()
    for banned in _BANNED:
        assert banned not in blob, blob


# ---------------------------------------------------------------------------
# Read-only invariant (design §7.7)
# ---------------------------------------------------------------------------


def test_assembler_and_bundle_build_are_read_only(conn):
    sid = _seed_ready_slate_with_main_event(conn)
    before = _db_snapshot(conn)
    bundle = build_run_log(conn, slate_id=sid, n_lineups=3)
    assemble_reasoning_context(conn, slate_id=sid, bundle=bundle)
    assert _db_snapshot(conn) == before


# ---------------------------------------------------------------------------
# Excluded fighters carried through verbatim
# ---------------------------------------------------------------------------


def test_excluded_fighters_carried_through(conn):
    sid = _seed_ready_slate_one_uncovered(conn)
    bundle = build_run_log(conn, slate_id=sid, n_lineups=1)
    assert bundle.diagnostics is not None
    bundle_excluded = {e.name: e.reason for e in bundle.diagnostics.excluded}
    assert "G7_B" in bundle_excluded  # the uncovered fighter

    ctx = assemble_reasoning_context(conn, slate_id=sid, bundle=bundle)
    ctx_excluded = {e.name: e.reason for e in ctx.excluded}
    assert "G7_B" in ctx_excluded
    # Reason is the pool's own string, passed through, not invented.
    assert ctx_excluded["G7_B"] == bundle_excluded["G7_B"]
    assert ctx_excluded["G7_B"]

    result = build_lineup_reasoning(ctx)
    notes = [i for i in result.items if i.kind == KIND_EXCLUSION_OR_WARNING]
    assert any("G7_B" in i.text for i in notes)


# ---------------------------------------------------------------------------
# Active gate flags surfaced as warnings, with the gate's own wording
# ---------------------------------------------------------------------------


def test_active_gate_flags_carried_as_warnings(conn):
    sid = _seed_ready_slate_with_main_event(conn)
    bundle = build_run_log(conn, slate_id=sid, n_lineups=1)
    ctx = assemble_reasoning_context(conn, slate_id=sid, bundle=bundle)

    readiness = evaluate_manual_review(conn, sid)
    expected = {
        c.code
        for c in readiness.checks
        if c.category in (CATEGORY_BLOCKING, CATEGORY_WARNING)
        and c.status == STATUS_FAIL
    }
    assert {w.code for w in ctx.warnings} == expected
    # A ready slate has no failing Blocking checks.
    assert not [
        c
        for c in readiness.checks
        if c.category == CATEGORY_BLOCKING and c.status == STATUS_FAIL
    ]
    # Each surfaced warning carries the gate's own message verbatim.
    msg_by_code = {c.code: c.message for c in readiness.checks}
    for w in ctx.warnings:
        assert w.message == msg_by_code[w.code]


# ---------------------------------------------------------------------------
# Gate-blocked bundle → empty lineups, diagnostics still surfaced, no crash
# ---------------------------------------------------------------------------


def test_gate_blocked_bundle_yields_empty_lineups_with_context(conn):
    sid = _seed_blocked_no_odds(conn)
    bundle = build_run_log(conn, slate_id=sid, n_lineups=1)
    assert bundle.lineups == ()

    ctx = assemble_reasoning_context(conn, slate_id=sid, bundle=bundle)
    assert ctx.lineups == ()
    assert ctx.warnings  # the failing gate flags still surface

    result = build_lineup_reasoning(ctx)
    assert result.items
    assert any("No lineups were generated" in i.text for i in result.items)


# ---------------------------------------------------------------------------
# Missing optional inputs → no crash, no fabrication
# ---------------------------------------------------------------------------


def test_unknown_fighter_carries_no_invented_facts(conn):
    sid = SlateRepository(conn).create(
        event_name="UFC Empty",
        salary_csv_status="validated",
        salary_row_count=0,
    ).id
    # A synthetic bundle whose fighter has no projection / odds row on the
    # slate. The assembler must leave every optional fact None, never guess.
    bundle = InternalExportBundle(
        metadata=ExportRunMetadata(
            run_id="r",
            generated_at_utc="2026-05-20T00:00:00Z",
            n_lineups_requested=1,
            slate_id=sid,
        ),
        optimizer_status="ok",
        optimizer_reason="",
        n_lineups_generated=1,
        lineups=(
            BundleLineup(
                lineup_index=1,
                total_salary=8000,
                total_projection=42.0,
                fighters=(BundleFighter("Ghost", 8000, 42.0, None),),
            ),
        ),
        diagnostics=ExportDiagnostics(pool_size=1, excluded=()),
    )

    ctx = assemble_reasoning_context(conn, slate_id=sid, bundle=bundle)
    f = ctx.lineups[0].fighters[0]
    # Required facts carried verbatim.
    assert f.name == "Ghost"
    assert f.salary == 8000
    assert f.projection == 42.0
    # Nothing matched → every optional fact stays None (no fabrication).
    assert f.implied_win_probability is None
    assert f.value_gap_bonus is None
    assert f.five_round_bonus is None
    assert f.scheduled_rounds is None
    assert f.fight_group_id is None
    assert f.projection_status is None

    # The generator still renders without crashing.
    result = build_lineup_reasoning(ctx)
    assert result.items
