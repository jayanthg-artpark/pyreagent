"""The plate reader interface: optional measurements, limits and checks, against the simulated reader."""

from __future__ import annotations

import asyncio

import pytest

from pyreagent.core import DeviceHub, InvalidArgument, NeedsAttention, NotSupported
from pyreagent.devices.plate_reader import PlateReader
from pyreagent.devices.plate_reader.simulated import SimulatedPlateReaderBackend

ABSORBANCE_ONLY = {"absorbance": {"min_nm": 400, "max_nm": 750, "fixed_nm": [450, 600]}, "tray": False,
                   "plate_formats": [(8, 12)]}


async def reader(spec: dict | None = None, **options) -> DeviceHub:
    backend = SimulatedPlateReaderBackend(seconds_per_well=0, tray_s=0, spec=spec, **options)
    hub = DeviceHub([PlateReader("reader", backend, poll_interval_s=0.01)])
    await hub.start()
    return hub


async def run(hub: DeviceHub, capability: str, args: dict | None = None) -> dict:
    accepted = await hub.invoke("reader", capability, args or {})
    if accepted.job is None:
        return accepted.result
    job = await hub.jobs.wait(accepted.job.id, 5)
    assert job.status == "succeeded", job.error
    return job.result


async def test_multimode_reads():
    hub = await reader()
    fluo = await run(hub, "read_fluorescence", {"plate": "generic_96_wellplate", "excitation_nm": 485, "emission_nm": 528,
                                                "wells": ["A1", "B2"], "gain": 80})
    assert set(fluo["values"]) == {"A1", "B2"} and fluo["gain"] == 80 and fluo["optics"] == "top"
    lum = await run(hub, "read_luminescence", {"plate": "generic_96_wellplate", "wells": ["H12"], "integration_s": 0.5})
    assert lum["integration_s"] == 0.5 and lum["values"]["H12"] > 0
    spectrum = await run(hub, "read_absorbance_spectrum", {"plate": "generic_96_wellplate", "start_nm": 400, "end_nm": 500,
                                                            "step_nm": 20, "wells": ["A1"]})
    assert spectrum["wavelengths_nm"] == [400, 420, 440, 460, 480, 500] and len(spectrum["values"]["A1"]) == 6
    await hub.stop()


async def test_limits_list_what_the_reader_can_do():
    hub = await reader()
    limits = hub.describe_device("reader")["limits"]
    assert limits["features"] == ["absorbance", "absorbance_spectrum", "fluorescence", "luminescence", "temperature", "shaking", "tray"]
    assert limits["min_excitation_nm"] == 250 and limits["max_gain"] == 255 and limits["max_temperature_c"] == 45
    await hub.stop()


async def test_capabilities_an_instrument_lacks_are_hidden_and_refused():
    hub = await reader(ABSORBANCE_ONLY)
    described = hub.describe_device("reader")
    assert {c["name"] for c in described["capabilities"]} == {"read_absorbance", "status", "reset"}
    assert described["limits"]["features"] == ["absorbance"]
    with pytest.raises(NotSupported, match="cannot read_fluorescence"):
        await hub.invoke("reader", "read_fluorescence", {"plate": "generic_96_wellplate", "excitation_nm": 485, "emission_nm": 528})
    with pytest.raises(NotSupported):
        await hub.invoke("reader", "open_tray")
    status = await run(hub, "status")
    assert status == {"tray_open": None, "temperature_c": None, "target_temperature_c": None}
    await hub.stop()


async def test_reader_specific_checks_before_reading():
    hub = await reader(ABSORBANCE_ONLY)
    with pytest.raises(InvalidArgument, match="filters for"):
        await hub.invoke("reader", "read_absorbance", {"plate": "generic_96_wellplate", "wavelength_nm": 500})
    with pytest.raises(InvalidArgument, match="allowed range"):
        await hub.invoke("reader", "read_absorbance", {"plate": "generic_96_wellplate", "wavelength_nm": 800})
    with pytest.raises(InvalidArgument, match="not a plate format"):
        await hub.invoke("reader", "read_absorbance", {"plate": "generic_384_wellplate", "wavelength_nm": 450})
    assert "generic_384_wellplate" not in hub.describe_device("reader")["limits"]["plates"]
    assert (await run(hub, "read_absorbance", {"plate": "generic_96_wellplate", "wavelength_nm": 450}))["values"]["H12"] > 0
    await hub.stop()


async def test_fluorescence_and_shake_checks():
    hub = await reader()
    base = {"plate": "generic_96_wellplate", "excitation_nm": 485, "emission_nm": 528}
    with pytest.raises(InvalidArgument, match="at least 15 nm above"):
        await hub.invoke("reader", "read_fluorescence", {**base, "emission_nm": 490})
    with pytest.raises(InvalidArgument, match="allowed range"):
        await hub.invoke("reader", "read_fluorescence", {**base, "gain": 300})
    with pytest.raises(InvalidArgument, match="focal_height_mm"):
        await hub.invoke("reader", "read_fluorescence", {**base, "focal_height_mm": 28})
    with pytest.raises(InvalidArgument, match="scans at most"):
        await hub.invoke("reader", "read_absorbance_spectrum", {"plate": "generic_96_wellplate", "start_nm": 230, "end_nm": 1000, "step_nm": 1})
    with pytest.raises(InvalidArgument, match="allowed range"):
        await hub.invoke("reader", "shake", {"duration_s": 5, "rpm": 2000})
    await hub.stop()

    auto_gain = await reader({"fluorescence": {"min_excitation_nm": 400, "max_excitation_nm": 600, "min_emission_nm": 450,
                                               "max_emission_nm": 700}, "shaking": {"modes": ["linear"]}})
    with pytest.raises(InvalidArgument, match="sets its gain automatically"):
        await auto_gain.invoke("reader", "read_fluorescence", {**base, "gain": 50})
    with pytest.raises(InvalidArgument, match="from the top only"):
        await auto_gain.invoke("reader", "read_fluorescence", {**base, "optics": "bottom"})
    with pytest.raises(InvalidArgument, match="shakes linear only"):
        await auto_gain.invoke("reader", "shake", {"duration_s": 5})
    with pytest.raises(InvalidArgument, match="not settable"):
        await auto_gain.invoke("reader", "shake", {"duration_s": 5, "mode": "linear", "rpm": 300})
    await auto_gain.stop()


async def test_reads_need_a_closed_tray():
    hub = await reader()
    await run(hub, "open_tray")
    with pytest.raises(NeedsAttention, match="tray is open"):
        await run(hub, "read_luminescence", {"plate": "generic_96_wellplate"})
    assert (await run(hub, "close_tray"))["tray_open"] is False
    await hub.stop()


async def test_incubation():
    hub = await reader(heat_c_per_s=1000)
    with pytest.raises(InvalidArgument, match="allowed range"):
        await hub.invoke("reader", "set_temperature", {"celsius": 60})
    with pytest.raises(InvalidArgument, match="call set_temperature first"):
        await run(hub, "wait_for_temperature")
    assert (await run(hub, "set_temperature", {"celsius": 37}))["target_temperature_c"] == 37
    reached = await run(hub, "wait_for_temperature")
    assert reached["temperature_c"] == pytest.approx(37, abs=0.5)
    assert (await run(hub, "deactivate_temperature"))["target_temperature_c"] is None
    await hub.stop()


async def test_cancelling_a_shake_stops_it():
    hub = await reader()
    backend = hub.device("reader").backend
    stopped = []
    backend.stop_shaking = lambda: stopped.append(True) or asyncio.sleep(0)
    accepted = await hub.invoke("reader", "shake", {"duration_s": 60, "rpm": 500})
    await asyncio.sleep(0.05)
    await hub.jobs.cancel(accepted.job.id)
    assert (await hub.jobs.wait(accepted.job.id, 5)).status == "cancelled"
    assert stopped == [True]
    await hub.stop()
