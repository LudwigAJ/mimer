# Async Patterns & Parallelism

## Table of Contents
- Why Async/Await Over Threads
- Structured Concurrency
- Cancellation Safety
- Current-Thread / Thread-Per-Core Runtimes
- Rayon Data Parallelism

## Why Async/Await Over Threads

### Async makes fallible async code read like ordinary Rust
Replace `Future` combinator chains with `async fn`, `.await`, and `?`:

```rust
async fn handle_get_counters(&self, p: &mut P::Deserializer) -> Result<EncodedFinal<P>, Error> {
    let args = parse_args(p)?;                    // fallible sync
    let res = self.service.get_counters(args).await?;  // async
    let enc = write_message(p, "getCounters", MessageType::Reply, |p| res.write(p))?;
    Ok(enc)
}
```

### Native control flow works in async code
`if`, `while`, `match` work directly — no need for combinator-driven state machines:

```rust
while keep_going().await {
    do_the_thing().await?;
}
```

### Timeouts by racing futures
Wrap real I/O in one future, create a timeout future, then race them:

```rust
async fn handle_client(client: TcpStream) -> io::Result<()> {
    let driver = async move {
        let mut data = vec![];
        client.read_to_end(&mut data).await?;
        let response = do_something_with_data(data).await?;
        client.write_all(&response).await?;
        Ok(())
    };
    let timeout = async {
        Timer::after(Duration::from_secs(3)).await;
        Err(io::ErrorKind::TimedOut.into())
    };
    driver.race(timeout).await
}
```

## Structured Concurrency

### Static: `try_join!` for fixed future sets
When you know the exact number of async operations up front:

```rust
let image = storage::load_profile_image(user.id);
let profile = db::load_profile(user.id);
let (image, profile) = futures::try_join!(image, profile)?;
```

No task spawning overhead. No heap allocation for the fixed set.

### Dynamic: scoped tasks for runtime-determined task sets
Spawn child futures inside an explicit async scope so they cannot outlive the parent. Child tasks can borrow local state instead of forcing `Arc` and `'static`:

```rust
let value = RwLock::new(22);
moro::async_scope!(|scope| {
    scope.spawn(async {
        *value.write().unwrap() *= 2;
    });
}).await;
```

Errors bubble up through the scope instead of disappearing into detached background work.

## Cancellation Safety

Rust futures are passive state machines. Cancellation is usually just dropping the future. Parent futures own child futures, so cancellation propagates non-locally.

### Split into cancel-safe reservation + infallible commit
Separate waiting for capacity from the irreversible act:

```rust
// Good: cancel-safe — timeout can cancel the wait without losing the message
loop {
    match timeout(Duration::from_secs(5), tx.reserve()).await {
        Ok(Ok(permit)) => {
            permit.send(next_message());
            println!("sent successfully");
        }
        Ok(Err(_)) => return,
        Err(_) => println!("no space for 5 seconds"),
    }
}

// Bad: cancel-unsafe — dropping loses `msg`
match timeout(Duration::from_secs(5), tx.send(msg)).await { /* ... */ }
```

### Keep mutex state valid across every `.await`
Don't suspend while shared state is in a temporary invalid state:

```rust
// Good: invariant broken and restored between polls
let mut guard = mutex.lock().await;
let data = guard.data.take();
let new_data = process_data(data);    // no .await here
guard.data = Some(new_data);

// Bad: cancellation at await leaves guard.data == None
let mut guard = mutex.lock().await;
let data = guard.data.take();
let new_data = process_data(data).await;  // cancellation hazard
guard.data = Some(new_data);
```

### Preserve in-flight futures in `select!` loops
Pin a future to resume the same operation instead of cancelling it repeatedly:

```rust
let mut reserve_fut = Box::pin(channel.reserve());
loop {
    tokio::select! {
        permit = &mut reserve_fut => break permit,
        _ = other_condition() => continue,
    }
}
```

For work that must finish even if the caller disappears, move it into a spawned task:
```rust
tokio::spawn(handle_request(req));
```

## Current-Thread / Thread-Per-Core Runtimes

Keep futures pinned to the thread that created them to avoid `Send` bounds:

```rust
#[derive(Default)]
struct Context {
    db: Database,
    service_b: ServiceB,
    service_c: ServiceC,
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), ()> {
    let context = Context::default();
    let b = context.service_b.do_something_else();
    let c = context.service_c.do_something_else_else();
    let (_b, _c) = futures::try_join!(b, c)?;
    Ok(())
}
```

Code becomes simpler because ordinary references work again. No `Arc`, fewer `Mutex`, less `async move` boilerplate. Ecosystem support is still uneven — many higher-level frameworks default to `Send + 'static` handler assumptions.

## Rayon Data Parallelism

### `rayon::join` for divide-and-conquer
Takes two closures that *may* run in parallel. Rayon treats this as a hint, not a guarantee:

```rust
fn quick_sort<T: PartialOrd + Send>(v: &mut [T]) {
    if v.len() > 1 {
        let mid = partition(v);
        let (lo, hi) = v.split_at_mut(mid);
        rayon::join(|| quick_sort(lo), || quick_sort(hi));
    }
}
```

### Safety comes from Rust's normal rules
- `join` requires closures that are `Send` — captured state must be thread-safe
- Split mutable data into disjoint regions before spawning parallel work (`split_at_mut`)
- `Rc` in both closures won't compile — it's not `Send`/`Sync`

### Split-until-small, then run sequentially
Stop splitting once the chunk is small enough. Pushing parallel work all the way down to tiny chunks adds overhead:

```rust
fn process(shared: &Shared, state: &mut [Item]) {
    if state.len() > THRESHOLD {
        let mid = state.len() / 2;
        let (left, right) = state.split_at_mut(mid);
        rayon::join(|| process(shared, left), || process(shared, right));
    } else {
        for item in state { process_item(shared, item); }
    }
}
```

### Deadlock warning
The closures in `join` must not depend on each other making progress. Using a channel to coordinate the two closures can deadlock:

```rust
// DEADLOCK: second closure waits for first to send
rayon::join(
    || tx.send(1).unwrap(),
    || rx.recv().unwrap(),
);
```
