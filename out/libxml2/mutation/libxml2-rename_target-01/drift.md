# Architecture drift report  (commit libxml2-rename_target-01)

## New code facts not represented in model
- new target `cpp:target:testAutomatastrucmut`
- new edge `cpp:target:testAutomatastrucmut` → `cpp:target:libxml2`

## Model elements no longer found in code
- `cpp:target:testAutomata`
- removed edge `cpp:target:testAutomata` → `cpp:target:libxml2`

## Suspected renames / moves (§7.3 — pin via id_aliases)
- `cpp:target:testAutomata` → `cpp:target:testAutomatastrucmut` (suspected rename/move)

## Suspicious dependencies (declared but unused / layering violations)
- _none_
