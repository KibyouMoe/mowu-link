"""Resolve the old implementation solely for the migration and rollback tests."""
import os
from pathlib import Path


def source_for(component):
    root = Path(os.environ.get("MOWULINK_TEST_LEGACY_ROOT") or Path(__file__).resolve().parents[1])
    source = (root/"server"/component).resolve()
    if not (source/"server.py").is_file():
        raise RuntimeError(f"Legacy {component} test source is missing. Set MOWULINK_TEST_LEGACY_ROOT "
                           "to a directory containing the pinned old server sources.")
    return source
