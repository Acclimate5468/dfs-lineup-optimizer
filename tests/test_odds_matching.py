import pytest

from src.ingestion.name_matching import (
    NICKNAME_EXPANSIONS,
    best_match,
    normalize_name_aggressive,
    suggest_best_fighter,
)
from src.utils.text_cleaning import normalize_name


def test_normalize_name_strips_accents_and_case():
    assert normalize_name("José  Aldo") == "jose aldo"
    assert normalize_name("  Conor   McGregor  ") == "conor mcgregor"


def test_best_match_returns_close_match():
    candidates = ["Conor McGregor", "Dustin Poirier", "Khabib Nurmagomedov"]
    result = best_match("Conor Mcgregor", candidates)
    assert result is not None
    name, score = result
    assert name == "Conor McGregor"
    assert score >= 88


def test_best_match_returns_none_when_below_threshold():
    candidates = ["Conor McGregor"]
    assert best_match("Totally Unrelated", candidates) is None


def test_best_match_empty_inputs():
    assert best_match("", ["A"]) is None
    assert best_match("A", []) is None


# ---------------------------------------------------------------------------
# suggest_best_fighter — low-confidence hint for the manual odds fixer
# ---------------------------------------------------------------------------


def test_suggest_best_fighter_surfaces_near_miss_below_review_threshold():
    """The real smoke case: the sportsbook 'Bruno Gustavo da Silva' scores ~85
    against DK 'Bruno Silva' — below the matcher's 88, so it lands unmatched —
    but the fixer should still suggest Bruno Silva."""
    roster = ["Bruno Silva", "Edgar Chairez", "Belal Muhammad", "Gabriel Bonfim"]
    assert (
        suggest_best_fighter("Bruno Gustavo da Silva", roster) == "Bruno Silva"
    )


def test_suggest_best_fighter_returns_original_dk_name_casing():
    roster = ["Conor McGregor", "Dustin Poirier"]
    assert suggest_best_fighter("conor mcgregor jr", roster) == "Conor McGregor"


def test_suggest_best_fighter_none_when_nothing_close():
    roster = ["Bruno Silva", "Edgar Chairez"]
    assert suggest_best_fighter("Totally Unrelated Person", roster) is None


def test_suggest_best_fighter_none_for_empty_roster():
    assert suggest_best_fighter("Bruno Gustavo da Silva", []) is None


# ---------------------------------------------------------------------------
# Conservative normalize_name regression guard (design §10.1.7)
#
# Conservative form is the persisted index key — it must keep suffixes,
# apostrophes, and original token order. Changing it would silently invalidate
# stored data.
# ---------------------------------------------------------------------------

def test_normalize_name_keeps_suffix_and_punctuation_and_order():
    assert normalize_name("Jose Aldo Jr.") == "jose aldo jr."
    assert normalize_name("D'Angelo Smith") == "d'angelo smith"
    assert normalize_name("Zhang Weili") == "zhang weili"


# ---------------------------------------------------------------------------
# normalize_name_aggressive — empty / whitespace inputs
# ---------------------------------------------------------------------------

def test_aggressive_empty_string_returns_empty():
    assert normalize_name_aggressive("") == ""


def test_aggressive_whitespace_only_returns_empty():
    assert normalize_name_aggressive("   ") == ""


# ---------------------------------------------------------------------------
# Design §10.1.1 — trailing generational suffix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected_tokens",
    [
        ("Jose Aldo Jr", {"aldo", "jose"}),
        ("Jose Aldo jr.", {"aldo", "jose"}),
        ("Jose Aldo Sr", {"aldo", "jose"}),
        ("Robert Whittaker II", {"robert", "whittaker"}),
        ("Robert Whittaker III", {"robert", "whittaker"}),
        ("Robert Whittaker IV", {"robert", "whittaker"}),
    ],
)
def test_aggressive_drops_trailing_suffix(raw, expected_tokens):
    assert set(normalize_name_aggressive(raw).split()) == expected_tokens


def test_aggressive_jose_aldo_jr_collapses_to_jose_aldo():
    assert normalize_name_aggressive("Jose Aldo Jr") == normalize_name_aggressive(
        "Jose Aldo"
    )


def test_aggressive_does_not_drop_internal_suffix_like_token():
    # The rule is "trailing" — a stray 'ii' that is not at the end must stay.
    result = normalize_name_aggressive("ii jose aldo")
    assert "ii" in result.split()


def test_aggressive_does_not_reduce_to_empty_when_only_suffix():
    # If the entire input is a suffix-shaped token, leave it alone rather
    # than producing an empty string.
    assert normalize_name_aggressive("Jr") == "jr"


# ---------------------------------------------------------------------------
# Design §10.1.2 — punctuation collapse
# ---------------------------------------------------------------------------

def test_aggressive_collapses_apostrophe_inside_word():
    # D'Angelo Smith → tokens d, angelo, smith — order is lossy (token-sort).
    assert normalize_name_aggressive("D'Angelo Smith") == "angelo d smith"


def test_aggressive_collapses_smart_apostrophe():
    assert normalize_name_aggressive("D’Angelo Smith") == normalize_name_aggressive(
        "D'Angelo Smith"
    )


def test_aggressive_collapses_hyphen():
    # Hyphen is replaced with space, but 'o' is a recognised surname particle
    # so it survives the middle-initial drop.
    result = normalize_name_aggressive("Sean O-Malley")
    assert set(result.split()) == {"malley", "o", "sean"}


def test_aggressive_collapses_period():
    # St. Preux → tokens ovince, st, preux (st is 2 chars, kept).
    assert set(normalize_name_aggressive("Ovince St. Preux").split()) == {
        "ovince",
        "preux",
        "st",
    }


def test_aggressive_collapses_underscore():
    assert set(normalize_name_aggressive("conor_mcgregor").split()) == {
        "conor",
        "mcgregor",
    }


def test_aggressive_case_insensitive_and_whitespace_normalized():
    assert normalize_name_aggressive("Jose Aldo") == normalize_name_aggressive(
        "  JOSE   ALDO  "
    )


# ---------------------------------------------------------------------------
# Design §10.1.3 — bracketed nickname removal
# ---------------------------------------------------------------------------

def test_aggressive_drops_double_quoted_nickname_mid_name():
    assert set(normalize_name_aggressive('John "The Rock" Smith').split()) == {
        "john",
        "smith",
    }


def test_aggressive_drops_smart_double_quoted_nickname():
    assert set(
        normalize_name_aggressive("John “The Rock” Smith").split()
    ) == {"john", "smith"}


def test_aggressive_drops_single_quoted_nickname_with_spaces():
    # Only single-quoted spans containing whitespace are treated as a wrapper.
    assert set(normalize_name_aggressive("John 'The Rock' Smith").split()) == {
        "john",
        "smith",
    }


def test_aggressive_does_not_treat_intra_word_apostrophe_as_wrapper():
    # D'Angelo: the apostrophe is intra-word, not a wrapper. It splits the
    # word into tokens but must not swallow surrounding tokens.
    result = normalize_name_aggressive("D'Angelo Smith")
    assert "angelo" in result.split()
    assert "smith" in result.split()
    assert "d" in result.split()


# ---------------------------------------------------------------------------
# Design §10.1.4 — standalone middle-initial tokens
# ---------------------------------------------------------------------------

def test_aggressive_drops_standalone_middle_initial():
    assert set(normalize_name_aggressive("Michael J Pereira").split()) == {
        "michael",
        "pereira",
    }


def test_aggressive_drops_middle_initial_with_period():
    assert set(normalize_name_aggressive("Michael J. Pereira").split()) == {
        "michael",
        "pereira",
    }


def test_aggressive_michael_j_pereira_equals_michael_pereira():
    assert normalize_name_aggressive(
        "Michael J Pereira"
    ) == normalize_name_aggressive("Michael Pereira")


def test_aggressive_keeps_single_letter_token_in_two_token_name():
    # 'J Smith' has only two tokens — no middle position, nothing to drop.
    assert set(normalize_name_aggressive("J Smith").split()) == {"j", "smith"}


# ---------------------------------------------------------------------------
# Surname-particle preservation — single-letter particles like the Irish
# `O` (O'Malley) and French/Italian `D` (D'Angelo) must not be swept up by
# the middle-initial drop, even when they appear in the middle token slot.
# ---------------------------------------------------------------------------

def test_aggressive_sean_o_malley_apostrophe_keeps_o():
    assert set(normalize_name_aggressive("Sean O'Malley").split()) == {
        "malley",
        "o",
        "sean",
    }


def test_aggressive_sean_o_malley_hyphen_keeps_o():
    assert set(normalize_name_aggressive("Sean O-Malley").split()) == {
        "malley",
        "o",
        "sean",
    }


def test_aggressive_sean_o_malley_spaced_keeps_o():
    assert set(normalize_name_aggressive("Sean O Malley").split()) == {
        "malley",
        "o",
        "sean",
    }


def test_aggressive_o_malley_three_forms_collapse_identically():
    apostrophe = normalize_name_aggressive("Sean O'Malley")
    hyphen = normalize_name_aggressive("Sean O-Malley")
    spaced = normalize_name_aggressive("Sean O Malley")
    assert apostrophe == hyphen == spaced


def test_aggressive_still_drops_middle_initial_J_after_particle_fix():
    # Regression guard: the surname-particle whitelist must not broaden
    # to all single-letter middle tokens.
    assert set(normalize_name_aggressive("Michael J Pereira").split()) == {
        "michael",
        "pereira",
    }


def test_aggressive_d_angelo_remains_safe():
    # D'Angelo as a surname after a given name: 'd' is in the middle slot
    # but is a recognised particle, so it survives.
    result = normalize_name_aggressive("John D'Angelo Smith")
    assert "d" in result.split()
    assert "angelo" in result.split()
    assert {"john", "smith"}.issubset(set(result.split()))


# ---------------------------------------------------------------------------
# Design §10.1.5 — curated bidirectional nickname expansion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("short,full", list(NICKNAME_EXPANSIONS.items()))
def test_aggressive_nickname_expansion_bidirectional(short, full):
    """Every curated pair must collapse short and full forms to one result."""
    assert normalize_name_aggressive(
        f"{short} smith"
    ) == normalize_name_aggressive(f"{full} smith")


def test_aggressive_nickname_dan_ige():
    # design §3.1 calibration row
    assert normalize_name_aggressive("Dan Ige") == normalize_name_aggressive(
        "Daniel Ige"
    )


def test_aggressive_nickname_tom_almeida():
    # design §3.1 calibration row
    assert normalize_name_aggressive("Tom Almeida") == normalize_name_aggressive(
        "Thomas Almeida"
    )


def test_aggressive_does_not_apply_uncurated_nickname():
    # 'jon' is NOT in the curated table — even though 'jon jones' /
    # 'jonathan jones' is a common pair, v0 refuses to assume it.
    assert normalize_name_aggressive("Jon Jones") != normalize_name_aggressive(
        "Jonathan Jones"
    )


def test_aggressive_no_broad_prefix_match():
    # 'danny' is not 'dan' and is not in the table — must not silently
    # collapse to 'daniel'.
    assert normalize_name_aggressive("Danny Ige") != normalize_name_aggressive(
        "Daniel Ige"
    )


def test_aggressive_does_not_expand_within_substring():
    # 'tomas' is not 'tom' — token-equality only. Must not be rewritten
    # to 'thomas'.
    result = normalize_name_aggressive("Tomas Smith")
    assert "tomas" in result.split()
    assert "thomas" not in result.split()


def test_aggressive_alex_does_not_match_alexandra():
    # 'alex' → 'alexander' is curated; 'alexandra' is not. They must remain
    # distinct after aggressive normalization.
    assert normalize_name_aggressive("Alex Smith") != normalize_name_aggressive(
        "Alexandra Smith"
    )


# ---------------------------------------------------------------------------
# Design §10.1.6 — token-sort
# ---------------------------------------------------------------------------

def test_aggressive_token_sort_zhang_weili():
    assert normalize_name_aggressive("Zhang Weili") == normalize_name_aggressive(
        "Weili Zhang"
    )


def test_aggressive_token_sort_output_is_alphabetical():
    tokens = normalize_name_aggressive("Weili Zhang").split()
    assert tokens == sorted(tokens)


# ---------------------------------------------------------------------------
# Exact-normalized match / different-name guard
# ---------------------------------------------------------------------------

def test_aggressive_simple_name_matches_itself_with_extra_whitespace():
    assert normalize_name_aggressive("Jose Aldo") == normalize_name_aggressive(
        "JOSE  ALDO"
    )


def test_aggressive_unrelated_names_do_not_collide():
    assert normalize_name_aggressive("Conor McGregor") != normalize_name_aggressive(
        "Dustin Poirier"
    )


def test_aggressive_distinct_short_names_do_not_collide():
    # Two different fighters whose given names happen to share a curated
    # nickname target must still be distinguishable by surname.
    assert normalize_name_aggressive("Dan Smith") != normalize_name_aggressive(
        "Dan Hooker"
    )
