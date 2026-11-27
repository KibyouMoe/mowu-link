"""Hermes plugin entry point. The rewrite package must be on this installation's Python path."""
from pathlib import Path

from rewrite.hermes_tools import register as register_manager


def register(ctx):
    register_manager(ctx,plugin_entry=Path(__file__))
