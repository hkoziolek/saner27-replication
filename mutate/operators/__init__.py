"""Operator registry (§8.1 mutation table, one module per row)."""
from __future__ import annotations

from . import (add_forbidden_dep, add_target, merge_targets, remove_dep,
               remove_target, rename_target)

OPERATORS = {
    "add_forbidden_dep": add_forbidden_dep,
    "remove_dep": remove_dep,
    "add_target": add_target,
    "remove_target": remove_target,
    "rename_target": rename_target,
    "merge_targets": merge_targets,
}
