"""A fake Opentrons robot-server for testing the Opentrons driver without a robot.

Implements the endpoints the drivers use and enforces the rules they depend on:
version header, required command fields (from command schema 18), one current
run at a time, homing before motion, tip state, slot occupancy, stop/release,
and attached modules (GET /modules, loadModule, thermocycler/*, temperatureModule/*).
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any

import httpx

# Required params per command type, transcribed from shared-data/command/schemas/18.json.
REQUIRED = {
    "loadPipette": {"pipetteName", "mount"},
    "loadLabware": {"location", "loadName", "namespace", "version"},
    "moveLabware": {"labwareId", "newLocation", "strategy"},
    "home": set(),
    "pickUpTip": {"pipetteId", "labwareId", "wellName"},
    "aspirate": {"pipetteId", "labwareId", "wellName", "flowRate", "volume"},
    "dispense": {"pipetteId", "labwareId", "wellName", "flowRate", "volume"},
    "moveToAddressableAreaForDropTip": {"pipetteId", "addressableAreaName"},
    "dropTipInPlace": {"pipetteId"},
    "loadModule": {"model", "location"},
    "thermocycler/openLid": {"moduleId"},
    "thermocycler/closeLid": {"moduleId"},
    "thermocycler/setTargetBlockTemperature": {"moduleId", "celsius"},
    "thermocycler/setTargetLidTemperature": {"moduleId", "celsius"},
    "thermocycler/waitForLidTemperature": {"moduleId"},
    "thermocycler/deactivateBlock": {"moduleId"},
    "thermocycler/deactivateLid": {"moduleId"},
    "thermocycler/runExtendedProfile": {"moduleId", "profileElements"},
    "temperatureModule/setTargetTemperature": {"moduleId", "celsius"},
    "temperatureModule/deactivate": {"moduleId"},
    "blowout": {"pipetteId", "labwareId", "wellName", "flowRate"},
    "touchTip": {"pipetteId", "labwareId", "wellName"},
    "moveToWell": {"pipetteId", "labwareId", "wellName"},
    "tryLiquidProbe": {"pipetteId", "labwareId", "wellName"},
    "verifyTipPresence": {"pipetteId", "expectedState"},
    "configureNozzleLayout": {"pipetteId", "configurationParams"},
    "configureForVolume": {"pipetteId", "volume"},
    "prepareToAspirate": {"pipetteId"},
    "setRailLights": {"on"},
    "dropTip": {"pipetteId", "labwareId", "wellName"},
}
MOTION = {"pickUpTip", "aspirate", "dispense", "moveToAddressableAreaForDropTip", "dropTipInPlace", "blowout", "touchTip",
          "moveToWell", "tryLiquidProbe", "dropTip", "moveLabware"}
WELL_TOP_Z = 100.0
WELL_DEPTH = 10.5
ALL_WELLS = [f"{r}{c}" for r in "ABCDEFGHIJKLMNOP" for c in range(1, 25)]

OT2_PIPETTES = [
    {"mount": "left", "instrumentType": "pipette", "instrumentName": "p300_single_gen2", "instrumentModel": "p300_single_v2.1",
     "serialNumber": "P3HSV2100000", "ok": True, "data": {"channels": 1, "min_volume": 20.0, "max_volume": 300.0}},
    {"mount": "right", "instrumentType": "pipette", "instrumentName": "p20_multi_gen2", "instrumentModel": "p20_multi_v2.1",
     "serialNumber": "P20MV2100000", "ok": True, "data": {"channels": 8, "min_volume": 1.0, "max_volume": 20.0}},
]


def thermocycler(serial: str = "TC2-0001") -> dict[str, Any]:
    return {"id": f"hw-{serial}", "serialNumber": serial, "moduleModel": "thermocyclerModuleV2", "moduleType": "thermocyclerModuleType",
            "data": {"status": "idle", "currentTemperature": 23.0, "targetTemperature": None, "lidStatus": "closed",
                     "lidTemperatureStatus": "idle", "lidTemperature": 23.0, "lidTargetTemperature": None,
                     "currentCycleIndex": None, "totalCycleCount": None}}


def temperature_module(serial: str = "TDV2-0001") -> dict[str, Any]:
    return {"id": f"hw-{serial}", "serialNumber": serial, "moduleModel": "temperatureModuleV2", "moduleType": "temperatureModuleType",
            "data": {"status": "idle", "currentTemperature": 23.0, "targetTemperature": None}}


def _error(status: int, kind: str, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"errors": [{"id": kind, "title": kind, "detail": detail}]})


class FakeOpentronsRobot:
    def __init__(self, *, model: str = "OT-2 Standard", instruments: list[dict[str, Any]] | None = None,
                 homed: bool = True, delay_s: float = 0.0, running_polls: int = 0,
                 modules: list[dict[str, Any]] | None = None, deck_fixtures: list[dict[str, Any]] | None = None,
                 software: str = "8.6.0") -> None:
        self.model = model
        self.software = software
        self.instruments = instruments if instruments is not None else OT2_PIPETTES
        self.attached_modules = modules or []
        self.deck_fixtures = deck_fixtures if deck_fixtures is not None else [
            {"cutoutId": f"cutout{r}{c}", "cutoutFixtureId": {1: "singleLeftSlot", 2: "singleCenterSlot", 3: "singleRightSlot"}[c],
             "opentronsModuleSerialNumber": None} for r in "ABCD" for c in (1, 2, 3) if (r, c) != ("A", 3)
        ] + [{"cutoutId": "cutoutA3", "cutoutFixtureId": "trashBinAdapter", "opentronsModuleSerialNumber": None}]
        self.lights = False
        self.liquid_heights: dict[str, float | None] = {}
        """Well name -> liquid height above the bottom reported by tryLiquidProbe (default 5.0; None = no liquid)."""
        self.definitions: list[dict[str, Any]] = []
        self.homed = homed
        self.delay_s = delay_s
        self.running_polls = running_polls
        self.runs: dict[str, dict[str, Any]] = {}
        self.current: str | None = None
        self.sent: list[dict[str, Any]] = []  # every command create body ("data"), in order
        self.polls = 0
        self._ids = itertools.count(1)
        self._reset_engine()

    def _reset_engine(self) -> None:
        self.pipettes: dict[str, dict[str, Any]] = {}
        self.labware: dict[str, str] = {}  # labwareId -> slot or "offDeck"
        self.loaded_modules: dict[str, dict[str, Any]] = {}  # moduleId -> attached module
        self.slots: dict[str, str] = {}  # slot -> labwareId

    @property
    def transport(self) -> httpx.MockTransport:
        """One transport per fake robot: devices on the same robot share it (and so share a session)."""
        if not hasattr(self, "_transport"):
            self._transport = httpx.MockTransport(self.handle)
        return self._transport

    def commands(self, run_id: str | None = None) -> list[str]:
        return [c["commandType"] for c in self.sent if run_id is None or c.get("_run") == run_id]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if "Opentrons-Version" not in request.headers:
            return _error(400, "InvalidVersionHeader", "missing Opentrons-Version")
        method, parts = request.method, request.url.path.strip("/").split("/")
        body = json.loads(request.content) if request.content else {}
        if (method, parts) == ("GET", ["health"]):
            return httpx.Response(200, json={"name": "fake-robot", "robot_model": self.model, "api_version": self.software})
        if (method, parts) == ("GET", ["modules"]):
            return httpx.Response(200, json={"data": self.attached_modules, "meta": {"cursor": 0, "totalLength": len(self.attached_modules)}})
        if (method, parts) == ("GET", ["deck_configuration"]):
            if self.model != "OT-3 Standard":
                return _error(404, "NotFound", "deck configuration is Flex-only")
            return httpx.Response(200, json={"data": {"cutoutFixtures": self.deck_fixtures, "lastModifiedAt": None}})
        if (method, parts) == ("GET", ["instruments"]):
            return httpx.Response(200, json={"data": self.instruments, "meta": {"cursor": 0, "totalLength": len(self.instruments)}})
        if (method, parts) == ("POST", ["runs"]):
            if self.current is not None:
                return _error(409, "RunAlreadyActive", f"run {self.current} is currently active")
            run_id = f"run-{next(self._ids)}"
            self.runs[run_id] = {"id": run_id, "status": "idle", "current": True, "commands": {}}
            self.current = run_id
            self._reset_engine()
            return httpx.Response(201, json={"data": {k: v for k, v in self.runs[run_id].items() if k != "commands"}})
        if len(parts) >= 2 and parts[0] == "runs" and parts[1] in self.runs:
            run = self.runs[parts[1]]
            if method == "PATCH" and len(parts) == 2:
                if body["data"].get("current") is False:
                    run["current"] = False
                    if self.current == run["id"]:
                        self.current = None
                return httpx.Response(200, json={"data": {"id": run["id"], "current": run["current"]}})
            if method == "POST" and parts[2:] == ["actions"]:
                if body["data"]["actionType"] == "stop":
                    run["status"] = "stopped"
                return httpx.Response(201, json={"data": {"id": f"action-{next(self._ids)}", **body["data"]}})
            if method == "POST" and parts[2:] == ["labware_definitions"]:
                definition = body["data"]
                self.definitions.append(definition)
                uri = f"{definition['namespace']}/{definition['parameters']['loadName']}/{definition['version']}"
                return httpx.Response(201, json={"data": {"definitionUri": uri}})
            if method == "POST" and parts[2:] == ["commands"]:
                return await self._create_command(run, body["data"], request.url.params)
            if method == "GET" and len(parts) == 4 and parts[2] == "commands":
                self.polls += 1
                command = run["commands"][parts[3]]
                if command["status"] == "running":
                    command.update(command.pop("_final"))
                return httpx.Response(200, json={"data": command})
        return _error(404, "NotFound", f"{method} {request.url.path}")

    async def _create_command(self, run: dict[str, Any], data: dict[str, Any], query: httpx.QueryParams) -> httpx.Response:
        if run["status"] == "stopped" or not run["current"]:
            return _error(409, "RunStopped", f"Run {run['id']} is not the current run")
        assert query.get("waitUntilComplete") == "true" and int(query["timeout"]) > 0
        self.sent.append({**data, "_run": run["id"]})
        command_type, params = data["commandType"], data.get("params", {})
        missing = REQUIRED[command_type] - set(params)
        if missing:
            return _error(422, "InvalidRequest", f"{command_type} missing {sorted(missing)}")
        if command_type in MOTION and self.delay_s:
            await asyncio.sleep(self.delay_s)
        outcome = self._execute(run, command_type, params)
        command = {"id": f"cmd-{next(self._ids)}", "commandType": command_type, "intent": data.get("intent"),
                   "params": params, **outcome}
        if self.running_polls and outcome["status"] == "succeeded":
            self.running_polls -= 1
            command["_final"] = {"status": "succeeded", "result": command.pop("result")}
            command["status"] = "running"
        run["commands"][command["id"]] = command
        return httpx.Response(201, json={"data": {k: v for k, v in command.items() if k != "_final"}})

    def _execute(self, run: dict[str, Any], command_type: str, p: dict[str, Any]) -> dict[str, Any]:
        def fail(kind: str, detail: str) -> dict[str, Any]:
            return {"status": "failed", "error": {"id": "err", "errorType": kind, "errorCode": "4000", "detail": detail}}

        if run["status"] == "stopped":
            return fail("RunStoppedError", "The run was stopped.")
        if command_type in MOTION and not self.homed:
            return fail("MustHomeError", "Machine is not homed yet.")
        if command_type == "home":
            self.homed = True
            return {"status": "succeeded", "result": {}}
        if command_type == "loadPipette":
            attached = {i["mount"]: i.get("instrumentName") for i in self.instruments if i["instrumentType"] == "pipette"}
            if attached.get(p["mount"]) != p["pipetteName"]:
                return fail("PipetteNotAttachedError", f"no {p['pipetteName']} on {p['mount']}")
            pipette_id = f"pipette-{p['mount']}"
            channels = next(i["data"]["channels"] for i in self.instruments if i["mount"] == p["mount"])
            self.pipettes[pipette_id] = {"mount": p["mount"], "tip": False, "volume": 0.0, "ready": True,
                                         "layout": {"style": "ALL"}, "channels": channels}
            return {"status": "succeeded", "result": {"pipetteId": pipette_id}}
        if command_type == "loadModule":
            slot = p["location"]["slotName"]
            module = next((m for m in self.attached_modules if m["moduleModel"] == p["model"]), None)
            if module is None:
                return fail("ModuleNotAttachedError", f"no {p['model']} attached")
            if slot in self.slots:
                return fail("LocationIsOccupiedError", f"slot {slot} is occupied")
            module_id = f"module-{p['model']}-{slot}"
            self.loaded_modules[module_id], self.slots[slot] = module, module_id
            return {"status": "succeeded", "result": {"moduleId": module_id, "model": p["model"], "serialNumber": module["serialNumber"]}}
        if command_type.startswith(("thermocycler/", "temperatureModule/")):
            module = self.loaded_modules.get(p["moduleId"])
            if module is None:
                return fail("ModuleNotLoadedError", p["moduleId"])
            data, action = module["data"], command_type.split("/", 1)[1]
            if action == "openLid":
                data["lidStatus"] = "open"
            elif action == "closeLid":
                data["lidStatus"] = "closed"
            elif action in ("setTargetBlockTemperature", "setTargetTemperature"):
                data["targetTemperature"] = data["currentTemperature"] = p["celsius"]
            elif action == "setTargetLidTemperature":
                data["lidTargetTemperature"] = data["lidTemperature"] = p["celsius"]
            elif action in ("deactivateBlock", "deactivate"):
                data["targetTemperature"] = None
            elif action == "deactivateLid":
                data["lidTargetTemperature"] = None
            elif action == "runExtendedProfile":
                last = p["profileElements"][-1]
                data["currentTemperature"] = (last["steps"][-1] if "steps" in last else last)["celsius"]
            return {"status": "succeeded", "result": {}}
        if command_type == "loadLabware":
            location = p["location"]
            if "moduleId" in location:
                if location["moduleId"] not in self.loaded_modules:
                    return fail("ModuleNotLoadedError", location["moduleId"])
                labware_id = f"labware-{next(self._ids)}"
                self.labware[labware_id] = location["moduleId"]
                return {"status": "succeeded", "result": {"labwareId": labware_id, "definition": {}}}
            slot = location.get("slotName") or location["addressableAreaName"]
            if slot in self.slots:
                return fail("LocationIsOccupiedError", f"slot {slot} is occupied")
            labware_id = f"labware-{next(self._ids)}"
            self.labware[labware_id], self.slots[slot] = slot, labware_id
            definition = {"parameters": {"loadName": p["loadName"], "isTiprack": "tiprack" in p["loadName"]},
                          "wells": {w: {"depth": WELL_DEPTH} for w in ALL_WELLS}}
            return {"status": "succeeded", "result": {"labwareId": labware_id, "definition": definition}}
        if command_type == "setRailLights":
            self.lights = p["on"]
            return {"status": "succeeded", "result": {}}
        if command_type == "moveLabware":
            if p["strategy"] == "usingGripper":
                if self.model != "OT-3 Standard":
                    return fail("NotSupportedOnRobotType", "the OT-2 has no gripper")
                if not any(i["instrumentType"] == "gripper" for i in self.instruments):
                    return fail("GripperNotAttachedError", "no gripper attached")
            slot = self.labware[p["labwareId"]]
            self.slots.pop(slot, None)
            target = p["newLocation"]
            if isinstance(target, str):
                new = target
            else:
                new = target.get("slotName") or target.get("addressableAreaName") or target["moduleId"]
            if new != "offDeck":
                if new in self.slots:
                    return fail("LocationIsOccupiedError", f"{new} is occupied")
                self.slots[new] = p["labwareId"]
            self.labware[p["labwareId"]] = new
            return {"status": "succeeded", "result": {}}
        pipette = self.pipettes.get(p["pipetteId"])
        if pipette is None:
            return fail("PipetteNotLoadedError", p["pipetteId"])
        flex = self.model == "OT-3 Standard"
        if command_type == "configureNozzleLayout":
            config = p["configurationParams"]
            if pipette["tip"]:
                return fail("CommandPreconditionViolated", "cannot change nozzle layout with a tip attached")
            if pipette["channels"] == 1 or (pipette["channels"] == 8 and config["style"] in ("ROW", "COLUMN")):
                return fail("InvalidNozzleConfiguration", f"{config} on a {pipette['channels']}-channel")
            pipette["layout"] = config
            return {"status": "succeeded", "result": {}}
        if command_type == "verifyTipPresence":
            if flex and (p["expectedState"] == "present") != pipette["tip"]:
                kind = "TipNotAttachedError" if p["expectedState"] == "present" else "TipAttachedError"
                return fail(kind, "tip sensor mismatch")
            return {"status": "succeeded", "result": {}}
        if command_type == "configureForVolume":
            if pipette["volume"] > 0:
                return fail("CommandPreconditionViolated", "configureForVolume needs an empty tip")
            pipette["ready"] = False
            return {"status": "succeeded", "result": {}}
        if command_type == "prepareToAspirate":
            pipette["ready"] = True
            return {"status": "succeeded", "result": {}}
        if command_type == "pickUpTip":
            if pipette["tip"]:
                return fail("TipAttachedError", "tip already attached")
            pipette["tip"], pipette["volume"] = True, 0.0
        elif command_type in ("aspirate", "dispense", "blowout", "touchTip", "tryLiquidProbe") and not pipette["tip"]:
            return fail("TipNotAttachedError", "no tip")
        elif command_type in ("dropTipInPlace", "dropTip"):
            pipette["tip"], pipette["volume"] = False, 0.0
        if "labwareId" in p and p["labwareId"] not in self.labware:
            return fail("LabwareNotLoadedError", p["labwareId"])
        if command_type == "aspirate":
            pipette["volume"] += p["volume"]
        elif command_type == "dispense":
            pipette["volume"] = max(0.0, pipette["volume"] - p["volume"])
            pipette["ready"] = not flex  # a Flex dispense pushes out, leaving the plunger not ready
        elif command_type == "blowout":
            pipette["volume"], pipette["ready"] = 0.0, False
        elif command_type == "moveToWell":
            z = WELL_TOP_Z + p.get("wellLocation", {}).get("offset", {}).get("z", 0)
            return {"status": "succeeded", "result": {"position": {"x": 0.0, "y": 0.0, "z": z}}}
        elif command_type == "tryLiquidProbe":
            if not flex:
                return fail("UnsupportedHardwareCommand", "liquid probing needs a Flex pipette")
            if pipette["volume"] > 0:
                return fail("TipNotEmptyError", "probe with an empty tip")
            if not pipette["ready"]:
                return fail("PipetteNotReadyToAspirateError", "call prepareToAspirate first")
            height = self.liquid_heights.get(p["wellName"], 5.0)
            z = None if height is None else WELL_TOP_Z - WELL_DEPTH + height
            return {"status": "succeeded", "result": {"position": {"x": 0.0, "y": 0.0, "z": WELL_TOP_Z}, "z_position": z}}
        return {"status": "succeeded", "result": {}}


def flex_pipette(mount: str, name: str, channels: int, min_ul: float, max_ul: float) -> dict[str, Any]:
    return {"mount": mount, "instrumentType": "pipette", "instrumentName": name, "instrumentModel": f"{name}_v3.6",
            "serialNumber": f"{name.upper()}-1", "ok": True, "data": {"channels": channels, "min_volume": min_ul, "max_volume": max_ul}}


GRIPPER = {"mount": "extension", "instrumentType": "gripper", "instrumentModel": "gripperV1.3", "serialNumber": "GRPV13",
           "ok": True, "data": {"jawState": "homed"}}
