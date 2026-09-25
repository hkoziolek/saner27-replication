"""ID reconciliation: bind hand-crafted element ids to extracted stable ids (§7.5).

Hand-authored element ids (``orderProcessor``, ``Order Processor``, ``OPSvc``) do
**not** line up with the pipeline's build-derived stable ids (``cpp:target:OrderProcessor``,
§6.3) — and the §4.5.3 honest caveat is that this is a genuine entity-matching problem
with no perfectly reliable solution. So this module only *proposes* bindings, deterministically
and heuristically; a human confirms them in ``rules/human_id_bindings.yaml``.

The HARD RULE (§7.5, §11.5, §11.6): **no ``confirmed`` binding, no downstream use.**
:func:`confirmed` returns *only* ``status == "confirmed"`` entries; everything else
(``proposed``, absent) is invisible to seeding (§7.4) and baseline reconciliation (§11.5).
A wrong auto-binding silently transplants a human's curated name/position onto the wrong
code element and corrupts the layout-merge (§9.1) — strictly worse than an honest "unmatched".

Pure functions only: no I/O beyond :func:`load_bindings`, no timestamps, no randomness.
Scores are rounded to 3 decimals so rendered output is byte-stable (determinism is a hard
CI gate, §1.1 #2).
"""
from __future__ import annotations

import copy
import difflib
import re
from pathlib import Path
from typing import Any

import yaml

# A proposal must clear this floor to be emitted as a candidate; below it the human
# element is reported unmatched (``extracted_id: None``) rather than guessed at (§7.5).
SCORE_FLOOR = 0.4

# Weights for the three deterministic signals (§7.5 (a)/(b)/(c)). Slug equality is the
# strongest, path/namespace overlap next, raw name-similarity the weakest.
_W_SLUG = 1.0
_W_PATH = 0.85
_W_NAME = 0.7

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_PATH_TOKEN = re.compile(r"[^0-9a-z]+")


def _norm_slug(text: str) -> str:
    """Lowercase, strip every non-alphanumeric char (§7.5 normalized slug equality).

    ``Order Processor`` / ``order_processor`` / ``OrderProcessor`` all collapse to
    ``orderprocessor`` — deterministic and cross-OS stable (no locale-sensitive casing).
    """
    return _NON_ALNUM.sub("", (text or "").lower())


def _path_tokens(text: str) -> set[str]:
    """Lowercased alnum path/namespace tokens, splitting on ``/``, ``\\``, ``.``, ``::`` etc.

    ``src/order_processor`` -> ``{"src", "order", "processor"}`` so a human element naming a
    folder can overlap a target's ``path`` / ``namespaces`` (§7.5 (b)). Single-letter and
    purely-numeric noise tokens are dropped.
    """
    raw = (text or "").lower().replace("\\", "/")
    toks = {t for t in _PATH_TOKEN.split(raw) if len(t) > 1 and not t.isdigit()}
    return toks


def _stable_key(target_id: str) -> str:
    """The trailing key segment of a stable id, slug-normalized.

    ``cpp:target:OrderProcessor`` -> ``orderprocessor``;
    ``csharp:csproj:src/order_processor/Op.csproj`` -> ``oporderprocessorsrccsproj`` is *not*
    wanted, so we take only the **basename** of a path-like key (mirroring §9 slugging) before
    normalizing: ``Op.csproj`` -> ``opcsproj``. The path itself is matched separately via (b).
    """
    segments = [s for s in str(target_id).split(":") if s]
    key = segments[-1] if segments else str(target_id)
    basename = key.replace("\\", "/").rsplit("/", 1)[-1]
    return _norm_slug(basename)


def _human_aliases(human: dict) -> list[str]:
    """Candidate slug surfaces for a human element: its id/key and its display name (§7.5)."""
    out: list[str] = []
    for field_name in ("id", "key", "name"):
        val = human.get(field_name)
        if isinstance(val, str) and val.strip():
            out.append(val)
    return out


def human_id_of(human: dict) -> str:
    """The human element's authored id (``id`` or ``key``), falling back to ``name`` (§7.5)."""
    for field_name in ("id", "key"):
        val = human.get(field_name)
        if isinstance(val, str) and val.strip():
            return val
    name = human.get("name")
    return name if isinstance(name, str) else ""


def _name_similarity(a: str, b: str) -> float:
    """Edit-distance-ish name similarity via :class:`difflib.SequenceMatcher` (stdlib, §7.5 (c))."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _score_candidate(human: dict, target: dict) -> tuple[float, str]:
    """Score one (human, target) pair; return ``(score 0..1, evidence)`` — deterministic.

    Combines the three §7.5 signals and reports the single strongest as the evidence string,
    so the proposal is auditable. The score is the max of the per-signal contributions (a
    strong slug match should not be diluted by a weak name match), then rounded to 3 dp.
    """
    aliases = _human_aliases(human)
    human_slugs = {_norm_slug(a) for a in aliases if _norm_slug(a)}

    target_name = str(target.get("name", ""))
    target_id = str(target.get("id", ""))
    target_slugs = {s for s in (_norm_slug(target_name), _stable_key(target_id)) if s}

    best_score = 0.0
    best_evidence = "no signal"

    # (a) exact/normalized slug equality between human id/name and target name or stable-key.
    if human_slugs & target_slugs:
        matched = sorted(human_slugs & target_slugs)[0]
        score = _W_SLUG
        if score > best_score:
            best_score = score
            best_evidence = f"slug-equality: {matched!r}"

    # (b) path / namespace overlap.
    human_path_text = " ".join(
        str(human.get(k, ""))
        for k in ("path", "group", "description")
        if human.get(k)
    )
    human_tokens = _path_tokens(human_path_text) | {t for a in aliases for t in _path_tokens(a)}
    target_path_text = " ".join(
        [str(target.get("path", ""))] + [str(n) for n in (target.get("namespaces") or [])]
    )
    target_tokens = _path_tokens(target_path_text)
    if human_tokens and target_tokens:
        overlap = human_tokens & target_tokens
        if overlap:
            jaccard = len(overlap) / len(human_tokens | target_tokens)
            score = round(_W_PATH * jaccard, 6)
            if score > best_score:
                best_score = score
                shown = ", ".join(sorted(overlap)[:4])
                best_evidence = f"path/namespace-overlap: {{{shown}}}"

    # (c) token / edit-distance name similarity (weaker signal).
    name_ratio = 0.0
    for slug in human_slugs:
        for tslug in target_slugs:
            name_ratio = max(name_ratio, _name_similarity(slug, tslug))
    if name_ratio:
        score = round(_W_NAME * name_ratio, 6)
        if score > best_score:
            best_score = score
            best_evidence = f"name-similarity: {round(name_ratio, 3)}"

    return round(best_score, 3), best_evidence


def propose_bindings(
    human_elements: list[dict], targets: list[dict]
) -> list[dict]:
    """Propose at most one extracted target per hand-crafted element (§7.5, deterministic).

    For each human element we score every extracted target via the three §7.5 signals and
    keep only the **best** candidate above :data:`SCORE_FLOOR`. A human element with no
    candidate clearing the floor is reported *unmatched* (``extracted_id: None``) rather than
    guessed at — the §4.5.3 honest-failure rule (an unmatched element may be aspirational,
    stale, or above the build graph's altitude).

    Returns a list of ``{human_id, extracted_id, score, evidence, status: "proposed"}``,
    sorted deterministically by ``human_id`` then ``extracted_id`` so the output is byte-stable.
    Every proposal is ``status: "proposed"`` — a human promotes it to ``confirmed`` (§7.5).
    """
    proposals: list[dict] = []
    for human in human_elements:
        h_id = human_id_of(human)
        # Score against every target, deterministically tie-broken by (−score, target id).
        scored: list[tuple[float, str, str]] = []
        for target in targets:
            score, evidence = _score_candidate(human, target)
            t_id = str(target.get("id", ""))
            scored.append((score, evidence, t_id))
        # Best score wins; ties broken by lexicographic target id for stability.
        scored.sort(key=lambda s: (-s[0], s[2]))
        if scored and scored[0][0] >= SCORE_FLOOR:
            best_score, best_evidence, best_id = scored[0]
            proposals.append(
                {
                    "human_id": h_id,
                    "extracted_id": best_id,
                    "score": best_score,
                    "evidence": best_evidence,
                    "status": "proposed",
                }
            )
        else:
            proposals.append(
                {
                    "human_id": h_id,
                    "extracted_id": None,
                    "score": 0.0,
                    "evidence": "no candidate above floor",
                    "status": "proposed",
                }
            )

    proposals.sort(key=lambda p: (p["human_id"], p["extracted_id"] or ""))
    return proposals


def load_bindings(path: Path) -> dict:
    """Load ``human_id_bindings.yaml`` (§7.5 shape). Absent file -> ``{}``.

    Shape: ``{bindings: {humanId: {extracted_id, status, confidence}}}``. No defaults are
    invented; an absent or empty file yields an empty mapping so callers degrade to "no
    confirmed bindings" rather than crashing.
    """
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        return {}
    return data


def confirmed(bindings: dict) -> dict[str, str]:
    """Return ONLY ``status == "confirmed"`` bindings as ``{human_id: extracted_id}`` (§7.5).

    This is the gate enforcing the hard rule **no ``confirmed`` binding, no downstream use**
    (§7.5, §11.5, §11.6): ``proposed`` / absent / malformed entries and any entry missing an
    ``extracted_id`` are silently dropped, so an unconfirmed match can never masquerade as an
    honored binding in seeding (§7.4) or baseline reconciliation (§11.5).
    """
    out: dict[str, str] = {}
    table = (bindings or {}).get("bindings", {}) or {}
    if not isinstance(table, dict):
        return out
    for human_id, entry in table.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "confirmed":
            continue
        extracted_id = entry.get("extracted_id")
        if isinstance(extracted_id, str) and extracted_id:
            out[str(human_id)] = extracted_id
    return out


# §7.5 confidence bands, strongest first. `bulk_accept` uses this ordering for the `>=band`
# comparison: high > medium > low, and ">= band" means at-or-above the requested band.
_BAND_ORDER = ("high", "medium", "low")
_BAND_RANK = {band: rank for rank, band in enumerate(_BAND_ORDER)}  # high=0 (strongest)


def bulk_accept(bindings: dict, min_band: str) -> tuple[dict, int]:
    """Promote ``proposed`` bindings at-or-above ``min_band`` to ``confirmed`` (T2.7, §7.5).

    Backs ``arch reconcile --accept-all >=<band>`` — the bulk-confirm that retires the
    one-by-one confirmation tax. Given a loaded bindings dict (shape
    ``{bindings: {human: {extracted_id, status, confidence}}}``) and a band
    (``high|medium|low``), return a NEW bindings dict in which every entry that is currently
    ``status: proposed`` AND whose ``confidence`` band is **at or above** ``min_band`` is
    flipped to ``status: confirmed``, plus the count promoted.

    Band ordering is ``high > medium > low``; "``>= band``" is inclusive (``>=medium`` promotes
    ``high`` and ``medium`` but not ``low``). An already-``confirmed`` entry is left untouched
    (so re-running is idempotent); an entry missing/with an unknown ``confidence`` band, or
    missing an ``extracted_id``, is never auto-confirmed (conservative — a wrong auto-confirm
    transplants curation onto the wrong element, §7.4). Deterministic: a deep copy is mutated in
    sorted-key order, so the result and count never depend on dict insertion order.
    """
    out = copy.deepcopy(bindings) if isinstance(bindings, dict) else {}
    threshold = _BAND_RANK.get((min_band or "").strip().lower())
    if threshold is None:
        # unknown band requested -> promote nothing rather than guess (fail-safe).
        return out, 0
    table = out.get("bindings")
    if not isinstance(table, dict):
        return out, 0
    promoted = 0
    for human_id in sorted(table):
        entry = table[human_id]
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "proposed":
            continue
        extracted_id = entry.get("extracted_id")
        if not (isinstance(extracted_id, str) and extracted_id):
            continue  # nothing to confirm a binding TO -> skip (honest unmatched)
        rank = _BAND_RANK.get(str(entry.get("confidence", "")).strip().lower())
        if rank is None or rank > threshold:
            # unknown band, or strictly below the requested floor -> leave proposed.
            continue
        entry["status"] = "confirmed"
        promoted += 1
    return out, promoted


def bindings_to_yaml(proposed: list[dict]) -> str:
    """Render proposals into the ``human_id_bindings.yaml`` shape, all ``status: proposed`` (§7.5).

    A human edits ``proposed`` -> ``confirmed`` in place; only then is the binding honored
    (:func:`confirmed`). Unmatched human elements (``extracted_id: None``) are emitted with a
    ``null`` ``extracted_id`` so the architect sees them and can hand-bind or leave them as an
    honest §11.5 ``unreconciled`` finding. Deterministic ``yaml.safe_dump(sort_keys=True)``.
    """
    table: dict[str, Any] = {}
    for p in proposed:
        human_id = p.get("human_id", "")
        table[str(human_id)] = {
            "extracted_id": p.get("extracted_id"),
            "status": "proposed",
            "confidence": _confidence_band(p.get("score", 0.0)),
        }
    doc = {"bindings": table}
    return yaml.safe_dump(doc, sort_keys=True, default_flow_style=False, allow_unicode=True)


def render_reconciliation_table(proposed: list[dict]) -> str:
    """A Markdown ``id-reconciliation`` table for the curation proposal (§7.2 B / §7.5).

    Columns: ``human_id | extracted_id | score | evidence | accept?`` — the ``accept?`` column
    is an empty checkbox the architect ticks when confirming. Deterministic (input is already
    sorted by :func:`propose_bindings`); unmatched elements render with ``—``.
    """
    lines = [
        "| human_id | extracted_id | score | evidence | accept? |",
        "|---|---|---|---|---|",
    ]
    for p in proposed:
        human_id = str(p.get("human_id", ""))
        extracted = p.get("extracted_id")
        extracted_cell = str(extracted) if extracted else "—"
        score = p.get("score", 0.0)
        evidence = str(p.get("evidence", ""))
        lines.append(
            f"| {human_id} | {extracted_cell} | {score:.3f} | {evidence} | [ ] |"
        )
    return "\n".join(lines) + "\n"


def _confidence_band(score: float) -> str:
    """Map a numeric score to the §7.5 ``high | medium | low`` audit band (retained, not gating)."""
    if score >= 0.85:
        return "high"
    if score >= 0.6:
        return "medium"
    return "low"
