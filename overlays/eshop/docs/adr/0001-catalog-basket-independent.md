---
status: accepted
date: 2024-02-03
---

# 1. Catalog and Basket are independent bounded contexts

## Context

The Catalog and Basket services own separate data and have separate lifecycles.
Coupling them directly would make either one impossible to deploy or scale on its
own and would blur a context boundary we want to keep sharp.

## Decision

The Catalog API must not depend on the Basket API. Any cross-context coordination
happens only through integration events published on the Event Bus.

## Consequences

Each context can evolve and deploy independently; the price is eventual consistency
across the bus rather than a synchronous call.
