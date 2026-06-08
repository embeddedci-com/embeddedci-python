"""I2C decoder: packing, round-trip, and a realistic BMP280 chip-id read.

No hardware: waveforms are synthesized in Python, packed exactly like the pod's
``sensor_la`` output, then decoded back — so this doubles as a runnable demo of
what the decoder extracts from a real bus capture.
"""

from embeddedci.benchpod import i2c

BMP280 = 0x76
REG_CHIP_ID = 0xD0
CHIP_ID = 0x58


def test_unpack_matches_gateware_layout():
    # 0b11_10_01_00 -> samples (scl,sda): (1,1),(1,0),(0,1),(0,0)
    assert i2c.unpack_samples([0b11100100]) == [(1, 1), (1, 0), (0, 1), (0, 0)]
    # idle byte = four (1,1)
    assert i2c.unpack_samples([0xFF]) == [(1, 1)] * 4


def test_pack_unpack_roundtrip():
    samples = [(1, 1), (0, 1), (1, 0), (0, 0), (1, 1), (0, 0)]
    packed = i2c.pack_samples(samples)
    # pad to a multiple of 4 with idle (1,1)
    assert i2c.unpack_samples(packed) == samples + [(1, 1), (1, 1)]


def test_decode_simple_write():
    # Write 0xAB to device 0x50, then STOP.
    samples = i2c.synthesize([
        {"address": 0x50, "read": False, "data": [(0xAB, True)]},
    ])
    txns = i2c.decode_samples(samples)
    assert len(txns) == 1
    t = txns[0]
    assert t.complete and t.address == 0x50
    assert len(t.messages) == 1
    m = t.messages[0]
    assert m.read is False and m.address_ack is True
    assert m.values == [0xAB]


def test_decode_bmp280_chip_id_read():
    # The classic "pointer write + repeated-START read" a BMP280 driver does:
    #   S 0x76 W  D0 (ack)  Sr 0x76 R  58 (nack)  P
    samples = i2c.synthesize([
        {"address": BMP280, "read": False, "data": [(REG_CHIP_ID, True)]},
        {"address": BMP280, "read": True, "data": [(CHIP_ID, False)]},
    ])
    txns = i2c.decode(i2c.pack_samples(samples))

    assert i2c.addressed(txns, BMP280)
    assert i2c.read_register(txns, BMP280, REG_CHIP_ID) == [CHIP_ID]

    # The human-readable trace shows the actual bus activity.
    trace = i2c.format_transactions(txns)
    assert "0x76W" in trace and "0x76R" in trace
    assert "0xD0+" in trace and "0x58-" in trace
    assert trace.endswith("P")


def test_decode_handles_idle_and_partial_capture():
    # Leading/trailing idle plus a capture that starts mid-bus is tolerated.
    body = i2c.synthesize([{"address": 0x10, "read": False, "data": [(0x01, True)]}])
    samples = [(1, 1)] * 8 + body + [(1, 1)] * 8
    txns = i2c.decode_samples(samples)
    assert any(t.to(0x10) for t in txns)


def test_decode_empty_and_idle():
    assert i2c.decode([]) == []
    assert i2c.decode([0xFF] * 16) == []  # idle bus -> no transactions
