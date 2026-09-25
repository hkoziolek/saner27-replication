---
status: accepted
date: 2024-01-20
---

# 3. Use Redis as the basket data store

## Context

Shopping baskets are read and written on nearly every page interaction and do not
require relational integrity or long-term durability.

## Decision

The Basket API stores baskets in Redis for low-latency key/value access. This is a
persistence-technology choice and implies no structural dependency rule between
services.

## Consequences

Basket reads stay fast and basket state is treated as ephemeral, cache-like data.
