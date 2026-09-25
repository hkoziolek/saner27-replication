# Architecture drift report  (commit libxml2-rename_target-02)

## New code facts not represented in model
- new target `cpp:target:testHTMLstrucmut`
- new edge `cpp:target:testHTMLstrucmut` → `cpp:target:libxml2`

## Model elements no longer found in code
- `cpp:target:testHTML`
- removed edge `cpp:target:testHTML` → `cpp:target:libxml2`

## Suspected renames / moves (§7.3 — pin via id_aliases)
- `cpp:target:testHTML` → `cpp:target:testHTMLstrucmut` (suspected rename/move)

## Suspicious dependencies (declared but unused / layering violations)
- _none_
