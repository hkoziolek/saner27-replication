---
status: proposed
date: 2024-03-01
---

# 4. Admin tooling stays outside the transactional core

## Context

A future back-office Admin Portal will need read access to catalog data, but it
should never become a hidden coupling point for the transactional services.

## Decision

The Admin Portal must not call the Catalog API directly; it reads through a
dedicated read-model API instead.

## Consequences

The transactional Catalog API keeps a small, well-known set of clients. (The Admin
Portal is not yet in the codebase, so this constraint names an element the model
does not contain — a deliberate UNVERIFIED case.)
