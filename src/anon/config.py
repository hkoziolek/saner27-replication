"""Loading of curation rules with documented defaults (plan §7.4).

``mapping-rules.yaml`` is the reviewed, committed source of truth for curation. This
module loads it (or supplies defaults when absent — the §18.3 zero-config first run)
and exposes the ``defaults`` block as typed accessors so stages don't re-parse YAML.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# LLM operating modes (plan §8.5). MVP-0/1 default is no-llm.
LLM_MODES = ("no-llm", "llm-propose", "llm-accepted")

# §7.4 defaults block — the documented default values.
DEFAULTS = {
    "min_relationship_weight": 3,   # non-declared edges only; declared deps never dropped
    "drop_external": True,
    "down_weight_private": True,
    "on_unmapped": "annotate",      # annotate | fail  (§7.3 gate strictness)
    "on_llm_failure": "degrade",    # degrade | fail   (§8.3 terminal policy)
    "use_llm_names": True,          # superseded by --no-llm / enrich.mode (§8.5)
}


@dataclass
class MappingRules:
    """Parsed ``mapping-rules.yaml`` (plan §7.4)."""

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def defaults(self) -> dict[str, Any]:
        merged = dict(DEFAULTS)
        merged.update(self.raw.get("defaults", {}) or {})
        return merged

    @property
    def exclude_globs(self) -> list[str]:
        ex = (self.raw.get("exclude", {}) or {}).get("targets", []) or []
        return [e["glob"] for e in ex if isinstance(e, dict) and "glob" in e]

    @property
    def exclude_ids(self) -> set[str]:
        ex = (self.raw.get("exclude", {}) or {}).get("targets", []) or []
        return {e["id"] for e in ex if isinstance(e, dict) and "id" in e}

    @property
    def groups(self) -> list[dict[str, Any]]:
        return self.raw.get("group", []) or []

    @property
    def parents(self) -> dict[str, str]:
        """Optional cosmetic ``parent:`` label per ``group:`` rule (container-groups plan §3):
        container display name -> parent label. A parent is a visual boundary only — it is
        never an element (no id, no slug, no relationships); it renders as a Structurizr
        ``group{}`` and a canvas boundary box. Non-string / blank values are ignored
        (curation must degrade, never crash)."""
        out: dict[str, str] = {}
        for g in self.groups:
            cname = g.get("container")
            parent = g.get("parent")
            if cname and isinstance(parent, str) and parent.strip():
                out[str(cname)] = parent.strip()
        return out

    @property
    def id_aliases(self) -> dict[str, str]:
        return self.raw.get("id_aliases", {}) or {}

    @property
    def name_overrides(self) -> dict[str, str]:
        return self.raw.get("name_overrides", {}) or {}

    @property
    def description_overrides(self) -> dict[str, str]:
        """Reviewed/pinned element descriptions (§8.3/§8.5), keyed by element id.

        The description twin of ``name_overrides``: written by the human (or the GUI
        review queue's ``accept`` splice, `enrich_llm.accept_proposals`) after reviewing
        an ``llm-propose`` run; served without a model call in ``llm-accepted`` mode."""
        return self.raw.get("description_overrides", {}) or {}

    @property
    def descriptions(self) -> dict[str, str]:
        """Hand-authored container descriptions (§7.4), keyed by container display name.

        Wired into curate (stamped as ``container_description`` on each member target) and
        rendered by the §9 generator as the container's C4 description. Absent the block,
        the generator synthesizes a deterministic fallback so no container is ever
        description-less (clears the Structurizr ``model.container.description`` inspection)."""
        return self.raw.get("descriptions", {}) or {}

    @property
    def technology(self) -> dict[str, str]:
        return self.raw.get("technology", {}) or {}

    @property
    def naming(self) -> dict[str, Any]:
        # evidence_strength_k (§16#10b): the K evidence-count threshold for the §8.3
        # deterministic confidence cross-check (enrich_llm.evidence_strength). Below K
        # incident-evidence items (or no doc text / fresh needs-curation) flags the element
        # for review even if the LLM self-reported high confidence.
        n = {"style": "descriptive", "expand_acronyms": False, "max_name_words": 3,
             "glossary": {}, "evidence_strength_k": 2}
        n.update(self.raw.get("naming", {}) or {})
        return n

    @property
    def model_shape(self) -> str:
        """``model_shape: auto | x | y`` (drill-down plan D§2.3/D§6.1), top-level key.

        ``x``/``y`` PIN the model shape (the anti-flapping valve for repos hovering at the
        ``MODEL_Y_MAX`` threshold); the default ``auto`` lets the generator choose from the
        curated target count. An unrecognized value degrades to ``auto`` (never fails)."""
        v = str(self.raw.get("model_shape") or "auto").strip().lower()
        return v if v in ("auto", "x", "y") else "auto"

    def expand_rules(self) -> list[dict[str, Any]]:
        """Normalized D§5/D§6.1 ``expand:`` entries: ``[{"pattern": str, "classes": bool}]``.

        Accepted YAML forms, top-level ``expand:`` and per-``group`` ``expand:`` alike:
          - ``"<target-id-or-glob>"`` (plain string)
          - ``{target: "<target-id-or-glob>", classes: true}`` (dict row; ``classes``
            surfaces the promoted target's ``code[]`` class tier instead of namespaces)
          - ``{target: ..., namespace: "<ns-substring>"}`` (class tier scoped to the
            matching namespace(s) — the faithful successor of the old ``code_view``)
        A per-group row carries ``"container": <group's container name>`` so curate scopes
        it to that group's members; top-level rows have ``"container": None`` (global).
        Order is preserved (top-level first, then per-group in group order) and malformed
        rows are skipped — curation must degrade, never crash (§7.4)."""
        out: list[dict[str, Any]] = []

        def _norm(entry: Any, container: str | None) -> dict[str, Any] | None:
            if isinstance(entry, str) and entry.strip():
                return {"pattern": entry.strip(), "classes": False, "namespace": None,
                        "container": container}
            if isinstance(entry, dict) and entry.get("target"):
                ns = entry.get("namespace")
                return {"pattern": str(entry["target"]), "classes": bool(entry.get("classes")),
                        "namespace": str(ns) if ns else None, "container": container}
            return None

        for entry in (self.raw.get("expand", []) or []):
            row = _norm(entry, None)
            if row:
                out.append(row)
        for g in self.groups:
            if not isinstance(g, dict):
                continue
            for entry in (g.get("expand", []) or []):
                row = _norm(entry, g.get("container"))
                if row:
                    out.append(row)
        return out

    def component_view_containers(self) -> set[str]:
        """Container names flagged ``component_view: true`` (§7.4 / §9).

        DEPRECATED (drill-down plan D§6.2): the target tier is drillable by default now;
        curate reinterprets this flag as ``expand: [<all members>]`` and notes the
        deprecation in the curated provenance."""
        return {g["container"] for g in self.groups
                if isinstance(g, dict) and g.get("component_view")}

    def code_view_containers(self) -> dict[str, str]:
        """Container name -> namespace-substring filter for a class/code drill-down (§9.1).

        A group with ``code_view: "<namespace-substring>"`` renders the CLASSES of its
        matching component(s) as elements (the C4 code altitude) instead of the namespace
        tier. Implies a Component view. Bounded by the §1.1#1 budget like any Component view.
        """
        return {g["container"]: str(g["code_view"]) for g in self.groups
                if isinstance(g, dict) and g.get("code_view") and g.get("container")}


def load_mapping_rules(path: str | Path) -> MappingRules:
    p = Path(path)
    if not p.exists():
        return MappingRules(raw={})
    with open(p, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return MappingRules(raw=data)


def load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}
