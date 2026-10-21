from __future__ import annotations

import pytest

from fakes import FakeNE1000
from pyreagent.core import DeviceState, HardwareError, InvalidArgument
from pyreagent.devices.pump import Pump
from pyreagent.devices.pump.new_era import NewEraSyringePumpBackend
from pyreagent.devices.pump.new_era.ne1000 import format_number, parse_reply
from pyreagent.io import MockTransport


async def make(fake: FakeNE1000, **pump_options) -> Pump:
    backend = NewEraSyringePumpBackend(transport=MockTransport(fake), syringe_diameter_mm=14.43, address=fake.address)
    pump = Pump("syringe", backend, max_volume_ml=10, max_rate_ml_min=20, poll_interval_s=0, **pump_options)
    await pump.connect()
    return pump


@pytest.mark.parametrize(
    ("value", "text"),
    [(14.43, "14.43"), (0.5, "0.500"), (12.345, "12.35"), (123.45, "123.5"), (1234.4, "1234"), (2, "2.000")],
)
def test_format_number(value, text):
    assert format_number(value) == text


def test_format_number_rejects_unrepresentable():
    for bad in (0, -1, 10000, 0.0001):
        with pytest.raises(InvalidArgument):
            format_number(bad)


def test_parse_reply():
    reply = parse_reply(b"\x0200I1.250W0.000ML\x03")
    assert (reply.address, reply.status, reply.data, reply.alarm) == (0, "I", "1.250W0.000ML", None)
    assert parse_reply(b"\x0203A?S\x03").alarm == "motor stalled"


async def test_connect_clears_power_on_alarm_and_configures_syringe():
    fake = FakeNE1000(address=3)
    pump = await make(fake)
    sent = pump.backend.transport.sent
    assert sent[:2] == [b"03VER\r", b"03VER\r"]  # first answered with the reset alarm, so asked again
    assert fake.commands == ["VER", "DIA 14.43", "VOL ML"]
    assert pump.describe()["driver"]["firmware"] == "NE1000V3.928"


async def test_dispense_end_to_end():
    fake = FakeNE1000()
    pump = await make(fake)
    run = await pump.dispense(1.0, 5.0)
    assert run.moved_ml == pytest.approx(1.0)
    assert fake.rate == (5.0, "MM") and fake.volume == 1.0 and fake.direction == "INF"
    assert fake.commands[-1] == "DIS" and "RUN" in fake.commands


async def test_slow_rates_are_sent_in_microlitres():
    fake = FakeNE1000()
    pump = await make(fake, min_rate_ml_min=0.001)
    await pump.withdraw(0.5, 0.025)
    assert fake.rate == (25.0, "UM") and fake.direction == "WDR"


async def test_pump_reports_out_of_range():
    fake = FakeNE1000(max_volume=5)
    pump = await make(fake)
    with pytest.raises(InvalidArgument, match="out of range"):
        await pump.dispense(8, 5)
    assert pump.state is DeviceState.IDLE


async def test_stall_alarm_faults_the_device():
    fake = FakeNE1000()
    fake.stall_after = 0.5
    pump = await make(fake)
    with pytest.raises(HardwareError, match="stalled"):
        await pump.dispense(2, 5)
    assert pump.state is DeviceState.ERROR
