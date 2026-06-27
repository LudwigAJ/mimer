# Cancellation and Timeouts

The single largest correctness gap in production async code is in cancellation handling. This file is the working reference for getting it right.

## 1. The model

Cancellation in asyncio is **cooperative** and **exception-based**. When you call `task.cancel()`:

1. The loop schedules a `CancelledError` to be raised inside the task at its next `await`.
2. The task can catch it, do cleanup, and re-raise. It can also (with care) suppress it -- but that is almost always a bug.
3. If the task is currently NOT awaiting (running CPU code), the cancellation is delivered when it next yields.

Two non-obvious consequences:

- A coroutine that never `await`s cannot be cancelled. A tight CPU loop in a coroutine ignores cancellation completely until it yields.
- A task that catches `CancelledError` and returns normally has effectively suppressed cancellation. The caller will see a result, not a cancellation.

## 2. The rules for handling `CancelledError`

```python
async def operation():
    try:
        return await do_work()
    except asyncio.CancelledError:
        await cleanup()
        raise                # always re-raise
```

Rules:

1. **Re-raise after cleanup.** Suppressing `CancelledError` breaks the cancellation chain and confuses callers expecting the task to actually stop.
2. **Catch `CancelledError` explicitly, not `Exception`.** In Python 3.8+, `CancelledError` is a `BaseException`, not an `Exception`, specifically so that `except Exception:` does not accidentally catch it. If you have legacy code that does `except Exception:` somewhere, audit it.
3. **Do not `await` long-running operations inside the cleanup branch** unless they are bounded by `asyncio.shield` or by their own `timeout`. Otherwise the cleanup itself can be cancelled.
4. **Use `try / finally` for resource cleanup**, not `try / except CancelledError / finally`. `finally` runs on cancellation too, and uses the cleaner code path.

```python
async def operation():
    resource = await acquire()
    try:
        return await do_work(resource)
    finally:
        await release(resource)        # runs on success, error, OR cancellation
```

Even better: `async with` for resources whose lifecycle should follow the scope.

## 3. `asyncio.timeout` (Python 3.11+) -- the modern API

```python
async def fetch_with_deadline(url: str) -> dict:
    async with asyncio.timeout(5.0):
        return await fetch(url)
```

How it works:
- The context manager schedules a cancellation of the *current task* after 5 seconds.
- If the body returns first, the cancellation is unscheduled.
- If the deadline fires, the body's `await` is cancelled. The context manager catches the resulting `CancelledError` and re-raises as `TimeoutError`.

Properties to know:
- The deadline applies to **all** awaits inside the body, not just the next one.
- The deadline can be **adjusted** mid-flight: `cm = asyncio.timeout(5)`; `cm.reschedule(None)` to disable; `cm.reschedule(when)` to set a new absolute deadline.
- Nested `asyncio.timeout` blocks are independent. The innermost expiring deadline wins for code under it.

Common pattern: deadline propagation across calls.

```python
async def handler(request):
    async with asyncio.timeout(30.0):                  # whole-request budget
        prefs = await fetch_prefs(request.user_id)     # whatever this takes
        return await render(prefs)                     # using the rest of the budget
```

## 4. `asyncio.wait_for` (older, still valid)

```python
result = await asyncio.wait_for(fetch(url), timeout=5.0)
```

`wait_for` wraps a single awaitable, applies a timeout, and raises `TimeoutError` on expiration. Differences from `asyncio.timeout`:

- Operates on one coroutine, not a region.
- The wrapped coroutine is cancelled if it has not completed.
- More awkward to compose for "deadline across multiple calls" -- you would compute the remaining budget yourself.

For Python <= 3.10, use `wait_for`. For >= 3.11, prefer `asyncio.timeout`.

## 5. `asyncio.shield` -- "do not let me be cancelled mid-flight"

```python
result = await asyncio.shield(write_to_disk(data))
```

If the surrounding task is cancelled while shielded code is running, the shielded coroutine continues to completion. The cancellation surfaces in the parent immediately, but the shielded work finishes.

Use cases:
- Critical writes that must complete to keep state consistent (committing a trade, finalizing a database transaction).
- "I will tell you the result if it finishes within the deadline; if it doesn't, just let it finish in the background."

Mistakes to avoid:
- `shield` does not cancel the shielded task on its own. If you abandon a shielded task, it keeps running.
- A `CancelledError` raised inside the shielded coroutine is unaffected -- shielding only stops *external* cancellation.

## 6. Cancelling a task you started

```python
task = asyncio.create_task(work())
...
task.cancel()
try:
    await task                 # propagates the CancelledError
except asyncio.CancelledError:
    pass                       # we initiated it, so we can swallow here
```

Note that `task.cancel()` is idempotent and non-blocking. You still need to `await task` to know it has actually finished its cleanup.

For multi-task cancellation (cleanup paths in worker pools), the typical idiom:

```python
for t in tasks:
    t.cancel()
await asyncio.gather(*tasks, return_exceptions=True)   # absorb the CancelledErrors
```

Within a `TaskGroup` you do not need this -- exiting the group already awaits all tasks.

## 7. Graceful shutdown for a service

A clean shutdown pattern, suitable for any long-running async service:

```python
import asyncio
import signal

async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with asyncio.TaskGroup() as tg:
        server = tg.create_task(run_server())
        tg.create_task(periodic_metrics())

        await stop.wait()                  # block until a signal arrives

        # Begin graceful shutdown. Cancel cleanly; TaskGroup will await them.
        server.cancel()
        # Allow up to 10s for in-flight work to drain.
        async with asyncio.timeout(10.0):
            # Tasks within the group will be awaited on exit; we do not need
            # to do it explicitly here.
            pass
```

Variations:
- For a process that has long-running queries (e.g. a venue WebSocket), prefer letting them finish on a flag (`stop.is_set()`) rather than cancelling them, because cancellation may leave server-side state untidy.
- For workers, drain by stopping `put`s, then `await queue.join()`, then cancel the consumers.

## 8. Timeouts and retries together

Combine timeouts (per-attempt) with retry/backoff (across attempts):

```python
async def call_with_retries(coro_factory, *, attempts: int, per_attempt: float):
    for attempt in range(attempts):
        try:
            async with asyncio.timeout(per_attempt):
                return await coro_factory()
        except asyncio.TimeoutError:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(0.2 * 2 ** attempt)
```

Common mistake: applying a single outer timeout *and* retries. The outer timeout will fire mid-retry, leaving you uncertain whether the work is in-flight or not.

## 9. Cancellation and coroutines that must clean up over the network

When a coroutine is mid-write to a socket and gets cancelled, the partial write is observable to the peer. Two strategies:

1. **Two-phase commit.** Write to a staging area, then atomically move/rename. Cancellation between the two phases leaves the staging area, which you can sweep on restart.
2. **Shield the write.** `await asyncio.shield(write_payload(socket, data))` so the write completes; the cancellation propagates after.

For trade-execution code, prefer (1). It is more predictable and survives crashes, not just cancellations.

## 10. Common cancellation-related bugs

- **`except Exception` swallowing `CancelledError`.** In 3.7 and earlier, `CancelledError` was an `Exception` subclass. Mixing 3.7 and 3.8+ codebases is the most common source of subtle cancellation bugs. Always `except (Exception,)` plus an explicit `except asyncio.CancelledError: raise` if you need to be exhaustive.
- **`asyncio.gather` with cancellation.** If the parent task is cancelled, `gather`'s children are cancelled too -- but the parent only sees the cancellation after all children have finished propagating it. Use `TaskGroup` for cleaner semantics.
- **`shield` plus discarded reference.** If you `await asyncio.shield(coro)` and your task is cancelled, the parent task moves on, but the underlying task created by `shield` continues. Make sure that is what you want; if it is, retain a reference so it does not get GC'd before completion.
- **Forgetting `await task` after `task.cancel()`.** The task is still running until it finishes its cleanup. If you do not await it, you may exit the parent before the cancellation has actually completed.
