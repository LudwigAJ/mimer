# Production Hygiene (Standard Library Only)

Things that pay for themselves the first time you have an incident, using only what ships with Python. None of this is required for correctness; all of it makes incident response and observability tractable.

## 1. Detecting a blocked event loop

The single most common production incident in async services is a blocked event loop. Symptoms: latency spikes, timeouts, heartbeats stop firing, structured-logging flushes back up. Stack traces look fine because the offending code already returned.

### 1.1 Debug mode

The first line of defence, and the one that costs you nothing:

```python
asyncio.run(main(), debug=True)
# or set the env var:
# PYTHONASYNCIODEBUG=1
```

Debug mode causes the loop to:

- Log "Executing <Handle ...> took X seconds" when a callback runs longer than `loop.slow_callback_duration` (default 0.1 s).
- Emit a warning when a coroutine is created but never awaited.
- Warn when a task takes too long to be cancelled.
- Raise an exception when non-thread-safe APIs (e.g. `loop.call_soon`) are called from the wrong thread.
- Produce more verbose tracebacks for unhandled task exceptions, including where the offending coroutine was created.

Tune the threshold for production-shaped services. For a low-latency service, 100 ms is permissive; 10 ms surfaces real issues:

```python
loop = asyncio.get_running_loop()
loop.slow_callback_duration = 0.010
```

You can also enable Python development mode (`-X dev` or `PYTHONDEVMODE=1`), which turns on asyncio debug mode along with `ResourceWarning` logging and other diagnostics. Run integration tests with development mode on.

### 1.2 Routing the asyncio logger

asyncio uses Python's `logging` module under the namespace `asyncio`. Slow-callback warnings, unhandled exceptions, and other diagnostics all go through this logger. Set its level explicitly:

```python
import logging
logging.getLogger("asyncio").setLevel(logging.WARNING)
```

In production, route this logger to your normal alerting pipeline. Slow-callback warnings are an early indicator that something is blocking the loop.

## 2. Naming everything

Two layers of naming pay off in incident response:

```python
async def main():
    asyncio.current_task().set_name("main")          # the entry task
    async with asyncio.TaskGroup() as tg:
        tg.create_task(handle_orders(), name="orders")
        tg.create_task(handle_quotes(), name="quotes")
```

Names appear in `repr(task)`, in `str(task)`, and in tracebacks like `Task exception was never retrieved [task: orders]`. Without names, tasks show up as `Task pending coro=<func() running at file:line>` -- readable, but not searchable in logs.

Combine with structured logging: log the current task name in every event, so you can grep by task identity end-to-end.

## 3. Loop exception handler

Any exception that escapes a task and is never retrieved (because the task is GC'd before `task.exception()` is called) reaches the loop's exception handler. By default it is logged at ERROR. In production you almost certainly want it on a metric or an alert:

```python
def on_loop_exception(loop, context):
    msg = context.get("message")
    exc = context.get("exception")
    log.error("loop exception: %s", msg, exc_info=exc)
    metrics.increment("asyncio.unhandled")

loop = asyncio.get_running_loop()
loop.set_exception_handler(on_loop_exception)
```

The `context` dict can also include `task`, `handle`, `protocol`, `transport`, and `socket`. Inspecting `context.get("task")` in the handler tells you which task lost the exception.

## 4. Structured logging via `contextvars`

`contextvars` is the canonical way to propagate per-request context through asyncio. Each task inherits its parent's context at creation time, but mutations stay local to the task. This is exactly the property you want for request IDs, trace IDs, and per-tenant context.

```python
import contextvars

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id")

async def handle(req):
    request_id.set(req.id)
    log.info("handling")            # log filter pulls request_id from contextvars
    await downstream(req)           # downstream sees the same request_id

async def downstream(req):
    log.info("downstream call")     # same request_id is available
```

Wire this into your logging via a filter:

```python
import logging

class ContextFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id.get("")
        return True

handler.addFilter(ContextFilter())
formatter = logging.Formatter("%(asctime)s %(request_id)s %(message)s")
```

Across `to_thread` and `run_in_executor`, contextvars are propagated automatically. Across `run_coroutine_threadsafe`, you must propagate explicitly (use `contextvars.copy_context().run(...)` on the receiving end).

## 5. Resource lifecycle in services

Build your service as a single `async with` chain at the top of `main`. This guarantees that on cancellation or shutdown, every resource is released in the right order. `contextlib.AsyncExitStack` makes this composable when the number of resources is dynamic:

```python
import contextlib

async def main():
    async with contextlib.AsyncExitStack() as stack:
        client = await stack.enter_async_context(open_async_http_client())
        db = await stack.enter_async_context(open_db_pool(DSN))
        cache = await stack.enter_async_context(open_cache())
        await run_service(client, db, cache)
```

If `run_service` is cancelled (signal, deadline), the `AsyncExitStack` unwinds in LIFO order regardless. This is strictly safer than a chain of try/finally blocks, which break on the first cancellation.

## 6. Observability metrics that matter

The metrics that matter for an asyncio service:

- **Loop lag.** Time between a `loop.call_later(0, ...)` being scheduled and the callback being run. Persistent lag above a few ms means the loop is being blocked. Sample at 1 Hz; alert on the p99.
- **In-flight task count.** `len(asyncio.all_tasks())` is cheap. Sudden growth signals a leak or a hang.
- **Per-endpoint p99 latency.** Asyncio's whole win is overlapping waits; if p99 climbs without p50 changing, you have head-of-line blocking somewhere -- a single coroutine is monopolising the loop.
- **Queue depth** for any internal `asyncio.Queue` you depend on. Backpressure shows up here first.
- **Connection pool saturation.** Time spent waiting in `pool.acquire()` (or your equivalent) is invisible from outside the pool but is often the actual bottleneck.
- **Cancellation rate.** Spikes correlate with timeout misconfiguration or upstream slowness.

Loop lag is easy to measure with a small dedicated coroutine:

```python
async def loop_lag_probe(interval: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    while True:
        start = loop.time()
        await asyncio.sleep(interval)
        actual = loop.time() - start
        lag = actual - interval
        metrics.histogram("asyncio.loop_lag_seconds", lag)
```

If `lag` is consistently above a few ms, something is blocking the loop. The next step is debug mode, which will name the offending callback.

## 7. Production checklist (expansion of the SKILL.md final checklist)

Before deploying an asyncio service:

- [ ] `asyncio.run(main(), debug=True)` runs in CI; the slow-callback log is empty under realistic load.
- [ ] `loop.slow_callback_duration` is set explicitly to match the service's latency budget.
- [ ] Loop exception handler is installed; orphan exceptions go to metrics, not just stderr.
- [ ] All long-lived resources are owned by an `AsyncExitStack` rooted in `main`.
- [ ] Signal handlers route to a single `asyncio.Event` that drives shutdown.
- [ ] Loop lag, task count, and queue depth are exported as metrics.
- [ ] Tasks have names; logs include task name and request id (or trace id) via `contextvars`.
- [ ] Every external client (HTTP, DB, cache) is constructed once and reused.
- [ ] Every external call has a timeout via `async with asyncio.timeout(...)`.
- [ ] No bare `asyncio.create_task` whose return value is discarded.
- [ ] Non-thread-safe resources are owned at exactly one boundary -- not protected ad hoc at every call site.

This list is what separates "it works in dev" from "it stays up under traffic".
