"""Opentrons Thermocycler and Temperature Module drivers: through the robot, and directly over USB."""

from __future__ import annotations

import pytest

from fake_opentrons import FakeOpentronsRobot, temperature_module, thermocycler
from fakes import FakeTempDeckFirmware, FakeThermocyclerFirmware
from pyreagent.core import DeviceHub, DeviceState, NotSupported
from pyreagent.devices.temperature_controller import TemperatureController
from pyreagent.devices.temperature_controller.opentrons import (
    OpentronsTemperatureModuleBackend,
    OpentronsTemperatureModuleUSBBackend,
)
from pyreagent.devices.thermocycler import Thermocycler
from pyreagent.devices.thermocycler.opentrons import OpentronsThermocyclerBackend, OpentronsThermocyclerUSBBackend
from pyreagent.io import MockTransport

FAST_PCR = {"stages": [{"steps": [{"temperature_c": 95, "hold_s": 0.01}]},
                       {"repeat": 3, "steps": [{"temperature_c": 95, "hold_s": 0.01}, {"temperature_c": 60, "hold_s": 0.01}]}],
            "lid_temperature_c": 105, "final_hold_c": 4}


def tc_device(robot: FakeOpentronsRobot, robot_type: str = "ot2", **options) -> Thermocycler:
    backend = OpentronsThermocyclerBackend(robot=robot_type, transport=robot.transport, poll_s=0.01, **options)
    backend.session.client.poll_interval_s = 0.01
    return Thermocycler("tc-1", backend)


async def test_thermocycler_via_robot():
    robot = FakeOpentronsRobot(modules=[thermocycler()])
    hub = DeviceHub([tc_device(robot)])
    await hub.start()
    info = hub.describe_device("tc-1")
    assert info["state"] == "idle", info["last_error"]
    assert info["driver"]["slot"] == "7" and info["driver"]["module_model"] == "thermocyclerModuleV2"
    load = next(c for c in robot.sent if c["commandType"] == "loadModule")["params"]
    assert load == {"model": "thermocyclerModuleV2", "location": {"slotName": "7"}}

    assert (await hub.invoke("tc-1", "open_lid")).result["lid_open"] is True
    await hub.invoke("tc-1", "close_lid")
    status = (await hub.invoke("tc-1", "set_block_temperature", {"temperature_c": 37})).result
    assert status["block_target_c"] == 37 and status["lid_open"] is False

    job = await hub.jobs.wait((await hub.invoke("tc-1", "run_profile", {"profile": FAST_PCR})).job.id, 10)
    assert job.status == "succeeded", job.error
    temps = [c["params"]["celsius"] for c in robot.sent if c["commandType"] == "thermocycler/setTargetBlockTemperature"]
    assert temps[-7:] == [95, 95, 60, 95, 60, 95, 60] or temps[-8:-1] == [95, 95, 60, 95, 60, 95, 60]
    assert temps[-1] == 4  # final hold
    await hub.stop()
    assert robot.current is None


async def test_native_profile_and_ramp_rates():
    robot = FakeOpentronsRobot(modules=[thermocycler()])
    hub = DeviceHub([tc_device(robot, native_profiles=True, block_max_volume_ul=25)])
    await hub.start()
    profile = {**FAST_PCR, "stages": [*FAST_PCR["stages"][:1],
                                      {"repeat": 3, "steps": [{"temperature_c": 95, "hold_s": 0.01, "ramp_rate_c_per_s": 2.0},
                                                              {"temperature_c": 60, "hold_s": 0.01}]}]}
    job = await hub.jobs.wait((await hub.invoke("tc-1", "run_profile", {"profile": profile})).job.id, 10)
    assert job.status == "succeeded", job.error
    run = next(c["params"] for c in robot.sent if c["commandType"] == "thermocycler/runExtendedProfile")
    assert run["blockMaxVolumeUl"] == 25
    assert run["profileElements"] == [
        {"celsius": 95, "holdSeconds": 0.01},
        {"steps": [{"celsius": 95, "holdSeconds": 0.01, "rampRate": 2.0}, {"celsius": 60, "holdSeconds": 0.01}], "repetitions": 3},
    ]
    await hub.invoke("tc-1", "set_block_temperature", {"temperature_c": 50})
    await hub.stop()


async def test_temperature_module_via_flex():
    robot = FakeOpentronsRobot(model="OT-3 Standard", instruments=[], modules=[temperature_module()])
    backend = OpentronsTemperatureModuleBackend(robot="flex", slot="C1", transport=robot.transport)
    hub = DeviceHub([TemperatureController("temp-1", backend, min_c=4, max_c=95)])
    await hub.start()
    reading = (await hub.invoke("temp-1", "set_temperature", {"target_c": 37.4})).result
    assert reading == {"current_c": 37, "target_c": 37, "active": True}  # the robot works in whole degrees
    assert (await hub.invoke("temp-1", "deactivate")).result["active"] is False
    assert next(c["params"] for c in robot.sent if c["commandType"] == "loadModule")["location"] == {"slotName": "C1"}
    await hub.stop()


async def test_ambiguous_or_missing_module_stays_offline():
    hub = DeviceHub([tc_device(FakeOpentronsRobot(modules=[]))])
    await hub.start()
    assert hub.describe_device("tc-1")["last_error"]["code"] == "needs_attention"

    two = FakeOpentronsRobot(modules=[thermocycler("TC-A"), thermocycler("TC-B")])
    hub = DeviceHub([tc_device(two)])
    await hub.start()
    assert "serial" in hub.describe_device("tc-1")["last_error"]["message"]

    hub = DeviceHub([tc_device(FakeOpentronsRobot(modules=[thermocycler("TC-A"), thermocycler("TC-B")]), serial="TC-B")])
    await hub.start()
    assert hub.describe_device("tc-1")["state"] == "idle"
    await hub.stop()


def test_modules_must_not_overlap_or_use_missing_slots():
    robot = FakeOpentronsRobot()
    OpentronsThermocyclerBackend(robot="ot2", transport=robot.transport)  # covers 7, 8, 10, 11
    with pytest.raises(ValueError, match="overlaps the thermocycler"):
        OpentronsTemperatureModuleBackend(robot="ot2", slot="8", transport=robot.transport)
    with pytest.raises(ValueError, match="no deck slot"):
        OpentronsTemperatureModuleBackend(robot="ot2", slot="12", transport=robot.transport)
    OpentronsTemperatureModuleBackend(robot="ot2", slot="3", transport=robot.transport)


@pytest.mark.parametrize("generation", [1, 2])
async def test_thermocycler_over_usb(generation):
    firmware = FakeThermocyclerFirmware(generation=generation)
    backend = OpentronsThermocyclerUSBBackend(transport=MockTransport(firmware), poll_s=0.01, block_max_volume_ul=20)
    tc = Thermocycler("tc-usb", backend)
    await tc.connect()
    assert backend.hardware_generation == generation
    assert tc.describe()["driver"]["serial"] == ("TC2-USB-01" if generation == 2 else "TC1-USB-01")
    await tc.open_lid()
    assert (await tc.status()).lid_open is True
    status = await tc.set_block_temperature(temperature_c=50)
    assert status.block_target_c == 50 and status.block_c == 50
    assert "M104 S50.0 H0 V20" in firmware.commands  # same formatting as Opentrons' own driver
    if generation == 2:
        await tc.lift_plate()
        assert firmware.plate_lifted
    else:
        with pytest.raises(NotSupported):
            await tc.lift_plate()
    await tc.close_lid()
    result = await tc.run_profile(profile=FAST_PCR)
    assert result.steps_run == 7 and (await tc.status()).block_target_c == 4
    await tc.deactivate()
    assert firmware.block_target is None and firmware.lid_target is None
    assert tc.state is DeviceState.IDLE


async def test_ramp_rates_need_gen2_over_usb():
    tc = Thermocycler("tc-usb", OpentronsThermocyclerUSBBackend(transport=MockTransport(FakeThermocyclerFirmware(generation=1))))
    await tc.connect()
    profile = {**FAST_PCR, "stages": [{"steps": [{"temperature_c": 95, "hold_s": 0.01, "ramp_rate_c_per_s": 1.5}]}]}
    with pytest.raises(NotSupported, match="ramp"):
        await tc.run_profile(profile=profile)


async def test_temperature_module_over_usb():
    firmware = FakeTempDeckFirmware()
    backend = OpentronsTemperatureModuleUSBBackend(transport=MockTransport(firmware))
    module = TemperatureController("temp-usb", backend, min_c=4, max_c=95)
    await module.connect()
    assert backend.hardware_generation == 2 and backend.device_info["serial"] == "TDV21-USB-01"
    reading = await module.set_temperature(target_c=4.4)
    assert firmware.commands[-2] == "M104 S4" and reading.target_c == 4
    assert (await module.deactivate()).active is False
