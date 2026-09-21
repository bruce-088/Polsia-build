"""Per-agent Celery tasks — dispatched by the orchestrator or Beat."""
import asyncio
import time

from celery_app.worker import app


def _run_sync(coro):
    """Run an async coroutine from a sync Celery task."""
    return asyncio.get_event_loop().run_until_complete(coro)


@app.task(name="celery_app.tasks.agent_tasks.run_agent_task", bind=True, max_retries=2)
def run_agent_task(self, task_id: int):
    """Load a Task from DB, build context, run the correct agent, save result."""
    import asyncio

    async def _execute():
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
        from app.config import settings
        from app.agents.crew_factory import run_agent_for_task
        from app.services.task_service import get_task, update_task_status, create_agent_run, finish_agent_run
        from app.services.company_service import get_full_context
        from app.services.activity_service import log_activity

        engine = create_async_engine(settings.database_url)
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with Session() as db:
            task = await get_task(db, task_id)
            if not task:
                return

            await update_task_status(db, task_id, "in_progress")
            context = await get_full_context(db)

            if task.agent_type == "finance":
                from app.services.finance_service import poll_stripe_snapshot

                snapshot = await poll_stripe_snapshot(db)
                if snapshot is not None and context:
                    context["kpis"] = {
                        **context.get("kpis", {}),
                        "mrr_cents": snapshot.mrr_cents,
                        "arr_cents": snapshot.arr_cents,
                        "active_subscribers": snapshot.active_subscribers,
                        "stripe_balance_cents": snapshot.stripe_balance_cents,
                    }

            run = await create_agent_run(db, task.agent_type, task_id=task_id, input_context=context)
            await db.commit()

        start = time.monotonic()
        try:
            task_dict = {
                "id": task.id,
                "title": task.title,
                "description": task.description,
                "task_metadata": task.task_metadata,
            }
            result = run_agent_for_task(task.agent_type, task_dict, context)
            status = "completed"
            summary = result.get("summary", "Task completed.")
            error = None
        except Exception as exc:
            status = "failed"
            summary = None
            error = str(exc)
            result = {}

        duration = round(time.monotonic() - start, 2)

        # The one autonomous send path in the whole codebase — narrow by
        # design. Only reachable for customer_support replies that came
        # from the real inbound sweep (task_metadata carries a real
        # reply_to only in that case; manually-triggered runs never have
        # it) and only if every independent layer of auto_send_policy
        # agrees. Any failure anywhere here just leaves the reply as a
        # draft — the existing, safe default.
        if status == "completed" and task.agent_type == "customer_support" and task.source == "scheduler":
            reply_to = (task.task_metadata or {}).get("reply_to")
            if reply_to:
                try:
                    from app.config import settings
                    from app.services import approval_service
                    from app.services.auto_send_policy import is_safe_to_auto_send
                    from app.services.email_service import send_email

                    reply_subject = task.task_metadata.get("subject", task.title)
                    async with Session() as db:
                        if await is_safe_to_auto_send(db, task.description or "", result):
                            send_email(
                                to_email=reply_to,
                                subject=f"Re: {reply_subject}",
                                body=result.get("reply_draft", ""),
                                from_email=settings.imap_username or None,
                            )
                            result["auto_sent"] = True
                            result["status"] = "sent"
                            summary = f"Auto-sent: {summary}"
                        elif result.get("reply_draft"):
                            # Not safe to send autonomously, but there's a
                            # real draft waiting on a real inbound message —
                            # surface it in the Founder Inbox instead of
                            # letting it sit silently in the task list.
                            await approval_service.create_approval_request(
                                db,
                                task_id=task.id,
                                requested_by_agent="customer_support",
                                decision_type="send_customer_reply",
                                risk_level="YELLOW",
                                title=f"Send reply: {reply_subject}",
                                summary=result.get(
                                    "summary", "A customer reply is drafted and awaiting review."
                                ),
                                evidence=[
                                    f"Inbound message from {reply_to}",
                                    (task.description or "")[:500],
                                ],
                                options=[
                                    {"id": "approve", "label": "Send as drafted"},
                                    {"id": "reject", "label": "Don't send"},
                                ],
                                cost_or_commitment="One outbound email reply.",
                                payload={
                                    "reply_to": reply_to,
                                    "subject": f"Re: {reply_subject}",
                                    "reply_draft": result.get("reply_draft", ""),
                                    "from_email": settings.imap_username or None,
                                },
                            )
                            await db.commit()
                except Exception:
                    pass  # any failure here just leaves the reply as a draft

        async with Session() as db:
            await update_task_status(db, task_id, status, result_summary=summary, error_message=error)
            await finish_agent_run(db, run.id, status, output=result, duration_secs=duration)
            await db.commit()

        # Activity-feed logging is best-effort (drives the live dashboard
        # feed, not task correctness) — a failure here (e.g. Redis publish)
        # must not be able to lose the task completion committed above.
        try:
            async with Session() as db:
                await log_activity(
                    db,
                    agent_type=task.agent_type,
                    action="task_completed" if status == "completed" else "task_failed",
                    summary=summary or error or "No output",
                    level="success" if status == "completed" else "error",
                )
                await db.commit()
        except Exception:
            pass

        await engine.dispose()

    asyncio.run(_execute())


@app.task(name="celery_app.tasks.agent_tasks.run_social_sweep")
def run_social_sweep():
    """Sweep social mentions every 2h."""
    from celery_app.tasks.agent_tasks import run_agent_task
    run_agent_task.delay_with_id = None  # placeholder — create task then enqueue
    _create_and_run("social_media", "Check social mentions and reply to engaging comments")


@app.task(name="celery_app.tasks.agent_tasks.run_email_sweep")
def run_email_sweep():
    """Poll the real inbox via IMAP; each unread message becomes its own
    customer_support task carrying that message's real content, so the
    agent drafts a reply to a real email instead of a generic placeholder.
    No-op if IMAP isn't configured yet (expected until Bruce sets it up)."""
    from app.services.email_inbox_service import fetch_unread_messages

    try:
        messages = fetch_unread_messages()
    except RuntimeError:
        return

    for msg in messages:
        title = f"Reply to: {msg['subject'] or '(no subject)'}"
        description = f"From: {msg['from']}\n\n{msg['body']}"
        task_metadata = {
            "reply_to": msg["from"],
            "message_id": msg["message_id"],
            "subject": msg["subject"] or "(no subject)",
        }
        _create_and_run("customer_support", title, description=description, task_metadata=task_metadata)


@app.task(name="celery_app.tasks.agent_tasks.run_ads_stripe_sync")
def run_ads_stripe_sync():
    _create_and_run("ads_management", "Sync ad metrics from Google and Meta")
    _create_and_run("finance", "Check for failed Stripe payments and update revenue snapshot")


def _create_and_run(
    agent_type: str, title: str, description: str | None = None, task_metadata: dict | None = None
):
    async def _inner():
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
        from app.config import settings
        from app.services.task_service import create_task

        engine = create_async_engine(settings.database_url)
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with Session() as db:
            task = await create_task(
                db,
                title=title,
                agent_type=agent_type,
                source="scheduler",
                description=description,
                task_metadata=task_metadata,
            )
            await db.commit()
            task_id = task.id

        await engine.dispose()
        return task_id

    task_id = asyncio.run(_inner())
    run_agent_task.delay(task_id)
