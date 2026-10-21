"""The ``@capability`` decorator and the specs built from it.

A capability is an async method on a ``Device`` subclass. Its signature is the
contract: parameters are typed with ``Annotated[T, Field(description=..., ge=..., le=...)]``
and its docstring is what an agent reads. From that one definition pyreagent
builds the argument validator, the HTTP route, the OpenAPI schema and the MCP tool.
"""

from __future__ import annotations

import functools
import inspect
import re
import typing
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, create_model

from .errors import InvalidArgument

# Names the MCP and HTTP layers add next to a capability's own parameters.
RESERVED_PARAMETERS = frozenset({"confirmation_id", "idempotency_key"})
# Sub-paths of /devices/{name}/ used by the server itself.
RESERVED_CAPABILITY_NAMES = frozenset({"reservation", "connect", "disconnect"})


@dataclass(frozen=True)
class Within:
    """Bound a parameter by this device instance's limits.

    ``low`` / ``high`` name keys of ``Device.limits()``. Use inside ``Annotated``::

        volume_ml: Annotated[float, Field(gt=0, description=...), Within(high="max_volume_ml")]

    Checked before a confirmation is requested, so a human is never asked to
    approve a call that would be refused anyway.
    """

    low: str | None = None
    high: str | None = None


@dataclass(frozen=True)
class CapabilityMeta:
    name: str
    description: str
    long_running: bool = False
    """Runs as a background job; callers get a job_id and poll it."""
    requires_confirmation: bool = False
    """A human must approve each call (door open, heater on, reagent movement...)."""
    hazard: str | None = None
    """Shown to the human who approves the call."""
    read_only: bool = False
    """Only queries the instrument; allowed while the device is busy, faulted or reserved."""
    interrupt: bool = False
    """Safety action (stop, deactivate) that bypasses the mutex and reservations."""
    allow_in_error: bool = False
    """May run while the device is in ERROR (used by reset)."""


def capability(
    fn: Callable[..., Any] | None = None,
    /,
    *,
    long_running: bool = False,
    requires_confirmation: bool = False,
    hazard: str | None = None,
    read_only: bool = False,
    interrupt: bool = False,
    allow_in_error: bool = False,
) -> Any:
    """Mark an async ``Device`` method as an agent-facing capability."""

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(f"capability {func.__qualname__} must be an async def")
        if read_only and (requires_confirmation or long_running):
            raise TypeError(f"capability {func.__qualname__}: read_only cannot be combined with long_running or requires_confirmation")
        description = inspect.getdoc(func) or ""
        if not description:
            raise TypeError(f"capability {func.__qualname__} needs a docstring; agents read it to decide when to call it")
        meta = CapabilityMeta(
            name=func.__name__,
            description=description,
            long_running=long_running,
            requires_confirmation=requires_confirmation,
            hazard=hazard,
            read_only=read_only,
            interrupt=interrupt,
            allow_in_error=allow_in_error,
        )

        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            spec = type(self).capability_specs()[meta.name]
            return await self._dispatch(spec, spec.bind(args, kwargs))

        wrapper.__capability__ = meta  # type: ignore[attr-defined]
        return wrapper

    return decorate(fn) if fn is not None else decorate


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


@dataclass
class CapabilitySpec:
    meta: CapabilityMeta
    func: Callable[..., Any]
    """The undecorated method."""
    args_model: type[BaseModel]
    returns: TypeAdapter[Any] | None
    _signature: inspect.Signature = field(repr=False)
    bounds: dict[str, Within] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.meta.name

    @property
    def summary(self) -> str:
        return self.meta.description.split("\n\n", 1)[0].replace("\n", " ")

    def validate(self, args: dict[str, Any] | None) -> BaseModel:
        try:
            return self.args_model.model_validate(args or {})
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}" for err in exc.errors()
            )
            raise InvalidArgument(
                f"Invalid arguments for {self.name}: {problems}",
                details={"errors": exc.errors(include_url=False, include_context=False, include_input=False)},
            ) from None

    def bind(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> BaseModel:
        """Validate a direct Python call (positional args allowed)."""
        try:
            bound = self._signature.bind(None, *args, **kwargs)
        except TypeError as exc:
            raise InvalidArgument(f"Invalid arguments for {self.name}: {exc}") from None
        arguments = dict(bound.arguments)
        arguments.pop(next(iter(self._signature.parameters)))
        return self.validate(arguments)

    def values(self, validated: BaseModel) -> dict[str, Any]:
        return {name: getattr(validated, name) for name in type(validated).model_fields}

    def check_bounds(self, values: dict[str, Any], limits: dict[str, Any]) -> None:
        for name, within in self.bounds.items():
            value = values.get(name)
            if value is None:
                continue
            low = limits.get(within.low) if within.low else None
            high = limits.get(within.high) if within.high else None
            if (low is not None and value < low) or (high is not None and value > high):
                allowed = f"{low if low is not None else '-inf'} .. {high if high is not None else 'inf'}"
                raise InvalidArgument(
                    f"{name}={value} is outside this device's allowed range {allowed}.",
                    details={"parameter": name, "min": low, "max": high},
                )

    def dump_result(self, result: Any) -> Any:
        if self.returns is None:
            return None
        return self.returns.dump_python(result, mode="json")

    def parameters_schema(self) -> dict[str, Any]:
        return self.args_model.model_json_schema()

    def returns_schema(self) -> dict[str, Any] | None:
        if self.returns is None:
            return None
        try:
            return self.returns.json_schema()
        except Exception:
            return None

    def describe(self, *, requires_confirmation: bool) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "description": self.meta.description,
            "long_running": self.meta.long_running,
            "requires_confirmation": requires_confirmation,
            "hazard": self.meta.hazard,
            "read_only": self.meta.read_only,
            "interrupt": self.meta.interrupt,
            "parameters": self.parameters_schema(),
            "returns": self.returns_schema(),
        }


def build_spec(owner: type, wrapper: Callable[..., Any]) -> CapabilitySpec:
    meta: CapabilityMeta = wrapper.__capability__  # type: ignore[attr-defined]
    func = inspect.unwrap(wrapper)
    signature = inspect.signature(func)
    hints = typing.get_type_hints(func, include_extras=True)
    fields: dict[str, Any] = {}
    bounds: dict[str, Within] = {}
    for index, (pname, param) in enumerate(signature.parameters.items()):
        if index == 0:
            continue  # self
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            raise TypeError(f"capability {func.__qualname__}: *args/**kwargs are not allowed")
        if pname in RESERVED_PARAMETERS:
            raise TypeError(f"capability {func.__qualname__}: parameter name {pname!r} is reserved")
        annotation = hints.get(pname, Any)
        for marker in getattr(annotation, "__metadata__", ()):
            if isinstance(marker, Within):
                bounds[pname] = marker
        default = ... if param.default is param.empty else param.default
        fields[pname] = (annotation, default)
    args_model = create_model(  # type: ignore[call-overload]
        f"{owner.__name__}{_camel(meta.name)}Args",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    return_type = hints.get("return", None)
    returns = None if return_type in (None, type(None)) else TypeAdapter(return_type)
    return CapabilitySpec(meta=meta, func=func, args_model=args_model, returns=returns, _signature=signature, bounds=bounds)


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


def collect_capabilities(cls: type) -> dict[str, CapabilitySpec]:
    specs: dict[str, CapabilitySpec] = {}
    for attr in dir(cls):
        member = getattr(cls, attr, None)
        if callable(member) and hasattr(member, "__capability__"):
            if not _NAME_RE.match(attr):
                raise TypeError(f"capability name {attr!r} must be snake_case")
            if attr in RESERVED_CAPABILITY_NAMES:
                raise TypeError(f"capability name {attr!r} is reserved by the server")
            specs[attr] = build_spec(cls, member)
    return specs
