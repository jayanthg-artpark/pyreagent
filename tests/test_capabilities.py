"""Capability definitions are the agent's documentation, so lint them."""

from __future__ import annotations

import pytest

from pyreagent.core import Device, InvalidArgument, capability
from pyreagent.devices import DEVICE_TYPES, device_class


@pytest.mark.parametrize("device_type", sorted(DEVICE_TYPES))
def test_every_capability_is_documented(device_type):
    cls = device_class(device_type)
    specs = cls.capability_specs()
    assert "reset" in specs
    for name, spec in specs.items():
        assert spec.meta.description.strip(), f"{device_type}.{name} has no docstring"
        schema = spec.parameters_schema()
        for pname, prop in schema.get("properties", {}).items():
            assert prop.get("description"), f"{device_type}.{name}({pname}) has no Field(description=...)"
        if spec.meta.requires_confirmation:
            assert spec.meta.hazard, f"{device_type}.{name} is gated but has no hazard text for the human"


def test_validation_rejects_bad_and_extra_arguments():
    spec = device_class("pump").capability_specs()["dispense"]
    with pytest.raises(InvalidArgument) as err:
        spec.validate({"volume_ml": -1, "rate_ml_min": 1})
    assert err.value.details["errors"][0]["loc"] == ("volume_ml",)
    with pytest.raises(InvalidArgument):
        spec.validate({"volume_ml": 1, "rate_ml_min": 1, "speed": "fast"})
    assert spec.validate({"volume_ml": "1.5", "rate_ml_min": 2}).volume_ml == 1.5


def test_capabilities_must_be_async_and_documented():
    with pytest.raises(TypeError, match="async"):
        class A(Device):  # noqa: F841
            @capability
            def sync(self) -> None:
                """Docs."""

    with pytest.raises(TypeError, match="docstring"):
        class B(Device):  # noqa: F841
            @capability
            async def undocumented(self) -> None:
                pass
