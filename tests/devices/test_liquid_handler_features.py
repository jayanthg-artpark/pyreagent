"""Partial tips, 96-channel heads and the optional liquid-handling features, on the simulated driver."""

from __future__ import annotations

import pytest

from pyreagent.core import DeviceHub, InvalidArgument, NeedsAttention, NotSupported
from pyreagent.devices.liquid_handler import LiquidHandler, PipetteInfo
from pyreagent.devices.liquid_handler.simulated import SimulatedLiquidHandlerBackend

DECK = {
    "1": "generic_96_tiprack_300ul",
    "2": "generic_96_tiprack_20ul",
    "3": {"labware": "generic_12_reservoir_15ml", "initial_volume_ul": 10000},
    "4": {"labware": "corning_96_wellplate_360ul", "initial_volume_ul": 0},
    "5": "generic_96_tiprack_1000ul",
}
PIPETTES = [
    PipetteInfo(name="left", channels=1, min_volume_ul=20, max_volume_ul=300),
    PipetteInfo(name="right", channels=8, min_volume_ul=1, max_volume_ul=20),
    PipetteInfo(name="head", channels=96, min_volume_ul=5, max_volume_ul=1000, tip_volumes_ul=[1000]),
]


@pytest.fixture
async def lh():
    backend = SimulatedLiquidHandlerBackend(pipettes=PIPETTES)
    hub = DeviceHub([LiquidHandler("lh", backend, deck=DECK)])
    await hub.start()
    yield hub
    await hub.stop()


def ops(hub: DeviceHub, kind: str | None = None) -> list[dict]:
    return [o for o in hub.device("lh").backend.operations if kind is None or o["op"] == kind]


def deck_slot(hub: DeviceHub, name: str) -> dict:
    return next(s for s in hub.device("lh")._deck.snapshot({}).model_dump()["slots"] if s["slot"] == name)


async def test_single_nozzle_on_an_eight_channel(lh: DeviceHub):
    single = {"style": "single"}
    state = (await lh.invoke("lh", "pick_up_tip", {"pipette": "right", "nozzles": single})).result
    assert state["layout"]["style"] == "single"
    assert ops(lh, "configure_nozzles")[0]["layout"] == single
    assert ops(lh, "pick_up")[0]["well"] == "A1"  # the safe position: nothing behind the H1 nozzle
    await lh.invoke("lh", "aspirate", {"slot": "3", "well": "A1", "volume_ul": 10, "pipette": "right"})
    await lh.invoke("lh", "dispense", {"slot": "4", "well": "D5", "pipette": "right"})
    assert deck_slot(lh, "4")["volumes_ul"] == {"D5": 10}  # one well, not a column
    assert deck_slot(lh, "3")["volumes_ul"]["A1"] == 9990
    await lh.invoke("lh", "drop_tip", {"pipette": "right"})
    # The next single tip is B1 (A1 is gone, so nothing is left behind the H1 nozzle).
    await lh.invoke("lh", "pick_up_tip", {"pipette": "right", "nozzles": single})
    assert ops(lh, "pick_up")[-1]["well"] == "B1"


async def test_partial_tips_refuse_positions_that_would_grab_extra_tips(lh: DeviceHub):
    with pytest.raises(InvalidArgument, match="unused nozzles would land on tips at A1, B1, C1"):
        await lh.invoke("lh", "pick_up_tip", {"pipette": "right", "slot": "2", "well": "D1", "nozzles": {"style": "single"}})
    partial = {"style": "partial_column", "nozzles": 3}
    await lh.invoke("lh", "pick_up_tip", {"pipette": "right", "nozzles": partial})
    assert ops(lh, "pick_up")[0]["well"] == "C1"  # H1 nozzle at C1 takes A1-C1
    await lh.invoke("lh", "aspirate", {"slot": "3", "well": "A2", "volume_ul": 5, "pipette": "right"})
    assert deck_slot(lh, "3")["volumes_ul"]["A2"] == 10000 - 15  # three nozzles share the trough
    await lh.invoke("lh", "dispense", {"slot": "4", "well": "C1", "pipette": "right"})
    assert deck_slot(lh, "4")["volumes_ul"] == {"A1": 5, "B1": 5, "C1": 5}
    with pytest.raises(InvalidArgument, match="outside"):  # H1 nozzle at A1 would put the other two above row A
        await lh.invoke("lh", "aspirate", {"slot": "4", "well": "A1", "volume_ul": 1, "pipette": "right"})


async def test_ninety_six_channel_head(lh: DeviceHub):
    await lh.invoke("lh", "pick_up_tip", {"pipette": "head"})
    assert deck_slot(lh, "5")["tips_remaining"] == 0
    await lh.invoke("lh", "aspirate", {"slot": "3", "well": "A1", "volume_ul": 50, "pipette": "head"})
    assert deck_slot(lh, "3")["volumes_ul"]["A12"] == 10000 - 8 * 50  # each trough column feeds 8 nozzles
    await lh.invoke("lh", "dispense", {"slot": "4", "well": "A1", "pipette": "head"})
    assert len(deck_slot(lh, "4")["volumes_ul"]) == 96
    with pytest.raises(InvalidArgument, match="first nozzle at A1"):
        await lh.invoke("lh", "aspirate", {"slot": "4", "well": "B1", "volume_ul": 10, "pipette": "head"})
    await lh.invoke("lh", "drop_tip", {"pipette": "head"})
    with pytest.raises(NeedsAttention, match="No unused tips"):
        await lh.invoke("lh", "pick_up_tip", {"pipette": "head"})


async def test_ninety_six_channel_column_pickup_order(lh: DeviceHub):
    await lh.invoke("lh", "load_labware", {"slot": "5", "labware": "generic_96_tiprack_1000ul"})
    column = {"style": "column"}
    for expected in ("A1", "A2", "A3"):
        await lh.invoke("lh", "pick_up_tip", {"pipette": "head", "nozzles": column})
        assert ops(lh, "pick_up")[-1]["well"] == expected
        await lh.invoke("lh", "drop_tip", {"pipette": "head"})
    with pytest.raises(NotSupported):
        await lh.invoke("lh", "pick_up_tip", {"pipette": "head", "nozzles": {"style": "partial_column", "nozzles": 2}})


async def test_blow_out_touch_tip_mix_and_return(lh: DeviceHub):
    await lh.invoke("lh", "pick_up_tip", {"pipette": "left"})
    await lh.invoke("lh", "aspirate", {"slot": "3", "well": "A1", "volume_ul": 200, "pipette": "left"})
    await lh.invoke("lh", "dispense", {"slot": "4", "well": "A1", "volume_ul": 150, "pipette": "left"})
    state = (await lh.invoke("lh", "blow_out", {"slot": "4", "well": "A1", "pipette": "left"})).result
    assert state["tip_contents_ul"] == 0 and deck_slot(lh, "4")["volumes_ul"]["A1"] == 200
    await lh.invoke("lh", "touch_tip", {"slot": "4", "well": "A1", "pipette": "left"})
    job = await lh.jobs.wait((await lh.invoke("lh", "mix", {"slot": "4", "well": "A1", "volume_ul": 50, "cycles": 2, "pipette": "left"})).job.id, 5)
    assert job.status == "succeeded", job.error
    assert deck_slot(lh, "4")["volumes_ul"]["A1"] == 200  # mixing moves nothing in or out
    returned = (await lh.invoke("lh", "return_tip", {"pipette": "left"})).result
    assert not returned["tip_attached"] and deck_slot(lh, "1")["tips_remaining"] == 96
    assert [o["op"] for o in ops(lh)][-6:] == ["touch_tip", "aspirate", "dispense", "aspirate", "dispense", "return_tips"]


async def test_transfer_with_mixing_touch_tip_and_blow_out(lh: DeviceHub):
    args = {"source_slot": "3", "source_wells": ["A1"], "destination_slot": "4", "destination_wells": ["A1"], "volume_ul": 100,
            "pipette": "left", "mix_before_cycles": 1, "mix_after_cycles": 1, "touch_tip": True, "blow_out": True}
    job = await lh.jobs.wait((await lh.invoke("lh", "transfer", args)).job.id, 5)
    assert job.status == "succeeded", job.error
    assert [o["op"] for o in ops(lh)] == ["pick_up", "aspirate", "dispense", "aspirate", "touch_tip", "dispense", "touch_tip",
                                          "aspirate", "dispense", "blow_out", "drop"]
    assert deck_slot(lh, "4")["volumes_ul"]["A1"] == 100


async def test_move_labware_probe_and_lights(lh: DeviceHub):
    job = await lh.jobs.wait((await lh.invoke("lh", "move_labware", {"from_slot": "4", "to_slot": "6"})).job.id, 5)
    assert job.status == "succeeded" and deck_slot(lh, "6")["labware"] == "corning_96_wellplate_360ul"
    assert ops(lh, "move_labware")[0]["use_gripper"] is True
    with pytest.raises(InvalidArgument, match="already holds"):
        await lh.invoke("lh", "move_labware", {"from_slot": "6", "to_slot": "3"})
    await lh.invoke("lh", "pick_up_tip", {"pipette": "left"})
    lh.device("lh").backend.liquid_heights_mm[("3", "A2")] = None
    heights = (await lh.jobs.wait((await lh.invoke("lh", "probe_liquid_heights", {"slot": "3", "wells": ["A1", "A2"], "pipette": "left"})).job.id, 5)).result
    assert heights == {"slot": "3", "heights_mm": {"A1": 5.0, "A2": None}}
    await lh.invoke("lh", "aspirate", {"slot": "3", "well": "A1", "volume_ul": 50, "pipette": "left"})
    with pytest.raises(InvalidArgument, match="empty tip"):
        await lh.invoke("lh", "probe_liquid_heights", {"slot": "3", "wells": ["A1"], "pipette": "left"})
    await lh.invoke("lh", "set_lights", {"on": True})
    assert lh.device("lh").backend.lights is True


async def test_features_a_driver_lacks_are_refused_up_front():
    class Basic(SimulatedLiquidHandlerBackend):
        features = frozenset()

    hub = DeviceHub([LiquidHandler("lh", Basic(pipettes=PIPETTES), deck=DECK)])
    await hub.start()
    for capability, args in [("set_lights", {"on": True}), ("move_labware", {"from_slot": "4", "to_slot": "6"}),
                             ("pick_up_tip", {"pipette": "right", "nozzles": {"style": "single"}}),
                             ("transfer", {"source_slot": "3", "source_wells": ["A1"], "destination_slot": "4", "destination_wells": ["A1"],
                                           "volume_ul": 50, "pipette": "left", "blow_out": True})]:
        with pytest.raises(NotSupported):
            await hub.invoke("lh", capability, args)
    assert hub.device("lh").backend.operations == []
    await hub.stop()
