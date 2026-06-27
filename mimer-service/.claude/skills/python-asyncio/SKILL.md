---
name: python-asyncio
description: Best practices, patterns, and pitfalls for Python asyncio. Use this skill whenever the user is writing, reviewing, refactoring, or debugging async Python code -- coroutines, tasks, event loops, gather/TaskGroup/semaphores/queues/locks, timeouts, cancellation, async streams/servers, or async libraries in general. Trigger on `async def`, `await`, `asyncio`, coroutines, event loops, concurrent fetching, fan-out, rate limiting. Trigger on symptoms like "coroutine was never awaited", "task was destroyed but it is pending", code that hangs, blocks, leaks tasks, or fails to cancel cleanly. Also trigger when choosing between asyncio, threading, and multiprocessing; when async and sync code interoperate; when serializing access to non-thread-safe resources or designing safe concurrency boundaries; when designing producer/consumer pipelines, worker pools, sentinel shutdown, coroutine chains, or transaction-style serialization; or when diagnosing a blocked event loop and slow callbacks.
---

# Python Asyncio: Best Practices, Patterns, and Pitfalls

This skill is a working guide to writing correct, idiomatic, production-grade asyncio code in Python 3.11+. The body below is self-contained -- read it end to end before writing or reviewing async code. Each section points to a deeper reference under `references/` when more depth is warranted.

> Audience assumption: Python 3.11+. Patterns that require older Python are flagged inline.

---

## 1. Mental model: what asyncio actually is

asyncio is a **single-threaded, cooperative concurrency** framework for I/O-bound work. It is not parallelism. There is one OS thread, one event loop, and many coroutines that voluntarily yield control at `await` points.

Three things to internalize:

1. **`async def` defines a coroutine function.** Calling it does not execute it -- it returns a coroutine object that must be awaited or scheduled as a Task. This is the source of the most common bug in async code: a forgotten `await`.
2. **The event loop is a scheduler.** It picks ready jobs, runs them until they hit `await`, then picks another. Anything that does not `await` (a tight CPU loop, `time.sleep`, a synchronous network call) freezes the entire program.
3. **Concurrency comes from scheduling many coroutines together**, not from defining functions with `async def`. A coroutine awaited in a `for` loop is fully sequential. To get concurrency you must hand multiple coroutines to the loop at once via `asyncio.gather`, `TaskGroup`, or `create_task`.

A useful framing: asyncio is not a "speed knob" you turn on by sprinkling `async`/`await` around. It is a different way of organizing work -- one where I/O waits become opportunities to run other tasks rather than wall-clock time you spend doing nothing. If your service is slow because of CPU work, asyncio will not help; if it is slow because of waiting on the network, disk, or downstream services, asyncio is exactly the right tool. Once you stop expecting it to make individual operations faster and start using it to overlap waits, the patterns below stop feeling arbitrary.

When asyncio is the right tool: thousands of HTTP calls, database queries with async drivers, WebSocket fan-out, file I/O at scale, chat/streaming servers, anything where time is dominated by waiting.

When it is the wrong tool: heavy numeric computation (use `multiprocessing` or `concurrent.futures.ProcessPoolExecutor`); small fan-out using libraries with no async support (a `ThreadPoolExecutor` may be simpler).

For a deeper explanation of coroutines, tasks, futures, and how the event loop schedules them, read `references/fundamentals.md`.

---

## 2. The decision flow

Before writing async code, walk this checklist:

1. **Is the workload I/O-bound?** If no, asyncio will not help. Use processes for CPU-bound work, or threads for blocking I/O libraries with no async equivalent.
2. **Do I have async-native equivalents for every blocking call?** asyncio works only when every potentially-slow call is awaitable. The standard library covers networking via `asyncio.open_connection` / `start_server` and subprocesses via `asyncio.create_subprocess_exec`. For higher-level work (HTTP clients, database drivers, file I/O at scale) you generally need an async-native library, or you must run the sync version inside an executor. If a critical dependency is sync-only, you have two options: wrap each call in `asyncio.to_thread` / `loop.run_in_executor`, or own the dependency on a single dedicated worker thread. See `references/integration-and-blocking-code.md` and `references/thread-safety-and-serialization.md`.
3. **Do I need ordered results, streamed results, or all-or-nothing semantics?**
   - All complete, results in input order, fail-fast: `TaskGroup` (3.11+) or `asyncio.gather`.
   - All complete, collect results and exceptions: `asyncio.gather(..., return_exceptions=True)`.
   - Stream results as they finish (order does not matter): `asyncio.as_completed`.
   - Bounded concurrency over a large list: queue + worker pool, or `Semaphore`.
4. **Do I need to bound concurrency?** Almost always yes for external services. Use a `Semaphore` or worker-pool pattern. Unbounded `gather` over thousands of items is a classic outage cause.
5. **Do I need timeouts?** Almost always yes. Wrap the work in `async with asyncio.timeout(...)`.

---

## 3. Quick reference: the patterns you will use most

### 3.1 Entry point

```python
import asyncio

async def main() -> None:
    ...

if __name__ == "__main__":
    asyncio.run(main())
```

Use `asyncio.run` exactly once, at the top of the program. Do not call `asyncio.get_event_loop()` or `loop.run_until_complete` in new code -- those patterns predate Python 3.7 and create their own foot-guns. In development, pass `debug=True` to surface slow callbacks and unawaited coroutines.

### 3.2 Run many coroutines concurrently

```python
# Modern, structured concurrency (Python 3.11+). Preferred default.
async def fetch_all(client, ids: list[int]) -> list[dict]:
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(fetch_one(client, i), name=f"fetch-{i}") for i in ids]
    return [t.result() for t in tasks]
```

`TaskGroup` is the right default. If any task raises, all siblings are cancelled and the exception is re-raised as an `ExceptionGroup` you handle with `except*`. No orphan tasks, no swallowed exceptions.

`asyncio.gather` is still valid and you will see it everywhere:

```python
results = await asyncio.gather(*(fetch_one(client, i) for i in ids))
```

Two important `gather` semantics:
- Results come back **in input order**, regardless of completion order.
- The default behavior is fail-fast on the first exception, but **other tasks keep running** until they hit a yield point. Use `TaskGroup` if you want them actually cancelled.
- Pass `return_exceptions=True` to collect exceptions as values; you must then loop through results and check `isinstance(r, Exception)`.

### 3.3 Stream results as they complete

When ordering does not matter and you want to start processing the first result immediately:

```python
async for coro in asyncio.as_completed(tasks):
    result = await coro
    handle(result)
```

### 3.4 Bound concurrency with a semaphore

```python
async def fetch_all(client, urls: list[str], limit: int = 10) -> list[dict]:
    sem = asyncio.Semaphore(limit)

    async def bounded(url: str) -> dict:
        async with sem:
            return await fetch_one(client, url)

    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(bounded(u)) for u in urls]
    return [t.result() for t in tasks]
```

Choose `limit` deliberately. A defensible default for external HTTP is 5-20. For a database, match the connection pool size. Prefer `BoundedSemaphore` if you want to catch programming errors (over-release raises `ValueError`).

For when to choose Semaphore vs Queue-based worker pools, and how to size limits, see `references/synchronization-and-throttling.md`.

### 3.5 Timeouts

```python
# Python 3.11+ context manager (preferred).
async def fetch_with_deadline(url: str) -> dict:
    async with asyncio.timeout(5.0):
        return await fetch(url)
```

The body of `async with asyncio.timeout(...)` is cancelled when the deadline passes; a `TimeoutError` is raised at the `async with`. The deadline applies to **all** awaits inside, not just the next one -- this is what you want.

For Python <= 3.10 use `asyncio.wait_for(coro, timeout=5.0)`.

### 3.6 Background fire-and-forget (with the gotchas baked in)

```python
# Keep a strong reference. Tasks held only by a local that goes out of scope
# can be garbage-collected mid-flight, silently cancelling the work.
_BACKGROUND: set[asyncio.Task] = set()

def schedule(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task
```

Anywhere you "fire and forget", retain a reference and attach a done callback that handles or logs exceptions. A bare `asyncio.create_task(...)` whose return value is discarded is a bug factory. Prefer `TaskGroup` whenever the lifetime is bounded by a scope.

### 3.7 Cancellation, cleanly

```python
async def operation() -> Result:
    try:
        return await do_work()
    except asyncio.CancelledError:
        await cleanup()       # release resources, flush buffers, etc.
        raise                  # re-raise. Do not swallow.
```

Rules:
- `CancelledError` is not a normal exception. Catch only to clean up, then **re-raise**.
- Use `async with` for resources -- their `__aexit__` runs on cancellation.
- Wrap any "must complete" critical section in `asyncio.shield(coro)` so an outer cancellation does not interrupt mid-write.

See `references/cancellation-and-timeouts.md` for the full taxonomy of cancellation patterns and shielding.

### 3.8 Mixing in blocking code

```python
result = await asyncio.to_thread(blocking_call, arg1, arg2)
```

`asyncio.to_thread` (3.9+) is the simplest way to run a sync function without blocking the loop. For more control or a process pool, use `loop.run_in_executor(executor, fn, *args)`. Never call `time.sleep`, a synchronous HTTP call, a sync database driver, a large file read, or any other blocking operation directly from a coroutine.

See `references/integration-and-blocking-code.md` for executor sizing, contextvars propagation, and the sync-from-async / async-from-sync corner cases.

---

## 4. The pitfalls list (read this before reviewing async code)

The single highest-leverage thing you can do when reviewing async code is run through this checklist. Each one is a real bug pattern, not a style preference.

1. **Forgotten `await`.** `fetch_one(id)` without `await` returns a coroutine object that is never executed. Python emits `RuntimeWarning: coroutine '...' was never awaited` -- treat it as an error. Run with `asyncio.run(main(), debug=True)` during development.

2. **Awaiting in a loop instead of gathering.** `for x in xs: await fetch(x)` is sequential. If you wanted concurrency, you wrote a bug. Hand the coroutines to `gather` or a `TaskGroup`.

3. **Blocking the event loop.** Any of these freezes everything:
   - `time.sleep(...)` -> use `await asyncio.sleep(...)`.
   - A synchronous HTTP call -> use an async-native HTTP client, or wrap the sync call in `asyncio.to_thread`.
   - Heavy CPU work (hashing, parsing, big array math) -> push to `asyncio.to_thread` or, for true parallelism, a `concurrent.futures.ProcessPoolExecutor`.
   - Synchronous database driver calls -> use an async-native driver, or own the driver on a single worker thread (see `references/thread-safety-and-serialization.md`).

4. **Sync library inside an `async def`.** Wrapping a blocking call in `async def` does not make it non-blocking. The function will still freeze the loop. Either swap for an async-native library or push to an executor.

5. **Orphan / GC'd background tasks.** `asyncio.create_task(...)` whose return value is dropped may be garbage-collected before completion. The Python docs warn: "Save a reference to the result of this function." Either store the task, or use a `TaskGroup`.

6. **Task bombs.** `asyncio.gather(*[fetch(u) for u in million_urls])` will start a million in-flight requests, exhaust file descriptors, and trigger 429s or upstream outages. Bound it with a semaphore or worker queue.

7. **Silent failures from `gather(return_exceptions=True)`.** Exceptions arrive as values in the result list, not raised. Always loop the results and `isinstance(r, Exception)` check.

8. **Swallowing `CancelledError`.** Bare `except Exception` catches it. Either use `except Exception` after re-raising `BaseException`, or be explicit:

   ```python
   try:
       await something()
   except asyncio.CancelledError:
       raise
   except Exception as exc:
       log.exception("...")
   ```

9. **Mixing event loop styles.** `asyncio.get_event_loop()` followed by `loop.run_until_complete(...)` is legacy. Stick to `asyncio.run` as the single entry point.

10. **Assuming asyncio gives you parallelism for CPU work.** It does not. Asyncio is single-threaded cooperative. If your benchmark is CPU-bound, asyncio will be slower than a `for` loop because of scheduling overhead.

11. **Forgetting `await writer.drain()` after `writer.write(...)`.** Without it, you can outpace the kernel send buffer and accumulate unbounded memory. See `references/streams-and-servers.md`.

12. **Closing without `await writer.wait_closed()`.** `writer.close()` is non-blocking and merely *schedules* the close. To know it is done (and to surface any errors), `await writer.wait_closed()`.

A more thorough treatment with diagnostic tips is in `references/pitfalls.md`.

---

## 5. Choosing the right primitive

| Need | Use | Notes |
|------|-----|-------|
| Run N coroutines concurrently, all-or-nothing | `asyncio.TaskGroup` | Python 3.11+. Auto-cancels siblings on error. |
| Run N coroutines, get results in input order | `asyncio.gather` | Fail-fast unless `return_exceptions=True`. |
| Stream results as they finish | `asyncio.as_completed` | Order = completion order. |
| Schedule background work (lifetime > caller) | `asyncio.create_task` + retained reference | Always store a reference. |
| Bound concurrency to N at a time | `asyncio.Semaphore(N)` | `BoundedSemaphore` for stricter checks. |
| Mutual exclusion of a critical section | `asyncio.Lock` | Coordinates coroutines on one loop. NOT thread-safe. |
| Mutual exclusion across OS threads | `threading.Lock` | Use when work runs in `to_thread` / executors. |
| Producer/consumer pipeline | `asyncio.Queue` | Use `queue.join()` and `task_done()`. |
| Signal between coroutines | `asyncio.Event` | Set once / wait many. |
| Wait for a complex predicate | `asyncio.Condition` | Pair with a `Lock`. |
| Hard deadline on a region | `async with asyncio.timeout(s)` | 3.11+. Otherwise `wait_for`. |
| Run sync code without blocking | `asyncio.to_thread` | 3.9+. For CPU-bound, use `ProcessPoolExecutor`. |
| Serialize a non-thread-safe sync resource | `ThreadPoolExecutor(max_workers=1)` | One owning thread; pass it to `run_in_executor`. |
| Stateful / transactional resource | Dedicated worker thread + `queue.Queue` | Queue whole transactions; bridge results with `loop.call_soon_threadsafe`. |
| Notify the loop from a non-loop thread | `loop.call_soon_threadsafe(callback, ...)` | Safe across thread boundaries. |
| Submit a coroutine from a non-loop thread | `asyncio.run_coroutine_threadsafe(coro, loop)` | Returns `concurrent.futures.Future`. |

Detailed semantics, sizing guidance, and patterns for each are in `references/concurrency-patterns.md`, `references/synchronization-and-throttling.md`, and `references/thread-safety-and-serialization.md`.

---

## 6. Streams, servers, and protocols

`asyncio.start_server` (TCP) and `loop.create_datagram_endpoint` (UDP) are the building blocks. The high-level streams API gives you `(reader, writer)` pairs:

```python
async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            line = await reader.readline()
            if not line:                   # EOF
                break
            writer.write(process(line))
            await writer.drain()           # respect flow control
    finally:
        writer.close()
        await writer.wait_closed()         # surface close-time errors

async def serve() -> None:
    server = await asyncio.start_server(handle_client, "127.0.0.1", 8888)
    async with server:
        await server.serve_forever()
```

Two non-obvious points:
- `writer.drain()` is the back-pressure mechanism. Without it, a slow client lets memory grow unbounded.
- `reader.read(n)` returns up to n bytes -- it is **not** "exactly n". For framed protocols use `readexactly(n)` or `readuntil(sep)`.

Full TCP, UDP, TLS, and graceful-shutdown patterns are in `references/streams-and-servers.md`.

---

## 7. Production hygiene

These are not optional in code that ships:

- **Name your tasks.** `tg.create_task(work(x), name=f"work-{x.id}")` -- the name appears in tracebacks and `repr()`.
- **Always handle exceptions on background tasks.** Either via `TaskGroup` (which surfaces them) or via `task.add_done_callback(handler)` that calls `task.exception()`.
- **Install a loop exception handler** for truly orphan exceptions. `loop.set_exception_handler(handler)` catches what would otherwise be logged as "Task exception was never retrieved".
- **Handle signals.** Add `loop.add_signal_handler(signal.SIGTERM, ...)` to trigger graceful shutdown via an `asyncio.Event`. Cancel outstanding work, await it (with a timeout), then exit. See `references/cancellation-and-timeouts.md` for the full pattern.
- **Reuse long-lived clients and pools.** Constructing a new HTTP client or DB connection per request defeats the purpose. Construct one and reuse it for the lifetime of the service, ideally as an `async with` rooted in `main()` (see `contextlib.AsyncExitStack`).
- **Always `async with` your resources** (clients, connections, file handles). Their `__aexit__` runs on cancellation, which the manual close pattern does not guarantee.
- **Test with the stdlib's async-aware test infrastructure.** `unittest.IsolatedAsyncioTestCase` lets you write `async def test_*` methods in a fresh event loop per test, with no third-party dependencies.
- **In dev, run with `asyncio.run(main(), debug=True)`** (or set `PYTHONASYNCIODEBUG=1`). It logs slow callbacks (>100ms by default; tune via `loop.slow_callback_duration`), unawaited coroutines, and bad scheduling. This is your first line of defence against accidental blocking.
- **Tune `loop.slow_callback_duration` for your latency budget.** The default 100ms is permissive for low-latency services. Setting it to 10ms surfaces real issues; the warnings appear in the asyncio logger.
- **Serialize non-thread-safe resources at one boundary.** Do not try to make an unsafe library safe internally; place it behind one owner (an `asyncio.Lock`, a `threading.Lock`, or a single-worker `ThreadPoolExecutor`). For stateful or transactional resources, prefer a dedicated worker. See `references/thread-safety-and-serialization.md`.

---

## 8. Domain notes for numerical / financial code

When asyncio is being used to fetch market data, run pricing requests, or fan out to risk services:

- **Order matters when reconstructing time series.** `asyncio.gather` preserves input order, so building `(timestamp, value)` series is safe if the input list is ordered. `as_completed` returns in completion order -- do not feed its output directly into an ordered series without re-sorting.
- **Determinism for backtests.** The event loop's interleaving is not deterministic across runs (especially under load). If you need reproducible runs, do the fetching in async, then run the deterministic computation synchronously over the (sorted) result.
- **Decimal precision survives fine.** `Decimal` is just a Python object -- it crosses awaits without issue. Do not convert to `float` for transport just to avoid serialization friction; pick a transport (msgpack, protobuf, JSON-with-string-decimals) that round-trips `Decimal` losslessly.
- **Careful with cancellation mid-write.** When writing partial state (e.g. an order book snapshot to a sink), wrap the critical section in `asyncio.shield` or commit-then-ack so a cancellation cannot leave the sink in an inconsistent state.
- **Bound concurrency to the venue's published rate limit.** A `Semaphore` keyed per venue/account is the standard pattern; one global semaphore is rarely the right shape.

---

## 9. Quick reference: the file map

- `references/fundamentals.md` -- coroutines, the event loop, tasks, futures, awaitables, and how `await` actually transfers control.
- `references/concurrency-patterns.md` -- `gather`, `TaskGroup`, `as_completed`, `wait`, `create_task`, queue-based worker pools, sentinel/poison-pill shutdown, coroutine chaining, pipelines.
- `references/synchronization-and-throttling.md` -- `Lock`, `Semaphore`, `BoundedSemaphore`, `Event`, `Condition`, `Queue`; sizing, retry/backoff, dynamic limits.
- `references/cancellation-and-timeouts.md` -- `asyncio.timeout`, `wait_for`, `CancelledError`, `shield`, structured cancellation, signal-driven graceful shutdown.
- `references/streams-and-servers.md` -- TCP/UDP servers, `StreamReader`/`StreamWriter`, framing, TLS, back-pressure, graceful shutdown.
- `references/pitfalls.md` -- a longer catalogue of bug patterns with diagnostics and fixes.
- `references/integration-and-blocking-code.md` -- bridging sync and async, `to_thread`, executors, Jupyter quirks, async generators and context managers, subprocesses, signals, contextvars.
- `references/thread-safety-and-serialization.md` -- making non-thread-safe code safe behind a concurrency boundary; single-worker executors; dedicated worker threads with queues; transaction-scope serialization for stateful resources; `call_soon_threadsafe` and `run_coroutine_threadsafe`.
- `references/production-hygiene.md` -- debug mode, structured logging via `contextvars`, task naming, `AsyncExitStack`-rooted services, observability metrics that matter.

---

## 10. Final checklist (paste into reviews)

Before approving async code, confirm:

- [ ] Every coroutine call is either `await`ed, scheduled with `create_task` (with a retained reference), or owned by a `TaskGroup`.
- [ ] No `time.sleep`, no sync HTTP, no sync DB driver inside `async def`.
- [ ] Every `gather`/`TaskGroup` call site has a defensible concurrency bound (semaphore, queue, batch size).
- [ ] Every external call has a timeout via `async with asyncio.timeout(...)` or `wait_for`.
- [ ] `CancelledError` is either propagated or handled with cleanup and re-raised.
- [ ] Resources use `async with`. No bare `try/finally close`.
- [ ] Streams: `await writer.drain()` after `write`, `await writer.wait_closed()` after `close`.
- [ ] No `asyncio.get_event_loop()` / `run_until_complete` in new code -- only `asyncio.run`.
- [ ] Background tasks have done-callbacks that retrieve exceptions.
- [ ] Tests use the stdlib's async testing facilities (e.g. `unittest.IsolatedAsyncioTestCase`) and exercise the cancellation paths.
- [ ] Non-thread-safe resources are owned at one boundary (`asyncio.Lock`, `threading.Lock`, single-worker executor, or dedicated worker), not protected ad hoc at every call site.
