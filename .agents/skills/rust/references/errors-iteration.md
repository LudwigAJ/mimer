# Error Handling & Pattern Matching & Iterators

## Table of Contents
- Error Handling Basics
- Context-Preserving Errors
- The Three Kinds of Unwrap
- When to Panic vs Return Result
- anyhow vs thiserror
- Pattern Matching
- Iterator Gotchas
- Iteration Patterns for Result & Option

## Error Handling Basics

- **`unwrap()` panics on None/Err** — use `?` operator or `match` in production
- **`?` requires function returns Result/Option** — can't use in main without `-> Result<()>`
- **Converting errors: `map_err()` or `From` trait implementation**
- **`expect("msg")` better than `unwrap()`** — shows context on panic
- **`Option` and `Result` don't mix** — use `.ok()` or `.ok_or()` to convert
- **`?` in closures** — closure must also return Result/Option

## Context-Preserving Errors

Give each important fallible step its own error variant instead of collapsing many failures into one generic case.

```rust
#[derive(thiserror::Error, Debug)]
enum Error {
    #[error("Cannot open file")]
    OpenFile(#[source] std::io::Error),
    #[error("Cannot read file contents")]
    ReadFileContents(#[source] std::io::Error),
}

fn load(path: &Path) -> Result<Vec<u8>, Error> {
    let mut file = File::open(path).map_err(Error::OpenFile)?;
    let mut data = Vec::new();
    file.read_to_end(&mut data).map_err(Error::ReadFileContents)?;
    Ok(data)
}
```

### When many sites share one failure kind, add distinguishing fields
```rust
#[derive(thiserror::Error, Debug)]
enum Error {
    #[error("Cannot bind {2} listener to port {1}")]
    Bind(#[source] std::io::Error, u16, &'static str),
}

http_socket.bind(addr80).map_err(|e| Error::Bind(e, 80, "http"))?;
https_socket.bind(addr443).map_err(|e| Error::Bind(e, 443, "https"))?;
```

### Avoid blanket `From` conversions for application errors
Don't implement `From<SourceError>` for your top-level error when it erases call-site context. Prefer explicit `map_err(Error::Variant)` at each propagation point.

## The Three Kinds of Unwrap

Distinguish the *semantic intent* behind each `.unwrap()`:

**1. Fatal unwrap — "program cannot continue"**
Acceptable at startup or boundary code where failure is terminal:
```rust
let addr: SocketAddr = address_str.parse().unwrap();
let listener = TcpListener::bind(&addr).await.unwrap();
```

**2. Invariant-backed unwrap — "this branch is unreachable by construction"**
The error branch should be impossible if the code is correct:
```rust
static HEX_REGEX: LazyLock<Regex> =
    LazyLock::new(|| Regex::new("^[0-9a-f]*$").unwrap()); // literal pattern, can't be invalid
```

**3. Prototype / TODO unwrap — "real error handling deferred"**
Mark these separately so they can be found and fixed later:
```rust
let age: i32 = user_input.parse().unwrap(); // TODO: handle parse error
```

## When to Panic vs Return Result

- **Panics are last-resort, not normal error reporting** — use `Result`/`Option` for expected failures
- **Local proof makes a panic acceptable** — bounds check adjacent to indexing is OK
- **Non-local "caller must ensure" preconditions are risky** — prefer returning `Option` or `Result`
- **Contain panics at architectural boundaries** — thread boundaries, process supervisors

```rust
// Good: local proof
if i < arr.len() { println!("{}", arr[i]); }

// Better: fallible API
let elem = arr.get(index).ok_or(Error::OutOfBounds)?;

// Risky: non-local invariant
/// Caller must ensure `i < arr.len()` (otherwise will panic)
pub fn foo<T>(arr: &[T], i: usize) -> &T { &arr[i] }
```

### Prefer fallible APIs over panics and silent truncation
```rust
// Good: failure is explicit
fn calculate_total(price: u32, quantity: u32) -> Result<u32, ArithmeticError> {
    price.checked_mul(quantity).ok_or(ArithmeticError::Overflow)
}
let small = i8::try_from(big).map_err(|_| Error::OutOfRange)?;

// Bad: `as` may truncate, unchecked arithmetic may overflow
let y: i8 = x as i8;
let total = price * quantity;
```

## anyhow vs thiserror

**`anyhow` for application code** — flexible error type with context stacking:
```rust
use anyhow::{anyhow, Context};
fn main() -> anyhow::Result<()> {
    let file_name = env::args().nth(1).ok_or_else(|| anyhow!("missing file name"))?;
    let content = fs::read_to_string(&file_name)
        .with_context(|| format!("error reading {}", file_name))?;
    Ok(())
}
```

**`thiserror` for library code** — custom error enum with stable variants for callers to match on:
```rust
#[derive(Debug, thiserror::Error)]
enum MyError {
    #[error("error reading the file: {0}")]
    FileReadError(#[from] std::io::Error),
    #[error("parsing error: {0}")]
    ParsingError(String),
}
```

Use `ok_or_else` over `ok_or` when building the error is non-trivial (lazy vs eager evaluation).

## Composing Option and Result with Combinators

Use `map`, `and_then`, `ok_or`, `map_err` to transform values without nested `match`:

```rust
fn double_arg(mut argv: env::Args) -> Result<i32, String> {
    argv.nth(1)
        .ok_or("Please give at least one argument".to_owned())
        .and_then(|arg| arg.parse::<i32>().map_err(|err| err.to_string()))
        .map(|n| n * 2)
}
```

## Pattern Matching

- **Match must be exhaustive** — use `_` wildcard for remaining cases
- **`if let` for single pattern** — avoids verbose match for one case
- **Guard conditions: `match x { n if n > 0 => ... }`** — guards don't create bindings
- **`@` bindings: `Some(val @ 1..=5)`** — binds matched value to name
- **`ref` keyword in patterns to borrow** — often unnecessary with match ergonomics

## Iterator Gotchas

- **`.iter()` borrows, `.into_iter()` moves, `.iter_mut()` borrows mutably**
- **`.collect()` needs type annotation** — `collect::<Vec<_>>()` or let binding with type
- **Iterators are lazy** — nothing happens until consumed
- **`.map()` returns iterator, not collection** — chain with `.collect()`
- **Modifying while iterating impossible** — collect indices first, then modify
- **`.filter_map()` combines filter and map** — cleaner than chaining
- **`.flatten()` for nested iterators** — `Option` and `Result` are iterators too
- **`.cloned()` vs `.copied()`** — copied for Copy types, cloned calls clone
- **`Iterator` vs `IntoIterator`** — for loops call `into_iter()` automatically

## Iteration Patterns for Result & Option

### Fail-fast: `collect::<Result<Vec<_>, _>>()`
Short-circuits on first `Err`. Same works for `Option`:
```rust
let parsed = ["1", "2", "x"].into_iter()
    .map(|s| s.parse::<u32>())
    .collect::<Result<Vec<_>, _>>(); // Err on "x"
```

### Drop failures: `filter_map(Result::ok)`
Keep only `Ok` values, silently skip errors:
```rust
let values: Vec<_> = ["7", "bad", "9"].into_iter()
    .map(|s| s.parse::<u32>())
    .filter_map(Result::ok)
    .collect(); // [7, 9]
```

### Keep both sides: `partition(Result::is_ok)`
When you need all successes AND all errors:
```rust
let (oks, errs): (Vec<_>, Vec<_>) = ["1", "no", "3"].into_iter()
    .map(|s| s.parse::<u32>())
    .partition(Result::is_ok);
let oks: Vec<_> = oks.into_iter().map(Result::unwrap).collect();
let errs: Vec<_> = errs.into_iter().map(Result::unwrap_err).collect();
```
