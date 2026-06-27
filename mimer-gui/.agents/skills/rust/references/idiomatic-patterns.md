# Idiomatic Patterns

## Table of Contents
- Defensive Programming
- Advanced Pattern Matching
- Option Chaining with and_then
- Delimiter Matching Idioms

## Defensive Programming

### Exhaustive pattern matching over decoupled checks
Replace `is_empty()` + indexing or wildcard match arms with explicit, exhaustive matches:

```rust
// Good: only access data inside the arm where shape is proven
match matching_users.as_slice() {
    [] => todo!("handle no users"),
    [existing_user] => { /* exactly one user */ }
    _ => return Err(RepositoryError::DuplicateUsers),
}

// Bad: indexing can panic if a separate emptiness check is removed
if !matching_users.is_empty() {
    let existing_user = &matching_users[0];
}
```

Match enum variants explicitly instead of using `_ => ...` so new variants trigger compile-time updates.

### Exhaustive struct destructuring to future-proof code
Fully destructure a struct so adding a field forces an explicit decision:

```rust
// Adding a new field to Foo forces this code to be updated
let Foo { field1, field2, field3, field4 } = Foo::default();
let foo = Foo { field1: value1, field2: value2, field3, field4 };
```

In trait impls like `PartialEq`, `Hash`, `Debug`:
```rust
impl PartialEq for PizzaOrder {
    fn eq(&self, other: &Self) -> bool {
        let Self { size, toppings, crust_type, ordered_at: _ } = self;
        let Self { size: other_size, toppings: other_toppings, crust_type: other_crust, ordered_at: _ } = other;
        size == other_size && toppings == other_toppings && crust_type == other_crust
    }
}
```

### Seal construction so validation cannot be bypassed
Escalating options: private field → `#[non_exhaustive]` → nested private module with seal type:

```rust
mod inner {
    pub struct S { field1: String, field2: u32, _seal: Seal }
    struct Seal;
    impl S {
        pub fn new(field1: String, field2: u32) -> Result<Self, String> {
            if field1.is_empty() || field2 == 0 { return Err("invalid state".to_string()); }
            Ok(Self { field1, field2, _seal: Seal })
        }
    }
}
pub use inner::S;
```

Stronger sealing is more defensive but also more complex — mainly justified for libraries.

## Advanced Pattern Matching

### Ownership-aware destructuring with `ref`
Mix cheap fields (Copy types) with expensive ones without consuming the whole value:

```rust
struct Task { user_id: u64, image: Vec<u8> }

fn run(task: Task) {
    match task {
        Task { user_id, ref image } if has_quota(user_id, image) => do_task(task),
        _ => {}
    }
}
```

### Match guards for runtime conditions
```rust
fn classify(input: Option<&str>) -> &'static str {
    match input {
        Some(url) if url.starts_with("https://") => "remote",
        Some(path) if path.starts_with('/') => "local",
        Some(_) => "other",
        None => "missing",
    }
}
```

### `@` bindings to keep the whole value while matching
```rust
enum Event { Http(u16), Tick }
fn handle(event: Event) {
    match event {
        whole @ Event::Http(500..=599) => retry(whole),
        Event::Tick => {}
        _ => {}
    }
}
```

## Option Chaining with `and_then`

Replace deeply nested `match` blocks with `Option::and_then` pipelines:

```rust
// Good: linear control flow
fn update_duration(conf: &Config) -> u64 {
    conf.get("site")
        .and_then(Value::as_table)
        .and_then(|t| t.get("sleep_update"))
        .and_then(Value::as_integer)
        .unwrap_or(5) as u64
}

// Bad: deeply nested match
fn update_duration(conf: &Config) -> u64 {
    match conf.get("site") {
        Some(&Value::Table(ref t)) => match t.get("sleep_update") {
            Some(&Value::Integer(v)) => v as u64,
            _ => 5,
        },
        _ => 5,
    }
}
```

Use typed accessors (`as_table`, `as_integer`, `as_str`) instead of pattern-matching every layer by hand. Use `unwrap_or` instead of `is_some()` + `unwrap()`.

## Delimiter Matching Idioms

### Push the expected closer, not the opener
Turns later validation into a direct comparison:

```rust
match c {
    '(' => stack.push(')'),
    '[' => stack.push(']'),
    '{' => stack.push('}'),
    ')' | ']' | '}' if stack.pop() != Some(c) => return false,
    _ => {}
}
```

### Recursive parsing with a shared iterator
Passes a mutable `Chars` iterator plus an expected terminator. Reuses one iterator across recursive calls instead of maintaining an explicit stack:

```rust
fn balanced(input: &str) -> bool { expect(None, &mut input.chars()) }

fn expect(end: Option<char>, input: &mut std::str::Chars<'_>) -> bool {
    loop {
        let c = input.next();
        let good = match c {
            Some('(') => expect(Some(')'), input),
            Some('[') => expect(Some(']'), input),
            Some('{') => expect(Some('}'), input),
            Some(')') | Some(']') | Some('}') | None => return end == c,
            _ => true,
        };
        if !good { return false; }
    }
}
```
