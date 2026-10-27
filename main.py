"""Compatibility entrypoint: python -m uvicorn main:app --reload."""

import logging

from ai_sre_agent.app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
app = create_app()
