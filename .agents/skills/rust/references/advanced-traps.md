# Advanced Traps — unsafe, macros, FFI, testing, performance, documentation

## Unsafe Code

- **`unsafe` doesn't disable borrow checker** — only allows 5 specific operations
- **Raw pointers `*const T` / `*mut T`** — can be null, dangling, or aliased
- **`unsafe impl` for Send/Sync** — you guarantee invariants compiler can't check
- **`transmute` is nuclear** — reinterprets bits, can cause UB easily
- **Undefined behavior is NEVER acceptable** — even if "it works on my machine"

## Macro Pitfalls

- **`macro_rules!` hygiene** — identifiers don't leak, but paths can be tricky
- **Macro expansion order** — can cause surprising errors
- **`$crate` for paths in macros** — ensures correct crate resolution
- **Proc macros need separate crate** — `proc-macro = true` in Cargo.toml
- **Debug macros with `cargo expand`** — see what code actually generates
- **`stringify!` and `concat!`** — compile-time string operations

## FFI Issues

- **`#[repr(C)]` for C-compatible layout** — Rust default layout is unspecified
- **Null-terminated strings** — `CString` / `CStr` not `String` / `&str`
- **`extern "C"` for C ABI** — Rust ABI is unstable
- **Ownership across FFI** — who frees what? Document clearly
- **Panics across FFI boundary** — undefined behavior, use `catch_unwind`
- **`Option<&T>` is nullable pointer** — FFI-safe optimization

## Testing Traps

- **`#[cfg(test)]` module not in release** — but dependencies still compile
- **`assert_eq!` shows both values** — better than `assert!(a == b)`
- **`#[should_panic]` for panic tests** — can specify `expected = "message"`
- **`Result<(), E>` return in tests** — use `?` in test functions
- **Integration tests in `tests/`** — separate compilation, external API only
- **`cargo test -- --nocapture`** — to see println! output

## Performance Traps

- **`.clone()` is not free** — deep copy for most types
- **String allocation on every `format!`** — reuse buffers with `write!`
- **`Vec` reallocation** — use `with_capacity()` if size known
- **Iterator vs loop** — usually same perf, but check with `cargo bench`
- **`Box<dyn Trait>` indirection** — generics are faster if possible
- **`#[inline]` across crates** — needed for cross-crate inlining
- **Debug vs Release** — 10-100x difference, always bench in release

## Documentation Best Practices

### Crate-level front page with `//!` docs
Use `//!` at the top of `lib.rs` as the entry point: explain the crate's role, show a realistic starting example, and document major features.

### Per-item docs follow a predictable shape
One-line summary → more detail → example → `# Panics` / `# Errors` sections when needed. The first paragraph should stand on its own because rustdoc reuses it in search.

```rust
/// Returns a new [`String`] with `s` appended.
///
/// # Panics
/// Panics if `s` is empty.
///
/// # Example
/// ```
/// let s = MyType::new("hello ");
/// assert_eq!("hello Georges", s.concat_str("Georges").as_str());
/// ```
pub fn concat_str(&self, s: &str) -> String { /* ... */ }
```

### Treat documentation examples as tests
Rustdoc compiles code blocks by default. Use targeted annotations:
- `no_run` — compiles but doesn't execute
- `should_panic` — failure is the point
- `compile_fail` — type-system counterexample
- Avoid `ignore` — disables checking and makes stale examples easier to miss

### Guides should work like integration tests
Write end-to-end examples that compile and reflect real usage, not just isolated method docs. Keep guide code additive (each stage self-contained) rather than patching previous snippets.
