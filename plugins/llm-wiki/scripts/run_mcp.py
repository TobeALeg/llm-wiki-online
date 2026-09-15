#!/usr/bin/env python3
"""Run the bundled MCP server after installing this plugin's dependencies."""

from pathlib import Path
import sys

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from llm_wiki_mcp.server import main


if __name__ == "__main__":
    main()
