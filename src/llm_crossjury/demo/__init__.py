"""Demo mode: the real UI with simulated providers and canned answers (no keys, no cost)."""

from llm_crossjury.demo.app import BANNER_ID, BANNER_TEXT, build_demo_deps, create_demo_app
from llm_crossjury.demo.script import DEMO_QUESTION

__all__ = ["BANNER_ID", "BANNER_TEXT", "DEMO_QUESTION", "build_demo_deps", "create_demo_app"]
