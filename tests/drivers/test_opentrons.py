from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from fake_opentrons import FakeOpentronsRobot
from pyreagent.core import DeviceHub, DeviceState, InvalidArgument, PyreagentError
from pyreagent.devices.liquid_handler import LiquidHandler
from pyreagent.devices.liquid_handler.opentrons import OpentronsBackend

DECK = {
    "1": "opentrons_96_tiprack_300ul",
    "2": "opentrons_96_tiprack_20ul",
    "3": {"labware": "nest_12_reservoir_15ml", "initial_volume_ul": 10000},
    "4": {"labware": "corning_96_wellplate_360ul_flat", "initial_volume_ul": 0},
}
SCHEMA = Path(__file__).resolve().parents[2] / "_reference/opentrons/shared-data/command/schemas/18.json"


async def start(robot: FakeOpentronsRobot, *, robot_type: str = "ot2", deck: dict = DECK, **options) -> DeviceHub:
    backend = OpentronsBackend(robot=robot_type, transport=robot.transport, **options)
    backend.session.client.poll_interval_s = 0.01
    hub = DeviceHub([LiquidHandler("ot-1", backend, deck=deck)])
    await hub.start()
    return hub


def transfer_args(**overrides) -> dict:
    return {"source_slot": "3", "source_wells": ["A1"], "destination_slot": "4", "destination_wells": ["A1", "B1"],
            "volume_ul": 50, "pipette": "left", **overrides}


async def run_job(hub: DeviceHub, capability: str, args: dict) -> dict:
    """Run a gated job; a failure before the job's first network wait is reported the same way as a failed job."""
    try:
        accepted = await hub.invoke("ot-1", capability, args)
    except PyreagentError as err:
        return {"status": "failed", "error": err.to_dict()}
    return (await hub.jobs.wait(accepted.job.id, 10)).model_dump(mode="json")


async def test_connect_reads_pipettes_and_claims_the_robot():
    robot = FakeOpentronsRobot()
    hub = await start(robot)
    info = hub.describe_device("ot-1")
    assert info["state"] == "idle"
    pipettes = {p["name"]: p for p in info["limits"]["pipettes"]}
    assert pipettes["left"]["channels"] == 1 and pipettes["left"]["max_volume_ul"] == 300
    assert pipettes["left"]["tip_volumes_ul"] == [200.0, 300.0]
    assert pipettes["right"]["channels"] == 8 and pipettes["right"]["tip_volumes_ul"] == [10.0, 20.0]
    assert info["driver"]["pipettes"] == {"left": "p300_single_gen2", "right": "p20_multi_gen2"}
    assert robot.commands() == ["loadPipette", "loadPipette"]
    assert robot.current == info["driver"]["run_id"]
    await hub.stop()
    assert robot.current is None and robot.runs[info["driver"]["run_id"]]["status"] == "stopped"


async def test_transfer_becomes_opentrons_commands():
    robot = FakeOpentronsRobot()
    hub = await start(robot)
    job = await run_job(hub, "transfer", transfer_args())
    assert job["status"] == "succeeded", job["error"]
    per_pair = ["pickUpTip", "aspirate", "dispense", "moveToAddressableAreaForDropTip", "dropTipInPlace"]
    assert [c for c in robot.commands() if c != "loadLabware"][2:] == per_pair * 2
    loads = [c["params"] for c in robot.sent if c["commandType"] == "loadLabware"]
    assert [(p["location"]["slotName"], p["loadName"], p["namespace"], p["version"]) for p in loads] == [
        ("1", "opentrons_96_tiprack_300ul", "opentrons", 1),
        ("3", "nest_12_reservoir_15ml", "opentrons", 3),
        ("4", "corning_96_wellplate_360ul_flat", "opentrons", 5),
    ]  # each loaded once, on first use, in the version Opentrons loads by default
    aspirate = next(c["params"] for c in robot.sent if c["commandType"] == "aspirate")
    assert aspirate["wellName"] == "A1" and aspirate["volume"] == 50
    assert aspirate["wellLocation"] == {"origin": "bottom", "offset": {"x": 0, "y": 0, "z": 1.0}}
    assert aspirate["flowRate"] == 92.86  # Opentrons' default for a P300 GEN2 with 300 µL tips
    drop = next(c["params"] for c in robot.sent if c["commandType"] == "moveToAddressableAreaForDropTip")
    assert drop["addressableAreaName"] == "fixedTrash"
    assert all(c["intent"] == "setup" for c in robot.sent)
    await hub.stop()


async def test_eight_channel_uses_matching_tips():
    robot = FakeOpentronsRobot()
    hub = await start(robot)
    job = await run_job(hub, "transfer", transfer_args(source_wells=["A2"], destination_wells=["A1"], volume_ul=10, pipette="right"))
    assert job["status"] == "succeeded", job["error"]
    pick = next(c["params"] for c in robot.sent if c["commandType"] == "pickUpTip")
    tiprack_20 = next(lw for lw, slot in robot.labware.items() if slot == "2")
    assert pick == {"pipetteId": "pipette-right", "labwareId": tiprack_20, "wellName": "A1"}
    await hub.stop()


async def test_unhomed_robot_needs_attention_then_works_after_home():
    robot = FakeOpentronsRobot(homed=False)
    hub = await start(robot)
    job = await run_job(hub, "transfer", transfer_args())
    assert job["status"] == "failed" and job["error"]["code"] == "needs_attention"
    assert "home" in job["error"]["suggested_action"]
    assert hub.device("ot-1").state is DeviceState.IDLE
    home = await hub.invoke("ot-1", "home")
    assert (await hub.jobs.wait(home.job.id, 5)).status == "succeeded"
    assert (await run_job(hub, "transfer", transfer_args()))["status"] == "succeeded"
    await hub.stop()


async def test_wrong_robot_model_or_busy_robot_stays_offline():
    hub = await start(FakeOpentronsRobot(model="OT-3 Standard"))
    info = hub.describe_device("ot-1")
    assert info["state"] == "offline" and "OT-3 Standard" in info["last_error"]["message"]

    busy = FakeOpentronsRobot()
    busy.current = "run-from-the-app"
    hub = await start(busy)
    info = hub.describe_device("ot-1")
    assert info["state"] == "offline" and info["last_error"]["code"] == "needs_attention"
    assert "Opentrons App" in info["last_error"]["message"]


async def test_labware_needs_an_opentrons_load_name():
    robot = FakeOpentronsRobot()
    hub = await start(robot)
    with pytest.raises(InvalidArgument, match="no Opentrons load name"):
        await hub.invoke("ot-1", "load_labware", {"slot": "5", "labware": "generic_96_wellplate"})
    await hub.stop()

    mapped = await start(FakeOpentronsRobot(), deck={**DECK, "1": "generic_96_tiprack_300ul"},
                         load_names={"generic_96_tiprack_300ul": {"load_name": "opentrons_96_tiprack_300ul", "version": 2}})
    assert mapped.describe_device("ot-1")["state"] == "idle"
    await mapped.stop()


async def test_redeclared_slot_moves_old_labware_off_deck():
    robot = FakeOpentronsRobot()
    hub = await start(robot)
    assert (await run_job(hub, "transfer", transfer_args(destination_wells=["A1"])))["status"] == "succeeded"
    await hub.invoke("ot-1", "load_labware", {"slot": "4", "labware": "nest_96_wellplate_200ul_flat", "initial_volume_ul": 0})
    assert (await run_job(hub, "transfer", transfer_args(destination_wells=["A1"])))["status"] == "succeeded"
    move = next(c["params"] for c in robot.sent if c["commandType"] == "moveLabware")
    assert move["newLocation"] == "offDeck" and move["strategy"] == "manualMoveWithoutPause"
    assert [c["params"]["loadName"] for c in robot.sent if c["commandType"] == "loadLabware"][-1] == "nest_96_wellplate_200ul_flat"
    await hub.stop()


async def test_robot_is_homed_after_the_run_is_released():
    robot = FakeOpentronsRobot()
    hub = await start(robot)
    assert (await run_job(hub, "transfer", transfer_args()))["status"] == "succeeded"
    assert robot.robot_homes == []
    await hub.stop()
    assert robot.current is None and robot.robot_homes == [None]  # released first, then homed


async def test_homing_after_the_run_can_be_turned_off():
    robot = FakeOpentronsRobot()
    hub = await start(robot, home_after_run=False)
    await hub.stop()
    assert robot.current is None and robot.robot_homes == []


async def test_slow_commands_are_polled():
    robot = FakeOpentronsRobot(running_polls=1)
    hub = await start(robot)  # the first loadPipette comes back "running" and is polled to completion
    assert hub.describe_device("ot-1")["state"] == "idle"
    assert robot.polls == 1
    await hub.stop()


async def test_stop_mid_transfer_faults_and_reset_starts_a_new_run():
    robot = FakeOpentronsRobot(delay_s=0.05)
    hub = await start(robot)
    first_run = robot.current
    accepted = await hub.invoke("ot-1", "transfer", transfer_args(destination_wells=["A1", "A2", "A3", "A4"]))
    await asyncio.sleep(0.12)
    await hub.invoke("ot-1", "stop", caller="human")
    assert (await hub.jobs.wait(accepted.job.id, 5)).status == "cancelled"
    assert hub.device("ot-1").state is DeviceState.ERROR
    assert robot.runs[first_run]["status"] in ("stop-requested", "stopped")
    await hub.invoke("ot-1", "reset", {})
    assert hub.device("ot-1").state is DeviceState.IDLE
    assert robot.current != first_run and robot.runs[first_run]["current"] is False
    assert robot.commands(robot.current) == ["loadPipette", "loadPipette"]
    await hub.stop()


async def test_flex_deck_tips_and_trash():
    flex_pipette = {"mount": "left", "instrumentType": "pipette", "instrumentName": "p1000_multi_flex",
                    "instrumentModel": "p1000_multi_v3.5", "serialNumber": "P1KMV35", "ok": True,
                    "data": {"channels": 8, "min_volume": 5.0, "max_volume": 1000.0}}
    head_96 = {"mount": "right", "instrumentType": "pipette", "instrumentName": "p1000_96", "instrumentModel": "p1000_96_v3.6",
               "serialNumber": "P1KH96", "ok": True, "data": {"channels": 96, "min_volume": 5.0, "max_volume": 1000.0}}
    robot = FakeOpentronsRobot(model="OT-3 Standard", instruments=[flex_pipette, head_96])
    deck = {"B1": "opentrons_flex_96_tiprack_200ul", "C1": {"labware": "nest_12_reservoir_15ml", "initial_volume_ul": 10000},
            "D1": {"labware": "nest_96_wellplate_2ml_deep", "initial_volume_ul": 0}}
    hub = await start(robot, robot_type="flex", deck=deck)
    info = hub.describe_device("ot-1")
    assert "A3" not in info["limits"]["slots"]  # trash bin
    assert {p["name"]: p["channels"] for p in info["limits"]["pipettes"]} == {"left": 8, "right": 96}
    assert info["driver"]["unsupported_pipettes"] == []
    assert next(p for p in info["limits"]["pipettes"] if p["name"] == "left")["tip_volumes_ul"] == [50.0, 200.0, 1000.0]
    args = {"source_slot": "C1", "source_wells": ["A1"], "destination_slot": "D1", "destination_wells": ["A1"], "volume_ul": 150,
            "pipette": "left"}
    job = await run_job(hub, "transfer", args)
    assert job["status"] == "succeeded", job["error"]
    assert next(c["params"] for c in robot.sent if c["commandType"] == "moveToAddressableAreaForDropTip")["addressableAreaName"] == "movableTrashA3"
    await hub.stop()


async def test_commands_match_the_official_opentrons_schema():
    jsonschema = pytest.importorskip("jsonschema")
    if not SCHEMA.exists():
        pytest.skip("Opentrons reference not cloned; see scripts/gen_opentrons_labware.py for the checkout command")
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
    robot = FakeOpentronsRobot()
    hub = await start(robot)
    await run_job(hub, "transfer", transfer_args())
    await hub.invoke("ot-1", "load_labware", {"slot": "4", "labware": "nest_96_wellplate_200ul_flat", "initial_volume_ul": 0})
    await run_job(hub, "transfer", transfer_args(destination_wells=["A1"]))
    await hub.jobs.wait((await hub.invoke("ot-1", "home")).job.id, 5)
    await hub.stop()
    seen = set()
    for command in robot.sent:
        body = {k: v for k, v in command.items() if not k.startswith("_")}
        errors = sorted(validator.iter_errors(body), key=lambda e: e.path)
        assert not errors, f"{body['commandType']}: {[e.message for e in errors][:3]}"
        seen.add(body["commandType"])
    assert seen == {"loadPipette", "loadLabware", "moveLabware", "pickUpTip", "aspirate", "dispense",
                    "moveToAddressableAreaForDropTip", "dropTipInPlace", "home"}
