# Ownership & Borrowing & Lifetimes

## Ownership Traps

- **Variable moved after use** — clone explicitly or borrow with `&`
- **`for item in vec` moves vec** — use `&vec` or `.iter()` to borrow
- **Struct field access moves field if not Copy** — destructure or clone
- **Closure captures by move with `move ||`** — needed for threads and `'static`
- **`String` moved into function** — pass `&str` for read-only access
- **Partial moves in structs** — moving one field makes whole struct unusable (unless using remaining fields explicitly)

## Borrowing Battles

- **Can't have mutable and immutable borrow simultaneously** — restructure code or use interior mutability
- **Borrow lasts until last use (NLL)** — not until scope end in modern Rust
- **Returning reference to local fails** — return owned value or use lifetime parameter
- **Mutable borrow through `&mut self` blocks all other access** — split struct or use `RefCell`
- **`Option<&T>` vs `&Option<T>`** — `.as_ref()` converts outer to inner reference
- **Reborrowing `&mut` through `&`** — auto-reborrow works but explicit sometimes needed

## Lifetime Gotchas

- **Missing lifetime annotation** — compiler usually infers, explicit when multiple references
- **`'static` means "can live forever", not "lives forever"** — `String` is `'static`, `&str` may not be
- **Struct holding reference needs lifetime parameter** — `struct Foo<'a> { bar: &'a str }`
- **Function returning reference must tie to input lifetime** — `fn get<'a>(s: &'a str) -> &'a str`
- **Lifetime elision rules** — `fn foo(x: &str) -> &str` implicitly ties output to input
- **`'a: 'b` means `'a` outlives `'b`** — covariance/contravariance matters in generics

## Descriptive Lifetime Names

Lifetimes don't need to be single letters. Descriptive names document what a borrow represents:

```rust
// Name after the role
impl Person {
    pub fn name<'me>(&'me self) -> &'me str { &self.name }
}

// Name after a long-lived owner
fn process_data<'prov>(data: &'prov Data) -> Result<&'prov str> { /* ... */ }

// Distinct names for multiple borrow sources
struct AuthorView<'art, 'auth> {
    author: &'auth Author,
    articles: Vec<&'art Article>,
}
```

Use distinct named lifetimes when one type or function combines references from different places. This prevents conflating independent borrow domains.

## Immutability Patterns

Rust treats mutability as opt-in. Signatures should advertise side effects instead of hiding them.

### Make mutation explicit at the API boundary
Use `&mut T` only when a function truly needs to update an existing value in place. Keep `mut` local and short-lived.

```rust
fn black_box(x: &mut i32) { *x = 23; }
// Readers can see state changes from the signature alone
```

### Derive aggregates on demand instead of caching shared mutable state
Avoid storing derived global fields when they can be computed from the primary data:

```rust
// Good: compute from source of truth
pub struct Mailbox { emails: Vec<String> }
impl Mailbox {
    pub fn get_word_count(&self) -> usize {
        self.emails.iter().map(|e| e.split_whitespace().count()).sum()
    }
}

// Bad: mutable cache duplicates state and can drift out of sync
pub struct Mailbox { emails: Vec<String>, total_word_count: usize }
```

### When performance matters, improve the data model
Cache immutable metadata inside each item rather than maintaining a mutable aggregate:

```rust
pub struct Mail { body: String, word_count: usize }
impl Mail {
    pub fn new(body: &str) -> Self {
        Self { body: body.to_string(), word_count: body.split_whitespace().count() }
    }
}
```

## Out Parameters

- **Return values are the idiomatic default** — prefer returning tuples/structs
- **Use out parameters for reusable buffers** — `read_line(&mut String)` pattern
- **Offer ergonomic wrappers on top** — allocating convenience over low-level reuse

```rust
// Idiomatic: reusable buffer
let mut guess = String::new();
loop {
    guess.clear();
    io::stdin().read_line(&mut guess)?;
}

// Convenience wrapper for one-shot use
fn read_all(mut file: std::fs::File) -> std::io::Result<String> {
    let mut s = String::new();
    file.read_to_string(&mut s)?;
    Ok(s)
}
```
