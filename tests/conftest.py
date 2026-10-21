from __future__ import annotations

import pytest

from pyreagent.core.hub import DeviceHub
from pyreagent.devices.balance import Balance
from pyreagent.devices.balance.simulated import SimulatedBalanceBackend
from pyreagent.devices.liquid_handler import LiquidHandler
from pyreagent.devices.liquid_handler.simulated import SimulatedLiquidHandlerBackend
from pyreagent.devices.plate_washer import PlateWasher
from pyreagent.devices.plate_washer.simulated import SimulatedPlateWasherBackend
from pyreagent.devices.qpcr import Qpcr
from pyreagent.devices.qpcr.simulated import SimulatedQpcrBackend
from pyreagent.devices.thermocycler import Thermocycler
from pyreagent.devices.thermocycler.simulated import SimulatedThermocyclerBackend
from pyreagent.devices.plate_reader import PlateReader
from pyreagent.devices.plate_reader.simulated import SimulatedPlateReaderBackend
from pyreagent.devices.pump import Pump
from pyreagent.devices.pump.simulated import SimulatedPumpBackend
from pyreagent.devices.shaker import Shaker
from pyreagent.devices.shaker.simulated import SimulatedShakerBackend
from pyreagent.devices.temperature_controller import TemperatureController
from pyreagent.devices.temperature_controller.simulated import SimulatedTemperatureControllerBackend

DECK = {
    "1": "generic_96_tiprack_300ul",
    "2": "generic_96_tiprack_20ul",
    "3": {"labware": "generic_12_reservoir_15ml", "initial_volume_ul": 10000},
    "4": {"labware": "corning_96_wellplate_360ul", "initial_volume_ul": 0},
}


def make_devices(*, pump_speedup: float = 600.0, lh_seconds_per_op: float = 0.0):
    return [
        Balance("balance-1", SimulatedBalanceBackend(settle_s=0)),
        Pump("pump-1", SimulatedPumpBackend(speedup=pump_speedup), max_volume_ml=10, max_rate_ml_min=20, poll_interval_s=0.01),
        PlateReader("reader-1", SimulatedPlateReaderBackend(seconds_per_well=0, tray_s=0)),
        TemperatureController("heater-1", SimulatedTemperatureControllerBackend(tau_s=0.05), min_c=4, max_c=95, poll_interval_s=0.01),
        Shaker("shaker-1", SimulatedShakerBackend(), min_rpm=100, max_rpm=3000),
        LiquidHandler("lh-1", SimulatedLiquidHandlerBackend(seconds_per_operation=lh_seconds_per_op), deck=DECK),
        Thermocycler("pcr-1", SimulatedThermocyclerBackend(speedup=100000)),
        Qpcr("qpcr-1", SimulatedQpcrBackend(speedup=100000, channels=["FAM", "HEX"])),
        PlateWasher("washer-1", SimulatedPlateWasherBackend(seconds_per_column=0)),
    ]

DEVICE_NAMES = {"balance-1", "pump-1", "reader-1", "heater-1", "shaker-1", "lh-1", "pcr-1", "qpcr-1", "washer-1"}


@pytest.fixture
async def hub():
    h = DeviceHub(make_devices(), name="test-lab")
    await h.start()
    yield h
    await h.stop()


@pytest.fixture
async def slow_hub():
    """Pump runs in real time, so a dispense stays busy long enough to poke at."""
    h = DeviceHub(make_devices(pump_speedup=1.0), name="test-lab")
    await h.start()
    yield h
    await h.stop()


async def approve_and_call(hub: DeviceHub, device: str, capability: str, args: dict, *, caller: str = "agent"):
    """Run a gated call the way an agent would: request, get human approval, repeat."""
    from pyreagent.core.errors import ConfirmationRequired

    try:
        return await hub.invoke(device, capability, args, caller=caller)
    except ConfirmationRequired as err:
        cid = err.details["confirmation_id"]
    hub.decide_confirmation(cid, approve=True, operator="human")
    return await hub.invoke(device, capability, args, caller=caller, confirmation_id=cid)
