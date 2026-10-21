"""Opentrons Thermocycler Module (GEN1 / GEN2), through the robot it is plugged into.

Shares the robot's run with the pipettes (``vendors.opentrons.session``), so a
plate on the thermocycler can be pipetted into by the Opentrons liquid handler
device. Status comes from ``GET /modules``; commands are ``thermocycler/*``.

Profiles are stepped from here by default (set target, wait, hold): that keeps
the robot's command queue free, so the pipettes can keep working during a long
PCR run. ``native_profiles=True`` hands the whole profile to the robot
(``thermocycler/runExtendedProfile``) for robot-timed holds, which blocks the
robot's other commands until it finishes.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

import httpx

from ....vendors.opentrons.client import DEFAULT_PORT
from ....vendors.opentrons.session import OpentronsSession
from ..backend import AfterStep, Progress, ThermocyclerBackend
from ..models import ThermalProfile, ThermocyclerStatus

ANCHOR_SLOT = {"ot2": "7", "flex": "B1"}


class OpentronsThermocyclerBackend(ThermocyclerBackend):
    vendor = "Opentrons"
    model = "Thermocycler Module (via robot)"
    features = frozenset({"ramp_rate"})

    def __init__(
        self,
        host: str | None = None,
        *,
        robot: str,
        port: int = DEFAULT_PORT,
        serial: str | None = None,
        native_profiles: bool = False,
        block_max_volume_ul: float | None = None,
        poll_s: float = 1.0,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Args:
            host, robot: The robot the module is plugged into (``"ot2"`` or ``"flex"``).
            serial: Module serial number, needed only if several thermocyclers are attached.
            native_profiles: Run profiles on the robot (precise holds, but blocks other robot commands).
            block_max_volume_ul: Sample volume per well (0-100 µL); improves temperature accuracy.
        """
        self.session = OpentronsSession.get(host=host, robot=robot, port=port, headers=headers, transport=transport)
        self.slot = ANCHOR_SLOT[robot]
        self.session.register_module("thermocycler", self.slot, serial=serial)
        self.native_profiles = native_profiles
        self.block_max_volume_ul = block_max_volume_ul
        self.poll_s = poll_s
        self._user = f"thermocycler@{self.slot}"

    async def connect(self) -> None:
        await self.session.acquire(self._user)
        try:
            await self.session.module_id(self.slot)
        except BaseException:
            await self.session.release(self._user)
            raise

    async def disconnect(self) -> None:
        await self.session.release(self._user)

    async def _command(self, command_type: str, **params: Any) -> dict[str, Any]:
        module_id = await self.session.module_id(self.slot)
        return await self.session.command(command_type, {"moduleId": module_id, **params})

    async def open_lid(self) -> None:
        await self._command("thermocycler/openLid")

    async def close_lid(self) -> None:
        await self._command("thermocycler/closeLid")

    async def lid_is_open(self) -> bool:
        return (await self.session.module_status(self.slot)).get("lidStatus") == "open"

    async def set_block_temperature(self, target_c: float, *, ramp_rate_c_per_s: float | None = None) -> None:
        params: dict[str, Any] = {"celsius": target_c}
        if self.block_max_volume_ul is not None:
            params["blockMaxVolumeUl"] = self.block_max_volume_ul
        if ramp_rate_c_per_s is not None:
            params["ramp_rate"] = ramp_rate_c_per_s  # snake_case in the Opentrons schema
        await self._command("thermocycler/setTargetBlockTemperature", **params)

    async def set_lid_temperature(self, target_c: float) -> None:
        await self._command("thermocycler/setTargetLidTemperature", celsius=target_c)

    async def deactivate_block(self) -> None:
        await self._command("thermocycler/deactivateBlock")

    async def deactivate_lid(self) -> None:
        await self._command("thermocycler/deactivateLid")

    async def read(self) -> ThermocyclerStatus:
        data = await self.session.module_status(self.slot)
        return ThermocyclerStatus(
            block_c=data.get("currentTemperature") or 0.0,
            block_target_c=data.get("targetTemperature"),
            lid_c=data.get("lidTemperature") or 0.0,
            lid_target_c=data.get("lidTargetTemperature"),
            lid_open=data.get("lidStatus") == "open",
        )

    async def run_profile(self, profile: ThermalProfile, *, progress: Progress, after_step: AfterStep | None = None) -> int:
        if not self.native_profiles or after_step is not None:
            return await super().run_profile(profile, progress=progress, after_step=after_step)
        await self.set_lid_temperature(profile.lid_temperature_c)
        progress(0.0, f"heating lid to {profile.lid_temperature_c:g} °C")
        await self._command("thermocycler/waitForLidTemperature")
        elements: list[dict[str, Any]] = []
        for stage in profile.stages:
            steps = [{"celsius": s.temperature_c, "holdSeconds": s.hold_s,
                      **({"rampRate": s.ramp_rate_c_per_s} if s.ramp_rate_c_per_s else {})} for s in stage.steps]
            elements.extend(steps if stage.repeat == 1 else [{"steps": steps, "repetitions": stage.repeat}])
        params: dict[str, Any] = {"profileElements": elements}
        if self.block_max_volume_ul is not None:
            params["blockMaxVolumeUl"] = self.block_max_volume_ul
        hold_s = sum(s.hold_s * st.repeat for st in profile.stages for s in st.steps)
        ramp_s = sum(abs(s.temperature_c - 60) / 2 * st.repeat for st in profile.stages for s in st.steps)
        module_id = await self.session.module_id(self.slot)
        reporter = asyncio.create_task(self._report(progress))
        try:
            await self.session.command("thermocycler/runExtendedProfile", {"moduleId": module_id, **params},
                                       timeout_s=hold_s + ramp_s + 900)
        finally:
            reporter.cancel()
        if profile.final_hold_c is None:
            await self.deactivate()
        else:
            await self.set_block_temperature(profile.final_hold_c)
        return profile.total_steps

    async def _report(self, progress: Progress) -> None:
        while True:
            await asyncio.sleep(self.poll_s)
            try:
                data = await self.session.module_status(self.slot)
            except Exception:
                continue
            cycle, cycles = data.get("currentCycleIndex"), data.get("totalCycleCount")
            if cycle and cycles:
                progress((cycle - 1) / cycles, f"cycle {cycle}/{cycles} at {data.get('currentTemperature')} °C")

    def info(self) -> dict[str, Any]:
        registration = self.session.modules[self.slot]
        return {**super().info(), "robot": self.session.robot, "url": self.session.client.base_url, "slot": self.slot,
                "module_model": registration.model, "serial": registration.serial, "native_profiles": self.native_profiles}
