"""Adopt an existing hand-crafted model AS A CURATION SEED (§4.5.2 / §7.4).

This module is the one-time **adopt-as-seed** bootstrap (§4.5.2 use (i)): it parses a
hand-authored Structurizr model — either a Structurizr DSL file (``*.dsl``) or a full
Structurizr ``workspace.json`` — into a neutral :class:`ParsedModel`, then lifts that
into a ``mapping-rules.yaml``-shaped fragment the architect reviews and locks like any
other curation proposal (§7.1). The seed never invents structure; it only pre-fills the
same rule constructs (``name_overrides``, ``group``, ``descriptions``) a human would
otherwise hand-write, plus a ``seed:`` provenance block (§7.4 example).

**Pure functions only.** No CLI, no file discovery, no I/O beyond reading the one model
path handed to it. Discovery (§4.5.1) and orchestration (``arch curate --seed-from``) are
the caller's job — see this module's WIRING SPEC in the task report.

**The honest id caveat (§4.5.3 / §7.5).** We cannot know the real *extracted* stable ids
(``cpp:target:OrderProcessor``) at seed time — they come from the build graph, which the
seed has not seen. So ``name_overrides`` and ``descriptions`` are keyed by the element's
*own* hand-crafted identifier/name, and ``seed.id_reconciliation`` is stamped ``pending``.
The orchestrator/§7.5 reconciliation later rebinds these keys to extracted stable ids via
``human_id_bindings.yaml``; until then the keys are honest placeholders, not real ids.

Determinism is a hard CI gate (§1.1 metric 2): every collection produced here is sorted,
there are no timestamps/random, and :func:`to_yaml` emits sorted-key block YAML with LF.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# §7.4 `seed:` provenance: the value stamped before §7.5 human-id reconciliation runs.
_ID_RECONCILIATION_PENDING = "pending"

# Structurizr element-declaration keywords whose instances we capture as architecture
# elements (§4.5.2). `group` is handled separately (it is a container of elements, not an
# element); `softwareSystem`/`container`/`component`/`person` are leaf/parent elements.
_ELEMENT_KEYWORDS = ("softwareSystem", "container", "component", "person")

# A Structurizr element declaration line, e.g.
#   orderProcessor = container "Order Processor" "Handles orders" "C#"
#   container "Order Processor" "Handles orders"
#   person "Operator"
# Capture: optional `<ident> =`, the keyword, then up to three "double-quoted" strings
# (name, description, technology). Trailing `{` (opening a block) is tolerated by the
# brace-walking caller, which strips it before this matches.
_ASSIGN_RE = re.compile(r'^\s*(?:(?P<ident>[A-Za-z_][\w.]*)\s*=\s*)?(?P<kw>[A-Za-z]+)\b(?P<rest>.*)$')
_GROUP_RE = re.compile(r'^\s*group\s+(?P<q>"(?:[^"\\]|\\.)*")\s*\{?\s*$')
# A run of double-quoted, possibly backslash-escaped strings (Structurizr DSL tokens).
_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_INCLUDE_RE = re.compile(r'^\s*!')  # !include / !docs / !adrs etc. — skipped (§4.5.2)


@dataclass
class ParsedModel:
    """Neutral parse of a hand-crafted Structurizr model (§4.5.2).

    ``elements`` — list of ``{key, name, description, technology, group}`` dicts, sorted
    deterministically by ``(group, key, name)``. ``key`` is the element's hand-crafted
    identifier when it declared one, else a slug of its display name (so unnamed
    declarations still seed something stable). ``group`` is the enclosing ``group "..."``
    label (or parent element name when nested without an explicit group), or ``""``.

    ``groups`` — sorted unique list of every ``group "..."``/parent label seen.

    ``views_text`` / ``styles_text`` — the raw ``views { ... }`` / ``styles { ... }`` block
    bodies captured verbatim (§4.5.2: imported into the hand-owned ``views.dsl`` once,
    never regenerated). Empty string when absent.
    """

    elements: list[dict[str, str]] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    views_text: str = ""
    styles_text: str = ""


# --------------------------------------------------------------------------- parsing

def _read(path: Path) -> str:
    # utf-8-sig: hand-authored DSL/JSON on Windows is frequently BOM-prefixed (jsonio.py
    # makes the same choice for externally-supplied snapshots).
    return Path(path).read_text(encoding="utf-8-sig")


def _slug(name: str) -> str:
    """A stable, lossless-enough key for an unnamed element (display name → token)."""
    s = re.sub(r"[^0-9A-Za-z]+", "-", name.strip()).strip("-").lower()
    return s or "element"


def _strings(rest: str) -> list[str]:
    """Extract the leading run of double-quoted strings from a declaration's remainder."""
    return [m.group(1) for m in _STRING_RE.finditer(rest)]


def _capture_block(lines: list[str], start: int) -> tuple[str, int]:
    """Capture a ``{ ... }`` block body starting at the line that opened it.

    *start* is the index of the line containing the opening ``{`` (e.g. ``views {``).
    Returns ``(body_text, index_of_line_after_closing_brace)``. The body excludes the
    opening/closing brace lines and is dedented-as-written (verbatim, §4.5.2). Brace
    counting ignores braces inside double-quoted strings so a name like ``"a{b}"`` does
    not unbalance the walk.
    """
    depth = 0
    body: list[str] = []
    i = start
    seen_open = False
    while i < len(lines):
        line = lines[i]
        # strip quoted strings before counting braces so quoted { } don't miscount.
        bare = _STRING_RE.sub('""', line)
        opens = bare.count("{")
        closes = bare.count("}")
        if not seen_open:
            # The opening line: everything after its first '{' begins the body.
            depth += opens - closes
            seen_open = True
            after = line.split("{", 1)[1] if "{" in line else ""
            if depth <= 0:
                # single-line block: `views { ... }`
                inner = after.rsplit("}", 1)[0]
                if inner.strip():
                    body.append(inner)
                return ("\n".join(body), i + 1)
            if after.strip():
                body.append(after)
            i += 1
            continue
        depth += opens - closes
        if depth <= 0:
            before = line.rsplit("}", 1)[0]
            if before.strip():
                body.append(before)
            return ("\n".join(body), i + 1)
        body.append(line)
        i += 1
    return ("\n".join(body), i)


def _parse_dsl(text: str) -> ParsedModel:
    """Pragmatic line/brace parser for a Structurizr DSL model (§4.5.2).

    NOT a full grammar — a brace-aware line walk that recognizes element declarations,
    ``group "..." { }`` nesting (which sets the enclosing group for its children), and
    captures ``views { }`` / ``styles { }`` bodies as verbatim text. ``!include`` lines are
    skipped. The ``group`` context stack also tracks a parent *element* when elements nest
    directly (a ``container`` declared inside a ``softwareSystem { }``), so the child's
    ``group`` records the parent's display name.
    """
    lines = text.splitlines()
    elements: list[dict[str, str]] = []
    groups: set[str] = set()
    views_text = ""
    styles_text = ""

    # Context stack of enclosing labels: each frame is the group/parent name in force.
    ctx: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            i += 1
            continue
        if _INCLUDE_RE.match(stripped):  # !include and friends (§4.5.2): skip
            i += 1
            continue

        # views { } / styles { }: capture verbatim, do not descend into them as elements.
        low = stripped.lower()
        if low == "views {" or low.startswith("views {") or low == "views{":
            views_text, i = _capture_block(lines, i)
            continue
        if low == "styles {" or low.startswith("styles {") or low == "styles{":
            styles_text, i = _capture_block(lines, i)
            continue

        gm = _GROUP_RE.match(line)
        if gm:
            label = _strings(gm.group("q"))[0] if _strings(gm.group("q")) else ""
            if label:
                groups.add(label)
            ctx.append(label)
            i += 1
            continue

        # A bare closing brace pops the innermost context frame.
        if stripped == "}":
            if ctx:
                ctx.pop()
            i += 1
            continue

        m = _ASSIGN_RE.match(line)
        if m and m.group("kw") in _ELEMENT_KEYWORDS:
            kw = m.group("kw")
            rest = m.group("rest")
            opens_block = rest.rstrip().endswith("{")
            strs = _strings(rest)
            name = strs[0] if strs else (m.group("ident") or "")
            desc = strs[1] if len(strs) > 1 else ""
            tech = strs[2] if len(strs) > 2 else ""
            ident = m.group("ident") or _slug(name)
            group = next((g for g in reversed(ctx) if g), "")
            elements.append({
                "key": ident,
                "name": name,
                "description": desc,
                "technology": tech,
                "group": group,
            })
            if opens_block:
                # This element opens a block; its display name becomes the enclosing
                # parent for nested children (a container inside a softwareSystem, etc.).
                ctx.append(name or ident)
            i += 1
            continue

        # Any other line that opens an unrecognized block: track its brace so the context
        # stack stays balanced (e.g. `properties {`), without recording an element.
        if stripped.endswith("{"):
            ctx.append("")
        i += 1

    return _finish(elements, sorted(groups), views_text, styles_text)


def _walk_json_elements(node: Any, group: str, out: list[dict[str, str]],
                        groups: set[str]) -> None:
    """Recurse a Structurizr workspace.json model element and its children (§4.5.2)."""
    if not isinstance(node, dict):
        return
    name = str(node.get("name", "") or "")
    key = str(node.get("id") or node.get("name") or _slug(name))
    own_group = str(node.get("group", "") or "")
    eff_group = own_group or group
    if own_group:
        groups.add(own_group)
    out.append({
        "key": key,
        "name": name,
        "description": str(node.get("description", "") or ""),
        "technology": str(node.get("technology", "") or ""),
        "group": eff_group,
    })
    # children carry this element's name as their parent label when ungrouped.
    child_group = own_group or name or group
    for child_key in ("containers", "components"):
        for child in node.get(child_key, []) or []:
            _walk_json_elements(child, child_group, out, groups)


def _parse_workspace_json(text: str) -> ParsedModel:
    """Walk a full Structurizr ``workspace.json`` model for names/descriptions/groups.

    Captures ``model.people[]``, ``model.softwareSystems[].containers[].components[]``.
    ``views`` / ``styles`` (configuration.styles) are JSON, not DSL text, so they are
    serialized back to a stable JSON string for the caller's records — they are *not*
    DSL-importable verbatim the way a ``.dsl`` block is, but the seed still surfaces them.
    """
    data = json.loads(text)
    model = data.get("model", {}) if isinstance(data, dict) else {}
    out: list[dict[str, str]] = []
    groups: set[str] = set()

    for person in model.get("people", []) or []:
        if isinstance(person, dict):
            name = str(person.get("name", "") or "")
            out.append({
                "key": str(person.get("id") or person.get("name") or _slug(name)),
                "name": name,
                "description": str(person.get("description", "") or ""),
                "technology": "",
                "group": str(person.get("group", "") or ""),
            })
            if person.get("group"):
                groups.add(str(person["group"]))

    for system in model.get("softwareSystems", []) or []:
        _walk_json_elements(system, "", out, groups)

    views_obj = data.get("views") if isinstance(data, dict) else None
    styles_obj = None
    if isinstance(data, dict):
        styles_obj = (data.get("configuration", {}) or {}).get("styles")
        if styles_obj is None and isinstance(views_obj, dict):
            styles_obj = views_obj.get("configuration", {}).get("styles")
    views_text = _stable_json(views_obj) if views_obj else ""
    styles_text = _stable_json(styles_obj) if styles_obj else ""

    return _finish(out, sorted(groups), views_text, styles_text)


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2)


def _finish(elements: list[dict[str, str]], groups: list[str],
            views_text: str, styles_text: str) -> ParsedModel:
    """De-duplicate + deterministically sort elements; assemble the ParsedModel."""
    # De-dup on (key, name, group) keeping first-seen, then sort for determinism.
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, str]] = []
    for e in elements:
        sig = (e["key"], e["name"], e["group"])
        if sig in seen:
            continue
        seen.add(sig)
        deduped.append(e)
    deduped.sort(key=lambda e: (e["group"], e["key"], e["name"]))
    return ParsedModel(
        elements=deduped,
        groups=sorted(set(groups)),
        views_text=views_text,
        styles_text=styles_text,
    )


def parse_model(path: Path) -> ParsedModel:
    """Parse a hand-crafted Structurizr model (``.dsl`` or ``workspace.json``) (§4.5.2).

    Dispatch is by suffix: ``.json`` → full-model JSON walk; everything else (``.dsl``,
    ``.c4``) → the pragmatic DSL line/brace parser. Reads with ``utf-8-sig`` (BOM-tolerant).
    """
    path = Path(path)
    text = _read(path)
    if path.suffix.lower() == ".json":
        return _parse_workspace_json(text)
    return _parse_dsl(text)


# --------------------------------------------------------------------- seed rule build

def build_seed_rules(parsed: ParsedModel, source: str | None = None) -> dict[str, Any]:
    """Lift a :class:`ParsedModel` into a ``mapping-rules.yaml``-shaped fragment (§7.4).

    Produces exactly the blocks the §7.4 ``seed:`` example pre-populates:

    - ``name_overrides``: ``{<element key/name> : <display name>}`` for every element that
      has a display name. **Keyed by the element's own hand-crafted key, NOT a real
      extracted stable id** (§4.5.3): the seed has not seen the build graph, so these keys
      are placeholders the §7.5 reconciliation rebinds onto extracted ids via
      ``human_id_bindings.yaml``. They are emitted now so the architect can review them and
      so the reconciler has something to bind.
    - ``group``: one ``{container, members: []}`` row per group/nesting label. ``members``
      is intentionally **empty** — it is filled *after* reconciliation maps human element
      ids to extracted target ids (§7.5); a seed cannot list real stable-id members.
    - ``seed``: the §7.4 provenance block ``{from, imported_views, id_reconciliation}`` with
      ``id_reconciliation: "pending"`` (no human-confirmed bindings exist yet, §7.5).
    - ``descriptions``: ``{<element key/name> : <text>}`` for elements that carry one.
      These seed the §8 enrichment input — the LLM *refines* them, never silently overrides
      (a seeded description loses to nothing; a seeded ``name_overrides`` pin is hard, §8.3).

    *source* is the human-readable path string recorded in ``seed.from`` (defaults to "").
    Determinism: ``yaml.safe_dump(sort_keys=True)`` in :func:`to_yaml` sorts mapping keys;
    ``group`` rows are sorted here by container name.
    """
    name_overrides: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    for e in parsed.elements:
        key = e.get("key") or e.get("name")
        if not key:
            continue
        if e.get("name"):
            # last writer wins is fine: dedupe in _finish already collapsed identical sigs,
            # and sorting upstream makes the surviving value order-independent.
            name_overrides[key] = e["name"]
        if e.get("description"):
            descriptions[key] = e["description"]

    # group rows: one per distinct group/nesting label. members empty until reconciliation.
    group_names = sorted(set(parsed.groups))
    groups = [{"container": g, "members": []} for g in group_names]

    seed_block = {
        "from": source or "",
        "imported_views": bool(parsed.views_text or parsed.styles_text),
        "id_reconciliation": _ID_RECONCILIATION_PENDING,
    }

    rules: dict[str, Any] = {
        "seed": seed_block,
        "name_overrides": name_overrides,
        "group": groups,
        "descriptions": descriptions,
    }
    return rules


def to_yaml(seed_rules: dict[str, Any]) -> str:
    """Deterministic YAML for a seed-rules fragment (§7.4).

    ``sort_keys=True`` + ``default_flow_style=False`` give byte-stable block YAML; the
    output is normalized to LF (no CRLF) regardless of platform, matching the repo's
    LF-everywhere determinism gate (§1.1 metric 2). ``allow_unicode=True`` keeps unicode
    names readable and stable rather than ``\\uXXXX``-escaped.
    """
    text = yaml.safe_dump(
        seed_rules,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
    )
    # yaml.safe_dump uses \n already, but normalize defensively for cross-platform bytes.
    return text.replace("\r\n", "\n").replace("\r", "\n")
