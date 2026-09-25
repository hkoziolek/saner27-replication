---
status: accepted
date: 2024-02-10
---

# 2. The Integration Event Log is private to its owning service

## Context

The Integration Event Log table implements the transactional-outbox pattern for a
single owning service. Treating it as a shared component would leak persistence
details across context boundaries and create a hidden coupling point.

## Decision

The Webhooks API must not depend on the Integration Event Log. A service that needs
to react to domain changes subscribes to the Event Bus instead.

## Consequences

Outbox internals stay encapsulated behind their owner. This is an illustrative
guardrail: the current eShop model violates it, which is exactly what makes it a
useful conformance demonstration.
