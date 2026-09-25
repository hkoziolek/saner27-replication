"""Pipeline stages (plan §4–§11). Each module exposes a ``run(ws, ...)`` entry point
and is also runnable as ``python -m anon.stages.<name>``. The ``arch`` CLI
(``anon.cli``) chains them; CI may call them individually (plan §10).
"""
