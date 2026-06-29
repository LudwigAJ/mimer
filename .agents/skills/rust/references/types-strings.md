# Strings & Type System

## Table of Contents
- String Confusion
- String Argument Patterns
- Type System Traps
- Newtypes — Parse, Don't Validate
- Enums Instead of Booleans
- Compile-Time Invariants
- Encoding Invariants in Types

## String Confusion

- **`String` is owned, `&str` is borrowed slice** — convert with `.as_str()` or `String::from()`
- **Indexing `s[0]` fails** — UTF-8 variable width, use `.chars().nth(0)` or `.bytes()`
- **Concatenation: `s1 + &s2` moves s1** — use `format!("{}{}", s1, s2)` to keep both
- **`.len()` returns bytes, not characters** — use `.chars().count()` for char count
- **`&String` auto-derefs to `&str`** — but prefer `&str` in function params
- **`str::from_utf8` can fail** — use `String::from_utf8_lossy` if uncertain
- **`char` is 4 bytes (Unicode scalar)** — not 1 byte like C
- **`OsString` for paths** — not all paths are valid UTF-8

## String Argument Patterns

### When to use `&str` vs `String` vs `Into<String>` vs `AsRef<str>`

**`&str` for read-only inputs** — lets callers pass literals and borrowed views cheaply:
```rust
fn hello(name: &str) { println!("Hello, {name}!"); }
hello("world");
hello(&String::from("Alice"));
```

**`String` when function needs ownership** — makes cost model explicit:
```rust
struct Greetings { names: Vec<String> }
impl Greetings {
    pub fn add(&mut self, name: String) { self.names.push(name); }
}
```

**`Into<String>` for constructors that store owned text** — ergonomic for both `&str` and `String`:
```rust
impl Person {
    fn new<S: Into<String>>(name: S) -> Person {
        Person { name: name.into() }
    }
}
let a = Person::new("Herman");
let b = Person::new("Herman".to_string());
```

**`AsRef<str>` for maximum polymorphism** — accepts `&str`, `String`, `&String`:
```rust
fn hello<S: AsRef<str>>(name: S) {
    println!("Hello, {}!", name.as_ref());
}
```

### Rules of thumb
- **Own strings in structs with `String`** — avoid `&str` fields unless lifetime constraints are justified
- **Take `&str` in function params, return `String` by default** — the 95% rule
- **Return `&str` only when output is an unchanged slice of input** — otherwise return `String`
- **Don't hide allocation behind a `&str` param** — if you'll `.to_owned()` internally, take `String` or `Into<String>`

## Type System Traps

- **Orphan rule: can't impl external trait on external type** — newtype pattern workaround
- **Trait objects `dyn Trait` have runtime cost** — generics monomorphize for performance
- **`Box<dyn Trait>` for heap-allocated trait object** — `&dyn Trait` for borrowed
- **Associated types vs generics** — use associated when one impl per type
- **`Self` vs `self`** — type vs value: `Self::new()` vs `&self`
- **`impl Trait` vs `dyn Trait`** — static dispatch vs dynamic, different use cases
- **`Sized` bound implicit** — `?Sized` to accept unsized types
- **`PhantomData<T>`** — for unused type parameters (e.g., lifetime markers)
- **`Deref` coercion** — `&String` to `&str` automatic, but can be confusing

## Newtypes — Parse, Don't Validate

Wrap primitives in private newtypes. Validate once in the constructor. Pass the newtype everywhere else.

```rust
pub struct EmailAddress(String);
impl EmailAddress {
    pub fn new(raw: &str) -> Result<Self, EmailAddressError> {
        if email_regex().is_match(raw) { Ok(Self(raw.into())) }
        else { Err(EmailAddressError(raw.into())) }
    }
}
// Business logic accepts validated types, not raw strings
fn create_user(email: EmailAddress, password: Password) -> Result<User, Error> { /* ... */ }
```

**Prevents argument swapping** — `EmailAddress` and `Password` are distinct types even though both wrap `String`.

### Exposing inner values conservatively
- Prefer explicit getters or `AsRef` over `Deref`
- Use `Deref` only if the wrapper should behave almost exactly like the inner type
- Never impl `Borrow<T>` if equality/hashing semantics differ from `T`

```rust
impl EmailAddress {
    pub fn into_string(self) -> String { self.0 }
}
impl AsRef<str> for EmailAddress {
    fn as_ref(&self) -> &str { &self.0 }
}
```

### Invariants let you offer stronger APIs
Once a newtype guarantees an invariant, methods can be stricter:

```rust
struct NonEmptyVec<T>(Vec<T>);
impl<T> NonEmptyVec<T> {
    fn pop(&mut self) -> Option<T> {
        if self.0.len() == 1 { None } else { self.0.pop() }
    }
    fn last(&self) -> &T { self.0.last().unwrap() } // infallible!
}
```

## Enums Instead of Booleans

Use a named enum instead of `bool` when a function argument represents a mode or reason:

```rust
// Good: self-documenting call site
enum BlockRotateReason { Full, Timeout }
fn rotate_block(&self, block: &mut BlockMut, reason: BlockRotateReason) -> Result<(), io::Error>;
self.rotate_block(&mut block, BlockRotateReason::Timeout)?;

// Bad: what does `true` mean?
fn rotate_block(&self, block: &mut BlockMut, timed_out: bool) -> Result<(), io::Error>;
self.rotate_block(&mut block, true)?;
```

Enums can grow to three or more cases. Booleans cannot. Exhaustive matching forces all cases to be handled when new variants are added.

## Compile-Time Invariants

### Non-empty collections in the type
```rust
pub struct Vec1<T> { first: T, rest: Vec<T> }
fn connect_to_kafka(brokers: Vec1<&str>) { /* ... */ }
```

Keep fields private and only expose operations that cannot break the invariant. Don't impl `Deref<Target=Vec<T>>` — it would expose `clear()`, `pop()`, etc.

### Const-generic variant for static sizes
```rust
pub struct Array1<T, const N: usize> { first: T, rest: [T; N] }
let brokers = array1!["kafka:9092", "kafka2:9092"];
```

## Encoding Invariants in Types

Use newtypes, bounded constructors, and enums so invalid values or field combinations cannot exist:

```rust
// Good: impossible states are unrepresentable
enum ConnectionSecurity { Insecure, Ssl { cert_path: String } }

// Bad: raw fields can encode impossible states (ssl=false, ssl_cert=Some(...))
struct Configuration { ssl: bool, ssl_cert: Option<String> }
```

### Wrap sensitive data
Put secrets behind dedicated types with redacted `Debug` and validated deserialization:

```rust
#[derive(Deserialize)]
#[serde(try_from = "String")]
struct Password(String);
impl std::fmt::Debug for Password {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("[REDACTED]")
    }
}
```
