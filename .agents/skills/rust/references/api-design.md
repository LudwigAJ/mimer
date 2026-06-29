# API Design Patterns

## Table of Contents
- Be Simple: Concrete Over Speculative
- Replace Stringly-Typed Parameters
- Accept Flexible Inputs with Conversion Traits
- Type-State Pattern
- Hexagonal Architecture / Domain Boundaries
- Generalize Inputs with Slices and Traits

## Be Simple: Concrete Over Speculative

Start with the narrowest type that fits the current requirement. Generalize only when multiple real use cases exist.

```rust
// Good: simple, readable, debuggable
fn process_user_input(input: &str) { /* ... */ }

// Bad: generalized for imagined future needs
fn process_user_input<'a, S>(input: &'a S) -> &'a str
where S: AsRef<str> + Send + Sync + ?Sized
{ input.as_ref() }
```

### Make the common path the obvious path
Design library APIs so the most common use case is easy and direct:

```rust
// Good: simple façade for the common case
fn base64_encode(input: &str) -> String;

// Bad: forces every caller through generic configuration
fn base64_encode<T: AsRef<[u8]>>(input: T, alphabet: Base64Alphabet) -> String;
```

### Optimize after the design is stable and measured
Write the naïve, obvious version first. Delay lifetime-heavy, allocation-avoiding, or deeply abstract refactors until a real bottleneck appears.

```rust
// Good: simple, easy to reason about
pub fn quicksort(mut v: Vec<usize>) -> Vec<usize> {
    let Some(pivot) = v.pop() else { return v };
    let (smaller, larger) = v.into_iter().partition(|x| x < &pivot);
    quicksort(smaller).into_iter()
        .chain(std::iter::once(pivot))
        .chain(quicksort(larger))
        .collect()
}
```

## Replace Stringly-Typed Parameters

Encode a closed set of valid inputs as an `enum`. Parse from strings only at the boundary:

```rust
enum Color { Red, Green, Blue, LightGoldenRodYellow }
fn color_me(input: &str, color: Color) { /* ... */ }
color_me("surprised", Color::Blue); // compile-time guidance

// Bad: free-form strings push validation into runtime
fn output_a(f: &Foo, color: &str) -> Result<Bar, ParseError> {
    let color: Color = color.parse()?;
    f.to_bar(&color)
}
```

## Accept Flexible Inputs with Conversion Traits

Use `AsRef`, `Into`, or `Into<Option<T>>` to broaden accepted inputs:

```rust
fn open_file<P: Into<PathBuf>>(path: P) {
    let path: PathBuf = path.into();
    // ...
}
open_file("foo.txt");
open_file(PathBuf::from("/absolute/path"));
```

**Tradeoffs:** more complex signatures/docs and potentially longer compile times from monomorphization.

## Generalize Function Inputs with Slices

Accept the least specific input type that fits the job:

```rust
// Good: accepts &[i32], &Vec<i32>, arrays
fn sum(xs: &[i32]) -> i32 { xs.iter().sum() }

// Bad: forces Vec specifically
fn sum(xs: &Vec<i32>) -> i32 { xs.iter().sum() }
```

Use `Cow<'a, str>` when a value is usually borrowed but may sometimes need owned data:

```rust
use std::borrow::Cow;
fn label<'a>(name: &'a str, suffix: bool) -> Cow<'a, str> {
    if suffix { Cow::Owned(format!("{name}!")) }
    else { Cow::Borrowed(name) }
}
```

## Type-State Pattern

Model each state as a different type. Give each state only the methods valid there. Transitions consume one state and return another.

### Generic machine wrapper
```rust
struct Machine<S> {
    shared_value: usize,
    state: S,
}
struct Waiting { waiting_time: std::time::Duration }
struct Filling { rate: usize }
struct Done;
```

### Consuming transitions via From/Into
Define `From` only for valid edges in the state graph. Invalid transitions become compile errors:

```rust
impl From<Machine<Waiting>> for Machine<Filling> {
    fn from(val: Machine<Waiting>) -> Machine<Filling> {
        Machine { shared_value: val.shared_value, state: Filling { rate: 1 } }
    }
}
fn step(m: Machine<Waiting>) -> Machine<Filling> { m.into() }
```

### Wrap typed states in an enum at system boundaries
When a parent struct must hold one of several concrete machine states:

```rust
enum MachineWrapper {
    Waiting(Machine<Waiting>),
    Filling(Machine<Filling>),
    Done(Machine<Done>),
}
impl MachineWrapper {
    fn step(self) -> Self {
        match self {
            MachineWrapper::Waiting(m) => MachineWrapper::Filling(m.into()),
            MachineWrapper::Filling(m) => MachineWrapper::Done(m.into()),
            MachineWrapper::Done(m) => MachineWrapper::Waiting(m.into()),
        }
    }
}
```

## Hexagonal Architecture / Domain Boundaries

### Boundary request objects use simple input types
Keep callers independent from domain internals:
```rust
struct Request { number: u16, name: String, types: Vec<String> }
fn execute(req: Request) -> Response { /* convert to domain types inside */ }
```

### Encode business rules with validated newtypes and `TryFrom`
Each domain concept gets its own type with guarded construction:

```rust
pub struct PokemonNumber(u16);
impl TryFrom<u16> for PokemonNumber {
    type Error = ();
    fn try_from(n: u16) -> Result<Self, Self::Error> {
        if n > 0 && n < 899 { Ok(Self(n)) } else { Err(()) }
    }
}
```

### Validate multiple conversions together
Pattern-match on the combined result of domain conversions:

```rust
enum Response { Ok(u16), BadRequest }
fn execute(req: Request) -> Response {
    match (
        PokemonNumber::try_from(req.number),
        PokemonName::try_from(req.name),
        PokemonTypes::try_from(req.types),
    ) {
        (Ok(number), Ok(_), Ok(_)) => Response::Ok(u16::from(number)),
        _ => Response::BadRequest,
    }
}
```

## HashMap Entry API

Use `entry()` for insert-or-update logic to avoid double lookups:

```rust
use std::collections::HashMap;
let mut counts = HashMap::new();
for word in ["a", "b", "a"] {
    *counts.entry(word).or_insert(0) += 1;
}
```
