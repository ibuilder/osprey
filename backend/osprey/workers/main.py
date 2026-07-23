"""ARQ worker entrypoint:  arq osprey.workers.main.WorkerSettings

Scheduled polling + on-demand ingest/score jobs. Requires the ``prod`` extra
(arq + redis); this module is only imported by the ``arq`` CLI. The heavy lifting
lives in :mod:`osprey.workers.tasks`.
"""

from __future__ import annotations

import logging

from arq import cron
from arq.connections import RedisSettings

from ..config import settings
from ..db import session_scope
from ..logging_setup import configure_logging
from . import tasks

log = logging.getLogger("osprey.worker")


async def poll_connection(ctx: dict, connection_id: str) -> dict:
    async with session_scope() as session:
        return await tasks.poll_connection(session, connection_id)


async def refresh_project(ctx: dict, project_id: str) -> dict:
    async with session_scope() as session:
        return await tasks.refresh_project_task(session, project_id)


async def poll_all(ctx: dict) -> dict:
    async with session_scope() as session:
        return await tasks.poll_all_active(session)


async def run_scripts(ctx: dict) -> dict:
    async with session_scope() as session:
        return await tasks.run_scheduled_scripts(session)


async def startup(ctx: dict) -> None:
    configure_logging(settings.log_level)
    log.info("Osprey worker online (redis=%s)", settings.redis_url)


class WorkerSettings:
    functions = [poll_connection, refresh_project, poll_all, run_scripts]
    on_startup = startup
    # Poll every source every 5 minutes as the reliable backbone (webhooks add
    # near-real-time on top); run due user scripts every minute.
    cron_jobs = [
        cron(poll_all, minute=set(range(0, 60, 5))),
        cron(run_scripts, minute=set(range(0, 60))),
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
