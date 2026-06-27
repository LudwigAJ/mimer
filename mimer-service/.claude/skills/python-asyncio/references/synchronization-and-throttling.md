# Synchronization Primitives and Throttling

asyncio's synchronization primitives mirror the threading API but are **not thread-safe** -- they coordinate coroutines on a single event loop. Pick by the question being answered.

| Question | Primitive |
|----------|-----------|
| "Only N tasks may be in this region at once." | `Semaphore` / `BoundedSemaphore` |
| "Only one task at a time may run this region." | `Lock` |
| "Wait until something has happened." | `Event` |
| "Wait until a condition is true; multiple waiters." | `Condition` |
| "Hand work between producers and consumers." | `Queue`, `LifoQueue`, `PriorityQueue` |

## 1. `asyncio.Lock`

A mutex: one holder at a time. Useful when a region of async code (one that crosses an `await`) must not interleave with itself.

```python
lock = asyncio.Lock()

async def update_shared_state(...):
    async with lock:
        snapshot = await load_state()
        new = compute(snapshot)
        await store_state(new)
```

When you do *not* need a Lock: a function that mutates shared state without `await`ing in between. The event loop guarantees a single coroutine runs Python bytecode at a time, so a non-await region is already atomic across coroutines.

When you *do* need a Lock: any read-modify-write across an `await` boundary, where another coroutine could modify the same state during the suspension.

## 2. `asyncio.Semaphore` and `BoundedSemaphore`

A counter: at most N concurrent holders.

```python
sem = asyncio.Semaphore(10)

async def call_external(req):
    async with sem:
        return await client.post(URL, json=req)
```

The single most useful asyncio primitive for production code. Almost any code that fans out to an external service should be guarded by a semaphore.

`BoundedSemaphore` raises `ValueError` if released more times than acquired -- catches programming errors. Prefer it unless you need the regular `Semaphore`'s ability to "grow" capacity by extra `release()` calls (rare).

### Sizing guidance

There is no universally correct N. Defaults that travel well:

| Resource | Starting N |
|----------|-----------|
| External HTTP API | 5-20 (or whatever rate limit allows) |
| Internal microservice | 50-100 if you control both ends |
| DB queries (matching pool size) | match the pool size of the async DB driver in use |
| File / disk I/O | 4-16 depending on disk class (SSD vs network FS) |
| CPU-bound (in `to_thread`) | `os.cpu_count()` or a fixed small number |

Then **measure**. Run with two limits, compare latency p50/p95 and error rate. Production-correct N is usually below the point where p99 latency starts to climb.

### Per-resource semaphores

Do not use one global semaphore for everything. Different downstreams have different limits and different failure modes. Concretely, in financial systems:

```python
class RateLimiters:
    venue_a: asyncio.Semaphore = asyncio.Semaphore(20)   # exchange A: 20 req/s
    venue_b: asyncio.Semaphore = asyncio.Semaphore(5)    # exchange B: 5 req/s
    risk_service: asyncio.Semaphore = asyncio.Semaphore(50)
```

### Combine with a token-bucket for *rate* limits

A semaphore bounds *concurrency*, not *rate*. If the venue says "100 requests per second", a semaphore of 100 only enforces that ceiling if each request takes >=1 s. For real rate limiting, layer a token bucket on top -- a small dedicated coroutine that refills tokens on a timer and that callers `await` before proceeding.

## 3. `asyncio.Event`

A one-bit flag: many coroutines wait, one (or any) sets.

```python
shutdown = asyncio.Event()

async def worker():
    while not shutdown.is_set():
        ...
        # alternative: race the work against shutdown
        done, pending = await asyncio.wait(
            [asyncio.create_task(work()), asyncio.create_task(shutdown.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        ...

async def signal_shutdown():
    shutdown.set()
```

Use cases:
- Graceful shutdown signal.
- "Initialization complete; everyone can start" gating.
- Coordinating an async context that should remain open until told otherwise.

`Event.set()` is sticky -- once set, all current and future `wait()` calls return immediately until you `clear()`.

## 4. `asyncio.Condition`

A `Lock` plus a way to wait for a predicate. The standard pattern:

```python
cond = asyncio.Condition()
buffer: list[Item] = []

async def producer(item):
    async with cond:
        buffer.append(item)
        cond.notify()                 # wake one waiter

async def consumer():
    async with cond:
        await cond.wait_for(lambda: buffer)   # waits, releasing the lock; reacquires before returning
        return buffer.pop(0)
```

In practice, `asyncio.Queue` covers most "producer-consumer" use cases more cleanly. Reach for `Condition` when the predicate is more complex than "queue is non-empty".

## 5. `asyncio.Queue`

A FIFO; the work-horse for producer-consumer designs.

```python
q: asyncio.Queue[Item] = asyncio.Queue(maxsize=100)

async def producer():
    for x in source():
        await q.put(x)         # blocks if full; this IS your back-pressure

async def consumer():
    while True:
        x = await q.get()
        try:
            await process(x)
        finally:
            q.task_done()      # required if you ever call q.join()
```

Important behaviors:
- `maxsize=0` (default) is unbounded -- a memory leak waiting to happen. Set a real bound.
- A bounded queue is your back-pressure mechanism. If consumers are slower than producers, `put()` blocks producers, propagating backpressure upstream.
- `q.join()` waits until every `put` has a matching `task_done`. Forgetting `task_done` causes deadlocks.
- For priority work, use `PriorityQueue`. For LIFO, `LifoQueue`.

Cancellation: when a consumer is cancelled while awaiting `q.get()`, no item has been removed, so no `task_done` is needed. When cancelled mid-process, you must decide whether to call `task_done` before re-raising.

## 6. Patterns

### 6.1 Bounded fan-out

The most common pattern in production async code:

```python
async def fan_out_bounded(items, fn, *, limit: int):
    sem = asyncio.Semaphore(limit)

    async def bounded(x):
        async with sem:
            return await fn(x)

    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(bounded(x)) for x in items]
    return [t.result() for t in tasks]
```

Compose this with retries, timeouts, and circuit breakers as needed.

### 6.2 Retry with exponential backoff

```python
async def retry(fn, *, attempts: int = 5, base: float = 0.2):
    for attempt in range(attempts):
        try:
            return await fn()
        except (OSError, asyncio.TimeoutError) as exc:
            if attempt == attempts - 1:
                raise
            delay = base * (2 ** attempt)
            await asyncio.sleep(delay)
```

For production: add jitter (`random.uniform(0, delay)`) so retries do not synchronize across instances. For thundering-herd protection at scale, add full jitter (`random.uniform(0, base * 2**attempt)`).

### 6.3 Circuit breaker

When a downstream is failing, fail fast for a cool-down window rather than piling on:

```python
class Breaker:
    def __init__(self, fail_threshold: int = 5, recover_after: float = 30.0):
        self._fails = 0
        self._opened_at: float | None = None
        self._fail_threshold = fail_threshold
        self._recover_after = recover_after

    async def call(self, coro_factory):
        now = asyncio.get_running_loop().time()
        if self._opened_at is not None and now - self._opened_at < self._recover_after:
            raise RuntimeError("circuit open")
        try:
            result = await coro_factory()
        except Exception:
            self._fails += 1
            if self._fails >= self._fail_threshold:
                self._opened_at = now
            raise
        else:
            self._fails = 0
            self._opened_at = None
            return result
```

### 6.4 Per-key locking

Sometimes you need "only one task at a time per key" -- e.g. invalidating a cache entry, processing a per-user mutation. Use a dict of locks with a guard:

```python
_locks: dict[str, asyncio.Lock] = {}
_guard = asyncio.Lock()

async def lock_for(key: str) -> asyncio.Lock:
    async with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _locks[key] = lock
        return lock

async def update(key, ...):
    lock = await lock_for(key)
    async with lock:
        ...
```

For long-running services, evict old locks periodically to avoid unbounded growth.

## 7. The thread-safety boundary

asyncio synchronization primitives are **not** thread-safe. Locks, Events, Queues, and Futures created by one event loop must not be used from another OS thread. To pass data from a thread into the loop:

- `loop.call_soon_threadsafe(callback, *args)` -- schedule a non-coroutine callback on the loop.
- `asyncio.run_coroutine_threadsafe(coro, loop)` -- schedule a coroutine and get a `concurrent.futures.Future` back.

If you find yourself using these, consider whether the architecture can be cleaned up so the threaded code lives entirely behind `to_thread` / `run_in_executor` and never directly touches the loop.

For the full discussion of crossing thread/loop boundaries -- single-worker executors, dedicated worker threads, transaction-scope serialization, and the common mistakes (using `asyncio.Lock` to protect threaded work, using the default executor for "serialized" calls, queuing statements instead of transactions) -- see `references/thread-safety-and-serialization.md`.
