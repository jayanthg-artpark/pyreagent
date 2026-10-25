"""Tecan EVO driver against a fake firmware, checked against the command strings PyLabRobot's tests expect."""

from __future__ import annotations

import pytest

from fake_tecan import FakeEvo
from pyreagent.core import DeviceHub, InvalidArgument, NeedsAttention, PyreagentError
from pyreagent.devices.liquid_handler import LiquidHandler
from pyreagent.devices.liquid_handler.tecan import TecanEvoBackend
from pyreagent.io import MockUsbTransport
from pyreagent.vendors.tecan.firmware import decode, encode

# PyLabRobot's EVO test deck: DiTi_SBS_3_Pos_MCA96 carrier on rail 10, MP_3Pos_PCR on rail 16.
CARRIERS = {"tips": {"type": "DiTi_SBS_3_Pos_MCA96", "rail": 10}, "plates": {"type": "MP_3Pos_PCR", "rail": 16}}
DECK = {"tips.1": "tecan_diti_100ul_te_mo", "plates.1": {"labware": "tecan_deepwell_96_well", "initial_volume_ul": 1000}}
N = ",".join(["2000"] * 7)


async def start(fake: FakeEvo, **options) -> DeviceHub:
    backend = TecanEvoBackend(carriers=CARRIERS, transport=MockUsbTransport(fake), prime_on_connect=False, **options)
    hub = DeviceHub([LiquidHandler("evo", backend, deck=DECK)])
    await hub.start()
    return hub


def test_framing():
    assert encode("C5", "PAA", [2380, 1991, 90, None]) == b"\x02C5PAA2380,1991,90,\x00"
    assert decode(b"\x02C5\x808\x00") == ("C5", 0, [8])
    assert decode(b"\x02C5\x99\x00")[1] == 25


async def test_initialisation_sequence():
    fake = FakeEvo()
    hub = await start(fake)
    assert hub.describe_device("evo")["state"] == "idle", hub.describe_device("evo")["last_error"]
    assert fake.without_queries()[:9] == ["C5PIA", "C5BMX2", "W1PIB", "C1PIA", "C1BMX2", "C1PIX", "C1SAA1,9000,2000,2464,1800,,1,0,0",
                                          "C1AAC", "C5PIX"]
    assert "gripper" in hub.describe_device("evo")["limits"]["features"]
    await hub.stop()


async def test_golden_pickup_aspirate_dispense():
    fake = FakeEvo()
    hub = await start(fake)
    fake.commands.clear()
    await hub.invoke("evo", "pick_up_tip", {"slot": "tips.1", "well": "A1", "nozzles": {"style": "single"}})
    assert fake.without_queries() == ["C5SHZ" + ",".join(["2000"] * 8), f"C5PAA2380,1991,90,2000,{N}", "C5PVL0,,,,,,,",
                                      "C5SEP420,,,,,,,", "C5PPR30,,,,,,,", "C5AGT1,1023,210,0"]
    fake.commands.clear()
    await hub.invoke("evo", "aspirate", {"slot": "plates.1", "well": "A1", "volume_ul": 100, "flow_rate_ul_s": 100})
    # SHZ 2000s and the lag-gap SEP 420 repeat the previous values, so the set-command cache skips them.
    assert fake.without_queries() == [
        f"C5PAA3829,2051,90,2100,{N}", "C5PVL0,,,,,,,", "C5PPR15,,,,,,,", "C5SDM7,1", "C5SSL600,,,,,,,", "C5SDL40,,,,,,,",
        "C5STL1375,,,,,,,", f"C5SML985,{N}", "C5SBL20,,,,,,,", "C5SHZ" + ",".join(["2100"] * 8), "C5MDT1,,,,30," + ",".join(["0"] * 7),
        "C5SHZ" + ",".join(["2000"] * 8), "C5SSZ30,,,,,,,", "C5SEP600,,,,,,,", "C5STZ-30,,,,,,,", "C5MTR313,,,,,,,",
        "C5SSZ200,,,,,,,", "C5MAZ1375,,,,,,,", "C5PVL0,,,,,,,", "C5SEP420,,,,,,,", "C5PPR30,,,,,,,",
    ]
    fake.commands.clear()
    await hub.invoke("evo", "dispense", {"slot": "plates.1", "well": "A1", "volume_ul": 100, "flow_rate_ul_s": 100})
    assert fake.without_queries() == ["C5SHZ" + ",".join(["2100"] * 8), f"C5PAA3829,2051,90,1355,{N}", "C5SEP600,,,,,,,",
                                      "C5SPP2400,,,,,,,", "C5STZ0,,,,,,,", "C5MTR-358,,,,,,,"]
    await hub.stop()


async def test_golden_roma_pick_and_place():
    fake = FakeEvo()
    hub = await start(fake)
    fake.x["C5"] = 0  # LiHa parked far away
    fake.commands.clear()
    job = await hub.jobs.wait((await hub.invoke("evo", "move_labware", {"from_slot": "plates.1", "to_slot": "plates.2"})).job.id, 5)
    assert job.status == "succeeded", job.error
    sent = fake.without_queries()
    assert sent[:6] == ["C1RPZ5", "C1SSM1", "C1SFX10000,", "C1SFY5000,1500", "C1SFZ1300,", "C1SFR5000,1500"]
    assert "C1SAA1,5561,2368,1054,900,,1,0,0" in sent and "C1SAA1,5561,2368,59,900,,1,0,0" in sent and "C1AGR754" in sent
    # destination: plates.2 (site y 109.5 mm further back)
    assert "C1SAA6,5561,1408,59,900,,1,0,0" in sent and sent[-3:] == ["C1AAC", "C1SFY3500,1000", "C1SFR2000,600"]
    deck = (await hub.invoke("evo", "get_deck")).result
    assert next(s for s in deck["slots"] if s["slot"] == "plates.2")["labware"] == "tecan_deepwell_96_well"
    await hub.stop()


async def test_full_column_and_trough_spacing():
    fake = FakeEvo()
    backend = TecanEvoBackend(carriers={**CARRIERS, "tips2": {"type": "DiTi_3Pos", "rail": 4}}, transport=MockUsbTransport(fake),
                              prime_on_connect=False)
    deck = {**DECK, "tips2.1": "tecan_diti_200ul_liha"}
    hub = DeviceHub([LiquidHandler("evo", backend, deck=deck)])
    await hub.start()
    await hub.invoke("evo", "pick_up_tip", {"slot": "tips2.1"})
    agt = next(c for c in fake.commands if c.startswith("C5AGT"))
    assert agt.startswith("C5AGT255,")  # all 8 channels
    await hub.invoke("evo", "aspirate", {"slot": "plates.1", "well": "A2", "volume_ul": 50})
    paa = [c for c in fake.commands if c.startswith("C5PAA")][-1]
    assert paa.split(",")[2] == "90"  # 9 mm between channels
    await hub.stop()


async def test_labware_without_tecan_parameters_is_refused():
    hub = await start(FakeEvo())
    with pytest.raises(InvalidArgument, match="EVOware z parameters"):
        await hub.invoke("evo", "load_labware", {"slot": "plates.2", "labware": "corning_96_wellplate_360ul_flat"})
    with pytest.raises(InvalidArgument, match="No worktable slot|No deck slot"):
        await hub.invoke("evo", "load_labware", {"slot": "plates.9", "labware": "tecan_deepwell_96_well"})
    await hub.stop()


async def test_firmware_errors_become_structured():
    fake = FakeEvo()
    hub = await start(fake)
    fake.fail["AGT"] = 25
    with pytest.raises(PyreagentError) as err:
        await hub.invoke("evo", "pick_up_tip", {"slot": "tips.1", "well": "A1", "nozzles": {"style": "single"}})
    assert err.value.code == "hardware_error" and "tip not fetched" in err.value.message
    await hub.stop()

    no_liquid = FakeEvo()
    hub = await start(no_liquid)
    await hub.invoke("evo", "pick_up_tip", {"slot": "tips.1", "well": "A1", "nozzles": {"style": "single"}})
    no_liquid.fail["MDT"] = 9
    with pytest.raises(NeedsAttention, match="no liquid detected"):
        await hub.invoke("evo", "aspirate", {"slot": "plates.1", "well": "A1", "volume_ul": 100})
    await hub.stop()


async def test_no_roma_means_no_gripper():
    hub = await start(FakeEvo(roma=False))
    assert "gripper" not in hub.describe_device("evo")["limits"]["features"]
    with pytest.raises(PyreagentError, match="does not support gripper"):
        await hub.invoke("evo", "move_labware", {"from_slot": "plates.1", "to_slot": "plates.2"})
    await hub.stop()
