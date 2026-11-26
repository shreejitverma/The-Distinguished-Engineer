"""Lab: grpc.health.v1.Health, server reflection, and the drain choreography on grpc.aio 1.85.1.

`grpcio-health-checking` and `grpcio-reflection` are not pinned in this workspace, so the lab
rebuilds the protocol from first principles:

1. `build_schema` assembles `FileDescriptorProto` objects for `grpc/health/v1/health.proto`
   and `grpc/reflection/v1/reflection.proto` (field numbers copied from grpc-proto master),
   plus a tiny `lab.work.v1.Work` service and an unregistered `lab.internal.v1.Admin` service.
2. `HealthServicer` follows the semantics of `grpc_health.v1._async.HealthServicer` (Check,
   Watch, `enter_graceful_shutdown`) and grpc-go `health.Server` (`List`, `Resume`, the
   one-slot coalescing Watch queue).
3. `ReflectionServicer` answers `list_services`, `file_by_filename`, and
   `file_containing_symbol` the way `grpc_reflection.v1alpha._base` does, plus an optional
   allowlist that the reference implementation lacks.
4. `LabServer` and `drain_and_stop` run the sequence a production process follows on SIGTERM.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUnknownArgumentType=false, reportMissingTypeArgument=false, reportAttributeAccessIssue=false

from __future__ import annotations

import asyncio
import functools
import importlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import grpc  # type: ignore[import-untyped]
from grpc import aio

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

_pb: Any = importlib.import_module("google.protobuf.descriptor_pb2")
_pool_module: Any = importlib.import_module("google.protobuf.descriptor_pool")
_factory: Any = importlib.import_module("google.protobuf.message_factory")
_F: Any = _pb.FieldDescriptorProto

HEALTH_SERVICE: Final = "grpc.health.v1.Health"
REFLECTION_SERVICE: Final = "grpc.reflection.v1.ServerReflection"
WORK_SERVICE: Final = "lab.work.v1.Work"
ADMIN_SERVICE: Final = "lab.internal.v1.Admin"

UNKNOWN: Final = 0
SERVING: Final = 1
NOT_SERVING: Final = 2
SERVICE_UNKNOWN: Final = 3
STATUS_NAMES: Final = {0: "UNKNOWN", 1: "SERVING", 2: "NOT_SERVING", 3: "SERVICE_UNKNOWN"}

MAX_LIST_SERVICES: Final = 100  # grpc-go health/server.go maxAllowedServices

# --- layer 1: descriptors built by hand -----------------------------------------------------


def _field(
    name: str, number: int, kind: int, *, type_name: str = "", repeated: bool = False
) -> Any:
    label = _F.LABEL_REPEATED if repeated else _F.LABEL_OPTIONAL
    proto = _F(name=name, number=number, type=kind, label=label)
    if type_name:
        proto.type_name = type_name
    return proto


def _health_file() -> Any:
    fd = _pb.FileDescriptorProto(
        name="grpc/health/v1/health.proto", package="grpc.health.v1", syntax="proto3"
    )
    request = fd.message_type.add(name="HealthCheckRequest")
    request.field.append(_field("service", 1, _F.TYPE_STRING))
    response = fd.message_type.add(name="HealthCheckResponse")
    enum = response.enum_type.add(name="ServingStatus")
    for number, label in STATUS_NAMES.items():
        enum.value.add(name=label, number=number)
    response.field.append(
        _field(
            "status",
            1,
            _F.TYPE_ENUM,
            type_name=".grpc.health.v1.HealthCheckResponse.ServingStatus",
        )
    )
    fd.message_type.add(name="HealthListRequest")
    listing = fd.message_type.add(name="HealthListResponse")
    entry = listing.nested_type.add(name="StatusesEntry")
    entry.options.map_entry = True
    entry.field.append(_field("key", 1, _F.TYPE_STRING))
    entry.field.append(
        _field("value", 2, _F.TYPE_MESSAGE, type_name=".grpc.health.v1.HealthCheckResponse")
    )
    listing.field.append(
        _field(
            "statuses",
            1,
            _F.TYPE_MESSAGE,
            type_name=".grpc.health.v1.HealthListResponse.StatusesEntry",
            repeated=True,
        )
    )
    service = fd.service.add(name="Health")
    service.method.add(
        name="Check",
        input_type=".grpc.health.v1.HealthCheckRequest",
        output_type=".grpc.health.v1.HealthCheckResponse",
    )
    service.method.add(
        name="List",
        input_type=".grpc.health.v1.HealthListRequest",
        output_type=".grpc.health.v1.HealthListResponse",
    )
    service.method.add(
        name="Watch",
        input_type=".grpc.health.v1.HealthCheckRequest",
        output_type=".grpc.health.v1.HealthCheckResponse",
        server_streaming=True,
    )
    return fd


def _reflection_file() -> Any:
    package = ".grpc.reflection.v1."
    fd = _pb.FileDescriptorProto(
        name="grpc/reflection/v1/reflection.proto", package="grpc.reflection.v1", syntax="proto3"
    )
    request = fd.message_type.add(name="ServerReflectionRequest")
    request.oneof_decl.add(name="message_request")
    request.field.append(_field("host", 1, _F.TYPE_STRING))
    for name, number in (
        ("file_by_filename", 3),
        ("file_containing_symbol", 4),
        ("all_extension_numbers_of_type", 6),
        ("list_services", 7),
    ):
        member = _field(name, number, _F.TYPE_STRING)
        member.oneof_index = 0
        request.field.append(member)
    extension = _field(
        "file_containing_extension", 5, _F.TYPE_MESSAGE, type_name=package + "ExtensionRequest"
    )
    extension.oneof_index = 0
    request.field.append(extension)

    ext_request = fd.message_type.add(name="ExtensionRequest")
    ext_request.field.append(_field("containing_type", 1, _F.TYPE_STRING))
    ext_request.field.append(_field("extension_number", 2, _F.TYPE_INT32))

    response = fd.message_type.add(name="ServerReflectionResponse")
    response.oneof_decl.add(name="message_response")
    response.field.append(_field("valid_host", 1, _F.TYPE_STRING))
    response.field.append(
        _field(
            "original_request", 2, _F.TYPE_MESSAGE, type_name=package + "ServerReflectionRequest"
        )
    )
    for name, number, message in (
        ("file_descriptor_response", 4, "FileDescriptorResponse"),
        ("all_extension_numbers_response", 5, "ExtensionNumberResponse"),
        ("list_services_response", 6, "ListServiceResponse"),
        ("error_response", 7, "ErrorResponse"),
    ):
        member = _field(name, number, _F.TYPE_MESSAGE, type_name=package + message)
        member.oneof_index = 0
        response.field.append(member)

    file_response = fd.message_type.add(name="FileDescriptorResponse")
    file_response.field.append(_field("file_descriptor_proto", 1, _F.TYPE_BYTES, repeated=True))
    numbers = fd.message_type.add(name="ExtensionNumberResponse")
    numbers.field.append(_field("base_type_name", 1, _F.TYPE_STRING))
    numbers.field.append(_field("extension_number", 2, _F.TYPE_INT32, repeated=True))
    listing = fd.message_type.add(name="ListServiceResponse")
    listing.field.append(
        _field("service", 1, _F.TYPE_MESSAGE, type_name=package + "ServiceResponse", repeated=True)
    )
    fd.message_type.add(name="ServiceResponse").field.append(_field("name", 1, _F.TYPE_STRING))
    error = fd.message_type.add(name="ErrorResponse")
    error.field.append(_field("error_code", 1, _F.TYPE_INT32))
    error.field.append(_field("error_message", 2, _F.TYPE_STRING))

    service = fd.service.add(name="ServerReflection")
    service.method.add(
        name="ServerReflectionInfo",
        input_type=package + "ServerReflectionRequest",
        output_type=package + "ServerReflectionResponse",
        client_streaming=True,
        server_streaming=True,
    )
    return fd


def _work_files() -> tuple[Any, Any]:
    types = _pb.FileDescriptorProto(
        name="lab/work/v1/types.proto", package="lab.work.v1", syntax="proto3"
    )
    types.message_type.add(name="WorkRequest").field.append(_field("text", 1, _F.TYPE_STRING))
    types.message_type.add(name="WorkReply").field.append(_field("text", 1, _F.TYPE_STRING))
    work = _pb.FileDescriptorProto(
        name="lab/work/v1/work.proto",
        package="lab.work.v1",
        syntax="proto3",
        dependency=["lab/work/v1/types.proto"],
    )
    work.service.add(name="Work").method.add(
        name="Do", input_type=".lab.work.v1.WorkRequest", output_type=".lab.work.v1.WorkReply"
    )
    return types, work


def _admin_file() -> Any:
    fd = _pb.FileDescriptorProto(
        name="lab/internal/v1/admin.proto",
        package="lab.internal.v1",
        syntax="proto3",
        dependency=["lab/work/v1/types.proto"],
    )
    fd.service.add(name="Admin").method.add(
        name="Purge", input_type=".lab.work.v1.WorkRequest", output_type=".lab.work.v1.WorkReply"
    )
    return fd


@dataclass(frozen=True)
class Schema:
    """A private descriptor pool plus message classes, so the lab never touches the default pool."""

    pool: Any

    def message(self, full_name: str) -> Any:
        return _factory.GetMessageClass(self.pool.FindMessageTypeByName(full_name))


@functools.cache
def build_schema() -> Schema:
    """Health, reflection, a two-file `Work` service, and an `Admin` service nobody registers."""
    pool = _pool_module.DescriptorPool()
    types, work = _work_files()
    for file_proto in (
        _health_file(),
        _reflection_file(),
        types,
        work,
        _admin_file(),
    ):
        pool.AddSerializedFile(file_proto.SerializeToString())
    return Schema(pool)


# --- layer 2: the Health service ------------------------------------------------------------


class _Watcher:
    """One Watch stream's one-slot mailbox: a newer status overwrites an unsent older one."""

    def __init__(self, initial: int) -> None:
        self.pending: int | None = initial
        self.closed = False
        self.wake = asyncio.Event()

    def push(self, status: int) -> None:
        self.pending = status
        self.wake.set()

    def close(self) -> None:
        self.closed = True
        self.wake.set()


class HealthServicer:
    """`grpc.health.v1.Health` with Check, List, and Watch, driven by `set` and the drain calls."""

    def __init__(self, schema: Schema) -> None:
        self._schema = schema
        self._response: Any = schema.message("grpc.health.v1.HealthCheckResponse")
        self._list_response: Any = schema.message("grpc.health.v1.HealthListResponse")
        self._status: dict[str, int] = {"": SERVING}
        self._watchers: dict[str, set[_Watcher]] = {}
        self._shutting_down = False

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    def watcher_count(self, service: str = "") -> int:
        return len(self._watchers.get(service, ()))

    def set(self, service: str, status: int) -> None:
        """Set one service's status; ignored for good once `enter_graceful_shutdown` ran."""
        if self._shutting_down:
            return
        self._set(service, status)

    def _set(self, service: str, status: int) -> None:
        self._status[service] = status
        for watcher in self._watchers.get(service, ()):
            watcher.push(status)

    def enter_graceful_shutdown(self) -> None:
        """Flip every known service to NOT_SERVING and freeze the registry (Python reference)."""
        if self._shutting_down:
            return
        for service in list(self._status):
            self._set(service, NOT_SERVING)
        self._shutting_down = True

    def resume(self) -> None:
        """Flip every known service back to SERVING and unfreeze (grpc-go `Resume`)."""
        self._shutting_down = False
        for service in list(self._status):
            self._set(service, SERVING)

    def close_watch_streams(self) -> None:
        """End every Watch stream with OK after it has delivered its pending status."""
        for watchers in self._watchers.values():
            for watcher in watchers:
                watcher.close()

    async def check(self, request: Any, context: Any) -> Any:
        status = self._status.get(request.service)
        if status is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "unknown service")
        return self._response(status=status)

    async def list_statuses(self, request: Any, context: Any) -> Any:
        if len(self._status) > MAX_LIST_SERVICES:
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"server health list exceeds maximum capacity: {MAX_LIST_SERVICES}",
            )
        reply = self._list_response()
        for service, status in self._status.items():
            reply.statuses[service].status = status
        return reply

    async def watch(self, request: Any, context: Any) -> AsyncGenerator[Any, None]:
        service: str = request.service
        watcher = _Watcher(self._status.get(service, SERVICE_UNKNOWN))
        self._watchers.setdefault(service, set()).add(watcher)
        last: int | None = None
        try:
            while True:
                while watcher.pending is not None:
                    status, watcher.pending = watcher.pending, None
                    if status != last:
                        last = status
                        yield self._response(status=status)
                if watcher.closed:
                    return
                watcher.wake.clear()
                await watcher.wake.wait()
        finally:
            self._watchers[service].discard(watcher)
            if not self._watchers[service]:
                del self._watchers[service]

    def handler(self) -> grpc.GenericRpcHandler:
        request_type = self._schema.message("grpc.health.v1.HealthCheckRequest")
        list_request_type = self._schema.message("grpc.health.v1.HealthListRequest")
        return grpc.method_handlers_generic_handler(
            HEALTH_SERVICE,
            {
                "Check": grpc.unary_unary_rpc_method_handler(
                    self.check,
                    request_deserializer=request_type.FromString,
                    response_serializer=self._response.SerializeToString,
                ),
                "List": grpc.unary_unary_rpc_method_handler(
                    self.list_statuses,
                    request_deserializer=list_request_type.FromString,
                    response_serializer=self._list_response.SerializeToString,
                ),
                "Watch": grpc.unary_stream_rpc_method_handler(
                    self.watch,
                    request_deserializer=request_type.FromString,
                    response_serializer=self._response.SerializeToString,
                ),
            },
        )


# --- layer 3: the reflection service --------------------------------------------------------


def _transitive_files(file_descriptor: Any) -> list[Any]:
    """The file first, then its dependencies depth-first, as the Python reference orders them."""
    seen: dict[str, Any] = {}

    def visit(current: Any) -> None:
        seen[current.name] = current
        for dependency in current.dependencies:
            if dependency.name not in seen:
                visit(dependency)

    visit(file_descriptor)
    return list(seen.values())


class ReflectionServicer:
    """`grpc.reflection.v1.ServerReflection`: one bidi stream, one response per request."""

    def __init__(
        self,
        schema: Schema,
        service_names: tuple[str, ...],
        *,
        allowlist_only: bool = False,
    ) -> None:
        self._schema = schema
        self._service_names = tuple(sorted(service_names))
        self._request_type: Any = schema.message("grpc.reflection.v1.ServerReflectionRequest")
        self._response_type: Any = schema.message("grpc.reflection.v1.ServerReflectionResponse")
        self._allowed: set[str] | None = None
        if allowlist_only:
            self._allowed = set()
            for name in self._service_names:
                service = schema.pool.FindServiceByName(name)
                for file_descriptor in _transitive_files(service.file):
                    self._allowed.add(file_descriptor.name)

    def _error(self, request: Any, code: grpc.StatusCode) -> Any:
        error = self._response_type.DESCRIPTOR.fields_by_name["error_response"].message_type
        message = _factory.GetMessageClass(error)(
            error_code=code.value[0], error_message=code.value[1]
        )
        return self._response_type(error_response=message, original_request=request)

    def _files(self, request: Any, file_descriptor: Any) -> Any:
        if self._allowed is not None and file_descriptor.name not in self._allowed:
            return self._error(request, grpc.StatusCode.NOT_FOUND)
        payload = []
        for member in _transitive_files(file_descriptor):
            proto = _pb.FileDescriptorProto()
            member.CopyToProto(proto)
            payload.append(proto.SerializeToString())
        reply_type = self._response_type.DESCRIPTOR.fields_by_name[
            "file_descriptor_response"
        ].message_type
        body = _factory.GetMessageClass(reply_type)(file_descriptor_proto=payload)
        return self._response_type(file_descriptor_response=body, original_request=request)

    def _file_of_symbol(self, symbol: str) -> Any:
        """Resolve `pkg.Type` or `pkg.Service[.Method]`; the pool itself knows no method names."""
        pool = self._schema.pool
        try:
            return pool.FindFileContainingSymbol(symbol)
        except KeyError:
            service_name, _, method = symbol.rpartition(".")
            service = pool.FindServiceByName(service_name)
            service.FindMethodByName(method)  # raises KeyError for an unknown method
            return service.file

    def answer(self, request: Any) -> Any:
        """Answer one request; the oneof decides the query, exactly as in reflection.proto."""
        kind: str | None = request.WhichOneof("message_request")
        pool = self._schema.pool
        try:
            if kind == "file_by_filename":
                return self._files(request, pool.FindFileByName(request.file_by_filename))
            if kind == "file_containing_symbol":
                return self._files(request, self._file_of_symbol(request.file_containing_symbol))
        except KeyError:
            return self._error(request, grpc.StatusCode.NOT_FOUND)
        if kind == "list_services":
            reply_type = self._response_type.DESCRIPTOR.fields_by_name[
                "list_services_response"
            ].message_type
            item_type = reply_type.fields_by_name["service"].message_type
            listing = _factory.GetMessageClass(reply_type)(
                service=[_factory.GetMessageClass(item_type)(name=n) for n in self._service_names]
            )
            return self._response_type(list_services_response=listing, original_request=request)
        if kind in {"file_containing_extension", "all_extension_numbers_of_type"}:
            return self._error(request, grpc.StatusCode.UNIMPLEMENTED)
        return self._error(request, grpc.StatusCode.INVALID_ARGUMENT)

    async def server_reflection_info(
        self, request_iterator: AsyncIterator[Any], context: Any
    ) -> AsyncGenerator[Any, None]:
        async for request in request_iterator:
            yield self.answer(request)

    def handler(self) -> grpc.GenericRpcHandler:
        return grpc.method_handlers_generic_handler(
            REFLECTION_SERVICE,
            {
                "ServerReflectionInfo": grpc.stream_stream_rpc_method_handler(
                    self.server_reflection_info,
                    request_deserializer=self._request_type.FromString,
                    response_serializer=self._response_type.SerializeToString,
                )
            },
        )


# --- layer 4: a server that can be drained --------------------------------------------------


class WorkService:
    """`lab.work.v1.Work/Do`: echoes `text`, but `block` parks on `gate` so tests hold RPCs open."""

    def __init__(self, schema: Schema) -> None:
        self._schema = schema
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.outcomes: list[str] = []
        self.seen: list[str] = []

    async def do(self, request: Any, context: Any) -> Any:
        self.seen.append(request.text)
        if request.text == "block":
            self.entered.set()
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.outcomes.append("cancelled")
                raise
            self.outcomes.append("completed")
        reply_type = self._schema.message("lab.work.v1.WorkReply")
        return reply_type(text=request.text)

    def handler(self) -> grpc.GenericRpcHandler:
        request_type = self._schema.message("lab.work.v1.WorkRequest")
        reply_type = self._schema.message("lab.work.v1.WorkReply")
        return grpc.method_handlers_generic_handler(
            WORK_SERVICE,
            {
                "Do": grpc.unary_unary_rpc_method_handler(
                    self.do,
                    request_deserializer=request_type.FromString,
                    response_serializer=reply_type.SerializeToString,
                )
            },
        )


@dataclass
class LabServer:
    """A loopback `grpc.aio` server with Health, Reflection, and Work registered."""

    schema: Schema = field(default_factory=build_schema)
    with_health: bool = True
    allowlist_only: bool = False
    server: Any = None
    port: int = 0
    health: HealthServicer = field(init=False)
    work: WorkService = field(init=False)
    reflection: ReflectionServicer = field(init=False)

    async def __aenter__(self) -> LabServer:
        self.health = HealthServicer(self.schema)
        self.work = WorkService(self.schema)
        listed = (WORK_SERVICE, REFLECTION_SERVICE) + (
            (HEALTH_SERVICE,) if self.with_health else ()
        )
        self.reflection = ReflectionServicer(
            self.schema, listed, allowlist_only=self.allowlist_only
        )
        self.server = aio.server()
        handlers = [self.work.handler(), self.reflection.handler()]
        if self.with_health:
            handlers.append(self.health.handler())
        self.server.add_generic_rpc_handlers(tuple(handlers))
        self.port = int(self.server.add_insecure_port("127.0.0.1:0"))
        await self.server.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.server.stop(0)

    @property
    def target(self) -> str:
        return f"127.0.0.1:{self.port}"

    def channel(self) -> aio.Channel:
        return aio.insecure_channel(self.target)


class Client:
    """Typed-enough client stubs over one channel, built from the same schema."""

    def __init__(self, channel: aio.Channel, schema: Schema) -> None:
        self._schema = schema
        health_request = schema.message("grpc.health.v1.HealthCheckRequest")
        health_response = schema.message("grpc.health.v1.HealthCheckResponse")
        list_request = schema.message("grpc.health.v1.HealthListRequest")
        list_response = schema.message("grpc.health.v1.HealthListResponse")
        work_request = schema.message("lab.work.v1.WorkRequest")
        work_reply = schema.message("lab.work.v1.WorkReply")
        self._health_request = health_request
        self._work_request = work_request
        self.check_rpc: Any = channel.unary_unary(
            f"/{HEALTH_SERVICE}/Check",
            request_serializer=health_request.SerializeToString,
            response_deserializer=health_response.FromString,
        )
        self.list_rpc: Any = channel.unary_unary(
            f"/{HEALTH_SERVICE}/List",
            request_serializer=list_request.SerializeToString,
            response_deserializer=list_response.FromString,
        )
        self.watch_rpc: Any = channel.unary_stream(
            f"/{HEALTH_SERVICE}/Watch",
            request_serializer=health_request.SerializeToString,
            response_deserializer=health_response.FromString,
        )
        self.work_rpc: Any = channel.unary_unary(
            f"/{WORK_SERVICE}/Do",
            request_serializer=work_request.SerializeToString,
            response_deserializer=work_reply.FromString,
        )
        reflection_request = schema.message("grpc.reflection.v1.ServerReflectionRequest")
        reflection_response = schema.message("grpc.reflection.v1.ServerReflectionResponse")
        self.reflection_rpc: Any = channel.stream_stream(
            f"/{REFLECTION_SERVICE}/ServerReflectionInfo",
            request_serializer=reflection_request.SerializeToString,
            response_deserializer=reflection_response.FromString,
        )

    async def check(self, service: str = "", *, deadline: float = 2.0) -> int:
        reply = await self.check_rpc(self._health_request(service=service), timeout=deadline)
        return int(reply.status)

    def watch(self, service: str = "") -> Any:
        return self.watch_rpc(self._health_request(service=service))

    async def do(self, text: str, *, deadline: float | None = 5.0) -> str:
        reply = await self.work_rpc(self._work_request(text=text), timeout=deadline)
        return str(reply.text)


@dataclass
class DrainReport:
    """What `drain_and_stop` did, in order, and how long `server.stop` itself took."""

    events: list[str] = field(default_factory=lambda: list[str]())
    stop_seconds: float = 0.0


async def drain_and_stop(
    lab: LabServer, *, drain_delay: float, grace: float | None, end_watches: bool
) -> DrainReport:
    """The SIGTERM sequence: advertise NOT_SERVING, let observers react, then `stop(grace)`.

    `drain_delay` is the time a load balancer or kubelet needs to notice NOT_SERVING; it is
    a sleep because health state is advisory and nothing in gRPC reports who has noticed.
    """
    report = DrainReport()
    lab.health.enter_graceful_shutdown()
    report.events.append("not_serving")
    if end_watches:
        lab.health.close_watch_streams()
        report.events.append("watches_closed")
    await asyncio.sleep(drain_delay)
    report.events.append("drain_delay_elapsed")
    started = time.monotonic()
    await lab.server.stop(grace)
    report.stop_seconds = time.monotonic() - started
    report.events.append("stopped")
    return report


async def next_or_none(call: Any, wait: float) -> Any:
    """Next streamed message within `wait` seconds, or None at end of stream.

    Uses `read()` because grpc.aio forbids mixing it with `async for` on one call.
    """
    message = await asyncio.wait_for(call.read(), wait)
    return None if message is aio.EOF else message
