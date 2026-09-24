"""Regression tests for the Anthropic model-picker dropping curated aliases.

Bug — newly-routed curated aliases vanished on a native Anthropic setup
    ``provider_model_ids("anthropic")`` returned the live ``/v1/models`` dump
    verbatim whenever Anthropic credentials were configured. Anthropic's API
    lags behind freshly-routed aliases (e.g. ``claude-fable-5``, which is
    reachable on Anthropic before the models endpoint enumerates it), so the
    curated entry disappeared from the picker. The picker now merges the
    curated ``_PROVIDER_MODELS["anthropic"]`` list with the live catalog —
    curated entries first, live-only models appended, deduped — mirroring the
    OpenAI curated-merge philosophy.
"""

from unittest.mock import patch

import re

from hermes_cli import models as M


def test_anthropic_native_list_keeps_aggregator_flagships():
    """Native Anthropic must list the same current flagships OpenRouter/Nous already ship.

    Aggregator catalogs get the new aliases first; the native curated list is what
    `/model` falls back to when live `/v1/models` lags or 401s. Newest-first order
    is the contract that keeps Fable 5.1 / Opus 5 from hiding behind older 4.x ids.
    Native Anthropic IDs are dash-separated (``claude-fable-5-1``), NOT the dotted
    OpenRouter slug convention (``claude-fable-5.1``) — see
    test_anthropic_curated_list_uses_native_dash_ids_only.
    """
    or_ids = {mid for mid, _ in M.OPENROUTER_MODELS}
    native = M._PROVIDER_MODELS["anthropic"]
    for or_slug, native_slug in (("claude-fable-5.1", "claude-fable-5-1"), ("claude-opus-5", "claude-opus-5")):
        assert f"anthropic/{or_slug}" in or_ids
        assert native_slug in native
    assert native.index("claude-fable-5-1") < native.index("claude-fable-5")
    assert native.index("claude-opus-5") < native.index("claude-opus-4-8")


def test_anthropic_curated_alias_survives_when_live_omits_it():
    """A curated alias missing from /v1/models still surfaces (first)."""
    curated = M._PROVIDER_MODELS["anthropic"]
    assert "claude-fable-5-1" in curated  # sanity: newest Fable alias is curated
    assert "claude-fable-5" in curated  # sanity: the alias is curated
    assert "claude-opus-5" in curated  # sanity: native flagship matches aggregators
    assert "claude-sonnet-5" in curated  # newest Sonnet alias is curated

    # Live catalog the API would actually return — no fable-5-1 / opus-5.
    live = ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
    with patch.object(M, "_fetch_anthropic_models", return_value=live):
        result = M.provider_model_ids("anthropic")

    assert "claude-fable-5-1" in result
    assert "claude-fable-5" in result
    assert "claude-opus-5" in result
    assert "claude-sonnet-5" in result
    # Curated order is preserved at the front.
    assert result[:len(curated)] == list(curated)


def test_anthropic_merge_dedupes_overlap_and_appends_live_only():
    """Models in both lists appear once; live-only models are appended."""
    live = [
        "claude-opus-4-8",          # overlaps curated
        "claude-sonnet-4-6",        # overlaps curated
        "claude-future-9-99",       # live-only, not curated
    ]
    with patch.object(M, "_fetch_anthropic_models", return_value=live):
        result = M.provider_model_ids("anthropic")

    # No duplicates introduced by the merge.
    assert result.count("claude-opus-4-8") == 1
    # Live-only entry is preserved (discovery still works for unknown models).
    assert "claude-future-9-99" in result
    # Curated entries lead, live-only trails.
    assert result.index("claude-fable-5-1") < result.index("claude-future-9-99")
    assert result.index("claude-opus-5") < result.index("claude-future-9-99")


def test_anthropic_falls_back_to_curated_when_live_unavailable():
    """No creds / live failure -> curated list verbatim (alias still present)."""
    with patch.object(M, "_fetch_anthropic_models", return_value=None):
        result = M.provider_model_ids("anthropic")

    assert result == list(M._PROVIDER_MODELS["anthropic"])
    assert "claude-fable-5-1" in result
    assert "claude-opus-5" in result
    assert "claude-fable-5" in result


def test_anthropic_curated_list_uses_native_dash_ids_only():
    """Every curated Anthropic entry uses the native dash-separated model ID.

    Anthropic's own API (platform.claude.com/docs) ALWAYS uses dashes for its native model
    IDs (``claude-opus-5-5``, ``claude-fable-5-1``) — the dotted form (``claude-opus-5.5``) is
    the OpenRouter/aggregator slug convention for the exact same model, used only inside the
    ``anthropic/...`` OpenRouter namespace. A curated entry here written with a dot silently
    survives review (case-only dedup lets it coexist with the real dashed id returned by live
    ``/v1/models``), producing a duplicate row in every model picker (#claude-opus-5.5 dup,
    2026-09-23, from copy-pasting the OpenRouter slug into this table instead of the native
    one). This test fails loudly the next time that happens.
    """
    for model in M._PROVIDER_MODELS["anthropic"]:
        # A version-looking dotted segment (digit.digit) after "claude" is the OpenRouter/
        # aggregator convention leaking into the native-Anthropic table — reject it. Trailing
        # dated suffixes like "claude-haiku-4-5-20251001" have no such dot, so they're unaffected.
        assert not re.search(r"claude-[\w-]*\d\.\d", model), (
            f"{model!r} looks like an OpenRouter-style dotted slug, not a native Anthropic "
            "model ID (native IDs use dashes: claude-opus-5-5, not claude-opus-5.5)"
        )


def test_anthropic_merge_folds_dot_dash_variants_of_the_same_model():
    """A curated dotted id and a live dashed id for the SAME model must merge to one row.

    Defense in depth for the bug above: even if a dotted typo slips into the curated table
    again, the merge's dedup key folds "." and "-" so the live dashed id can't produce a
    second row.
    """
    curated_with_typo = [
        "claude-opus-5.5" if m == "claude-opus-5-5" else m for m in M._PROVIDER_MODELS["anthropic"]
    ]
    live = ["claude-opus-5-5", "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
    with patch.object(M, "_PROVIDER_MODELS", {**M._PROVIDER_MODELS, "anthropic": curated_with_typo}), \
         patch.object(M, "_fetch_anthropic_models", return_value=live):
        result = M.provider_model_ids("anthropic")

    lowered = [str(m).lower() for m in result]
    assert lowered.count("claude-opus-5.5") + lowered.count("claude-opus-5-5") == 1, (
        f"dot/dash variants of the same model produced duplicate rows: {result!r}"
    )
