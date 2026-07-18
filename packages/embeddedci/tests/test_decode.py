"""Unit tests for the UART + SPI LA decoders (mirror of the server's decoders)."""

from __future__ import annotations

from typing import Dict, List

import pytest

from embeddedci.benchpod import decode


def _pack(channels: Dict[int, List[int]]) -> List[int]:
    """Pack per-channel (1-based) 0/1 lists into LA words (bit ch-1)."""
    n = max(len(v) for v in channels.values())
    words = []
    for i in range(n):
        w = 0
        for ch, bits in channels.items():
            if i < len(bits) and bits[i]:
                w |= 1 << (ch - 1)
        words.append(w)
    return words


def _uart_byte_samples(value: int, spb: int, *, data_bits: int = 8) -> List[int]:
    """One idle-high + start + LSB-first data + stop-bit UART frame at ``spb`` samples/bit."""
    out: List[int] = [1] * spb  # idle high
    out += [0] * spb            # start bit
    for b in range(data_bits):
        bit = (value >> b) & 1
        out += [bit] * spb
    out += [1] * spb            # stop bit
    return out


def test_uart_decodes_bytes():
    spb = 10
    baud = 100000.0
    sample_rate = baud * spb
    samples: List[int] = [1] * spb
    for v in (0x41, 0x42, 0x43):  # "ABC"
        samples += _uart_byte_samples(v, spb)
    words = _pack({5: samples})
    frames = decode.decode_uart(words, rx=5, baud=baud, sample_rate_hz=sample_rate)
    assert [f.value for f in frames] == [0x41, 0x42, 0x43]
    assert decode.uart_text(frames) == "ABC"
    assert all(f.ok for f in frames)


def test_uart_via_unified_entry():
    spb = 8
    baud = 115200.0
    sr = baud * spb
    words = _pack({3: [1] * spb + _uart_byte_samples(0x7A, spb)})
    frames = decode.decode(words, "uart", rx=3, baud=baud, sample_rate_hz=sr)
    assert frames[0].value == 0x7A and frames[0].text == "z"


def test_uart_rejects_low_sample_rate():
    with pytest.raises(Exception):
        decode.decode_uart([0, 1] * 10, rx=1, baud=100000.0, sample_rate_hz=100000.0)  # 1 sample/bit


def _spi_word_samples(value: int, *, bits: int = 8, per_phase: int = 2):
    """Build sclk/mosi sample lists for one MSB-first mode-0 word (idle-low clock)."""
    sclk: List[int] = []
    mosi: List[int] = []
    for b in range(bits - 1, -1, -1):
        bit = (value >> b) & 1
        # data set up while clock low, then a rising edge samples it
        sclk += [0] * per_phase + [1] * per_phase
        mosi += [bit] * per_phase + [bit] * per_phase
    return sclk, mosi


def test_spi_mode0_msb_first():
    sclk, mosi = _spi_word_samples(0xA5)
    # cs active-low around the word
    cs = [0] * len(sclk)
    words = _pack({1: sclk, 2: mosi, 4: cs})
    frames = decode.decode_spi(words, sclk=1, mosi=2, cs=4, mode=0, bits=8, sample_rate_hz=1e6)
    assert len(frames) == 1
    assert frames[0].mosi == 0xA5
    assert frames[0].mosi_hex == "0xA5"


def test_spi_two_words_without_cs():
    s1, m1 = _spi_word_samples(0x0F)
    s2, m2 = _spi_word_samples(0xF0)
    words = _pack({1: s1 + s2, 2: m1 + m2})
    frames = decode.decode(words, "spi", sclk=1, mosi=2, mode=0, bits=8, sample_rate_hz=1e6)
    assert [f.mosi for f in frames] == [0x0F, 0xF0]


def test_unknown_protocol_raises():
    with pytest.raises(Exception):
        decode.decode([0, 1, 2], "onewire")
