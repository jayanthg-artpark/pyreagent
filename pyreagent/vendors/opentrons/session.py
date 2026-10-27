"""One shared connection and run per Opentrons robot.

The pipettes and each module on a robot are separate pyreagent devices, but on
the robot they must live in one run: only one run can be current, and labware
on a module is loaded relative to the module's id in that run. A session is
shared by every device configured with the same robot address. It creates the
run when the first device connects and releases it when the last disconnects.

When the last device disconnects, the run is released and the robot homed.
If the run is stopped (``stop``, or the E-stop), the next device that connects
or resets starts a fresh run; ``generation`` increases and every device reloads
its own pipettes/modules into the new run before its next command.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping

import httpx

from ...core.errors import InvalidArgument, NeedsAttention
from .client import DEFAULT_PORT, OpentronsClient
from .deck import STAGING_SLOTS

log = logging.getLogger("pyreagent.opentrons")

ROBOTS: dict[str, dict[str, Any]] = {
    "ot2": {"model": "OT-2 Standard", "slots": [str(i) for i in range(1, 12)], "trash": "fixedTrash"},
    "flex": {"model": "OT-3 Standard", "slots": [f"{row}{col}" for row in "ABCD" for col in (1, 2, 3)], "trash": "movableTrashA3"},
}

# Module kind -> robot -> anchor slot -> slots the module covers (labware on it is loaded via the module).
MODULE_FOOTPRINTS: dict[str, dict[str, dict[str, list[str]]]] = {
    "thermocycler": {"ot2": {"7": ["7", "8", "10", "11"]}, "flex": {"B1": ["A1", "B1"]}},
    "absorbance_reader": {"flex": {slot: [slot] for slot in ("A3", "B3", "C3", "D3")}},  # lid docks in column 4
}
MODULE_TYPES = {
    "thermocycler": "thermocyclerModuleType",
    "temperature": "temperatureModuleType",
    "heater_shaker": "heaterShakerModuleType",
    "absorbance_reader": "absorbanceReaderType",
    "magnetic": "magneticModuleType",
}
# Modules that only some robots take.
MODULE_ROBOTS = {"absorbance_reader": {"flex"}, "magnetic_block": {"flex"}, "magnetic": {"ot2"}}
# Passive modules: no USB connection, so not in GET /modules; loaded into the run by model.
PASSIVE_MODELS = {"magnetic_block": "magneticBlockV1"}


@dataclass
class ModuleSlot:
    kind: str
    slot: str
    serial: str | None
    covers: list[str]
    model: str | None = None
    module_id: str | None = None
    generation: int = -1


class OpentronsSession:
    _registry: ClassVar[dict[tuple[Any, ...], "OpentronsSession"]] = {}

    @classmethod
    def get(
        cls,
        *,
        host: str | None,
        robot: str,
        port: int = DEFAULT_PORT,
        robot_name: str | None = None,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> "OpentronsSession":
        """The session for this robot address, created on first use.

        ``host="auto"`` finds the robot on the network (mDNS) when the run starts;
        ``robot_name`` picks one if several robots of that type are found.
        """
        if robot not in ROBOTS:
            raise ValueError(f"robot must be one of {sorted(ROBOTS)}")
        if host is None and transport is None:
            raise ValueError('host is required (an address, or "auto" to find the robot on the network)')
        if transport is not None:
            key: tuple[Any, ...] = ("transport", id(transport))
        elif host == "auto":
            key = ("auto", robot, robot_name)
        else:
            key = (host, port)
        session = cls._registry.get(key)
        if session is None:
            session = cls._registry[key] = cls(host=host or "robot", port=port, robot=robot, headers=headers, transport=transport)
            session.robot_name = robot_name
        elif session.robot != robot:
            raise ValueError(f"{session.client.base_url} is configured as both {session.robot!r} and {robot!r}")
        return session

    def __init__(self, *, host: str, port: int, robot: str, headers: Mapping[str, str] | None,
                 transport: httpx.AsyncBaseTransport | None) -> None:
        self.robot = robot
        self.client = OpentronsClient(host, port=port, headers=headers, transport=transport)
        self.command_timeout_s = 300.0
        self.home_after_run = True
        """Home every axis when the last device disconnects, so the robot is never left parked mid-deck."""
        self.run_id: str | None = None
        self.generation = 0
        self.stopped = False
        self.health: dict[str, Any] = {}
        self.deck_configuration: list[dict[str, Any]] = []
        self.users: set[str] = set()
        self.modules: dict[str, ModuleSlot] = {}
        self.auto = host == "auto" and transport is None
        self.robot_name: str | None = None
        self._stored_offsets: list[dict[str, Any]] | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle ------------------------------------------------------------------------------

    async def acquire(self, user: str) -> None:
        """Make sure there is a usable run; ``user`` names the device holding the session."""
        async with self._lock:
            if self.run_id is None or self.stopped:
                await self._start_run()
            self.users.add(user)

    async def release(self, user: str) -> None:
        async with self._lock:
            self.users.discard(user)
            if not self.users:
                await self._end_run(home=self.home_after_run)
                await self.client.close()

    async def _start_run(self) -> None:
        if self.run_id is not None:
            await self._end_run()
        else:
            if self.auto:
                from .discovery import choose, discover

                found = choose(await discover(), robot=self.robot, name=self.robot_name)
                self.client.base_url = f"http://{found.host}:{found.port}"
            await self.client.open()
        try:
            self.health = await self.client.health()
            expected = ROBOTS[self.robot]["model"]
            if self.health.get("robot_model") != expected:
                raise NeedsAttention(
                    f"Configured robot={self.robot!r}, but {self.client.base_url} reports {self.health.get('robot_model')!r}.",
                    suggested_action="Fix robot in this device's driver_options.",
                )
            if self.robot == "flex":
                self.deck_configuration = await self.client.deck_configuration()
            self.run_id = await self.client.create_run()
            self._stored_offsets = None  # re-read per run: someone may have run Labware Position Check meanwhile
        except BaseException:
            await self.client.close()
            raise
        self.stopped = False
        self.generation += 1

    async def _end_run(self, *, home: bool = False) -> None:
        run_id, self.run_id = self.run_id, None
        if run_id is None:
            return
        try:
            await self.client.stop_run(run_id)
        except Exception as exc:  # already stopped, or the robot went away
            log.debug("stopping run %s: %s", run_id, exc)
        try:
            await self.client.wait_for_run_end(run_id)
            await self.client.release_run(run_id)
        except Exception as exc:  # the robot stays busy for the Opentrons App until the run is released
            log.warning("could not release run %s on %s: %s", run_id, self.client.base_url, exc)
        if home:
            try:
                await self.client.home_robot()
            except Exception as exc:  # e.g. the E-stop is engaged: a human has to home it
                log.warning("could not home %s after the run: %s", self.client.base_url, exc)

    async def stop(self) -> None:
        """Halt the robot: stops the run for every device on it."""
        if self.run_id is not None and not self.stopped:
            self.stopped = True
            await self.client.stop_run(self.run_id)

    async def command(self, command_type: str, params: Mapping[str, Any], *, timeout_s: float | None = None) -> dict[str, Any]:
        if self.run_id is None:
            raise ConnectionError(f"no run on {self.client.base_url}; connect the device first")
        return await self.client.run_command(self.run_id, command_type, params, timeout_s=timeout_s or self.command_timeout_s)

    # -- modules --------------------------------------------------------------------------------

    def register_module(self, kind: str, slot: str, *, serial: str | None = None) -> ModuleSlot:
        """Declare which module sits in ``slot`` (at device construction; does not touch the robot)."""
        if self.robot not in MODULE_ROBOTS.get(kind, {self.robot}):
            raise ValueError(f"a {kind} module does not fit the {self.robot}")
        footprints = MODULE_FOOTPRINTS.get(kind, {}).get(self.robot)
        if footprints is not None and slot not in footprints:
            raise ValueError(f"a {kind} module on {self.robot} must be in slot {', '.join(footprints)}")
        if slot not in ROBOTS[self.robot]["slots"]:
            raise ValueError(f"no deck slot {slot!r} on {self.robot}")
        covers = footprints[slot] if footprints else [slot]
        for other in self.modules.values():
            if set(other.covers) & set(covers):
                raise ValueError(f"slot {slot} overlaps the {other.kind} module in slot {other.slot}")
        registration = ModuleSlot(kind=kind, slot=slot, serial=serial, covers=covers)
        self.modules[slot] = registration
        return registration

    def module_covering(self, slot: str) -> ModuleSlot | None:
        return next((m for m in self.modules.values() if slot in m.covers), None)

    async def attached_modules(self) -> list[dict[str, Any]]:
        return await self.client.modules()

    async def _resolve(self, registration: ModuleSlot) -> dict[str, Any]:
        """The attached module (GET /modules entry) that ``registration`` refers to."""
        wanted_type = MODULE_TYPES[registration.kind]
        candidates = [m for m in await self.client.modules() if m.get("moduleType") == wanted_type]
        if registration.serial:
            candidates = [m for m in candidates if m.get("serialNumber") == registration.serial]
        if not candidates:
            raise NeedsAttention(
                f"No {registration.kind} module" + (f" with serial {registration.serial}" if registration.serial else "")
                + f" is attached to {self.client.base_url}.",
                suggested_action="Ask a human to check the module's USB and power, then reset the device.",
            )
        if len(candidates) > 1:
            raise InvalidArgument(
                f"Several {registration.kind} modules are attached; set serial in this device's driver_options.",
                details={"serials": [m.get("serialNumber") for m in candidates]},
            )
        return candidates[0]

    async def module_status(self, slot: str) -> dict[str, Any]:
        """The live ``data`` block (temperatures, lid...) for the module in ``slot``."""
        registration = self.modules[slot]
        module = await self._resolve(registration)
        registration.serial = registration.serial or module.get("serialNumber")
        return module.get("data", {})

    async def module_id(self, slot: str) -> str:
        """The module's id in the current run, loading it into the run if needed."""
        registration = self.modules[slot]
        if registration.module_id is not None and registration.generation == self.generation:
            return registration.module_id
        if registration.kind in PASSIVE_MODELS:
            registration.model = PASSIVE_MODELS[registration.kind]
        else:
            module = await self._resolve(registration)
            registration.serial = registration.serial or module.get("serialNumber")
            registration.model = module["moduleModel"]
        result = await self.command("loadModule", {"model": registration.model, "location": {"slotName": slot}})
        registration.module_id, registration.generation = result["moduleId"], self.generation
        return registration.module_id

    async def stored_offset(self, definition_uri: str, slot: str) -> dict[str, float] | None:
        """The robot's stored Labware Position Check offset for this labware in ``slot``, if any.

        Like the Opentrons App: an offset saved for this exact deck slot wins over the
        labware's default (``anyLocation``) offset; the most recent of each kind counts.
        On a module only the default offset applies.
        """
        if self._stored_offsets is None:
            self._stored_offsets = await self.client.stored_labware_offsets()
        on_deck = [{"kind": "onAddressableArea", "addressableAreaName": slot}] if self.module_covering(slot) is None else None
        exact, default = [], []
        for offset in self._stored_offsets:
            if offset.get("definitionUri") != definition_uri:
                continue
            sequence = offset.get("locationSequence")
            if sequence == "anyLocation":
                default.append(offset)
            elif on_deck is not None and [{k: c.get(k) for k in ("kind", "addressableAreaName")} for c in sequence or []] == on_deck:
                exact.append(offset)
        best = max(exact or default, key=lambda o: o.get("createdAt", ""), default=None)
        return dict(best["vector"]) if best else None

    async def labware_location(self, slot: str) -> dict[str, Any]:
        """``loadLabware``/``moveLabware`` location for ``slot``: on the module there, or on the deck."""
        module = self.module_covering(slot)
        if module is None:
            return {"addressableAreaName": slot} if slot in STAGING_SLOTS else {"slotName": slot}
        if slot != module.slot:
            raise InvalidArgument(f"Slot {slot} is covered by the {module.kind} module in slot {module.slot}.")
        return {"moduleId": await self.module_id(slot)}
