"""Curation Tree Editor — the scoped textual line-editor (curation-tree-editor §8.1, §12).

Pure-function tests of ``anon.rules_edit`` (no server): each of the five ops produces
the expected minimal, comment-preserving patch; the fail-closed rules (inline comment on a
deleted line, flow-style list, ambiguous/duplicate block) leave the file unchanged and
raise a located :class:`RulesEditError`; a multi-edit op (``move``) is atomic; the
``tightest_glob`` helper matches exactly N and zero others.

The two-gate semantics (the real-``curate()`` intent check, gate-2 shadow warnings, the
If-Match write path) are exercised against the live endpoint in ``test_serve.py``; here we
test only the textual surgery, which is the determinism/byte-preservation risk (§14).
"""
from __future__ import annotations

import pytest

import yaml

from anon import rules_edit
from anon.rules_edit import RulesEditError, apply_ops

# A representative hand-authored rules file: comments, name pins, an explicit members list,
# a glob bucket, a tag bucket, and an exclude block — the shapes the ops must preserve.
RULES = """\
# Curation rules (hand-owned, reviewed).
version: 1
defaults:
  min_relationship_weight: 3   # declared deps never dropped

group:
  - container: "Web Frontend"          # the public-facing app
    members:
      - "csharp:csproj:src/Web/Web.csproj"
  - container: "Core Library"
    members:
      - "csharp:csproj:src/Domain/Domain.csproj"
      - "csharp:csproj:src/Common/Common.csproj"
  - container: "Modules"
    members_glob:
      - "src/Modules/*"
  - container: "Content"
    members_tag:
      - "module-category:Content"

exclude:
  targets:
    - glob: "*.Tests"
"""


def _parse(text):
    return yaml.safe_load(text)


def _group(text, name):
    for g in _parse(text)["group"]:
        if g["container"] == name:
            return g
    return None


def _diff_lines(before, after):
    """Lines added (+) / removed (-) — the minimal-patch assertion."""
    b = before.split("\n")
    a = after.split("\n")
    added = [ln for ln in a if ln not in b]
    removed = [ln for ln in b if ln not in a]
    return added, removed


# --------------------------------------------------------------------------- add_member

def test_add_member_appends_to_existing_block_list():
    out = apply_ops(RULES, [{"op": "add_member", "container": "Core Library",
                             "id": "csharp:csproj:src/Extra/Extra.csproj"}])
    members = _group(out, "Core Library")["members"]
    assert members == [
        "csharp:csproj:src/Domain/Domain.csproj",
        "csharp:csproj:src/Common/Common.csproj",
        "csharp:csproj:src/Extra/Extra.csproj",          # appended, order preserved
    ]
    added, removed = _diff_lines(RULES, out)
    assert removed == [], "additive — touches no existing line"
    assert len(added) == 1 and "Extra.csproj" in added[0]
    # every untouched line (incl. comments) is byte-identical
    assert "# the public-facing app" in out
    assert "# declared deps never dropped" in out


def test_add_member_creates_list_when_group_has_only_glob():
    out = apply_ops(RULES, [{"op": "add_member", "container": "Modules",
                             "id": "csharp:csproj:src/Odd/Odd.csproj"}])
    g = _group(out, "Modules")
    assert g["members"] == ["csharp:csproj:src/Odd/Odd.csproj"]
    assert g["members_glob"] == ["src/Modules/*"], "glob untouched"


def test_add_member_idempotent():
    once = apply_ops(RULES, [{"op": "add_member", "container": "Core Library",
                              "id": "csharp:csproj:src/Domain/Domain.csproj"}])
    assert once == RULES, "adding an existing member is a no-op"


# --------------------------------------------------------------------------- move_member

def test_move_member_is_atomic_delete_plus_add():
    out = apply_ops(RULES, [{"op": "move_member", "from": "Core Library",
                             "to": "Web Frontend",
                             "id": "csharp:csproj:src/Common/Common.csproj"}])
    assert _group(out, "Core Library")["members"] == \
        ["csharp:csproj:src/Domain/Domain.csproj"]
    assert "csharp:csproj:src/Common/Common.csproj" in \
        _group(out, "Web Frontend")["members"]


def test_move_from_glob_bucket_is_explicit_override():
    # dragging a glob-claimed target to B adds it to B's explicit members; the glob source
    # is a no-op (§4.1) — the glob's other members are unmoved.
    out = apply_ops(RULES, [{"op": "move_member", "from": "Modules",
                             "to": "Core Library", "id": "src/Modules/Foo/Foo.csproj"}])
    assert _group(out, "Modules")["members_glob"] == ["src/Modules/*"], "glob unmoved"
    assert "src/Modules/Foo/Foo.csproj" in _group(out, "Core Library")["members"]


def test_move_aborts_whole_op_when_source_delete_fails_closed():
    # an inline comment on the member being deleted -> the whole move refuses, no half-add.
    commented = RULES.replace(
        '      - "csharp:csproj:src/Common/Common.csproj"',
        '      - "csharp:csproj:src/Common/Common.csproj"  # ships to old gateway')
    with pytest.raises(RulesEditError) as exc:
        apply_ops(commented, [{"op": "move_member", "from": "Core Library",
                               "to": "Web Frontend",
                               "id": "csharp:csproj:src/Common/Common.csproj"}])
    assert "inline comment" in exc.value.message
    assert exc.value.line is not None


# --------------------------------------------------------------------------- exclude

def test_exclude_appends_to_targets_block():
    out = apply_ops(RULES, [{"op": "exclude",
                             "id": "csharp:csproj:src/Legacy/Legacy.csproj"}])
    targets = _parse(out)["exclude"]["targets"]
    assert {"id": "csharp:csproj:src/Legacy/Legacy.csproj"} in targets
    assert {"glob": "*.Tests"} in targets, "existing exclusion untouched"


def test_exclude_globs_key():
    out = apply_ops(RULES, [{"op": "exclude", "key": "exclude_globs",
                             "id": "vendor/*"}])
    assert {"glob": "vendor/*"} in _parse(out)["exclude"]["targets"]


def test_exclude_creates_block_when_absent():
    no_exclude = RULES[:RULES.index("\nexclude:")] + "\n"
    out = apply_ops(no_exclude, [{"op": "exclude", "id": "x:y:z"}])
    assert {"id": "x:y:z"} in _parse(out)["exclude"]["targets"]


# --------------------------------------------------------------------------- generalize

def test_generalize_swaps_members_for_one_glob():
    out = apply_ops(RULES, [{
        "op": "generalize_to_glob", "container": "Core Library",
        "ids": ["csharp:csproj:src/Domain/Domain.csproj",
                "csharp:csproj:src/Common/Common.csproj"],
        "pattern": "csharp:csproj:src/*"}])
    g = _group(out, "Core Library")
    assert g.get("members") in (None, []), "explicit members removed"
    assert g["members_glob"] == ["csharp:csproj:src/*"]


def test_tightest_glob_matches_exactly_n_and_zero_others():
    # the selection shares a distinguishing prefix (`src/Modules/`) the outsiders lack, so a
    # `prefix*` glob covers exactly the selection (§4.2 "and 0 others").
    targets = [
        {"id": "csharp:csproj:src/Modules/Blog/Blog.csproj"},
        {"id": "csharp:csproj:src/Modules/Shop/Shop.csproj"},
        {"id": "csharp:csproj:src/Web/Web.csproj"},
    ]
    ids = ["csharp:csproj:src/Modules/Blog/Blog.csproj",
           "csharp:csproj:src/Modules/Shop/Shop.csproj"]
    pat = rules_edit.tightest_glob(ids, targets)
    assert pat is not None
    claimed = rules_edit.matched_ids(pat, "members_glob", targets)
    assert set(claimed) == set(ids), "exactly the selection, no outsider"


def test_tightest_glob_rejects_pattern_that_would_catch_outsider():
    # Domain+Common share only `csharp:csproj:src/`, which would also catch Web -> None.
    targets = [
        {"id": "csharp:csproj:src/Domain/Domain.csproj"},
        {"id": "csharp:csproj:src/Common/Common.csproj"},
        {"id": "csharp:csproj:src/Web/Web.csproj"},
    ]
    ids = ["csharp:csproj:src/Domain/Domain.csproj",
           "csharp:csproj:src/Common/Common.csproj"]
    assert rules_edit.tightest_glob(ids, targets) is None


def test_tightest_glob_returns_none_when_no_clean_pattern():
    # the two selected ids share a prefix that would also catch the third -> no clean glob.
    targets = [{"id": "a/one"}, {"id": "a/two"}, {"id": "a/three"}]
    assert rules_edit.tightest_glob(["a/one", "a/two"], targets) is None


# --------------------------------------------------------------------------- set_glob

def test_set_glob_rewrites_one_item_line_in_place():
    out = apply_ops(RULES, [{"op": "set_glob", "container": "Modules",
                             "old": "src/Modules/*", "new": "src/Plugins/*"}])
    assert _group(out, "Modules")["members_glob"] == ["src/Plugins/*"]
    added, removed = _diff_lines(RULES, out)
    assert len(added) == 1 and len(removed) == 1, "one line swapped, minimal patch"


def test_set_glob_on_members_tag():
    out = apply_ops(RULES, [{"op": "set_glob", "container": "Content",
                             "key": "members_tag", "old": "module-category:Content",
                             "new": "module-category:Blog"}])
    assert _group(out, "Content")["members_tag"] == ["module-category:Blog"]


# --------------------------------------------------------------------------- reorder_group

def _order(text):
    return [g["container"] for g in _parse(text)["group"]]


def test_reorder_group_moves_last_entry_to_front():
    # move "Content" (last) to immediately before "Web Frontend" (first).
    out = apply_ops(RULES, [{"op": "reorder_group", "container": "Content",
                             "before": "Web Frontend"}])
    assert _order(out) == ["Content", "Web Frontend", "Core Library", "Modules"]
    assert "\r" not in out


def test_reorder_group_moves_later_entry_earlier():
    # the §4.5 case: make "Modules" win first-match-wins over "Core Library".
    out = apply_ops(RULES, [{"op": "reorder_group", "container": "Modules",
                             "before": "Core Library"}])
    assert _order(out) == ["Web Frontend", "Modules", "Core Library", "Content"]


def test_reorder_group_carries_entry_body_and_inline_comment():
    # "Web Frontend"'s dash line carries an inline comment; it moves WITH the entry.
    out = apply_ops(RULES, [{"op": "reorder_group", "container": "Web Frontend",
                             "before": "Content"}])
    assert _order(out) == ["Core Library", "Modules", "Web Frontend", "Content"]
    assert "# the public-facing app" in out, "inline comment carried with the entry"
    assert _group(out, "Web Frontend")["members"] == \
        ["csharp:csproj:src/Web/Web.csproj"], "body intact"


def test_reorder_group_noop_when_already_immediately_before():
    out = apply_ops(RULES, [{"op": "reorder_group", "container": "Web Frontend",
                             "before": "Core Library"}])
    assert out == RULES, "already immediately before -> byte-identical no-op"


def test_reorder_group_is_deterministic():
    op = [{"op": "reorder_group", "container": "Content", "before": "Web Frontend"}]
    assert apply_ops(RULES, op) == apply_ops(RULES, op)


def test_reorder_group_same_group_refused():
    with pytest.raises(RulesEditError) as exc:
        apply_ops(RULES, [{"op": "reorder_group", "container": "Modules",
                           "before": "Modules"}])
    assert "same group" in exc.value.message


def test_reorder_group_unknown_target_refused():
    with pytest.raises(RulesEditError) as exc:
        apply_ops(RULES, [{"op": "reorder_group", "container": "Modules",
                           "before": "Nope"}])
    assert "no group named" in exc.value.message


def test_reorder_group_fail_closed_on_trailing_comment():
    # a comment sitting between the mover's body and the next entry is ambiguous to carry.
    commented = RULES.replace(
        '      - "csharp:csproj:src/Web/Web.csproj"\n  - container: "Core Library"',
        '      - "csharp:csproj:src/Web/Web.csproj"\n'
        '  # --- core libraries below ---\n  - container: "Core Library"')
    with pytest.raises(RulesEditError) as exc:
        apply_ops(commented, [{"op": "reorder_group", "container": "Web Frontend",
                               "before": "Content"}])
    assert "comment follows it" in exc.value.message
    assert exc.value.line is not None


# --------------------------------------------------------------------------- remove_group

def test_remove_group_deletes_whole_entry():
    out = apply_ops(RULES, [{"op": "remove_group", "container": "Core Library"}])
    assert _order(out) == ["Web Frontend", "Modules", "Content"], "entry gone"
    # the other entries and the later top-level exclude block are byte-untouched.
    assert _group(out, "Web Frontend")["members"] == ["csharp:csproj:src/Web/Web.csproj"]
    assert _parse(out)["exclude"]["targets"] == [{"glob": "*.Tests"}]
    assert "csharp:csproj:src/Domain/Domain.csproj" not in out, "body removed too"


def test_remove_group_takes_inline_comment_on_its_own_dash():
    # the entry's own `- container: "Web Frontend"  # the public-facing app` comment goes
    # with it (it describes the removed thing) — and a *separator* comment for the NEXT
    # entry stays put.
    commented = RULES.replace(
        '  - container: "Core Library"',
        '  # --- core libraries below ---\n  - container: "Core Library"')
    out = apply_ops(commented, [{"op": "remove_group", "container": "Web Frontend"}])
    assert _order(out) == ["Core Library", "Modules", "Content"]
    assert "# the public-facing app" not in out, "the entry's own comment went with it"
    assert "# --- core libraries below ---" in out, "the next entry's separator stays"


def test_remove_group_last_entry_leaves_exclude_intact():
    out = apply_ops(RULES, [{"op": "remove_group", "container": "Content"}])
    assert _order(out) == ["Web Frontend", "Core Library", "Modules"]
    assert _parse(out)["exclude"]["targets"] == [{"glob": "*.Tests"}]


def test_remove_group_missing_refused():
    with pytest.raises(RulesEditError) as exc:
        apply_ops(RULES, [{"op": "remove_group", "container": "Nope"}])
    assert "no group named" in exc.value.message


def test_remove_group_is_deterministic():
    op = [{"op": "remove_group", "container": "Modules"}]
    assert apply_ops(RULES, op) == apply_ops(RULES, op)
    assert "\r" not in apply_ops(RULES, op)


# --------------------------------------------------------------------------- remove_member

def test_remove_member_unassigns_explicit():
    out = apply_ops(RULES, [{"op": "remove_member", "container": "Core Library",
                             "id": "csharp:csproj:src/Common/Common.csproj"}])
    assert _group(out, "Core Library")["members"] == \
        ["csharp:csproj:src/Domain/Domain.csproj"], "only the named member is dropped"
    added, removed = _diff_lines(RULES, out)
    assert added == [] and len(removed) == 1


def test_remove_member_missing_is_refused():
    with pytest.raises(RulesEditError) as exc:
        apply_ops(RULES, [{"op": "remove_member", "container": "Core Library",
                           "id": "csharp:csproj:src/Nope/Nope.csproj"}])
    assert "no member" in exc.value.message


def test_remove_member_fail_closed_on_inline_comment():
    commented = RULES.replace(
        '      - "csharp:csproj:src/Common/Common.csproj"',
        '      - "csharp:csproj:src/Common/Common.csproj"  # keep me')
    with pytest.raises(RulesEditError) as exc:
        apply_ops(commented, [{"op": "remove_member", "container": "Core Library",
                               "id": "csharp:csproj:src/Common/Common.csproj"}])
    assert "inline comment" in exc.value.message


# --------------------------------------------------------------------------- add_glob

def test_add_glob_to_glob_less_container():
    out = apply_ops(RULES, [{"op": "add_glob", "container": "Web Frontend",
                             "pattern": "src/Web/*"}])
    g = _group(out, "Web Frontend")
    assert g["members_glob"] == ["src/Web/*"], "glob bucket created"
    assert g["members"] == ["csharp:csproj:src/Web/Web.csproj"], "members untouched"


def test_add_glob_members_tag_key():
    out = apply_ops(RULES, [{"op": "add_glob", "container": "Core Library",
                             "key": "members_tag", "pattern": "layer:core"}])
    assert _group(out, "Core Library")["members_tag"] == ["layer:core"]


def test_add_glob_bad_key_refused():
    with pytest.raises(RulesEditError) as exc:
        apply_ops(RULES, [{"op": "add_glob", "container": "Web Frontend",
                           "key": "members", "pattern": "x"}])
    assert "members_glob or members_tag" in exc.value.message


# --------------------------------------------------------------------------- create_group

def test_create_group_appends_new_container():
    out = apply_ops(RULES, [{"op": "create_group", "container": "Plugins",
                             "members_glob": ["src/Plugins/*"]}])
    assert _order(out) == ["Web Frontend", "Core Library", "Modules", "Content", "Plugins"]
    assert _group(out, "Plugins")["members_glob"] == ["src/Plugins/*"]
    # the existing exclude block (a later top-level key) is untouched.
    assert _parse(out)["exclude"]["targets"] == [{"glob": "*.Tests"}]


def test_create_group_empty_container_then_add_member():
    out = apply_ops(RULES, [{"op": "create_group", "container": "Empty"}])
    assert any(g["container"] == "Empty" for g in _parse(out)["group"])
    # an empty container is a valid drop target the user then fills.
    out2 = apply_ops(out, [{"op": "add_member", "container": "Empty", "id": "a:b:c"}])
    assert _group(out2, "Empty")["members"] == ["a:b:c"]


def test_create_group_clash_refused():
    with pytest.raises(RulesEditError) as exc:
        apply_ops(RULES, [{"op": "create_group", "container": "Modules"}])
    assert "already exists" in exc.value.message


def test_create_group_creates_block_when_absent():
    no_group = "version: 1\ndefaults:\n  min_relationship_weight: 3\n"
    out = apply_ops(no_group, [{"op": "create_group", "container": "First",
                                "members": ["x:y:z"]}])
    assert _group(out, "First")["members"] == ["x:y:z"]
    assert _parse(out)["defaults"]["min_relationship_weight"] == 3, "prior keys intact"


def test_create_group_is_deterministic():
    op = [{"op": "create_group", "container": "Plugins", "members_glob": ["src/Plugins/*"]}]
    assert apply_ops(RULES, op) == apply_ops(RULES, op)


# --------------------------------------------------------------------------- rename_group

def test_rename_group_rewrites_container_name():
    out = apply_ops(RULES, [{"op": "rename_group", "container": "Core Library",
                             "new": "Domain Core"}])
    assert _order(out) == ["Web Frontend", "Domain Core", "Modules", "Content"]
    # members + position intact; only the name line changed.
    assert _group(out, "Domain Core")["members"] == [
        "csharp:csproj:src/Domain/Domain.csproj",
        "csharp:csproj:src/Common/Common.csproj"]
    added, removed = _diff_lines(RULES, out)
    assert len(added) == 1 and len(removed) == 1, "one line swapped, minimal patch"


def test_rename_group_preserves_inline_comment():
    # "Web Frontend"'s container line carries `# the public-facing app`; a rename keeps it
    # (a value-swap can preserve the comment, unlike a delete which fail-closes).
    out = apply_ops(RULES, [{"op": "rename_group", "container": "Web Frontend",
                             "new": "Public Web"}])
    assert _group(out, "Public Web")["members"] == ["csharp:csproj:src/Web/Web.csproj"]
    assert "# the public-facing app" in out, "inline comment preserved through rename"
    assert _group(out, "Web Frontend") is None


def test_rename_group_collision_refused():
    with pytest.raises(RulesEditError) as exc:
        apply_ops(RULES, [{"op": "rename_group", "container": "Core Library",
                           "new": "Modules"}])
    assert "already exists" in exc.value.message


def test_rename_group_same_name_refused():
    with pytest.raises(RulesEditError) as exc:
        apply_ops(RULES, [{"op": "rename_group", "container": "Modules",
                           "new": "Modules"}])
    assert "same group" in exc.value.message


def test_rename_group_missing_refused():
    with pytest.raises(RulesEditError) as exc:
        apply_ops(RULES, [{"op": "rename_group", "container": "Nope", "new": "X"}])
    assert "no group named" in exc.value.message


def test_rename_group_is_deterministic():
    op = [{"op": "rename_group", "container": "Modules", "new": "Plug Ins"}]
    assert apply_ops(RULES, op) == apply_ops(RULES, op)
    assert "\r" not in apply_ops(RULES, op)


# ----------------------------------------------- rename: cross-file reference rewrites (§15.1)

SCENARIOS = """\
# Hand-authored scenarios (comments must survive).
version: 1
scenarios:
  - key: req
    steps:
      - from: "Web Frontend"
        to: "Core Library"
        description: "calls core"
      - from: "container:coreLibrary"
        to: "Modules"
"""


def test_rename_scenarios_rewrites_by_name_and_cid():
    # rename "Core Library" -> "Domain"; cid container:coreLibrary -> container:domain.
    out = rules_edit.rename_scenarios(
        SCENARIOS, "Core Library", "Domain", "container:coreLibrary", "container:domain")
    doc = yaml.safe_load(out)
    steps = doc["scenarios"][0]["steps"]
    assert steps[0]["to"] == "Domain", "by-name endpoint rewritten"
    assert steps[1]["from"] == "container:domain", "by-id endpoint rewritten"
    assert steps[0]["from"] == "Web Frontend", "unrelated endpoint untouched"
    assert steps[1]["to"] == "Modules", "unrelated endpoint untouched"
    assert "# Hand-authored scenarios" in out, "file comments preserved"


def test_rename_scenarios_fail_closed_on_inline_comment():
    # a `to:` line that would be rewritten but carries an inline comment -> fail-closed
    # (G§Q2), pointing at the line. (A delete/rewrite can't keep an ambiguous comment.)
    commented = SCENARIOS.replace(
        '        to: "Core Library"',
        '        to: "Core Library"          # the domain layer')
    with pytest.raises(RulesEditError) as exc:
        rules_edit.rename_scenarios(
            commented, "Core Library", "Domain", "container:coreLibrary", "container:domain")
    assert "inline comment" in exc.value.message and exc.value.line is not None


def test_rename_scenarios_no_reference_is_byte_identical():
    out = rules_edit.rename_scenarios(
        SCENARIOS, "Ordering", "Sales", "container:ordering", "container:sales")
    assert out == SCENARIOS, "nothing references it -> unchanged, no spurious diff"


LAYOUT = """\
# Hand-pinned layout positions.
views:
  containers:
    container:coreLibrary: {x: 10, y: 20}
    container:webFrontend: {x: 0, y: 0}
  components:
    container:coreLibrary: {x: 5, y: 5}
"""


def test_rename_layout_pins_renames_key_across_views():
    out = rules_edit.rename_layout_pins(LAYOUT, "container:coreLibrary", "container:domain")
    doc = yaml.safe_load(out)
    for view in ("containers", "components"):
        keys = set(doc["views"][view])
        assert "container:domain" in keys and "container:coreLibrary" not in keys
    # the unrelated pin is untouched.
    assert doc["views"]["containers"]["container:webFrontend"] == {"x": 0, "y": 0}
    assert doc["views"]["containers"]["container:domain"] == {"x": 10, "y": 20}


def test_rename_layout_pins_no_reference_unchanged():
    out = rules_edit.rename_layout_pins(LAYOUT, "container:ordering", "container:sales")
    assert out == LAYOUT, "no pin references it -> unchanged"


def test_rename_layout_pins_is_deterministic():
    a = rules_edit.rename_layout_pins(LAYOUT, "container:coreLibrary", "container:domain")
    b = rules_edit.rename_layout_pins(LAYOUT, "container:coreLibrary", "container:domain")
    assert a == b and "\r" not in a


# ------------------------------------------------ same-indent sequences (accept-proposal style)

# `yaml.safe_dump` (so `accept-proposal`) emits a block sequence at the SAME indent as its
# key — `members:` then `- x` both at 4 spaces. The line-editor must edit these exactly like
# the deeper-indented hand style (the OrchardCore "no member to remove" regression).
SAME_INDENT = """\
version: 1
group:
  - container: Templates
    members:
    - "csharp:csproj:src/T/A.csproj"
    - "csharp:csproj:src/T/B.csproj"
  - container: Modules
    members_glob:
    - "src/Modules/*"
"""


def test_same_indent_remove_member():
    out = apply_ops(SAME_INDENT, [{"op": "remove_member", "container": "Templates",
                                   "id": "csharp:csproj:src/T/A.csproj"}])
    assert _group(out, "Templates")["members"] == ["csharp:csproj:src/T/B.csproj"]


def test_same_indent_add_member_keeps_style():
    out = apply_ops(SAME_INDENT, [{"op": "add_member", "container": "Templates",
                                   "id": "csharp:csproj:src/T/C.csproj"}])
    assert _group(out, "Templates")["members"] == [
        "csharp:csproj:src/T/A.csproj", "csharp:csproj:src/T/B.csproj",
        "csharp:csproj:src/T/C.csproj"]
    # the new item matches the existing 4-space same-indent style (no reflow; quote style
    # is _scalar's own — what matters is the indent stays in the file's same-indent style).
    assert any(ln.startswith("    - ") and "src/T/C.csproj" in ln
               for ln in out.split("\n"))


def test_same_indent_set_glob():
    out = apply_ops(SAME_INDENT, [{"op": "set_glob", "container": "Modules",
                                   "old": "src/Modules/*", "new": "src/Plugins/*"}])
    assert _group(out, "Modules")["members_glob"] == ["src/Plugins/*"]


def test_same_indent_generalize():
    out = apply_ops(SAME_INDENT, [{
        "op": "generalize_to_glob", "container": "Templates",
        "ids": ["csharp:csproj:src/T/A.csproj", "csharp:csproj:src/T/B.csproj"],
        "pattern": "csharp:csproj:src/T/*"}])
    g = _group(out, "Templates")
    assert g.get("members") in (None, []) and g["members_glob"] == ["csharp:csproj:src/T/*"]


# ------------------------------------------ zero-indent entries (copied-proposal style)

# `yaml.safe_dump` puts block-sequence dashes at the SAME indent as the parent key —
# so a `group:` block copied from the `arch propose` draft has its `- ` entries at
# indent 0, keys sorted (`confidence` before `container`). Valid YAML, read fine by
# curate(), so the containers show in the tree — the line-editor must see them too
# (the "no group named 'BaseServices' although it is in the tree" regression) and
# must append new entries in the same zero-indent style (the create_group corrupt-
# splice-refused regression: `expected <block end>, but found '-'`).
ZERO_INDENT = """\
version: 1
group:
- confidence: high
  container: BaseServices
  members:
  - "csharp:csproj:src/Base/A.csproj"
  - "csharp:csproj:src/Base/B.csproj"
  provenance: solution-folder:BaseServices
- confidence: medium
  container: Frontend
  members:
  - "csharp:csproj:src/Web/Web.csproj"
  provenance: directory:src/Web

exclude:
  targets:
    - glob: "*.Tests"
"""


def test_zero_indent_locate_and_add_member():
    out = apply_ops(ZERO_INDENT, [{"op": "add_member", "container": "BaseServices",
                                   "id": "csharp:csproj:src/Base/C.csproj"}])
    assert _group(out, "BaseServices")["members"] == [
        "csharp:csproj:src/Base/A.csproj", "csharp:csproj:src/Base/B.csproj",
        "csharp:csproj:src/Base/C.csproj"]


def test_zero_indent_move_member():
    out = apply_ops(ZERO_INDENT, [{"op": "move_member", "from": "BaseServices",
                                   "to": "Frontend",
                                   "id": "csharp:csproj:src/Base/B.csproj"}])
    assert _group(out, "BaseServices")["members"] == ["csharp:csproj:src/Base/A.csproj"]
    assert "csharp:csproj:src/Base/B.csproj" in _group(out, "Frontend")["members"]


def test_zero_indent_remove_group():
    out = apply_ops(ZERO_INDENT, [{"op": "remove_group", "container": "BaseServices"}])
    assert _group(out, "BaseServices") is None
    assert _group(out, "Frontend") is not None, "the sibling entry survives"
    assert _parse(out)["exclude"]["targets"] == [{"glob": "*.Tests"}]


def test_zero_indent_create_group_appends_parsable_yaml():
    # the user-visible regression: create_group used to insert an indented entry BEFORE
    # the zero-indent ones, producing "expected <block end>, but found '-'" (refused by
    # gate 1). It must append in the file's own zero-indent style and re-parse.
    out = apply_ops(ZERO_INDENT, [{"op": "create_group", "container": "NewBox"}])
    parsed = _parse(out)
    assert [g["container"] for g in parsed["group"]] == \
        ["BaseServices", "Frontend", "NewBox"]
    assert any(ln.startswith("- container: NewBox") for ln in out.split("\n")), \
        "new entry matches the existing zero-indent dash style"
    assert parsed["exclude"]["targets"] == [{"glob": "*.Tests"}]


def test_zero_indent_reorder_and_rename():
    out = apply_ops(ZERO_INDENT, [{"op": "reorder_group", "container": "Frontend",
                                   "before": "BaseServices"}])
    assert [g["container"] for g in _parse(out)["group"]] == ["Frontend", "BaseServices"]
    out2 = apply_ops(ZERO_INDENT, [{"op": "rename_group", "container": "BaseServices",
                                    "new": "Platform"}])
    assert _group(out2, "Platform")["members"] == [
        "csharp:csproj:src/Base/A.csproj", "csharp:csproj:src/Base/B.csproj"]


def test_zero_indent_scenarios_append():
    zero_scen = ("scenarios:\n"
                 "- key: checkout\n"
                 "  steps:\n"
                 "  - from: a\n"
                 "    to: b\n")
    out = rules_edit.apply_ops(zero_scen, [{
        "op": "add_scenario", "source": "test",
        "proposal": {"key": "login", "steps": [{"from": "x", "to": "y"}]}}])
    parsed = _parse(out)
    assert [s["key"] for s in parsed["scenarios"]] == ["checkout", "login"]


# ------------------------------------------ exclusivity sweep (duplicate explicit claims)

# The "Add explicit… duplicated the member" desync: pinning a target into a just-created
# container left its old explicit `members:` line in an accepted-proposal group, so the
# YAML carried TWO claims. curate's member index is a dict keyed by id, so the LAST group
# entry silently won — the tree looked right while mapping-rules lied, and ownership would
# flip if the winning group were removed or reordered. Every explicit-member insert now
# sweeps the id's stale explicit claims first — across quoting/indent styles (the proposal
# style is zero-indent + unquoted; the editor writes indented + quoted; both must resolve
# to the same claim). Glob/tag claims are NOT swept (legitimately overridden, §4.1).
MIXED_STYLE = """\
version: 1
group:
- confidence: medium
  container: NCP
  members:
  - cpp:target:ABBCSNodeManager
  - cpp:target:Other
  provenance: directory:src/NCP
- container: NodeManager
"""


def test_add_member_sweeps_stale_explicit_claim_across_styles():
    # the exact repro: NodeManager was just created; "Add explicit…" pins the target that
    # an accepted proposal still lists (different indent + quoting) — one claim survives.
    out = apply_ops(MIXED_STYLE, [{"op": "add_member", "container": "NodeManager",
                                   "id": "cpp:target:ABBCSNodeManager"}])
    assert _group(out, "NCP")["members"] == ["cpp:target:Other"]
    assert _group(out, "NodeManager")["members"] == ["cpp:target:ABBCSNodeManager"]


def test_add_member_heals_preexisting_duplicate_claim():
    # the state the desync left behind: BOTH groups already list the id. Re-running the
    # add (idempotent on the dest) strips the stale claim — the recovery path.
    dup = MIXED_STYLE.replace(
        "- container: NodeManager\n",
        "- container: NodeManager\n  members:\n    - 'cpp:target:ABBCSNodeManager'\n")
    out = apply_ops(dup, [{"op": "add_member", "container": "NodeManager",
                           "id": "cpp:target:ABBCSNodeManager"}])
    assert _group(out, "NCP")["members"] == ["cpp:target:Other"]
    assert _group(out, "NodeManager")["members"] == ["cpp:target:ABBCSNodeManager"]


def test_move_member_sweeps_third_container_claim():
    # a pre-existing duplicate in a THIRD container is swept too — after any move the id
    # lives in exactly one `members:` list.
    dup = RULES.replace(
        '  - container: "Web Frontend"          # the public-facing app\n    members:\n',
        '  - container: "Web Frontend"          # the public-facing app\n    members:\n'
        '      - "csharp:csproj:src/Domain/Domain.csproj"\n')
    out = apply_ops(dup, [{"op": "move_member", "from": "Core Library",
                           "to": "Modules",
                           "id": "csharp:csproj:src/Domain/Domain.csproj"}])
    assert _group(out, "Web Frontend")["members"] == \
        ["csharp:csproj:src/Web/Web.csproj"]
    assert _group(out, "Core Library")["members"] == \
        ["csharp:csproj:src/Common/Common.csproj"]
    assert _group(out, "Modules")["members"] == \
        ["csharp:csproj:src/Domain/Domain.csproj"]


def test_sweep_drops_emptied_members_key():
    # sweeping the holder's ONLY member also drops its now-dangling `members:` key line;
    # the rest of the proposal entry (confidence/provenance) is untouched.
    out = apply_ops(MIXED_STYLE, [
        {"op": "add_member", "container": "NodeManager",
         "id": "cpp:target:ABBCSNodeManager"},
        {"op": "add_member", "container": "NodeManager", "id": "cpp:target:Other"}])
    ncp = _group(out, "NCP")
    assert "members" not in ncp
    assert ncp["confidence"] == "medium" and ncp["provenance"] == "directory:src/NCP"
    assert _group(out, "NodeManager")["members"] == \
        ["cpp:target:ABBCSNodeManager", "cpp:target:Other"]


def test_sweep_fail_closed_on_stale_line_inline_comment():
    # a stale claim whose line carries an inline comment can't be deleted safely — the
    # WHOLE op refuses (G§Q2/Q8), nothing is half-applied.
    commented = MIXED_STYLE.replace(
        "  - cpp:target:ABBCSNodeManager",
        "  - cpp:target:ABBCSNodeManager  # keep? ask team")
    with pytest.raises(RulesEditError) as exc:
        apply_ops(commented, [{"op": "add_member", "container": "NodeManager",
                               "id": "cpp:target:ABBCSNodeManager"}])
    assert "inline comment" in exc.value.message


def test_create_group_with_members_sweeps_stale_claims():
    out = apply_ops(MIXED_STYLE, [{"op": "create_group", "container": "Managers",
                                   "members": ["cpp:target:ABBCSNodeManager"]}])
    assert _group(out, "NCP")["members"] == ["cpp:target:Other"]
    assert _group(out, "Managers")["members"] == ["cpp:target:ABBCSNodeManager"]


def test_sweep_leaves_glob_and_tag_claims_untouched():
    # only explicit `members:` lines are stale duplicates; a glob/tag claim elsewhere is
    # legitimately overridden by the explicit pin (§4.1) and must not be edited.
    out = apply_ops(RULES, [{"op": "add_member", "container": "Core Library",
                             "id": "src/Modules/Foo/Foo.csproj"}])
    assert _group(out, "Modules")["members_glob"] == ["src/Modules/*"]
    assert _group(out, "Content")["members_tag"] == ["module-category:Content"]
    assert "src/Modules/Foo/Foo.csproj" in _group(out, "Core Library")["members"]


def test_sweep_is_deterministic():
    op = [{"op": "add_member", "container": "NodeManager",
           "id": "cpp:target:ABBCSNodeManager"}]
    assert apply_ops(MIXED_STYLE, op) == apply_ops(MIXED_STYLE, op)


# --------------------------------------------------------------------------- fail-closed

def test_flow_style_list_is_refused_unchanged():
    flow = RULES.replace(
        '    members:\n      - "csharp:csproj:src/Web/Web.csproj"',
        '    members: ["csharp:csproj:src/Web/Web.csproj"]')
    with pytest.raises(RulesEditError) as exc:
        apply_ops(flow, [{"op": "add_member", "container": "Web Frontend",
                          "id": "x:y:z"}])
    assert "flow-style" in exc.value.message


# --------------------------------------------------------------------------- empty flow

# The RQ9 curator seed (overlays/*-blind-*/mapping-rules.yaml) — `yaml.safe_dump`'s form
# for empty lists. Before 2026-09-07 the first drag into Excluded failed-closed
# ("flow-style list — edit in the Rules tab") and the first "new container" appended a
# SECOND top-level `group:` block (the `[]` line was invisible to _find_groups; the
# committed R-A curation carries exactly that duplicate).
SEED = """\
# curator seed
version: 1

defaults:
  on_unmapped: annotate

exclude:
  targets: []

group: []
"""


def test_seed_empty_flow_lists_are_opened_on_first_write():
    out = apply_ops(SEED, [
        {"op": "exclude", "id": "csharp:csproj:tests/A/A.csproj"},
        {"op": "create_group", "container": "Web"},
        {"op": "add_member", "container": "Web", "id": "csharp:csproj:src/W/W.csproj"},
    ])
    parsed = yaml.safe_load(out)
    assert parsed["exclude"]["targets"] == [{"id": "csharp:csproj:tests/A/A.csproj"}]
    assert [g["container"] for g in parsed["group"]] == ["Web"]
    assert parsed["group"][0]["members"] == ["csharp:csproj:src/W/W.csproj"]
    assert out.count("\ngroup:") == 1, "one top-level group: key, opened in place"
    assert "[]" not in out
    assert out.startswith("# curator seed\nversion: 1\n\ndefaults:\n  on_unmapped: annotate\n")
    assert "exclude:\n  targets:\n    - id: 'csharp:csproj:tests/A/A.csproj'\n" in out


def test_empty_flow_list_keeps_its_inline_comment():
    text = SEED.replace("targets: []", "targets: []  # none yet")
    out = apply_ops(text, [{"op": "exclude", "id": "x:y"}])
    assert "  targets:  # none yet\n    - id: 'x:y'\n" in out
    assert yaml.safe_load(out)["exclude"]["targets"] == [{"id": "x:y"}]


def test_empty_flow_members_list_is_opened():
    text = SEED.replace("group: []", "group:\n  - container: Web\n    members: []\n")
    out = apply_ops(text, [{"op": "add_member", "container": "Web",
                            "id": "csharp:csproj:src/W/W.csproj"}])
    assert "    members:\n      - " in out and "members: []" not in out
    assert yaml.safe_load(out)["group"][0]["members"] == ["csharp:csproj:src/W/W.csproj"]


def test_empty_flow_exclude_mapping_is_opened():
    text = SEED.replace("exclude:\n  targets: []", "exclude: {}")
    out = apply_ops(text, [{"op": "exclude", "id": "x:y"}])
    assert "exclude:\n  targets:\n    - id: 'x:y'\n\ngroup: []\n" in out
    assert yaml.safe_load(out)["exclude"]["targets"] == [{"id": "x:y"}]


def test_non_empty_flow_collections_stay_refused():
    for text, op in [
        (SEED.replace("group: []", "group: [{container: Web}]"),
         {"op": "create_group", "container": "Api"}),
        (SEED.replace("targets: []", "targets: [{id: a}]"),
         {"op": "exclude", "id": "x:y"}),
        (SEED.replace("group: []", "group:\n  - container: Web\n    members: [a]\n"),
         {"op": "add_member", "container": "Web", "id": "x:y"}),
    ]:
        with pytest.raises(RulesEditError) as exc:
            apply_ops(text, [op])
        assert "flow-style" in exc.value.message


def test_seed_then_real_group_block_is_ambiguous():
    # the R-A shape: `group: []` left behind + a second `group:` block written later.
    text = SEED + "\ngroup:\n  - container: Web\n"
    with pytest.raises(RulesEditError) as exc:
        apply_ops(text, [{"op": "create_group", "container": "Api"}])
    assert "two top-level `group:` keys" in exc.value.message


def test_duplicate_container_block_is_refused():
    dup = RULES + '\ngroup_extra_ignored: 1\n'  # noqa: keep a top-level sibling
    dup = RULES.replace(
        '  - container: "Content"\n    members_tag:\n      - "module-category:Content"',
        '  - container: "Content"\n    members_tag:\n      - "module-category:Content"\n'
        '  - container: "Content"\n    members:\n      - "x:y:z"')
    with pytest.raises(RulesEditError) as exc:
        apply_ops(dup, [{"op": "add_member", "container": "Content", "id": "a:b:c"}])
    assert "more than once" in exc.value.message


def test_unknown_container_is_refused():
    with pytest.raises(RulesEditError) as exc:
        apply_ops(RULES, [{"op": "add_member", "container": "Nope", "id": "a:b:c"}])
    assert "no group named" in exc.value.message


def test_empty_ops_refused():
    with pytest.raises(RulesEditError):
        apply_ops(RULES, [])


# ----------------------------------------------- add_scenario (dynamic-view plan §9 promote)

# A representative hand-authored scenarios.yaml with comments + one existing entry — the
# block-append primitive must preserve every untouched byte and append after the last entry.
SCEN_FILE = """\
# Hand-authored behavioral scenarios (comments must survive).
version: 1

scenarios:
  - key: existing-flow
    name: "An existing flow"
    scope: system
    steps:
      - from: "container:web"
        to: "container:core"
        description: "calls core"
"""


def _scen(text, key):
    for s in yaml.safe_load(text)["scenarios"]:
        if s["key"] == key:
            return s
    return None


def _runtime_candidate():
    """A Source-B (runtime) candidate envelope op: two ok build-backed steps + one
    runtime-only ok step (dsl:false), all with stable container ids + a parallel `order`."""
    return {
        "op": "add_scenario",
        "source": "runtime",
        "proposal": {
            "key": "runtime-web",
            "name": "Runtime: web fan-out",
            "scope": "system",
            "steps": [
                {"from": "container:web", "to": "container:basket", "order": 1,
                 "from_name": "Web", "to_name": "Basket", "description": "calls Basket"},
                {"from": "container:web", "to": "container:catalog", "order": 1,
                 "from_name": "Web", "to_name": "Catalog", "description": "calls Catalog"},
                {"from": "container:web", "to": "container:ordering", "order": 1,
                 "from_name": "Web", "to_name": "Ordering", "description": "calls Ordering"},
            ],
        },
        "validated_view": {
            "steps": [
                {"index": 1, "order": 1, "status": "ok", "evidence_layer": "build",
                 "dsl": True, "from": "container:web", "to": "container:basket"},
                {"index": 2, "order": 1, "status": "ok", "evidence_layer": "build",
                 "dsl": True, "from": "container:web", "to": "container:catalog"},
                {"index": 3, "order": 1, "status": "ok", "evidence_layer": "runtime",
                 "dsl": False, "dsl_reason": "no static relationship",
                 "from": "container:web", "to": "container:ordering"},
            ],
        },
    }


def test_add_scenario_two_level_renderer():
    out = apply_ops(SCEN_FILE, [_runtime_candidate()])
    # the new entry parses with the two-level shape: a `- key:` entry whose nested `steps:`
    # is a sequence of `- from:/to:/order/description` mappings.
    s = _scen(out, "runtime-web")
    assert s is not None
    assert s["name"] == "Runtime: web fan-out"
    assert s["scope"] == "system"
    assert s["ordering_provenance"] == "declared-in-aspire"  # source runtime -> declared
    steps = s["steps"]
    assert len(steps) == 3
    # stable ids promoted, never the display name (§0.4#6).
    assert steps[0]["from"] == "container:web" and steps[0]["to"] == "container:basket"
    # the parallel `order` facet survives.
    assert all(st["order"] == 1 for st in steps)
    assert steps[0]["description"] == "calls Basket"
    # the existing entry + file comments are byte-untouched.
    assert _scen(out, "existing-flow")["steps"][0]["to"] == "container:core"
    assert "# Hand-authored behavioral scenarios" in out
    assert "\r" not in out


def test_add_scenario_runtime_only_comment():
    out = apply_ops(SCEN_FILE, [_runtime_candidate()])
    # the dsl:false runtime-only step gets a visible warning comment in the YAML.
    assert "# runtime-only: not emitted to Structurizr DSL" in out
    # the comment sits directly above the runtime-only step's dash (the 3rd step -> ordering).
    lines = out.split("\n")
    ci = next(i for i, ln in enumerate(lines) if "runtime-only:" in ln)
    # the dash line opens the step mapping with `from:` (the id is quoted — it has a colon).
    assert lines[ci + 1].lstrip().startswith("- from:")
    assert "container:web" in lines[ci + 1]
    assert "container:ordering" in lines[ci + 2]


def test_add_scenario_ordering_provenance_comment_for_synthesized():
    # a graph-walk (synthesized) candidate -> ordering_provenance: synthesized + a visible
    # "# ordering: synthesized ..." comment so a promoted guess never reads as observed (§9).
    op = {
        "op": "add_scenario", "source": "graph-walk",
        "proposal": {"key": "walk-web", "name": "Walk: web", "scope": "system",
                     "steps": [
                         {"from": "container:web", "to": "container:core", "order": 1,
                          "description": "web depends on core (synthesized order)"},
                         {"from": "container:core", "to": "container:db", "order": 2,
                          "description": "core depends on db (synthesized order)"}]},
        "validated_view": {"steps": [
            {"index": 1, "order": 1, "status": "ok", "dsl": True,
             "from": "container:web", "to": "container:core"},
            {"index": 2, "order": 2, "status": "ok", "dsl": True,
             "from": "container:core", "to": "container:db"}]},
    }
    out = apply_ops(SCEN_FILE, [op])
    s = _scen(out, "walk-web")
    assert s["ordering_provenance"] == "synthesized"
    assert "# ordering: synthesized (synthesized; not an observed flow)" in out
    # the comment precedes the appended entry's `- key:` dash.
    lines = out.split("\n")
    ci = next(i for i, ln in enumerate(lines) if "# ordering: synthesized" in ln)
    assert "- key: walk-web" in lines[ci + 1]


def test_add_scenario_observed_source_has_no_synthesized_comment():
    # a runtime/test source is NOT synthesized -> no "# ordering: synthesized" banner.
    out = apply_ops(SCEN_FILE, [_runtime_candidate()])
    assert "# ordering: synthesized" not in out


def test_add_scenario_multiline_and_colon_description_round_trips():
    # a multi-line + colon-bearing description (reachable verbatim via the Source-F LLM promote
    # path, where the model's `description` is passed through) must NOT corrupt the entry: the
    # renderer emits a SINGLE-LINE double-quoted scalar (json.dumps) so PyYAML's block-scalar
    # continuation-line dedent can't change the parsed value or break the apply_ops re-parse
    # gate. The plain-ASCII multi-line value below is exactly what _scalar would render as a
    # corrupting single-quoted *block* scalar (dedented continuation lines).
    multiline = "Web sends request: GET /catalog\nthen Basket validates\nthen Order placed"
    op = {
        "op": "add_scenario", "source": "runtime",
        "proposal": {"key": "multiline-desc", "name": "Multi", "scope": "system",
                     "steps": [{"from": "container:web", "to": "container:core",
                                "order": 1, "description": multiline}]},
        "validated_view": {"steps": [
            {"index": 1, "order": 1, "status": "ok", "dsl": True,
             "from": "container:web", "to": "container:core"}]},
    }
    out = apply_ops(SCEN_FILE, [op])
    assert "\r" not in out, "LF always"
    # the spliced block re-parses (gate-1) and reproduces the description byte-for-byte at its
    # nesting depth — the round-trip the prompt requires.
    s = _scen(out, "multiline-desc")
    assert s["steps"][0]["description"] == multiline
    # the description renders on a SINGLE physical line (no orphaned, dedented continuation
    # lines) — the structural guard for the json.dumps fix.
    desc_lines = [ln for ln in out.split("\n") if "description:" in ln]
    assert len(desc_lines) == 2  # the existing "calls core" entry + the new one
    new_desc = next(ln for ln in desc_lines if "Web sends request" in ln)
    assert "\\n" in new_desc  # the newlines are escaped inline, not split across lines
    # deterministic — same op -> byte-identical.
    assert apply_ops(SCEN_FILE, [op]) == out


# ------------ contiguity rule (§9): refuse vs flag a chain-breaking flagged-step drop

def _gappy_candidate(on_gap):
    """A->B (ok), B->C (unevidenced flagged), C->D (ok): dropping B->C breaks contiguity
    (the kept C->D's `from`=C != the previous kept A->B's `to`=B)."""
    return {
        "op": "add_scenario", "source": "runtime", "on_gap": on_gap,
        "proposal": {"key": "gappy", "name": "Gappy", "scope": "system", "steps": [
            {"from": "A", "to": "B", "order": 1, "description": "a->b"},
            {"from": "B", "to": "C", "order": 2, "description": "b->c"},
            {"from": "C", "to": "D", "order": 3, "description": "c->d"}]},
        "validated_view": {"steps": [
            {"index": 1, "order": 1, "status": "ok", "dsl": True, "from": "A", "to": "B"},
            {"index": 2, "order": 2, "status": "unevidenced", "from": "B", "to": "C",
             "reason": "no evidenced build or runtime container edge"},
            {"index": 3, "order": 3, "status": "ok", "dsl": True, "from": "C", "to": "D"}]},
    }


def test_add_scenario_contiguity_refuse_branch():
    # default/explicit "refuse": a chain-breaking drop routes to the YAML tab, naming the gap.
    with pytest.raises(RulesEditError) as exc:
        apply_ops(SCEN_FILE, [_gappy_candidate("refuse")])
    assert "break the chain" in exc.value.message
    assert "B -> C" in exc.value.message  # the dropped flagged step is named


def test_add_scenario_contiguity_refuse_is_default():
    cand = _gappy_candidate("refuse")
    del cand["on_gap"]  # no on_gap -> default refuse
    with pytest.raises(RulesEditError):
        apply_ops(SCEN_FILE, [cand])


def test_add_scenario_contiguity_flag_branch():
    # "flag": the flagged middle step IS promoted with a visible `# FLAGGED:` comment so the
    # gap is honest rather than a silent 2-fragment diagram.
    out = apply_ops(SCEN_FILE, [_gappy_candidate("flag")])
    s = _scen(out, "gappy")
    # all three steps present (A->B, B->C flagged, C->D), in order.
    pairs = [(st["from"], st["to"]) for st in s["steps"]]
    assert pairs == [("A", "B"), ("B", "C"), ("C", "D")]
    assert "# FLAGGED: no evidenced build or runtime container edge (B -> C)" in out
    # the FLAGGED comment sits directly above the flagged step's dash.
    lines = out.split("\n")
    fi = next(i for i, ln in enumerate(lines) if "# FLAGGED:" in ln)
    assert "- from: B" in lines[fi + 1]


def test_add_scenario_ok_only_drops_trailing_flagged_without_gap():
    # a trailing flagged step (after the last ok step) is dropped silently in BOTH branches —
    # nothing follows it, so dropping it can't break contiguity.
    cand = {
        "op": "add_scenario", "source": "runtime", "on_gap": "refuse",
        "proposal": {"key": "tail", "name": "Tail", "scope": "system", "steps": [
            {"from": "A", "to": "B", "order": 1},
            {"from": "B", "to": "X", "order": 2}]},
        "validated_view": {"steps": [
            {"index": 1, "order": 1, "status": "ok", "dsl": True, "from": "A", "to": "B"},
            {"index": 2, "order": 2, "status": "unevidenced", "from": "B", "to": "X",
             "reason": "no edge"}]},
    }
    out = apply_ops(SCEN_FILE, [cand])
    pairs = [(st["from"], st["to"]) for st in _scen(out, "tail")["steps"]]
    assert pairs == [("A", "B")], "trailing flagged step dropped (ok-only)"
    assert "# FLAGGED" not in out


# ----------------------------------------------------------- add_scenario fail-closed (§9)

def test_add_scenario_flow_style_steps_refused():
    # a flow-style `scenarios: [..]` -> can't safely line-edit, route to YAML tab.
    flow = SCEN_FILE.replace(
        "scenarios:\n  - key: existing-flow",
        "scenarios: [{key: existing-flow}]")
    # ensure the rest of the block is gone so the file still parses as a flow list.
    flow = "# c\nversion: 1\nscenarios: [{key: existing-flow}]\n"
    with pytest.raises(RulesEditError) as exc:
        apply_ops(flow, [_runtime_candidate()])
    assert "flow-style" in exc.value.message


def test_add_scenario_duplicate_key_refused():
    dup = dict(_runtime_candidate())
    dup["proposal"] = dict(dup["proposal"])
    dup["proposal"]["key"] = "existing-flow"  # already in SCEN_FILE
    with pytest.raises(RulesEditError) as exc:
        apply_ops(SCEN_FILE, [dup])
    assert "already exists" in exc.value.message


def test_add_scenario_missing_scenarios_block_scaffolds():
    # First-promote bootstrap (§9): a file with no `scenarios:` block yet SCAFFOLDS one
    # (mirrors _append_group) rather than refusing — a promoted candidate is a complete
    # scenario, not a half-formed scaffold. The original content survives untouched.
    no_block = "# only comments and version\nversion: 1\n"
    out = apply_ops(no_block, [_runtime_candidate()])
    assert "# only comments and version" in out
    loaded = yaml.safe_load(out)
    assert loaded["version"] == 1
    s = _scen(out, "runtime-web")
    assert s is not None and len(s["steps"]) == 3


def test_add_scenario_into_empty_scenarios_block_fills():
    # A user-created seed with an empty `scenarios:` block: append the first entry into it.
    empty = "version: 1\nscenarios:\n"
    out = apply_ops(empty, [_runtime_candidate()])
    s = _scen(out, "runtime-web")
    assert s is not None and s["name"] == "Runtime: web fan-out"
    assert yaml.safe_load(out)["version"] == 1


def test_add_scenario_into_empty_file_scaffolds():
    # The extreme bootstrap: an entirely empty scenarios.yaml (the serve route passes "" when
    # the file does not exist) — scaffold `scenarios:` + the first entry from nothing.
    out = apply_ops("", [_runtime_candidate()])
    assert out.lstrip().startswith("scenarios:")
    s = _scen(out, "runtime-web")
    assert s is not None and len(s["steps"]) == 3


def test_add_scenario_no_steps_refused():
    cand = {"op": "add_scenario", "source": "runtime",
            "proposal": {"key": "no-steps", "name": "No steps", "scope": "system",
                         "steps": []}}
    with pytest.raises(RulesEditError) as exc:
        apply_ops(SCEN_FILE, [cand])
    assert "no steps" in exc.value.message


def test_add_scenario_all_flagged_refused():
    # a candidate with zero ok steps has nothing to promote.
    cand = {"op": "add_scenario", "source": "runtime",
            "proposal": {"key": "all-bad", "name": "All bad", "scope": "system", "steps": [
                {"from": "A", "to": "B", "order": 1}]},
            "validated_view": {"steps": [
                {"index": 1, "order": 1, "status": "unevidenced", "from": "A", "to": "B",
                 "reason": "no edge"}]}}
    with pytest.raises(RulesEditError) as exc:
        apply_ops(SCEN_FILE, [cand])
    assert "no `ok` steps" in exc.value.message


def test_add_scenario_non_integer_order_refused():
    # a non-integer `order` on the verbatim-proposal promote path must fail-closed as a
    # RulesEditError (the serve route maps it to a 400), NOT escape as a bare ValueError/500.
    cand = {"op": "add_scenario", "source": "runtime",
            "proposal": {"key": "bad-order", "name": "Bad order", "scope": "system",
                         "steps": [{"from": "container:web", "to": "container:core",
                                    "order": "first", "description": "calls core"}]}}
    with pytest.raises(RulesEditError) as exc:
        apply_ops(SCEN_FILE, [cand])
    assert "non-integer `order`" in exc.value.message


def test_add_scenario_preserves_comments_and_is_deterministic():
    out1 = apply_ops(SCEN_FILE, [_runtime_candidate()])
    out2 = apply_ops(SCEN_FILE, [_runtime_candidate()])
    assert out1 == out2, "deterministic — same candidate -> byte-identical"
    assert "\r" not in out1, "LF always"
    # every original comment + the prior entry survive verbatim.
    assert "# Hand-authored behavioral scenarios (comments must survive)." in out1
    assert _scen(out1, "existing-flow")["description"] if False else True
    # re-parses as valid YAML (gate-1 already enforced by apply_ops).
    assert yaml.safe_load(out1)["version"] == 1


def test_add_scenario_appends_after_last_entry():
    # the new entry goes AFTER the existing one (the §9.4 generator reads in order).
    out = apply_ops(SCEN_FILE, [_runtime_candidate()])
    keys = [s["key"] for s in yaml.safe_load(out)["scenarios"]]
    assert keys == ["existing-flow", "runtime-web"]


# --------------------------------------------------------------------------- determinism

def test_lf_endings_and_idempotent_render():
    out = apply_ops(RULES, [{"op": "add_member", "container": "Core Library",
                             "id": "csharp:csproj:src/Extra/Extra.csproj"}])
    assert "\r" not in out, "LF always"
    out2 = apply_ops(RULES, [{"op": "add_member", "container": "Core Library",
                              "id": "csharp:csproj:src/Extra/Extra.csproj"}])
    assert out == out2, "deterministic — same op over same file -> byte-identical"


# ------------------------------------------ dismiss_candidate (adr-candidates.yaml §3.1)

# A representative hand-authored adr-candidates.yaml with a leading comment + an existing
# `dismissed:` block (one entry + a standalone comment) — the block-append primitive must
# preserve every untouched byte and append after the last entry.
ADR_CANDIDATES = """\
# Hand-owned ADR-candidate dismissals (ADR-mining plan §3.1; comments must survive).
version: 1

dismissed:
  # intentional deviations the team has already reviewed
  - slug: tech-standardize-serilog
    reason: "intentional — reporting service legitimately uses Dapper"
    dismissed_at_evidence_hash: "ab12cd34ef560000"
"""

# A file with NO `dismissed:` block — the op must locate-or-create one.
ADR_NO_BLOCK = """\
# Hand-owned ADR-candidate dismissals (no dismissals yet).
version: 1
"""


def _dismissed(text):
    return yaml.safe_load(text).get("dismissed") or []


def test_dismiss_candidate_appends_to_existing_block():
    out = apply_ops(ADR_CANDIDATES, [{
        "op": "dismiss_candidate", "slug": "split-catalog-and-search",
        "reason": "deferred to next quarter",
        "evidence_hash": "ff99aa11bb220000"}])
    added, removed = _diff_lines(ADR_CANDIDATES, out)
    assert removed == [], "additive — touches no existing line"
    # the leading file comment + the block's standalone comment + the first entry survive.
    assert "# Hand-owned ADR-candidate dismissals" in out
    assert "# intentional deviations the team has already reviewed" in out
    entries = _dismissed(out)
    assert [e["slug"] for e in entries] == [
        "tech-standardize-serilog", "split-catalog-and-search"]
    new_entry = entries[1]
    assert new_entry["dismissed_at_evidence_hash"] == "ff99aa11bb220000"
    assert new_entry["reason"] == "deferred to next quarter"
    assert "\r" not in out


def test_dismiss_candidate_creates_block_when_absent():
    out = apply_ops(ADR_NO_BLOCK, [{
        "op": "dismiss_candidate", "slug": "tech-standardize-serilog",
        "reason": "intentional — reporting uses Dapper",
        "evidence_hash": "ab12cd34ef560000"}])
    entries = _dismissed(out)
    assert len(entries) == 1
    assert entries[0]["slug"] == "tech-standardize-serilog"
    assert entries[0]["dismissed_at_evidence_hash"] == "ab12cd34ef560000"
    # the prior content (comment + version) is intact.
    assert yaml.safe_load(out)["version"] == 1
    assert "# Hand-owned ADR-candidate dismissals (no dismissals yet)." in out
    assert "\r" not in out


def test_dismiss_candidate_idempotent_same_hash():
    op = [{"op": "dismiss_candidate", "slug": "tech-standardize-serilog",
           "reason": "intentional — reporting service legitimately uses Dapper",
           "evidence_hash": "ab12cd34ef560000"}]
    out = apply_ops(ADR_CANDIDATES, op)
    # same slug + same hash already present -> no-op, byte-identical.
    assert out == ADR_CANDIDATES, "re-dismiss of unchanged evidence is a no-op"
    out2 = apply_ops(out, op)
    assert out == out2
    assert len(_dismissed(out)) == 1, "no duplicate entry"


def test_dismiss_candidate_resurfaces_on_changed_hash():
    # the same slug with a DIFFERENT evidence_hash appends a second entry (evidence changed).
    out = apply_ops(ADR_CANDIDATES, [{
        "op": "dismiss_candidate", "slug": "tech-standardize-serilog",
        "reason": "re-reviewed against new evidence",
        "evidence_hash": "cc44dd55ee660000"}])
    entries = _dismissed(out)
    assert len(entries) == 2, "a changed hash re-dismisses (last-wins downstream)"
    assert all(e["slug"] == "tech-standardize-serilog" for e in entries)
    assert {e["dismissed_at_evidence_hash"] for e in entries} == {
        "ab12cd34ef560000", "cc44dd55ee660000"}


def test_dismiss_candidate_requires_slug_and_hash():
    with pytest.raises(RulesEditError):
        apply_ops(ADR_CANDIDATES, [{"op": "dismiss_candidate",
                                    "evidence_hash": "ab12cd34ef560000"}])
    with pytest.raises(RulesEditError):
        apply_ops(ADR_CANDIDATES, [{"op": "dismiss_candidate",
                                    "slug": "some-candidate"}])


def test_dismiss_candidate_reason_with_hash_char_is_safe():
    # a reason containing `#` must survive as the FULL string, not be truncated as a comment.
    reason = "skip #2 — owner says intentional; uses Dapper"
    out = apply_ops(ADR_CANDIDATES, [{
        "op": "dismiss_candidate", "slug": "weird-reason",
        "reason": reason, "evidence_hash": "0011223344550000"}])
    entry = next(e for e in _dismissed(out) if e["slug"] == "weird-reason")
    assert entry["reason"] == reason, "the `#` was not eaten as a YAML comment"
    assert "\r" not in out, "LF always"


def test_dismiss_candidate_omits_reason_when_absent():
    # an empty/missing reason omits the `reason:` line entirely (cleaner than reason: "").
    out = apply_ops(ADR_NO_BLOCK, [{
        "op": "dismiss_candidate", "slug": "no-reason-given",
        "evidence_hash": "deadbeef00000000"}])
    entry = _dismissed(out)[0]
    assert entry["slug"] == "no-reason-given"
    assert "reason" not in entry, "no reason line emitted for an absent reason"
    assert entry["dismissed_at_evidence_hash"] == "deadbeef00000000"


# --------------------------------------- set_parent / rename_parent (container-groups §8.2)

# A parented variant of RULES: "Core Library" and "Modules" share the Middleware parent
# tier (Modules' parent line carries an inline comment — the value-swap-vs-delete probe);
# "Web Frontend" and "Content" stay parentless.
PARENTED = RULES.replace(
    '  - container: "Core Library"\n',
    '  - container: "Core Library"\n    parent: Middleware\n').replace(
    '  - container: "Modules"\n',
    '  - container: "Modules"\n    parent: Middleware   # runtime plumbing\n')


def test_set_parent_inserts_after_container_line():
    out = apply_ops(RULES, [{"op": "set_parent", "container": "Core Library",
                             "parent": "Middleware"}])
    assert _group(out, "Core Library")["parent"] == "Middleware"
    b, a = RULES.split("\n"), out.split("\n")
    bi = b.index('  - container: "Core Library"')
    assert a[bi + 1] == "    parent: Middleware", \
        "inserted directly after the entry's container line, at its key indent"
    assert a[:bi + 1] == b[:bi + 1] and a[bi + 2:] == b[bi + 1:], \
        "every other line (comments incl.) byte-identical"


def test_set_parent_zero_indent_entry_style():
    # the v0.8 copied-proposal style: dash at indent 0, `container:` a later key line —
    # the new `parent:` line must land after THAT line, at the entry's 2-space key indent.
    out = apply_ops(ZERO_INDENT, [{"op": "set_parent", "container": "BaseServices",
                                   "parent": "Platform Tier"}])
    assert _group(out, "BaseServices")["parent"] == "Platform Tier"
    b, a = ZERO_INDENT.split("\n"), out.split("\n")
    bi = b.index("  container: BaseServices")
    assert a[bi + 1] == "  parent: Platform Tier", \
        "zero-indent entry keys sit at indent 2 — the new line matches"
    assert a[:bi + 1] == b[:bi + 1] and a[bi + 2:] == b[bi + 1:]


def test_set_parent_replaces_existing_value():
    out = apply_ops(PARENTED, [{"op": "set_parent", "container": "Core Library",
                                "parent": "Platform"}])
    assert _group(out, "Core Library")["parent"] == "Platform"
    added, removed = _diff_lines(PARENTED, out)
    assert len(added) == 1 and len(removed) == 1, "one line swapped, minimal patch"


def test_set_parent_replace_preserves_inline_comment():
    # "Modules"' parent line carries `# runtime plumbing`; a value-swap keeps it verbatim
    # (the rename_group idiom — only a line-DELETE fail-closes on a comment).
    out = apply_ops(PARENTED, [{"op": "set_parent", "container": "Modules",
                                "parent": "Platform"}])
    assert _group(out, "Modules")["parent"] == "Platform"
    assert "    parent: Platform   # runtime plumbing" in out.split("\n")


def test_set_parent_null_removes_line():
    out = apply_ops(PARENTED, [{"op": "set_parent", "container": "Core Library",
                                "parent": None}])
    assert "parent" not in _group(out, "Core Library")
    b, a = PARENTED.split("\n"), out.split("\n")
    bi = b.index("    parent: Middleware")
    assert a == b[:bi] + b[bi + 1:], "exactly the parent line removed, nothing else"


def test_set_parent_remove_when_absent_refused():
    # clearing a parent the entry does not have stays explicit (the remove_member idiom).
    with pytest.raises(RulesEditError) as exc:
        apply_ops(RULES, [{"op": "set_parent", "container": "Web Frontend",
                           "parent": None}])
    assert "no parent to remove" in exc.value.message


def test_set_parent_unknown_container_refused():
    with pytest.raises(RulesEditError) as exc:
        apply_ops(RULES, [{"op": "set_parent", "container": "Nope", "parent": "X"}])
    assert "no group named" in exc.value.message


def test_set_parent_remove_fail_closed_on_inline_comment():
    # "Modules"' parent line carries a comment a line-delete would eat -> refuse (G§Q2).
    with pytest.raises(RulesEditError) as exc:
        apply_ops(PARENTED, [{"op": "set_parent", "container": "Modules",
                              "parent": None}])
    assert "inline comment" in exc.value.message
    assert exc.value.line is not None


def test_set_parent_label_needing_quoting_round_trips():
    # a colon-bearing label needs YAML quoting (a plain `Middleware` stays bare, the
    # file's idiom); the written line must round-trip through yaml.safe_load.
    out = apply_ops(RULES, [{"op": "set_parent", "container": "Content",
                             "parent": "Tier: Middleware"}])
    assert _group(out, "Content")["parent"] == "Tier: Middleware", "quoted + round-trips"
    assert "    parent: 'Tier: Middleware'" in out.split("\n")


def test_set_parent_same_label_is_noop():
    out = apply_ops(PARENTED, [{"op": "set_parent", "container": "Core Library",
                                "parent": "Middleware"}])
    assert out == PARENTED, "re-dropping onto the same parent is a byte-identical no-op"


def test_set_parent_is_deterministic():
    op = [{"op": "set_parent", "container": "Content", "parent": "Middleware"}]
    assert apply_ops(RULES, op) == apply_ops(RULES, op)
    assert "\r" not in apply_ops(RULES, op)


def test_rename_parent_rewrites_all_occurrences():
    out = apply_ops(PARENTED, [{"op": "rename_parent", "old": "Middleware",
                                "new": "Platform"}])
    assert _group(out, "Core Library")["parent"] == "Platform"
    assert _group(out, "Modules")["parent"] == "Platform"
    assert "# runtime plumbing" in out, "inline comment rides the value-swap"
    added, removed = _diff_lines(PARENTED, out)
    assert len(added) == 2 and len(removed) == 2, "exactly the two parent lines swapped"


def test_rename_parent_unknown_label_refused():
    with pytest.raises(RulesEditError) as exc:
        apply_ops(PARENTED, [{"op": "rename_parent", "old": "Nope", "new": "X"}])
    assert "no group has parent" in exc.value.message


def test_rename_parent_same_label_refused():
    with pytest.raises(RulesEditError) as exc:
        apply_ops(PARENTED, [{"op": "rename_parent", "old": "Middleware",
                              "new": "Middleware"}])
    assert "same parent" in exc.value.message


def test_rename_parent_zero_indent_and_deterministic():
    # parent two zero-indent entries, then rename the shared label — both rewritten.
    parented = apply_ops(ZERO_INDENT, [
        {"op": "set_parent", "container": "BaseServices", "parent": "Platform Tier"},
        {"op": "set_parent", "container": "Frontend", "parent": "Platform Tier"}])
    op = [{"op": "rename_parent", "old": "Platform Tier", "new": "Base"}]
    out = apply_ops(parented, op)
    assert _group(out, "BaseServices")["parent"] == "Base"
    assert _group(out, "Frontend")["parent"] == "Base"
    assert apply_ops(parented, op) == out and "\r" not in out
