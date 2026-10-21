from __future__ import annotations

import asyncio

import pytest

from conftest import approve_and_call, make_devices
from pyreagent.core import DeviceHub, DeviceState, InvalidArgument, NeedsAttention


def slot(deck: dict, name: str) -> dict:
    return next(s for s in deck["slots"] if s["slot"] == name)


async def test_deck_from_config(hub: DeviceHub):
    deck = (await hub.invoke("lh-1", "get_deck")).result
    assert slot(deck, "1")["tips_remaining"] == 96
    assert slot(deck, "3")["volumes_ul"]["A1"] == 10000
    assert slot(deck, "4")["volumes_ul"] == {} and slot(deck, "4")["unknown_volume_wells"] == 0
    assert {p["name"] for p in deck["pipettes"]} == {"left", "right"}
    assert hub.describe_device("lh-1")["limits"]["slots"][0] == "1"


async def test_transfer_moves_liquid_and_tracks_tips(hub: DeviceHub):
    args = {"source_slot": "3", "source_wells": ["A1"], "destination_slot": "4",
            "destination_wells": ["A1", "B1", "C1"], "volume_ul": 100, "pipette": "left"}
    accepted = await approve_and_call(hub, "lh-1", "transfer", args)
    job = await hub.jobs.wait(accepted.job.id, 5)
    assert job.status == "succeeded", job.error
    assert job.result["transfers"] == 3 and job.result["tips_used"] == 3
    deck = (await hub.invoke("lh-1", "get_deck")).result
    assert slot(deck, "3")["volumes_ul"]["A1"] == 9700
    assert slot(deck, "4")["volumes_ul"] == {"A1": 100, "B1": 100, "C1": 100}
    assert slot(deck, "1")["tips_remaining"] == 93
    ops = [o["op"] for o in hub.device("lh-1").backend.operations]
    assert ops == ["pick_up", "aspirate", "dispense", "drop"] * 3


async def test_large_volume_is_split_into_trips():
    devices = make_devices()
    lh = next(d for d in devices if d.name == "lh-1")
    await lh.connect()
    await lh.load_labware("4", "generic_96_deepwell_2ml", initial_volume_ul=0)
    result = await lh.transfer("3", ["A1"], "4", ["A1"], 700, pipette="left")
    aspirates = [o["volume_ul"] for o in lh.backend.operations if o["op"] == "aspirate"]
    assert aspirates == [pytest.approx(233.3333, abs=1e-3)] * 3
    assert result.operations == 8  # pick, 3 x (asp, disp), drop


async def test_eight_channel_addresses_columns(hub: DeviceHub):
    args = {"source_slot": "3", "source_wells": ["A2"], "destination_slot": "4",
            "destination_wells": ["A1", "A2"], "volume_ul": 10, "pipette": "right", "new_tip": "once"}
    job = await hub.jobs.wait((await approve_and_call(hub, "lh-1", "transfer", args)).job.id, 5)
    assert job.status == "succeeded", job.error
    deck = (await hub.invoke("lh-1", "get_deck")).result
    assert slot(deck, "3")["volumes_ul"]["A2"] == 10000 - 2 * 8 * 10  # all 8 channels share the trough
    assert len(slot(deck, "4")["volumes_ul"]) == 16  # columns 1 and 2, all rows
    assert slot(deck, "2")["tips_remaining"] == 88  # one full column of 20 µL tips
    with pytest.raises(InvalidArgument, match="first channel"):
        await hub.invoke("lh-1", "transfer", {**args, "destination_wells": ["B1"]})


async def test_impossible_plans_are_refused_before_asking_a_human(hub: DeviceHub):
    base = {"source_slot": "3", "source_wells": ["A1"], "destination_slot": "4", "destination_wells": ["A1"], "pipette": "left"}
    with pytest.raises(InvalidArgument, match="holds 0 of 360 µL; this transfer adds 400"):
        await hub.invoke("lh-1", "transfer", {**base, "volume_ul": 400})
    with pytest.raises(InvalidArgument, match="below"):
        await hub.invoke("lh-1", "transfer", {**base, "volume_ul": 5})  # left pipette minimum 20 µL
    with pytest.raises(InvalidArgument, match="tip_rack|tip rack"):
        await hub.invoke("lh-1", "aspirate", {"slot": "1", "well": "A1", "volume_ul": 50, "pipette": "left"})
    with pytest.raises(InvalidArgument, match="no tip"):
        await hub.invoke("lh-1", "aspirate", {"slot": "3", "well": "A1", "volume_ul": 50, "pipette": "left"})
    with pytest.raises(InvalidArgument, match="several pipettes"):
        await hub.invoke("lh-1", "transfer", {**base, "pipette": None, "volume_ul": 50})
    with pytest.raises(InvalidArgument, match="empty"):
        await hub.invoke("lh-1", "transfer", {**base, "source_slot": "9", "volume_ul": 50})
    with pytest.raises(NeedsAttention, match="unused tips"):
        await hub.invoke("lh-1", "transfer", {**base, "destination_wells": [f"{r}{c}" for c in range(1, 13) for r in "ABCDEFGH"] + ["A1"],
                                              "volume_ul": 20})
    assert hub.confirmations.list() == []


async def test_source_volume_is_checked(hub: DeviceHub):
    await approve_and_call(hub, "lh-1", "load_labware", {"slot": "3", "labware": "generic_12_reservoir_15ml", "volumes_ul": {"A1": 150}})
    with pytest.raises(InvalidArgument, match="holds 150 µL; this transfer needs 200"):
        await hub.invoke("lh-1", "transfer", {"source_slot": "3", "source_wells": ["A1"], "destination_slot": "4",
                                              "destination_wells": ["A1", "A2"], "volume_ul": 100, "pipette": "left"})


async def test_low_level_steps(hub: DeviceHub):
    state = (await hub.invoke("lh-1", "pick_up_tip", {"pipette": "left"})).result
    assert state["tip_attached"] and state["tip_capacity_ul"] == 300
    await approve_and_call(hub, "lh-1", "aspirate", {"slot": "3", "well": "a1", "volume_ul": 120, "pipette": "left"})
    state = (await approve_and_call(hub, "lh-1", "dispense", {"slot": "4", "well": "H12", "pipette": "left"})).result
    assert state["tip_contents_ul"] == 0
    assert (await hub.invoke("lh-1", "get_deck")).result["slots"][3]["volumes_ul"] == {"H12": 120}
    state = (await hub.invoke("lh-1", "drop_tip", {"pipette": "left"})).result
    assert not state["tip_attached"]


async def test_stop_mid_transfer_faults_until_reset():
    hub = DeviceHub(make_devices(lh_seconds_per_op=0.05))
    await hub.start()
    lh = hub.device("lh-1")
    args = {"source_slot": "3", "source_wells": ["A1"], "destination_slot": "4",
            "destination_wells": ["A1", "A2", "A3", "A4"], "volume_ul": 50, "pipette": "left"}
    accepted = await approve_and_call(hub, "lh-1", "transfer", args)
    await asyncio.sleep(0.12)
    await hub.invoke("lh-1", "stop", caller="human")
    job = await hub.jobs.wait(accepted.job.id, 5)
    assert job.status == "cancelled"
    assert lh.state is DeviceState.ERROR and lh.describe()["last_error"]["code"] == "interrupted"
    await approve_and_call(hub, "lh-1", "reset", {})
    assert lh.state is DeviceState.IDLE
    assert not (await hub.invoke("lh-1", "get_deck")).result["pipettes"][0]["tip_attached"]
    await hub.stop()


async def test_tips_must_fit_the_pipette(hub: DeviceHub):
    state = (await hub.invoke("lh-1", "pick_up_tip", {"pipette": "right"})).result
    assert state["tip_capacity_ul"] == 20  # smallest compatible rack (slot 2), not the 300 µL rack in slot 1
    await hub.invoke("lh-1", "drop_tip", {"pipette": "right"})
    with pytest.raises(InvalidArgument, match="does not take"):
        await hub.invoke("lh-1", "pick_up_tip", {"pipette": "left", "slot": "2"})  # 20 µL tips on a 300 µL pipette


async def test_serial_dilution_within_one_plate(hub: DeviceHub):
    await approve_and_call(hub, "lh-1", "load_labware", {"slot": "4", "labware": "corning_96_wellplate_360ul",
                                                         "initial_volume_ul": 100, "volumes_ul": {"A1": 200}})
    args = {"source_slot": "4", "source_wells": ["A1", "A2", "A3"], "destination_slot": "4",
            "destination_wells": ["A2", "A3", "A4"], "volume_ul": 100, "pipette": "left", "mix_after_cycles": 2}
    job = await hub.jobs.wait((await approve_and_call(hub, "lh-1", "transfer", args)).job.id, 5)
    assert job.status == "succeeded", job.error
    volumes = slot((await hub.invoke("lh-1", "get_deck")).result, "4")["volumes_ul"]
    assert [volumes[w] for w in ("A1", "A2", "A3", "A4")] == [100, 100, 100, 200]


async def test_reloading_a_tip_rack_resets_tracking(hub: DeviceHub):
    await hub.invoke("lh-1", "pick_up_tip", {"pipette": "left"})
    await hub.invoke("lh-1", "drop_tip", {"pipette": "left"})
    await approve_and_call(hub, "lh-1", "load_labware", {"slot": "1", "labware": "generic_96_tiprack_300ul"})
    assert slot((await hub.invoke("lh-1", "get_deck")).result, "1")["tips_remaining"] == 96
    with pytest.raises(InvalidArgument, match="not liquid"):
        await hub.invoke("lh-1", "load_labware", {"slot": "1", "labware": "generic_96_tiprack_300ul", "initial_volume_ul": 5})
