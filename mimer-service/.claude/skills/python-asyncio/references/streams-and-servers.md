# Streams, Servers, and Protocols

asyncio offers two layers for network code:

1. **Streams** (`asyncio.start_server`, `asyncio.open_connection`, `StreamReader`, `StreamWriter`) -- the high-level API. Reads and writes are coroutines on a `(reader, writer)` pair.
2. **Protocols and Transports** (`asyncio.Protocol`, `asyncio.DatagramProtocol`) -- the lower-level callback API. Required for UDP and useful when you want explicit lifecycle hooks.

In application code, default to streams for TCP. Use protocols for UDP.

## 1. TCP server with streams

```python
import asyncio

async def handle_client(reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    try:
        while True:
            line = await reader.readline()
            if not line:                        # EOF -- peer closed cleanly
                break
            response = process(line)
            writer.write(response)
            await writer.drain()                # back-pressure
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("error handling %s", peer)
    finally:
        writer.close()
        await writer.wait_closed()              # surface close-time errors

async def serve() -> None:
    server = await asyncio.start_server(handle_client, "127.0.0.1", 8888)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    logging.info("listening on %s", addrs)
    async with server:
        await server.serve_forever()
```

### `read*` methods

`StreamReader` exposes several read methods. Pick by protocol shape, not preference:

- `await reader.read(n)` -- up to n bytes. May return fewer than n. Returns empty `bytes` on EOF. Use this for streaming/binary protocols where you handle framing yourself.
- `await reader.readexactly(n)` -- exactly n bytes, or raises `IncompleteReadError`. Use this for binary protocols with fixed-size headers.
- `await reader.readline()` -- bytes up to and including `\n`. Returns empty bytes on EOF. Use for line-delimited text protocols.
- `await reader.readuntil(separator)` -- bytes up to and including a custom separator. Raises `LimitOverrunError` if the buffer fills before the separator is found.
- `async for line in reader:` -- iterator equivalent to repeated `readline`.

A common mistake is to assume `read(n)` returns exactly n bytes. It does not -- it returns *up to* n. For length-prefixed protocols use `readexactly`.

### `write` and the back-pressure rule

```python
writer.write(data)
await writer.drain()
```

`writer.write` is non-blocking and buffers in memory. If the peer is slow, the buffer grows without bound -- a memory leak under sustained load.

`writer.drain()` waits until the buffer has drained below the low watermark before resuming. Always pair `write` with `drain`, especially in loops or when writing large payloads.

The high/low watermarks are configurable via `transport.set_write_buffer_limits(high, low)` if you need to tune them.

### Closing a writer

```python
writer.close()
await writer.wait_closed()
```

`writer.close()` schedules the close but returns immediately. `await writer.wait_closed()` waits for the close to complete and surfaces any error during close. Skipping the `wait_closed` is the second most common bug in stream code (after forgetting `drain`).

## 2. TCP client with streams

```python
async def fetch_line(host: str, port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(request)
        await writer.drain()
        return await reader.readline()
    finally:
        writer.close()
        await writer.wait_closed()
```

For TLS, pass `ssl=` (a `ssl.SSLContext`) to `open_connection`. For mutual TLS or pinning, build the context with the certs you need.

## 3. UDP via DatagramProtocol

UDP has no streams API; use the protocol/transport interface.

```python
class EchoUDP(asyncio.DatagramProtocol):
    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.transport.sendto(data, addr)

    def error_received(self, exc: Exception) -> None:
        logging.warning("udp error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        pass

async def serve_udp() -> None:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        EchoUDP, local_addr=("127.0.0.1", 8888),
    )
    try:
        await asyncio.Event().wait()      # run forever
    finally:
        transport.close()
```

Notes:
- UDP has no connection. `connection_made` simply means the socket is ready.
- `datagram_received` is a synchronous callback. To do async work in response, schedule a task: `asyncio.create_task(handle(data, addr))`. Retain references to those tasks (see "background tasks" in `concurrency-patterns.md`).
- `sendto` is non-blocking and never raises for queue-full -- the OS will drop packets silently. Monitor your loss rate at the application layer for non-trivial UDP services.

## 4. TLS

```python
import ssl

ctx = ssl.create_default_context(cafile=ca_path)
ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)

reader, writer = await asyncio.open_connection(host, port, ssl=ctx)
```

For a server:

```python
server = await asyncio.start_server(handle_client, host, port, ssl=ctx)
```

Supply `ssl_handshake_timeout` and `ssl_shutdown_timeout` to bound how long handshakes can hang. Default is 60 seconds, which is too generous for most production systems.

## 5. Graceful shutdown of a server

```python
async def serve(stop: asyncio.Event) -> None:
    server = await asyncio.start_server(handle_client, "0.0.0.0", 8888)
    async with server:
        async with asyncio.TaskGroup() as tg:
            serve_task = tg.create_task(server.serve_forever())
            await stop.wait()
            server.close()                   # stop accepting new connections
            await server.wait_closed()
            serve_task.cancel()
```

To drain in-flight connections, hand each handler an `asyncio.Event` and check it in the read loop. Cancelling open handlers is acceptable for idempotent protocols; it is dangerous for stateful ones (a half-acknowledged trade).

## 6. Framing: getting reliable messages out of a byte stream

TCP is a stream of bytes, not of messages. You must implement framing. The three common approaches:

- **Newline-delimited.** Use `reader.readline()`. Simple but breaks on payloads containing newlines.
- **Length-prefixed.** Read a fixed-size header (`readexactly(N)`), parse the length, then `readexactly(length)` for the body. Robust and binary-safe.
- **Delimiter-based.** Use `readuntil(b"\r\n\r\n")` or similar. Works for HTTP-shaped headers but does not handle binary payloads.

Length-prefixed is the default for new binary protocols. Always also include a sanity-check max length so a corrupted header cannot make you allocate gigabytes.

## 7. Backpressure end-to-end

For high-throughput pipelines, plumb backpressure all the way through:

- Read side: do not read faster than you can process. Use a bounded `Queue` between the reader and the worker.
- Write side: always `await writer.drain()`. If you fan in from multiple producers, gate them at a bounded queue rather than letting each write directly.
- For TLS, the same rules apply -- TLS sits between you and the transport but does not exempt you from the backpressure pattern.

## 8. Common server bugs

- **Long-blocking handler.** A `time.sleep` (or a sync DB call) inside `handle_client` blocks every other connection on the server. Audit handlers for accidental sync calls.
- **Leaked tasks per connection.** If your handler spawns subtasks (e.g. a heartbeat timer per connection) and you do not cancel them on disconnect, they accumulate. Use a per-connection `TaskGroup` or scoped task list.
- **Missing `wait_closed`.** Closing without waiting can mask errors and lose buffered data on shutdown.
- **`server.serve_forever` swallowed by an outer timeout.** A whole-process timeout should not include the serve loop. Structure your shutdown via signals + an `Event`, not via `wait_for`.
