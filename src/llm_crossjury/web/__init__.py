"""Local web UI for compare mode (FastAPI + one static page, no build step)."""

from llm_crossjury.web.app import LOOPBACK_HOSTS, create_app

__all__ = ["LOOPBACK_HOSTS", "create_app"]
