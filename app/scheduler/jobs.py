"""APScheduler jobs + startup recovery.

All jobs are idempotent and safe to run in any order. On startup we
reconstruct pending work from the DB (analytics checkpoints are persisted
rows; product states are persisted; nothing is lost on restart).
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Settings, get_settings
from app.pipeline import Pipeline
from app.trends.service import TrendService

log = logging.getLogger("scheduler")


class Scheduler:
    def __init__(self, pipeline: Pipeline, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.pipeline = pipeline
        self.sched = AsyncIOScheduler(timezone=self.s.tz)

    def start(self) -> None:
        p = self.pipeline

        self.sched.add_job(
            self._safe(p.tick_intake),
            IntervalTrigger(seconds=30),
            id="intake",
            max_instances=1,
            coalesce=True,
        )
        self.sched.add_job(
            self._safe(p.scan_opportunities),
            IntervalTrigger(minutes=self.s.opportunity_scan_minutes),
            id="opportunity_scan",
            max_instances=1,
            coalesce=True,
        )
        self.sched.add_job(
            self._safe(p.tick_ready_to_publish),
            IntervalTrigger(minutes=5),
            id="ready_to_publish",
            max_instances=1,
            coalesce=True,
        )
        self.sched.add_job(
            self._safe(p.tick_analytics),
            IntervalTrigger(minutes=5),
            id="analytics",
            max_instances=1,
            coalesce=True,
        )
        self.sched.add_job(
            self._safe(self._trend_refresh),
            IntervalTrigger(minutes=self.s.trend_refresh_minutes),
            id="trend_radar",
            max_instances=1,
            coalesce=True,
        )
        self.sched.add_job(
            self._safe(self._strategy_refresh),
            "cron",
            hour=6,
            minute=30,  # 06:30 MYT daily
            id="strategy_memory",
            max_instances=1,
            coalesce=True,
        )
        self.sched.start()
        log.info(
            "scheduler started: trend every %dm, opportunity every %dm",
            self.s.trend_refresh_minutes,
            self.s.opportunity_scan_minutes,
        )

    def shutdown(self) -> None:
        if self.sched.running:
            self.sched.shutdown(wait=False)

    async def _trend_refresh(self) -> None:
        svc = TrendService(self.s)
        await svc.refresh()

    def _strategy_refresh(self) -> None:
        from app.learning.memory import StrategyMemoryService

        StrategyMemoryService(self.s).refresh()

    @staticmethod
    def _safe(coro_fn):
        async def wrapper():
            try:
                await coro_fn()
            except Exception:
                log.exception("scheduled job %s crashed", getattr(coro_fn, "__name__", "?"))
        return wrapper
