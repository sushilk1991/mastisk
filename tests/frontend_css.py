"""Read the PWA's CSS for static contract tests.

The shell sheet (``mastisk.css``) holds what every view shares; rules used by
one lazily loaded view live in ``styles/views/<Component>.css``.
"""
from __future__ import annotations

from pathlib import Path

STYLES = Path(__file__).resolve().parent.parent / "frontend/src/styles"


def shell_css() -> str:
    return (STYLES / "mastisk.css").read_text(encoding="utf-8")


def view_css(*components: str) -> str:
    return "\n".join((STYLES / "views" / f"{name}.css").read_text(encoding="utf-8") for name in components)


def frontend_css() -> str:
    """Every stylesheet the app ships, shell first."""
    views = sorted(p.stem for p in (STYLES / "views").glob("*.css"))
    return shell_css() + "\n" + view_css(*views)
