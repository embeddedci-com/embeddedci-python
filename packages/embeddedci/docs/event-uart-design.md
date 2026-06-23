# Design: event-based UART for pytest (`open_uart`)

**Status: design only — not implemented.**

## Problem

Today UART is *capture-window* based:

```python
cap = bp.capture_uart(rx=5, tx=4, baud=115200, duration=6, until="APP_OK")
```

`capture()` ([uart.py](../src/embeddedci/benchpod/uart.py)) opens the proxy, then
**blocks** reading for `duration` (or until a match) and closes. Because the read
loop owns the calling thread, you cannot do anything else — including powering
the target — while it runs. To catch a boot banner you must therefore use
`power_cycle_and_capture`, which schedules a **delayed, pod-side** eFuse power-on
so it fires *inside* the capture window:

```python
# the power-on is delayed so the banner lands in the window
cap = bp.power_cycle_and_capture(rx=5, tx=4, efuse=1, delay=1.0, duration=6.0, until="APP_OK")
```

That delay is a workaround. We want to **start the eFuse immediately** (no
pod-side timer) and still see the boot message. The only way is to begin
*listening before* powering the target, then read at a later point.

## Goal

An event-based session: open the UART (start buffering in the background),
power the target with a plain non-delayed `power_on`, then read.

```python
with bp.open_uart(rx=5, tx=4, baud=115200) as uart:
    bp.power_on(bp.INTERNAL)                 # immediate — no pod-side delay
    assert uart.read_until("APP_OK", timeout=6)
    # ... later, a second read from the same stream:
    uart.write(b"status\r\n")
    print(uart.read_until("rx_byte_count", timeout=2))
```

Because the background reader is already draining the proxy when `power_on`
fires, the banner emitted microseconds after power-up is buffered and the first
`read_until` finds it.

## Proposed API

A new `UartSession` returned by `BenchPod.open_uart(...)`:

```python
class UartSession:
    # --- reading ---
    def read_until(self, pattern: Until, *, timeout: float) -> Optional[re.Match | str]:
        """Block until `pattern` (substring/regex/predicate) appears in the
        accumulated text or `timeout` elapses. Returns the match (truthy) or
        None on timeout. Does NOT consume — `text` keeps growing."""

    def read(self, *, timeout: float = 0.0) -> str:
        """Return all text received so far. timeout>0 waits up to that long for
        at least one new byte; timeout=0 is non-blocking."""

    def expect(self, pattern: Until, *, timeout: float) -> re.Match | str:
        """Like read_until but raises UartTimeout instead of returning None."""

    @property
    def text(self) -> str: ...          # everything decoded so far
    @property
    def lines(self) -> list[str]: ...   # convenience split

    def drain(self) -> str:
        """Return + clear the buffer (so the next read_until only sees new data)."""

    # --- writing (proxy is bidirectional) ---
    def write(self, data: bytes | str) -> None: ...

    # --- lifecycle ---
    def close(self) -> None: ...        # stop reader, leave the proxy
    def __enter__(self) -> "UartSession": ...
    def __exit__(self, *exc) -> None: ...   # calls close()
```

`BenchPod`:

```python
def open_uart(self, *, rx, tx, baud=115200, max_buffer=1<<20) -> UartSession: ...
```

`UartTimeout(BenchPodError)` is raised by `expect`.

## Internals

```
open_uart()
  link = transport.uart_proxy_start(rx, tx, baud)   # existing RawLink, raw 8N1
  start a daemon thread: loop link.read(chunk) -> append under lock -> notify
  return UartSession(link, thread, buffer, condition)
```

- **Buffer**: a `bytearray` guarded by a `threading.Lock`; a `threading.Condition`
  signals readers when new bytes arrive or the link closes.
- **Reader thread**: `while not stopped: data = link.read(chunk); if not data: mark
  closed + notify; break; with lock: buffer += data; notify_all()`.
  `link.read` already blocks until data/EOF, so the thread parks when the DUT is
  quiet — no busy-poll.
- **`read_until`**: under the condition, re-evaluate the predicate against
  `buffer.decode("utf-8", errors="replace")` (decode the whole buffer each time, as
  `capture()` does, so multi-byte chars split across chunks resolve); `wait(timeout)`
  until it matches, the deadline passes, or the link closes.
- **`close()`**: set `stopped`, `link.close()` (which unblocks the reader's
  in-flight `read` and tells the firmware to leave PROTO_UART), `thread.join()`.
- **Bounded buffer**: cap at `max_buffer`; on overflow drop oldest and set an
  `overflowed` flag (surfaced on read) so a chatty DUT can't OOM a long session.
- **Decode position**: keep raw bytes; decode lazily in `text`/`read_until`. Track a
  `consumed` offset for `drain()`.

`capture_uart` can be re-expressed on top of this (open → `read_until` or sleep
`duration` → snapshot `text`/`lines` → close), keeping the existing one-shot API
backward-compatible.

## The key concurrency question: powering while listening

`open_uart` holds the proxy connection for the whole session. `power_on` must run
**concurrently** on a *different* path. This differs by transport:

- **TCP (`TcpTransport`)** — every command dials a *fresh* socket
  ([tcp.py](../src/embeddedci/benchpod/transport/tcp.py) `_dial`). The proxy holds
  socket A; `power_on` opens socket B. The firmware services multiple AT
  connections, and the soft-UART (FPGA) and the eFuse (target-power GPIO) are
  independent subsystems — so they run concurrently. **Works as-is.**

- **Cloud (`CloudTransport`)** — **implemented.** `CloudTransport.command()` now
  routes non-streaming commands over the cloud **command channel**
  (`POST /api/cloud/devices/command`, which the server turns into a
  `command.request`/`command.response`) instead of dialing a second byte tunnel.
  The firmware services the command channel (`CH_CLOUD_CONN`) and the byte tunnel
  (`CH_CLOUD_TUNNEL_CONN`) as independent connections and announces
  `"command":true` in its capabilities, so `power_on`/`power_off`/`gpio_set`/etc.
  run *while* `uart_proxy_start` holds the tunnel. Streaming modes
  (`dap_start`/`uart_proxy_start`) still use the tunnel via `_dial`. This both
  removes the two-concurrent-tunnels requirement and makes every command faster
  (one HTTP round-trip vs a fresh WebSocket dial).

- **Thread-safety of the link** — the reader thread calls `link.read` while the
  main thread may call `link.write`/`close`. Sockets allow concurrent
  read/write in opposite directions; the cloud WS socket is created with
  `enable_multithread=True` already, so this holds. `close()` from the main thread
  unblocking a `read()` in the reader thread is the same pattern `capture()`'s
  timer already relies on.

## Edge cases to cover in implementation

- **close() races**: idempotent close; reader thread exits on EOF or `stopped`.
- **timeout returns vs raises**: `read_until` returns `None`; `expect` raises
  `UartTimeout` carrying the buffered text (mirrors how `flash` surfaces context).
- **partial UTF-8 / binary**: decode the whole buffer with `errors="replace"`;
  never split a decode across a chunk boundary.
- **predicate cost**: re-decoding the whole buffer per notify is O(n) per wakeup;
  for very large/long sessions, decode incrementally or cap the scan window.
- **proxy escape**: `close()` relies on `RawLink.close()` to leave the proxy
  (TCP: socket close; serial: the proxy escape). No in-band sentinel needed.
- **teardown ordering**: `__exit__` closes the UART session; target power is the
  caller's responsibility (or a `power_off` in a fixture finalizer), unchanged.

## Worked example (the motivating case)

```python
def test_boot_banner(benchpod):
    bp = benchpod
    with bp.open_uart(rx=5, tx=4, baud=115200) as uart:   # listening starts now
        bp.power_on(bp.INTERNAL)                           # immediate eFuse start
        m = uart.read_until(r"APP_OK", timeout=6)
        assert m, f"no boot banner; got:\n{uart.text}"

        # a second, independent read on the same live stream
        uart.write(b"ping\r\n")
        assert uart.expect("pong", timeout=2)
    bp.power_off(bp.INTERNAL)
```

No `delay=`, no `power_cycle_and_capture` — the banner is caught because listening
began before power-up.
