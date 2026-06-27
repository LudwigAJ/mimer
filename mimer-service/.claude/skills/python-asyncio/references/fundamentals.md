# Asyncio Fundamentals

This file is the deep-dive companion to Section 1 of `SKILL.md`. Read it when you need to reason carefully about *why* something does or does not work, not just *how* to write the pattern.

## 1. The four core abstractions

asyncio's surface area is small once you separate the four core abstractions.

### 1.1 The event loop

The event loop is a single-threaded scheduler. Its main loop is roughly:

1. Pick a ready job from the queue.
2. Run it until it returns or hits an `await` that suspends.
3. If it suspended on I/O, register the I/O with the OS (via `epoll`/`kqueue`/IOCP) so the loop can wake the job when ready.
4. Service any I/O the OS reports as ready -> mark those jobs ready.
5. Repeat. If nothing is ready, sleep on the OS poll until something is.

There is exactly one event loop per thread. In modern code you should not touch it directly -- `asyncio.run(main())` creates one, runs your top coroutine, and shuts it down.

### 1.2 Coroutine functions and coroutine objects

`async def` declares a **coroutine function**. *Calling* it does not execute the body. It returns a **coroutine object**, which is a paused, resumable computation.

```python
async def f():
    print("ran")

c = f()                 # nothing prints; c is a coroutine object
await c                 # now it prints
```

This is the single most common source of bugs in async code. If you call a coroutine function and discard the return value, Python emits `RuntimeWarning: coroutine '...' was never awaited` and the work never happens.

### 1.3 Tasks

A `Task` is a coroutine wrapped so the event loop can run it independently. Creating a task schedules it -- it begins running at the next loop iteration without you having to `await` it.

```python
task = asyncio.create_task(f())   # scheduled to run; can be awaited later
```

A task can be in one of these states: pending, running, done (with a result), done (with an exception), or cancelled.

Things you can do with a task:
- `await task` -- get the result (or re-raise the exception).
- `task.cancel()` -- request cancellation. The task receives `CancelledError` at its next `await`.
- `task.done()`, `task.cancelled()`, `task.exception()`, `task.result()` -- inspection.
- `task.add_done_callback(cb)` -- register a callback.

**Critical:** the event loop only holds a *weak* reference to scheduled tasks. If you do not retain a strong reference to a Task and your local goes out of scope, the garbage collector can destroy it mid-flight. Either store it (a `set` is the canonical container, with `add_done_callback(s.discard)`), or use a `TaskGroup` which owns its tasks.

### 1.4 Futures

A `Future` is a low-level placeholder for a result that will be set later. Tasks are a subclass of Future.

You almost never create a Future directly in application code. They surface when you bridge to callback-based APIs or build custom synchronization primitives. If you find yourself reaching for `loop.create_future()`, ask whether `asyncio.Event` or `asyncio.Queue` would do the job.

## 2. What `await` actually does

`await x` does three things:

1. Asks `x` for an iterator over its waiting points (this is what makes `x` "awaitable").
2. Runs the iterator one step. If `x` yields, `await` suspends the current coroutine and bubbles the suspension all the way up the call stack to the event loop.
3. The event loop schedules other ready jobs. When the suspension reason is resolved (I/O ready, timer expired, future result set), the loop resumes the awaiting coroutine, and `await x` finally produces the value.

The control-transfer detail matters:

- **`await coroutine` does not, by itself, transfer to other tasks.** It transfers control only when the awaited coroutine itself suspends. A coroutine that never `awaits` anything (or only awaits things that complete synchronously) hogs the loop just as effectively as a `while True` loop.
- **`await asyncio.sleep(0)` is the canonical "yield to scheduler" trick** -- useful in long-running CPU-ish loops to let other tasks run, though if you find yourself reaching for it often the work probably belongs in an executor.

## 3. Awaitables: the duck type behind `await`

Three things satisfy "awaitable":

1. **Coroutine objects.** Returned by calling an `async def`.
2. **Tasks** (and Futures, since Task subclasses Future). Awaiting a task means "wait for it to finish, give me its result."
3. **Objects with an `__await__` method** that returns an iterator. Used by libraries to integrate with the loop -- you usually do not write these.

A subtlety: awaiting a coroutine object runs *that coroutine to completion inline* (modulo its own suspensions). It does not give you concurrency. To get concurrency, wrap each coroutine in a `Task` (via `create_task` or `TaskGroup`) so the loop can interleave them.

```python
# Sequential. Both run; the second only starts after the first completes.
a = await fetch(1)
b = await fetch(2)

# Concurrent.
ta = asyncio.create_task(fetch(1))
tb = asyncio.create_task(fetch(2))
a = await ta
b = await tb

# Cleaner concurrent (3.11+).
async with asyncio.TaskGroup() as tg:
    ta = tg.create_task(fetch(1))
    tb = tg.create_task(fetch(2))
a, b = ta.result(), tb.result()
```

## 4. The "sequential async trap"

The most insidious newcomer mistake -- and one that survives code review -- is async-flavoured sequential code:

```python
async def fetch_all(ids: list[int]) -> list[Data]:
    out = []
    for i in ids:
        out.append(await fetch_one(i))   # awaiting in a loop = sequential
    return out
```

This has the syntax of async, performs zero concurrency, and runs in the same wall time as the synchronous version. The compiler does not warn you. The fix is to gather (or TaskGroup) all of `[fetch_one(i) for i in ids]` at once.

Look for this pattern in code review: any `for` / `while` that contains a single `await` of an I/O call is a candidate for parallelization.

## 5. The single-thread invariant and what it implies

asyncio guarantees that, within a single event loop, only one coroutine is executing Python bytecode at any moment. This is a powerful guarantee:

- You do not need a `Lock` to protect a non-await critical section. If a function never `await`s, it is atomic with respect to other coroutines.
- You *do* need a `Lock` to protect a critical section that spans an `await`, because between the `await` and its resumption, other coroutines run.
- `asyncio.Queue.put_nowait` / `get_nowait` are atomic for the same reason.
- Mutating a shared list/dict between awaits is a footgun; another coroutine may have mutated it while you were suspended.

Threads break this invariant. asyncio synchronization primitives (`asyncio.Lock`, etc.) are **not thread-safe**. To pass data from a thread into the loop, use `loop.call_soon_threadsafe(...)` or `asyncio.run_coroutine_threadsafe(coro, loop)`.

## 6. Debug mode

Run with `asyncio.run(main(), debug=True)` during development. It enables:

- `RuntimeWarning` -> error on unawaited coroutines.
- Logging of "slow callbacks" (>100ms by default; configurable via `loop.slow_callback_duration`). A slow callback is a strong signal that you have CPU-bound or sync work blocking the loop.
- Logging when a task takes too long to be cancelled.
- More verbose tracebacks pointing at the source of unhandled task exceptions.

You can set `PYTHONASYNCIODEBUG=1` instead of changing code.
