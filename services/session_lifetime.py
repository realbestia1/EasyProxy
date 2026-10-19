import asyncio


def retire_session(owner, session, grace: float = 30.0):
    """Detach immediately; let requests already using the connector drain.

    `grace` is the window that lets borrowers between session lookup and
    request startup run before the drain check; it can be 0 when the caller
    already removed every reference to the session (e.g. retired extractor).
    """
    if session is None or session.closed:
        return
    tasks = getattr(owner, "_retired_session_tasks", None)
    if tasks is None:
        tasks = owner._retired_session_tasks = set()

    async def drain():
        try:
            # Allow borrowers between session lookup and request startup to run.
            await asyncio.sleep(grace)
            while not session.closed:
                connector = session.connector
                if connector is None or not (
                    getattr(connector, "_acquired", ())
                    or getattr(connector, "_waiters", ())
                ):
                    break
                await asyncio.sleep(1)
        finally:
            await session.close()

    task = asyncio.create_task(drain())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
