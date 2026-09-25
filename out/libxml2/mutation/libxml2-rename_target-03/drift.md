# Architecture drift report  (commit libxml2-rename_target-03)

## New code facts not represented in model
- new target `cpp:target:testSchemasstrucmut`
- new edge `cpp:target:testSchemasstrucmut` → `cpp:target:libxml2`

## Model elements no longer found in code
- `cpp:target:testSchemas`
- removed edge `cpp:target:testSchemas` → `cpp:target:libxml2`

## Suspected renames / moves (§7.3 — pin via id_aliases)
- `cpp:target:testSchemas` → `cpp:target:testSchemasstrucmut` (suspected rename/move)

## Suspicious dependencies (declared but unused / layering violations)
- _none_
