"""Byonoy Absorbance 96 / Luminescence 96 driver against a fake reader, with byte vectors from the protocol spec."""

from __future__ import annotations

import asyncio

import pytest

from fake_byonoy import ABS_OD, LUM_RLU, FakeByonoy
from pyreagent.core import DeviceHub, HardwareError, InvalidArgument, NeedsAttention, NotSupported, PyreagentError
from pyreagent.devices.plate_reader import PlateReader
from pyreagent.devices.plate_reader.byonoy import ByonoyBackend
from pyreagent.devices.plate_reader.byonoy.protocol import LEGACY, NO_REPLY, Status, frame, read_field
from pyreagent.io import MockUsbTransport


def hexframe(head: str, routing: bytes) -> bytes:
    return bytes.fromhex(head).ljust(62, b"\x00") + routing


async def start(fake: FakeByonoy, **options) -> DeviceHub:
    backend = ByonoyBackend(model=fake.model, transport=MockUsbTransport(fake), **options)  # type: ignore[arg-type]
    hub = DeviceHub([PlateReader("byonoy", backend)])
    await hub.start()
    return hub


async def outcome(hub: DeviceHub, capability: str, args: dict) -> dict | None:
    """The call's error (or None), whether it finished inline or as a job."""
    try:
        accepted = await hub.invoke("byonoy", capability, args)
    except PyreagentError as err:
        return err.to_dict()
    return accepted.job and (await hub.jobs.wait(accepted.job.id, 5)).error


async def run(hub: DeviceHub, capability: str, args: dict) -> dict:
    accepted = await hub.invoke("byonoy", capability, args)
    if accepted.job is None:
        return accepted.result
    job = await hub.jobs.wait(accepted.job.id, 5)
    assert job.status == "succeeded", job.error
    return job.result


def test_framing_matches_the_spec_vectors():
    assert frame(0x0300) == hexframe("0003", b"\x80\x40")
    assert frame(0x0320, bytes.fromhex("c201000000000000"), NO_REPLY) == hexframe("2003c201000000000000", NO_REPLY)  # 450 nm read
    assert frame(0x0060, bytes.fromhex("4003"), LEGACY) == hexframe("60004003", LEGACY)  # abort a luminescence read
    status = Status.decode(hexframe("0003000101e80000000001",b"\x00\x00"))
    assert (status.initialized, status.slot, status.error_code, status.uptime_s, status.booted) == (False, "occupied", 1, 232, True)
    assert read_field(hexframe("00020e80c101000000", b"\x00\x00")) == 1


async def test_absorbance_connect_runs_the_reference_read_and_learns_the_filters():
    fake = FakeByonoy()
    hub = await start(fake)
    assert fake.triggers[0] == hexframe("2003940200000100", NO_REPLY)  # 660 nm, is_reference = 1
    limits = hub.describe_device("byonoy")["limits"]
    assert limits["absorbance_wavelengths_nm"] == [450, 600] and limits["features"] == ["absorbance"]
    assert limits["plate_formats"] == ["8x12"]
    names = {c["name"] for c in hub.describe_device("byonoy")["capabilities"]}
    assert "read_luminescence" not in names and "open_tray" not in names
    assert hub.describe_device("byonoy")["driver"]["serial_number"] == "BYO-1234"
    await hub.stop()


async def test_absorbance_read_reassembles_rows_and_keeps_requested_wells():
    fake = FakeByonoy()
    hub = await start(fake)
    before = len(fake.sent)
    result = await run(hub, "read_absorbance", {"plate": "corning_96_wellplate_360ul_flat", "wavelength_nm": 450, "wells": ["A1", "A12", "C6", "H12"]})
    assert result["values"] == {"A1": ABS_OD[0], "A12": ABS_OD[11], "C6": ABS_OD[29], "H12": ABS_OD[95]}
    sent = fake.sent[before:]
    assert sent[0] == hexframe("1000", LEGACY) and sent[1] == hexframe("00020700", LEGACY)  # the vendor prelude
    assert sent[2] == hexframe("2003c201000000000000", NO_REPLY)
    with pytest.raises(InvalidArgument, match="filters for"):
        await hub.invoke("byonoy", "read_absorbance", {"plate": "corning_96_wellplate_360ul_flat", "wavelength_nm": 500})
    with pytest.raises(InvalidArgument, match="plate format"):
        await hub.invoke("byonoy", "read_absorbance", {"plate": "corning_384_wellplate_112ul_flat", "wavelength_nm": 450})
    await hub.stop()


async def test_luminescence_reads_all_wells_and_keeps_the_first_plane():
    fake = FakeByonoy("luminescence_96", planes=8)
    hub = await start(fake)
    assert fake.triggers == []  # no reference read on a luminescence reader
    result = await run(hub, "read_luminescence", {"plate": "corning_96_wellplate_360ul_flat", "integration_s": 2, "wells": ["A1", "B1"]})
    assert result["values"] == {"A1": LUM_RLU[0], "B1": LUM_RLU[12]}  # -5.0: dark wells may be negative
    assert fake.triggers[-1] == hexframe("4003" "80841e00" + "ff" * 12, LEGACY)  # 2 s, every well
    with pytest.raises(InvalidArgument, match="allowed range"):
        await hub.invoke("byonoy", "read_luminescence", {"plate": "corning_96_wellplate_360ul_flat", "integration_s": 30})
    with pytest.raises(NotSupported):
        await hub.invoke("byonoy", "read_absorbance", {"plate": "corning_96_wellplate_360ul_flat", "wavelength_nm": 450})
    await hub.stop()


async def test_reader_errors_after_a_read():
    fake = FakeByonoy()
    hub = await start(fake)
    fake.error_after_read = 2  # too much ambient light: a human can fix it
    error = await outcome(hub, "read_absorbance", {"plate": "corning_96_wellplate_360ul_flat", "wavelength_nm": 600})
    assert error["code"] == NeedsAttention.code and "ambient light" in error["message"]
    await hub.stop()

    lum = FakeByonoy("luminescence_96")
    hub = await start(lum)
    lum.error_after_read = 0x17
    assert (await outcome(hub, "read_luminescence", {"plate": "corning_96_wellplate_360ul_flat"}))["code"] == HardwareError.code
    await hub.stop()


async def test_cancelling_a_read_aborts_the_measurement():
    fake = FakeByonoy("luminescence_96")
    hub = await start(fake)
    fake.hang = True
    accepted = await hub.invoke("byonoy", "read_luminescence", {"plate": "corning_96_wellplate_360ul_flat"})
    await asyncio.sleep(0.05)
    await hub.jobs.cancel(accepted.job.id)
    assert (await hub.jobs.wait(accepted.job.id, 5)).status == "cancelled"
    assert fake.sent[-1] == hexframe("60004003", LEGACY)
    await hub.stop()


async def test_status_reports_the_chamber_temperature():
    hub = await start(FakeByonoy())
    assert await run(hub, "status", {}) == {"tray_open": None, "temperature_c": 23.5, "target_temperature_c": None}
    await hub.stop()
