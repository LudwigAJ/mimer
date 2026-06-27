# Thread Safety and Serialization

How to safely call non-thread-safe code from async, threaded, and mixed environments. This file is the working reference for picking the right concurrency boundary -- a question that comes up the moment your service has to call any sync library, owns any stateful connection, or manages any transactional resource.

## 1. The core principle

You generally do **not** make an unsafe function thread-safe internally. You place it behind a safe concurrency boundary.

The boundary is one of:

1. An `asyncio.Lock` -- when all callers are coroutines on the **same event loop**.
2. A `threading.Lock` -- when the unsafe function may run on any OS thread but must not run concurrently.
3. A `ThreadPoolExecutor(max_workers=1)` -- when the unsafe resource should be serialized through a single worker thread.
4. A dedicated worker thread + `queue.Queue` (the actor pattern) -- when the unsafe resource is stateful, transactional, or thread-affine.
5. `loop.call_soon_threadsafe()` / `asyncio.run_coroutine_threadsafe()` -- when code on another OS thread must safely interact with the event loop.

The guiding rule for transactional resources is stronger:

> Send whole transaction jobs to one owning worker, rather than allowing multiple threads to interleave individual statements.

## 2. The two questions that pick the boundary

Concurrency safety has two separate questions:

1. **Can this function run at the same time as itself?** -- determines whether you need mutual exclusion.
2. **Does this function or resource need to always run on the same OS thread?** -- determines whether you need thread affinity.

These lead to different solutions:

| Situation | Pattern |
|-----------|---------|
| Only async tasks on one event loop access the resource | `asyncio.Lock` |
| Multiple OS threads may call it, but any thread is okay | `threading.Lock` |
| Calls must be serialized and preferably run on one worker thread | `ThreadPoolExecutor(max_workers=1)` |
| Stateful or transactional resource, e.g. a DB connection | Dedicated worker thread + queue |
| A background thread needs to notify the event loop | `loop.call_soon_threadsafe()` |
| A background thread needs to submit a coroutine | `asyncio.run_coroutine_threadsafe()` |

## 3. Pattern: `asyncio.Lock` for same-loop coroutine serialization

Use this only when all access happens inside the same event loop.

```python
import asyncio

lock = asyncio.Lock()

async def safe_call(*args, **kwargs):
    async with lock:
        return unsafe_nonblocking_function(*args, **kwargs)
```

When this is appropriate:
- The unsafe state is touched only by coroutines.
- No worker threads touch the same object.
- The unsafe function does not block for long.
- You only need to prevent coroutine-level interleaving across awaits.

When this is not enough:
- Do **not** use `asyncio.Lock` to protect work running in `asyncio.to_thread` or in any executor. `asyncio.Lock` is not thread-safe -- it coordinates coroutines on one loop, not OS threads.
- If OS threads are involved, use `threading.Lock`, a queue, or a single owning worker.

## 4. Pattern: `threading.Lock` around a blocking unsafe function

Use this when the function is unsafe only because it must not be called concurrently, but it is otherwise fine for any OS thread to call it.

```python
import asyncio
import threading

unsafe_lock = threading.Lock()

def unsafe_call_we_do_not_control(*args, **kwargs):
    ...

def safe_sync_call(*args, **kwargs):
    with unsafe_lock:
        return unsafe_call_we_do_not_control(*args, **kwargs)

async def safe_async_call(*args, **kwargs):
    return await asyncio.to_thread(safe_sync_call, *args, **kwargs)
```

What this gives you:
- The event loop stays responsive (the blocking work happens in a worker thread).
- Only one thread enters the unsafe function at a time.

When to use:
- The unsafe function is blocking.
- It can run on any thread.
- It just must not run concurrently.
- It has no thread affinity.

Caveat: this guarantees mutual exclusion, but **not** that calls always happen on the same worker thread. If the unsafe object must always be accessed from the same thread (thread-local state, connection-bound resources), use a single-worker executor or a dedicated worker thread instead.

## 5. Pattern: single-worker `ThreadPoolExecutor`

Use this when you want all calls to go through one serialized worker thread.

```python
import asyncio
import concurrent.futures
import functools

unsafe_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="unsafe-resource",
)

def unsafe_call_we_do_not_control(*args, **kwargs):
    ...

async def call_unsafe_serially(*args, **kwargs):
    loop = asyncio.get_running_loop()
    fn = functools.partial(unsafe_call_we_do_not_control, *args, **kwargs)
    return await loop.run_in_executor(unsafe_executor, fn)
```

`ThreadPoolExecutor(max_workers=1)` guarantees jobs execute one at a time on a single worker thread. Useful when:
- The resource is not thread-safe.
- The resource is stateful (a connection, a session, a global).
- You want serialized execution and a stable owning thread.
- You want a simple async wrapper around a sync API.

### Two warnings

**Do not** let code running inside the single worker submit more work to the *same* single-worker executor and wait for it. The submitted job needs the worker thread to run, but the worker thread is busy waiting for the result, so it deadlocks:

```python
# Dangerous: code already running on the only worker thread submits
# another job to the same executor and waits for it.
def inner():
    fut = unsafe_executor.submit(more_work)
    return fut.result()    # deadlock: this thread IS the executor

unsafe_executor.submit(inner).result()
```

**Do not** assume `loop.run_in_executor(None, ...)` serializes anything:

```python
await loop.run_in_executor(None, unsafe_call)   # NOT serialized
```

Passing `None` uses the loop's *default* executor, which has multiple worker threads. Multiple awaiting coroutines will run their `unsafe_call`s concurrently on different threads. To serialize, you must pass your own executor created with `max_workers=1`.

### Reusable wrapper

```python
class SingleThreadAsyncWrapper:
    def __init__(self, fn, name="single-thread-wrapper"):
        self._fn = fn
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=name,
        )

    async def __call__(self, *args, **kwargs):
        loop = asyncio.get_running_loop()
        bound = functools.partial(self._fn, *args, **kwargs)
        return await loop.run_in_executor(self._executor, bound)

    def close(self):
        self._executor.shutdown(wait=True)
```

Usage:

```python
safe_write = SingleThreadAsyncWrapper(write_to_database)
await safe_write(record)
```

## 6. Pattern: dedicated worker thread (the actor pattern)

For stateful or transactional resources, the cleanest design gives one worker thread *full ownership* of the resource. Callers send jobs in; the worker processes them in order.

```python
import asyncio
import threading
import queue

class TransactionWorker:
    def __init__(self, make_connection):
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            args=(make_connection,),
            daemon=True,
        )
        self._thread.start()

    def _run(self, make_connection):
        conn = make_connection()
        try:
            while True:
                job = self._jobs.get()
                if job is None:                  # sentinel for shutdown
                    return
                fn, set_result, set_exception = job
                try:
                    result = fn(conn)
                except BaseException as exc:
                    set_exception(exc)
                else:
                    set_result(result)
        finally:
            conn.close()

    async def run(self, fn):
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def set_result(result):
            loop.call_soon_threadsafe(future.set_result, result)

        def set_exception(exc):
            loop.call_soon_threadsafe(future.set_exception, exc)

        self._jobs.put((fn, set_result, set_exception))
        return await future

    def close(self):
        self._jobs.put(None)
        self._thread.join()
```

Usage:

```python
worker = TransactionWorker(make_connection)

async def write_record(record):
    return await worker.run(lambda conn: conn.execute(INSERT_SQL, record))
```

This shape gives you:
- One owner of the unsafe resource.
- Serialized access -- zero interleaving.
- A clear concurrency boundary, easy to reason about.
- An async-friendly facade over a sync API.
- A natural place to add retries, batching, logging, metrics, and graceful shutdown.

The thread-safety bridge here is `loop.call_soon_threadsafe(future.set_result, ...)` -- the worker thread cannot directly call `future.set_result` because asyncio Futures are not thread-safe.

## 7. Pattern: queue *whole transactions*, not individual statements

For databases or any multi-step stateful resource, the *unit* of serialization matters as much as the serialization itself. A queue of single statements does not protect a multi-statement transaction.

Bad:

```python
await db_worker.run(lambda conn: conn.execute("BEGIN"))
await db_worker.run(lambda conn: conn.execute("INSERT INTO orders ..."))
await db_worker.run(lambda conn: conn.execute("INSERT INTO order_items ..."))
await db_worker.run(lambda conn: conn.execute("COMMIT"))
```

Why this is dangerous:
- Other callers may enqueue work *between* your statements.
- Transactions from different callers interleave.
- A `ROLLBACK` may affect the wrong logical operation.
- The queue serializes statements; it does not serialize business transactions.

Good:

```python
async def create_order(order, items):
    def transaction(conn):
        conn.execute("BEGIN")
        try:
            conn.execute("INSERT INTO orders ...", order)
            for item in items:
                conn.execute("INSERT INTO order_items ...", item)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return await db_worker.run(transaction)
```

The rule:

> If correctness depends on multiple operations being atomic, enqueue the whole atomic unit -- not the individual operations.

This applies beyond databases: any resource where state must move through several steps without interference (file rename + checksum, two-phase commit to an external system, a stateful protocol exchange) needs the same shape.

## 8. Pattern: `loop.call_soon_threadsafe` -- crossing back into the loop

If a background thread needs to notify or wake an event loop, it must not directly mutate loop-owned state (Tasks, Futures, Events, Queues). Use `loop.call_soon_threadsafe` to schedule a callback on the loop:

```python
import asyncio
import threading
import time

async def main():
    loop = asyncio.get_running_loop()
    done = asyncio.Event()

    def background_thread():
        time.sleep(1)
        loop.call_soon_threadsafe(done.set)   # safe across threads

    threading.Thread(target=background_thread, daemon=True).start()
    await done.wait()
```

Use this when:
- A normal thread needs to wake or notify the event loop.
- A worker thread has finished work and needs to signal an async caller.
- You need to safely schedule a callback from outside the loop's thread.

The callback runs on the loop thread, in the loop's normal scheduling, so it can touch loop-owned state safely.

## 9. Pattern: `asyncio.run_coroutine_threadsafe` -- submitting coroutines from a thread

If non-loop-thread code needs to *run a coroutine* on an existing loop:

```python
future = asyncio.run_coroutine_threadsafe(coro(), loop)
result = future.result(timeout=5)             # blocks the calling thread
```

Returns a `concurrent.futures.Future` (not an `asyncio.Future`), suitable for synchronous waiting.

Use this when:
- A worker thread must invoke async code that lives on the loop.
- You already have a running loop and a reference to it.

Warning: do not call `.result()` from a thread that the coroutine itself depends on. If the coroutine awaits something that is supposed to be set by the very thread that is now blocked in `.result()`, you have a deadlock. This is the same shape as Pattern 5's "do not submit-and-wait from inside the only worker".

## 10. Decision tree

When you need to call an unsafe resource:

1. **Is all access from coroutines on a single event loop?**
   - Yes: `asyncio.Lock`. Done.
   - No: continue.
2. **Are OS threads involved?** (`to_thread`, `run_in_executor`, threads outside asyncio)
   - Yes: do not rely on `asyncio.Lock`. Continue.
3. **Can the unsafe function run on any thread, as long as calls do not overlap?**
   - Yes: `threading.Lock` + `asyncio.to_thread` is enough.
   - No (resource is thread-affine, stateful, or holds connection-local state): continue.
4. **Does the unsafe resource need one owning thread?**
   - Yes: `ThreadPoolExecutor(max_workers=1)`, or a dedicated worker thread + queue.
5. **Are there multi-step transactions whose atomicity matters?**
   - Yes: queue whole transaction functions (Pattern 7). Do **not** queue individual statements unless interleaving is harmless.

## 11. Recommended starting designs

### Simple mutual exclusion wrapper (any thread, but one at a time)

```python
import asyncio
import threading

class AsyncSerializedFunction:
    def __init__(self, fn):
        self._fn = fn
        self._lock = threading.Lock()

    def _call_sync(self, *args, **kwargs):
        with self._lock:
            return self._fn(*args, **kwargs)

    async def __call__(self, *args, **kwargs):
        return await asyncio.to_thread(self._call_sync, *args, **kwargs)
```

### Single-thread async wrapper (one owning thread)

See Pattern 5 above (`SingleThreadAsyncWrapper`).

### Transaction worker (whole-transaction serialization)

See Pattern 6 above (`TransactionWorker`). Expose `run_transaction(fn)` where `fn(conn)` performs the entire atomic unit.

## 12. Common mistakes

### Mistake: assuming asyncio means single-threaded safety

Even if your event loop is single-threaded, `asyncio.to_thread` and `run_in_executor` introduce real OS threads. Code that was safe under "only one coroutine at a time" can race the moment it is wrapped in `to_thread`.

### Mistake: using `asyncio.Lock` to protect threaded work

`asyncio.Lock` coordinates coroutine scheduling on one loop. It does not synchronize OS threads. Using it around code in `to_thread` provides no protection.

### Mistake: using the default executor for unsafe calls

```python
await loop.run_in_executor(None, unsafe_call)   # NOT serialized
```

The default executor has multiple worker threads. Multiple in-flight coroutines will run their `unsafe_call`s concurrently. Pass your own `ThreadPoolExecutor(max_workers=1)`.

### Mistake: serializing statements instead of transactions

Per-statement serialization protects each statement individually but does nothing for atomicity across statements. Other callers can interleave their work between yours. Queue whole transaction functions.

### Mistake: submit-and-wait from inside the only worker

Submitting work to the same single-worker executor *from* code already running on that worker, and then waiting for it, deadlocks. The worker is busy waiting for itself. The same shape applies to `run_coroutine_threadsafe(...).result()` from a thread the coroutine depends on.

### Mistake: too coarse, or too fine, a lock

A single global lock is sometimes correct -- but it is sometimes too broad (everything serializes through one bottleneck) or too narrow (the lock protects one call but not a multi-step operation). Ask:

- Is the unsafe state global?
- Per connection?
- Per object?
- Per file?
- Per transaction?
- Does the object require thread affinity?

The answer drives the boundary: per-connection lock vs per-object lock vs single-owner worker.

### Mistake: ignoring shutdown

Worker threads and executors must be shut down cleanly:

```python
executor.shutdown(wait=True)
```

For dedicated worker threads with a queue, send a sentinel value (`None` or a unique object) and `join()` the thread. Without explicit shutdown, the program may hang or lose buffered work on exit.

### Mistake: blocking the event loop with sync calls "just for now"

```python
async def handler():
    write_to_database(record)        # blocks the loop for the entire DB call
```

It is tempting to "just call the sync function" because everything seems to work in development. Under load, the entire service stalls each time a DB call is made. Put it behind a serialized boundary:

```python
async def handler():
    await safe_write(record)
```

## 13. Final takeaway

> Own unsafe resources in one place, serialize all access to them, and expose an async-friendly API around that serialized boundary.

For simple cases, a `threading.Lock` around `asyncio.to_thread` is enough. For stateful or transactional resources, prefer a single owner: a one-worker executor, a dedicated worker thread with a queue, or a queue-whole-transactions facade. The wins are the same in every case: the unsafe code is unchanged, the rest of the system is unaware of the boundary, and the failure modes are confined to one well-named location.
