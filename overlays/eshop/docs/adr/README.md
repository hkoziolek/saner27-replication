# eShop sample ADRs (conformance demo)

`dotnet/eShop` ships **no** Architecture Decision Records, so `arch adr check` against
a fresh clone correctly reports "No ADRs discovered". These four hand-written ADRs give
the conformance path something real to check against — they reference eShop's **actual**
curated containers and together exercise the full verdict spectrum (engine §2a):

| ADR  | Constraint                                              | Expected verdict   | Why |
|------|---------------------------------------------------------|--------------------|-----|
| 0001 | Catalog API must not depend on the Basket API           | **HELD**           | no such edge exists |
| 0002 | Webhooks API must not depend on the Integration Event Log | **VIOLATED**     | that edge *does* exist |
| 0003 | Use Redis for the basket store                          | **NOT-A-CONSTRAINT** | a quality/tech choice, no structural rule (§2a.7) |
| 0004 | Admin Portal must not call the Catalog API              | **UNVERIFIED**     | "Admin Portal" names no model element — never a guess |

## Why this lives in the overlay (and how to use it)

ADRs are a property of the *repo under analysis*: discovery scans the `--repo` tree
(`docs/adr/`, `doc/adr/`, `adr/`, `architecture/decisions/` …), **not** the overlay.
The eShop clone under `fixtures/eshop/` is gitignored and re-fetched, so the committed,
reviewable copy lives here. Materialize it into the clone before checking:

```powershell
# from the repo root, after `arch fetch eshop` and a run into out/eshop:
Copy-Item -Recurse -Force overlays\eshop\docs fixtures\eshop\docs
arch adr check --repo fixtures\eshop --arch-dir out\eshop\architecture
```

`arch run` also picks them up (its discovery pass bakes them into
`discovered-artifacts.json`) if you copy them in *before* the run.
