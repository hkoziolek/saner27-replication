"""ADR adoption — rationale, never structure (plan §4.5.4).

ADRs are decisions and their rationale; they are emphatically **not** a structural source
(principle #3: LLM for semantics only; principle #4: structure comes from the build graph).
The pipeline **never** extracts relationships from ADR prose. This module implements the
four *bounded, advisory* uses §4.5.4 allows, all deterministic and human-reviewable:

  1. **Bounded naming context** (:func:`naming_context`): an element's relevant ADR titles
     (+status) are offered to the §8 enrichment step as *context* to sharpen a description —
     bounded, cited, reconciled like any other enrichment.
  2. **Link, don't restructure** (:func:`link_adrs_to_targets` → ``adr:<id>`` element tags):
     where an ADR *names* an element, annotate that element with the ADR id so a view can
     show "decisions touching this element". This adds a *reference*, never an edge.
  3. **Coexist with the repo's ADR convention**: detection/location is the discovery pass's
     job (§4.5.1, :mod:`anon.discover`); this module consumes whatever it found.
  4. **ADR-stated constraints → *candidate* fitness rules** (:func:`candidate_layering_rules`):
     a structural constraint stated in an ADR ("the UI layer must not call the database") is
     surfaced as a *candidate* ``layering-rules.yaml`` entry — **proposed for human promotion,
     never auto-enforced**. Until a human promotes it, it is a suggestion, not a CI gate.

Nothing here lets ADR text invent an element or a relationship; the worst a misread ADR can
do is propose a name, a tag, or a *candidate* rule a human then rejects (§4.5.4).

Determinism is a hard CI gate (§1.1 #2): matching is purely a function of the discovered
ADRs + the extracted targets, every collection is sorted, and the extracted-facts rewrite
goes through :func:`model.canonicalize` + :func:`jsonio.dump_json`. A repo with **no** ADRs
is a strict no-op — nothing is written, so the toy goldens stay byte-identical.
"""
from __future__ import annotations

import argparse
import hashlib
import math
import re
from typing import Any

from .jsonio import dump_json, dumps_json, load_json
from .model import canonicalize
from .paths import Workspace, resolve_workspace

# A short single-token element name must be at least this long to anchor an ADR match,
# so a generic "App"/"Lib"/"Core" target does not get tagged by an unrelated ADR title.
_MIN_SINGLE_TOKEN = 4

_WORD_RE = re.compile(r"[0-9A-Za-z]+")

# §4.5.4 (4) candidate-constraint sniff: "<A> must|should|may|shall not|never
# call|use|depend on|access|reference|import|include <B>". Best-effort, advisory.
_CONSTRAINT_RE = re.compile(
    r"(?P<from>[\w .\-/]+?)\s+(?:must|should|may|shall)\s+(?:not|never)\s+"
    r"(?:call|use|access|reference|import|include|talk\s+to|depend\s+(?:on|upon))\s+"
    r"(?:the\s+|a\s+|an\s+)?(?P<to>[\w .\-/]+)",
    re.IGNORECASE,
)


def _words(text: str) -> list[str]:
    """Lowercased alphanumeric word tokens of *text* (deterministic, locale-independent)."""
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """True iff *needle* appears as a CONTIGUOUS sublist of *haystack* (token-level)."""
    if not needle or len(needle) > len(haystack):
        return False
    first = needle[0]
    for i in range(len(haystack) - len(needle) + 1):
        if haystack[i] == first and haystack[i:i + len(needle)] == needle:
            return True
    return False


def _title_mentions(target_name: str, title_words: list[str]) -> bool:
    """Conservative match: the target's display name appears (as a token run) in the title.

    A single-token name must clear :data:`_MIN_SINGLE_TOKEN` so a generic name doesn't
    over-match; a multi-token name must appear as a contiguous run (§4.5.4 (2) — link only
    where an ADR genuinely *names* the element)."""
    name_words = _words(target_name)
    if not name_words:
        return False
    if len(name_words) == 1:
        tok = name_words[0]
        return len(tok) >= _MIN_SINGLE_TOKEN and tok in title_words
    return _is_subsequence(name_words, title_words)


def link_adrs_to_targets(adrs: list[dict[str, Any]],
                         targets: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Map each target id -> sorted ADR ids whose title names it (§4.5.4 (2), deterministic).

    Conservative name match only — never a structural inference. A target the ADRs don't
    name maps to nothing (it is simply absent from the result)."""
    title_tokens = [(a.get("id", ""), _words(a.get("title", ""))) for a in adrs]
    out: dict[str, list[str]] = {}
    for t in targets:
        name = t.get("name", "")
        hits = sorted({adr_id for adr_id, words in title_tokens
                       if adr_id and _title_mentions(name, words)})
        if hits:
            out[t["id"]] = hits
    return out


def naming_context(adrs: list[dict[str, Any]], target_name: str) -> list[str]:
    """ADR titles (+status) that name *target_name* — bounded §8 enrichment context (§4.5.4 (1)).

    Returned as short ``"<title> [status]"`` strings, sorted and deduped. The enrichment step
    may include these as DATA (never instructions, §8.3); they are advisory and cited."""
    title_words_cache = [(a, _words(a.get("title", ""))) for a in adrs]
    out: list[str] = []
    for a, words in title_words_cache:
        if _title_mentions(target_name, words):
            title = a.get("title", "").strip()
            status = a.get("status")
            out.append(f"{title} [{status}]" if status else title)
    return sorted(set(out))


def candidate_layering_rules(adrs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Surface ADR-stated structural constraints as *candidate* fitness rules (§4.5.4 (4)).

    Best-effort regex over each ADR title; returns ``{from, to, because, adr}`` rows. These
    are **candidates for human promotion** into ``layering-rules.yaml`` (§11.2), never
    auto-enforced. Deterministic: sorted by ``(from, to, adr)``."""
    out: list[dict[str, str]] = []
    for a in adrs:
        title = a.get("title", "") or ""
        m = _CONSTRAINT_RE.search(title)
        if not m:
            continue
        frm = m.group("from").strip(" .-/").strip()
        to = m.group("to").strip(" .-/").strip()
        if not frm or not to:
            continue
        out.append({"from": frm, "to": to, "because": title.strip(), "adr": a.get("id", "")})
    out.sort(key=lambda r: (r["from"].lower(), r["to"].lower(), r["adr"]))
    return out


def _render_candidates_md(candidates: list[dict[str, str]]) -> str:
    """Markdown for the ADR-derived candidate layering rules (advisory, for promotion)."""
    lines = ["# ADR-derived candidate layering rules (§4.5.4 (4))", "",
             "_Advisory only — a human PROMOTES a candidate into `rules/layering-rules.yaml`; "
             "the pipeline never auto-enforces an ADR-extracted constraint (§11.2)._", ""]
    if not candidates:
        lines += ["_No structural constraints detected in ADR titles._", ""]
        return "\n".join(lines)
    lines += ["```yaml", "forbidden:"]
    for c in candidates:
        lines += [f'  - from: "{c["from"]}"',
                  f'    to:   "{c["to"]}"',
                  f'    # because (adr:{c["adr"]}): {c["because"]}']
    lines += ["```", ""]
    return "\n".join(lines)


def run(ws: Workspace) -> dict[str, Any]:
    """Apply ADR adoption to the extracted facts (§4.5.4); advisory + additive.

    Reads the discovery report (``discovered-artifacts.json``, written by
    :mod:`anon.discover`; falls back to a fresh discovery scan if absent), then:
      - tags each named target ``adr:<id>`` (§4.5.4 (2)) and rewrites ``extracted-facts.json``
        (canonicalized) — but **only when at least one tag is added**;
      - writes ``adr-candidate-rules.md`` when constraints are found (§4.5.4 (4)).

    A repo with **no ADRs** is a strict no-op (no file is written), so the determinism /
    golden guarantee is preserved. Never raises on a missing facts file or odd input."""
    if ws.discovered_artifacts.exists():
        try:
            report = load_json(ws.discovered_artifacts)
        except (OSError, ValueError):
            report = {}
    else:
        from . import discover
        report = discover.discover(ws.repo, exclude_dirs=[ws.arch_dir])
    adrs = (report or {}).get("adrs", []) or []
    summary = {"adrs": len(adrs), "tagged": 0, "candidates": 0}
    if not adrs or not ws.extracted_facts.exists():
        return summary

    facts = load_json(ws.extracted_facts)
    targets = facts.get("targets", [])
    links = link_adrs_to_targets(adrs, targets)
    tagged = 0
    for t in targets:
        adr_ids = links.get(t["id"])
        if adr_ids:
            tags = set(t.get("tags", [])) | {f"adr:{a}" for a in adr_ids}
            t["tags"] = sorted(tags)
            tagged += 1

    candidates = candidate_layering_rules(adrs)
    if candidates:
        ws.adr_candidates_md.parent.mkdir(parents=True, exist_ok=True)
        ws.adr_candidates_md.write_text(_render_candidates_md(candidates),
                                        encoding="utf-8", newline="")

    if tagged:
        # rewrite canonicalized so the adr: tags land deterministically (§6.4d).
        dump_json(canonicalize(facts), ws.extracted_facts)

    summary["tagged"] = tagged
    summary["candidates"] = len(candidates)
    return summary


# =================================================================== conformance (§2a)
# Engine plan §2a: ingest hand-authored ADRs and CHECK them against the live model.
# Constraint extraction runs over the ADR TITLE **and BODY** (sentence-level) — the
# §2a.7 prerequisite: real ADRs state the constraint in the body ("## Decision"),
# rarely in the title, and the dangerous error is the silent false negative. Conformance
# is evaluated with drift_report.check_layering — the same forbidden-edge engine
# `verify` uses — so an ADR constraint is checked IDENTICALLY to a hand-written
# layering rule, including the coverage-conditioned UNVERIFIED semantics (§2a.2).

CONFORMANCE_SCHEMA = "adr-conformance/1"
_ADR_BODY_LIMIT = 256 * 1024          # bytes of ADR body we scan
_FVC_MAX_SNAPSHOTS = 200              # first-violating-commit walk cap (oldest first)

# sentence-ish chunks: markdown lines and prose sentences both become scan units.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_MARKUP = re.compile(r"[*_`]")

# A leading MADR/YAML front-matter block (``---\n…\n---``). Stripped from the body we
# carry into the conformance artifact for UI rendering so the prose starts at its first
# heading (status is already extracted by discover); deterministic, no-op when absent.
_FRONT_MATTER = re.compile(r"^---\r?\n.*?\r?\n---\r?\n?", re.DOTALL)


def _adr_prose(body: str | None) -> str | None:
    """The ADR body with any leading front-matter removed — the source rendered verbatim
    in the §5.3 Decisions screen (§2a.7: we show the human's prose, never a paraphrase)."""
    if not body:
        return None
    return _FRONT_MATTER.sub("", body, count=1).lstrip("\n") or None


def extract_constraints(title: str, body: str | None) -> list[dict[str, str]]:
    """Structural ``<A> must not <verb> <B>`` constraints from an ADR's title AND body.

    Sentence-level scan with markdown emphasis stripped (so "**must not**" matches).
    Returns ``{from_raw, to_raw, because, source}`` rows, deduped case-insensitively,
    sorted. Raw tokens are prose — resolution against the model happens in
    :func:`check_conformance` (an unresolvable token is UNVERIFIED, never guessed).
    """
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _scan(text: str, source: str) -> None:
        for sentence in _SENTENCE_SPLIT.split(_MARKUP.sub("", text or "")):
            sentence = sentence.strip()
            if not sentence:
                continue
            m = _CONSTRAINT_RE.search(sentence)
            if not m:
                continue
            frm = m.group("from").strip(" .-/").strip()
            to = m.group("to").strip(" .-/").strip()
            if not frm or not to:
                continue
            key = (frm.lower(), to.lower())
            if key in seen:
                continue
            seen.add(key)
            rows.append({"from_raw": frm, "to_raw": to,
                         "because": sentence, "source": source})

    _scan(title or "", "title")
    if body:
        _scan(body, "body")
    rows.sort(key=lambda r: (r["from_raw"].lower(), r["to_raw"].lower(), r["source"]))
    return rows


def _element_lookup(targets: list[dict[str, Any]]) -> dict[str, str]:
    """lowercase name/id → the canonical token ``check_layering`` matches on
    (container id, container name, or target id — :func:`_matches_container`)."""
    lookup: dict[str, str] = {}
    for t in sorted(targets, key=lambda t: t.get("id", "")):
        tid = t.get("id")
        if not tid:
            continue
        cid = t.get("container_id") or tid
        cname = t.get("container_name") or t.get("name") or cid
        lookup.setdefault(cid.lower(), cid)
        lookup.setdefault(cname.lower(), cname)
        lookup.setdefault(tid.lower(), tid)
        if t.get("name"):
            lookup.setdefault(t["name"].lower(), tid)
    return lookup


def _resolve_token(token: str, lookup: dict[str, str], *, side: str) -> str | None:
    """Resolve a prose token to a model element, trimming the regex's noisy capture.

    The constraint regex captures from the sentence start ("We decided that the Web
    tier must not …" → from_raw "We decided that the Web tier"), so the *from* side
    tries progressively shorter SUFFIXES and the *to* side progressively shorter
    PREFIXES, longest first — the longest piece of prose that names a real element
    wins. No match → ``None`` (UNVERIFIED, never a guess)."""
    words = token.split()
    if side == "from":
        candidates = [" ".join(words[i:]) for i in range(len(words))]
    else:
        candidates = [" ".join(words[:len(words) - i]) for i in range(len(words))]
    for cand in candidates:
        c = cand.strip(" .-/").strip().lower()
        if c and c in lookup:
            return lookup[c]
    return None


def _git(repo, *args: str, timeout: int = 60) -> str | None:
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                             text=True, timeout=timeout, encoding="utf-8",
                             errors="replace")
        if out.returncode == 0:
            return out.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def first_violating_commit(ws: Workspace, frm: str, to: str,
                           since: str | None = None) -> str | None:
    """The first commit at which the committed ``extracted-facts.json`` snapshot shows
    the forbidden edge (engine §2a / §0.6: committed snapshots ONLY — re-extracting
    history is non-deterministic and forbidden). ``None`` = history unavailable, the
    normal answer on out-of-tree pilots. *since* (a git ref) bounds the walk to
    ``<since>..HEAD``."""
    import json

    from .stages.drift_report import check_layering
    try:
        rel = ws.extracted_facts.resolve().relative_to(ws.repo.resolve())
    except ValueError:
        return None  # arch-dir redirected out of tree — no committed snapshots
    relp = rel.as_posix()
    range_args = [f"{since}..HEAD"] if since else []
    log = _git(ws.repo, "log", "--format=%H", "--reverse", *range_args, "--", relp)
    if not log or not log.strip():
        return None
    for commit in log.split()[:_FVC_MAX_SNAPSHOTS]:
        content = _git(ws.repo, "show", f"{commit}:{relp}")
        if not content:
            continue
        try:
            snapshot = json.loads(content)
        except ValueError:
            continue
        findings = check_layering(snapshot, {"forbidden": [{"from": frm, "to": to}]})
        if findings and findings[0]["status"] == "VIOLATION":
            return commit
    return None


def check_conformance(ws: Workspace, *, history: bool = True,
                      baseline_path: str | None = None,
                      since: str | None = None) -> dict[str, Any] | None:
    """The ``arch adr check`` engine (§2a): per ADR, the linked elements, the
    interpreted constraint(s), and HELD | VIOLATED | UNVERIFIED | NOT-A-CONSTRAINT —
    evaluated by the same ``check_layering`` machinery as ``arch verify``, with the
    coverage-conditioned no-cry-wolf downgrade (a degraded run never gates).

    *baseline_path* checks the ADRs against a given facts snapshot instead of the
    workspace's current model (§2a.4 ``--baseline``); *since* bounds the
    first-violating-commit walk (§2a.4 ``--since``). Returns ``None`` when no fact
    model exists. Writes nothing — the writing wrapper is :func:`run_check`.
    """
    from pathlib import Path

    from .stages.drift_report import COVERAGE_FLOOR, _coverage_l0, check_layering
    from .model import content_hash

    if ws.discovered_artifacts.exists():
        try:
            report = load_json(ws.discovered_artifacts)
        except (OSError, ValueError):
            report = {}
    else:
        from . import discover
        report = discover.discover(ws.repo, exclude_dirs=[ws.arch_dir])
    adrs = (report or {}).get("adrs", []) or []

    if baseline_path:
        facts_path = Path(baseline_path)
    elif ws.curated_facts.exists():
        facts_path = ws.curated_facts
    else:
        facts_path = ws.extracted_facts
    if not facts_path.exists():
        return None
    facts = load_json(facts_path)
    targets = facts.get("targets", []) or []
    lookup = _element_lookup(targets)
    links = link_adrs_to_targets(adrs, targets)
    coverage_l0 = _coverage_l0(facts)
    advisory = coverage_l0 < COVERAGE_FLOOR

    rel_index = {(r.get("source"), r.get("target")): r
                 for r in facts.get("relationships", []) or []}

    out_adrs: list[dict[str, Any]] = []
    for adr in sorted(adrs, key=lambda a: (a.get("id") or "", a.get("path") or "")):
        path = ws.repo / (adr.get("path") or "")
        body: str | None
        parse = "ok"
        try:
            body = path.read_text(encoding="utf-8", errors="replace")[:_ADR_BODY_LIMIT]
        except OSError:
            body, parse = None, "unreadable"
        constraints = extract_constraints(adr.get("title") or "", body)

        rows: list[dict[str, Any]] = []
        for c in constraints:
            frm = _resolve_token(c["from_raw"], lookup, side="from")
            to = _resolve_token(c["to_raw"], lookup, side="to")
            row: dict[str, Any] = {**c, "from": frm, "to": to}
            if frm is None or to is None:
                missing = c["from_raw"] if frm is None else c["to_raw"]
                row["status"] = "UNVERIFIED"
                row["reason"] = f"'{missing}' does not name a model element"
                row["offending_edges"] = []
            else:
                finding = check_layering(facts,
                                         {"forbidden": [{"from": frm, "to": to}]})[0]
                status = {"VIOLATION": "VIOLATED", "OK": "HELD"}.get(
                    finding["status"], "UNVERIFIED (degraded)")
                row["status"] = status
                row["offending_edges"] = [
                    {"source": s, "target": t,
                     "evidence_strength": (rel_index.get((s, t)) or {})
                     .get("evidence_strength", "")}
                    for s, t in finding["edges"]]
                if status == "VIOLATED" and history:
                    fvc = first_violating_commit(ws, frm, to, since=since)
                    row["first_violating_commit"] = fvc or "history unavailable"
            rows.append(row)

        statuses = {r["status"] for r in rows}
        if parse == "unreadable":
            verdict = "UNVERIFIED"
            note = "ADR file unreadable — could not parse a constraint"
        elif not rows:
            verdict = "NOT-A-CONSTRAINT"
            note = ("checked title + body — none found (no structural constraint "
                    "sentence; quality-attribute decisions are out of scope, §2a.7)")
        elif "VIOLATED" in statuses:
            verdict, note = "VIOLATED", ""
        elif statuses & {"UNVERIFIED", "UNVERIFIED (degraded)"}:
            verdict, note = "UNVERIFIED", ""
        else:
            verdict, note = "HELD", ""
        out_adrs.append({"id": adr.get("id"), "title": adr.get("title"),
                         "adr_status": adr.get("status"), "path": adr.get("path"),
                         "parse": parse, "verdict": verdict, "note": note,
                         # the human's full prose (front-matter stripped) so the GUI can
                         # render the whole decision, not just the interpreted constraint.
                         "body": _adr_prose(body),
                         "linked_elements": sorted(
                             tid for tid, ids in links.items()
                             if adr.get("id") in ids),
                         "constraints": rows})

    counts: dict[str, int] = {"HELD": 0, "VIOLATED": 0, "UNVERIFIED": 0,
                              "NOT-A-CONSTRAINT": 0}
    for a in out_adrs:
        counts[a["verdict"]] = counts.get(a["verdict"], 0) + 1
    return {
        "schema": CONFORMANCE_SCHEMA,
        "derived_from_hash": content_hash(facts),
        "coverage": {"l0_coverage": round(coverage_l0, 4), "floor": COVERAGE_FLOOR,
                     "advisory": advisory},
        "note": ("constraints are extracted from ADR title AND body text "
                 "(sentence-level); 'checked, none found' is distinguished from "
                 "'could not parse' (§2a.7); a below-floor run downgrades VIOLATED "
                 "to advisory — never a gate over a degraded model (§2a.3)"),
        "adrs": out_adrs,
        "summary": counts,
        "gating": counts["VIOLATED"] > 0 and not advisory,
    }


def render_conformance(report: dict[str, Any]) -> str:
    s = report["summary"]
    cov = report["coverage"]
    lines = ["# ADR conformance report  (`arch adr check`)", "",
             f"> {report['note']}", "",
             f"- {len(report['adrs'])} ADR(s): **{s['VIOLATED']} VIOLATED** · "
             f"{s['HELD']} HELD · {s['UNVERIFIED']} UNVERIFIED · "
             f"{s['NOT-A-CONSTRAINT']} not-a-constraint",
             f"- L0 coverage {cov['l0_coverage']:.0%} (floor {cov['floor']:.0%})"
             + (" — **ADVISORY: degraded run, findings are not gating**"
                if cov["advisory"] else ""), ""]
    if not report["adrs"]:
        lines += ["_No ADRs discovered (docs/adr, doc/adr, adr/, decisions/ …)._", ""]
    for a in report["adrs"]:
        lines += [f"## [{a['verdict']}] {a['id'] or '?'} — {a['title'] or a['path']}", ""]
        if a["note"]:
            lines.append(f"_{a['note']}_")
        if a["linked_elements"]:
            lines.append("- linked elements: "
                         + ", ".join(f"`{e}`" for e in a["linked_elements"]))
        for c in a["constraints"]:
            frm = c["from"] or f"?({c['from_raw']})"
            to = c["to"] or f"?({c['to_raw']})"
            lines.append(f"- **{c['status']}** `{frm}` -/-> `{to}` "
                         f"({c['source']}: “{c['because']}”)")
            if c.get("reason"):
                lines.append(f"  - {c['reason']}")
            for e in c.get("offending_edges", []):
                lines.append(f"  - offending: `{e['source']}` → `{e['target']}` "
                             f"({e['evidence_strength']})")
            if c.get("first_violating_commit"):
                lines.append(f"  - first violating commit: "
                             f"{c['first_violating_commit']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_check(ws: Workspace, *, history: bool = True, baseline_path: str | None = None,
              since: str | None = None) -> dict[str, Any] | None:
    """Check conformance and write ``generated/adr-conformance.{md,json}`` (advisory)."""
    report = check_conformance(ws, history=history, baseline_path=baseline_path,
                               since=since)
    if report is None:
        return None
    dump_json(report, ws.adr_conformance_json)
    md = ws.generated / "adr-conformance.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(render_conformance(report), encoding="utf-8", newline="")
    return report


# ============================================================ decision archaeology (§7.4)
# Engine §7.4: surface where the code CONSISTENTLY enforces a pattern, and where it
# doesn't, as ADR-CANDIDATE stubs with the EVIDENCE filled and the rationale a literal
# `TODO (human)`. The hard line (§0.5): ## Decision / ## Rationale / ## Rejected
# alternatives are NEVER filled by the machine (or an LLM) — inventing rationale is the
# banned hallucination. Noise control: minority-share + scale gates, a hard cap, and
# suppression of candidates an existing ADR already covers.

STUBS_SCHEMA = "adr-candidates/2"  # ADR-mining plan §3: structured evidence + category/provenance
MINED_SCHEMA = "adr-mined-candidates/1"   # §5 soft-tier LLM/agentic sidecar (never CI)
MERGED_SCHEMA = "adr-merged-candidates/1"  # human "Merge selected" sidecar (soft tier, never CI)
INBOX_SCHEMA = "adr-candidate-inbox/1"    # §9 pre-materialized union view (serve echoes verbatim)
STUBS_CAP = 8                      # legacy flat cap — still honored as an explicit `cap=` override
GLOBAL_CAP = 12                    # §7 global backstop ceiling so no system ever drowns
_PATTERN_MIN_SOURCES = 3           # a sole-provider pattern needs ≥ this many sources
_PATTERN_MIN_WEIGHT = 4           # …and this much total edge weight — the §7.4
                                   # "high-weight" gate: filters whisper-weight patterns
                                   # (a bare 3×package_ref triangle totals 3) while a
                                   # single declared link/project_ref edge (base 5)
                                   # already clears it
_INCONSISTENCY_MIN_SOURCES = 4     # an inconsistency needs ≥ this many classified sources
_INCONSISTENCY_MAX_MINORITY = 0.34 # …and a minority share at/below this
_COMM_KINDS = ("contract", "interop", "runtime")   # the cross-service transport seams

STUBS_NOTE = ("Decision archaeology surfaces DECISION CANDIDATES only: the evidence is "
              "machine-extracted and cited; the decision, its rationale, and rejected "
              "alternatives are left as a literal TODO (human) and are NEVER invented "
              "(engine §0.5/§7.4). Candidates are capped and thresholded so the list "
              "stays triageable; one already covered by an ADR is suppressed.")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _stub_slug(text: str) -> str:
    return _SLUG_RE.sub("-", (text or "").lower()).strip("-") or "candidate"


# §4/§7 technology taxonomy: map a package name to a coarse "role family" so the
# tech-standardization / tech-inconsistency detectors and the per-category budget share one
# notion of a distinct "technology axis". Substring match, first family wins; curated,
# conservative, ecosystem-spanning (C#/C++). An unrecognized package contributes no
# recognized family — the budget degrades gracefully, it never fabricates an axis.
_PACKAGE_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("orm",           ("entityframework", "ef.core", "dapper", "nhibernate", "linq2db",
                       "mongodb.driver", "ravendb", "marten")),
    ("logging",       ("serilog", "nlog", "log4net", "microsoft.extensions.logging",
                       "spdlog", "glog")),
    ("messaging",     ("masstransit", "nservicebus", "rabbitmq", "confluent.kafka",
                       "azure.messaging", "mqtt", "zeromq")),
    ("mediator",      ("mediatr", "brighter")),
    ("di",            ("autofac", "ninject", "simpleinjector", "lamar", "castle.windsor",
                       "microsoft.extensions.dependencyinjection")),
    ("rpc",           ("grpc", "thrift", "wcf")),
    ("web-framework", ("aspnetcore", "microsoft.aspnet", "nancy", "carter", "fastendpoints")),
    ("resilience",    ("polly", "steeltoe")),
    ("serialization", ("newtonsoft.json", "system.text.json", "protobuf", "messagepack",
                       "nlohmann", "rapidjson")),
    ("validation",    ("fluentvalidation",)),
    ("mapping",       ("automapper", "mapster")),
    ("caching",       ("stackexchange.redis", "microsoft.extensions.caching", "easycaching")),
    ("testing",       ("xunit", "nunit", "mstest", "moq", "fluentassertions", "gtest",
                       "catch2", "nsubstitute")),
    ("auth",          ("identityserver", "duende", "openiddict")),
    ("ui-framework",  ("blazor", "wpf", "winforms", "avalonia", "maui")),
)


def _package_family(package: str) -> str | None:
    """The coarse role family of a package name (ORM / logging / …), or ``None`` if unknown.
    Conservative substring match on a lowercased name."""
    p = (package or "").lower()
    for family, needles in _PACKAGE_FAMILIES:
        if any(n in p for n in needles):
            return family
    return None


def _clamp(lo: int, val: int, hi: int) -> int:
    return max(lo, min(val, hi))


def _evidence_hash(evidence: list[dict[str, Any]]) -> str:
    """Stable 64-bit hash of a candidate's structured evidence rows — the §3.1 dismissal-expiry
    key (evidence-LOCAL, not the whole-model hash, so an unrelated edge elsewhere does not lapse
    a dismissal). Order-independent: rows are canonicalized + sorted before hashing."""
    rows = sorted(dumps_json(e) for e in (evidence or []))
    return hashlib.sha256(dumps_json(rows).encode("utf-8")).hexdigest()[:16]


def _adr_names_any(adrs: list[dict[str, Any]], terms: list[str]) -> set[str]:
    """ADR ids whose TITLE names any of *terms* (token-run match, like link_adrs_to_targets).
    Title-only keeps category-aware suppression deterministic and consistent with the element
    match the shipped suppressor already uses."""
    real_terms = [t for t in (terms or []) if t]
    if not real_terms:
        return set()
    hits: set[str] = set()
    for a in adrs:
        words = _words(a.get("title", ""))
        if any(_title_mentions(term, words) for term in real_terms):
            aid = a.get("id")
            if aid:
                hits.add(aid)
    return hits


def _container_sccs(edges: list[dict[str, Any]]) -> list[list[str]]:
    """Container-level dependency cycles (Tarjan SCCs of size ≥2) over the lifted *edges*.
    Reuses risk.py's algorithm — never re-implements graph code (single-source-of-truth). The
    one place the SCC build lives, shared by the budget count and the cycle detector so the two
    can never skew."""
    try:
        from .stages.risk import strongly_connected_components
    except Exception:  # noqa: BLE001 - advisory; degrade to no cycles
        return []
    adj: dict[str, list[str]] = {}
    nodes: set[str] = set()
    for e in edges:
        nodes.add(e["from"])
        nodes.add(e["to"])
        adj.setdefault(e["from"], []).append(e["to"])
    return [s for s in strongly_connected_components(
        sorted(nodes), {k: sorted(v) for k, v in adj.items()}) if len(s) >= 2]


def _count_cycles(facts: dict[str, Any], edges: list[dict[str, Any]] | None = None) -> int:
    """Number of container-level dependency cycles. Accepts precomputed *edges* to avoid a
    redundant cross-edge lift when the caller already has them."""
    return len(_container_sccs(_cross_edges(facts) if edges is None else edges))


def per_category_budget(facts: dict[str, Any],
                        edges: list[dict[str, Any]] | None = None) -> dict[str, int]:
    """§7 per-category sub-budgets — each a single-signal function of its OWN natural unit,
    NOT a global weighted sum of heterogeneous terms (the rejected over-fit). technology /
    framework: distinct package families ("technology ambition"); structure: container + cycle
    stress; pattern: a small fixed cap (patterns are inherently few). A global backstop
    (:data:`GLOBAL_CAP`) then truncates the union. Divisors/clamps are first-principles rules
    of thumb (≈1 tech ADR per ≈3 package families), confirmed-not-fit on the fixtures (§7)."""
    targets = facts.get("targets", []) or []
    families = {fam for t in targets for p in (t.get("package_refs") or [])
                if (fam := _package_family(p))}
    cap_tech = _clamp(1, math.ceil(len(families) / 3), 6)
    cids = {t.get("container_id", t["id"]) for t in targets if t.get("id")}
    stress = len(cids) + _count_cycles(facts, edges)
    cap_struct = _clamp(2, math.ceil(stress / 4), 6)
    return {"technology": cap_tech, "framework": cap_tech,
            "structure": cap_struct, "pattern": 3}


def _finalize_candidate(c: dict[str, Any], *, provenance: str, method: str,
                        derived_hash: str, linked_adrs: list[str]) -> dict[str, Any]:
    """Stamp the §3 candidate-record contract: provenance/method tier, the linked existing
    ADRs, the evidence-local hash, and the model-staleness hash. Drops the internal
    ``subject_terms`` scratch field so the emitted record matches the schema exactly. There is
    deliberately NO rationale-bearing field — the §0.5 invariant is structural."""
    out = dict(c)
    out.setdefault("category", "structure")
    out.setdefault("provenance", provenance)
    out.setdefault("method", method)
    out["linked_adrs"] = sorted(linked_adrs)
    out["evidence_hash"] = _evidence_hash(out.get("evidence", []))
    out["derived_from_hash"] = derived_hash
    out.pop("subject_terms", None)
    return out


def _cross_edges(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Cross-container target edges with their lifted endpoints + evidence kinds."""
    t2c = {t["id"]: t.get("container_id", t["id"])
           for t in facts.get("targets", []) or [] if t.get("id")}
    out = []
    for r in sorted(facts.get("relationships", []) or [],
                    key=lambda r: (r.get("source", ""), r.get("target", ""))):
        cs, ct = t2c.get(r.get("source")), t2c.get(r.get("target"))
        if not cs or not ct or cs == ct:
            continue
        kinds = sorted({e.get("type") for e in r.get("evidence", []) or []
                        if e.get("type")})
        out.append({"source": r["source"], "target": r["target"],
                    "from": cs, "to": ct, "kinds": kinds,
                    "weight": r.get("weight", 0)})
    return out


def _candidates_sole_provider(edges: list[dict[str, Any]],
                              cname: dict[str, str]) -> list[dict[str, Any]]:
    """Consistently-enforced pattern: every cross-container edge of evidence kind *k*
    lands in ONE container (≥ :data:`_PATTERN_MIN_SOURCES` distinct sources)."""
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for e in edges:
        for k in e["kinds"]:
            by_kind.setdefault(k, []).append(e)
    out = []
    for kind in sorted(by_kind):
        rows = by_kind[kind]
        targets = sorted({e["to"] for e in rows})
        sources = sorted({e["from"] for e in rows})
        if len(targets) != 1 or len(sources) < _PATTERN_MIN_SOURCES \
                or sum(e["weight"] for e in rows) < _PATTERN_MIN_WEIGHT:
            continue
        provider = targets[0]
        name = cname.get(provider, provider)
        out.append({
            "kind": "sole-provider",
            "category": "structure",
            "subject_terms": [],
            "slug": _stub_slug(f"sole-{kind}-provider-{name}"),
            "title": f"`{name}` is the single {kind} provider",
            "statement": (f"All {len(rows)} cross-container `{kind}` "
                          f"dependencies from {len(sources)} container(s) land in "
                          f"`{name}` — the code consistently enforces a single "
                          f"{kind} provider."),
            "elements": [provider],
            "n_sources": len(sources),
            "total_weight": sum(e["weight"] for e in rows),
            "evidence": [{"type": "container_edge",
                          "element_ids": [e["from"], e["to"]],
                          "source_ids": [e["source"], e["target"]],
                          "detail": f"{e['from']} -> {e['to']} "
                                    f"({kind}, weight {e['weight']})"}
                         for e in sorted(rows, key=lambda e: (e["from"], e["to"]))],
        })
    return out


def _candidates_transport_inconsistency(edges: list[dict[str, Any]],
                                        cname: dict[str, str]) -> list[dict[str, Any]]:
    """Inconsistency: most source containers use one transport seam kind
    (contract/interop/runtime), a small minority uses another (§7.4: '12 of 14
    services use gRPC contract, 2 use REST runtime')."""
    used_by: dict[str, set[str]] = {}   # comm kind -> source containers
    for e in edges:
        for k in e["kinds"]:
            if k in _COMM_KINDS:
                used_by.setdefault(k, set()).add(e["from"])
    if len(used_by) < 2:
        return []
    classified = sorted(set().union(*used_by.values()))
    if len(classified) < _INCONSISTENCY_MIN_SOURCES:
        return []
    comm_weight = sum(e["weight"] for e in edges
                      if set(e["kinds"]) & set(_COMM_KINDS))
    if comm_weight < _PATTERN_MIN_WEIGHT:
        return []   # the §7.4 high-weight gate — whisper-weight seams are not a pattern
    majority = sorted(used_by, key=lambda k: (-len(used_by[k]), k))[0]
    minority_sources = sorted(s for s in classified if s not in used_by[majority])
    if not minority_sources:
        return []
    share = len(minority_sources) / len(classified)
    if share > _INCONSISTENCY_MAX_MINORITY:
        return []
    minority_kinds = sorted(k for k in used_by if k != majority
                            and used_by[k] & set(minority_sources))
    n_major = len(used_by[majority])
    return [{
        "kind": "transport-inconsistency",
        "category": "structure",
        "subject_terms": [],
        "slug": _stub_slug(f"transport-{majority}-vs-{'-'.join(minority_kinds)}"),
        "title": f"cross-container transport: {majority} with "
                 f"{len(minority_sources)} outlier(s)",
        "statement": (f"{n_major} of {len(classified)} container(s) communicate via "
                      f"`{majority}` seams; {len(minority_sources)} use "
                      f"{', '.join(f'`{k}`' for k in minority_kinds)} instead — "
                      "either an undocumented decision or an inconsistency."),
        "elements": minority_sources,
        "n_sources": len(classified),
        "total_weight": comm_weight,
        "minority": [{"container": cname.get(s, s), "id": s} for s in minority_sources],
        "evidence": [{"type": "container_edge",
                      "element_ids": [e["from"], e["to"]],
                      "source_ids": [e["source"], e["target"]],
                      "detail": f"{e['from']} -> {e['to']} "
                                f"({'/'.join(k for k in e['kinds'] if k in _COMM_KINDS)}, "
                                f"weight {e['weight']})"}
                     for e in sorted(edges, key=lambda e: (e["from"], e["to"]))
                     if set(e["kinds"]) & set(_COMM_KINDS)],
    }]


# ----------------------------------------------------- §4 additional deterministic detectors
# Each reads an EXISTING fact stream, stays a pure deterministic function of curated facts, and
# fires only where its signal is present (clean no-op elsewhere). All emit container/system
# altitude candidates even from component-grained evidence (§0.3 #4: detect fine, emit coarse).

_TECH_MIN_CONTAINERS = 3            # a "standardization" needs ≥ this many containers total
_TECH_STANDARD_MIN_SHARE = 0.5      # …and the package present in ≥ this share of them
_SHARED_KERNEL_MIN_FANIN = 4        # a shared kernel needs ≥ this many dependent containers
_SHARED_KERNEL_MIN_SHARE = 0.6      # …and ≥ this share of all other containers
_INTEROP_MIN_SITES = 3              # an interop boundary needs ≥ this many P/Invoke seams
_DEPLOY_MIN_DEPENDENTS = 3          # a deployment hub needs ≥ this many dependent containers
_PATTERN_MIN_MARKERS = 3            # a design-pattern candidate needs ≥ this many markers
_FRAMEWORK_FAMILIES = {"web-framework", "ui-framework"}

# §4 phase-2 design-pattern table: a framework-documented 1:1 marker → a canonical name. The
# hand-off rule (§4): if naming the pattern needs more than a marker→name lookup it is LLM-mining
# territory, not a deterministic detector. C#-only / canonical-markers-only / best-effort.
_PATTERN_TABLE: tuple[tuple[str, str, tuple[str, ...], list[str]], ...] = (
    ("cqrs",      "CQRS via MediatR `IRequestHandler<>`",
     ("IRequestHandler", "INotificationHandler"), ["CQRS", "MediatR"]),
    ("ef-data",   "EF Core `DbContext` data access",
     ("DbContext",), ["EF", "EntityFramework", "Entity Framework"]),
    ("mvc-api",   "ASP.NET Core MVC API controllers",
     ("ControllerBase", "ApiController"), ["MVC", "controllers", "API"]),
    ("worker",    "background workers via `IHostedService`",
     ("IHostedService", "BackgroundService"), ["worker", "background", "hosted"]),
)


def _t2c(facts: dict[str, Any]) -> dict[str, str]:
    """target id → container id (falling back to the target id when no container)."""
    return {t["id"]: t.get("container_id", t["id"])
            for t in facts.get("targets", []) or [] if t.get("id")}


def _tech_terms(package: str, family: str | None) -> list[str]:
    """Best-effort suppression terms for a technology candidate: the package name, its last
    dotted segment, and the role family (an ADR rarely spells the full NuGet id)."""
    terms = [package]
    seg = package.split(".")[-1]
    if seg and seg != package:
        terms.append(seg)
    if family:
        terms.append(family)
    return terms


def _candidates_tech_standardization(facts: dict[str, Any],
                                     cname: dict[str, str]) -> list[dict[str, Any]]:
    """§4 phase-1 ANCHOR: a `package_ref` present in ≥ a budgeted share of containers — the
    datasheet 'technology fingerprint'. Language-neutral (package_refs exist for C# and C++),
    highest-trust, cheapest. Lands the owner's technology + framework-selection categories."""
    targets = facts.get("targets", []) or []
    cids = {t.get("container_id", t["id"]) for t in targets if t.get("id")}
    n_total = len(cids)
    if n_total < _TECH_MIN_CONTAINERS:
        return []
    pkg_containers: dict[str, dict[str, list[str]]] = {}
    for t in targets:
        tid = t.get("id")
        if not tid:
            continue
        cid = t.get("container_id", tid)
        for p in t.get("package_refs") or []:
            pkg_containers.setdefault(p, {}).setdefault(cid, []).append(tid)
    out: list[dict[str, Any]] = []
    for pkg in sorted(pkg_containers):
        conts = pkg_containers[pkg]
        n_ref = len(conts)
        share = n_ref / n_total
        if n_ref < _TECH_MIN_CONTAINERS or share < _TECH_STANDARD_MIN_SHARE:
            continue
        family = _package_family(pkg)
        cat = "framework" if family in _FRAMEWORK_FAMILIES else "technology"
        concern = family.replace("-", " ") if family else "a shared concern"
        elems = sorted(conts)
        out.append({
            "kind": "tech-standardization", "category": cat,
            "subject_terms": _tech_terms(pkg, family),
            "slug": _stub_slug(f"tech-standardize-{pkg}"),
            "title": f"The system standardizes on `{pkg}`",
            "statement": (f"`{pkg}` is referenced by {n_ref} of {n_total} container(s) "
                          f"({share:.0%}) — the system appears to standardize on it for "
                          f"{concern}."),
            "elements": elems, "n_sources": n_ref, "total_weight": n_ref,
            "evidence": [{"type": "package_ref", "element_ids": [cid],
                          "source_ids": sorted(conts[cid]), "package": pkg,
                          "detail": f"{cname.get(cid, cid)} references {pkg}"}
                         for cid in elems],
        })
    return out


def _candidates_tech_inconsistency(facts: dict[str, Any],
                                   cname: dict[str, str]) -> list[dict[str, Any]]:
    """§4: two `package_ref`s serving the SAME role (ORM, logging, DI, mediator…) split across
    containers — the transport-inconsistency shape, over packages ('10 use EF Core, 2 use
    Dapper — an undocumented choice or drift')."""
    targets = facts.get("targets", []) or []
    fam_pkgs: dict[str, dict[str, set[str]]] = {}
    fam_sources: dict[str, dict[str, set[str]]] = {}   # family -> {cid -> source target ids}
    for t in targets:
        tid = t.get("id")
        if not tid:
            continue
        cid = t.get("container_id", tid)
        for p in t.get("package_refs") or []:
            fam = _package_family(p)
            if not fam:
                continue
            fam_pkgs.setdefault(fam, {}).setdefault(p, set()).add(cid)
            fam_sources.setdefault(fam, {}).setdefault(cid, set()).add(tid)
    out: list[dict[str, Any]] = []
    for fam in sorted(fam_pkgs):
        pkgs = fam_pkgs[fam]
        if len(pkgs) < 2:
            continue
        all_cids = sorted(set().union(*pkgs.values()))
        if len(all_cids) < _INCONSISTENCY_MIN_SOURCES:
            continue
        usage = {p: len(cids) for p, cids in pkgs.items()}
        majority = sorted(usage, key=lambda p: (-usage[p], p))[0]
        minority_pkgs = sorted(p for p in pkgs if p != majority)
        minority_cids = sorted(set().union(*(pkgs[p] for p in minority_pkgs))
                               - pkgs[majority])
        if not minority_cids:
            continue
        share = len(minority_cids) / len(all_cids)
        if share > _INCONSISTENCY_MAX_MINORITY:
            continue
        terms: list[str] = []
        for p in [majority, *minority_pkgs]:
            terms += _tech_terms(p, fam)
        out.append({
            "kind": "tech-inconsistency", "category": "technology",
            "subject_terms": sorted(set(terms)),
            "slug": _stub_slug(f"tech-inconsistency-{fam}"),
            "title": f"split {fam} choice: `{majority}` with "
                     f"{len(minority_pkgs)} outlier(s)",
            "statement": (f"{usage[majority]} container(s) use `{majority}` for {fam}; "
                          f"{len(minority_cids)} use "
                          f"{', '.join(f'`{p}`' for p in minority_pkgs)} instead — an "
                          "undocumented technology choice or drift."),
            "elements": all_cids, "n_sources": len(all_cids),
            "total_weight": len(all_cids),
            "evidence": [{"type": "package_ref", "element_ids": [cid],
                          "source_ids": sorted(fam_sources[fam].get(cid, set())),
                          "package": p,
                          "detail": f"{cname.get(cid, cid)} uses {p} for {fam}"}
                         for p in sorted(pkgs) for cid in sorted(pkgs[p])],
        })
    return out


def _candidates_shared_kernel(facts: dict[str, Any], edges: list[dict[str, Any]],
                              cname: dict[str, str]) -> list[dict[str, Any]]:
    """§4: a high-fan-in container everything depends on ('`BuildingBlocks` is a shared kernel:
    13 of 14 containers depend on it'). Fan-in computed from the lifted container edges."""
    cids = {t.get("container_id", t["id"]) for t in facts.get("targets", []) or []
            if t.get("id")}
    n = len(cids)
    if n < _SHARED_KERNEL_MIN_FANIN + 1:
        return []
    fan_in: dict[str, list[dict[str, Any]]] = {}
    for e in edges:
        if e["from"] != e["to"]:
            fan_in.setdefault(e["to"], []).append(e)
    out: list[dict[str, Any]] = []
    for cid in sorted(fan_in):
        srcs = sorted({e["from"] for e in fan_in[cid]})
        if len(srcs) < _SHARED_KERNEL_MIN_FANIN or len(srcs) / (n - 1) < _SHARED_KERNEL_MIN_SHARE:
            continue
        rows = sorted(fan_in[cid], key=lambda e: (e["from"], e["to"]))
        total_w = sum(e["weight"] for e in rows)
        if total_w < _PATTERN_MIN_WEIGHT:   # whisper-weight fan-in is not a kernel (§7.4 gate)
            continue
        name = cname.get(cid, cid)
        out.append({
            "kind": "shared-kernel", "category": "structure", "subject_terms": [],
            "slug": _stub_slug(f"shared-kernel-{name}"),
            "title": f"`{name}` is a shared kernel",
            "statement": (f"{len(srcs)} of {n - 1} other container(s) depend on `{name}` — "
                          "a shared kernel / common foundation everything builds on."),
            "elements": [cid], "n_sources": len(srcs),
            "total_weight": sum(e["weight"] for e in rows),
            "evidence": [{"type": "container_edge", "element_ids": [e["from"], e["to"]],
                          "source_ids": [e["source"], e["target"]],
                          "detail": f"{e['from']} -> {e['to']} (weight {e['weight']})"}
                         for e in rows],
        })
    return out


def _candidates_accepted_cycle(facts: dict[str, Any], edges: list[dict[str, Any]],
                               cname: dict[str, str]) -> list[dict[str, Any]]:
    """§4: a Tarjan SCC (size ≥2) of containers that persists ('Ordering ⇄ Payment — deliberate
    saga or accidental coupling?'). Reuses risk.py's SCC algorithm, never re-implements it."""
    sccs = _container_sccs(edges)
    out: list[dict[str, Any]] = []
    for members in sccs:
        members = sorted(members)
        member_set = set(members)
        # keep ALL parallel container edges (several target rels can lift to one (from,to)) so
        # the cycle weight/evidence are complete — mirrors shared-kernel/sole-provider, never a
        # by-(from,to) dict that would drop parallels and undercount the weight gate.
        internal = sorted((e for e in edges
                           if e["from"] in member_set and e["to"] in member_set),
                          key=lambda e: (e["from"], e["to"], e["source"], e["target"]))
        if sum(e["weight"] for e in internal) < _PATTERN_MIN_WEIGHT:
            continue   # whisper-weight cycle is noise, not a documented coupling (§7.4 gate)
        names = [cname.get(m, m) for m in members]
        head = " ⇄ ".join(f"`{n}`" for n in names[:2])
        extra = f" (+{len(members) - 2} more)" if len(members) > 2 else ""
        out.append({
            "kind": "accepted-cycle", "category": "structure", "subject_terms": [],
            "slug": _stub_slug("cycle-" + "-".join(members[:3])),
            "title": f"dependency cycle: {', '.join(names)}",
            "statement": (f"{head}{extra} form a dependency cycle — a deliberate saga / "
                          "bidirectional collaboration, or accidental coupling to break?"),
            "elements": members, "n_sources": len(members),
            "total_weight": sum(e["weight"] for e in internal),
            "evidence": [{"type": "container_edge", "element_ids": [e["from"], e["to"]],
                          "source_ids": [e["source"], e["target"]],
                          "detail": f"{e['from']} -> {e['to']} (weight {e['weight']})"}
                         for e in internal],
        })
    return out


def _candidates_interop(facts: dict[str, Any],
                        cname: dict[str, str]) -> list[dict[str, Any]]:
    """§4: P/Invoke seams (relationship evidence ``type == "interop"``) clustered at one
    container ('All managed↔native interop crosses `NativeBridge` (7 P/Invoke sites)')."""
    t2c = _t2c(facts)
    # resolve each interop seam's managed owner to a CONTAINER; a source that does not lift to a
    # curated container (excluded target / native endpoint) contributes no container-altitude
    # element and is dropped (§0.3 #4 — never leak a sub-container id into elements).
    owners: dict[str, list[dict[str, Any]]] = {}
    for r in facts.get("relationships", []) or []:
        if not any(e.get("type") == "interop" for e in r.get("evidence", []) or []):
            continue
        cid = t2c.get(r.get("source"))
        if cid is None:
            continue
        owners.setdefault(cid, []).append(r)
    n_sites = sum(len(rs) for rs in owners.values())
    if n_sites < _INTEROP_MIN_SITES:
        return []
    elems = sorted(owners)
    dominant = sorted(owners, key=lambda c: (-len(owners[c]), c))[0]
    name = cname.get(dominant, dominant)
    evidence = []
    for cid in elems:
        for r in sorted(owners[cid], key=lambda r: (r.get("source", ""), r.get("target", ""))):
            detail = next((e.get("detail") for e in r.get("evidence", []) or []
                           if e.get("type") == "interop"), "")
            evidence.append({"type": "interop", "element_ids": [cid],
                             "source_ids": [r.get("source"), r.get("target")],
                             "detail": f"{r.get('source')} -> {r.get('target')}: {detail}"})
    return [{
        "kind": "interop-boundary", "category": "structure", "subject_terms": [],
        "slug": _stub_slug(f"interop-boundary-{name}"),
        "title": f"managed↔native interop boundary at `{name}`",
        "statement": (f"{n_sites} P/Invoke / native-interop seam(s) cross "
                      + (f"`{name}`" if len(elems) == 1
                         else f"{len(elems)} container(s), concentrated at `{name}`")
                      + " — a managed↔native boundary worth documenting."),
        "elements": elems, "n_sources": len(elems), "total_weight": n_sites,
        "evidence": evidence,
    }]


def _candidates_deployment(facts: dict[str, Any],
                           cname: dict[str, str]) -> list[dict[str, Any]]:
    """§4: a deployment/runtime facet hub many containers route through ('All services discover
    dependencies via the Aspire AppHost'). Mirrors risk._detect_spofs over the deployment facet."""
    dep = facts.get("deployment") or {}
    edges = dep.get("edges") or []
    nodes = {n.get("id"): n for n in dep.get("nodes") or [] if n.get("id")}
    if not edges:
        return []
    t2c = _t2c(facts)
    # image (deploy:image:*) -> build target id, via the "packages" deployment edges.
    img2target = {e["source"]: e["target"] for e in edges
                  if e.get("kind") == "packages" and e.get("source") and e.get("target")}
    # infra node -> list of (container_id, edge) for the edges that resolve to a REAL container
    # (altitude discipline: a dependent that does not lift to a curated container is dropped, so
    # the cited evidence and the counts agree on the same set).
    infra_hits: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for e in edges:
        if not str(e.get("target") or "").startswith("infra:"):
            continue
        cid = t2c.get(img2target.get(e.get("source"), e.get("source")))
        if cid is None or cid not in cname:
            continue
        infra_hits.setdefault(str(e["target"]), []).append((cid, e))
    out: list[dict[str, Any]] = []
    for infra in sorted(infra_hits):
        pairs = sorted(infra_hits[infra],
                       key=lambda p: (p[0], str(p[1].get("source")), str(p[1].get("target"))))
        deps = sorted({cid for cid, _e in pairs})
        if len(deps) < _DEPLOY_MIN_DEPENDENTS:
            continue
        infra_name = (nodes.get(infra, {}) or {}).get("name", infra)
        out.append({
            "kind": "deployment-topology", "category": "structure", "subject_terms": [],
            "slug": _stub_slug(f"deployment-hub-{infra_name}"),
            "title": f"deployment hub: `{infra_name}`",
            "statement": (f"{len(deps)} container(s) discover or route through `{infra_name}` "
                          "in the deployment facet — a topology decision worth documenting."),
            "elements": deps, "n_sources": len(deps), "total_weight": len(pairs),
            "evidence": [{"type": "deploy", "element_ids": [cid], "source_ids": [infra],
                          "detail": f"{e.get('source')} -> {e.get('target')} "
                                    f"({e.get('kind', 'edge')})"}
                         for cid, e in pairs],
        })
    return out


def _candidates_layering(facts: dict[str, Any], layering_rules: dict[str, Any] | None,
                         cname: dict[str, str]) -> list[dict[str, Any]]:
    """§4: a layer ordering CONSISTENTLY HELD (check_layering's dual — a forbidden rule with
    status OK, not VIOLATION and not degraded) across a clear tier partition. Opt-in on the
    repo having hand-authored layering rules; a clean no-op otherwise."""
    if not isinstance(layering_rules, dict):
        return []   # a malformed/non-mapping layering-rules.yaml degrades to no candidate
    forbidden = layering_rules.get("forbidden") or []
    if not forbidden:
        return []
    try:
        from .stages.drift_report import check_layering
    except Exception:  # noqa: BLE001 - advisory
        return []
    findings = check_layering(facts, layering_rules)
    held = [f for f in findings if f.get("status") == "OK"]
    if not held:
        return []
    name2cid: dict[str, str] = {}
    for t in facts.get("targets", []) or []:
        cid = t.get("container_id", t.get("id"))
        if cid:
            name2cid[str(cid).lower()] = cid
        cn = t.get("container_name")
        if cn:
            name2cid.setdefault(str(cn).lower(), cid)
    elems: set[str] = set()
    for f in held:
        for tok in (f.get("from"), f.get("to")):
            resolved = name2cid.get(str(tok or "").lower())
            if resolved:
                elems.add(resolved)
    return [{
        "kind": "layering-convention", "category": "structure", "subject_terms": [],
        "slug": _stub_slug("layering-convention-held"),
        "title": f"held layering convention ({len(held)} rule(s))",
        "statement": (f"{len(held)} layering rule(s) are consistently HELD with no violating "
                      "edge — an architectural layering convention the code currently honours."),
        "elements": sorted(elems), "n_sources": len(held), "total_weight": len(held),
        "evidence": [{"type": "layering", "element_ids": [], "source_ids": [],
                      "detail": f"HELD: `{f.get('from')}` -/-> `{f.get('to')}`"}
                     for f in sorted(held, key=lambda f: (str(f.get("from")),
                                                          str(f.get("to"))))],
    }]


def _candidates_design_pattern(facts: dict[str, Any], api_skeleton: dict[str, Any] | None,
                               cname: dict[str, str]) -> list[dict[str, Any]]:
    """§4 phase-2 (C#-only / canonical-markers-only / best-effort): a framework-documented 1:1
    marker in the Roslyn `api_skeleton` (`: IRequestHandler<>` ⇒ CQRS, `: DbContext` ⇒ EF,
    `[ApiController]`/`ControllerBase` ⇒ MVC API, `IHostedService` ⇒ worker). Naming the style
    is a fixed lookup, never free prose — anything fuzzier is Tier-2's job (the hand-off rule)."""
    if not api_skeleton:
        return []
    t2c = _t2c(facts)
    # pattern key -> {container -> marker count}
    pat_hits: dict[str, dict[str, int]] = {}
    for tid, types in api_skeleton.items():
        # api_skeleton is the Roslyn sidecar keyed by EXTRACTED target ids; a target curation
        # excluded has no curated container — drop it rather than leak a target id to container
        # altitude (§0.3 #4: emit coarse).
        cid = t2c.get(tid)
        if cid is None:
            continue
        for ty in types or []:
            bag: list[str] = []
            if ty.get("base"):
                bag.append(str(ty["base"]))
            bag += [str(i) for i in ty.get("interfaces", []) or []]
            bag += [str(a) for a in ty.get("attributes", []) or []]
            for mem in ty.get("members", []) or []:
                bag += [str(a) for a in mem.get("attrs", []) or []]
            for key, _name, needles, _terms in _PATTERN_TABLE:
                if any(any(nd in s for s in bag) for nd in needles):
                    pat_hits.setdefault(key, {})[cid] = pat_hits.get(key, {}).get(cid, 0) + 1
    out: list[dict[str, Any]] = []
    table = {key: (name, terms) for key, name, _needles, terms in _PATTERN_TABLE}
    for key in sorted(pat_hits):
        per_container = pat_hits[key]
        total = sum(per_container.values())
        if total < _PATTERN_MIN_MARKERS:
            continue
        display, terms = table[key]
        elems = sorted(per_container)
        out.append({
            "kind": "design-pattern", "category": "pattern",
            "subject_terms": terms,
            "slug": _stub_slug(f"pattern-{key}"),
            "title": f"pattern: {display}",
            "statement": (f"{len(elems)} container(s) apply {display} "
                          f"({total} marker type(s)) — a recognised architectural pattern."),
            "elements": elems, "n_sources": len(elems), "total_weight": total,
            "evidence": [{"type": "api_skeleton", "element_ids": [cid], "source_ids": [],
                          "marker": key,
                          "detail": f"{cname.get(cid, cid)}: {per_container[cid]} "
                                    f"{display} marker(s)"}
                         for cid in elems],
        })
    return out


def _collect_detectors(facts: dict[str, Any], edges: list[dict[str, Any]],
                       cname: dict[str, str], *,
                       api_skeleton: dict[str, Any] | None,
                       layering_rules: dict[str, Any] | None) -> list[dict[str, Any]]:
    """All Tier-1 deterministic detectors (§4), aggregated. Each fires only where its signal is
    present and is a clean no-op elsewhere (the extractor-convention discipline)."""
    found: list[dict[str, Any]] = []
    found += _candidates_sole_provider(edges, cname)
    found += _candidates_transport_inconsistency(edges, cname)
    found += _candidates_tech_standardization(facts, cname)
    found += _candidates_tech_inconsistency(facts, cname)
    found += _candidates_shared_kernel(facts, edges, cname)
    found += _candidates_accepted_cycle(facts, edges, cname)
    found += _candidates_interop(facts, cname)
    found += _candidates_deployment(facts, cname)
    found += _candidates_layering(facts, layering_rules, cname)
    found += _candidates_design_pattern(facts, api_skeleton, cname)
    return found


def _salience(c: dict[str, Any]) -> tuple[int, int, str]:
    """Widest pattern first, then heaviest, then slug (deterministic, §7)."""
    return (-c.get("n_sources", 0), -c.get("total_weight", 0), c.get("slug", ""))


def decision_candidates(facts: dict[str, Any], adrs: list[dict[str, Any]],
                        *, cap: int | None = None,
                        api_skeleton: dict[str, Any] | None = None,
                        layering_rules: dict[str, Any] | None = None) -> dict[str, Any]:
    """The §4/§7 deterministic engine: detect candidates across every Tier-1 detector,
    apply category-aware ADR suppression (§3), then the §7 per-category sub-budgets + the
    global backstop. Returns ``{candidates, suppressed, dropped_over_cap, budget, advisory,
    coverage_l0}`` — pure, sorted, byte-identical. *adrs* come from the discovery report; an
    explicit *cap* is honored as a global override (legacy ``STUBS_CAP`` semantics, used by
    tests). *api_skeleton* / *layering_rules* are passed in by :func:`run_stubs` (the pure
    function never reads the workspace)."""
    from .stages.drift_report import COVERAGE_FLOOR, _coverage_l0
    from .model import content_hash

    targets = facts.get("targets", []) or []
    cname = {t.get("container_id", t["id"]): t.get("container_name",
                                                   t.get("name", t["id"]))
             for t in targets if t.get("id")}
    edges = _cross_edges(facts)
    found = _collect_detectors(facts, edges, cname,
                               api_skeleton=api_skeleton, layering_rules=layering_rules)

    # ADR linkage: which ADRs name which container (lifting target hits up to containers).
    links = link_adrs_to_targets(adrs, targets)
    t_by_id = {t["id"]: t for t in targets if t.get("id")}
    container_adrs: dict[str, set[str]] = {}
    for tid, aids in links.items():
        cid = t_by_id.get(tid, {}).get("container_id", tid)
        container_adrs.setdefault(cid, set()).update(aids)

    derived = content_hash(facts)
    kept, suppressed = [], []
    for c in found:
        elems = set(c.get("elements", []))
        elem_adrs = sorted({aid for cid in elems for aid in container_adrs.get(cid, set())})
        subject_adrs = _adr_names_any(adrs, c.get("subject_terms", []))
        linked = sorted(set(elem_adrs) | subject_adrs)
        cat = c.get("category", "structure")
        fc = _finalize_candidate(c, provenance="detector", method="deterministic",
                                 derived_hash=derived, linked_adrs=linked)
        # Category-aware suppression (§3): structure candidates suppress on element overlap
        # (the shipped sole-provider behavior); technology/framework/pattern candidates require
        # a SINGLE ADR that names BOTH the subject (package family / pattern marker) AND an
        # element — so one Basket ADR can't bury "the system standardizes on EF Core".
        if cat == "structure":
            hits = sorted(cid for cid in elems if container_adrs.get(cid))
            if hits:
                suppressed.append({"slug": fc["slug"], "title": fc["title"],
                                   "reason": "an existing ADR already names "
                                             + ", ".join(f"`{h}`" for h in hits)})
                continue
        else:
            both = sorted(set(elem_adrs) & subject_adrs)
            if both:
                suppressed.append({"slug": fc["slug"], "title": fc["title"],
                                   "reason": "an existing ADR names both the technology/"
                                             "pattern and an element ("
                                             + ", ".join(f"`{a}`" for a in both) + ")"})
                continue
        kept.append(fc)

    # §7 per-category sub-budgets (each on its own natural unit) then the global backstop.
    budget = per_category_budget(facts, edges)
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for c in kept:
        by_cat.setdefault(c.get("category", "structure"), []).append(c)
    emitted, dropped = [], 0
    for catname in sorted(by_cat):
        items = sorted(by_cat[catname], key=_salience)
        cap_cat = budget.get(catname, 3)
        emitted.extend(items[:cap_cat])
        dropped += max(0, len(items) - cap_cat)
    emitted.sort(key=_salience)
    global_cap = GLOBAL_CAP if cap is None else cap
    dropped += max(0, len(emitted) - global_cap)
    emitted = emitted[:global_cap]

    coverage_l0 = _coverage_l0(facts)
    return {"candidates": emitted, "suppressed": suppressed,
            "dropped_over_cap": dropped, "budget": budget,
            "advisory": coverage_l0 < COVERAGE_FLOOR,
            "coverage_l0": round(coverage_l0, 4)}


def _resolve_inbox_cap(ws: Workspace, override: int | None) -> int | None:
    """The effective inbox cap (#3): an explicit *override* wins (``0``/negative = unlimited),
    else the persistent ``anon.adr_max_candidates`` setting (``0`` = unlimited, the default).
    Reading the soft preference here keeps the cap consistent across every inbox producer (`arch
    run`, `adr stubs`, `adr mine`, serve) — it never touches the determinism-hashed core, and any
    resolution failure degrades to no cap."""
    if override is not None:
        return override if override > 0 else None
    try:
        from . import settings as settings_mod
        gen = settings_mod.load_general(settings_mod.general_path())
        n = int((gen.get("anon") or {}).get("adr_max_candidates", 0) or 0)
    except Exception:  # noqa: BLE001 - a missing/odd settings file must never break the inbox
        return None
    return n if n > 0 else None


def materialize_inbox(ws: Workspace, *, derived_hash: str | None = None,
                      max_candidates: int | None = None) -> dict[str, Any]:
    """§9: pre-materialize the inbox view serve echoes — union the deterministic decisions with
    the soft mined sidecar, dedup (deterministic wins, §5.4), filter the §3.1 dismissals
    (evidence-hash-keyed expiry), and write ``generated/adr-candidate-inbox.json``. Keeps serve
    dump-and-echo (GUI §2.1): it computes nothing serve couldn't echo. A pure read of
    ``generated/`` + the hand-owned dismissal list — it never perturbs the hashed core. The
    in-run caller passes *derived_hash* (already computed from the loaded facts) to avoid a
    redundant fact-model read + re-hash. *max_candidates* caps the shown inbox (#3): an explicit
    value overrides the ``anon.adr_max_candidates`` setting (``0`` = unlimited)."""
    from .config import load_yaml

    def _candidates(path):
        if not path.exists():
            return []
        try:
            return (load_json(path) or {}).get("candidates", []) or []
        except (OSError, ValueError):
            return []

    det = _candidates(ws.adr_candidate_decisions_json)
    mined = _candidates(ws.adr_mined_candidates_json)
    merged = _candidates(ws.adr_merged_candidates_json)

    # dedup: a mined candidate duplicating a deterministic one (same elements, matched by EITHER
    # detector kind OR category) is dropped in favour of the cited, byte-identical deterministic
    # row (§5.4 deferred-residual default). Matching on kind alone is a no-op — a mined row's kind
    # is always 'mined-decision' and never equals a detector family — so category is the live key.
    def _elem_key(c):
        return tuple(sorted(c.get("elements", []) or []))

    det_kind = {(c.get("kind"), _elem_key(c)) for c in det}
    det_cat = {(c.get("category"), _elem_key(c)) for c in det}
    # human-merged candidates are bespoke fusions — they are NOT deduped against the detectors
    # (their union-of-elements rarely matches a single detector) and lead the union so they sort
    # first within their category. Constituents are cleared at merge time, so no real dup arises.
    union = list(merged) + list(det) + [c for c in mined
                                        if (c.get("kind"), _elem_key(c)) not in det_kind
                                        and (c.get("category"), _elem_key(c)) not in det_cat]

    try:
        dismiss_doc = load_yaml(ws.adr_candidates_yaml)
    except Exception:  # noqa: BLE001 - a malformed hand-edited file must not crash the inbox
        dismiss_doc = {}
    dismissed_cfg = dismiss_doc.get("dismissed", []) if isinstance(dismiss_doc, dict) else []
    by_slug = {d["slug"]: d for d in (dismissed_cfg or [])
               if isinstance(d, dict) and d.get("slug")}
    # a candidate that has already been promoted into an ADR is the loop closed (§6.3) — drop it
    # from BOTH the inbox and the dismissed drawer so it never lingers as a candidate of itself.
    promoted = promoted_adr_slugs(ws, [c.get("slug", "") for c in union])
    inbox, dismissed_rows, promoted_rows = [], [], []
    for c in union:
        if c.get("slug") in promoted:
            promoted_rows.append(c)
            continue
        d = by_slug.get(c.get("slug"))
        ev = c.get("evidence_hash")
        # honored only while THIS candidate's evidence is unchanged — evidence-LOCAL expiry (§3.1);
        # require a real hash on BOTH sides so a hashless legacy row can't None==None-match.
        if d is not None and ev and d.get("dismissed_at_evidence_hash") == ev:
            dismissed_rows.append({**c, "dismiss_reason": d.get("reason", "")})
        else:
            inbox.append(c)
    inbox.sort(key=lambda c: (c.get("category", "zzz"),) + _salience(c))
    dismissed_rows.sort(key=lambda c: (c.get("category", "zzz"),) + _salience(c))

    # #3 — clamp the shown inbox to the configured maximum (the overflow is hidden, NOT dismissed:
    # it returns the moment the cap is raised or removed). Keep the top-`cap` by GLOBAL salience
    # (not the category-major display order — else a whole low-alpha category would be starved
    # before a higher-salience one), then preserve the category grouping for display.
    cap = _resolve_inbox_cap(ws, max_candidates)
    capped = 0
    if cap is not None and len(inbox) > cap:
        keep = {c["slug"] for c in sorted(inbox, key=_salience)[:cap]}
        capped = len(inbox) - len(keep)
        inbox = [c for c in inbox if c["slug"] in keep]

    if derived_hash is None:
        from .model import content_hash
        facts_path = ws.curated_facts if ws.curated_facts.exists() else ws.extracted_facts
        derived_hash = content_hash(load_json(facts_path)) if facts_path.exists() else None
    out = {"schema": INBOX_SCHEMA, "derived_from_hash": derived_hash, "note": STUBS_NOTE,
           "candidates": inbox, "dismissed": dismissed_rows,
           "counts": {"total": len(union), "shown": len(inbox),
                      "dismissed": len(dismissed_rows), "promoted": len(promoted_rows),
                      "capped": capped}}
    ws.adr_candidate_inbox_json.parent.mkdir(parents=True, exist_ok=True)
    dump_json(out, ws.adr_candidate_inbox_json)
    return out


def clear_candidates(ws: Workspace, *, slugs: list[str] | None = None) -> dict[str, Any]:
    """Inbox "Clear" — a TRANSIENT declutter, NOT a dismissal. Drop *slugs* (or EVERYTHING when
    ``slugs is None``) from the machine-owned deterministic decisions artifact AND the soft
    mined/merged sidecars, then re-materialize. Recoverable by design: ``arch adr stubs`` /
    ``arch run`` regenerates the deterministic rows from the build graph, ``arch adr mine`` the
    mined ones — so clearing a deterministic candidate hides it from the working inbox but the
    next full pipeline run will surface it again (use Dismiss for a durable, evidence-keyed
    triage). Touches only generated/soft artifacts — never the hashed core. Returns the
    refreshed inbox dict."""
    drop = set(slugs) if slugs is not None else None
    for path in (ws.adr_candidate_decisions_json, ws.adr_mined_candidates_json,
                 ws.adr_merged_candidates_json):
        if not path.exists():
            continue
        try:
            data = load_json(path) or {}
        except (OSError, ValueError):
            continue
        cands = data.get("candidates", []) or []
        kept = [] if drop is None else [c for c in cands if c.get("slug") not in drop]
        if len(kept) == len(cands):
            continue   # nothing to drop here — leave the bytes untouched
        data["candidates"] = kept
        dump_json(data, path)
    return materialize_inbox(ws)


def _append_merged(ws: Workspace, candidate: dict[str, Any]) -> None:
    """Append (replace-by-slug) one merged candidate into the soft merged sidecar."""
    existing: list[dict[str, Any]] = []
    if ws.adr_merged_candidates_json.exists():
        try:
            existing = (load_json(ws.adr_merged_candidates_json) or {}).get("candidates", []) or []
        except (OSError, ValueError):
            existing = []
    existing = [c for c in existing if c.get("slug") != candidate["slug"]]
    existing.append(candidate)
    ws.adr_merged_candidates_json.parent.mkdir(parents=True, exist_ok=True)
    dump_json({"schema": MERGED_SCHEMA, "candidates": existing},
              ws.adr_merged_candidates_json)


def merge_candidates(ws: Workspace, slugs: list[str]) -> dict[str, Any] | None:
    """Inbox "Merge selected" — fuse ≥2 inbox candidates into ONE combined candidate written to
    the soft merged sidecar, then SUPERSEDE the originals (transient-clear them from the inbox).
    The fusion invents nothing (§0.5): linked elements + evidence are the UNION, the
    WHAT-statements are concatenated VERBATIM, and the merged title joins the source titles. The
    merged candidate authors/promotes through the normal flow (``find_candidate`` searches the
    merged sidecar). Returns the merged candidate, or ``None`` when fewer than two slugs resolve."""
    from .model import content_hash

    sources, seen = [], set()
    for s in slugs or []:
        if s in seen:
            continue
        c = find_candidate(ws, s)
        if c is not None:
            sources.append(c)
            seen.add(s)
    if len(sources) < 2:
        return None
    src_slugs = sorted(seen)

    elements = sorted({e for c in sources for e in (c.get("elements") or [])})
    evidence, ev_seen = [], set()
    for c in sources:
        for e in c.get("evidence") or []:
            key = (e.get("type"), e.get("detail"))
            if key in ev_seen:
                continue
            ev_seen.add(key)
            evidence.append(e)
    statements = [c.get("statement", "").strip() for c in sources]
    statement = "  ".join(
        (s if s.endswith((".", "!", "?")) else s + ".") for s in statements if s)
    title = " + ".join(c.get("title", "") for c in sources if c.get("title"))
    if len(title) > 90:
        title = title[:89].rstrip() + "…"
    cats = {c.get("category", "structure") for c in sources}
    category = next(iter(cats)) if len(cats) == 1 else "structure"
    linked = sorted({a for c in sources for a in (c.get("linked_adrs") or [])})

    facts_path = ws.curated_facts if ws.curated_facts.exists() else ws.extracted_facts
    derived = content_hash(load_json(facts_path)) if facts_path.exists() else ""
    merged = _finalize_candidate({
        "kind": "merged-decision", "category": category, "subject_terms": [],
        "slug": _stub_slug("merged-" + "-".join(src_slugs)[:60] + "-"
                           + hashlib.sha256("".join(src_slugs).encode("utf-8")).hexdigest()[:6]),
        "title": title, "statement": statement, "merged_from": src_slugs,
        "elements": elements, "n_sources": len(elements),
        "total_weight": sum(c.get("total_weight", 0) for c in sources),
        "evidence": evidence,
    }, provenance="merged", method="manual", derived_hash=derived, linked_adrs=linked)

    _append_merged(ws, merged)
    # supersede: clear the constituents (they're folded into `merged` now). This also drops any
    # constituent that was ITSELF a prior merge. The merged slug is hash-distinct, so it survives.
    clear_candidates(ws, slugs=src_slugs)
    return merged


def render_stub(candidate: dict[str, Any], *, advisory: bool = False) -> str:
    """One ADR-CANDIDATE stub — evidence filled, everything human a literal TODO."""
    lines = [f"# ADR-CANDIDATE: {candidate['title']}", "",
             f"> {STUBS_NOTE}", "",
             "## Status", "",
             "CANDIDATE (machine-surfaced; promote by writing a real ADR"
             + ("; ADVISORY — model below the coverage floor" if advisory else "")
             + ")", "",
             "## Evidence (machine-extracted, cited)", "",
             candidate["statement"], ""]
    # the mined WHAT-only elaboration (#5) is part of the audited model output — keep it in the
    # durable stub, not just the inbox, so a promoted ADR carries the full cited evidence.
    if candidate.get("description"):
        lines += [candidate["description"], ""]
    for e in candidate["evidence"]:
        lines.append(f"- {e['type']}: `{e['detail']}`")
    lines += ["", "## Decision", "", "TODO (human)", "",
              "## Rationale", "", "TODO (human)", "",
              "## Rejected alternatives", "", "TODO (human)"]
    return "\n".join(lines) + "\n"


def render_stub_summary(report: dict[str, Any]) -> str:
    lines = ["# ADR-candidate decisions  (`arch adr stubs`)", "",
             f"> {STUBS_NOTE}", ""]
    if report["advisory"]:
        lines.append(f"- ⚠ L0 coverage {report['coverage_l0']:.0%} is below the floor "
                     "— candidates are advisory")
    lines.append(f"- {len(report['candidates'])} candidate(s)"
                 + (f", {report['dropped_over_cap']} dropped over the cap"
                    if report["dropped_over_cap"] else "")
                 + (f", {len(report['suppressed'])} suppressed (ADR exists)"
                    if report["suppressed"] else ""))
    lines.append("")
    for c in report["candidates"]:
        lines.append(f"- **{c['title']}** (`{c['slug']}`) — {c['statement']}")
    for s in report["suppressed"]:
        lines.append(f"- suppressed `{s['slug']}`: {s['reason']}")
    if not report["candidates"] and not report["suppressed"]:
        lines.append("_No candidate patterns above thresholds — this means "
                     "'none detected', not 'no decisions exist'._")
    return "\n".join(lines).rstrip() + "\n"


def run_stubs(ws: Workspace, *, cap: int | None = None) -> dict[str, Any] | None:
    """The ``arch adr stubs`` entry point: write ``generated/adr-stubs/<slug>.md`` +
    ``generated/adr-candidate-decisions.json`` (advisory, machine-owned — the stub dir
    is swept so a renamed/decided candidate leaves no stale stub)."""
    if ws.curated_facts.exists():
        facts_path = ws.curated_facts
    else:
        facts_path = ws.extracted_facts
    if not facts_path.exists():
        return None
    facts = load_json(facts_path)

    if ws.discovered_artifacts.exists():
        try:
            report = load_json(ws.discovered_artifacts)
        except (OSError, ValueError):
            report = {}
    else:
        from . import discover
        report = discover.discover(ws.repo, exclude_dirs=[ws.arch_dir])
    adrs = (report or {}).get("adrs", []) or []

    from .model import content_hash

    # api_skeleton (sidecar, not on the fact model) + hand-authored layering rules feed the
    # phase-2 pattern detector and the held-layering detector; both degrade to a clean no-op
    # when absent (the §4 extractor-convention discipline).
    try:
        from . import apiskeleton
        api_skeleton = apiskeleton.load(ws)
    except Exception:  # noqa: BLE001 - advisory; degrade to no api-skeleton signal
        api_skeleton = {}
    from .config import load_yaml
    try:
        layering_rules = load_yaml(ws.layering_rules)
    except Exception:  # noqa: BLE001 - a malformed layering-rules.yaml must not crash the verb
        layering_rules = {}

    result = decision_candidates(facts, adrs, cap=cap,
                                 api_skeleton=api_skeleton, layering_rules=layering_rules)
    derived = content_hash(facts)
    out = {"schema": STUBS_SCHEMA, "derived_from_hash": derived,
           "note": STUBS_NOTE, **result}

    ws.adr_stubs_dir.mkdir(parents=True, exist_ok=True)
    for old in ws.adr_stubs_dir.glob("*.md"):
        old.unlink()
    for c in out["candidates"]:
        (ws.adr_stubs_dir / f"{c['slug']}.md").write_text(
            render_stub(c, advisory=out["advisory"]), encoding="utf-8", newline="")
    dump_json(out, ws.adr_candidate_decisions_json)
    # §8/§9: refresh the unioned, dismissal-filtered inbox after writing the deterministic side
    # (reuse the hash already computed — no redundant fact-model read).
    materialize_inbox(ws, derived_hash=derived)
    return out


# ====================================================== Tier-2 LLM / agentic miner (§5)
# The owner's question — "can the narrative/agent machinery be reused for ADR candidate
# mining?" — answers itself: the miner points a DIFFERENT system prompt at the SAME narrative
# payload (so it inherits zero new egress data categories, §5.3), and interprets the returned
# cited key-points as decision candidates. Two structural barriers keep it §0.5-clean: the
# grounding gate (a candidate citing no real id is dropped, §5.4) and the causal/intent reject
# filter (a statement that phrases WHY is dropped, §0.1). Soft tier: written only on demand,
# NEVER in `arch run`, NEVER in CI.

MINING_SYSTEM_PROMPT = (
    "You are a software-architecture analyst. From the supplied element's public surface, "
    "anchors, dependencies, and package/technology facts, NAME the architecturally-significant "
    "decisions that appear ALREADY to have been made in this code, and cite the structural "
    "evidence for each using ONLY ids drawn from the supplied citable_ids.\n"
    "WHAT COUNTS AS ARCHITECTURALLY SIGNIFICANT (prefer these, in roughly this order):\n"
    " - a TECHNOLOGY / LIBRARY selection (the messaging broker, ORM, logging, serialization, "
    "auth, caching, mediator, resilience library it commits to);\n"
    " - an ARCHITECTURAL or DESIGN PATTERN the code embodies — e.g. CQRS, event-driven / "
    "publish-subscribe integration, the saga / process-manager, the outbox, repository / "
    "unit-of-work, API gateway, backend-for-frontend, layered/clean architecture. Look "
    "actively for these; they are the decisions an architect most wants recorded;\n"
    " - a BOUNDARY or INTEGRATION choice (a synchronous gRPC/REST seam vs. asynchronous "
    "messaging, a managed↔native interop boundary, an anti-corruption layer);\n"
    " - a CROSS-CUTTING consistency choice (the same persistence/logging/validation approach "
    "applied across components).\n"
    "DO NOT REPORT (these are noise, not decisions):\n"
    " - a plain container-to-container dependency that is ALREADY visible in the C4 diagram — "
    "'Webhooks depends on Building Blocks', 'Ordering uses the Building-Blocks library', "
    "'Catalog references Ordering'. Restating an edge the diagram already shows is not a "
    "decision. Only name a dependency when the INTERESTING part is the technology or pattern it "
    "realises (e.g. 'Basket persists to Redis', not 'Basket depends on the cache project').\n"
    "HARD RULES (a violation makes the output unusable):\n"
    " - START each statement with the SPECIFIC element under analysis, named by the supplied "
    "`name` field (e.g. 'Ordering implements…', 'The Catalog service exposes…'). NEVER open "
    "with a generic 'The container', 'The service', or 'This element'.\n"
    " - Describe only WHAT the decision is and the evidence for it. NEVER state WHY it was made, "
    "the motivation, the goal, the trade-off, or the alternatives considered. Do not use words "
    "like 'because', 'in order to', 'to ensure', 'so that', 'deliberately', or 'the rationale'. "
    "Name a pattern as a bare fact ('Ordering implements the CQRS pattern via MediatR'), never "
    "its purpose ('…to separate reads from writes' is forbidden — that is a WHY).\n"
    " - Every key point MUST cite at least one id from citable_ids; an uncited claim is "
    "discarded.\n"
    " - Do not invent element names or relationships. Use the supplied `name` for the element "
    "under analysis and cite only what is supplied.\n"
    "Return each decision as one key_points entry whose `text` is a SHORT decision sentence "
    "OPTIONALLY followed by one or two further WHAT-only sentences elaborating what the cited "
    "evidence shows (still never a WHY), and whose `cites` are the supporting ids."
)
MINING_PROMPT = (
    "List the architecturally-significant decisions this element appears to embody — favour "
    "technology selections and architectural/design patterns over plain dependencies, and skip "
    "anything that merely restates an edge already visible in the C4 diagram. One key point per "
    "decision: a WHAT-only statement that NAMES the element (use its `name`, not a generic 'the "
    "container'/'the service'), optionally with a sentence or two of supporting WHAT detail, "
    "plus its citing ids."
)

# §0.1/§3 causal/intent reject filter — phrasing that asserts WHY (motive/goal/trade-off).
_CAUSAL_RE = re.compile(
    r"\b(because|in order to|so as to|so that|to ensure|to avoid|to prevent|to enable|"
    r"to allow|to support|to improve|to reduce|to increase|to minimi[sz]e|to maximi[sz]e|"
    r"to optimi[sz]e|to simplify|to decouple|to guarantee|"
    r"for the purpose|for performance|for scalability|for maintainability|for security|"
    r"deliberately|intentionally|on purpose|the rationale|the reason|reasoning|motivat\w*|"
    r"we chose|we decided|chose to|decided to|aims? to|intended to|designed to|the goal|"
    r"in favour of|in favor of|trade[- ]?off|rejected|considered)\b",
    re.IGNORECASE,
)

_CAT_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # pattern first: an "event-driven repository" should bucket as a pattern, not a technology,
    # so the pattern needles win the first-match (#4 — patterns were under-surfaced).
    ("pattern", ("pattern", "cqrs", "mediator", "mediatr", "repository", "unit of work",
                 "event-driven", "event driven", "event bus", "eventbus", "integration event",
                 "saga", "process manager", "outbox", "inbox", "pub/sub", "publish/subscribe",
                 "publish-subscribe", "gateway", "backend for frontend", "backend-for-frontend",
                 "bff", "anti-corruption", "clean architecture", "layered architecture",
                 "domain event", "mvc")),
    ("framework", ("framework", "asp.net", "blazor", "controller", "minimal api", "razor")),
    ("technology", ("uses ", "use of", "library", "package", "nuget", "depends on the",
                    "persistence", "persists", "logging", "messaging", "serialization",
                    "caching", "cache", "broker")),
)


def _is_causal(text: str) -> bool:
    """True iff *text* phrases intent/motive — the mined tier may not say WHY (§0.1)."""
    return bool(_CAUSAL_RE.search(text or ""))


def _infer_category(text: str) -> str:
    """Best-effort §3 category for a mined statement (drives GUI grouping + budget). Falls
    back to ``structure`` — it is never a verdict, only a bucket."""
    low = (text or "").lower()
    for cat, needles in _CAT_HINTS:
        if any(n in low for n in needles):
            return cat
    return "structure"


# common abbreviations whose trailing '.' is NOT a sentence end — so _split_statement doesn't
# cut "…gRPC vs. REST" into a fragment. Lowercased, trailing dot stripped.
_ABBREVS = frozenset({"e.g", "i.e", "vs", "etc", "cf", "al", "approx", "no", "fig",
                      "inc", "ltd", "co", "incl", "esp"})


def _split_statement(text: str) -> tuple[str, str]:
    """Split a mined WHAT-text into ``(statement, description)`` (#5): the first sentence is the
    crisp decision shown as the headline; any following sentences are the WHAT-only elaboration
    shown beneath it. Both are verbatim substrings of the audited model output — nothing is
    paraphrased — so ``statement`` + ``description`` reconstruct exactly what the model returned.
    Skips a terminator that is actually an abbreviation ("vs.", "e.g."), a single initial, or a
    leading punctuation mark, so the headline is a real first sentence (a decimal like "7.4" is
    already safe — its dot isn't followed by whitespace)."""
    t = (text or "").strip()
    for m in re.finditer(r"[.!?](?:\s+|$)", t):
        head = t[:m.start()]
        if not head.strip():
            continue   # a leading terminator — not a sentence boundary
        last = re.split(r"[\s(]", head)[-1].lower().rstrip(".")
        if last in _ABBREVS or (len(last) == 1 and last.isalpha()):
            continue   # abbreviation / single initial — keep scanning
        return t[:m.end()].strip(), t[m.end():].strip()
    return t, ""


# #2 — a dependency-verb head. A mined statement whose verb is one of these AND whose object is
# another CONTAINER (not a technology) merely restates an edge the C4 diagram already shows; it
# is dropped as noise rather than surfaced as an ADR candidate (:func:`_is_bare_dependency`).
_DEP_VERB_RE = re.compile(
    r"\b(depends?\s+(?:on|upon)|relies?\s+(?:on|upon)|references?|calls?|invokes?|"
    r"talks?\s+to|communicates?\s+with|connects?\s+to|consumes?|imports?|uses?|"
    r"is\s+built\s+on|builds?\s+on|is\s+coupled\s+to|integrates?\s+with)\b",
    re.IGNORECASE)


def _is_bare_dependency(text: str, container_names: list[str]) -> bool:
    """True iff *text* merely restates a container→container dependency already visible in the
    C4 model (#2): a dependency verb whose subject AND object are both model containers, with no
    specific technology named. A statement that names a real technology (``_statement_techs``)
    or a pattern is never trivial — only the bare 'X depends on/uses container-Y' shape is."""
    if _statement_techs(text):
        return False   # names a real technology → a genuine decision, keep it
    if _infer_category(text) == "pattern":
        return False   # phrased as a pattern → keep it (the pattern is the decision). Reuse the
        # single _CAT_HINTS pattern list so this guard can never drift out of sync with it (#4).
    low = (text or "").lower()
    if not _DEP_VERB_RE.search(low):
        return False
    words = _words(text)
    hits = {n for n in container_names if n and _title_mentions(n, words)}
    return len(hits) >= 2


# #1 — flatten the package-family taxonomy to (needle, family) so a mined statement can be
# matched to a SPECIFIC technology and consolidated with its siblings across containers.
_TECH_NEEDLES: tuple[tuple[str, str], ...] = tuple(
    (needle, family) for family, needles in _PACKAGE_FAMILIES for needle in needles)


def _statement_techs(text: str) -> set[tuple[str, str, str]]:
    """The specific technologies a mined statement names, matched against the package-family
    taxonomy: a set of ``(needle, family, display)`` where *display* is the cased span from the
    statement (so ``rabbitmq`` surfaces as ``RabbitMQ``). Empty when no known technology is
    named — those candidates are never consolidated (#1). Matches each needle on alphanumeric
    word boundaries — the needles are package-name fragments, so a bare substring scan over
    PROSE would mis-fire (e.g. 'marten' inside 'smarten')."""
    out: set[tuple[str, str, str]] = set()
    for needle, family in _TECH_NEEDLES:
        m = re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", text or "",
                      re.IGNORECASE)
        if m:
            out.add((needle, family, text[m.start():m.end()]))
    return out


def _consolidate_mined(candidates: list[dict[str, Any]], cname: dict[str, str], *,
                       derived_hash: str) -> list[dict[str, Any]]:
    """Deterministic consolidation (#1, hybrid step 1): fold mined candidates that name the SAME
    specific technology (via the package-family taxonomy) across ≥2 anchor containers into one
    'N containers use X' candidate, preserving every per-container WHAT-statement as cited
    evidence (nothing invented, §0.5). Non-technology / singleton candidates pass through."""
    groups: dict[str, list[dict[str, Any]]] = {}
    meta: dict[str, tuple[str, str]] = {}      # needle -> (family, display)
    passthrough: list[dict[str, Any]] = []
    for c in candidates:
        techs = _statement_techs(c.get("statement", "")) \
            if c.get("provenance") == "mined" else set()
        if len(techs) == 1:
            needle, family, display = next(iter(techs))
            groups.setdefault(needle, []).append(c)
            meta.setdefault(needle, (family, display))
        else:
            passthrough.append(c)
    out = list(passthrough)
    for needle in sorted(groups):
        members = groups[needle]
        anchors = sorted({m.get("anchor") for m in members if m.get("anchor")})
        if len(members) < 2 or len(anchors) < 2:
            out.extend(members)                # not a real cross-container duplicate
            continue
        family, display = meta[needle]
        cat = "framework" if family in _FRAMEWORK_FAMILIES else "technology"
        names = [cname.get(a, a) for a in anchors]
        elements = sorted({e for m in members for e in (m.get("elements") or [])})
        statement = (f"{len(anchors)} containers — {', '.join(f'`{n}`' for n in names)} — "
                     f"use {display} ({family.replace('-', ' ')}).")
        description = ("Consolidated from per-container findings.  "
                       + "  ".join(m.get("statement", "") for m in members)).strip()
        evidence = [{"type": "mined-statement",
                     "element_ids": [m["anchor"]] if m.get("anchor") else [],
                     "source_ids": [], "detail": m.get("statement", "")}
                    for m in sorted(members,
                                    key=lambda m: (m.get("anchor") or "", m.get("slug", "")))]
        digest = hashlib.sha256(
            "".join(sorted(m.get("slug", "") for m in members)).encode("utf-8")).hexdigest()[:6]
        out.append(_finalize_candidate({
            "kind": "mined-decision", "category": cat, "subject_terms": [],
            "slug": _stub_slug(f"mined-consolidated-{needle}-{digest}"),
            "title": f"{len(anchors)} containers use {display}",
            "statement": statement, "description": description,
            "elements": elements, "n_sources": len(anchors),
            "total_weight": sum(m.get("total_weight", 0) for m in members),
            "evidence": evidence,
        }, provenance="mined", method="consolidated", derived_hash=derived_hash, linked_adrs=[]))
    out.sort(key=_salience)
    return out


# #1 hybrid step 2 (optional, opt-in): a SECOND LLM pass that fuses leftover mined candidates
# describing the same decision across components. It re-sends only ALREADY-EMITTED WHAT
# statements (model outputs — no new egress category, §5.3) and degrades to the input unchanged
# on any provider hiccup, so it is always safe to skip.
CONSOLIDATE_SYSTEM_PROMPT = (
    "You are given a list of architecture DECISION STATEMENTS already mined from one system, "
    "each with a slug, its WHAT-statement, and the elements it touches. Identify groups that "
    "describe the SAME underlying technology or pattern decision spread across different "
    "components (e.g. several services that each 'use RabbitMQ' are one messaging decision). "
    "Return one key_points entry PER GROUP of two or more statements that are genuinely the same "
    "decision. Each entry's `text` is a single consolidated WHAT-statement (no WHY — never a "
    "motivation, goal, or trade-off), `merge` is the list of slugs it fuses, and `cites` are ids "
    "drawn ONLY from the supplied citable_ids. Never group unrelated decisions; never invent a "
    "decision; if nothing should be merged, return no key_points."
)
CONSOLIDATE_PROMPT = (
    "Group the decision statements that describe the same technology/pattern decision across "
    "components. One key point per group of 2+ slugs; WHAT-only `text`, the fused `merge` slugs, "
    "and citing ids."
)


def _consolidate_llm(candidates: list[dict[str, Any]], provider: Any, cname: dict[str, str],
                     cite_universe: set[str], containers: Any, t2c: dict[str, str], *,
                     derived_hash: str) -> list[dict[str, Any]]:
    """Optional LLM residue merge (#1, hybrid step 2). Returns the candidate list with fused
    groups replacing their constituents; returns it unchanged on too-few inputs or any provider
    error (honest degradation — the deterministic pass already ran)."""
    mined = [c for c in candidates if c.get("provenance") == "mined"
             and c.get("method") in ("llm", "agentic")]
    if len(mined) < 2:
        return candidates
    by_slug = {c["slug"]: c for c in mined}
    payload = {"task": "consolidate-decisions",
               "candidates": [{"slug": c["slug"], "statement": c.get("statement", ""),
                               "elements": c.get("elements", [])} for c in mined],
               "citable_ids": sorted(cite_universe)}
    try:
        rec = provider.complete(CONSOLIDATE_SYSTEM_PROMPT, CONSOLIDATE_PROMPT, payload)
    except Exception:  # noqa: BLE001 - residue merge is best-effort; never break the mine
        return candidates
    if not isinstance(rec, dict):
        return candidates
    consolidated: list[dict[str, Any]] = []
    fused: set[str] = set()
    for kp in rec.get("key_points", []) or []:
        text = str(kp.get("text", "") or "").strip()
        if not text or _is_causal(text):
            continue
        merge = sorted({s for s in (kp.get("merge") or kp.get("slugs") or []) if s in by_slug})
        if len(merge) < 2:
            continue
        cites = sorted({c for c in (kp.get("cites") or []) if c in cite_universe})
        elems: set[str] = set()
        for c in cites:
            elems |= _lift_cite(c, "", containers, t2c)
        members = [by_slug[s] for s in merge]
        elements = sorted(elems | {e for m in members for e in (m.get("elements") or [])})
        statement, description = _split_statement(text)
        evidence = [{"type": "mined-statement",
                     "element_ids": (m.get("elements") or [])[:1],
                     "source_ids": [], "detail": m.get("statement", "")} for m in members]
        digest = hashlib.sha256("".join(merge).encode("utf-8")).hexdigest()[:8]
        consolidated.append(_finalize_candidate({
            "kind": "mined-decision", "category": _infer_category(text), "subject_terms": [],
            "slug": _stub_slug(f"mined-merged-{digest}"),
            "title": statement if len(statement) <= 90 else statement[:89].rstrip() + "…",
            "statement": statement, "description": description,
            "elements": elements, "n_sources": len(elements),
            "total_weight": sum(m.get("total_weight", 0) for m in members),
            "evidence": evidence,
        }, provenance="mined", method="llm-consolidated", derived_hash=derived_hash,
            linked_adrs=[]))
        fused.update(merge)
    if not consolidated:
        return candidates
    out = [c for c in candidates if c.get("slug") not in fused] + consolidated
    out.sort(key=_salience)
    return out


def _lift_cite(cite: str, element_id: str, containers: Any,
               t2c: dict[str, str]) -> set[str]:
    """Lift a cited id to container/system altitude (§0.3 #4: evidence may be component-grained,
    the emitted candidate is aggregated up). Drops adr: cites (not C4 elements)."""
    out: set[str] = set()
    if not cite or cite.startswith("adr:"):
        return out
    if "->" in cite:
        for part in cite.split("->"):
            out |= _lift_cite(part.strip(), element_id, containers, t2c)
        return out
    if cite in containers:
        out.add(cite)
    elif cite in t2c:
        out.add(t2c[cite])
    elif cite == element_id:
        out.add(cite)
    return out


def _candidates_from_narrative(rec: dict[str, Any], element_id: str, cite_universe: set[str],
                               containers: Any, t2c: dict[str, str], *, method: str,
                               derived_hash: str) -> list[dict[str, Any]]:
    """Convert one narrative-shaped reply into rationale-free, cited decision candidates,
    applying the §5.4 grounding gate + the §0.1 causal/intent reject filter."""
    out: list[dict[str, Any]] = []
    if not isinstance(rec, dict):
        return out   # a misbehaving provider returned a non-record — honest absence, never crash
    container_names = [getattr(containers[c], "name", c) for c in containers]
    for kp in rec.get("key_points", []) or []:
        text = str(kp.get("text", "") or "").strip()
        if not text or _is_causal(text):
            continue   # §0.1 — the mined tier may state THAT, never WHY
        if _is_bare_dependency(text, container_names):
            continue   # #2 — a plain container→container edge the C4 diagram already shows
        cites = sorted({c for c in (kp.get("cites") or []) if c in cite_universe})
        if not cites:
            continue   # §5.4 grounding gate — an uncited candidate is dropped, never softened
        elems: set[str] = set()
        for c in cites:
            elems |= _lift_cite(c, element_id, containers, t2c)
        elements = sorted(elems) or [element_id]
        # The ANCHOR is the container this decision is ABOUT — the element whose payload was
        # mined. The other `elements` are only the endpoints its cited evidence touches, lifted
        # to container altitude. Carry the anchor explicitly and lead the display title with its
        # name, so the inbox row is self-identifying instead of an unnamed "The container uses…"
        # the reader must reverse-engineer from the evidence (mining §10). `statement`+`description`
        # are VERBATIM substrings of the audited model output, split at the first sentence (#5):
        # the headline decision, then its WHAT-only elaboration. The mining prompt asks the model
        # to name the element itself; the prefix is the DETERMINISTIC guarantee for when it
        # doesn't — when the name already leads the prose we skip it (no "Catalog — Catalog …").
        anchor = element_id
        statement, description = _split_statement(text)
        label = containers[anchor].name if anchor in containers else anchor
        # lead the title with the subject name UNLESS the statement already opens with it — a
        # token-run check (not a substring), so a short name like "Web" doesn't count itself as
        # present inside an unrelated word like "webhook" and suppress the helpful prefix (#13).
        name_words = _words(label)
        leads = bool(name_words) and _words(statement)[:len(name_words)] == name_words
        titled = statement if leads else f"{label} — {statement}"
        title = titled if len(titled) <= 90 else titled[:89].rstrip() + "…"
        evidence = [{"type": "citation",
                     "element_ids": sorted(_lift_cite(c, element_id, containers, t2c)),
                     "source_ids": [c], "detail": f"cites `{c}`"}
                    for c in cites]
        out.append(_finalize_candidate({
            "kind": "mined-decision", "category": _infer_category(text), "subject_terms": [],
            "slug": _stub_slug(f"mined-{element_id}-{text[:40]}-"
                               + hashlib.sha256(text.encode("utf-8")).hexdigest()[:6]),
            "title": title, "statement": statement, "description": description, "anchor": anchor,
            "elements": elements, "n_sources": len(elements), "total_weight": len(cites),
            "evidence": evidence,
        }, provenance="mined", method=method, derived_hash=derived_hash, linked_adrs=[]))
    return out


def mine_candidates(ws: Workspace, *, provider: Any, deep: set[str] | None = None,
                    cap: int | None = None,
                    llm_consolidate: bool = False) -> list[dict[str, Any]]:
    """§5 Tier-2: point the MINING system prompt at the SAME narrative payload (no new egress),
    parse the returned cited key-points as decision candidates, and apply the grounding gate +
    the causal/intent reject filter + the #2 bare-dependency drop. Then CONSOLIDATE per-container
    duplicate technology choices into one candidate (#1, deterministic; *llm_consolidate* adds the
    optional second LLM merge for the residue). Mines CONTAINER altitude only (system-altitude
    mining input is the §16 open question, deferred to phase 4). Returns the mined candidate list;
    pure of workspace writes (the caller :func:`run_mine` writes the soft sidecar)."""
    from .model import content_hash
    from .stages import enrich_llm

    facts_path = ws.curated_facts if ws.curated_facts.exists() else ws.extracted_facts
    if not facts_path.exists() or provider is None:
        return []
    curated = load_json(facts_path)
    redaction, _src = enrich_llm.effective_redaction(ws)
    containers, cedges, cite_universe = enrich_llm.narrative_context(curated)
    ctx = enrich_llm.NarrativeInputs(ws, curated, containers, cedges)
    cite_universe = cite_universe | ctx.adr_cite_ids()
    t2c = _t2c(curated)
    cname = {cid: getattr(containers[cid], "name", cid) for cid in containers}
    derived = content_hash(curated)
    deep = set(deep or [])

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cid in sorted(containers):
        if {"external", "person"} & containers[cid].tags:
            continue   # mine the system under study, not its boundary
        band = ctx.band_for(cid)
        payload = enrich_llm.container_payload(cid, containers, cedges, curated, redaction,
                                               ctx=ctx, band=band)
        method = "llm"
        if cid in deep:
            payload = enrich_llm._agentic_augment(payload, cid, curated, ctx, redaction)
            method = "agentic"
        try:
            rec = provider.complete(MINING_SYSTEM_PROMPT, MINING_PROMPT, payload)
        except Exception:  # noqa: BLE001 - honest absence: a failed element yields no candidate
            continue
        for c in _candidates_from_narrative(rec, cid, cite_universe, containers, t2c,
                                            method=method, derived_hash=derived):
            if c["slug"] in seen:
                continue
            seen.add(c["slug"])
            out.append(c)
    # #1 — fold per-container duplicate technology choices into one candidate (deterministic),
    # then optionally a second LLM pass for the fuzzier residue.
    out = _consolidate_mined(out, cname, derived_hash=derived)
    if llm_consolidate:
        out = _consolidate_llm(out, provider, cname, cite_universe, containers, t2c,
                               derived_hash=derived)
    out.sort(key=_salience)
    if cap is not None and cap >= 0:
        out = out[:cap]
    return out


def run_mine(ws: Workspace, *, llm: bool = False, provider: Any = None,
             deep: set[str] | None = None, cap: int | None = None,
             llm_consolidate: bool = False,
             accept_unredacted: bool = False) -> dict[str, Any] | None:
    """``arch adr mine`` entry point. Without ``--llm`` (or no provider) it is an alias for
    re-running the Tier-1 deterministic detectors (§5.6). With ``--llm`` it gates egress
    fail-closed, writes the exact post-redaction preview (no provider call) so what-leaves is
    auditable first, mines, writes the SOFT sidecar ``adr-mined-candidates.json``, and refreshes
    the inbox. *cap* caps both the mined list and the materialized inbox for this run;
    *llm_consolidate* runs the optional second LLM merge pass (#1). NEVER called from `arch run`;
    never in CI."""
    if not llm or provider is None:
        return run_stubs(ws, cap=cap)
    from .model import content_hash
    from .stages import enrich_llm

    ok, msg = enrich_llm.egress_gate(ws, "llm-propose", accept_unredacted)
    if not ok:
        return {"schema": MINED_SCHEMA, "error": msg, "candidates": []}
    if ws.curated_facts.exists():
        try:
            enrich_llm.write_egress_preview(ws)  # band-for-band identical to what the miner sends
        except Exception:  # noqa: BLE001 - advisory preview; never block the run
            pass

    cands = mine_candidates(ws, provider=provider, deep=deep, cap=cap,
                            llm_consolidate=llm_consolidate)
    facts_path = ws.curated_facts if ws.curated_facts.exists() else ws.extracted_facts
    derived = content_hash(load_json(facts_path)) if facts_path.exists() else None
    out = {"schema": MINED_SCHEMA, "derived_from_hash": derived, "note": STUBS_NOTE,
           "method": "agentic" if deep else "llm", "candidates": cands}
    dump_json(out, ws.adr_mined_candidates_json)
    try:
        enrich_llm.write_egress_manifest(ws, provider_desc=getattr(provider, "model_id", "?"),
                                         model_id=getattr(provider, "model_id", "?"))
    except Exception:  # noqa: BLE001 - manifest is best-effort for the on-demand miner
        pass
    # a per-run cap also bounds the materialized inbox for this run; with no cap the inbox falls
    # back to the persistent `anon.adr_max_candidates` setting (#3).
    materialize_inbox(ws, max_candidates=cap)
    return out


# ============================================ Tier-3 authoring agent + promote (§6)
# The architect supplies the SUBSTANCE (decision / rationale / considered alternatives) as
# sectioned prose; the machine STRUCTURES it into a well-formed ADR with the Evidence + the
# Linked-C4 elements pre-filled from the candidate. §0.5 holds because the rationale text is
# the human's — the machine shapes prose + grounds the doc, its sanctioned role. Empty input
# sections stay a literal `TODO (human)`; the constraint offer is category-aware + human-gated.

DRAFT_SYSTEM_PROMPT = (
    "You restructure an architect's own ADR notes into clean prose. You are given the human's "
    "sections. Improve grammar, flow, and structure ONLY. NEVER add a decision, a rationale, a "
    "consequence, or an alternative the human did not write. NEVER fill an empty section. Return "
    "ONLY a JSON object {\"sections\": {<same keys you were given>: <improved prose string>}}."
)
DRAFT_PROMPT = "Restructure these ADR sections into clean prose without adding any content."

# MADR-ish draft sections, in render order. Empty human input stays `TODO (human)` (§0.5).
_DRAFT_SECTIONS: tuple[tuple[str, str], ...] = (
    ("context", "## Context"),
    ("decision", "## Decision"),
    ("rationale", "## Rationale"),
    ("consequences", "## Consequences"),
    ("alternatives", "## Considered alternatives"),
)


def find_candidate(ws: Workspace, slug: str) -> dict[str, Any] | None:
    """Look up a candidate by slug across the inbox, the deterministic decisions, and the soft
    mined/merged sidecars (in that precedence). Returns ``None`` if no candidate has that slug."""
    for path in (ws.adr_candidate_inbox_json, ws.adr_candidate_decisions_json,
                 ws.adr_mined_candidates_json, ws.adr_merged_candidates_json):
        if not path.exists():
            continue
        try:
            data = load_json(path) or {}
        except (OSError, ValueError):
            continue
        for bucket in ("candidates", "dismissed"):
            for c in data.get(bucket, []) or []:
                if c.get("slug") == slug:
                    return c
    return None


def _polish_sections(provider: Any, slug: str,
                     sections: dict[str, str]) -> tuple[dict[str, str], str]:
    """Optionally restructure the human's NON-EMPTY sections via the provider (§6.1). §0.5-safe:
    an empty section is never filled, and a section the human did not supply is never created;
    any provider hiccup (incl. the live provider mis-routing a non-narrative payload) falls back
    to verbatim deterministic structuring."""
    if provider is None:
        return sections, "deterministic"
    nonempty = {k: v for k, v in sections.items() if (v or "").strip()}
    if not nonempty:
        return sections, "deterministic"
    payload = {"element_id": slug, "task": "structure-adr", "sections": nonempty}
    try:
        rec = provider.complete(DRAFT_SYSTEM_PROMPT, DRAFT_PROMPT, payload)
    except Exception:  # noqa: BLE001 - structuring is best-effort; fall back to the human's prose
        return sections, "deterministic"
    polished = rec.get("sections") if isinstance(rec, dict) else None
    if not isinstance(polished, dict):
        return sections, "deterministic"
    out = dict(sections)
    for k in nonempty:   # only sections the human supplied — never fill an empty one (§0.5)
        v = polished.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out, str(getattr(provider, "model_id", "llm"))


def _offered_constraints(ws: Workspace, candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """§6.1 detect-and-offer: a checkable constraint is offered ONLY for a structure candidate
    carrying an explicit invariant shape — a held layering convention. NEVER derived from
    ordinary positive dependency evidence, and NEVER for technology candidates (forcing a
    constraint there is the distortion we avoid). Each offer carries a live HELD/VIOLATED/
    UNVERIFIED verdict from the same check_layering engine `arch adr check` uses."""
    if candidate.get("category") != "structure" or candidate.get("kind") != "layering-convention":
        return []
    from .config import load_yaml
    rules = load_yaml(ws.layering_rules)
    forbidden = (rules or {}).get("forbidden") or []
    if not forbidden:
        return []
    facts_path = ws.curated_facts if ws.curated_facts.exists() else ws.extracted_facts
    if not facts_path.exists():
        return []
    facts = load_json(facts_path)
    try:
        from .stages.drift_report import check_layering
    except Exception:  # noqa: BLE001
        return []
    by_rule = {(f.get("from"), f.get("to")): f.get("status")
               for f in check_layering(facts, rules)}
    out: list[dict[str, Any]] = []
    for r in forbidden:
        frm, to = r.get("from"), r.get("to")
        if not frm or not to:
            continue
        verdict = {"VIOLATION": "VIOLATED", "OK": "HELD"}.get(
            by_rule.get((frm, to)), "UNVERIFIED")
        out.append({"sentence": f"`{frm}` must not depend on `{to}`", "verdict": verdict,
                    "from": frm, "to": to})
    return out


def _render_draft(candidate: dict[str, Any], sections: dict[str, str],
                  offered: list[dict[str, Any]], accepted: set[str],
                  structured_by: str, human_input_hash: str,
                  title: str | None) -> str:
    """Assemble the MADR draft: human prose under each heading (empty → TODO (human)),
    the machine-extracted Evidence + Linked-C4 pre-fill, accepted constraints, and honest
    provenance front-matter (`structured_by` / `human_input_hash` / `machine_filled`)."""
    head = title or candidate.get("title", "Architecture decision")
    lines = ["---", "status: proposed", f"structured_by: {structured_by}",
             f"human_input_hash: {human_input_hash}",
             "machine_filled: [evidence, linked_c4_elements]", "---", "",
             f"# {head}", ""]
    for key, heading in _DRAFT_SECTIONS:
        content = (sections.get(key) or "").strip() or "TODO (human)"
        lines += [heading, "", content, ""]
        if key == "decision":
            for o in offered:
                if o["sentence"] in accepted:
                    lines += [f"Constraint (checked every run — §2a): {o['sentence']} "
                              f"— currently {o['verdict']}.", ""]
    lines += ["## Evidence (machine-extracted, cited)", "", candidate.get("statement", ""), ""]
    if candidate.get("description"):   # the WHAT-only elaboration (#5) — keep it in the draft
        lines += [candidate["description"], ""]
    for e in candidate.get("evidence", []) or []:
        lines.append(f"- {e.get('type')}: `{e.get('detail', '')}`")
    lines += ["", "## Linked C4 elements", ""]
    for el in candidate.get("elements", []) or []:
        lines.append(f"- `{el}` (tagged `adr:<id>` once promoted)")
    not_accepted = [o for o in offered if o["sentence"] not in accepted]
    if not_accepted:
        lines += ["", "## Offered constraint (NOT added — accept to include)", ""]
        for o in not_accepted:
            lines.append(f"- {o['sentence']} — §2a would check this every run "
                         f"(currently {o['verdict']})")
    return "\n".join(lines).rstrip() + "\n"


def draft_adr(ws: Workspace, slug: str, sections: dict[str, str] | None, *,
              provider: Any = None, accept_constraints: list[str] | None = None,
              title: str | None = None) -> dict[str, Any] | None:
    """§6.1: structure the human's sectioned prose into a well-formed ADR draft with Evidence +
    Linked-C4 pre-filled from the candidate. Does NOT write to the repo (the GUI reviews first);
    the writing surface is :func:`promote_adr`. Returns ``None`` if the slug is unknown."""
    candidate = find_candidate(ws, slug)
    if candidate is None:
        return None
    sections = {k: (str(v) if v is not None else "")
                for k, v in (sections or {}).items()}
    polished, structured_by = _polish_sections(provider, slug, sections)
    human_input_hash = _evidence_hash(
        [{"section": k, "text": v} for k, v in sorted(sections.items())])
    offered = _offered_constraints(ws, candidate)
    accepted = set(accept_constraints or [])
    draft_md = _render_draft(candidate, polished, offered, accepted, structured_by,
                             human_input_hash, title)
    return {"slug": slug, "title": title or candidate.get("title"),
            "draft_md": draft_md, "offered_constraints": offered,
            "machine_filled": ["evidence", "linked_c4_elements"],
            "human_input_hash": human_input_hash, "structured_by": structured_by}


def adr_target_dir(ws: Workspace) -> str:
    """The repo-relative directory a promoted ADR lands in: the most-common discovered ADR dir
    (deterministic tiebreak: ``(-count, dir)``), else discovery's first convention. Shared by
    the CLI promote verb and the serve promote route so both write to the same place (§6.2)."""
    from pathlib import Path

    from . import discover
    adr_dirs: dict[str, int] = {}
    if ws.discovered_artifacts.exists():
        try:
            report = load_json(ws.discovered_artifacts)
        except (OSError, ValueError):
            report = {}
        for a in (report or {}).get("adrs", []) or []:
            if a.get("path"):
                d = str(Path(str(a["path"])).parent).replace("\\", "/")
                adr_dirs[d] = adr_dirs.get(d, 0) + 1
    if adr_dirs:
        return min(adr_dirs, key=lambda d: (-adr_dirs[d], d))
    return discover.ADR_DIRS[0]


def _next_adr_number(dest_dir: Any) -> int:
    from . import discover
    nums: list[int] = []
    if dest_dir.exists():
        for p in sorted(dest_dir.glob("*.md")):
            m = discover._ADR_ID_RE.match(p.name)
            if m:
                try:
                    nums.append(int(m.group("id")))
                except (TypeError, ValueError):
                    pass
    return (max(nums) + 1) if nums else 1


def _promoted_adr_file(dest_dir: Any, slug: str):
    """The promoted ADR file for *slug* under *dest_dir* (the ``NNNN-<slug>.md`` a promote writes,
    or the bare ``<slug>.md``), or ``None`` when the candidate has not been promoted. The numeric
    prefix is matched precisely so a candidate whose slug is a *suffix* of a longer promoted slug
    (e.g. ``serilog`` vs ``tech-standardize-serilog``) is not mistaken for promoted. Shared by the
    promote already-promoted guard and the §9 inbox filter so "already promoted" has one
    definition."""
    if not dest_dir.exists():
        return None
    pat = re.compile(rf"\d+-{re.escape(slug)}\.md")
    matches = sorted(p for p in dest_dir.glob("*.md") if pat.fullmatch(p.name))
    bare = dest_dir / f"{slug}.md"
    if bare.exists():
        matches.append(bare)
    return matches[0] if matches else None


def promoted_adr_slugs(ws: Workspace, slugs: list[str]) -> set[str]:
    """The subset of *slugs* that already have a promoted ADR file in the repo's ADR directory.
    A promoted candidate is a closed loop — it is an ADR now, not a candidate — so the §9 inbox
    drops it (it would otherwise linger forever, the duplicate-of-itself the user hits). A pure
    read of the hand-owned ADR dir; it never perturbs the hashed core."""
    if not slugs:
        return set()
    dest_dir = ws.repo / adr_target_dir(ws)
    if not dest_dir.exists():
        return set()
    names = {p.name for p in dest_dir.glob("*.md")}
    out: set[str] = set()
    for slug in slugs:
        if f"{slug}.md" in names or any(
                re.fullmatch(rf"\d+-{re.escape(slug)}\.md", n) for n in names):
            out.add(slug)
    return out


def promote_adr(ws: Workspace, slug: str, body: str | None = None, *,
                title: str | None = None) -> dict[str, Any] | None:
    """§6.2 promote: write the reviewed, human-authored *body* (or, when ``None``, the engine
    stub for the candidate) into the repo's ADR directory as the next-numbered file. Scaffold-
    once / never-overwrite / hand-owned the moment it lands; refreshes discovery so the next
    `arch adr check` sees it. Returns the written path (or ``None`` for an unknown slug)."""
    # slug becomes the filename — guard path traversal HERE (defense in depth; the CLI verb does
    # not pre-sanitize as the serve route does, and the repo-escape check below only validates the
    # directory, not the slug-bearing filename).
    if not slug or any(sep in slug for sep in ("/", "\\", "..")):
        raise ValueError(f"unsafe ADR slug {slug!r} (no path separators or '..')")
    candidate = find_candidate(ws, slug)
    if body is None:
        if candidate is not None:
            body = render_stub(candidate)
        else:
            # backward-compat: promote the committed engine stub .md if it exists (the
            # pre-mining promote surface), else there is nothing to promote.
            stub_path = ws.adr_stubs_dir / f"{slug}.md"
            if stub_path.is_file():
                body = stub_path.read_text(encoding="utf-8")
            else:
                return None
    rel_dir = adr_target_dir(ws)
    dest_dir = ws.repo / rel_dir
    try:
        dest_dir.resolve().relative_to(ws.repo.resolve())
    except ValueError as exc:
        raise ValueError(f"ADR directory {rel_dir!r} escapes the repo") from exc
    dest_dir.mkdir(parents=True, exist_ok=True)
    # already-promoted guard: a file for THIS slug (any number prefix) means this candidate was
    # already promoted — refuse rather than mint a duplicate (the serve route's 409 semantics).
    prior = _promoted_adr_file(dest_dir, slug)
    if prior is not None:
        raise FileExistsError(
            f"{rel_dir}/{prior.name} already exists — '{slug}' was already promoted")
    num = _next_adr_number(dest_dir)
    fname = f"{num:04d}-{slug}.md"
    dest = dest_dir / fname
    if dest.exists():
        raise FileExistsError(f"{rel_dir}/{fname} already exists — promote never overwrites")
    # LF always (.gitattributes / CLAUDE.md): a Windows-edited or browser-submitted body may carry
    # CRLF — normalize before writing so no CRLF lands in the tracked ADR artifact.
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    if not body.endswith("\n"):
        body += "\n"
    with open(dest, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    try:
        from . import discover
        discover.run(ws)   # refresh discovery so the report does not go stale (§6.2)
    except Exception:  # noqa: BLE001 - advisory refresh; the file is already written
        pass
    # §8 inbox-refresh discipline: a promote changes what the inbox should show (the now-promoted
    # candidate must leave it), so re-materialize before returning — exactly like dismiss/clear.
    # Advisory: the ADR is already written, so a hiccup here must not fail the promote.
    try:
        materialize_inbox(ws)
    except Exception:  # noqa: BLE001
        pass
    return {"path": f"{rel_dir}/{fname}", "file": fname, "id": f"{num:04d}", "dir": rel_dir}


def _main(argv: list[str] | None = None) -> int:  # pragma: no cover
    from . import force_utf8_stdio
    force_utf8_stdio()   # the §22.3 reconfig the arch CLI applies — needed here too
    ap = argparse.ArgumentParser(prog="anon.adr", description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--arch-dir")
    ap.add_argument("--check", action="store_true",
                    help="run the §2a conformance check (the `arch adr check` path)")
    ap.add_argument("--stubs", action="store_true",
                    help="emit the §7.4 ADR-candidate stubs (the `arch adr stubs` path)")
    args = ap.parse_args(argv)
    ws = resolve_workspace(args.repo, args.arch_dir)
    if args.check:
        report = run_check(ws)
        if report is None:
            print("[adr] no fact model — run `arch run` first.")
            return 2
        print(render_conformance(report), end="")
        return 0
    if args.stubs:
        report = run_stubs(ws)
        if report is None:
            print("[adr] no fact model — run `arch run` first.")
            return 2
        print(render_stub_summary(report), end="")
        return 0
    s = run(ws)
    print(f"[adr] adrs={s['adrs']} tagged={s['tagged']} candidates={s['candidates']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
