"""Off-device protocol decoding over a raw 12-channel LA capture.

The FPGA captures raw logic only; protocol interpretation runs on the host over the sampled
``words`` plus a channel assignment — the same model the server uses (``POST /analyzer/decode``).
This module is the Python mirror of the server's decoders (``benchpod_uart_decode.go`` /
``benchpod_spi_decode.go``) and reuses :mod:`embeddedci.benchpod.i2c` for I2C, so a test can
decode the same capture identically whether it goes through the server or stays client-side.

``words[i]`` packs all channels for sample ``i`` (bit ``n`` = channel ``LA{n+1}``); channel
numbers are 1-based (1..12).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

from . import i2c as _i2c
from .errors import BenchPodError


def _channel_bits(words: Sequence[int], ch: int) -> List[int]:
    """Extract one 1-based LA channel as a list of 0/1 samples."""
    bit = ch - 1
    return [(w >> bit) & 1 for w in words]


# -- UART --------------------------------------------------------------------

@dataclass
class UartFrame:
    """One decoded UART character/byte with the time window it occupies."""

    index: int
    start_us: float
    end_us: float
    value: int
    hex: str
    text: str
    ok: bool
    error: str = ""


def _printable_ascii(v: int) -> str:
    return chr(v) if 0x20 <= v <= 0x7E else ""


def decode_uart(words: Sequence[int], *, rx: int, baud: float, sample_rate_hz: float,
                data_bits: int = 8, parity: str = "none", stop_bits: float = 1.0,
                msb_first: bool = False) -> List[UartFrame]:
    """Decode an async-serial (UART) line. Mirrors the server's ``decodeUARTFromLA``.

    ``rx`` is the 1-based LA channel; ``baud`` and ``sample_rate_hz`` recover the bit timing (no
    clock line). The line idles high; each frame is a start bit (low), ``data_bits`` LSB-first
    data bits, an optional ``parity`` bit (``none``/``even``/``odd``), then stop bit(s).
    """
    if not 1 <= rx <= 12:
        raise BenchPodError("uart decode needs an rx channel in 1..12")
    if sample_rate_hz <= 0:
        raise BenchPodError("uart decode needs a positive sample rate")
    if baud <= 0:
        raise BenchPodError("uart decode needs a positive baud rate")
    if not 5 <= data_bits <= 9:
        data_bits = 8
    if stop_bits <= 0:
        stop_bits = 1.0
    spb = sample_rate_hz / baud
    if spb < 2:
        raise BenchPodError(
            f"sample rate {sample_rate_hz:.0f} Hz is too low to decode {baud:.0f} baud "
            "(need at least ~2 samples/bit)"
        )
    has_parity = parity in ("even", "odd")

    line = _channel_bits(words, rx)
    n = len(line)
    us_per_sample = 1e6 / sample_rate_hz

    frames: List[UartFrame] = []
    i = 0
    while i < n - 1:
        if not (line[i] == 1 and line[i + 1] == 0):
            i += 1
            continue
        start_edge = i + 1

        def centre_of(k: int) -> int:
            return start_edge + int(round((k + 0.5) * spb))

        sc = centre_of(0)
        if sc >= n or line[sc] != 0:
            i += 1
            continue

        ok = True
        err_kind = ""
        bits: List[int] = []
        last_idx = centre_of(0)
        truncated = False
        for b in range(data_bits):
            c = centre_of(1 + b)
            if c >= n:
                truncated = True
                break
            bits.append(line[c])
            last_idx = c
        if truncated:
            break

        value = 0
        ones = 0
        for b, bit in enumerate(bits):
            ones += bit
            if msb_first:
                value = (value << 1) | bit
            elif bit == 1:
                value |= 1 << b

        next_bit = 1 + data_bits
        if has_parity:
            c = centre_of(next_bit)
            if c < n:
                pbit = line[c]
                even = (ones + pbit) % 2 == 0
                if (parity == "even" and not even) or (parity == "odd" and even):
                    ok = False
                    err_kind = "parity"
                last_idx = c
                next_bit += 1

        sc = centre_of(next_bit)
        if sc < n:
            if line[sc] != 1:
                ok = False
                if not err_kind:
                    err_kind = "framing"
            last_idx = sc

        frames.append(UartFrame(
            index=len(frames) + 1,
            start_us=start_edge * us_per_sample,
            end_us=last_idx * us_per_sample,
            value=value,
            hex=f"0x{value:02X}",
            text=_printable_ascii(value),
            ok=ok,
            error=err_kind,
        ))

        advance = start_edge + int(round((next_bit + stop_bits - 0.5) * spb))
        if advance <= i:
            advance = i + 1
        i = advance
    return frames


def uart_text(frames: Sequence[UartFrame]) -> str:
    """Join the decoded byte values of ``frames`` into a UTF-8 string (lossy)."""
    return bytes(f.value & 0xFF for f in frames).decode("utf-8", errors="replace")


# -- SPI ---------------------------------------------------------------------

@dataclass
class SpiFrame:
    """One decoded SPI word (the MOSI + MISO bytes clocked together)."""

    index: int
    start_us: float
    end_us: float
    mosi: int
    miso: int
    mosi_hex: str
    miso_hex: str


def decode_spi(words: Sequence[int], *, sclk: int, mosi: int = 0, miso: int = 0, cs: int = 0,
               mode: int = 0, bits: int = 8, msb_first: bool = True,
               sample_rate_hz: float = 0.0) -> List[SpiFrame]:
    """Decode an SPI bus. Mirrors the server's ``decodeSPIFromLA``.

    ``sclk`` is required (1..12); ``mosi``/``miso``/``cs`` are optional (0 = not wired). ``mode``
    is 0..3 (CPOL/CPHA). Bits are read on the mode-selected clock edge, MSB-first by convention;
    an active-low ``cs`` gates and delimits words.
    """
    if not 1 <= sclk <= 12:
        raise BenchPodError("spi decode needs a sclk channel in 1..12")
    if not 0 <= mode <= 3:
        raise BenchPodError("spi mode must be 0..3")
    if not 1 <= bits <= 32:
        bits = 8
    cpol = (mode >> 1) & 1
    cpha = mode & 1
    sample_rising = (cpha == 0 and cpol == 0) or (cpha == 1 and cpol == 1)

    sclk_bits = _channel_bits(words, sclk)
    mosi_bits = _channel_bits(words, mosi) if 1 <= mosi <= 12 else None
    miso_bits = _channel_bits(words, miso) if 1 <= miso <= 12 else None
    cs_bits = _channel_bits(words, cs) if 1 <= cs <= 12 else None
    n = len(sclk_bits)
    us_per_sample = (1e6 / sample_rate_hz) if sample_rate_hz > 0 else 0.0

    frames: List[SpiFrame] = []
    mosi_acc = miso_acc = cnt = start_idx = 0

    for idx in range(1, n):
        if cs_bits is not None and cs_bits[idx - 1] != cs_bits[idx]:
            mosi_acc = miso_acc = cnt = 0
        if cs_bits is not None and cs_bits[idx] != 0:
            continue
        rising = sclk_bits[idx - 1] == 0 and sclk_bits[idx] == 1
        falling = sclk_bits[idx - 1] == 1 and sclk_bits[idx] == 0
        if not ((sample_rising and rising) or (not sample_rising and falling)):
            continue
        if cnt == 0:
            start_idx = idx
        mb = mosi_bits[idx] if mosi_bits is not None else 0
        sb = miso_bits[idx] if miso_bits is not None else 0
        if msb_first:
            mosi_acc = (mosi_acc << 1) | mb
            miso_acc = (miso_acc << 1) | sb
        else:
            mosi_acc |= mb << cnt
            miso_acc |= sb << cnt
        cnt += 1
        if cnt == bits:
            frames.append(SpiFrame(
                index=len(frames) + 1,
                start_us=start_idx * us_per_sample,
                end_us=idx * us_per_sample,
                mosi=mosi_acc,
                miso=miso_acc,
                mosi_hex=f"0x{mosi_acc:02X}",
                miso_hex=f"0x{miso_acc:02X}",
            ))
            mosi_acc = miso_acc = cnt = 0
    return frames


# -- unified entry -----------------------------------------------------------

def decode(words: Sequence[int], protocol: str = "i2c", *, sample_rate_hz: float = 0.0,
           **channels):
    """Decode ``protocol`` (``i2c``/``uart``/``spi``) from raw LA ``words``.

    Channel/param names match the protocol:

    * i2c:  ``sda``, ``scl`` → list of :class:`~embeddedci.benchpod.i2c.I2CTransaction`
    * uart: ``rx``, ``baud`` (+ ``data_bits``, ``parity``, ``stop_bits``) → list of :class:`UartFrame`
    * spi:  ``sclk`` (+ ``mosi``, ``miso``, ``cs``, ``mode``, ``bits``, ``msb_first``) → list of :class:`SpiFrame`
    """
    proto = (protocol or "").lower()
    if proto == "i2c":
        return _i2c.decode_from_la(words, sda_ch=channels["sda"], scl_ch=channels["scl"])
    if proto == "uart":
        return decode_uart(words, sample_rate_hz=sample_rate_hz, **channels)
    if proto == "spi":
        return decode_spi(words, sample_rate_hz=sample_rate_hz, **channels)
    raise BenchPodError(f"unsupported protocol {protocol!r} (supported: i2c, uart, spi)")
