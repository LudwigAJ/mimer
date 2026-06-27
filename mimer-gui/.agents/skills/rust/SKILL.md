---
name: rust
description: Write idiomatic, production-quality Rust — avoiding ownership pitfalls, lifetime confusion, borrow checker battles, and common design mistakes. Use this skill whenever writing, reviewing, refactoring, or debugging Rust code. Covers ownership & borrowing, strings & types, error handling, iterators, concurrency, async patterns, API design, newtypes, state machines, defensive programming, pattern matching, documentation, and performance. Trigger on any Rust-related question, even if the user doesn't explicitly mention a specific topic — if they're writing Rust, this skill helps.
---

## Quick Reference

| Topic | File | Key Content |
|-------|------|-------------|
| Ownership & Borrowing | `references/ownership-borrowing.md` | Moves, borrows, lifetimes, immutability |
| Strings & Types | `references/types-strings.md` | `String`/`&str`, newtypes, enums over bools, compile-time invariants |
| Errors & Iteration | `references/errors-iteration.md` | `?` operator, `anyhow`/`thiserror`, context-preserving errors, unwrap semantics, Result/Option iteration |
| Concurrency & Memory | `references/concurrency-memory.md` | Send/Sync, Arc/Mutex, smart pointers, async traps |
| Advanced Traps | `references/advanced-traps.md` | unsafe, macros, FFI, testing, performance |
| API Design | `references/api-design.md` | Library APIs, conversion traits, type-state, generics, documentation |
| Idiomatic Patterns | `references/idiomatic-patterns.md` | State machines, defensive programming, pattern matching, arena trees, Option chaining |
| Async Patterns | `references/async-patterns.md` | Structured concurrency, cancellation safety, async/await idioms, rayon parallelism |

Read the relevant reference file before writing code on that topic. When multiple topics intersect (e.g. async + error handling), read both reference files.

---

## Critical Traps (High-Frequency Failures)

### Ownership — #1 Source of Compiler Errors
- **Variable moved after use** — clone explicitly or borrow with `&`
- **`for item in vec` moves vec** — use `&vec` or `.iter()` to borrow
- **`String` moved into function** — pass `&str` for read-only access
- **Partial moves in structs** — moving one field makes whole struct unusable

### Borrowing — The Borrow Checker Always Wins
- **Can't have `&mut` and `&` simultaneously** — restructure or interior mutability
- **Returning reference to local fails** — return owned value instead
- **Mutable borrow through `&mut self` blocks all access** — split struct or `RefCell`
- **Make mutation explicit at the API boundary** — `&mut T` should advertise side effects

### Lifetimes — When Compiler Can't Infer
- **`'static` means CAN live forever, not DOES** — `String` is `'static`-capable
- **Struct with reference needs `<'a>`** — `struct Foo<'a> { bar: &'a str }`
- **Function returning ref must tie to input** — `fn get<'a>(s: &'a str) -> &'a str`
- **Name lifetimes descriptively** — `'me`, `'prov`, `'auth` document what a borrow represents

### Strings — UTF-8 Surprises
- **`s[0]` doesn't compile** — use `.chars().nth(0)` or `.bytes()`
- **`.len()` returns bytes, not chars** — use `.chars().count()`
- **`s1 + &s2` moves s1** — use `format!("{}{}", s1, s2)` to keep both
- **Take `&str` for read-only params, `String` for ownership** — see `references/types-strings.md`

### Error Handling — Production Code
- **`unwrap()` panics** — use `?` or `match` in production
- **`?` needs `Result`/`Option` return type** — main needs `-> Result<()>`
- **`expect("context")` > `unwrap()`** — shows why it panicked
- **Avoid blanket `From` conversions** — they erase call-site context, use `map_err` instead
- **`anyhow` for apps, `thiserror` for libraries** — see `references/errors-iteration.md`

### Iterators — Lazy Evaluation
- **`.iter()` borrows, `.into_iter()` moves** — choose carefully
- **`.collect()` needs type** — `collect::<Vec<_>>()` or typed binding
- **Iterators are lazy** — nothing runs until consumed
- **`collect::<Result<Vec<_>, _>>()` short-circuits** — transposes iterator of Results

### Concurrency — Thread Safety
- **`Rc` is NOT `Send`** — use `Arc` for threads
- **`Mutex` lock returns guard** — auto-unlocks on drop, don't hold across `.await`
- **`RwLock` deadlock** — reader upgrading to writer blocks forever
- **Async cancellation drops the future** — split into reservation + commit phases

### Memory — Smart Pointers
- **`RefCell` panics at runtime** — if borrow rules violated
- **`Box` for recursive types** — compiler needs known size
- **Avoid `Rc<RefCell<T>>` spaghetti** — use arena + index-based links instead

### API Design — Library Quality
- **Replace stringly-typed parameters with enums** — compile-time guidance over runtime parsing
- **Use enums instead of booleans for flags** — self-documenting call sites
- **Start concrete, generalize only when needed** — premature generics hurt readability
- **Validate at construction with newtypes** — parse, don't validate

---

## Common Compiler Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `value moved here` | Used after move | Clone or borrow |
| `cannot borrow as mutable` | Already borrowed | Restructure or RefCell |
| `missing lifetime specifier` | Ambiguous reference | Add `<'a>` |
| `the trait bound X is not satisfied` | Missing impl | Check trait bounds |
| `type annotations needed` | Can't infer | Turbofish or explicit type |
| `cannot move out of borrowed content` | Deref moves | Clone or pattern match |

---

## Cargo Traps

- **`cargo update` updates Cargo.lock, not Cargo.toml** — manual version bump needed
- **Features are additive** — can't disable a feature a dependency enables
- **`[dev-dependencies]` not in release binary** — but in tests/examples
- **`cargo build --release` much faster** — debug builds are slow intentionally
- **Debug vs Release is 10-100x** — always benchmark in release mode

---

## Tooling Essentials

- **`Cargo.toml`** — project manifest: dependencies, features, edition, build profiles; `Cargo.lock` pins exact versions
- **`cargo build`** — compile the project; add `--release` for optimized builds
- **`cargo check`** — type-check without producing a binary — much faster feedback loop than `build`
- **`cargo test`** — run unit tests, doc tests, and integration tests (`tests/` dir); `-- --nocapture` to see stdout
- **`cargo fmt`** — auto-format code via `rustfmt`; run before committing to keep style consistent
- **`cargo clippy`** — lint for common mistakes and non-idiomatic code; treat warnings seriously
- **`rust-analyzer`** — LSP server for IDE support (completions, go-to-definition, inline errors); install via editor plugin or `rustup component add rust-analyzer`

---

## Design Principles (from reference articles)

1. **Aim for immutability** — derive values on demand instead of caching shared mutable state
2. **Be simple** — prefer concrete types over speculative generics; optimize after measuring
3. **Parse, don't validate** — newtypes with private fields enforce invariants at construction
4. **Encode state in types** — type-state patterns make invalid transitions unrepresentable
5. **Defensive exhaustiveness** — match slices/enums explicitly; destructure structs fully to catch new fields
6. **Context-preserving errors** — each fallible step gets its own error variant, not a generic catch-all
7. **Cancel-safe async** — split work into reservation (cancel-safe) and commit (infallible) phases
