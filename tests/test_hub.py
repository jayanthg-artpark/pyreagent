"""Agent-safety behaviour of the runtime: jobs, locking, gates, reservations, idempotency, faults, audit."""

from __future__ import annotations

import asyncio

import pytest

from pyreagent.core import (
    DeviceBusy,
    DeviceFault,
    DeviceHub,
    DeviceOffline,
    DeviceReserved,
    DeviceState,
    InvalidArgument,
    NeedsAttention,
    NotFound,
)
from pyreagent.devices.pump import Pump
from pyreagent.devices.pump.simulated import SimulatedPumpBackend


async def test_short_capability_completes_and_is_audited(hub: DeviceHub):
    hub.device("balance-1").backend.place(12.5)
    result = await hub.invoke("balance-1", "weigh", {}, caller="agent")
    assert result.status == "completed"
    assert result.result == {"grams": 12.5, "stable": True}
    record = hub.audit.query(device="balance-1")[0]
    assert (record.caller, record.capability, record.outcome) == ("agent", "weigh", "completed")
    assert record.args == {"stable": True, "timeout_s": 30.0}


async def test_unknown_device_and_capability(hub: DeviceHub):
    with pytest.raises(NotFound):
        await hub.invoke("nope", "weigh")
    with pytest.raises(NotFound) as err:
        await hub.invoke("balance-1", "dance")
    assert "weigh" in err.value.details["capabilities"]


async def test_long_running_capability_returns_job(hub: DeviceHub):
    accepted = await hub.invoke("reader-1", "read_absorbance", {"plate": "generic_96_wellplate", "wavelength_nm": 600})
    assert accepted.status == "accepted"
    job = await hub.jobs.wait(accepted.job.id, timeout_s=5)
    assert job.status == "succeeded", job.error
    assert job.progress == 1.0
    assert len(job.result["values"]) == 96
    assert hub.audit.query(event="job_finished")[0].outcome == "succeeded"


async def test_server_side_limits(hub: DeviceHub):
    with pytest.raises(InvalidArgument):
        await hub.invoke("reader-1", "read_absorbance", {"plate": "generic_96_wellplate", "wavelength_nm": 600, "wells": ["Z99"]})
    with pytest.raises(NotFound):
        await hub.invoke("reader-1", "read_absorbance", {"plate": "made_up_plate", "wavelength_nm": 600})
    # Instance limit (max_volume_ml=10). Checks that fail before a job's first await are
    # reported to the caller immediately rather than as a failed job.
    with pytest.raises(InvalidArgument) as err:
        await hub.invoke("pump-1", "dispense", {"volume_ml": 50, "rate_ml_min": 5})
    assert err.value.details == {"parameter": "volume_ml", "min": None, "max": 10}
    with pytest.raises(InvalidArgument):
        await hub.invoke("heater-1", "set_temperature", {"target_c": 300})
    with pytest.raises(InvalidArgument):  # schema allows >= 200 nm, this reader only >= 230 nm
        await hub.invoke("reader-1", "read_absorbance", {"plate": "generic_96_wellplate", "wavelength_nm": 210})


async def test_mutex_busy_reads_and_interrupts(slow_hub: DeviceHub):
    hub = slow_hub
    accepted = await hub.invoke("pump-1", "dispense", {"volume_ml": 5, "rate_ml_min": 1})
    pump = hub.device("pump-1")
    assert pump.state is DeviceState.BUSY  # claimed before invoke returned
    # A second mutating command fails fast and says which job to wait for.
    with pytest.raises(DeviceBusy) as busy:
        await hub.invoke("pump-1", "dispense", {"volume_ml": 1, "rate_ml_min": 1}, caller="agent-2")
    assert busy.value.details["job_id"] == accepted.job.id
    # Reads still work.
    status = await hub.invoke("pump-1", "status")
    assert status.result["running"] is True
    # A safety stop always works and ends the job as interrupted.
    await asyncio.sleep(0.05)
    stopped = await hub.invoke("pump-1", "stop", caller="agent-2")
    assert stopped.result["running"] is False
    job = await hub.jobs.wait(accepted.job.id, 5)
    assert job.status == "failed" and job.error["code"] == "interrupted"
    assert job.error["details"]["moved_ml"] > 0
    assert pump.state is DeviceState.IDLE


async def test_cancel_job_stops_the_hardware(slow_hub: DeviceHub):
    hub = slow_hub
    accepted = await hub.invoke("pump-1", "dispense", {"volume_ml": 5, "rate_ml_min": 1})
    await asyncio.sleep(0.05)
    job = await hub.jobs.cancel(accepted.job.id)
    assert job.status == "cancelled"
    status = (await hub.invoke("pump-1", "status")).result
    assert status["running"] is False
    assert hub.device("pump-1").state is DeviceState.IDLE


async def test_reservations(hub: DeviceHub):
    hub.reserve("balance-1", caller="agent-a", ttl_s=60)
    with pytest.raises(DeviceReserved):
        await hub.invoke("balance-1", "tare", caller="agent-b")
    assert (await hub.invoke("balance-1", "weigh", caller="agent-b")).status == "completed"  # reads allowed
    assert (await hub.invoke("balance-1", "tare", caller="agent-a")).status == "completed"
    with pytest.raises(DeviceReserved):
        hub.release("balance-1", caller="agent-b")
    with pytest.raises(InvalidArgument):
        hub.reserve("balance-1", caller="anonymous", ttl_s=60)
    hub.release("balance-1", caller="agent-a")
    assert (await hub.invoke("balance-1", "tare", caller="agent-b")).status == "completed"


async def test_interrupts_bypass_reservations(hub: DeviceHub):
    hub.reserve("shaker-1", caller="agent-a", ttl_s=60)
    await hub.invoke("shaker-1", "start_shaking", {"rpm": 500}, caller="agent-a")
    stopped = await hub.invoke("shaker-1", "stop_shaking", caller="agent-b")
    assert stopped.result == {"shaking": False, "rpm": 0.0}


async def test_idempotency_key(hub: DeviceHub):
    first = await hub.invoke("reader-1", "read_absorbance", {"plate": "generic_6_wellplate", "wavelength_nm": 450},
                             caller="agent", idempotency_key="read-1")
    retry = await hub.invoke("reader-1", "read_absorbance", {"plate": "generic_6_wellplate", "wavelength_nm": 450},
                             caller="agent", idempotency_key="read-1")
    assert retry.job.id == first.job.id
    assert len(hub.jobs.list(device="reader-1")) == 1
    with pytest.raises(InvalidArgument):
        await hub.invoke("reader-1", "read_absorbance", {"plate": "generic_6_wellplate", "wavelength_nm": 600},
                         caller="agent", idempotency_key="read-1")


async def test_hardware_fault_locks_device_until_reset(hub: DeviceHub):
    pump = hub.device("pump-1")
    pump.backend.alarm = "motor stalled"
    accepted = await hub.invoke("pump-1", "dispense", {"volume_ml": 1, "rate_ml_min": 10})
    job = await hub.jobs.wait(accepted.job.id, 5)
    assert job.status == "failed" and job.error["code"] == "hardware_error"
    assert pump.state is DeviceState.ERROR
    with pytest.raises(DeviceFault):
        await hub.invoke("pump-1", "dispense", {"volume_ml": 1, "rate_ml_min": 10}, caller="agent")
    assert (await hub.invoke("pump-1", "status")).status == "completed"  # reads still allowed
    pump.backend.alarm = None
    await hub.invoke("pump-1", "reset", {})
    assert pump.state is DeviceState.IDLE
    assert hub.describe_device("pump-1")["last_error"] is None


async def test_unreachable_device_is_offline_not_fatal():
    class Broken(SimulatedPumpBackend):
        async def connect(self) -> None:
            raise OSError("COM7: device not found")

    hub = DeviceHub([Pump("pump-x", Broken(), max_volume_ml=1, max_rate_ml_min=1)])
    await hub.start()
    info = hub.describe_device("pump-x")
    assert info["state"] == "offline"
    assert info["last_error"]["code"] == "communication_error"
    with pytest.raises(DeviceOffline):
        await hub.invoke("pump-x", "status")
    await hub.stop()


async def test_temperature_wait_and_progress(hub: DeviceHub):
    await hub.invoke("heater-1", "set_temperature", {"target_c": 37})
    accepted = await hub.invoke("heater-1", "wait_for_temperature", {"tolerance_c": 0.5, "timeout_s": 10})
    job = await hub.jobs.wait(accepted.job.id, 10)
    assert job.status == "succeeded", job.error
    assert abs(job.result["current_c"] - 37) <= 0.5
    reading = (await hub.invoke("heater-1", "deactivate")).result
    assert reading["active"] is False


async def test_plate_reader_needs_closed_tray(hub: DeviceHub):
    await hub.invoke("reader-1", "open_tray", {})
    with pytest.raises(NeedsAttention) as err:
        await hub.invoke("reader-1", "read_absorbance", {"plate": "generic_96_wellplate", "wavelength_nm": 600})
    assert err.value.recoverable
    await hub.invoke("reader-1", "close_tray")
    accepted = await hub.invoke("reader-1", "read_absorbance", {"plate": "generic_96_wellplate", "wavelength_nm": 600, "wells": ["a1", "H12"]})
    job = await hub.jobs.wait(accepted.job.id, 5)
    assert set(job.result["values"]) == {"A1", "H12"}


async def test_direct_python_use_is_guarded_too():
    pump = Pump("pump-1", SimulatedPumpBackend(speedup=1), max_volume_ml=10, max_rate_ml_min=20, poll_interval_s=0.01)
    await pump.connect()
    task = asyncio.create_task(pump.dispense(5, 1))
    await asyncio.sleep(0.05)
    with pytest.raises(DeviceBusy):
        await pump.withdraw(1, 1)
    await pump.stop()
    with pytest.raises(Exception) as err:
        await task
    assert getattr(err.value, "code", None) == "interrupted"
