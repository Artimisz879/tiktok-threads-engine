"""Application entrypoint.

python -m app.main

Starts (all in one process, one event loop):
  1. Database (SQLite, schema created if missing)
  2. Pipeline + services
  3. Telegram bot (polling) — if token configured
  4. APScheduler jobs (trend radar, opportunity scan, publish retries, analytics)
  5. FastAPI admin/debug API on 127.0.0.1 (uvicorn in-process)

Startup recovery is implicit: all pending work lives in the DB
(product states, analytics checkpoints), so a restart simply resumes.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import uvicorn

from app.api.routes import admin_app
from app.config import get_settings
from app.database import init_db
from app.logging_setup import setup_logging
from app.pipeline import Pipeline
from app.scheduler.jobs import Scheduler
from app.telegram.bot import TelegramBot

log = logging.getLogger("main")


async def amain() -> None:
    s = get_settings()
    setup_logging(s.log_level)
    log.info("starting tiktok-threads-engine")

    # 1. Database
    import os

    os.makedirs(s.data_dir, exist_ok=True)
    init_db(s.db_path)
    log.info("database ready at %s", s.db_path)

    # 2. Pipeline
    pipeline = Pipeline(s)

    # 3. Telegram
    tg: TelegramBot | None = None
    if s.telegram_bot_token and s.telegram_allowed_user_id:
        tg = TelegramBot(pipeline, s)
        pipeline.notifier = tg.notify
        log.info("telegram bot configured (user %s)", s.telegram_allowed_user_id)
    else:
        log.warning(
            "telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_ALLOWED_USER_ID) — "
            "running without the bot interface"
        )

    # 4. Scheduler
    scheduler = Scheduler(pipeline, s)
    scheduler.start()

    # 5. Admin API (in-process uvicorn)
    config = uvicorn.Config(
        admin_app,
        host=s.admin_host,
        port=s.admin_port,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    admin_task = asyncio.create_task(server.serve())
    log.info("admin API on http://%s:%d", s.admin_host, s.admin_port)

    # Run telegram polling (blocks forever) or just wait.
    stop = asyncio.Event()
    if tg is not None:
        tg_task = asyncio.create_task(tg.run_polling())
        tasks = [tg_task, admin_task]
    else:
        tasks = [admin_task]

    def _shutdown(*_):
        log.info("shutdown signal received")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass  # Windows: rely on KeyboardInterrupt

    try:
        await stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("shutting down")
        scheduler.shutdown()
        server.should_exit = True
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("bye")


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
