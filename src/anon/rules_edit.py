"""The scoped textual line-editor for the Curation Tree Editor (curation-tree-editor §8.1).

The structured ops the tree compiles every drag/edit gesture to — ``add_member``,
``move_member``, ``remove_member``, ``exclude``, ``generalize_to_glob``, ``set_glob``,
``add_glob`` (the member/glob ops), ``create_group`` (append a new container), and
``reorder_group`` (the §4.5 one-click conflict reorder) — each modify, delete, move, or
append *inside* the ``group:`` block of the hand-owned ``mapping-rules.yaml``. The
existing :func:`propose.accept` splice is **append-only** (it renders new rows and
appends them to the end of the ``group:`` block, refusing when the container already
exists), so it cannot serve the in-place ops. This module is the in-place counterpart,
built on the same discipline (G§Q1): a **scoped textual line-editor, not ruamel** —
locate the named block, find its ``members:``/``members_glob:``/``members_tag:`` sub-list
(or the top-level ``exclude:`` block, locate-or-create), insert/remove/replace
**specific item lines**, and preserve every untouched byte. Ruamel is rejected because it
reflows quoting/indentation on untouched regions, spraying spurious diff noise across
every committable diff and breaking byte-preservation.

Two hard guarantees match the plan's sharp-edge decisions:

- **Fail-closed (G§Q2).** When safe line-surgery is impossible — a member line being
  deleted carries a trailing inline comment, the list is flow-style ``[a, b]``, or the
  block is ambiguous/duplicated — the op **raises** :class:`RulesEditError` with a precise,
  located message rather than guess or eat a comment. The one flow form that IS safe is
  the **empty** collection ``key: []`` / ``key: {}`` (what ``yaml.safe_dump`` writes for an
  empty list, so what every fresh seed carries — the RQ9 curator seeds' ``targets: []`` /
  ``group: []``): nothing inside can be mis-split, so the first insert opens it into block
  form in place (:func:`_open_empty_flow`) and keeps any inline comment.
- **Atomic, validated-before-return (G§Q8).** Every op builds ONE in-memory ``new_text``
  and re-parses it before returning; a multi-edit op (``move`` = delete + add) builds the
  whole ``new_text`` from a single edit pass, so a sub-edit that fails-closed aborts the
  whole op and nothing changes.

The verdict of *where a target lands* is never decided here — that is the real
:func:`apply_mapping_rules.curate` stage's job (the iron rule, GUI §2.1). This module only
authors the candidate YAML text; the endpoint runs ``curate()`` over it to confirm intent.
"""
from __future__ import annotations

import json
import re
from typing import Any

import yaml

from .stages.apply_mapping_rules import glob_matches_target, tag_matches_target

# The top-level exclusion keys an `exclude` op can target (locate-or-create, §8.1).
_EXCLUDE_KEYS = ("exclude_ids", "exclude_globs")


class RulesEditError(Exception):
    """A fail-closed refusal from the scoped line-editor (G§Q2).

    Carries a precise, deep-linked message ("can't safely edit *Catalog* here — member
    ``Foo.Legacy.csproj`` has an inline comment; edit in the Rules tab") so the GUI can
    bounce the user to the YAML tab rather than guess or destroy a comment. ``line`` is
    the 1-based source line the refusal points at (``None`` for whole-file/structural
    refusals)."""

    def __init__(self, message: str, *, line: int | None = None):
        super().__init__(message)
        self.message = message
        self.line = line


# --------------------------------------------------------------------------- scanning

def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_inline_comment_is_safe(value_part: str) -> bool:
    """True if *value_part* (everything after the ``- ``/``key:`` token) has NO trailing
    inline comment we'd silently destroy. A ``#`` inside a quoted string is fine; a bare
    ``#`` after the value is the unsafe case (G§Q2)."""
    in_single = in_double = False
    for ch in value_part:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return False
    return True


def _scalar_value(value_part: str) -> str:
    """The YAML scalar of an item/value token, comment-and-quote stripped. Used only to
    compare against an id we're locating; never written back."""
    try:
        return str(yaml.safe_load(value_part))
    except yaml.YAMLError:
        return value_part.strip()


def _split_value_comment(val: str) -> tuple[str, str]:
    """Split a value token into ``(value, comment_suffix)`` at the first UNQUOTED ``#`` — the
    leading whitespace before the ``#`` rides with the comment so it is reproduced verbatim.
    A ``#`` inside a quoted scalar is part of the value. Used by rename, which rewrites only
    the value and keeps the comment (a value-swap can preserve it; a line-delete cannot — so
    deletes still fail-closed via :func:`_strip_inline_comment_is_safe`, G§Q2)."""
    in_single = in_double = False
    for i, ch in enumerate(val):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            j = i
            while j > 0 and val[j - 1] in " \t":
                j -= 1
            return val[:j], val[j:]
    return val, ""


_EMPTY_FLOW_RE = re.compile(
    r"^(?P<key>\s*[^\s#\-][^:#]*:)\s*(?:\[\s*\]|\{\s*\})(?P<tail>\s*(?:#.*)?)$")


def _open_empty_flow(line: str) -> str | None:
    """``key: []`` / ``key: {}`` (optional inline comment) → the block-form key line
    ``key:`` with the comment kept; ``None`` for any other line.

    Only the caller that is about to insert the first item under the key may use the
    result: a bare ``key:`` with nothing below parses as null, not as an empty list, so
    the line is never opened on its own. Non-empty flow collections stay fail-closed
    (G§Q2) — item-line surgery can't touch ``[a, b]``."""
    m = _EMPTY_FLOW_RE.match(line)
    if not m:
        return None
    tail = m.group("tail").strip()
    return m.group("key") + (f"  {tail}" if tail else "")


def _open_empty_flow_at(lines: list[str], at: int) -> list[str]:
    """Open an empty ``key: []`` / ``key: {}`` at index *at* in place — called only after
    the first item has been inserted under it. A no-op for a block key."""
    opened = _open_empty_flow(lines[at])
    if opened is not None:
        lines[at] = opened
    return lines


def _find_groups(lines: list[str]) -> int | None:
    """Index of the top-level ``group:`` key line, or ``None``. Ambiguity (two top-level
    ``group:`` keys) is fail-closed — curation reads only the first, so a second would be
    a silent surprise."""
    found = None
    for i, ln in enumerate(lines):
        if _indent_of(ln) != 0:
            continue
        head = ln.split("#", 1)[0].rstrip()
        if head != "group:" and not head.startswith("group:"):
            continue
        if head != "group:" and _open_empty_flow(ln) is None:
            # `group: [{...}]` with content — item surgery can't touch it (G§Q2). An
            # EMPTY `group: []` IS the block key (the seed form); _append_group opens it.
            raise RulesEditError(
                "mapping-rules.yaml `group:` is a flow-style list ([..]) — can't safely "
                "line-edit; edit in the Rules tab", line=i + 1)
        if found is not None:
            raise RulesEditError(
                "mapping-rules.yaml has two top-level `group:` keys — ambiguous; "
                "edit in the Rules tab", line=i + 1)
        found = i
    return found


def _group_entries(lines: list[str], group_at: int) -> list[tuple[int, int]]:
    """The ``[start, end)`` line spans of each ``- container:`` entry under ``group:``.

    An entry starts at a ``-``-bearing line under ``group:`` and runs until the next
    sibling ``-`` at the same indent or the block dedents to a new top-level key. The
    dash may sit at indent 0 — a block sequence at the SAME indent as its key is valid
    YAML and is exactly what ``yaml.safe_dump`` (so the ``arch propose`` draft a user
    copies into the hand file) emits — so a zero-indent line ends the block only when
    it is a new ``key:`` line, not a ``- `` entry. (The earlier "any zero-indent line
    ends the block" rule made every copied-proposal group invisible to the editor: the
    "no group named X while X is right there in the tree" bug.)
    """
    body_start = group_at + 1
    # the entry indent is the indent of the first dash-line in the block (0 is valid —
    # the same-indent sequence form safe_dump writes).
    entry_indent = None
    for i in range(body_start, len(lines)):
        ln = lines[i]
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if ln.lstrip().startswith("-"):
            entry_indent = _indent_of(ln)
            break
        if _indent_of(ln) == 0:
            break  # dedented to a new top-level key — group block is empty
    if entry_indent is None:
        return []
    spans: list[tuple[int, int]] = []
    start = None
    block_end = len(lines)
    for i in range(body_start, len(lines)):
        ln = lines[i]
        stripped = ln.strip()
        if (stripped and _indent_of(ln) == 0
                and not (entry_indent == 0
                         and (ln.startswith("-") or stripped.startswith("#")))):
            block_end = i  # next top-level key ends the group block
            break
        if _indent_of(ln) == entry_indent and ln.lstrip().startswith("-"):
            if start is not None:
                spans.append((start, i))
            start = i
    if start is not None:
        # the final entry runs to the block end; trailing blank/comment lines belong to
        # the next section, so back up over the blanks.
        end = block_end
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        spans.append((start, end))
    return spans


def _entry_body_end(lines: list[str], start: int, entry_indent: int,
                    span_end: int) -> int:
    """Index one past the last line that BELONGS to the ``group:`` entry beginning at
    *start* — its ``- container:`` dash line plus every line indented deeper than the entry
    (the mapping body, incl. interior blanks). Trailing blank lines and any same-or-shallower
    comment lines between this entry and the next are NOT included: they are separators that
    stay put when the entry is reordered (the comment-safe move, §4.5)."""
    body_end = start + 1
    for i in range(start + 1, span_end):
        ln = lines[i]
        if not ln.strip():
            continue  # blank: keep scanning, but do not extend the body over it
        if _indent_of(ln) > entry_indent:
            body_end = i + 1
        else:
            break  # a separator comment or the next sibling — the body has ended
    return body_end


def _entry_container_name(lines: list[str], span: tuple[int, int]) -> str | None:
    """The ``container:`` display name of a ``group:`` entry span, parsed via yaml so
    quoting/flow forms resolve identically to curate's view."""
    start, end = span
    block = "\n".join(lines[start:end])
    # de-indent the `- ` so the entry parses as a standalone mapping.
    try:
        loaded = yaml.safe_load(block)
    except yaml.YAMLError:
        return None
    if isinstance(loaded, list) and loaded and isinstance(loaded[0], dict):
        return loaded[0].get("container")
    return None


def _locate_group(lines: list[str], container: str) -> tuple[int, int]:
    """The ``[start, end)`` span of the single ``group:`` entry whose ``container`` is
    *container*. Fail-closed when the container is missing or duplicated (G§Q2)."""
    group_at = _find_groups(lines)
    if group_at is None:
        raise RulesEditError("mapping-rules.yaml has no `group:` block to edit")
    matches = [span for span in _group_entries(lines, group_at)
               if _entry_container_name(lines, span) == container]
    if not matches:
        raise RulesEditError(f"no group named {container!r} in mapping-rules.yaml")
    if len(matches) > 1:
        raise RulesEditError(
            f"group {container!r} is defined more than once — ambiguous; edit in the "
            f"Rules tab", line=matches[1][0] + 1)
    return matches[0]


def _find_sublist(lines: list[str], span: tuple[int, int],
                  key: str) -> tuple[int, int, int] | None:
    """Locate a ``members:``/``members_glob:``/``members_tag:`` (or ``exclude_ids:`` …)
    sub-list inside the entry *span*.

    Returns ``(key_line, items_start, items_end)`` — the line carrying ``<key>:``, and the
    half-open range of its ``- item`` lines — or ``None`` if the key is absent. Fail-closed
    on a flow-style list (``members: [a, b]``, G§Q2) since item-line surgery can't touch it.
    """
    start, end = span
    key_re = re.compile(r"^(\s*)" + re.escape(key) + r"\s*:(.*)$")
    for i in range(start, end):
        m = key_re.match(lines[i])
        if not m:
            continue
        rest = m.group(2).strip()
        # split off any inline comment on the key line before judging flow-vs-block.
        rest_no_comment = rest
        if rest and not (rest.startswith("[") or rest.startswith("{")):
            # a plain scalar after the key (shouldn't happen for a list key) — skip comment
            rest_no_comment = rest.split("#", 1)[0].strip()
        if rest_no_comment.startswith("["):
            if _open_empty_flow(lines[i]) is not None:
                # `members: []` — an empty list with nothing to mis-split; report it as
                # an empty block (the inserting caller opens the key line in place).
                return i, i + 1, i + 1
            raise RulesEditError(
                f"`{key}` for this group is a flow-style list ([a, b]) — can't safely "
                f"line-edit; edit in the Rules tab", line=i + 1)
        key_indent = _indent_of(lines[i])
        items_start = i + 1
        # A block sequence may sit at the SAME indent as its key — `members:` then `- x` both
        # at 4 spaces — or be indented deeper; BOTH are valid YAML, and the same-indent form
        # is exactly what `yaml.safe_dump` (so `accept-proposal`) emits. Take the item indent
        # from the first `- ` line (at indent >= key_indent) and collect the run at that
        # indent; a non-dash line (a sibling key / dedent) or a different indent ends the list.
        # (The earlier `> key_indent` rule silently missed same-indent items, so the editor
        # couldn't see members another write path had written — the OrchardCore remove bug.)
        item_indent: int | None = None
        j = items_start
        while j < end:
            ln = lines[j]
            if not ln.strip():
                j += 1
                continue
            ind = _indent_of(ln)
            if ln.lstrip().startswith("-") and ind >= key_indent:
                if item_indent is None:
                    item_indent = ind
                if ind != item_indent:
                    break
                j += 1
                continue
            break
        # items_end backs up over trailing blank lines that belong after the list.
        items_end = j
        while items_end > items_start and not lines[items_end - 1].strip():
            items_end -= 1
        return i, items_start, items_end
    return None


def _item_value(line: str) -> tuple[str, bool]:
    """The scalar value of a ``- item`` line and whether it is comment-safe to delete.

    Returns ``(value, safe)``: *value* is the YAML-resolved scalar; *safe* is False when a
    trailing inline comment rides the line (deleting it would eat the comment, G§Q2)."""
    body = line.lstrip()[1:]  # drop the leading '-'
    safe = _strip_inline_comment_is_safe(body)
    return _scalar_value(body), safe


def _item_indent(lines: list[str], items_start: int, items_end: int,
                 key_line: int) -> str:
    """The leading whitespace to render new ``- item`` lines with — matched to the
    existing items, else two spaces deeper than the key line."""
    for i in range(items_start, items_end):
        if lines[i].lstrip().startswith("-"):
            return " " * _indent_of(lines[i])
    return " " * (_indent_of(lines[key_line]) + 2)


def _scalar(value: str) -> str:
    """Render *value* as a single YAML flow scalar — quoted iff YAML needs it (a colon, a
    glob ``*``, leading whitespace …), bare otherwise. Uses the ``[value]`` flow-list form
    so PyYAML never appends a ``\\n...`` document-end marker (which a plain ``yaml.dump``
    does for bare scalars). Deterministic and round-trip-safe."""
    return yaml.safe_dump([value], default_flow_style=True).strip()[1:-1].strip()


def _scalar_desc(value: str) -> str:
    """Render a free-text *description* as a SINGLE-LINE YAML scalar, safe at any nesting
    depth. :func:`_scalar`'s plain/single-quoted form is correct for single-line values, but
    PyYAML renders a value carrying a newline as a *block* single-quoted scalar whose
    continuation lines carry a fixed default indent — appended verbatim at the deeper
    ``step_body`` indent, those lines are dedented and can change the parsed value or break the
    apply_ops re-parse gate. Since YAML is a JSON superset, ``json.dumps`` yields a correct
    one-line double-quoted scalar (newlines/specials escaped) that round-trips to the original
    string regardless of where it is spliced. Keep the bare/single-quoted :func:`_scalar` form
    for newline-free descriptions (no quoting churn in the common case). Deterministic."""
    if "\n" in value:
        return json.dumps(value)
    return _scalar(value)


def _render_item(indent: str, value: str) -> str:
    """One ``- <value>`` line, value flow-scalar-rendered (quoted only where YAML needs
    it). Deterministic — stable for a given value."""
    return f"{indent}- {_scalar(value)}"


# --------------------------------------------------------------------------- primitive edits
# Each primitive takes the current line list and returns a NEW line list (pure). The op
# functions chain primitives over one in-memory copy so a multi-edit op is atomic (G§Q8).


def _insert_member(lines: list[str], container: str, key: str, value: str) -> list[str]:
    """Append *value* to *container*'s *key* sub-list, creating the sub-list (and, for an
    explicit ``members:`` on a glob-only group, the list itself) when absent. A value
    already present is a no-op (idempotent — dragging the same item twice is harmless)."""
    span = _locate_group(lines, container)
    found = _find_sublist(lines, span, key)
    if found is not None:
        key_line, items_start, items_end = found
        existing = [_item_value(lines[i])[0] for i in range(items_start, items_end)
                    if lines[i].lstrip().startswith("-")]
        if value in existing:
            return list(lines)
        indent = _item_indent(lines, items_start, items_end, key_line)
        new = list(lines)
        new.insert(items_end, _render_item(indent, value))
        return _open_empty_flow_at(new, key_line)  # `members: []` → `members:`
    # sub-list absent: create `<key>:` + one item at the end of the entry, indented one
    # level under the entry's `- container:` line.
    start, end = span
    entry_indent = _indent_of(lines[start])
    # the mapping keys inside the entry sit at entry_indent + 2 (past the "- ").
    key_indent = " " * (entry_indent + 2)
    item_indent = " " * (entry_indent + 4)
    new = list(lines)
    insert_at = end
    new.insert(insert_at, f"{key_indent}{key}:")
    new.insert(insert_at + 1, _render_item(item_indent, value))
    return new


def _drop_empty_sublist(lines: list[str], container: str, key: str) -> list[str]:
    """Remove the ``<key>:`` line of *container* when its sub-list has no items left (the
    generalize cleanup, §4.2). A no-op when the key is absent or still has items."""
    span = _locate_group(lines, container)
    found = _find_sublist(lines, span, key)
    if found is None:
        return list(lines)
    key_line, items_start, items_end = found
    if any(lines[i].lstrip().startswith("-")
           for i in range(items_start, items_end)):
        return list(lines)  # still has items
    new = list(lines)
    del new[key_line]
    return new


def _remove_member(lines: list[str], container: str, key: str, value: str,
                   *, required: bool) -> list[str]:
    """Remove the ``- <value>`` item line from *container*'s *key* sub-list.

    Fail-closed if the line being deleted carries an inline comment (G§Q2). When
    *required* is False a missing value/sub-list is a no-op (the move-from-glob case:
    the source was a glob bucket, so there is no explicit line to remove — §4.1)."""
    span = _locate_group(lines, container)
    found = _find_sublist(lines, span, key)
    if found is None:
        if required:
            raise RulesEditError(
                f"group {container!r} has no `{key}` list to remove {value!r} from")
        return list(lines)
    _key_line, items_start, items_end = found
    target_i = None
    for i in range(items_start, items_end):
        if not lines[i].lstrip().startswith("-"):
            continue
        val, safe = _item_value(lines[i])
        if val == value:
            if not safe:
                raise RulesEditError(
                    f"can't safely remove {value!r} from {container!r} — its line has an "
                    f"inline comment; edit in the Rules tab", line=i + 1)
            target_i = i
            break
    if target_i is None:
        if required:
            raise RulesEditError(
                f"group {container!r} has no member {value!r} to remove")
        return list(lines)
    new = list(lines)
    del new[target_i]
    return new


def _sweep_explicit_claims(lines: list[str], value: str,
                           dest_container: str) -> list[str]:
    """Remove *value* from every OTHER container's explicit ``members:`` list before it is
    inserted into *dest_container*'s (the exclusivity sweep).

    An id explicitly listed in two ``group:`` entries is never a meaningful state:
    curate's member index (``_group_index``) is a dict keyed by id, so the LAST entry
    silently wins — the YAML then carries a stale claim that contradicts the rendered
    tree, and ownership flips if the winning group is later removed or reordered. Every
    op that inserts an explicit member therefore first strips the id's other explicit
    claims; glob/tag claims are left untouched (they are legitimately overridden by an
    explicit member, not stale). Membership is detected by PARSING each entry — so a
    quoted, indented ``- 'x'`` and a zero-indent bare ``- x`` (the copied-proposal style)
    are the same claim — and removed via :func:`_remove_member`, which fail-closes (G§Q2)
    on an inline comment or a flow-style list rather than guess. Emptied ``members:``
    keys are dropped. Pure + deterministic (entries swept in file order)."""
    out = list(lines)
    while True:
        group_at = _find_groups(out)
        if group_at is None:
            return out
        holder = None
        for span in _group_entries(out, group_at):
            cname = _entry_container_name(out, span)
            if cname is None or cname == dest_container:
                continue
            start, end = span
            try:
                loaded = yaml.safe_load("\n".join(out[start:end]))
            except yaml.YAMLError:
                continue  # unparsable entry — gate 1's re-parse reports it, nothing to sweep
            entry = loaded[0] if isinstance(loaded, list) and loaded else None
            members = entry.get("members") if isinstance(entry, dict) else None
            if isinstance(members, list) and value in [str(m) for m in members]:
                holder = cname
                break
        if holder is None:
            return out
        out = _remove_member(out, holder, "members", value, required=True)
        out = _drop_empty_sublist(out, holder, "members")


def _replace_value(lines: list[str], container: str, key: str,
                   old: str, new_val: str) -> list[str]:
    """Rewrite ONE ``- <old>`` item line of *container*'s *key* sub-list to ``- <new_val>``
    in place (the ``set_glob`` primitive). Fail-closed on an inline comment, or when the
    sub-list has more than one item and *old* is not given precisely."""
    span = _locate_group(lines, container)
    found = _find_sublist(lines, span, key)
    if found is None:
        raise RulesEditError(f"group {container!r} has no `{key}` list to edit")
    _key_line, items_start, items_end = found
    indent_lines = [i for i in range(items_start, items_end)
                    if lines[i].lstrip().startswith("-")]
    target_i = None
    for i in indent_lines:
        val, safe = _item_value(lines[i])
        if val == old:
            if not safe:
                raise RulesEditError(
                    f"can't safely edit the {key} {old!r} in {container!r} — its line has "
                    f"an inline comment; edit in the Rules tab", line=i + 1)
            target_i = i
            break
    if target_i is None:
        raise RulesEditError(
            f"group {container!r} has no `{key}` entry {old!r} to rewrite")
    indent = " " * _indent_of(lines[target_i])
    new = list(lines)
    new[target_i] = _render_item(indent, new_val)
    return new


# --------------------------------------------------------------------------- exclude block

def _ensure_exclude_member(lines: list[str], key: str, value: str) -> list[str]:
    """Append *value* to the top-level ``exclude.targets`` list as a ``{<key.field>: …}``
    row, creating the ``exclude:`` / ``targets:`` block when absent (locate-or-create).

    The hand schema (config.MappingRules) reads ``exclude.targets`` as a list of
    ``{glob: …}`` / ``{id: …}`` rows; ``exclude_ids`` maps to ``- id: …`` and
    ``exclude_globs`` to ``- glob: …``. Fail-closed on a flow-style ``targets: [..]``."""
    field = "id" if key == "exclude_ids" else "glob"
    # find a top-level `exclude:` key (an empty `exclude: {}` counts and is opened below).
    exclude_at = None
    for i, ln in enumerate(lines):
        head = ln.split("#", 1)[0].rstrip()
        if _indent_of(ln) == 0 and (head == "exclude:" or
                                    (head.startswith("exclude:")
                                     and _open_empty_flow(ln) is not None)):
            if exclude_at is not None:
                raise RulesEditError(
                    "mapping-rules.yaml has two top-level `exclude:` keys — ambiguous; "
                    "edit in the Rules tab", line=i + 1)
            exclude_at = i
    if exclude_at is None:
        # create the whole block at end of file.
        new = list(lines)
        while new and not new[-1].strip():
            new.pop()
        new.append("")
        new.append("exclude:")
        new.append("  targets:")
        new.append(f'    - {field}: {_scalar(value)}')
        return new
    # find `targets:` under exclude (the next indented key).
    targets_at = None
    end = len(lines)
    for i in range(exclude_at + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and _indent_of(ln) == 0:
            end = i
            break
        m = re.match(r"^(\s+)targets\s*:(.*)$", ln)
        if m:
            rest = m.group(2).strip()
            if rest.startswith("[") and _open_empty_flow(ln) is None:
                raise RulesEditError(
                    "`exclude.targets` is a flow-style list — can't safely line-edit; "
                    "edit in the Rules tab", line=i + 1)
            targets_at = i  # a block list, or the seed's empty `targets: []` (opened below)
    if targets_at is None:
        # exclude: present but no targets list — append one under it (before the blank
        # lines that separate the block from the next top-level key).
        insert_at = end
        while insert_at > exclude_at + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        new = list(lines)
        new.insert(insert_at, "  targets:")
        new.insert(insert_at + 1,
                   f'    - {field}: {_scalar(value)}')
        return _open_empty_flow_at(new, exclude_at)
    # find the extent of the targets list + check for an existing identical row.
    targets_indent = _indent_of(lines[targets_at])
    items_start = targets_at + 1
    j = items_start
    item_indent = None
    while j < end:
        ln = lines[j]
        if not ln.strip():
            j += 1
            continue
        ind = _indent_of(ln)
        # same-indent OR deeper `- ` rows are the list (see _find_sublist); a non-dash line
        # or a differently-indented dash ends it.
        if not (ln.lstrip().startswith("-") and ind >= targets_indent):
            break
        if item_indent is None:
            item_indent = ind
        if ind != item_indent:
            break
        # already excluded? idempotent no-op.
        row = ln.lstrip()[1:].strip()
        try:
            parsed = yaml.safe_load(row)
        except yaml.YAMLError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get(field) == value:
            return list(lines)
        j += 1
    items_end = j
    while items_end > items_start and not lines[items_end - 1].strip():
        items_end -= 1
    indent = " " * (item_indent if item_indent is not None else targets_indent + 2)
    new = list(lines)
    new.insert(items_end,
               f'{indent}- {field}: {_scalar(value)}')
    return _open_empty_flow_at(new, targets_at)


# --------------------------------------------------------------------------- create group

def _render_group_entry(container: str, members: list[str], globs: list[str],
                        tags: list[str], dash_indent: str) -> list[str]:
    """Render one ``- container: …`` group entry (+ its sub-lists) as lines, indented to
    *dash_indent*. Deterministic: keys in members/glob/tag order, flow-scalar values."""
    body = dash_indent + "  "       # mapping keys sit past the "- "
    item = body + "  "
    out = [f"{dash_indent}- container: {_scalar(container)}"]
    for key, vals in (("members", members), ("members_glob", globs),
                      ("members_tag", tags)):
        if not vals:
            continue
        out.append(f"{body}{key}:")
        out.extend(f"{item}- {_scalar(v)}" for v in vals)
    return out


def _append_group(lines: list[str], container: str, members: list[str],
                  globs: list[str], tags: list[str]) -> list[str]:
    """Append a NEW ``group:`` entry named *container* (curation-tree-editor §3 "new
    container") — creating the top-level ``group:`` block itself when absent. Additive:
    fail-closed if the container already exists (use ``add_member``/``add_glob`` to extend
    one). An entry with no members/globs/tags is a valid empty container the user then
    drags members into. Deterministic; matches the existing entries' dash indent."""
    group_at = _find_groups(lines)
    if group_at is not None:
        spans = _group_entries(lines, group_at)
        for span in spans:
            if _entry_container_name(lines, span) == container:
                raise RulesEditError(
                    f"group {container!r} already exists — use add_member/add_glob to "
                    f"extend it (creation is additive)")
        dash_indent = " " * _indent_of(lines[spans[0][0]]) if spans else "  "
        entry = _render_group_entry(container, members, globs, tags, dash_indent)
        insert_at = spans[-1][1] if spans else group_at + 1
        new = list(lines)
        new[insert_at:insert_at] = entry
        return _open_empty_flow_at(new, group_at)  # the seed's `group: []` → `group:`
    # no group: block yet — create it (with a blank separator after existing content).
    entry = _render_group_entry(container, members, globs, tags, "  ")
    new = list(lines)
    while new and not new[-1].strip():
        new.pop()
    if new:
        new.append("")
    new.append("group:")
    new.extend(entry)
    return new


# --------------------------------------------------------------------------- reorder

def _reorder_group(lines: list[str], container: str, before: str) -> list[str]:
    """Move the top-level ``group:`` entry named *container* to immediately before the entry
    named *before* — the minimal "move X before Y" that makes X win first-match-wins over Y
    (curation-tree-editor §4.5, the one-click conflict reorder).

    Comment-safe (G§Q2): only the entry's own lines move (its ``- container:`` dash plus the
    deeper-indented body, incl. interior blanks); blank-line separators stay put. A comment
    sitting *between* this entry's body and the next is ambiguous to carry along, so it is
    **fail-closed** rather than moved. Fail-closed too (via :func:`_locate_group`) when either
    group is missing or duplicated; a no-op when X already sits immediately before Y. Pure +
    deterministic — the moved block is byte-identical, re-inserted at one stable position."""
    if container == before:
        raise RulesEditError(
            "reorder_group: `container` and `before` name the same group")
    mover = _locate_group(lines, container)
    target = _locate_group(lines, before)
    entry_indent = _indent_of(lines[mover[0]])
    m_start = mover[0]
    m_end = _entry_body_end(lines, m_start, entry_indent, mover[1])
    # a comment between the mover's body and the next entry can't be safely carried (G§Q2).
    for k in range(m_end, mover[1]):
        if lines[k].strip():
            raise RulesEditError(
                f"can't safely reorder {container!r} — a comment follows it; edit in the "
                f"Rules tab", line=k + 1)
    t_start = target[0]
    # already immediately before the target (only blank lines between)? -> nothing to do.
    if m_end <= t_start and all(not lines[k].strip() for k in range(m_end, t_start)):
        return list(lines)
    block = lines[m_start:m_end]
    rest = lines[:m_start] + lines[m_end:]
    removed = m_end - m_start
    insert_at = t_start if t_start <= m_start else t_start - removed
    return rest[:insert_at] + block + rest[insert_at:]


# --------------------------------------------------------------------------- remove group

def _remove_group(lines: list[str], container: str) -> list[str]:
    """Delete the whole ``- container:`` entry named *container* from the ``group:`` block
    (curation-tree-editor context-menu "Remove container").

    Only the entry's own lines go — its ``- container:`` dash (incl. an inline comment on it,
    which describes the thing being removed) plus the deeper-indented body and its interior
    blanks. Blank-line / comment **separators** that sit *after* the body belong to the next
    entry and stay put — the same comment-safe boundary :func:`_reorder_group` uses, so a
    ``# --- core libs below ---`` banner is never swallowed. Fail-closed (via
    :func:`_locate_group`) when the container is missing or duplicated. The members fall back
    to whatever glob/tag still claims them, else become needs-curation singletons — the real
    ``curate()`` preview shows that consequence (the iron rule). Pure + deterministic."""
    span = _locate_group(lines, container)
    entry_indent = _indent_of(lines[span[0]])
    start = span[0]
    end = _entry_body_end(lines, start, entry_indent, span[1])
    return lines[:start] + lines[end:]


# --------------------------------------------------------------------------- rename group

def _rename_group(lines: list[str], old: str, new: str) -> list[str]:
    """Rewrite the ``container:`` display name of the ``group:`` entry *old* to *new*
    (curation-tree-editor §15.1, the mapping-rules part of a container rename).

    Comment-preserving line-surgery: only the value on the ``- container: <old>`` line is
    rewritten; the dash, indent, sub-lists, and every other line stay byte-identical.
    Fail-closed (G§Q2) when that line carries a trailing inline comment, when *old* is
    missing/duplicated (via :func:`_locate_group`), when *new* already names another group
    (the rename would collide two containers — curate would silently merge them), or when
    *old* == *new*. Pure + deterministic.

    NOTE: this only renames *inside* ``mapping-rules.yaml``. The display name also derives the
    ``container_id`` (``dsl_identifier('x:container:'+name)``), so references in
    ``scenarios.yaml`` (by name OR id) and ``layout-overrides.yaml`` pins (by id) must be
    rewritten too — that is :func:`rename_scenarios` / :func:`rename_layout_pins`, orchestrated
    across files by the rename endpoint (§15.1). This function is the single-file piece."""
    if old == new:
        raise RulesEditError("rename_group: `container` and `new` name the same group")
    group_at = _find_groups(lines)
    if group_at is not None:
        for span in _group_entries(lines, group_at):
            if _entry_container_name(lines, span) == new:
                raise RulesEditError(
                    f"a group named {new!r} already exists — renaming {old!r} to it would "
                    f"collide; pick another name")
    span = _locate_group(lines, old)
    start, end = span
    key_re = re.compile(r"^(\s*(?:-\s*)?container\s*:\s*)(.*)$")
    for i in range(start, end):
        m = key_re.match(lines[i])
        if not m:
            continue
        prefix, val = m.group(1), m.group(2)
        value_token, comment = _split_value_comment(val)
        if _scalar_value(value_token) != old:
            continue  # a different `container:`-looking line — keep scanning (defensive)
        # rewrite ONLY the scalar; any trailing inline comment (common on container lines) is
        # preserved verbatim — a value-swap can keep it (unlike a line-delete).
        new_lines = list(lines)
        new_lines[i] = f"{prefix}{_scalar(new)}{comment}"
        return new_lines
    raise RulesEditError(f"could not locate the `container:` line for {old!r}")


# ------------------------------------------- parent tier (container-groups plan §8.2)
# `parent:` is a cosmetic display LABEL on a `group:` entry (never an element — no id, no
# slug, no relationships), the hand-curated tier above containers. A parent exists only
# through its members: assigning writes one `parent: <label>` line into the entry, clearing
# deletes it, and nothing else in any file references the label (unlike a container rename,
# §8.5) — so `rename_parent` is a one-file value sweep with no cross-file companion.


def _find_parent_line(lines: list[str], span: tuple[int, int]
                      ) -> tuple[int, str, str, str, str] | None:
    """Locate the single ``parent:`` key line of the ``group:`` entry *span*.

    Returns ``(index, key_prefix, sep_ws, value_token, comment)`` — the line is exactly
    their concatenation — or ``None`` when the entry has no ``parent:`` key. A dash-bearing
    ``- parent: …`` line counts as the entry's key only when it IS the entry's own dash
    line (the zero-indent proposal style puts the first mapping key on the dash); anywhere
    else it is a sub-list item, not a key, and is skipped. Fail-closed (G§Q2) on a
    duplicated ``parent:`` key — PyYAML would silently last-wins it."""
    start, end = span
    key_re = re.compile(r"^(\s*(?:-\s*)?parent\s*:)(\s*)(.*)$")
    found = None
    for i in range(start, end):
        m = key_re.match(lines[i])
        if not m:
            continue
        if "-" in m.group(1) and i != start:
            continue  # a `- parent: …` sub-list item, not the entry's key
        if found is not None:
            raise RulesEditError(
                "this group has two `parent:` lines — ambiguous; edit in the Rules tab",
                line=i + 1)
        value_token, comment = _split_value_comment(m.group(3))
        found = (i, m.group(1), m.group(2), value_token, comment)
    return found


def _set_parent(lines: list[str], container: str, parent: str | None) -> list[str]:
    """Assign, replace, or clear the cosmetic ``parent:`` label of the ``group:`` entry
    *container* (container-groups plan §8.2).

    Comment-preserving line-surgery, matching :func:`_rename_group`'s idiom: a REPLACE
    rewrites only the scalar value on the existing ``parent:`` line, so an inline comment
    rides along verbatim (same label again is an idempotent no-op — re-dropping onto the
    same parent is harmless); a CLEAR (*parent* ``None``/empty) deletes the line and
    therefore fail-closes (G§Q2) when the line carries an inline comment a delete would
    eat. An absent ``parent:`` line is created directly after the entry's ``container:``
    line, at the entry's key indent (dash indent + 2 — correct for both the indented and
    the zero-indent copied-proposal entry styles). Clearing a parent the entry does not
    have is refused — ops stay explicit (the ``remove_member`` "has no member" idiom).
    Fail-closed too (via :func:`_locate_group`) when the container is missing or
    duplicated. Pure + deterministic."""
    span = _locate_group(lines, container)
    found = _find_parent_line(lines, span)
    start, end = span
    if parent:
        if found is not None:
            i, key_prefix, sep, value_token, comment = found
            if value_token and _scalar_value(value_token) == parent:
                return list(lines)  # idempotent — already this parent, keep its quoting
            if not value_token and comment:
                # a bare `parent:  # note` — materialising a value would glue it to the
                # comment; too ambiguous to guess (G§Q2).
                raise RulesEditError(
                    f"can't safely edit the parent of {container!r} — its `parent:` line "
                    f"is empty but carries an inline comment; edit in the Rules tab",
                    line=i + 1)
            new = list(lines)
            new[i] = f"{key_prefix}{sep or ' '}{_scalar(parent)}{comment}"
            return new
        # no `parent:` line yet — insert one directly after the entry's `container:` line.
        key_re = re.compile(r"^(\s*(?:-\s*)?container\s*:\s*)(.*)$")
        for i in range(start, end):
            m = key_re.match(lines[i])
            if not m:
                continue
            value_token, _comment = _split_value_comment(m.group(2))
            if _scalar_value(value_token) != container:
                continue  # a different `container:`-looking line — keep scanning (defensive)
            key_indent = " " * (_indent_of(lines[start]) + 2)
            new = list(lines)
            new.insert(i + 1, f"{key_indent}parent: {_scalar(parent)}")
            return new
        raise RulesEditError(f"could not locate the `container:` line for {container!r}")
    # clear (parent None/empty) — delete the whole line; a delete cannot keep a comment.
    if found is None:
        raise RulesEditError(f"group {container!r} has no parent to remove")
    i, _key_prefix, _sep, _value_token, comment = found
    if comment:
        raise RulesEditError(
            f"can't safely remove the parent of {container!r} — its line has an inline "
            f"comment; edit in the Rules tab", line=i + 1)
    if i == start:
        # `parent:` opens the entry's own dash line — deleting the line would delete the
        # entry dash itself (G§Q2).
        raise RulesEditError(
            f"can't safely remove the parent of {container!r} — `parent:` sits on the "
            f"entry's own dash line; edit in the Rules tab", line=i + 1)
    new = list(lines)
    del new[i]
    return new


def _rename_parent(lines: list[str], old: str, new: str) -> list[str]:
    """Rewrite EVERY ``parent: <old>`` label inside the ``group:`` block to *new*
    (container-groups plan §8.2, the ``rename_parent`` companion op).

    Two rules sharing a ``parent:`` string share the boundary, so a rename is a value
    sweep over all carrying entries — one op, one file (nothing else references parent
    labels, §8.5). Comment-preserving: only the scalar value on each matching line is
    rewritten, so inline comments ride along verbatim (a value-swap can keep them — the
    :func:`_rename_group` idiom). Fail-closed (G§Q2) when *old* == *new*, when a carrying
    entry's ``parent:`` key is duplicated (via :func:`_find_parent_line`), and when NO
    entry carries the label — ops stay explicit. Pure + deterministic (entries swept in
    file order; a pure value-swap keeps every line index stable)."""
    if old == new:
        raise RulesEditError("rename_parent: `old` and `new` name the same parent")
    out = list(lines)
    changed = False
    group_at = _find_groups(lines)
    if group_at is not None:
        for span in _group_entries(lines, group_at):
            found = _find_parent_line(lines, span)
            if found is None:
                continue
            i, key_prefix, sep, value_token, comment = found
            if not value_token or _scalar_value(value_token) != old:
                continue
            out[i] = f"{key_prefix}{sep}{_scalar(new)}{comment}"
            changed = True
    if not changed:
        raise RulesEditError(f"no group has parent {old!r}")
    return out


# ----------------------------------------------------- add_scenario (scenarios.yaml append)
# The dynamic-view plan's §9 promote write-op. This is the HEAVIEST write primitive and is
# deliberately NOT a reuse of the group: line-editor: `scenarios.yaml` is a DIFFERENT file
# (and a two-level shape — a `- key:/name:/scope:` entry whose nested `steps:` is itself a
# sequence of `- from:/to:/order?/description?` mappings), so it needs its own locate +
# render + append trio mirroring _find_groups / _render_group_entry / _append_group. It
# inherits the SAME C§8.1 fail-closed / LF / re-parse discipline (apply_ops re-parses gate-1).
#
# Provenance mapping (§0.4#5): a candidate's `source` -> the `ordering_provenance` enum that
# survives promotion. Graph-walk / low-confidence promotions ALSO get a visible "synthesized"
# comment so a promoted guess never renders identically to an observed flow.
_SOURCE_TO_PROVENANCE = {
    "test": "observed-in-test",
    "runtime": "declared-in-aspire",
    "graph-walk": "synthesized",
    "llm": "llm-proposed",
}
# Sources whose ordering is a guess, not an observed/declared flow — these get the visible
# "# ordering: synthesized ..." comment on the appended block (§9, §0.4#5).
_SYNTHESIZED_SOURCES = frozenset({"graph-walk", "llm"})


def _locate_scenarios_block(lines: list[str]) -> tuple[int, int, int] | None:
    """Locate the top-level ``scenarios:`` list.

    Returns ``(scenarios_at, entry_indent, append_at)`` — the index of the ``scenarios:`` key
    line, the indent of its entries (defaulting to 2 for an as-yet-empty block), and the index
    at which a NEW ``- key:`` entry should be inserted (after the last existing entry / before
    the next top-level key). Returns ``None`` when there is NO ``scenarios:`` key at all — the
    caller (:func:`_append_scenario_entry`) then SCAFFOLDS one, mirroring how ``_append_group``
    self-creates its ``group:`` block: a promoted candidate is a *complete* scenario, not a
    half-formed scaffold, so seeding the hand-owned file's block on first promote is fine.
    Still fail-closed (G§Q2) — raises — for a flow-style (``scenarios: [..]``), scalar,
    duplicated, or otherwise malformed ``scenarios:`` block that can't be line-edited safely
    (routed to the YAML tab)."""
    scenarios_at = None
    for i, ln in enumerate(lines):
        if _indent_of(ln) != 0:
            continue
        head = ln.split("#", 1)[0].rstrip()
        # `scenarios:` (block) — a flow-style `scenarios: [..]` is refused below.
        m = re.match(r"^scenarios\s*:(.*)$", head)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest.startswith("["):
            raise RulesEditError(
                "scenarios.yaml `scenarios:` is a flow-style list ([..]) — can't safely "
                "line-edit; edit in the Rules tab", line=i + 1)
        if rest:
            # a scalar after `scenarios:` is not a block list we can append entries to.
            raise RulesEditError(
                "scenarios.yaml `scenarios:` is not a block list — can't safely append; "
                "edit in the Rules tab", line=i + 1)
        if scenarios_at is not None:
            raise RulesEditError(
                "scenarios.yaml has two top-level `scenarios:` keys — ambiguous; edit in "
                "the Rules tab", line=i + 1)
        scenarios_at = i
    if scenarios_at is None:
        return None  # no `scenarios:` key — the caller scaffolds one (first-promote bootstrap)
    body_start = scenarios_at + 1
    # the entry indent is the indent of the first dash-line under `scenarios:` — 0 is
    # valid (the same-indent block-sequence form `yaml.safe_dump` writes; see
    # _group_entries), so the dash check comes BEFORE the zero-indent "empty" bail.
    entry_indent = None
    for i in range(body_start, len(lines)):
        ln = lines[i]
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if ln.lstrip().startswith("-"):
            entry_indent = _indent_of(ln)
            break
        if _indent_of(ln) == 0:
            break  # dedented to a new top-level key — the list is empty
        # a non-dash, indented line directly under `scenarios:` is malformed for a list.
        raise RulesEditError(
            "scenarios.yaml `scenarios:` block is not a list of entries — edit in the "
            "Rules tab", line=i + 1)
    if entry_indent is None:
        # an empty `scenarios:` block (a user-created seed with no entries yet) — append the
        # first entry at the conventional 2-space indent rather than refusing (first-promote).
        entry_indent = 2
    # the append point: after the last entry, before the next top-level key / EOF; back up
    # over trailing blank lines so the new entry sits flush with the last one. A zero-indent
    # dash/comment line is part of a zero-indent entry list, not a new top-level key.
    block_end = len(lines)
    for i in range(body_start, len(lines)):
        ln = lines[i]
        if (ln.strip() and _indent_of(ln) == 0
                and not (entry_indent == 0
                         and (ln.startswith("-") or ln.lstrip().startswith("#")))):
            block_end = i
            break
    append_at = block_end
    while append_at > body_start and not lines[append_at - 1].strip():
        append_at -= 1
    return scenarios_at, entry_indent, append_at


def _scenario_keys(lines: list[str], scenarios_at: int) -> list[str]:
    """The ``key:`` of every existing scenario entry under ``scenarios:`` — for the
    duplicate-key fail-closed check. Parses the whole block via yaml so quoting/flow forms
    resolve exactly as the §9.4 generator reads them; a parse failure is reported by the
    caller's gate-1 re-parse, so here we degrade to ``[]`` (no false duplicate)."""
    body = "\n".join(lines[scenarios_at:])
    try:
        loaded = yaml.safe_load(body)
    except yaml.YAMLError:
        return []
    if not isinstance(loaded, dict):
        return []
    entries = loaded.get("scenarios")
    if not isinstance(entries, list):
        return []
    return [str(e["key"]) for e in entries
            if isinstance(e, dict) and e.get("key") is not None]


def _render_scenario_entry(key: str, name: str | None, scope: str | None,
                           ordering_provenance: str | None, steps: list[dict[str, Any]],
                           *, dash_indent: str, synthesized: bool) -> list[str]:
    """Render ONE ``- key: …`` scenario entry (+ optional name/scope/ordering_provenance and
    a nested ``steps:`` sequence) as lines, indented to *dash_indent*. Deterministic — keys in
    fixed order, flow-scalar values (quoted only where YAML needs it).

    The two-level shape: the entry mapping keys sit past the ``- `` (``dash_indent + "  "``);
    the nested ``steps:`` sequence dashes sit one level deeper, and each step's mapping keys
    one past THAT dash. Each step row carries ``from:``/``to:`` (the stable build-target id /
    container_id, §0.4#6) and, when present, ``order:`` (the parallel facet) and
    ``description:``. A step may carry a literal ``_comment`` (a ``# FLAGGED ...`` or runtime
    warning) rendered as its own line directly above the step's dash. When *synthesized* the
    block carries a leading ``# ordering: synthesized ...`` comment (§9, §0.4#5)."""
    body = dash_indent + "  "          # entry mapping keys sit past the "- "
    step_dash = body + "  "            # the `steps:` sequence dashes
    step_body = step_dash + "  "       # a step mapping's keys, past its own "- "
    out: list[str] = []
    if synthesized:
        out.append(f"{dash_indent}# ordering: synthesized "
                   f"({ordering_provenance or 'synthesized'}; not an observed flow)")
    out.append(f"{dash_indent}- key: {_scalar(key)}")
    if name:
        out.append(f"{body}name: {_scalar(name)}")
    if scope:
        out.append(f"{body}scope: {_scalar(scope)}")
    if ordering_provenance:
        out.append(f"{body}ordering_provenance: {_scalar(ordering_provenance)}")
    out.append(f"{body}steps:")
    for step in steps:
        comment = step.get("_comment")
        if comment:
            out.append(f"{step_dash}{comment}")
        # `from:` opens the step mapping (on the dash line); the rest sit at step_body.
        out.append(f"{step_dash}- from: {_scalar(str(step['from']))}")
        out.append(f"{step_body}to: {_scalar(str(step['to']))}")
        order = step.get("order")
        if order is not None:
            try:
                order_int = int(order)
            except (TypeError, ValueError):
                # fail-closed (§9): a non-integer `order` on a verbatim-promoted proposal can't
                # be line-edited safely — raise RulesEditError (a 400, not a bare ValueError/500).
                raise RulesEditError(
                    f"scenario step has a non-integer `order` ({order!r}) that can't be "
                    f"promoted — edit in the YAML tab")
            out.append(f"{step_body}order: {order_int}")
        desc = step.get("description")
        if desc:
            out.append(f"{step_body}description: {_scalar_desc(str(desc))}")
    return out


def _step_endpoints(vs: dict[str, Any], ps: dict[str, Any],
                    i: int) -> tuple[str, str]:
    """The **stable** ``from``/``to`` ids for step *i* (1-based ``i+1``), preferring the
    proposal row's ids (the promotable material, §0.4#6) and falling back to the validated
    row's resolved ids / raw tokens. Fail-closed when neither yields a usable endpoint."""
    frm = ps.get("from") or vs.get("from") or vs.get("from_raw")
    to = ps.get("to") or vs.get("to") or vs.get("to_raw")
    if frm is None or to is None:
        raise RulesEditError(
            f"scenario step {i + 1} has no usable from/to to promote — edit in the "
            f"Rules tab")
    return str(frm), str(to)


def _promotable_steps(view_steps: list[dict[str, Any]],
                      proposal_steps: list[dict[str, Any]], *,
                      on_gap: str, key: str) -> list[dict[str, Any]]:
    """Build the step rows to append, applying the §9 **contiguity rule**.

    Default policy: promote only ``status == "ok"`` validated steps. But silently dropping a
    flagged step in the MIDDLE of a chain (``A->B->C->D`` losing ``B->C``) produces a
    2-fragment diagram that LOOKS complete — so "ok-only + a count" is insufficient. The rule,
    walking validated steps in order and tracking the previous kept step's ``to``:

    - An ``ok`` step is kept. A gap is detected when a kept ``ok`` step's ``from`` != the
      previous kept step's ``to`` (a flagged step was dropped between them). On a gap:
      - ``on_gap == "refuse"`` (default): raise :class:`RulesEditError` routing to the YAML
        tab, naming the gap — silently shipping a 2-fragment diagram is not allowed.
      - ``on_gap == "flag"``: PROMOTE the intervening flagged step(s) with a visible
        ``# FLAGGED: <reason> (X -> Y)`` comment so the gap is honest in the YAML.
    - A runtime-only ``ok`` step (``dsl: false``) is always promotable but carries a
      ``# runtime-only: not emitted to Structurizr DSL`` warning comment (§9).

    Returns step dicts for :func:`_render_scenario_entry`
    (``from``/``to``/``order``/``description``/``_comment``) using the **stable** ids
    (§0.4#6). *proposal_steps* supplies the canonical promotable material; *view_steps*
    supplies the per-step ``status``. They are paired by 1-based ``index``."""
    out: list[dict[str, Any]] = []
    prev_to: str | None = None
    pending_flagged: list[tuple[int, dict[str, Any], str, str]] = []
    for i, vs in enumerate(view_steps):
        ps = proposal_steps[i] if i < len(proposal_steps) else {}
        frm, to = _step_endpoints(vs, ps, i)
        if vs.get("status") != "ok":
            # hold flagged steps aside; they are only materialised if keeping the chain
            # contiguous needs them (the `flag` branch) — else they are dropped (ok-only).
            pending_flagged.append((i, ps, frm, to))
            continue
        # an `ok` step. Does dropping the held flagged steps open a gap before it?
        if prev_to is not None and pending_flagged and frm != prev_to:
            if on_gap == "refuse":
                gi, _gps, gfrm, gto = pending_flagged[0]
                raise RulesEditError(
                    f"can't promote {key!r}: dropping flagged step {gi + 1} "
                    f"({gfrm} -> {gto}) would break the chain "
                    f"({prev_to} -> ... -> {frm}); promote with a FLAGGED comment or "
                    f"edit in the Rules tab")
            # on_gap == "flag": emit the held flagged steps with a visible comment.
            for gi, gps, gfrm, gto in pending_flagged:
                gvs = view_steps[gi]
                reason = gvs.get("reason") or "no evidenced build or runtime container edge"
                out.append({
                    "from": gfrm, "to": gto,
                    "order": gps.get("order", gvs.get("order")),
                    "description": gps.get("description") or gvs.get("description"),
                    "_comment": f"# FLAGGED: {reason} ({gfrm} -> {gto})",
                })
        pending_flagged = []
        row: dict[str, Any] = {
            "from": frm, "to": to,
            "order": ps.get("order", vs.get("order")),
            "description": ps.get("description") or vs.get("description"),
        }
        if vs.get("dsl") is False:
            row["_comment"] = ("# runtime-only: not emitted to Structurizr DSL "
                               "(no static container relationship)")
        out.append(row)
        prev_to = to
    # trailing flagged steps after the last `ok` step are tail noise — dropping them never
    # breaks contiguity (nothing follows), so they are dropped (ok-only) in both branches.
    if not out:
        raise RulesEditError(
            f"scenario {key!r} has no `ok` steps to promote — edit in the Rules tab")
    return out


def _append_scenario_entry(lines: list[str], key: str, name: str | None,
                           scope: str | None, ordering_provenance: str | None,
                           steps: list[dict[str, Any]], *, synthesized: bool) -> list[str]:
    """Append a NEW ``- key: …`` scenario entry to the top-level ``scenarios:`` block,
    mirroring :func:`_append_group` (§9). When the file has NO ``scenarios:`` block yet, one is
    SCAFFOLDED (the first-promote bootstrap — a promoted candidate is a complete scenario, not a
    half-formed scaffold, so seeding the hand-owned file's block here is appropriate, exactly as
    ``_append_group`` already does for ``group:``). Still fail-closed (G§Q2) on a flow-style/
    scalar/duplicated/malformed block (via :func:`_locate_scenarios_block`), a duplicate ``key:``
    (the §9.4 generator would shadow one), and an empty step list. Pure (returns a new list)."""
    if not steps:
        raise RulesEditError(
            f"scenario {key!r} has no promotable steps — nothing to append")
    loc = _locate_scenarios_block(lines)
    if loc is None:
        # No `scenarios:` key — scaffold one at EOF (after a blank separator), then append.
        base = list(lines)
        while base and not base[-1].strip():
            base.pop()
        if base:
            base.append("")
        base.append("scenarios:")
        entry_indent, append_at, existing_keys = 2, len(base), []
    else:
        scenarios_at, entry_indent, append_at = loc
        base = list(lines)
        existing_keys = _scenario_keys(base, scenarios_at)
    if key in existing_keys:
        raise RulesEditError(
            f"a scenario with key {key!r} already exists in scenarios.yaml — pick another "
            f"key (creation is additive)")
    dash_indent = " " * entry_indent
    entry = _render_scenario_entry(
        key, name, scope, ordering_provenance, steps,
        dash_indent=dash_indent, synthesized=synthesized)
    new = list(base)
    new[append_at:append_at] = entry
    return new


def _add_scenario(lines: list[str], op: dict[str, Any]) -> list[str]:
    """The ``add_scenario`` op: promote a candidate's ``proposal`` into ``scenarios.yaml``
    (§9). The op carries the candidate envelope's ``proposal`` (stable ids) + ``validated_view``
    (per-step status) + ``source`` (drives ordering_provenance + the synthesized comment). It
    applies the contiguity rule (:func:`_promotable_steps`) and appends a two-level entry."""
    proposal = op.get("proposal")
    if not isinstance(proposal, dict):
        raise RulesEditError("add_scenario requires a `proposal` object")
    key = proposal.get("key")
    if not key:
        raise RulesEditError("add_scenario `proposal` requires a non-empty `key`")
    proposal_steps = proposal.get("steps")
    if not isinstance(proposal_steps, list) or not proposal_steps:
        raise RulesEditError(
            f"add_scenario `proposal` for {key!r} has no steps to promote")
    view = op.get("validated_view") or {}
    view_steps = view.get("steps") if isinstance(view, dict) else None
    if not isinstance(view_steps, list) or not view_steps:
        # no validated view supplied: promote the raw proposal steps verbatim (all `ok`-shaped).
        view_steps = [{"status": "ok", "from": s.get("from"), "to": s.get("to"),
                       "order": s.get("order"), "description": s.get("description")}
                      for s in proposal_steps]
    source = op.get("source")
    ordering_provenance = (op.get("ordering_provenance")
                           or _SOURCE_TO_PROVENANCE.get(str(source), "hand-authored"))
    synthesized = str(source) in _SYNTHESIZED_SOURCES
    # contiguity policy (§9): "refuse" (default) routes a chain-breaking drop to the YAML tab;
    # "flag" promotes the intervening flagged step with a visible `# FLAGGED` comment.
    on_gap = op.get("on_gap", "refuse")
    if on_gap not in ("refuse", "flag"):
        raise RulesEditError("add_scenario `on_gap` must be 'refuse' or 'flag'")
    steps = _promotable_steps(view_steps, proposal_steps,
                              on_gap=on_gap, key=str(key))
    return _append_scenario_entry(
        lines, str(key), proposal.get("name"), proposal.get("scope"),
        ordering_provenance, steps, synthesized=synthesized)


# ------------------------------------------------ dismiss_candidate (adr-candidates.yaml append)
# The ADR-mining plan's §3.1 dismissal state. `rules/adr-candidates.yaml` is a DIFFERENT
# hand-owned file with a top-level `dismissed:` list of `- slug:/reason?/dismissed_at_evidence_hash:`
# entries. Like the scenario/exclude primitives this is a scoped textual block-append on its own
# file, inheriting the same C§8.1 fail-closed / LF / re-parse discipline (apply_ops re-parses
# gate-1). Unlike `scenarios:` (which REFUSES a missing block) and like the `group:`/`exclude:`
# blocks, a missing `dismissed:` block is CREATED at end of file (locate-or-create) — a dismissal
# is the first thing ever written to a fresh adr-candidates.yaml, so refusing it would be useless.


def _dismissed_entries(lines: list[str], dismissed_at: int,
                       body_end: int) -> list[dict[str, Any]]:
    """The parsed `- slug:/...` entries of the `dismissed:` block — for the idempotency
    check (same slug + same hash = no-op). Parses the block via yaml so quoting resolves
    exactly as a downstream consumer reads it; a parse failure degrades to ``[]`` (no false
    duplicate — the caller's gate-1 re-parse reports a genuinely broken splice)."""
    block = "dismissed:\n" + "\n".join(lines[dismissed_at + 1:body_end])
    try:
        loaded = yaml.safe_load(block)
    except yaml.YAMLError:
        return []
    if not isinstance(loaded, dict):
        return []
    entries = loaded.get("dismissed")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def _append_dismissal(lines: list[str], slug: str, reason: str,
                      evidence_hash: str) -> list[str]:
    """Append a dismissal entry to the top-level ``dismissed:`` list of
    ``rules/adr-candidates.yaml`` (ADR-mining plan §3.1), creating the block when absent
    (locate-or-create, mirroring :func:`_ensure_exclude_member`).

    Each entry is a ``- slug:/reason?/dismissed_at_evidence_hash:`` mapping. *slug* and
    *evidence_hash* are simple tokens (flow-scalar rendered via :func:`_scalar`); *reason* is
    free text that MUST be quoted (it can carry ``:``/``—``/``#``) — also via :func:`_scalar`,
    which double-quotes when YAML needs it, so a ``#`` in the reason never becomes a comment.
    An empty/missing *reason* OMITS the ``reason:`` line entirely (cleaner than an empty
    string; downstream treats an absent reason as no reason).

    Idempotency (§3.1): a re-dismiss of the SAME slug with the SAME
    ``dismissed_at_evidence_hash`` already present is a no-op (returns ``list(lines)``). A same
    slug with a DIFFERENT hash appends a new entry anyway — the candidate's evidence materially
    changed and was re-dismissed; downstream last-wins consumption handles the duplicate slug.

    Fail-closed (G§Q2): refuses a flow-style ``dismissed: [..]`` block and a ``dismissed:``
    block defined more than once (the house pattern). Pure + deterministic."""
    def _render_entry(dash_indent: str) -> list[str]:
        # the entry mapping keys sit past the "- " (dash_indent + two for "- ").
        body = " " * (len(dash_indent) + 2)
        out = [f"{dash_indent}- slug: {_scalar(slug)}"]
        if reason:
            out.append(f"{body}reason: {_scalar(reason)}")
        out.append(f"{body}dismissed_at_evidence_hash: {_scalar(evidence_hash)}")
        return out

    # find a top-level `dismissed:` key (flow-style / duplicate are fail-closed).
    dismissed_at = None
    for i, ln in enumerate(lines):
        if _indent_of(ln) != 0:
            continue
        m = re.match(r"^dismissed\s*:(.*)$", ln.split("#", 1)[0].rstrip())
        if not m:
            continue
        rest = m.group(1).strip()
        if rest.startswith("["):
            raise RulesEditError(
                "adr-candidates.yaml `dismissed:` is a flow-style list ([..]) — can't "
                "safely line-edit; edit in the Rules tab", line=i + 1)
        if rest:
            raise RulesEditError(
                "adr-candidates.yaml `dismissed:` is not a block list — can't safely "
                "append; edit in the Rules tab", line=i + 1)
        if dismissed_at is not None:
            raise RulesEditError(
                "adr-candidates.yaml has two top-level `dismissed:` keys — ambiguous; edit "
                "in the Rules tab", line=i + 1)
        dismissed_at = i
    if dismissed_at is None:
        # create the whole block at end of file (a blank separator, then the block + entry).
        new = list(lines)
        while new and not new[-1].strip():
            new.pop()
        if new:
            new.append("")
        new.append("dismissed:")
        new.extend(_render_entry("  "))
        return new
    # find the extent of the existing list (the run of `- ` rows under the key, up to the
    # next top-level key / EOF), then back up over trailing blank lines.
    block_end = len(lines)
    for i in range(dismissed_at + 1, len(lines)):
        if lines[i].strip() and _indent_of(lines[i]) == 0:
            block_end = i
            break
    # idempotency: same slug + same hash already present -> no-op.
    for e in _dismissed_entries(lines, dismissed_at, block_end):
        if (str(e.get("slug")) == slug
                and str(e.get("dismissed_at_evidence_hash")) == evidence_hash):
            return list(lines)
    # the list dash indent: match an existing `- ` row, else two spaces under the key.
    dash_indent = "  "
    for i in range(dismissed_at + 1, block_end):
        if lines[i].lstrip().startswith("-") and _indent_of(lines[i]) > 0:
            dash_indent = " " * _indent_of(lines[i])
            break
    append_at = block_end
    while append_at > dismissed_at + 1 and not lines[append_at - 1].strip():
        append_at -= 1
    new = list(lines)
    entry = _render_entry(dash_indent)
    new[append_at:append_at] = entry
    return new


# --------------------------------------------------------------------------- op dispatch

def _split(text: str) -> list[str]:
    return text.split("\n")


def _join(lines: list[str]) -> str:
    out = "\n".join(lines)
    if not out.endswith("\n"):
        out += "\n"
    return out


def _validate(new_text: str) -> None:
    """Gate 1 (G§Q3): the candidate must re-parse as a YAML mapping. A structural failure
    HARD-refuses (fail-closed) — never write an unparsable splice."""
    try:
        parsed = yaml.safe_load(new_text)
    except yaml.YAMLError as exc:
        raise RulesEditError(f"the edited rules no longer parse as YAML: {exc}")
    if parsed is not None and not isinstance(parsed, dict):
        raise RulesEditError("the edited rules are no longer a YAML mapping")


def _apply_one(lines: list[str], op: dict[str, Any]) -> list[str]:
    """Apply ONE structured op to the line list, returning the new line list. Pure; the
    caller threads these so a multi-edit op builds one in-memory text (G§Q8)."""
    kind = op.get("op")
    if kind == "add_member":
        # an explicit-member insert is a CLAIM: the id's stale explicit lines in other
        # containers are swept first (curate last-wins silently otherwise — the
        # "Add explicit… duplicated the member" desync). Glob/tag claims stay.
        container, mid = _req(op, "container"), _req(op, "id")
        key = op.get("key", "members")
        out = lines
        if key == "members":
            out = _sweep_explicit_claims(out, mid, container)
        return _insert_member(out, container, key, mid)
    if kind == "move_member":
        # delete-from-source (only if it was an explicit `members:` there) then add to
        # dest — ONE pass, so a fail-closed source delete aborts the whole move (G§Q8).
        # The exclusivity sweep also strips any OTHER stale explicit claim (a pre-existing
        # duplicate), so after a move the id lives in exactly one `members:` list.
        src, mid, to = op.get("from"), _req(op, "id"), _req(op, "to")
        out = lines
        if src:
            out = _remove_member(out, src, op.get("from_key", "members"),
                                 mid, required=False)
        if op.get("to_key", "members") == "members":
            out = _sweep_explicit_claims(out, mid, to)
        return _insert_member(out, to, op.get("to_key", "members"), mid)
    if kind == "exclude":
        key = op.get("key", "exclude_ids")
        if key not in _EXCLUDE_KEYS:
            raise RulesEditError(f"exclude op key must be one of {_EXCLUDE_KEYS}")
        # if the target sat in an explicit `members:` somewhere, drop it there too so the
        # exclusion isn't shadowed by an explicit claim (additive, fail-open removal).
        out = lines
        src = op.get("from")
        if src:
            out = _remove_member(out, src, op.get("from_key", "members"),
                                 _req(op, "id"), required=False)
        return _ensure_exclude_member(out, key, _req(op, "id"))
    if kind == "generalize_to_glob":
        # replace N explicit `members:` of one container with one `members_glob:` row.
        container = _req(op, "container")
        ids = op.get("ids") or []
        if not ids:
            raise RulesEditError("generalize_to_glob needs a non-empty `ids` list")
        pattern = _req(op, "pattern")
        out = lines
        for mid in ids:
            out = _remove_member(out, container, "members", mid, required=False)
        # if generalize emptied the explicit `members:`, drop the now-empty key line so
        # the patch reads as a clean swap (N members -> one glob, §4.2).
        out = _drop_empty_sublist(out, container, "members")
        return _insert_member(out, container, "members_glob", pattern)
    if kind == "set_glob":
        container = _req(op, "container")
        key = op.get("key", "members_glob")
        if key not in ("members_glob", "members_tag"):
            raise RulesEditError("set_glob op key must be members_glob or members_tag")
        return _replace_value(lines, container, key,
                              _req(op, "old"), _req(op, "new"))
    if kind == "remove_member":
        # un-assign an explicit member: delete it from the container's `members:` so it
        # falls back to whatever glob/tag still claims it, else a needs-curation singleton.
        return _remove_member(lines, _req(op, "container"), op.get("key", "members"),
                              _req(op, "id"), required=True)
    if kind == "add_glob":
        # add a NEW glob/tag pattern to a container (incl. one with no glob bucket yet).
        key = op.get("key", "members_glob")
        if key not in ("members_glob", "members_tag"):
            raise RulesEditError("add_glob op key must be members_glob or members_tag")
        return _insert_member(lines, _req(op, "container"), key, _req(op, "pattern"))
    if kind == "create_group":
        # append a brand-new container (§3 "new container"); additive, fail-closed on clash.
        # Explicit members it carries are claims too — sweep their stale lines elsewhere.
        container = _req(op, "container")
        members = [m for m in (op.get("members") or []) if isinstance(m, str)]
        out = lines
        for m in members:
            out = _sweep_explicit_claims(out, m, container)
        return _append_group(
            out, container, members,
            [g for g in (op.get("members_glob") or []) if isinstance(g, str)],
            [t for t in (op.get("members_tag") or []) if isinstance(t, str)])
    if kind == "reorder_group":
        # move group X to immediately before group Y (the §4.5 one-click conflict reorder).
        return _reorder_group(lines, _req(op, "container"), _req(op, "before"))
    if kind == "remove_group":
        # delete a whole container entry (the context-menu "Remove container"); members fall
        # back to a glob/tag claim or a needs-curation singleton (the preview shows it).
        return _remove_group(lines, _req(op, "container"))
    if kind == "rename_group":
        # rename a container's display name IN mapping-rules.yaml (the §15.1 single-file
        # part); scenarios/layout refs are rewritten by the endpoint via the helpers below.
        return _rename_group(lines, _req(op, "container"), _req(op, "new"))
    if kind == "set_parent":
        # assign/replace/clear the cosmetic `parent:` label on ONE group entry (container-
        # groups plan §8.2 — a display label, never an element; null/empty clears the line).
        parent = op.get("parent")
        parent = None if parent is None or parent == "" else str(parent)
        return _set_parent(lines, _req(op, "container"), parent)
    if kind == "rename_parent":
        # rewrite every `parent: <old>` label in the group block (§8.2's cheap companion);
        # one op, one file — nothing else references parent labels (unlike a container
        # rename, §8.5).
        return _rename_parent(lines, _req(op, "old"), _req(op, "new"))
    if kind == "add_scenario":
        # promote a candidate's proposal into the hand-owned scenarios.yaml (dynamic-view
        # plan §9 — the heaviest write primitive, a NEW scenarios.yaml block-append, NOT a
        # group: line-editor reuse). The op carries `proposal` + `validated_view` + `source`.
        return _add_scenario(lines, op)
    if kind == "dismiss_candidate":
        # append a dismissal entry to the hand-owned adr-candidates.yaml `dismissed:` list
        # (ADR-mining plan §3.1); locate-or-create, idempotent on slug+hash, last-wins on a
        # changed hash. A different file from mapping-rules/scenarios.
        return _append_dismissal(lines, _req(op, "slug"), op.get("reason", ""),
                                 _req(op, "evidence_hash"))
    raise RulesEditError(f"unknown op {kind!r}")


def _req(op: dict[str, Any], field: str) -> str:
    v = op.get(field)
    if v is None or v == "":
        raise RulesEditError(f"op {op.get('op')!r} requires field {field!r}")
    return str(v)


def apply_ops(text: str, ops: list[dict[str, Any]]) -> str:
    """Apply *ops* in order to the rules YAML *text*, returning the new text (G§Q8).

    ONE in-memory line list is threaded through every op and every primitive, validated
    once at the end (gate 1, re-parse). Any fail-closed sub-edit (:class:`RulesEditError`)
    aborts the whole call before any text is returned — so a multi-edit op like ``move``
    is atomic for free and the file is never half-edited. Deterministic: pure functions
    over a fixed input, LF, stable insert positions, existing member order preserved.
    """
    if not ops:
        raise RulesEditError("no ops to apply")
    lines = _split(text)
    for op in ops:
        if not isinstance(op, dict):
            raise RulesEditError("each op must be an object")
        lines = _apply_one(lines, op)
    new_text = _join(lines)
    _validate(new_text)
    return new_text


# --------------------------------------------------------------------------- glob helper

def tightest_glob(ids: list[str], targets: list[dict[str, Any]]) -> str | None:
    """The tightest ``members_glob`` pattern covering EXACTLY *ids* and nothing else
    (curation-tree-editor §4.2, the ``generalize_to_glob`` helper).

    Tries, cheapest→most-permissive: a shared id prefix + ``*``, a shared path prefix +
    ``*``, a shared name prefix + ``*``. A candidate is accepted only when re-running
    :func:`glob_matches_target` over the full *targets* set claims exactly the *ids* set
    (the "and 0 others" safety check, §4.2) — so it never silently catches an outsider.
    Returns ``None`` when no clean pattern exists (the GUI then falls back to explicit
    members). Pure + deterministic.
    """
    want = set(ids)
    if not want:
        return None
    by_id = {t.get("id"): t for t in targets if t.get("id")}
    selected = [by_id[i] for i in ids if i in by_id]
    if len(selected) != len(want):
        return None  # an id we were asked to cover isn't a real target

    def _claims_exactly(pat: str) -> bool:
        claimed = {t["id"] for t in targets if t.get("id")
                   and glob_matches_target(pat, t)}
        return claimed == want

    def _common_prefix(values: list[str]) -> str:
        if not values:
            return ""
        s1, s2 = min(values), max(values)
        n = 0
        while n < len(s1) and n < len(s2) and s1[n] == s2[n]:
            n += 1
        return s1[:n]

    for field in ("id", "path", "name"):
        values = [str(t.get(field, "")) for t in selected]
        if any(v == "" for v in values):
            continue
        prefix = _common_prefix(values)
        if not prefix:
            continue
        pat = prefix + "*"
        if _claims_exactly(pat):
            return pat
    return None


def matched_ids(pattern: str, key: str, targets: list[dict[str, Any]]) -> list[str]:
    """The id/path/name (``members_glob``) or tag (``members_tag``) *reach* of *pattern*
    over *targets* — sorted ids (curation-tree-editor §4.3 / §9.2 proposal expansion).

    Pure ``glob_matches_target``/``tag_matches_target`` *reach*, NOT the engine's *claim*
    (first-match-wins + exclude + tags) — used to render proposal leaves and the optimistic
    client highlight; the authoritative claim still comes from ``curate()``."""
    match = tag_matches_target if key == "members_tag" else glob_matches_target
    return sorted(t["id"] for t in targets
                  if t.get("id") and match(pattern, t))


# ------------------------------------------------------ rename: cross-file reference rewrites
# A container rename changes its DISPLAY NAME and (because container_id is derived from the
# name, `dsl_identifier('x:container:'+name)`) its container_id too. References live in two
# OTHER reviewed files; these pure helpers rewrite them so a rename doesn't leave dangling
# scenario steps or dead layout pins (curation-tree-editor §15.1). The endpoint computes the
# old/new container_ids (the id logic stays in apply_mapping_rules) and threads them in.


def rename_scenarios(text: str, old_name: str, new_name: str,
                     old_cid: str, new_cid: str) -> str:
    """Rewrite `scenarios.yaml` `from:`/`to:` step endpoints that reference the renamed
    container — by **name** (``old_name`` → ``new_name``) or by **container_id**
    (``old_cid`` → ``new_cid``). Build-target-id endpoints are unaffected (a rename never
    changes a target id).

    Comment-preserving line-surgery (scenarios is hand-authored with descriptive comments):
    only the scalar value on a matching `from:`/`to:` line is rewritten. Fail-closed (G§Q2)
    when a line being rewritten carries a trailing inline comment. Returns *text* byte-for-byte
    when nothing references the container (no spurious diff / no write). Pure + deterministic."""
    lines = _split(text)
    key_re = re.compile(r"^(\s*(?:-\s*)?(?:from|to)\s*:\s*)(.*)$")
    out = list(lines)
    changed = False
    for i, ln in enumerate(lines):
        m = key_re.match(ln)
        if not m:
            continue
        prefix, val = m.group(1), m.group(2)
        scalar = _scalar_value(val)
        if scalar == old_name:
            repl = new_name
        elif scalar == old_cid:
            repl = new_cid
        else:
            continue
        if not _strip_inline_comment_is_safe(val):
            raise RulesEditError(
                f"can't safely rename in scenarios.yaml — `{ln.strip()}` has an inline "
                f"comment; edit it in the Rules tab", line=i + 1)
        out[i] = f"{prefix}{_scalar(repl)}"
        changed = True
    return _join(out) if changed else text


# Mirrors layout.merge_overrides' file header so a rename re-dump is byte-consistent with how
# the GUI lock step writes this file (§5.10). Kept in sync deliberately (small, stable).
_LAYOUT_HEADER = (
    "# Hand-pinned layout positions (GUI plan §5.10) — rules-tier, per view,\n"
    "# keyed by stable element id; consumed as pinned coordinates by\n"
    "# `arch export --layout`. Written by the GUI's unlock->edit->lock\n"
    "# workflow (full re-dump: hand comments here do not survive a lock).\n")


def rename_layout_pins(text: str, old_cid: str, new_cid: str) -> str:
    """Rewrite `layout-overrides.yaml` pins keyed by the renamed container's id
    (``old_cid`` → ``new_cid``) across every view.

    Unlike the other two files this one's documented contract (layout.merge_overrides) is a
    deterministic **full re-dump** — comments do not survive a GUI lock — so the robust,
    contract-consistent rename is parse → rename the pin key → re-dump (sorted keys, the same
    header the lock step writes). This sidesteps the colon-in-key quoting fragility a textual
    edit would face (``container:webFrontend`` is itself colon-bearing). Returns *text*
    unchanged when no pin references the container. Pure + deterministic."""
    if not text.strip():
        return text
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RulesEditError(f"layout-overrides.yaml does not parse: {exc}")
    if not isinstance(doc, dict):
        return text
    views = doc.get("views")
    if not isinstance(views, dict):
        return text
    changed = False
    for pins in views.values():
        if isinstance(pins, dict) and old_cid in pins:
            pins[new_cid] = pins.pop(old_cid)
            changed = True
    if not changed:
        return text
    # re-sort each view's pins so the dump is deterministic regardless of dict insertion order.
    doc["views"] = {v: {k: pins[k] for k in sorted(pins)}
                    if isinstance(pins, dict) else pins
                    for v, pins in views.items()}
    return _LAYOUT_HEADER + yaml.safe_dump(
        doc, sort_keys=True, default_flow_style=False, allow_unicode=True)
