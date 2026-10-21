"""Opentrons liquid-handler features beyond plain pipetting, against the fake robot-server."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fake_opentrons import GRIPPER, FakeOpentronsRobot, flex_pipette, thermocycler
from pyreagent.core import DeviceHub, DeviceState, InvalidArgument, NotSupported, PyreagentError
from pyreagent.devices.liquid_handler import LiquidHandler
from pyreagent.devices.liquid_handler.opentrons import OpentronsBackend
from pyreagent.devices.thermocycler import Thermocycler
from pyreagent.devices.thermocycler.opentrons import OpentronsThermocyclerBackend
from pyreagent.labware import DEFAULT_CATALOG
from pyreagent.vendors.opentrons.deck import parse_flex_deck
from pyreagent.vendors.opentrons.discovery import FoundRobot, choose, confirm
from pyreagent.vendors.opentrons.pipettes import flow_rates, tip_volumes

OT2_DECK = {
    "1": "opentrons_96_tiprack_300ul",
    "2": "opentrons_96_tiprack_20ul",
    "3": {"labware": "nest_12_reservoir_15ml", "initial_volume_ul": 10000},
    "4": {"labware": "corning_96_wellplate_360ul_flat", "initial_volume_ul": 0},
}
FLEX_DECK = {
    "B1": "opentrons_flex_96_tiprack_200ul",
    "B2": "opentrons_flex_96_tiprack_50ul",
    "C1": {"labware": "nest_12_reservoir_15ml", "initial_volume_ul": 10000},
    "D1": {"labware": "nest_96_wellplate_2ml_deep", "initial_volume_ul": 0},
}
SCHEMA = Path(__file__).resolve().parents[2] / "_reference/opentrons/shared-data/command/schemas/18.json"


async def start(robot: FakeOpentronsRobot, *, robot_type: str = "ot2", deck: dict | None = None, extra=(), **options) -> DeviceHub:
    backend = OpentronsBackend(robot=robot_type, transport=robot.transport, **options)
    backend.session.client.poll_interval_s = 0.01
    hub = DeviceHub([LiquidHandler("ot", backend, deck=deck if deck is not None else (OT2_DECK if robot_type == "ot2" else FLEX_DECK)), *extra])
    await hub.start()
    return hub


async def job(hub: DeviceHub, device: str, capability: str, args: dict) -> dict:
    try:
        accepted = await hub.invoke(device, capability, args)
    except PyreagentError as err:
        return {"status": "failed", "error": err.to_dict()}
    if accepted.status == "completed":
        return {"status": "succeeded", "result": accepted.result}
    return (await hub.jobs.wait(accepted.job.id, 10)).model_dump(mode="json")


def sent(robot: FakeOpentronsRobot, command_type: str) -> list[dict]:
    return [c["params"] for c in robot.sent if c["commandType"] == command_type]


def flex_robot(**kwargs) -> FakeOpentronsRobot:
    instruments = kwargs.pop("instruments", [flex_pipette("left", "p1000_multi_flex", 8, 5, 1000),
                                             flex_pipette("right", "p50_single_flex", 1, 5, 50)])
    return FakeOpentronsRobot(model="OT-3 Standard", instruments=instruments, **kwargs)


# -- OT-2 ------------------------------------------------------------------------------------------

async def test_partial_tips_become_nozzle_layouts():
    robot = FakeOpentronsRobot()
    hub = await start(robot)
    assert "nozzle_layouts" in hub.describe_device("ot")["limits"]["features"]
    await hub.invoke("ot", "pick_up_tip", {"pipette": "right", "nozzles": {"style": "single"}})
    await hub.invoke("ot", "drop_tip", {"pipette": "right"})
    await hub.invoke("ot", "pick_up_tip", {"pipette": "right", "nozzles": {"style": "partial_column", "nozzles": 3}})
    await hub.invoke("ot", "drop_tip", {"pipette": "right"})
    await hub.invoke("ot", "pick_up_tip", {"pipette": "right"})
    assert [p["configurationParams"] for p in sent(robot, "configureNozzleLayout")] == [
        {"style": "SINGLE", "primaryNozzle": "H1"},
        {"style": "QUADRANT", "primaryNozzle": "H1", "frontRightNozzle": "H1", "backLeftNozzle": "F1"},
        {"style": "ALL"},
    ]
    # single: A1. partial 3: D1 (its H1 nozzle at D1 takes B1-D1; A1 behind it is already gone). all: column 2.
    assert [p["wellName"] for p in sent(robot, "pickUpTip")] == ["A1", "D1", "A2"]
    await hub.stop()


async def test_old_ot2_software_has_no_partial_tips():
    hub = await start(FakeOpentronsRobot(software="7.5.0"))
    with pytest.raises(NotSupported):
        await hub.invoke("ot", "pick_up_tip", {"pipette": "right", "nozzles": {"style": "single"}})
    await hub.stop()


async def test_blow_out_touch_tip_return_and_lights():
    robot = FakeOpentronsRobot()
    hub = await start(robot)
    args = {"source_slot": "3", "source_wells": ["A1"], "destination_slot": "4", "destination_wells": ["A1"], "volume_ul": 100,
            "pipette": "left", "touch_tip": True, "blow_out": True}
    result = await job(hub, "ot", "transfer", args)
    assert result["status"] == "succeeded", result["error"]
    blow = sent(robot, "blowout")[0]
    assert blow["flowRate"] == 92.86 and blow["wellLocation"]["origin"] == "top"
    assert len(sent(robot, "touchTip")) == 2
    await hub.invoke("ot", "pick_up_tip", {"pipette": "left"})
    await hub.invoke("ot", "return_tip", {"pipette": "left"})
    returned = sent(robot, "dropTip")[-1]
    assert returned["wellName"] == "B1" and returned["wellLocation"]["origin"] == "default"
    assert robot.labware[returned["labwareId"]] == "1"
    await hub.invoke("ot", "set_lights", {"on": True})
    assert robot.lights is True
    assert sent(robot, "verifyTipPresence") == []  # OT-2 has no tip sensors
    await hub.stop()


async def test_ot2_has_no_gripper_but_records_manual_moves():
    robot = FakeOpentronsRobot()
    hub = await start(robot)
    with pytest.raises(NotSupported):
        await hub.invoke("ot", "move_labware", {"from_slot": "4", "to_slot": "5"})
    result = await job(hub, "ot", "move_labware", {"from_slot": "4", "to_slot": "5", "use_gripper": False})
    assert result["status"] == "succeeded", result["error"]
    assert sent(robot, "moveLabware")[-1]["strategy"] == "manualMoveWithoutPause"
    assert sent(robot, "moveLabware")[-1]["newLocation"] == {"slotName": "5"}
    await hub.stop()


# -- Flex ----------------------------------------------------------------------------------------

async def test_flex_checks_tips_with_its_sensors():
    robot = flex_robot()
    hub = await start(robot, robot_type="flex")
    await hub.invoke("ot", "pick_up_tip", {"pipette": "left"})
    await hub.invoke("ot", "drop_tip", {"pipette": "left"})
    assert [p["expectedState"] for p in sent(robot, "verifyTipPresence")] == ["present", "absent"]
    await hub.invoke("ot", "pick_up_tip", {"pipette": "left", "nozzles": {"style": "partial_column", "nozzles": 2}})
    assert len(sent(robot, "verifyTipPresence")) == 2  # fewer than 4 tips: Opentrons does not check
    await hub.stop()


async def test_flex_low_volume_mode():
    robot = flex_robot()
    hub = await start(robot, robot_type="flex")
    await hub.invoke("ot", "pick_up_tip", {"pipette": "right"})
    await hub.invoke("ot", "aspirate", {"slot": "C1", "well": "A1", "volume_ul": 2, "pipette": "right"})
    order = [c["commandType"] for c in robot.sent if c["commandType"] != "loadLabware"][-3:]
    assert order == ["configureForVolume", "prepareToAspirate", "aspirate"]
    assert sent(robot, "configureForVolume")[0]["volume"] == 2
    assert sent(robot, "aspirate")[-1]["flowRate"] == 26.7  # low-volume default for 50 µL tips
    await hub.invoke("ot", "aspirate", {"slot": "C1", "well": "A1", "volume_ul": 10, "pipette": "right"})
    assert len(sent(robot, "configureForVolume")) == 1  # liquid in the tip: no switch
    with pytest.raises(InvalidArgument, match="at most 30"):
        await hub.invoke("ot", "aspirate", {"slot": "C1", "well": "A1", "volume_ul": 25, "pipette": "right"})
    await hub.stop()


async def test_flex_liquid_probe_reports_heights():
    robot = flex_robot()
    robot.liquid_heights = {"A2": None, "A3": 7.25}
    hub = await start(robot, robot_type="flex")
    await hub.invoke("ot", "pick_up_tip", {"pipette": "right"})
    result = await job(hub, "ot", "probe_liquid_heights", {"slot": "C1", "wells": ["A1", "A2", "A3"], "pipette": "right"})
    assert result["status"] == "succeeded", result["error"]
    assert result["result"]["heights_mm"] == {"A1": 5.0, "A2": None, "A3": 7.25}
    assert [c["commandType"] for c in robot.sent][-3:] == ["moveToWell", "prepareToAspirate", "tryLiquidProbe"]
    await hub.stop()

    ot2 = await start(FakeOpentronsRobot())
    with pytest.raises(NotSupported):
        await ot2.invoke("ot", "probe_liquid_heights", {"slot": "3", "wells": ["A1"], "pipette": "left"})
    await ot2.stop()


async def test_flex_gripper_staging_slots_and_deck_configuration():
    fixtures = [{"cutoutId": f"cutout{r}{c}", "cutoutFixtureId": {1: "singleLeftSlot", 2: "singleCenterSlot", 3: "singleRightSlot"}[c],
                 "opentronsModuleSerialNumber": None} for r in "ACD" for c in (1, 2, 3) if (r, c) != ("A", 3)]
    fixtures += [{"cutoutId": "cutoutB1", "cutoutFixtureId": "singleLeftSlot"}, {"cutoutId": "cutoutB2", "cutoutFixtureId": "singleCenterSlot"},
                 {"cutoutId": "cutoutB3", "cutoutFixtureId": "stagingAreaRightSlot"}, {"cutoutId": "cutoutA3", "cutoutFixtureId": "trashBinAdapter"}]
    robot = flex_robot(instruments=[flex_pipette("left", "p1000_multi_flex", 8, 5, 1000), GRIPPER], deck_fixtures=fixtures)
    hub = await start(robot, robot_type="flex")
    limits = hub.describe_device("ot")["limits"]
    assert "B4" in limits["slots"] and "A3" not in limits["slots"] and "gripper" in limits["features"]
    result = await job(hub, "ot", "move_labware", {"from_slot": "D1", "to_slot": "B4"})
    assert result["status"] == "succeeded", result["error"]
    move = sent(robot, "moveLabware")[-1]
    assert move["strategy"] == "usingGripper" and move["newLocation"] == {"addressableAreaName": "B4"}
    await hub.invoke("ot", "load_labware", {"slot": "A4" if "A4" in limits["slots"] else "C2", "labware": "nest_96_wellplate_200ul_flat"})
    await hub.stop()


async def test_flex_waste_chute_when_no_trash_bin():
    fixtures = [{"cutoutId": "cutoutB1", "cutoutFixtureId": "singleLeftSlot"}, {"cutoutId": "cutoutC1", "cutoutFixtureId": "singleLeftSlot"},
                {"cutoutId": "cutoutD1", "cutoutFixtureId": "singleLeftSlot"}, {"cutoutId": "cutoutB2", "cutoutFixtureId": "singleCenterSlot"},
                {"cutoutId": "cutoutD3", "cutoutFixtureId": "wasteChuteRightAdapterNoCover"}]
    robot = flex_robot(deck_fixtures=fixtures)
    hub = await start(robot, robot_type="flex")
    await hub.invoke("ot", "pick_up_tip", {"pipette": "left"})
    await hub.invoke("ot", "drop_tip", {"pipette": "left"})
    assert sent(robot, "moveToAddressableAreaForDropTip")[-1]["addressableAreaName"] == "8ChannelWasteChute"
    await hub.stop()


# -- custom labware, modules, recovery ---------------------------------------------------------------

def test_definition_catalog_and_uploads(tmp_path: Path):
    definition = {
        "namespace": "custom_beta", "version": 1, "schemaVersion": 2,
        "parameters": {"loadName": "lab_24_tuberack_2ml", "isTiprack": False, "format": "irregular"},
        "metadata": {"displayName": "Lab 24 tube rack", "displayCategory": "tubeRack"},
        "ordering": [[f"{r}{c}" for r in "ABCD"] for c in range(1, 7)],
        "wells": {f"{r}{c}": {"depth": 40, "totalLiquidVolume": 2000} for r in "ABCD" for c in range(1, 7)},
    }
    path = tmp_path / "lab_24_tuberack_2ml.json"
    path.write_text(json.dumps(definition), encoding="utf-8")
    OpentronsBackend(robot="ot2", transport=FakeOpentronsRobot().transport, labware_definitions=[str(path)])
    labware = DEFAULT_CATALOG.get("lab_24_tuberack_2ml")
    assert (labware.kind, labware.rows, labware.columns, labware.well_volume_ul) == ("tube_rack", 4, 6, 2000)


async def test_custom_labware_is_uploaded_to_the_run(tmp_path: Path):
    definition = {"namespace": "custom_beta", "version": 1, "parameters": {"loadName": "lab_6_reservoir", "isTiprack": False},
                  "metadata": {"displayName": "Lab 6 reservoir", "displayCategory": "reservoir"},
                  "ordering": [["A1"], ["A2"], ["A3"], ["A4"], ["A5"], ["A6"]],
                  "wells": {f"A{c}": {"depth": 30, "totalLiquidVolume": 25000} for c in range(1, 7)}}
    path = tmp_path / "res.json"
    path.write_text(json.dumps(definition), encoding="utf-8")
    robot = FakeOpentronsRobot()
    deck = {**OT2_DECK, "5": {"labware": "lab_6_reservoir", "initial_volume_ul": 20000}}
    hub = await start(robot, deck=deck, labware_definitions=[str(path)])
    assert robot.definitions[0]["parameters"]["loadName"] == "lab_6_reservoir"
    args = {"source_slot": "5", "source_wells": ["A1"], "destination_slot": "4", "destination_wells": ["A1"], "volume_ul": 100, "pipette": "left"}
    assert (await job(hub, "ot", "transfer", args))["status"] == "succeeded"
    load = next(p for p in sent(robot, "loadLabware") if p["loadName"] == "lab_6_reservoir")
    assert (load["namespace"], load["version"]) == ("custom_beta", 1)
    await hub.stop()


async def test_pipettes_and_thermocycler_share_one_run():
    robot = FakeOpentronsRobot(modules=[thermocycler()])
    tc = Thermocycler("tc", OpentronsThermocyclerBackend(robot="ot2", transport=robot.transport, poll_s=0.01))
    deck = {**OT2_DECK, "7": {"labware": "opentrons_96_wellplate_200ul_pcr_full_skirt", "initial_volume_ul": 0}}
    hub = await start(robot, deck=deck, extra=[tc])
    assert len(robot.runs) == 1
    await hub.invoke("tc", "open_lid")
    args = {"source_slot": "3", "source_wells": ["A1"], "destination_slot": "7", "destination_wells": ["A1"], "volume_ul": 20, "pipette": "left"}
    assert (await job(hub, "ot", "transfer", args))["status"] == "succeeded"
    on_module = next(p for p in sent(robot, "loadLabware") if p["loadName"] == "opentrons_96_wellplate_200ul_pcr_full_skirt")
    assert on_module["location"] == {"moduleId": "module-thermocyclerModuleV2-7"}
    with pytest.raises(InvalidArgument, match="covered by the thermocycler"):
        await hub.invoke("ot", "load_labware", {"slot": "8", "labware": "nest_96_wellplate_200ul_flat"})
    await hub.stop()
    assert robot.current is None


async def test_stopping_the_robot_and_recovering_every_device():
    robot = FakeOpentronsRobot(modules=[thermocycler()])
    tc = Thermocycler("tc", OpentronsThermocyclerBackend(robot="ot2", transport=robot.transport, poll_s=0.01))
    hub = await start(robot, extra=[tc])
    first = robot.current
    await hub.invoke("ot", "stop")
    with pytest.raises(PyreagentError):
        await hub.invoke("tc", "set_block_temperature", {"temperature_c": 40})
    assert hub.device("tc").state is DeviceState.ERROR
    await hub.invoke("tc", "reset")
    assert robot.current != first and hub.device("tc").state is DeviceState.IDLE
    await hub.invoke("tc", "set_block_temperature", {"temperature_c": 40})
    # The liquid handler notices the new run and reloads its pipettes before its next command.
    assert (await hub.invoke("ot", "pick_up_tip", {"pipette": "left"})).status == "completed"
    assert robot.commands(robot.current).count("loadPipette") == 2
    await hub.stop()


# -- vendor facts -------------------------------------------------------------------------------------

def test_flow_rate_and_tip_tables():
    assert flow_rates("p300_single_gen2", 300).aspirate == 92.86
    assert flow_rates("p1000_single_flex", 50).aspirate == 478 and flow_rates("p1000_single_flex", 200).aspirate == 716
    assert flow_rates("p50_single_flex", 50, low_volume=True).aspirate == 26.7
    assert flow_rates("p10_single", 10) is None
    assert tip_volumes("p50_single_flex") == [20.0, 50.0] and tip_volumes("p300_single") == [200.0, 300.0]


def test_flex_deck_parsing():
    deck = parse_flex_deck([{"cutoutId": "cutoutA3", "cutoutFixtureId": "trashBinAdapter"},
                            {"cutoutId": "cutoutC3", "cutoutFixtureId": "stagingAreaRightSlot"},
                            {"cutoutId": "cutoutD1", "cutoutFixtureId": "temperatureModuleV2"},
                            {"cutoutId": "cutoutD3", "cutoutFixtureId": "stagingAreaSlotWithWasteChuteRightAdapterNoCover"}])
    assert deck.trash_bins == ["movableTrashA3"] and deck.waste_chute
    assert deck.slots == ["C3", "D1", "C4", "D4"]


async def test_discovery_confirms_and_chooses():
    robot = FakeOpentronsRobot(model="OT-3 Standard")
    found = await confirm([("flex-1", "10.0.0.7", 31950)], transport=robot.transport)
    assert found == [FoundRobot(name="fake-robot", host="10.0.0.7", port=31950, robot_model="OT-3 Standard")]
    assert choose(found, robot="flex").host == "10.0.0.7"
    with pytest.raises(PyreagentError, match="No ot2"):
        choose(found, robot="ot2")


async def test_every_command_matches_the_official_schema():
    jsonschema = pytest.importorskip("jsonschema")
    if not SCHEMA.exists():
        pytest.skip("Opentrons reference not cloned; see scripts/gen_opentrons_labware.py for the checkout command")
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
    robots = []

    ot2 = FakeOpentronsRobot(modules=[thermocycler()])
    robots.append(ot2)
    tc = Thermocycler("tc", OpentronsThermocyclerBackend(robot="ot2", transport=ot2.transport, poll_s=0.01, native_profiles=True))
    hub = await start(ot2, extra=[tc])
    await job(hub, "ot", "transfer", {"source_slot": "3", "source_wells": ["A1"], "destination_slot": "4", "destination_wells": ["A1"],
                                      "volume_ul": 50, "pipette": "left", "touch_tip": True, "blow_out": True, "mix_after_cycles": 1})
    await hub.invoke("ot", "pick_up_tip", {"pipette": "right", "nozzles": {"style": "partial_column", "nozzles": 2}})
    await hub.invoke("ot", "return_tip", {"pipette": "right"})
    await hub.invoke("ot", "pick_up_tip", {"pipette": "right", "nozzles": {"style": "single"}})
    await hub.invoke("ot", "move_to", {"slot": "4", "well": "A1", "pipette": "right"})
    await job(hub, "ot", "move_labware", {"from_slot": "4", "to_slot": "5", "use_gripper": False})
    await hub.invoke("ot", "set_lights", {"on": False})
    profile = {"stages": [{"repeat": 2, "steps": [{"temperature_c": 95, "hold_s": 0.01, "ramp_rate_c_per_s": 2},
                                                   {"temperature_c": 60, "hold_s": 0.01}]}]}
    await job(hub, "tc", "run_profile", {"profile": profile})
    await hub.invoke("tc", "set_block_temperature", {"temperature_c": 37})
    await hub.invoke("tc", "open_lid")
    await hub.invoke("tc", "deactivate")
    await hub.stop()

    flex = flex_robot(instruments=[flex_pipette("left", "p1000_multi_flex", 8, 5, 1000), flex_pipette("right", "p50_single_flex", 1, 5, 50), GRIPPER])
    robots.append(flex)
    hub = await start(flex, robot_type="flex")
    await hub.invoke("ot", "pick_up_tip", {"pipette": "right"})
    await hub.invoke("ot", "aspirate", {"slot": "C1", "well": "A1", "volume_ul": 2, "pipette": "right"})
    await hub.invoke("ot", "dispense", {"slot": "D1", "well": "A1", "pipette": "right"})
    await hub.invoke("ot", "drop_tip", {"pipette": "right"})
    await hub.invoke("ot", "pick_up_tip", {"pipette": "right"})
    await job(hub, "ot", "probe_liquid_heights", {"slot": "C1", "wells": ["A1"], "pipette": "right"})
    await job(hub, "ot", "move_labware", {"from_slot": "D1", "to_slot": "D2"})
    await job(hub, "ot", "home", {})
    await hub.stop()

    seen = set()
    for robot in robots:
        for command in robot.sent:
            body = {k: v for k, v in command.items() if not k.startswith("_")}
            errors = sorted(validator.iter_errors(body), key=lambda e: e.path)
            assert not errors, f"{body['commandType']} {body['params']}: {[e.message for e in errors][:2]}"
            seen.add(body["commandType"])
    assert {"blowout", "touchTip", "configureNozzleLayout", "dropTip", "moveToWell", "tryLiquidProbe", "verifyTipPresence",
            "configureForVolume", "prepareToAspirate", "setRailLights", "moveLabware", "loadModule", "thermocycler/runExtendedProfile",
            "thermocycler/setTargetBlockTemperature", "thermocycler/openLid"} <= seen
