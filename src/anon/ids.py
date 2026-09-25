"""Stable identifiers and the DSL-identifier slug function.

Element identity is the canonical, content-independent stable id (plan §6.3),
e.g. ``csharp:csproj:src/Foo/Foo.csproj`` or ``cpp:target:OrderProcessor``. The
*display name* never participates in identity.

Structurizr DSL needs a syntactic identifier per element. :func:`dsl_identifier`
derives one deterministically from the stable id (plan §9), and :class:`SlugRegistry`
guarantees global uniqueness in an edit-order-independent way so that
``same id -> same identifier`` holds across runs (the precondition for layout-merge,
§9.1, and for byte-identical regeneration, §1.1 #2).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


# Multi-repo repo-namespace separator (plan §21.2/§6.3). The single-repo id is the
# degenerate case with NO prefix, so single-repo runs never see this.
REPO_SEP = "::"


def has_repo(element_id: str) -> bool:
    """True if *element_id* carries a ``<repo>::`` namespace prefix (§21.2)."""
    return REPO_SEP in element_id


def repo_of(element_id: str) -> str | None:
    """The repo segment of a namespaced id, or ``None`` for a bare single-repo id."""
    return element_id.split(REPO_SEP, 1)[0] if REPO_SEP in element_id else None


def strip_repo(element_id: str) -> str:
    """The bare (single-repo) id with any ``<repo>::`` prefix removed (§21.2)."""
    return element_id.split(REPO_SEP, 1)[1] if REPO_SEP in element_id else element_id


def with_repo(repo: str, element_id: str) -> str:
    """Stamp the additive ``<repo>::`` namespace onto a bare id (idempotent, §21.2).

    A no-op if *element_id* is already namespaced (so combining an already-combined model
    is safe). ``rel:``-prefixed relationship ids are namespaced on their endpoints by the
    combine step, not here — this stamps a single element id."""
    if not repo or REPO_SEP in element_id:
        return element_id
    return f"{repo}{REPO_SEP}{element_id}"


def split_id(element_id: str) -> tuple[str, str, str]:
    """Split a stable id into ``(namespace, kind, key)``.

    ``csharp:csproj:src/Foo/Foo.csproj`` -> ``("csharp", "csproj", "src/Foo/Foo.csproj")``
    Child ids (``.../component:x``) keep the trailing segment in *key*.
    """
    parts = element_id.split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return element_id, "", ""


_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")


def _camel(key: str) -> str:
    """Turn an arbitrary key into a lowerCamelCase alnum token, dropping path/dots.

    Path-like keys are reduced to their basename for readability (the C# stable key is
    a path, but ``src/Web/Toy.Web.csproj`` should slug to ``toyWeb``, mirroring the
    plan's ``cpp:target:OrderProcessor`` -> ``orderProcessor``, §9). Uniqueness across
    same-basename keys is the registry's job, not this function's.
    """
    key = key.replace("\\", "/").rsplit("/", 1)[-1]  # basename
    # Drop a trailing file extension on csproj-style keys for readability.
    key = re.sub(r"\.csproj$", "", key, flags=re.IGNORECASE)
    tokens = [t for t in _NON_ALNUM.split(key) if t]
    if not tokens:
        return ""
    head, *tail = tokens
    out = head[:1].lower() + head[1:]
    for t in tail:
        out += t[:1].upper() + t[1:]
    return out


def dsl_identifier(element_id: str) -> str:
    """Deterministic, *base* DSL identifier for an element id (before uniquing).

    Always starts with a letter and contains only ``[A-Za-z0-9_]`` (the Structurizr
    identifier grammar). Uniqueness is the registry's job, not this function's.

    Uses the last non-empty colon-segment as the key, so 3-part build ids
    (``csharp:csproj:src/Web/Toy.Web.csproj`` -> ``toyWeb``) and 2-part container ids
    (``container:webFrontend`` -> ``webFrontend``) both slug cleanly.

    Repo-namespaced ids (``ControlApp::csharp:csproj:src/Web/Web.csproj``, §21.2) prepend
    the repo segment so cross-repo elements stay readable and distinct
    (-> ``controlAppToyWeb``); single-repo ids are unchanged.
    """
    repo = repo_of(element_id)
    bare = strip_repo(element_id)
    segments = [s for s in bare.split(":") if s]
    key = segments[-1] if segments else bare
    slug = _camel(key) or _camel(bare)
    if not slug:
        slug = "e"
    if repo:
        rslug = _camel(repo)
        if rslug:
            slug = rslug + slug[:1].upper() + slug[1:]
    if not slug[0].isalpha():
        slug = "e" + slug
    return slug


def _short_hash(element_id: str) -> str:
    return hashlib.sha1(element_id.encode("utf-8")).hexdigest()[:6]


@dataclass
class SlugRegistry:
    """Assigns globally-unique DSL identifiers deterministically.

    Collisions are resolved by appending ``_<6-hex of sha1(id)>`` — derived from the
    *stable id*, so the assignment does not depend on the order elements are
    registered (two runs that process elements in different orders still agree).

    Uniqueness is enforced **case-insensitively** because the Structurizr DSL parser
    treats identifiers case-insensitively: ``basketAPI`` (a container) and ``basketApi``
    (a deployment node) are distinct strings but the SAME identifier to Structurizr, so
    declaring both raises "identifier already in use". We therefore key ``_used`` on the
    lowercased identifier while preserving the original-case identifier as the value.
    """

    _by_id: dict[str, str] = field(default_factory=dict)
    _used: set[str] = field(default_factory=set)  # holds LOWERCASED identifiers (§ Structurizr is case-insensitive)

    def assign(self, element_id: str) -> str:
        if element_id in self._by_id:
            return self._by_id[element_id]
        base = dsl_identifier(element_id)
        candidate = base
        if candidate.lower() in self._used:
            candidate = f"{base}_{_short_hash(element_id)}"
            # Extremely defensive: hash collision -> widen the suffix deterministically.
            n = 8
            while candidate.lower() in self._used and n <= 40:
                candidate = f"{base}_{hashlib.sha1(element_id.encode()).hexdigest()[:n]}"
                n += 2
        self._by_id[element_id] = candidate
        self._used.add(candidate.lower())
        return candidate

    def get(self, element_id: str) -> str | None:
        return self._by_id.get(element_id)


def rel_id(source: str, target: str) -> str:
    """Relationship id ``rel:<source>-><target>`` (plan §6.4a). ``kind`` is NOT baked in."""
    return f"rel:{source}->{target}"
