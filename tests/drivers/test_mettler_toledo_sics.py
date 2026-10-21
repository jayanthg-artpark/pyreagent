from __future__ import annotations

import pytest

from fakes import FakeSICSBalance
from pyreagent.core import DeviceTimeout, NeedsAttention, NotSupported
from pyreagent.devices.balance import Balance
from pyreagent.devices.balance.mettler_toledo import MettlerToledoSICSBackend
from pyreagent.devices.balance.mettler_toledo.sics import tokenize
from pyreagent.io import MockTransport


async def make(fake: FakeSICSBalance) -> tuple[Balance, MockTransport]:
    transport = MockTransport(fake)
    balance = Balance("bal", MettlerToledoSICSBackend(transport=transport))
    await balance.connect()
    return balance, transport


def test_tokenize_keeps_quoted_strings():
    assert tokenize('I2 A "XS205DU 220.0000 g"') == ["I2", "A", "XS205DU 220.0000 g"]
    assert tokenize("S S      100.0012 g") == ["S", "S", "100.0012", "g"]
    assert tokenize('I4 A ""') == ["I4", "A", ""]


async def test_connect_reads_identity():
    balance, transport = await make(FakeSICSBalance())
    assert transport.sent[:2] == [b"@\r\n", b"I2\r\n"]
    info = balance.describe()["driver"]
    assert info["serial_number"] == "B021002593"
    assert info["balance_type"] == "XS205DU"
    assert balance.limits() == {"capacity_g": 220.0}


async def test_tare_and_weigh():
    fake = FakeSICSBalance()
    balance, transport = await make(fake)
    fake.load_g = 31.2
    assert (await balance.tare()).tare_g == pytest.approx(31.2)
    fake.load_g += 2.5
    weight = await balance.weigh()
    assert weight.grams == pytest.approx(2.5) and weight.stable
    assert transport.sent[-1] == b"S\r\n"


async def test_immediate_read_and_units():
    fake = FakeSICSBalance()
    balance, transport = await make(fake)
    fake.settled, fake.unit, fake.load_g = False, "mg", 0.0123
    weight = await balance.weigh(stable=False)
    assert transport.sent[-1] == b"SI\r\n"
    assert weight.grams == pytest.approx(0.0123) and not weight.stable


async def test_status_codes_become_structured_errors():
    fake = FakeSICSBalance()
    balance, _ = await make(fake)
    fake.overloaded = True
    with pytest.raises(NeedsAttention):
        await balance.weigh()
    fake.overloaded, fake.settled = False, False
    with pytest.raises(DeviceTimeout) as err:
        await balance.weigh()
    assert err.value.recoverable
    with pytest.raises(NotSupported):
        await balance.backend._command("XYZ")
    assert balance.state.value == "idle"  # none of these leave the balance faulted
