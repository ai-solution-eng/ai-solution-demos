"""Pytest path setup: make `engine`, `mcp-server` importable from the services/ dir."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
