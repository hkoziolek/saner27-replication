"""Probe: does OrchardCore's module **manifest category** recover the reference `mod_*` families?

The §13 follow-up (``plans/notes/2026-06-14-a2-curation-ablation.md``) found OrchardCore is the one
C# system whose curation gap is NOT a naming convention (``name_segment`` *lowers* it) and NOT a
directory split (``dir_top`` caps at 38) — its reference groups modules into fine functional
families (``mod_OpenIDConnect``, ``mod_Communication``, per-OAuth-provider modules) that live only
in a *source-declared* signal: each module's ``Manifest.cs`` ``[assembly: Feature(Category="…")]``.

This probe extracts that category deterministically (no LLM, no build) and tests how much of the
reference it recovers — i.e. whether a future ``extract_orchardcore_manifest`` extractor would turn
this residual into another build-graph-style fact rather than an LLM guess.

Clustering under test (``manifest``): a module project (a csproj with a sibling ``Manifest.cs`` that
declares a Feature ``Category``) -> ``mod_<Category>`` (non-alphanumerics stripped, matching the
reference's label form); every other project (framework / theme / template / host — no manifest)
falls back to ``dir_top`` so the rung is a complete clustering and scores on the same entity set as
the ablation. Reported two ways: full-set MoJoFM/a2a vs the reference, and the cleaner *module-subset*
agreement (what fraction of the manifest-bearing modules land in the reference's family).

    python baselines/probe_orchardcore_manifest.py            # needs PYTHONUTF8=1 on Windows
"""
from __future__ import annotations

import collections
import re
import shutil
from pathlib import Path

from a2_ablation import _repo_rel, cluster_dir_top  # noqa: E402
from run_arcade import a2a, mojofm, read_contains, write_contains  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SYSTEM = "orchardcore"

_CATEGORY = re.compile(r'Category\s*=\s*"([^"]+)"')


def manifest_category(csproj_rel: str) -> str | None:
    """The primary module category from the sibling Manifest.cs, or None if the project is not a
    manifest-bearing module. Primary = the most frequently declared Feature ``Category`` in the
    file (modules are near-homogeneous; ties resolve to first occurrence)."""
    manifest = REPO / "fixtures" / SYSTEM / Path(csproj_rel).parent / "Manifest.cs"
    if not manifest.exists():
        return None
    cats = _CATEGORY.findall(manifest.read_text(encoding="utf-8", errors="replace"))
    if not cats:
        return None
    # most common, first-wins on ties (Counter preserves insertion order for equal counts)
    return collections.Counter(cats).most_common(1)[0][0]


def label(category: str) -> str:
    return "mod_" + re.sub(r"[^A-Za-z0-9]", "", category)


def main() -> int:
    ref = read_contains(REPO / "references" / SYSTEM / "reference.rsf")
    # build the manifest clustering over the reference's entity set
    nodes = set(ref)
    fallback = cluster_dir_top(nodes)
    manifest_cl: dict[str, str] = {}
    is_module: dict[str, bool] = {}
    for n in nodes:
        cat = manifest_category(_repo_rel(n))
        is_module[n] = cat is not None
        manifest_cl[n] = label(cat) if cat else fallback[n]

    # full-set score
    tmp = REPO / "out" / SYSTEM / "_probe"
    tmp.mkdir(parents=True, exist_ok=True)
    write_contains(ref, tmp / "ref.rsf")
    write_contains(manifest_cl, tmp / "manifest.rsf")
    full_mojo = mojofm(tmp / "manifest.rsf", tmp / "ref.rsf")
    full_a2a = round(a2a(tmp / "manifest.rsf", tmp / "ref.rsf"), 2)

    # module-subset: of the manifest-bearing modules, how many match the reference family?
    mods = [n for n in nodes if is_module[n]]
    # the reference family a module belongs to (its ref label) vs our manifest label
    exact = sum(1 for n in mods if manifest_cl[n] == ref[n])
    # purity-style: map each manifest cluster to the ref label of its majority member, count agreers
    by_mcl = collections.defaultdict(collections.Counter)
    for n in mods:
        by_mcl[manifest_cl[n]][ref[n]] += 1
    majority = sum(c.most_common(1)[0][1] for c in by_mcl.values())

    print(f"OrchardCore manifest-category probe (entities={len(nodes)}, modules with manifest={len(mods)})")
    print(f"  full clustering vs reference : MoJoFM {full_mojo:.1f} / a2a {full_a2a}")
    print(f"  module-subset exact-label match : {exact}/{len(mods)} = {100*exact/len(mods):.1f}%")
    print(f"  module-subset majority-aligned  : {majority}/{len(mods)} = {100*majority/len(mods):.1f}%")
    print(f"  distinct manifest categories    : {len(set(manifest_cl[n] for n in mods))}"
          f"  (reference mod_* families: {len(set(ref[n] for n in mods))})")

    # show where the manifest label disagrees with the reference label (the residual)
    miss = [(n, manifest_cl[n], ref[n]) for n in mods if manifest_cl[n] != ref[n]]
    if miss:
        print(f"\n  {len(miss)} module(s) where manifest != reference label:")
        for n, ml, rl in sorted(miss, key=lambda x: (x[2], x[1])):
            print(f"    {Path(_repo_rel(n)).name:54} manifest={ml:24} ref={rl}")
    shutil.rmtree(tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
