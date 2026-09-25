# Architecture drift report  (commit libxml2-rename_target-04)

## New code facts not represented in model
- new target `cpp:target:testXPathstrucmut`
- new edge `cpp:target:testXPathstrucmut` → `cpp:target:libxml2`

## Model elements no longer found in code
- `cpp:target:testXPath`
- removed edge `cpp:target:testXPath` → `cpp:target:libxml2`

## Suspected renames / moves (§7.3 — pin via id_aliases)
- `cpp:target:testXPath` → `cpp:target:testXPathstrucmut` (suspected rename/move)

## Suspicious dependencies (declared but unused / layering violations)
- _none_
