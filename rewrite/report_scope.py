"""Stage/clean a single temporary Desktop project MCP config, without changing trust."""
import argparse
import json
import os
from pathlib import Path
import sys

from .storage import encode


def config_text(state):
    quote = json.dumps
    return (f"[mcp_servers.mowulink_w04_test]\ncommand = {quote(sys.executable)}\n"
            f"args = [{quote('-B')}, {quote('-m')}, {quote('rewrite.project_report')}, {quote(str(state))}]\n"
            f"cwd = {quote(str(Path(__file__).resolve().parent.parent))}\n")


def configure(action, project, state):
    if (not project.name.startswith("mowulink-w04-") or not project.is_dir() or project.is_symlink()
            or state.is_symlink() or not state.is_dir() or state.stat().st_uid != os.getuid()
            or state.stat().st_mode & 0o077):
        raise ValueError("requires a dedicated test project and a private runtime directory")
    folder = project / ".codex"
    config = folder / "config.toml"
    expected = config_text(state)
    if action == "stage":
        folder.mkdir(mode=0o700)
        with config.open("x", encoding="utf-8") as f:
            f.write(expected)
    else:
        if folder.is_symlink() or config.is_symlink() or config.read_text() != expected:
            raise ValueError("refusing to remove changed project config")
        config.unlink()
        if not any(folder.iterdir()):
            folder.rmdir()
    return {"action": action, "config": str(config)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "clean"))
    parser.add_argument("project", type=Path)
    parser.add_argument("private_state", type=Path)
    args = parser.parse_args()
    print(encode(configure(args.action, args.project, args.private_state)))
