"""Celery workers -- docs/11_BackgroundWorkers.md.

Tasks are thin by rule (§1): fetch inputs, call one service method,
handle retry semantics. No business logic lives here.
"""

from backend.workers.app import celery_app

__all__ = ["celery_app"]
