from __future__ import annotations

import math

import pytest

from pyreagent.core import DeviceHub, InvalidArgument, NeedsAttention
from pyreagent.devices.qpcr import estimate_ct

PCR = {
    "stages": [
        {"steps": [{"temperature_c": 95, "hold_s": 180}]},
        {"repeat": 30, "steps": [{"temperature_c": 95, "hold_s": 15}, {"temperature_c": 60, "hold_s": 30}]},
        {"steps": [{"temperature_c": 72, "hold_s": 300}]},
    ],
    "lid_temperature_c": 105,
    "final_hold_c": 4,
}

QPCR = {
    "stages": [
        {"steps": [{"temperature_c": 95, "hold_s": 120}]},
        {"repeat": 40, "steps": [{"temperature_c": 95, "hold_s": 15}, {"temperature_c": 60, "hold_s": 30, "acquire": True}]},
    ],
}


async def test_pcr_program_runs_and_holds(hub: DeviceHub):
    accepted = await hub.invoke("pcr-1", "run_profile", {"profile": PCR})
    job = await hub.jobs.wait(accepted.job.id, 10)
    assert job.status == "succeeded", job.error
    assert job.result["total_cycles"] == 32 and job.result["steps_run"] == 62
    assert job.progress == 1.0
    status = (await hub.invoke("pcr-1", "status")).result
    assert status["block_target_c"] == 4 and status["lid_target_c"] == 105


async def test_profile_limits_are_checked_before_running(hub: DeviceHub):
    too_hot = {**PCR, "stages": [{"steps": [{"temperature_c": 120, "hold_s": 10}]}]}
    with pytest.raises(InvalidArgument, match="outside"):
        await hub.invoke("pcr-1", "run_profile", {"profile": too_hot})
    too_long = {**PCR, "stages": [{"repeat": 150, "steps": [{"temperature_c": 95, "hold_s": 10}]}]}
    with pytest.raises(InvalidArgument, match="max_cycles"):
        await hub.invoke("pcr-1", "run_profile", {"profile": too_long})
    with pytest.raises(InvalidArgument, match="Lid"):
        await hub.invoke("pcr-1", "run_profile", {"profile": {**PCR, "lid_temperature_c": 115}})


async def test_lid_must_be_closed(hub: DeviceHub):
    await hub.invoke("pcr-1", "open_lid", {})
    with pytest.raises(NeedsAttention, match="lid is open"):
        await hub.invoke("pcr-1", "run_profile", {"profile": PCR})
    await hub.invoke("pcr-1", "close_lid")
    assert (await hub.invoke("pcr-1", "status")).result["lid_open"] is False


async def test_deactivate_cancels_a_running_program(hub: DeviceHub):
    hub.device("pcr-1").backend.speedup = 50  # slow enough to interrupt
    accepted = await hub.invoke("pcr-1", "run_profile", {"profile": PCR})
    status = (await hub.invoke("pcr-1", "deactivate", caller="human")).result
    job = await hub.jobs.wait(accepted.job.id, 5)
    assert job.status == "cancelled"
    assert status["block_target_c"] is None and status["lid_target_c"] is None
    assert hub.device("pcr-1").state.value == "idle"


def test_estimate_ct_on_a_clean_sigmoid():
    curve = [1000 + 20000 / (1 + math.exp(-(c - 25) / 1.6)) for c in range(1, 41)]
    assert estimate_ct(curve) == pytest.approx(25 - 1.6 * math.log(9), abs=0.15)  # 10%-of-rise threshold
    assert estimate_ct([1000.0 + (c % 3) for c in range(40)]) is None
    assert estimate_ct(curve[:10]) is None


async def test_qpcr_run_returns_curves_and_ct(hub: DeviceHub):
    args = {"plate": "generic_96_pcr_plate_200ul", "profile": QPCR, "channels": ["FAM", "HEX"], "wells": ["A1", "A2", "A3", "A4", "A5"]}
    job = await hub.jobs.wait((await hub.invoke("qpcr-1", "run_qpcr", args)).job.id, 10)
    assert job.status == "succeeded", job.error
    result = job.result
    assert result["acquisitions"] == 40 and set(result["fluorescence"]) == set(args["wells"])
    assert len(result["fluorescence"]["A1"]["FAM"]) == 40
    sim = hub.device("qpcr-1").backend
    for well in args["wells"]:
        for channel in args["channels"]:
            midpoint = sim.midpoint_cycle(well, channel)
            ct = result["ct"][well][channel]
            if midpoint is None:
                assert ct is None
            else:
                assert ct == pytest.approx(midpoint - sim.slope * math.log(9), abs=0.5)


async def test_qpcr_validation(hub: DeviceHub):
    base = {"plate": "generic_96_pcr_plate_200ul", "profile": QPCR, "channels": ["FAM"]}
    with pytest.raises(InvalidArgument, match="not available"):
        await hub.invoke("qpcr-1", "run_qpcr", {**base, "channels": ["TexasRed"]})
    with pytest.raises(InvalidArgument, match="block takes"):
        await hub.invoke("qpcr-1", "run_qpcr", {**base, "plate": "generic_384_wellplate"})
    with pytest.raises(InvalidArgument, match="never acquires"):
        await hub.invoke("qpcr-1", "run_qpcr", {**base, "profile": PCR})
    with pytest.raises(InvalidArgument):
        await hub.invoke("qpcr-1", "run_qpcr", {**base, "wells": ["Q1"]})
    # A qPCR instrument is also a thermocycler.
    assert {"run_profile", "run_qpcr", "open_lid", "deactivate"} <= set(hub.device("qpcr-1").capability_specs())
