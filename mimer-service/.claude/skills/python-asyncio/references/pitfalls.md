# Asyncio Pitfalls: Diagnostic Catalogue

This is the extended companion to Section 4 of `SKILL.md`. Each entry has the symptom, the cause, and the fix. When debugging async code, scan this list before reaching for tracing tools.

## 1. The forgotten `await`

**Symptom.** `RuntimeWarning: coroutine 'X' was never awaited`. Function appears to do nothing. No exception, no error -- just no effect.

**Cause.** Calling a coroutine function without `await` (or `create_task`) just returns a coroutine object that is then garbage-collected.

**Fix.** Always `await` or schedule. In review, treat the warning as an error. In dev, run with `asyncio.run(main(), debug=True)` to escalate it.

```python
# Wrong:
fetch(url)

# Right:
await fetch(url)
# or:
task = asyncio.create_task(fetch(url))
```

## 2. Awaiting in a loop (the "sequential async trap")

**Symptom.** Async code is no faster than sync code; CPU is idle most of the time; total runtime is sum of individual durations.

**Cause.** `for x in xs: await fetch(x)` is sequential. The loop awaits each task to completion before starting the next.

**Fix.** Hand all coroutines to `gather` or a `TaskGroup`:

```python
# Wrong:
for x in xs:
    out.append(await fetch(x))

# Right:
async with asyncio.TaskGroup() as tg:
    tasks = [tg.create_task(fetch(x)) for x in xs]
out = [t.result() for t in tasks]
```

## 3. Blocking the event loop

**Symptom.** Other coroutines stop responding while a particular operation runs. `debug=True` reports "Executing ... took ... seconds" warnings. CPU pegged at 100% on one core.

**Cause.** A synchronous, blocking, or CPU-heavy call inside a coroutine. Common culprits:

- `time.sleep(...)` -- use `await asyncio.sleep(...)`.
- A synchronous HTTP client -- use an async-native HTTP client, or wrap in `asyncio.to_thread`.
- A sync database driver -- use an async-native driver, or own the driver on a single worker thread.
- A heavy compute step -- big array math, dataframe operations on millions of rows, regex on giant strings.
- `subprocess.run` -- use `asyncio.create_subprocess_exec`.
- Disk reads of large files -- wrap in `asyncio.to_thread` or use a single-worker executor.

**Fix.** Replace with async-native, or push to an executor:

```python
result = await asyncio.to_thread(blocking_fn, *args)
```

For CPU-bound work, use a `ProcessPoolExecutor`:

```python
loop = asyncio.get_running_loop()
with concurrent.futures.ProcessPoolExecutor() as pool:
    result = await loop.run_in_executor(pool, cpu_heavy_fn, payload)
```

## 4. Sync library inside `async def`

**Symptom.** `async def fetch_user(uid)` is awaited from a `gather` but the calls run sequentially.

**Cause.** Wrapping a blocking call in `async def` does not make it non-blocking. The coroutine yields no I/O; the loop has nothing else to schedule until it returns.

```python
# Looks async, behaves sync:
async def fetch_user(uid):
    return sync_http_client.get(f"/users/{uid}").json()
```

**Fix.** Use the async-native client, or push the sync call to an executor:

```python
async def fetch_user(uid):
    return await asyncio.to_thread(sync_http_client.get, f"/users/{uid}")
```

## 5. Orphan / GC'd background tasks

**Symptom.** Tasks vanish without running to completion. Logs show "Task was destroyed but it is pending!" warnings.

**Cause.** The event loop only weakly references tasks. If your reference goes out of scope, the GC may collect the task before it finishes.

**Fix.** Retain a strong reference for the lifetime of the task:

```python
_BG: set[asyncio.Task] = set()

def schedule(coro):
    task = asyncio.create_task(coro)
    _BG.add(task)
    task.add_done_callback(_BG.discard)
    return task
```

Better still: use a `TaskGroup` whenever the task lifetime is bounded by a scope.

## 6. Task bombs (unbounded fan-out)

**Symptom.** OSError "Too many open files", upstream 429s, OOM, network thrashing, downstream service outages caused by your service.

**Cause.** `asyncio.gather(*[fetch(u) for u in urls])` over a large list starts every request simultaneously.

**Fix.** Bound concurrency with a semaphore or worker pool. See `synchronization-and-throttling.md`.

## 7. Silent failures from `gather(return_exceptions=True)`

**Symptom.** Code does not raise but produces no useful output. Errors are silently absorbed.

**Cause.** Exceptions are returned as values in the result list and not raised. If you do not check, they pass through downstream as if they were valid results.

**Fix.** Always loop the results:

```python
results = await asyncio.gather(*coros, return_exceptions=True)
ok = []
for i, r in enumerate(results):
    if isinstance(r, BaseException):
        log.error("task %d failed: %r", i, r)
    else:
        ok.append(r)
```

## 8. Swallowed `CancelledError`

**Symptom.** Cancellations do not actually stop work. `task.cancel()` is followed by code that keeps running. Shutdowns hang.

**Cause.** `except Exception:` somewhere catches `CancelledError` and converts it to a normal exit. Worse: bare `except:` does the same.

```python
# Wrong (silently kills cancellation):
try:
    await work()
except Exception:                       # In 3.7 and earlier this caught CancelledError too.
    log.warning("work failed")
    return None
```

**Fix.** Catch `CancelledError` explicitly and re-raise, or be careful with broad excepts:

```python
try:
    await work()
except asyncio.CancelledError:
    raise
except Exception:
    log.exception("work failed")
    return None
```

In Python 3.8+, `CancelledError` is a `BaseException`, so `except Exception:` no longer catches it. But many codebases still have `except (Exception, BaseException):` or `except:` -- audit them.

## 9. Mixing event loop styles

**Symptom.** "RuntimeError: There is no current event loop", "RuntimeError: Event loop is closed", "got Future <...> attached to a different loop".

**Cause.** Mixing `asyncio.get_event_loop()`, `loop.run_until_complete`, and `asyncio.run` (or running multiple loops at once).

**Fix.** Use `asyncio.run` as the single entry point. If you need the running loop inside async code, use `asyncio.get_running_loop()` (raises if none -- this is correct behavior).

## 10. Treating `asyncio` as parallelism

**Symptom.** "I made it async, why is my CPU-bound code not faster?"

**Cause.** asyncio is single-threaded. CPU work blocks the loop and provides zero throughput improvement.

**Fix.** For CPU work, use `ProcessPoolExecutor` (true parallelism via subprocesses) or, in modern environments, free-threaded Python builds. asyncio-on-CPU-bound is a category error.

## 11. Streams: missing `drain` / `wait_closed`

**Symptom.** Memory grows unbounded under sustained writes; close-time errors disappear; connections hang during shutdown.

**Cause.** `writer.write` is unconditionally non-blocking and buffers in memory. `writer.close` returns immediately and merely *schedules* the close.

**Fix.** Always:

```python
writer.write(payload)
await writer.drain()        # respect back-pressure
...
writer.close()
await writer.wait_closed()  # wait for close to actually complete
```

## 12. Connection-per-request anti-pattern

**Symptom.** Latency dominated by TCP/TLS handshakes. Lots of `TIME_WAIT` sockets in `ss -tn`.

**Cause.** Constructing a new client (HTTP, DB pool, message broker) per call:

```python
async def fetch(url):
    async with new_async_http_client() as client:        # rebuilt each call!
        return await client.get(url)
```

**Fix.** Construct one client at startup and pass it down:

```python
async def main():
    async with new_async_http_client() as client:        # one per process
        await run(client)
```

## 13. Deadlocks via `Queue.join` without `task_done`

**Symptom.** `await queue.join()` hangs indefinitely.

**Cause.** Workers process items but never call `queue.task_done()`. `queue.join()` waits for one `task_done` per `put`.

**Fix.** Always call `task_done`, ideally in a `finally` so it runs on errors too:

```python
while True:
    item = await queue.get()
    try:
        await process(item)
    finally:
        queue.task_done()
```

## 14. Asyncio primitives across threads

**Symptom.** `RuntimeError: <Future ...> attached to a different loop`. Coroutines hang. State that should be visible isn't.

**Cause.** asyncio synchronization primitives are not thread-safe. They are tied to the event loop they were created in.

**Fix.** Use `loop.call_soon_threadsafe(callback, *args)` or `asyncio.run_coroutine_threadsafe(coro, loop)` to bridge from threads. Or restructure so threaded code runs entirely behind `to_thread` and never directly touches asyncio objects.

## 15. The Jupyter trap

**Symptom.** "RuntimeError: This event loop is already running" from `asyncio.run(...)` inside Jupyter or another framework that has its own loop.

**Cause.** Jupyter has a running event loop. Calling `asyncio.run` tries to start a second one in the same thread, which is forbidden.

**Fix.** Inside Jupyter, just `await` directly at the top level of a cell -- it works. Do NOT reach for re-entrant-loop hacks to "fix" this elsewhere; nesting loops violates assumptions of many libraries and is a frequent source of subtle bugs. The right fix in production code is to have one loop per thread.

## 16. Time-series ordering bugs from `as_completed`

**Symptom.** Reconstructed series has out-of-order timestamps; downstream model trains on noise.

**Cause.** `as_completed` and `wait` yield in completion order, not input order. Building a series from their output without re-sorting produces interleaved data.

**Fix.** Use `gather` (preserves order) or carry an explicit index/timestamp through the work:

```python
async def with_index(i, item):
    return i, await fetch(item)

out: list = [None] * len(items)
async for completed in asyncio.as_completed([with_index(i, x) for i, x in enumerate(items)]):
    i, value = await completed
    out[i] = value
```

## 17. `async for` on a sync iterator

**Symptom.** `TypeError: 'X' object is not async iterable`.

**Cause.** `async for` requires an `__aiter__` / `__anext__`. A regular generator works only with `for`.

**Fix.** Wrap the sync iterator in something async, or just use a regular `for`. There is no "automatic" conversion.

## 18. Diagnostic checklist when things go wrong

When async code misbehaves, run through:

1. Run with `asyncio.run(main(), debug=True)` (or `PYTHONASYNCIODEBUG=1`). Read every warning. Tune `loop.slow_callback_duration` lower if your latency budget is tight.
2. Grep the source for hidden sync calls. Names like `time.sleep`, `subprocess.run`, anything ending in `.read_text` / `.read_bytes`, sync HTTP clients, sync DB drivers. Anything that takes >1 ms and is not `await`ed is suspect.
3. Look for `await` inside a `for` loop -- candidate for `gather`.
4. Look for `gather` without a `Semaphore` -- candidate for task bomb.
5. Look for `gather(..., return_exceptions=True)` without a follow-up loop checking results.
6. Look for `create_task` whose return value is discarded.
7. Look for `except Exception:` that might be eating `CancelledError`.
8. Look for `writer.write(...)` without a following `await writer.drain()`.
9. Look for `writer.close()` without `await writer.wait_closed()`.
10. Look for new clients (HTTP, DB) constructed per call instead of reused.
11. Look for non-thread-safe resources accessed from multiple coroutines / threads without a serialization boundary -- see `references/thread-safety-and-serialization.md`.
12. Compare timing under `debug=True` with and without the suspected blocker; the slow-callback log will point at the offending coroutine.
