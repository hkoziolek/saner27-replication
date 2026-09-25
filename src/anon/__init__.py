"""Anon — evidence-grounded architecture recovery pipeline.

Turns large C/C++ and C# repositories into governed C4/Structurizr architecture
models. The build graph is the source of structural truth; LLMs are confined to
naming/descriptions only. See plans/2026-06-03-fact-extraction-structurizr-pipeline.md.

This package is the pipeline *tool*. Per plan §19.4 it is installed once and pointed
at a target working tree via `--repo`; it never modifies the target's source, only its
``architecture/`` folder (or an out-of-tree ``--arch-dir`` for fixtures).
"""

__version__ = "0.1.0"

SCHEMA_VERSION = "1.3"  # fact-model schema_version this build emits (schema/fact-model.schema.json)


def force_utf8_stdio() -> None:
    """Emit UTF-8 to the console regardless of the OS code page (plan §22.3).

    Anon's reports print `§`/`→`/`⇄`; on a Windows console or a cp1252-redirected
    pipe those raise UnicodeEncodeError. The `arch` CLI calls this at startup — and so
    must every stage's ``python -m`` entry point (the CI-granularity path runs WITHOUT
    the CLI wrapper, so a redirected stdout crashed mid-render before this was shared).
    File artifacts always carry an explicit encoding; this only affects console output.
    """
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
