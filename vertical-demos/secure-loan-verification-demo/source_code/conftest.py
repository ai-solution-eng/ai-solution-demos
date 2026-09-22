"""Pytest path setup: make `engine`, `mcp-server` importable from the source_code root."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
