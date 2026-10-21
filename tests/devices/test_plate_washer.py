from __future__ import annotations

import pytest

from conftest import approve_and_call
from pyreagent.core import DeviceHub, InvalidArgument, NeedsAttention


async def test_wash_uses_buffer_and_fills_waste(hub: DeviceHub):
    args = {"plate": "corning_96_wellplate_360ul", "buffer": "A", "dispense_volume_ul": 300, "cycles": 3}
    job = await hub.jobs.wait((await approve_and_call(hub, "washer-1", "wash", args)).job.id, 5)
    assert job.status == "succeeded", job.error
    assert job.result["columns"] == list(range(1, 13))
    assert job.result["buffer_used_ml"] == pytest.approx(96 * 3 * 0.3)
    status = (await hub.invoke("washer-1", "status")).result
    assert status["buffers_ml"]["A"] == pytest.approx(2000 - 86.4)
    assert status["waste_ml"] == pytest.approx(86.4)  # everything dispensed was aspirated again


async def test_strip_wash_and_dispense(hub: DeviceHub):
    args = {"plate": "corning_96_wellplate_360ul", "buffer": "B", "volume_ul": 100, "columns": [1, 2]}
    job = await hub.jobs.wait((await approve_and_call(hub, "washer-1", "dispense", args)).job.id, 5)
    assert job.result["buffer_used_ml"] == pytest.approx(16 * 0.1)


async def test_washer_validation_happens_before_approval(hub: DeviceHub):
    base = {"plate": "corning_96_wellplate_360ul", "buffer": "A", "dispense_volume_ul": 300}
    with pytest.raises(InvalidArgument, match="No buffer"):
        await hub.invoke("washer-1", "wash", {**base, "buffer": "Z"})
    with pytest.raises(InvalidArgument, match="columns 1..12"):
        await hub.invoke("washer-1", "wash", {**base, "columns": [13]})
    with pytest.raises(InvalidArgument, match="overflow"):
        await hub.invoke("washer-1", "wash", {**base, "dispense_volume_ul": 380})  # 360 µL wells
    with pytest.raises(InvalidArgument, match="allowed range"):
        await hub.invoke("washer-1", "wash", {**base, "dispense_volume_ul": 500})  # above max_dispense_ul
    with pytest.raises(InvalidArgument, match="6-well|wells; this washer"):
        await hub.invoke("washer-1", "wash", {**base, "plate": "generic_6_wellplate"})
    assert hub.confirmations.list() == []


async def test_full_waste_needs_a_human(hub: DeviceHub):
    hub.device("washer-1").backend.waste_ml = 3990
    args = {"plate": "corning_96_wellplate_360ul", "buffer": "A", "dispense_volume_ul": 300}
    with pytest.raises(NeedsAttention, match="waste"):
        await approve_and_call(hub, "washer-1", "wash", args)
    hub.device("washer-1").backend.levels["A"] = 5
    hub.device("washer-1").backend.waste_ml = 0
    with pytest.raises(NeedsAttention, match="refill|has 5"):
        await approve_and_call(hub, "washer-1", "prime", {"buffer": "A", "volume_ml": 50})
