"""Opentrons Heater-Shaker, Flex Absorbance Plate Reader, Magnetic Module and Magnetic Block, through the robot."""

from __future__ import annotations

import pytest

from fake_opentrons import (
    GRIPPER, FakeOpentronsRobot, absorbance, absorbance_reader, flex_pipette, heater_shaker, magnetic_module,
)
from pyreagent.core import DeviceHub, InvalidArgument, NeedsAttention, NotSupported, PyreagentError
from pyreagent.devices.liquid_handler import LiquidHandler
from pyreagent.devices.liquid_handler.opentrons import OpentronsBackend
from pyreagent.devices.magnetic_separator import MagneticSeparator
from pyreagent.devices.magnetic_separator.opentrons import OpentronsMagneticModuleBackend
from pyreagent.devices.plate_reader import PlateReader
from pyreagent.devices.plate_reader.opentrons import OpentronsAbsorbanceReaderBackend
from pyreagent.devices.shaker import Shaker
from pyreagent.devices.shaker.opentrons import OpentronsHeaterShakerBackend

FLEX_PIPETTES = [flex_pipette("left", "p1000_single_flex", 1, 5, 1000)]


def flex(*modules: dict, gripper: bool = False) -> FakeOpentronsRobot:
    return FakeOpentronsRobot(model="OT-3 Standard", instruments=FLEX_PIPETTES + ([GRIPPER] if gripper else []), modules=list(modules))


async def started(*devices) -> DeviceHub:
    hub = DeviceHub(list(devices))
    await hub.start()
    for device in devices:
        info = hub.describe_device(device.name)
        assert info["state"] == "idle", info["last_error"]
    return hub


async def run(hub: DeviceHub, device: str, capability: str, args: dict | None = None) -> dict:
    accepted = await hub.invoke(device, capability, args or {})
    if accepted.job is None:
        return accepted.result
    job = await hub.jobs.wait(accepted.job.id, 5)
    assert job.status == "succeeded", job.error
    return job.result


def sent(robot: FakeOpentronsRobot, prefix: str) -> list[tuple[str, dict]]:
    return [(c["commandType"], {k: v for k, v in c["params"].items() if k != "moduleId"}) for c in robot.sent if c["commandType"].startswith(prefix)]


# -- Heater-Shaker -------------------------------------------------------------------------------------

def heater_shaker_device(robot: FakeOpentronsRobot, slot: str = "D1") -> Shaker:
    return Shaker("hs", OpentronsHeaterShakerBackend(robot="flex", slot=slot, transport=robot.transport), poll_interval_s=0.01)


async def test_heater_shaker_shakes_heats_and_latches():
    robot = flex(heater_shaker())
    hub = await started(heater_shaker_device(robot))
    limits = hub.describe_device("hs")["limits"]
    assert limits == {"min_rpm": 200, "max_rpm": 3000, "features": ["heating", "latch"], "min_temperature_c": 37, "max_temperature_c": 95}

    # The latch is closed on the hardware but unknown to a new run: the driver tells the run before shaking.
    await run(hub, "hs", "shake", {"rpm": 1000, "duration_s": 0.05})
    assert sent(robot, "heaterShaker/") == [("heaterShaker/closeLabwareLatch", {}), ("heaterShaker/setAndWaitForShakeSpeed", {"rpm": 1000}),
                                            ("heaterShaker/deactivateShaker", {})]

    status = await run(hub, "hs", "set_temperature", {"celsius": 37})
    assert status["target_temperature_c"] == 37 and status["latch"] == "closed"
    assert (await run(hub, "hs", "wait_for_temperature"))["temperature_c"] == 37
    assert (await run(hub, "hs", "deactivate_heater"))["target_temperature_c"] is None
    await hub.stop()


async def test_heater_shaker_refuses_before_anything_moves():
    robot = flex(heater_shaker())
    hub = await started(heater_shaker_device(robot))
    with pytest.raises(InvalidArgument, match="allowed range"):
        await hub.invoke("hs", "start_shaking", {"rpm": 100})
    with pytest.raises(InvalidArgument, match="allowed range"):
        await hub.invoke("hs", "set_temperature", {"celsius": 25})  # heats only
    await run(hub, "hs", "open_latch")
    with pytest.raises(NeedsAttention, match="latch is not closed"):
        await hub.invoke("hs", "start_shaking", {"rpm": 500})
    await run(hub, "hs", "close_latch")
    await run(hub, "hs", "start_shaking", {"rpm": 500})
    with pytest.raises(InvalidArgument, match="stop_shaking first"):
        await hub.invoke("hs", "open_latch")
    assert (await run(hub, "hs", "stop_shaking"))["shaking"] is False
    await hub.stop()


# -- Absorbance Plate Reader ------------------------------------------------------------------------------

def reader_device(robot: FakeOpentronsRobot, slot: str = "C3") -> PlateReader:
    return PlateReader("abs", OpentronsAbsorbanceReaderBackend(slot=slot, transport=robot.transport))


async def test_absorbance_reader_initialises_once_and_reads():
    module = absorbance_reader()
    robot = flex(module)
    hub = await started(reader_device(robot))
    assert sent(robot, "absorbanceReader/") == [("absorbanceReader/initialize", {"measureMode": "multi", "sampleWavelengths": [450, 562, 600, 650]})]
    described = hub.describe_device("abs")
    assert described["limits"]["absorbance_wavelengths_nm"] == [450, 562, 600, 650]
    assert "read_fluorescence" not in {c["name"] for c in described["capabilities"]}

    with pytest.raises(NeedsAttention, match="no plate"):
        await run(hub, "abs", "read_absorbance", {"plate": "corning_96_wellplate_360ul_flat", "wavelength_nm": 450})
    module["data"]["platePresence"] = "present"
    result = await run(hub, "abs", "read_absorbance", {"plate": "corning_96_wellplate_360ul_flat", "wavelength_nm": 600, "wells": ["A1", "H12"]})
    assert result["values"] == {"A1": absorbance(0, 600), "H12": absorbance(95, 600)}
    assert [c for c, _ in sent(robot, "absorbanceReader/")].count("absorbanceReader/initialize") == 1
    with pytest.raises(InvalidArgument, match="filters for"):
        await hub.invoke("abs", "read_absorbance", {"plate": "corning_96_wellplate_360ul_flat", "wavelength_nm": 500})
    await hub.stop()


async def test_absorbance_reader_lid_without_a_gripper():
    module = absorbance_reader()
    module["data"]["lidStatus"] = "off"  # lid in its dock at connect: initialise later
    robot = flex(module)
    hub = await started(reader_device(robot))
    assert sent(robot, "absorbanceReader/") == []
    assert (await run(hub, "abs", "status"))["tray_open"] is True
    with pytest.raises(NeedsAttention, match="tray is open"):
        await run(hub, "abs", "read_absorbance", {"plate": "corning_96_wellplate_360ul_flat", "wavelength_nm": 450})
    with pytest.raises(NeedsAttention, match="no gripper to move the reader lid onto the reader"):
        await run(hub, "abs", "close_tray")
    module["data"]["lidStatus"] = "on"  # a human put the lid on; the robot is then told
    assert (await run(hub, "abs", "close_tray"))["tray_open"] is False
    module["data"]["platePresence"] = "present"
    await run(hub, "abs", "read_absorbance", {"plate": "corning_96_wellplate_360ul_flat", "wavelength_nm": 450})
    assert [c for c, _ in sent(robot, "absorbanceReader/")] == ["absorbanceReader/closeLid", "absorbanceReader/closeLid",
                                                                "absorbanceReader/initialize", "absorbanceReader/read"]
    await hub.stop()


async def test_absorbance_reader_with_a_gripper_moves_its_lid():
    robot = flex(absorbance_reader(), gripper=True)
    hub = await started(reader_device(robot))
    assert (await run(hub, "abs", "open_tray"))["tray_open"] is True
    assert (await run(hub, "abs", "close_tray"))["tray_open"] is False
    await hub.stop()


def test_absorbance_reader_fits_column_3_of_a_flex_only():
    robot = flex()
    with pytest.raises(ValueError, match="must be in slot A3, B3, C3, D3"):
        OpentronsAbsorbanceReaderBackend(slot="D1", transport=robot.transport)
    with pytest.raises(ValueError, match="does not fit the ot2"):
        OpentronsAbsorbanceReaderBackend(slot="3", robot="ot2", transport=FakeOpentronsRobot().transport)


# -- Magnetic Module and Magnetic Block -----------------------------------------------------------------------

@pytest.mark.parametrize(("model", "top"), [("magneticModuleV1", 20.0), ("magneticModuleV2", 22.5)])
async def test_magnetic_module(model: str, top: float):
    robot = FakeOpentronsRobot(modules=[magnetic_module(model=model)])
    hub = await started(MagneticSeparator("mag", OpentronsMagneticModuleBackend(slot="1", transport=robot.transport)))
    assert hub.describe_device("mag")["limits"] == {"min_height_mm": -2.5, "max_height_mm": top}
    assert await run(hub, "mag", "engage", {"height_mm": 10}) == {"engaged": True, "height_mm": 10.0}
    with pytest.raises(InvalidArgument, match="allowed range"):
        await hub.invoke("mag", "engage", {"height_mm": top + 1})
    assert await run(hub, "mag", "disengage") == {"engaged": False, "height_mm": None}
    assert sent(robot, "magneticModule/") == [("magneticModule/engage", {"height": 10.0}), ("magneticModule/disengage", {})]
    await hub.stop()


async def test_magnetic_block_holds_labware_for_the_pipettes():
    robot = flex()
    backend = OpentronsBackend(robot="flex", transport=robot.transport, magnetic_blocks=["C1"])
    deck = {"B2": "opentrons_flex_96_tiprack_200ul", "C1": {"labware": "nest_96_wellplate_2ml_deep", "initial_volume_ul": 500}}
    hub = await started(LiquidHandler("ot", backend, deck=deck))
    await run(hub, "ot", "move_to", {"slot": "C1", "well": "A1", "pipette": "left"})
    load_module = next(c["params"] for c in robot.sent if c["commandType"] == "loadModule")
    assert load_module == {"model": "magneticBlockV1", "location": {"slotName": "C1"}}
    plate = next(c["params"] for c in robot.sent if c["commandType"] == "loadLabware" and c["params"]["loadName"] == "nest_96_wellplate_2ml_deep")
    assert plate["location"] == {"moduleId": "module-magneticBlockV1-C1"}
    await hub.stop()


async def test_modules_share_one_run_with_the_pipettes():
    robot = flex(heater_shaker(), absorbance_reader())
    lh = LiquidHandler("ot", OpentronsBackend(robot="flex", transport=robot.transport), deck={"B2": "opentrons_flex_96_tiprack_200ul"})
    hub = await started(lh, heater_shaker_device(robot), reader_device(robot))
    assert len(robot.runs) == 1 and {c["params"]["model"] for c in robot.sent if c["commandType"] == "loadModule"} == {
        "heaterShakerModuleV1", "absorbanceReaderV1"}
    await hub.stop()
    assert robot.current is None


async def test_module_errors_are_not_hardware_faults():
    robot = flex(heater_shaker())
    hub = await started(heater_shaker_device(robot))
    backend = hub.device("hs").backend
    with pytest.raises(PyreagentError) as err:  # bypass the frontend: the robot refuses the speed itself
        await backend.module.command("heaterShaker/setAndWaitForShakeSpeed", rpm=5000)
    assert err.value.code in ("needs_attention", "invalid_argument")
    assert hub.describe_device("hs")["state"] == "idle"
    await hub.stop()


async def test_a_plain_shaker_has_no_heater_or_latch():
    from pyreagent.devices.shaker.simulated import SimulatedShakerBackend

    hub = await started(Shaker("plain", SimulatedShakerBackend(), min_rpm=100, max_rpm=1000))
    assert {c["name"] for c in hub.describe_device("plain")["capabilities"]} == {"shake", "start_shaking", "stop_shaking", "status", "reset"}
    with pytest.raises(NotSupported):
        await hub.invoke("plain", "set_temperature", {"celsius": 40})
    await hub.stop()


async def test_module_commands_match_the_official_schema():
    import json
    from pathlib import Path

    jsonschema = pytest.importorskip("jsonschema")
    schema = Path(__file__).resolve().parents[2] / "_reference/opentrons/shared-data/command/schemas/18.json"
    if not schema.exists():
        pytest.skip("Opentrons reference not cloned; see scripts/gen_opentrons_labware.py for the checkout command")
    validator = jsonschema.Draft202012Validator(json.loads(schema.read_text(encoding="utf-8")))

    reader_module = absorbance_reader()
    reader_module["data"]["platePresence"] = "present"
    robot = flex(heater_shaker(), reader_module, gripper=True)
    lh = LiquidHandler("ot", OpentronsBackend(robot="flex", transport=robot.transport, magnetic_blocks=["C1"]),
                       deck={"C1": {"labware": "nest_96_wellplate_2ml_deep", "initial_volume_ul": 0}})
    hub = await started(lh, heater_shaker_device(robot), reader_device(robot))
    await run(hub, "hs", "shake", {"rpm": 500, "duration_s": 0.01})
    await run(hub, "hs", "set_temperature", {"celsius": 40})
    await run(hub, "hs", "deactivate_heater")
    await run(hub, "hs", "open_latch")
    await run(hub, "abs", "open_tray")
    await run(hub, "abs", "close_tray")
    await run(hub, "abs", "read_absorbance", {"plate": "corning_96_wellplate_360ul_flat", "wavelength_nm": 450})
    await run(hub, "ot", "move_to", {"slot": "C1", "well": "A1", "pipette": "left"})
    await hub.stop()

    ot2 = FakeOpentronsRobot(modules=[magnetic_module()])
    hub = await started(MagneticSeparator("mag", OpentronsMagneticModuleBackend(slot="1", transport=ot2.transport)))
    await run(hub, "mag", "engage", {"height_mm": 5})
    await run(hub, "mag", "disengage")
    await hub.stop()

    seen = set()
    for command in robot.sent + ot2.sent:
        body = {k: v for k, v in command.items() if not k.startswith("_")}
        errors = sorted(validator.iter_errors(body), key=lambda e: e.path)
        assert not errors, f"{body['commandType']} {body['params']}: {[e.message for e in errors][:2]}"
        seen.add(body["commandType"])
    assert {"heaterShaker/setAndWaitForShakeSpeed", "heaterShaker/deactivateShaker", "heaterShaker/setTargetTemperature",
            "heaterShaker/deactivateHeater", "heaterShaker/openLabwareLatch", "heaterShaker/closeLabwareLatch",
            "absorbanceReader/initialize", "absorbanceReader/read", "absorbanceReader/openLid", "absorbanceReader/closeLid",
            "magneticModule/engage", "magneticModule/disengage", "loadModule"} <= seen
