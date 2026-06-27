# Integration: Bridging Sync and Async

Real codebases mix async and sync. This file covers the corner cases.

## 1. Running a sync function from async code

### `asyncio.to_thread` (3.9+) -- the simple default

```python
result = await asyncio.to_thread(blocking_fn, arg1, arg2, kw=value)
```

Behavior:
- Runs `blocking_fn(arg1, arg2, kw=value)` in the loop's default `ThreadPoolExecutor`.
- The current coroutine awaits the thread's completion.
- The default executor is `concurrent.futures.ThreadPoolExecutor` with `min(32, os.cpu_count() + 4)` workers.

Use `to_thread` for:
- One-off sync calls (a synchronous DB driver call you can't avoid).
- File I/O of bounded size (`pathlib.Path.read_bytes`, etc.).
- Library calls where there is no async equivalent.

`to_thread` propagates `contextvars` (since 3.7), so structured logging context survives the hop.

### `loop.run_in_executor` -- when you need control

```python
loop = asyncio.get_running_loop()
result = await loop.run_in_executor(executor, fn, arg1, arg2)
```

Use this when:
- You want a custom thread pool (e.g. dedicated for one subsystem so heavy users don't starve others).
- You want a `ProcessPoolExecutor` for CPU-bound work.

```python
import concurrent.futures

with concurrent.futures.ProcessPoolExecutor(max_workers=4) as pool:
    results = await asyncio.gather(*[
        loop.run_in_executor(pool, expensive_compute, x) for x in items
    ])
```

Caveats with `ProcessPoolExecutor`:
- Args and return values must be picklable.
- Fork (Linux) inherits open files; spawn (Windows, macOS default) does not. Initialise resources inside the worker, not above the fork.
- The pool's workers run their own Python; do not share asyncio state across the boundary.

### Sizing the executor

Defaults are usually wrong for non-trivial workloads:

| Workload | Pool type | Pool size |
|----------|-----------|-----------|
| File I/O | Thread | 16-64 (disk parallelism is the limit) |
| Sync HTTP via `requests` | Thread | matches your downstream rate limit |
| Heavy CPU | Process | `os.cpu_count()` |
| Mixed | Multiple separate pools | Don't share -- one heavy user starves others |

Pin pool sizes explicitly. Relying on the default thread pool for unbounded work is a footgun.

## 2. Running async code from sync code

### `asyncio.run` -- top-level entry only

```python
def main():
    result = asyncio.run(do_async_work())
```

`asyncio.run` creates a new event loop, runs the coroutine, and shuts the loop down. Call it once per program. Do not call it from inside an already-running async context.

### `asyncio.run_coroutine_threadsafe` -- from another thread into a running loop

```python
import asyncio
import concurrent.futures

# In a thread, given a reference to the loop running on the main thread:
fut: concurrent.futures.Future = asyncio.run_coroutine_threadsafe(coro, loop)
result = fut.result(timeout=5)
```

This is the bridge for "sync code running on a thread needs to call async code that lives on another thread's loop". The returned future is a `concurrent.futures.Future` (not `asyncio.Future`) -- it works with synchronous wait semantics.

Common case: a callback handler from a sync C-extension library needs to enqueue work into the async pipeline.

## 3. The Jupyter / IPython case

Jupyter notebooks already have a running event loop. Two things follow:

- You can `await` directly at the top level of a cell.
- You cannot call `asyncio.run(...)` from a cell -- it will raise.

Do **not** reach for re-entrant-loop hacks to "fix" this in production code. Nesting event loops violates the invariants of many libraries and creates subtle bugs that surface only under load. Inside Jupyter, top-level `await` is acceptable for exploration; outside Jupyter the right answer is one loop per thread, entered exactly once via `asyncio.run`.

## 4. Async generators

```python
async def stream_pages(client, base_url: str) -> AsyncIterator[Page]:
    cursor: str | None = None
    while True:
        page = await client.get(base_url, params={"cursor": cursor})
        for item in page["items"]:
            yield item
        cursor = page.get("next")
        if not cursor:
            return

# Usage:
async for item in stream_pages(client, "/api/feed"):
    process(item)
```

Notes:
- `async for` requires `__aiter__` / `__anext__`. Async generators provide both automatically.
- Use them for streaming data sources (paginated APIs, server-sent events, message queues) where buffering everything is wasteful.
- Cleanup uses `try/finally` inside the generator. The consumer can also `aclose()` it explicitly.
- `async for` cannot iterate a sync generator. There is no automatic conversion.

## 5. Async context managers

```python
@asynccontextmanager
async def open_session(url):
    session = await connect(url)
    try:
        yield session
    finally:
        await session.close()
```

`@asynccontextmanager` from `contextlib` is the cleanest way to build them. The `finally` block runs on cancellation as well as on normal exit, which is the property you want.

For dynamic numbers of resources (e.g. open N sockets where N is computed at runtime), `contextlib.AsyncExitStack`:

```python
async with contextlib.AsyncExitStack() as stack:
    sessions = [
        await stack.enter_async_context(open_session(url))
        for url in urls
    ]
    ...
# all sessions are closed in LIFO order, even on exception
```

## 6. Subprocesses

For shelling out, use `asyncio.create_subprocess_exec` instead of `subprocess.run`:

```python
proc = await asyncio.create_subprocess_exec(
    "ffmpeg", "-i", input_path, "-y", output_path,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
stdout, stderr = await proc.communicate()
if proc.returncode != 0:
    raise RuntimeError(stderr.decode())
```

The async subprocess API integrates with the loop -- writes to stdin, reads from stdout/stderr, and waits for exit are all async. Falling back to `subprocess.run` blocks the loop for the entire duration.

## 7. Signals

```python
import signal

stop = asyncio.Event()
loop = asyncio.get_running_loop()
for sig in (signal.SIGINT, signal.SIGTERM):
    loop.add_signal_handler(sig, stop.set)
```

`add_signal_handler` is Unix-only. On Windows, you need a different mechanism (a watchdog, a control connection, etc.).

The handler runs synchronously on the loop's thread. Keep it cheap -- set an event, do nothing else.

## 8. Cleaning up `contextvars` across boundaries

`contextvars` follow the asyncio task tree: a task inherits its parent's context at creation time, and changes inside a task do not propagate up.

For tracing/logging:

```python
import contextvars

trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id")

async def handler(req):
    trace_id.set(req.headers["x-trace-id"])
    await downstream(req)         # downstream sees the trace_id
```

Across `to_thread` and `run_in_executor`, contextvars are propagated automatically since 3.7. Across `run_coroutine_threadsafe`, you need to set them explicitly on the receiving end (or wrap the coroutine in `contextvars.copy_context().run(...)`).

## 9. asyncio + threading + multiprocessing decision tree

For any unit of work, ask in this order:

1. **Is it I/O-bound and is there an async-native library?** Use asyncio directly.
2. **Is it I/O-bound but only sync libraries are available?** Wrap in `to_thread`. This is fine up to a few hundred threads' worth of work. If the resource is non-thread-safe or stateful, see `references/thread-safety-and-serialization.md` for ownership patterns.
3. **Is it CPU-bound?** `ProcessPoolExecutor` via `run_in_executor`. asyncio gives no benefit; the goal is parallelism via processes.
4. **Is it a long-lived thread holding state (e.g. a market data feed)?** Run the thread, push events into an `asyncio.Queue` via `loop.call_soon_threadsafe(queue.put_nowait, item)`.
5. **Is it a separate process producing data (e.g. a model server)?** Use a real IPC mechanism -- network socket, shared memory, message queue. Do not try to share asyncio state across processes.
