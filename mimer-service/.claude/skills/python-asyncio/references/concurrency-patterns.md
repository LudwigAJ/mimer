# Concurrency Patterns

How to actually run things concurrently. Each pattern lists what it is for, the canonical code, and the failure modes that bite.

## 1. `asyncio.TaskGroup` (Python 3.11+) -- the modern default

```python
async def fetch_all(client, ids: list[int]) -> list[dict]:
    async with asyncio.TaskGroup() as tg:
        tasks = [
            tg.create_task(fetch_one(client, i), name=f"fetch-{i}")
            for i in ids
        ]
    # Reaching here means every task succeeded.
    return [t.result() for t in tasks]
```

Properties:

- All tasks created in the group must complete before the `async with` exits.
- If any task raises, all siblings are cancelled and the exception is re-raised on exit as an `ExceptionGroup` (or `BaseExceptionGroup`). Use `except*` to handle by type.
- `TaskGroup` will not let you create a task after one has already failed -- it raises immediately.
- Cancellation of the surrounding code propagates into the group cleanly.

Idiomatic exception handling:

```python
try:
    async with asyncio.TaskGroup() as tg:
        tg.create_task(a())
        tg.create_task(b())
        tg.create_task(c())
except* TimeoutError as eg:
    log.warning("timeouts: %s", eg.exceptions)
except* ConnectionError as eg:
    log.warning("connection errors: %s", eg.exceptions)
```

When NOT to use `TaskGroup`: when you genuinely want best-effort semantics and to keep partial results regardless of failures. Use `gather(..., return_exceptions=True)` or build per-task try/except.

## 2. `asyncio.gather` -- still ubiquitous

```python
results = await asyncio.gather(
    fetch_one(client, 1),
    fetch_one(client, 2),
    fetch_one(client, 3),
)
# results[i] corresponds to position i. Order is preserved.
```

Three semantics to keep straight:

1. **Default (`return_exceptions=False`).** As soon as any awaitable raises, `gather` raises that exception. The other tasks are NOT automatically cancelled -- they keep running until they reach a yield point. They will eventually be cancelled when the parent coroutine is collected, but you have a window of leaked work. This is the principal reason to prefer `TaskGroup`.

2. **`return_exceptions=True`.** Exceptions are returned as values in the result list. **Always** loop and check:

   ```python
   results = await asyncio.gather(*coros, return_exceptions=True)
   for i, r in enumerate(results):
       if isinstance(r, BaseException):
           log.error("task %d failed", i, exc_info=r)
   ```

   Skipping this check means errors silently disappear into a list comprehension downstream.

3. **Order is preserved.** `gather(a, b, c)` -> `[result_of_a, result_of_b, result_of_c]` regardless of completion order. Important for any use case where the inputs are an ordered series.

`gather` accepts plain coroutines and converts them to tasks internally; you do not need to wrap with `create_task`.

## 3. `asyncio.as_completed` -- streaming results

When you want to start processing results as soon as the first one finishes:

```python
async def first_to_finish(coros):
    for completed in asyncio.as_completed(coros):
        result = await completed
        yield result
```

Use cases:
- Returning the first valid response from a redundant set (then cancelling the rest).
- Feeding a downstream pipeline that processes records one at a time.
- Surfacing partial progress in long fan-outs.

Caveat: results arrive in completion order, **not input order**. If you build a series indexed by input position, capture the position alongside the work:

```python
async def fetch_with_index(i, item):
    return i, await fetch(item)

coros = [fetch_with_index(i, x) for i, x in enumerate(items)]
out: list = [None] * len(items)
for completed in asyncio.as_completed(coros):
    i, value = await completed
    out[i] = value
```

## 4. `asyncio.wait` -- low-level fan-out

`wait` returns two sets `(done, pending)` and gives you `return_when` control:

```python
done, pending = await asyncio.wait(
    tasks,
    return_when=asyncio.FIRST_COMPLETED,   # or FIRST_EXCEPTION, ALL_COMPLETED
    timeout=5.0,
)
for p in pending:
    p.cancel()
```

Use cases:
- "First successful response wins; cancel the rest." (FIRST_COMPLETED)
- "Stop on the first failure; don't kill survivors yet." (FIRST_EXCEPTION)

Two important non-obvious behaviors:
- `wait` does not raise exceptions itself. You must call `task.exception()` on each done task.
- `wait` does not cancel pending tasks on timeout -- you must do it yourself.

In most cases `TaskGroup` or `gather` is cleaner. Reach for `wait` when you need its specific `return_when` semantics.

## 5. Background fire-and-forget

A frequent need: trigger work whose lifetime is not tied to the current scope. The naive form is buggy:

```python
# Bug: this task may be GC'd before completion.
asyncio.create_task(send_metric(...))
```

The fix is to retain a strong reference until the task is done:

```python
_BG: set[asyncio.Task] = set()

def schedule(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BG.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _BG.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                log.exception("background task failed", exc_info=exc)

    task.add_done_callback(_on_done)
    return task
```

The done callback both keeps GC honest and, critically, *retrieves* the exception. Without `task.exception()` being called, the loop logs `"Task exception was never retrieved"` at GC time, which is easy to miss.

If the lifetime is bounded by a function or a request, prefer `TaskGroup` -- structured concurrency is strictly easier to reason about than ambient background tasks.

## 6. Worker-pool / queue pattern

When the work list is large or unbounded, a queue + N workers is more memory-friendly than `gather` over thousands of tasks:

```python
async def process_all(items: Iterable[Item], n_workers: int = 10) -> list[Result]:
    queue: asyncio.Queue[Item] = asyncio.Queue()
    results: list[Result] = []

    async def worker() -> None:
        while True:
            item = await queue.get()
            try:
                results.append(await process(item))
            finally:
                queue.task_done()

    async with asyncio.TaskGroup() as tg:
        workers = [tg.create_task(worker(), name=f"worker-{i}") for i in range(n_workers)]
        for item in items:
            await queue.put(item)
        await queue.join()                # wait for all items to be processed
        for w in workers:
            w.cancel()                    # workers loop forever; cancel to exit
        # TaskGroup will await the cancellations on exit.

    return results
```

Notes on this pattern:
- `queue.join()` waits until every `put` has a matching `task_done()`. Forgetting `task_done()` deadlocks.
- Workers loop forever, so we cancel them after `join`. The cancellation manifests as `CancelledError` inside `queue.get()`, which propagates out of `worker()` and into the `TaskGroup`. The `TaskGroup` recognises the cancellation we initiated (via `tg.create_task` and explicit `cancel`) and does not re-raise.
- For ordered output, attach a sequence number to each item and have workers return `(seq, result)` tuples; sort at the end.

Use a queue when:
- The input is a stream (you do not know the count in advance).
- The total count is large and instantiating one task per item is wasteful.
- You want a backpressure point you can monitor (`queue.qsize()`).

Use `Semaphore` + `gather`/`TaskGroup` when:
- The input fits in memory comfortably and you want simpler code.

### 6.1 Variant: sentinel-based shutdown

The pattern above shuts workers down via cancellation. An alternative -- worth knowing because you will see it in the wild -- is a sentinel value, sometimes called a "poison pill":

```python
SHUTDOWN = object()              # unique sentinel; do not use None if items can be None

async def worker(queue: asyncio.Queue) -> None:
    while True:
        item = await queue.get()
        try:
            if item is SHUTDOWN:
                return            # exit cleanly
            await process(item)
        finally:
            queue.task_done()

async def run(items, n_workers: int = 10) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    workers = [asyncio.create_task(worker(queue), name=f"worker-{i}") for i in range(n_workers)]

    for item in items:
        await queue.put(item)
    for _ in range(n_workers):
        await queue.put(SHUTDOWN)         # one sentinel per worker

    await asyncio.gather(*workers)        # workers exit on their own once they drain
```

When to prefer sentinels over cancellation:
- The work step is non-idempotent and you want every queued item processed before shutdown (cancellation can interrupt a worker mid-`process(item)`).
- You want shutdown to be "drain, then exit" rather than "stop now".
- You want to avoid the `CancelledError`-handling complexity inside `worker`.

When to prefer cancellation:
- You want immediate stop on shutdown, accepting that some items will not run.
- The work is naturally cancellable (HTTP fetches, DB queries that the driver can cancel).

A common bug is using `None` as a sentinel when `None` is also a valid item value. Use a dedicated sentinel object (`object()` or an explicit constant).

## 7. Pipeline pattern

When stages are dependent but each stage parallelises across items:

```python
async def pipeline(ids: list[int]) -> list[Final]:
    async with asyncio.TaskGroup() as tg:
        fetched = [tg.create_task(fetch(i)) for i in ids]
    raw = [t.result() for t in fetched]

    async with asyncio.TaskGroup() as tg:
        enriched = [tg.create_task(enrich(r)) for r in raw]
    detailed = [t.result() for t in enriched]

    async with asyncio.TaskGroup() as tg:
        saved = [tg.create_task(save(d)) for d in detailed]
    return [t.result() for t in saved]
```

For long pipelines, switch to a streaming model with bounded queues between stages. That keeps total memory flat regardless of input size.

## 7.5 Coroutine chaining

A different shape from "pipeline": each step depends on the previous step's *result for the same item*, and the steps are sequential **per item** but parallelisable **across items**. Express this by chaining inside a per-item coroutine, then fan out at the call site.

```python
async def fetch_and_render(user_id: int) -> Page:
    user = await fetch_user(user_id)            # step 1
    posts = await fetch_posts(user["id"])       # step 2: needs user
    enriched = await enrich(posts)              # step 3: needs posts
    return render(user, enriched)

async def run(user_ids: list[int]) -> list[Page]:
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(fetch_and_render(uid)) for uid in user_ids]
    return [t.result() for t in tasks]
```

Each `fetch_and_render(uid)` is sequential internally -- step 2 cannot start before step 1 -- but the entire chain runs concurrently with every other user's chain. This is usually what you want for "fetch a user dashboard": the per-user dependency is preserved, the per-call overlap is maximised.

Anti-pattern: trying to "parallelise" a chain where the next step *needs* the previous result. There is no concurrency to be had inside the chain; the only parallelism available is across items.

## 8. Choosing between the patterns

| Situation | Pick |
|-----------|------|
| Bounded list, all-or-nothing, fail-fast | `TaskGroup` |
| Bounded list, collect all results and exceptions | `gather(..., return_exceptions=True)` |
| Want first result, cancel rest | `wait(return_when=FIRST_COMPLETED)` then cancel pending |
| Stream output as tasks complete | `as_completed` |
| Large/unbounded input, bounded concurrency | Worker pool with `asyncio.Queue` |
| Small input, bounded concurrency | `Semaphore` + `TaskGroup` |
| Sequential dependencies between stages, parallelism within a stage | Pipeline of `TaskGroup`s |

## 9. Determinism and ordering (relevant for time-series / financial code)

- `gather`'s output order matches input order. Safe for building a series.
- `as_completed`, `wait`, and queue-based outputs are in completion order. Re-sort by an explicit key (sequence number, timestamp) before treating as a series.
- The interleaving of coroutines is **not deterministic** across runs -- it depends on I/O timing and OS scheduling. If your computation must be reproducible (backtests, regression tests), do the I/O fan-out async, then run the deterministic computation synchronously over the sorted result.
- `random` is not synchronized with the loop. If multiple coroutines share a `random.Random` instance, results depend on scheduling order. Use a per-coroutine RNG seeded deterministically.
