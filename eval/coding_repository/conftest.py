"""Fixture programs run in separate workspaces, never as host pytest modules."""

from pathlib import Path

collect_ignore = [str(p.relative_to(Path(__file__).parent))
                  for p in Path(__file__).parent.glob("*/*/repo/test_public.py")]
