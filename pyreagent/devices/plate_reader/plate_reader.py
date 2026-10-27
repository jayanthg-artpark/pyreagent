"""Plate reader capability interface.

Absorbance, fluorescence, luminescence, incubation and shaking are each optional:
the driver's ``ReaderSpec`` says what its instrument can do. Capabilities it cannot
do are hidden from ``describe()`` and refused, and every argument is checked
against the spec before the instrument is touched.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from pydantic import Field

from ...core.capability import Within, capability
from ...core.device import Device
from ...core.errors import DeviceTimeout, InvalidArgument, NeedsAttention
from ...core.jobs import utcnow
from ...labware import DEFAULT_CATALOG, Labware, LabwareCatalog
from .backend import FluorescenceSettings, PlateReaderBackend
from .models import (
    AbsorbanceRead,
    AbsorbanceSpectrum,
    FluorescenceRead,
    LuminescenceRead,
    Optics,
    PlateReaderStatus,
    ReaderSpec,
    ShakeMode,
)

Plate = Annotated[str, Field(description="Labware id of the plate in the reader, from limits.plates (or GET /labware).")]
Wells = Annotated[list[str] | None, Field(description="Wells to read, e.g. ['A1', 'B3']. Omit to read every well of the plate.")]
FocalHeight = Annotated[
    float | None, Field(ge=0, le=30, description="Read height above the plate bottom, mm, within limits; omit for the instrument default.")
]

READS = ("read_absorbance", "read_absorbance_spectrum", "read_fluorescence", "read_luminescence")


class PlateReader(Device[PlateReaderBackend]):
    """Microplate reader."""

    device_type = "plate_reader"

    def __init__(
        self,
        name: str,
        backend: PlateReaderBackend,
        *,
        labware: LabwareCatalog | None = None,
        poll_interval_s: float = 2.0,
    ) -> None:
        super().__init__(name, backend)
        self.labware = labware or DEFAULT_CATALOG
        self.poll_interval_s = poll_interval_s

    # -- what this instrument can do -----------------------------------------------------------------

    @property
    def reader(self) -> ReaderSpec:
        return self.backend.spec

    def supports(self, capability_name: str) -> bool:
        spec = self.reader
        needs = {
            "read_absorbance": spec.absorbance is not None,
            "read_absorbance_spectrum": spec.absorbance is not None and spec.absorbance.spectrum,
            "read_fluorescence": spec.fluorescence is not None,
            "read_luminescence": spec.luminescence is not None,
            "set_temperature": spec.temperature_c is not None,
            "wait_for_temperature": spec.temperature_c is not None,
            "deactivate_temperature": spec.temperature_c is not None,
            "shake": spec.shaking is not None,
            "open_tray": spec.tray,
            "close_tray": spec.tray,
        }
        return needs.get(capability_name, True)

    def _fits(self, labware: Labware) -> bool:
        """Whether this reader can read ``labware`` (format and, if it needs them, well positions)."""
        spec = self.reader
        if spec.plate_formats is not None and (labware.rows, labware.columns) not in [tuple(f) for f in spec.plate_formats]:
            return False
        return not (spec.needs_geometry and labware.geometry is None)

    def limits(self) -> dict[str, Any]:
        spec = self.reader
        limits: dict[str, Any] = {
            "features": [name.removeprefix("read_") for name in READS if self.supports(name)]
                        + [f for f, ok in (("temperature", spec.temperature_c), ("shaking", spec.shaking), ("tray", spec.tray)) if ok],
            "plates": [lw.id for lw in self.labware.list(kind="plate") if self._fits(lw)],
        }
        if spec.plate_formats is not None:
            limits["plate_formats"] = [f"{rows}x{columns}" for rows, columns in spec.plate_formats]
        if (a := spec.absorbance) is not None:
            limits |= {"min_wavelength_nm": a.min_nm, "max_wavelength_nm": a.max_nm}
            if a.fixed_nm:
                limits["absorbance_wavelengths_nm"] = list(a.fixed_nm)
            if a.spectrum:
                limits["max_spectrum_points"] = a.max_spectrum_points
        if (f := spec.fluorescence) is not None:
            limits |= {"min_excitation_nm": f.min_excitation_nm, "max_excitation_nm": f.max_excitation_nm,
                       "min_emission_nm": f.min_emission_nm, "max_emission_nm": f.max_emission_nm,
                       "min_stokes_shift_nm": f.min_stokes_shift_nm, "optics": list(f.optics),
                       "min_gain": f.min_gain, "max_gain": f.max_gain}
            if f.focal_height_mm:
                limits |= {"min_focal_height_mm": f.focal_height_mm[0], "max_focal_height_mm": f.focal_height_mm[1]}
            if f.max_flashes:
                limits["max_flashes"] = f.max_flashes
        if (lum := spec.luminescence) is not None:
            limits |= {"min_integration_s": lum.min_integration_s, "max_integration_s": lum.max_integration_s}
        if spec.temperature_c is not None:
            limits |= {"min_temperature_c": spec.temperature_c[0], "max_temperature_c": spec.temperature_c[1]}
        if (s := spec.shaking) is not None:
            limits |= {"shake_modes": list(s.modes), "min_rpm": s.min_rpm, "max_rpm": s.max_rpm, "max_shake_s": s.max_duration_s}
        return limits

    # -- checks before anything moves ---------------------------------------------------------------

    def validate_arguments(self, capability_name: str, values: dict[str, Any]) -> None:
        spec = self.reader
        if capability_name in READS:
            labware = self.labware.get(values["plate"], kind="plate")
            formats = [tuple(f) for f in spec.plate_formats or []]
            if spec.plate_formats is not None and (labware.rows, labware.columns) not in formats:
                raise InvalidArgument(f"{labware.id} ({labware.rows}x{labware.columns}) is not a plate format this reader takes.",
                                      details={"plate_formats": [f"{r}x{c}" for r, c in formats]})
            if spec.needs_geometry:
                labware.require_geometry()
            if values.get("wells"):
                labware.check_wells(values["wells"])
        if capability_name == "read_absorbance" and spec.absorbance and spec.absorbance.fixed_nm:
            if values["wavelength_nm"] not in spec.absorbance.fixed_nm:
                raise InvalidArgument(f"This reader has filters for {spec.absorbance.fixed_nm} nm only.",
                                      details={"absorbance_wavelengths_nm": spec.absorbance.fixed_nm})
        if capability_name == "read_absorbance_spectrum":
            self._spectrum_wavelengths(values["start_nm"], values["end_nm"], values["step_nm"])
        if capability_name == "read_fluorescence":
            self._check_fluorescence(values)
        if capability_name == "read_luminescence":
            lum = spec.luminescence
            self._check_focal_height(values.get("focal_height_mm"), lum.focal_height_mm if lum else None)
        if capability_name == "shake":
            self._check_shake(values)

    def _spectrum_wavelengths(self, start_nm: int, end_nm: int, step_nm: int) -> list[int]:
        absorbance = self.reader.absorbance
        assert absorbance is not None
        if end_nm <= start_nm:
            raise InvalidArgument("end_nm must be above start_nm.")
        wavelengths = list(range(start_nm, end_nm + 1, step_nm))
        if absorbance.max_spectrum_points and len(wavelengths) > absorbance.max_spectrum_points:
            raise InvalidArgument(f"{len(wavelengths)} wavelengths; this reader scans at most {absorbance.max_spectrum_points}.",
                                  suggested_action="Use a larger step_nm or a narrower range.")
        return wavelengths

    def _check_fluorescence(self, values: dict[str, Any]) -> None:
        f = self.reader.fluorescence
        assert f is not None
        if values["emission_nm"] < values["excitation_nm"] + f.min_stokes_shift_nm:
            raise InvalidArgument(f"emission_nm must be at least {f.min_stokes_shift_nm} nm above excitation_nm on this reader.")
        if values["optics"] not in f.optics:
            raise InvalidArgument(f"This reader reads fluorescence from the {' or '.join(f.optics)} only.")
        if values.get("gain") is not None and f.max_gain is None:
            raise InvalidArgument("This reader sets its gain automatically; omit gain.")
        if values.get("flashes") is not None and f.max_flashes is not None and values["flashes"] > f.max_flashes:
            raise InvalidArgument(f"flashes must be at most {f.max_flashes} on this reader.")
        self._check_focal_height(values.get("focal_height_mm"), f.focal_height_mm)

    @staticmethod
    def _check_focal_height(height: float | None, allowed: tuple[float, float] | None) -> None:
        if height is None:
            return
        if allowed is None:
            raise InvalidArgument("This reader's read height is fixed; omit focal_height_mm.")
        if not allowed[0] <= height <= allowed[1]:
            raise InvalidArgument(f"focal_height_mm must be within {allowed[0]:g} .. {allowed[1]:g} mm.")

    def _check_shake(self, values: dict[str, Any]) -> None:
        s = self.reader.shaking
        assert s is not None
        if values["mode"] not in s.modes:
            raise InvalidArgument(f"This reader shakes {', '.join(s.modes)} only.", details={"shake_modes": s.modes})
        if values.get("rpm") is not None and s.min_rpm is None:
            raise InvalidArgument("This reader's shake speed is not settable; omit rpm.")
        if values["duration_s"] > s.max_duration_s:
            raise InvalidArgument(f"duration_s must be at most {s.max_duration_s:g} on this reader.")

    # -- helpers ------------------------------------------------------------------------------------

    async def _prepare(self, plate: str, wells: list[str] | None) -> tuple[Labware, list[str]]:
        labware = self.labware.get(plate)
        targets = labware.check_wells(wells) if wells else labware.wells()
        if self.reader.tray and await self.backend.tray_is_open():
            raise NeedsAttention("The tray is open.", recoverable=True,
                                 suggested_action="Make sure the plate is seated, call close_tray, then retry.")
        return labware, targets

    def _progress(self, count: int) -> Any:
        return lambda f: self.report_progress(f, f"{round(f * count)}/{count} wells")

    async def on_cancel(self, capability_name: str) -> None:
        if capability_name in READS:
            await self.backend.abort_read()
        elif capability_name == "shake":
            await self.backend.stop_shaking()

    # -- measurements -------------------------------------------------------------------------------

    @capability(long_running=True)
    async def read_absorbance(
        self,
        plate: Plate,
        wavelength_nm: Annotated[
            int,
            Field(ge=200, le=1000, description="Measurement wavelength in nm, within limits.min_wavelength_nm .. limits.max_wavelength_nm."),
            Within("min_wavelength_nm", "max_wavelength_nm"),
        ],
        wells: Wells = None,
    ) -> AbsorbanceRead:
        """Measure absorbance (optical density) of a plate at one wavelength.

        The plate must already be in the reader with the tray closed. Runs as a
        job; poll it for progress and the per-well values.
        """
        labware, targets = await self._prepare(plate, wells)
        values = await self.backend.read_absorbance(plate=labware, wavelength_nm=wavelength_nm, wells=targets,
                                                    progress=self._progress(len(targets)))
        return AbsorbanceRead(plate=plate, wavelength_nm=wavelength_nm, values=values, read_at=utcnow())

    @capability(long_running=True)
    async def read_absorbance_spectrum(
        self,
        plate: Plate,
        start_nm: Annotated[int, Field(ge=200, le=1000, description="First wavelength, nm."), Within("min_wavelength_nm", "max_wavelength_nm")],
        end_nm: Annotated[int, Field(ge=200, le=1000, description="Last wavelength, nm (included)."), Within("min_wavelength_nm", "max_wavelength_nm")],
        step_nm: Annotated[int, Field(ge=1, le=100, description="Spacing between wavelengths, nm.")] = 10,
        wells: Wells = None,
    ) -> AbsorbanceSpectrum:
        """Measure an absorbance spectrum: OD of each well at every wavelength from start_nm to end_nm.

        The tray must be closed. At most limits.max_spectrum_points wavelengths. Runs as a job.
        """
        labware, targets = await self._prepare(plate, wells)
        wavelengths = self._spectrum_wavelengths(start_nm, end_nm, step_nm)
        values = await self.backend.read_absorbance_spectrum(plate=labware, wavelengths_nm=wavelengths, wells=targets,
                                                             progress=self._progress(len(targets)))
        return AbsorbanceSpectrum(plate=plate, wavelengths_nm=wavelengths, values=values, read_at=utcnow())

    @capability(long_running=True)
    async def read_fluorescence(
        self,
        plate: Plate,
        excitation_nm: Annotated[int, Field(ge=200, le=1000, description="Excitation wavelength, nm."), Within("min_excitation_nm", "max_excitation_nm")],
        emission_nm: Annotated[
            int, Field(ge=200, le=1000, description="Emission wavelength, nm; above excitation by at least limits.min_stokes_shift_nm."),
            Within("min_emission_nm", "max_emission_nm"),
        ],
        wells: Wells = None,
        optics: Annotated[Optics, Field(description="Read from above the plate (top) or through its bottom; see limits.optics.")] = "top",
        gain: Annotated[
            int | None, Field(ge=0, description="Detector gain within limits.min_gain .. max_gain; omit for the instrument's automatic/default gain."),
            Within("min_gain", "max_gain"),
        ] = None,
        focal_height_mm: FocalHeight = None,
        flashes: Annotated[int | None, Field(ge=1, description="Light flashes per well; omit for the default.")] = None,
    ) -> FluorescenceRead:
        """Measure fluorescence intensity (RFU) of a plate at one excitation/emission pair.

        RFU are only comparable between reads made at the same gain; the result reports
        the gain used. The tray must be closed. Runs as a job.
        """
        labware, targets = await self._prepare(plate, wells)
        settings = FluorescenceSettings(excitation_nm=excitation_nm, emission_nm=emission_nm, optics=optics, gain=gain,
                                        focal_height_mm=focal_height_mm, flashes=flashes)
        values, used_gain = await self.backend.read_fluorescence(plate=labware, settings=settings, wells=targets,
                                                                 progress=self._progress(len(targets)))
        return FluorescenceRead(plate=plate, excitation_nm=excitation_nm, emission_nm=emission_nm, optics=optics,
                                gain=used_gain, values=values, read_at=utcnow())

    @capability(long_running=True)
    async def read_luminescence(
        self,
        plate: Plate,
        wells: Wells = None,
        integration_s: Annotated[
            float, Field(gt=0, le=60, description="Light collection time per well, s, within limits."),
            Within("min_integration_s", "max_integration_s"),
        ] = 1.0,
        focal_height_mm: FocalHeight = None,
    ) -> LuminescenceRead:
        """Measure luminescence (RLU) of a plate. The tray must be closed. Runs as a job."""
        labware, targets = await self._prepare(plate, wells)
        values = await self.backend.read_luminescence(plate=labware, integration_s=integration_s, focal_height_mm=focal_height_mm,
                                                      wells=targets, progress=self._progress(len(targets)))
        return LuminescenceRead(plate=plate, integration_s=integration_s, values=values, read_at=utcnow())

    # -- incubation and shaking ---------------------------------------------------------------------

    @capability
    async def set_temperature(
        self,
        celsius: Annotated[float, Field(ge=0, le=100, description="Chamber set point, °C, within limits."),
                           Within("min_temperature_c", "max_temperature_c")],
    ) -> PlateReaderStatus:
        """Start heating the reading chamber to a set point and return at once. Use wait_for_temperature to wait."""
        await self.backend.set_temperature(celsius)
        return await self.status()

    @capability(long_running=True)
    async def wait_for_temperature(
        self,
        tolerance_c: Annotated[float, Field(gt=0, le=10, description="Done when within this many °C of the set point.")] = 0.5,
        timeout_s: Annotated[float, Field(gt=0, le=4 * 3600, description="Give up with a 'timeout' error after this long.")] = 1800,
    ) -> PlateReaderStatus:
        """Wait until the chamber reaches its set point. Runs as a job."""
        current, target = await self.backend.get_temperature()
        if target is None:
            raise InvalidArgument("Temperature control is off; call set_temperature first.")
        if current is None:
            raise NeedsAttention("This reader does not report its temperature.", recoverable=True)
        start_gap = abs(current - target) or tolerance_c
        deadline = asyncio.get_running_loop().time() + timeout_s
        while abs(current - target) > tolerance_c:
            if asyncio.get_running_loop().time() > deadline:
                raise DeviceTimeout(f"Still at {current:.1f} °C after {timeout_s:g}s (set point {target:g} °C).",
                                    suggested_action="Check the reader is heating, or allow more time.")
            self.report_progress(max(0.0, 1 - abs(current - target) / start_gap), f"{current:.1f} °C -> {target:g} °C")
            await asyncio.sleep(self.poll_interval_s)
            current, now_target = await self.backend.get_temperature()
            if now_target != target or current is None:
                raise InvalidArgument("The set point changed while waiting.", recoverable=True)
        return await self.status()

    @capability(interrupt=True)
    async def deactivate_temperature(self) -> PlateReaderStatus:
        """Turn incubation off; the chamber drifts back to room temperature."""
        await self.backend.deactivate_temperature()
        return await self.status()

    @capability(long_running=True)
    async def shake(
        self,
        duration_s: Annotated[float, Field(gt=0, le=3600, description="How long to shake, s; at most limits.max_shake_s.")],
        mode: Annotated[ShakeMode, Field(description="Shake pattern, one of limits.shake_modes.")] = "orbital",
        rpm: Annotated[int | None, Field(gt=0, description="Speed within limits.min_rpm .. max_rpm; omit for the default."),
                       Within("min_rpm", "max_rpm")] = None,
    ) -> PlateReaderStatus:
        """Shake the plate in the reader, then stop. The tray must be closed. Runs as a job; cancelling stops it."""
        if self.reader.tray and await self.backend.tray_is_open():
            raise NeedsAttention("The tray is open.", recoverable=True, suggested_action="Call close_tray, then retry.")
        self.report_progress(None, f"shaking {mode} for {duration_s:g}s")
        await self.backend.shake(mode=mode, rpm=rpm, duration_s=duration_s)
        return await self.status()

    # -- tray and status ----------------------------------------------------------------------------

    @capability
    async def open_tray(self) -> PlateReaderStatus:
        """Open the plate tray so a plate can be loaded or removed."""
        await self.backend.open_tray()
        return await self.status()

    @capability
    async def close_tray(self) -> PlateReaderStatus:
        """Close the plate tray. Required before reading."""
        await self.backend.close_tray()
        return await self.status()

    @capability(read_only=True)
    async def status(self) -> PlateReaderStatus:
        """Whether the tray is open, and the chamber temperature and set point if the reader has incubation."""
        current, target = await self.backend.get_temperature()
        return PlateReaderStatus(tray_open=await self.backend.tray_is_open() if self.reader.tray else None,
                                 temperature_c=current, target_temperature_c=target)
