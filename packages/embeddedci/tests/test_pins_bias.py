"""LA channel bias networks: LA7/LA8 pull DOWN, not up.

The pod's CTRL1..CTRL8 lines each switch in one fixed resistor, but the
direction is not uniform. Treating every biased channel as a pull-up (which the
table here used to) would silently drive an open-drain bus low, so these tests
pin the split as well as the values.
"""

from embeddedci.benchpod.pytest_plugin import BenchPodPins


def test_pull_up_channels():
    for channel in range(1, 7):
        assert BenchPodPins.has_pullup(channel), f"LA{channel} pulls up"
        assert not BenchPodPins.has_pulldown(channel)
        assert BenchPodPins.pull_direction(channel) == "up"


def test_pull_down_channels():
    for channel in (7, 8):
        assert BenchPodPins.has_pulldown(channel), f"LA{channel} pulls down"
        # The whole point: LA7/LA8 must not read as usable pull-ups.
        assert not BenchPodPins.has_pullup(channel)
        assert BenchPodPins.pullup_ohms(channel) is None
        assert BenchPodPins.pull_ohms(channel) == "10k"
        assert BenchPodPins.pull_direction(channel) == "down"


def test_unbiased_channels():
    for channel in range(9, 13):
        assert not BenchPodPins.has_pullup(channel)
        assert not BenchPodPins.has_pulldown(channel)
        assert BenchPodPins.pull_ohms(channel) is None
        assert BenchPodPins.pull_direction(channel) is None


def test_resistor_values_match_the_board():
    assert BenchPodPins.PULLUP_OHMS == {
        1: "4.7k", 2: "4.7k", 3: "2.2k", 4: "2.2k", 5: "10k", 6: "10k",
    }
    assert BenchPodPins.PULLDOWN_OHMS == {7: "10k", 8: "10k"}
