import inspect
from functools import partial, wraps
from inspect import Signature, _empty, _ParameterKind  # type: ignore
from typing import (
    Any,
    Awaitable,
    Callable,
    ForwardRef,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    get_type_hints,
)
from uuid import UUID

from guardpost import Identity, User
from rodi import ContainerProtocol

from blacksheep.messages import Request, Response
from blacksheep.normalization import copy_special_attributes
from blacksheep.server.routing import Route
from blacksheep.server.websocket import WebSocket

from .bindings import (
    Binder,
    BodyBinder,
    BoundValue,
    IdentityBinder,
    QueryBinder,
    RouteBinder,
    ServiceBinder,
    empty,
    get_binder_by_type,
)

_next_handler_binder = object()


# region PEP 604
try:
    # Python >= 3.10
    from types import UnionType
except ImportError:  # pragma: no cover
    UnionType = ...


def _is_union_type(annotation):
    if UnionType is not ... and isinstance(annotation, UnionType):  # type: ignore
        return True
    return False


# endregion


# region PEP 563


class ParamInfo:
    __slots__ = ("name", "annotation", "kind", "default", "_str")

    def __init__(self, name, annotation, kind, default, str_repr):
        self.name = name
        self.annotation = annotation
        self.kind = kind
        self.default = default
        self._str = str_repr

    def __str__(self) -> str:
        return self._str


def _get_method_annotations_or_throw(method):
    method_locals = getattr(method, "_locals", None)
    method_globals = getattr(method, "_globals", None)

    try:
        return get_type_hints(method, globalns=method_globals, localns=method_locals)
    except TypeError:
        if inspect.isclass(method) or hasattr(method, "__call__"):
            # can be a callable class
            return get_type_hints(
                method.__call__, globalns=method_globals, localns=method_locals
            )
        raise  # pragma: no cover


def _get_method_annotations_base(method, signature: Optional[Signature] = None):
    if signature is None:
        signature = Signature.from_callable(method)
    params = {
        key: ParamInfo(
            value.name, value.annotation, value.kind, value.default, str(value)
        )
        for key, value in signature.parameters.items()
    }

    annotations = _get_method_annotations_or_throw(method)
    for key, value in params.items():
        if key in annotations:
            value.annotation = annotations[key]
    return params


# endregion


class NormalizationError(Exception): ...


class UnsupportedSignatureError(NormalizationError):
    def __init__(self, method):
        super().__init__(
            f"Cannot normalize the method `{method.__qualname__}` because its "
            f"signature contains *args, or *kwargs, or keyword only parameters. "
            f"If you use a decorator, please use `functools.@wraps` "
            f"with your wrapper, to fix this error."
        )


class AsyncGeneratorMissingAnnotationError(NormalizationError):
    """
    Exception raised when a request handler is defined as async generator but is not
    annotated with return type information.
    """

    def __init__(self, method) -> None:
        super().__init__(
            f"Cannot normalize the method `{method.__qualname__}` because it "
            "is defined as asynchronous generator but its return type is not "
            "specified. To resolve, add a return type annotation like AsyncIterable[T] "
            "and ensure the type is configured using the register_streamed_type "
            "function."
        )


class AsyncGeneratorMissingResponseTypeError(NormalizationError):
    """
    Exception raised when a request handler is defined as async generator but there is
    no Response type configured to handle the type it yields.
    """

    def __init__(self, method, yielded_type) -> None:
        super().__init__(
            f"Cannot normalize the method `{method.__qualname__}` because there "
            f"is no Response type configured to handle its yield type {yielded_type}. "
            "To resolve, configure the response type using the register_streamed_type "
            "function."
        )


class UnsupportedForwardRefInSignatureError(NormalizationError):
    def __init__(self, unsupported_type):
        super().__init__(  # pragma: no cover
            f"Cannot normalize the method `{unsupported_type}` because its "
            f"signature contains a forward reference (type annotation as string). "
            f"Use type annotations to exact types to fix this error. "
        )


class AmbiguousMethodSignatureError(NormalizationError):
    def __init__(self, method):
        super().__init__(
            f"Cannot normalize the method `{method.__qualname__}` because it has an "
            "ambiguous signature. "
            "Please specify exact binders for its arguments."
        )


class RouteBinderMismatch(NormalizationError):
    def __init__(self, parameter_name, route):
        super().__init__(
            f"The parameter {parameter_name} for method "
            f"{route.handler.__name__} is bound to route path, "
            f"but the route doesn`t contain a parameter with matching name."
        )


_types_handled_with_query = {
    str,
    int,
    float,
    bool,
    list,
    set,
    tuple,
    List[str],
    List[int],
    List[float],
    List[bool],
    Sequence[str],
    Sequence[int],
    Sequence[float],
    Sequence[bool],
    Set[str],
    Set[int],
    Set[float],
    Set[bool],
    Tuple[str],
    Tuple[int],
    Tuple[float],
    Tuple[bool],
    UUID,
    List[UUID],
    Set[UUID],
    Tuple[UUID],
}

try:
    # Note: try catch here is to support Python 3.8
    # it can be removed when support for Python 3.8 is dropped
    _types_handled_with_query |= {
        list[str],
        list[int],
        list[float],
        list[bool],
        list[UUID],
        tuple[str],
        tuple[int],
        tuple[float],
        tuple[bool],
    }
except TypeError:
    pass


def _check_union(
    parameter: ParamInfo, annotation: Any, method: Callable[..., Any]
) -> Tuple[bool, Any]:
    """
    Checks if the given annotation is Optional[] - in such case unwraps it
    and returns its value.

    An exception is thrown if other kinds of Union[] are used, since they are
    not supported by method normalization.
    In such case, the user of the library should read the desired value from
    the request object.
    """

    if (
        hasattr(annotation, "__origin__") and annotation.__origin__ is Union
    ) or _is_union_type(annotation):
        # support only Union[None, Type] - that is equivalent of Optional[Type],
        # and also PEP 604 T | Non; None | T
        if type(None) not in annotation.__args__ or len(annotation.__args__) > 2:
            raise NormalizationError(
                f'Unsupported parameter type "{parameter.name}" '
                f'for method "{method.__name__}"; '
                f"only Optional types are supported for automatic binding. "
                f"Read the desired value from the request itself."
            )

        for possible_type in annotation.__args__:
            if type(None) is possible_type:
                continue
            return True, possible_type

    return False, annotation


def _get_parameter_binder_without_annotation(
    services: ContainerProtocol,
    route: Optional[Route],
    name: str,
) -> Binder:
    if route:
        # 1. does route contain a parameter with matching name?
        if name in route.param_names:
            return RouteBinder(str, name, True)

    # 2. do services contain a service with matching name?
    if name in services:
        return ServiceBinder(name, name, True, services)

    # 3. default to query parameter
    return QueryBinder(List[str], name, True)


def _is_bound_value_annotation(annotation: Any) -> bool:
    if inspect.isclass(annotation) and issubclass(annotation, BoundValue):
        return True
    return "__origin__" in annotation.__dict__ and issubclass(
        annotation.__dict__["__origin__"], BoundValue
    )


def _get_raw_bound_value_type(bound_type: Type[BoundValue]) -> Type[Any]:
    if hasattr(bound_type, "__args__"):
        return bound_type.__args__[0]  # type: ignore

    # the type can be a subclass of a type specifying the annotation
    if hasattr(bound_type, "__orig_bases__"):
        for subtype in bound_type.__orig_bases__:  # type: ignore
            if hasattr(subtype, "__args__"):
                return subtype.__args__[0]  # type: ignore
    return str


def _get_bound_value_type(bound_type: Type[BoundValue]) -> Type[Any]:
    value_type = _get_raw_bound_value_type(bound_type)
    assert not isinstance(value_type, TypeVar)

    return value_type


def _get_parameter_binder(
    parameter: ParamInfo,
    services: ContainerProtocol,
    route: Optional[Route],
    method: Callable[..., Any],
) -> Binder:
    name = parameter.name

    if name in Binder.aliases:
        return Binder.aliases[name](services)

    original_annotation = parameter.annotation

    if original_annotation is _empty:
        return _get_parameter_binder_without_annotation(services, route, name)

    # unwrap the Optional[] annotation, if present:
    is_root_optional, annotation = _check_union(parameter, original_annotation, method)

    if isinstance(annotation, (str, ForwardRef)):  # pragma: no cover
        raise UnsupportedForwardRefInSignatureError(original_annotation)

    if (
        annotation in Binder.handlers
        and annotation not in services
        and not issubclass(annotation, BoundValue)
    ):
        return Binder.handlers[annotation](annotation, parameter.name)

    # 1. is the type annotation of BoundValue[T] type?
    if _is_bound_value_annotation(annotation):
        binder_type = get_binder_by_type(annotation)
        expected_type = _get_bound_value_type(annotation)

        is_optional, expected_type = _check_union(parameter, expected_type, method)

        if isinstance(expected_type, (str, ForwardRef)):  # pragma: no cover
            raise UnsupportedForwardRefInSignatureError(expected_type)

        parameter_name = annotation.name or name

        if binder_type in services:
            # use DI container to instantiate
            # note that, currently, binders are always singletons instantiated at
            # application start
            binder = services.resolve(binder_type)
            binder.expected_type = expected_type
            binder.parameter_name = parameter_name
        else:
            binder = binder_type(expected_type, parameter_name, False)
        binder.required = not is_optional

        if is_root_optional:
            binder.root_required = False

        if route:
            if (
                isinstance(binder, RouteBinder)
                and parameter_name not in route.param_names
            ):
                raise RouteBinderMismatch(parameter_name, route)

        if isinstance(binder, ServiceBinder):
            binder.services = services

        return binder

    # 2. does route contain a parameter with matching name?
    if route and name in route.param_names:
        return RouteBinder(annotation, name, True)

    # 3. do services contain a service with matching type?
    if annotation in services:
        return ServiceBinder(annotation, annotation.__class__.__name__, True, services)

    # 4. is simple type?
    if annotation in _types_handled_with_query:
        return QueryBinder(annotation, name, True, required=not is_root_optional)

    # 5. is request user?
    if annotation is User or annotation is Identity:
        return IdentityBinder(
            annotation, name, implicit=True, required=not is_root_optional
        )

    raise ValueError("No matching binder.")


def get_parameter_binder(
    parameter: ParamInfo,
    services: ContainerProtocol,
    route: Optional[Route],
    method: Callable[..., Any],
) -> Binder:
    binder = _get_parameter_binder(parameter, services, route, method)
    if parameter.default is _empty:
        binder.default = empty
    else:
        binder.default = parameter.default
    return binder


def _get_binders_for_function(
    method: Callable[..., Any], services: ContainerProtocol, route: Optional[Route]
) -> List[Binder]:
    parameters = _get_method_annotations_base(method)
    body_binder = None

    binders = []
    for parameter_name, parameter in parameters.items():
        if not route and parameter_name in {"handler", "next_handler"}:
            binders.append(_next_handler_binder)
            continue

        binder = get_parameter_binder(parameter, services, route, method)
        if isinstance(binder, BodyBinder):
            if body_binder is None:
                body_binder = binder
            else:
                raise AmbiguousMethodSignatureError(method)
        binders.append(binder)

    return binders


def get_binders(route: Route, services: ContainerProtocol) -> List[Binder]:
    """
    Returns a list of binders to extract parameters
    for a request handler.
    """
    binders = _get_binders_for_function(route.handler, services, route)
    setattr(route.handler, "binders", binders)
    return binders


def get_binders_for_middleware(
    method: Callable[..., Any], services: ContainerProtocol
) -> Sequence[Binder]:
    return _get_binders_for_function(method, services, None)


def get_async_wrapper(
    services: ContainerProtocol,
    route: Route,
    method: Callable[..., Any],
    params: Mapping[str, ParamInfo],
    params_len: int,
) -> Callable[[Request], Awaitable[Response]]:
    """
    Returns an asynchronous wrapper for awaitable request handlers that return objects.
    """
    if params_len == 0:
        # the user defined a request handler with no input
        @wraps(method)
        async def handler(_):  # type: ignore
            return await method()

        return handler

    if params_len == 1:
        param_name = list(params)[0]
        # There is no need to wrap the request handler if it was
        # defined as asynchronous function accepting a single request or
        # websocket parameter
        if params[param_name].annotation in {Request, WebSocket}:
            return method

    binders = get_binders(route, services)

    @wraps(method)
    async def handler(request):
        values = [await binder.get_parameter(request) for binder in binders]
        return await method(*values)

    return handler


def get_async_wrapper_for_asyncgen(
    response_type: Any,
    services: ContainerProtocol,
    route: Route,
    method: Callable[..., Any],
    params: Mapping[str, ParamInfo],
    params_len: int,
) -> Callable[[Request], Awaitable[Response]]:
    """
    Returns an asynchronous wrapper for a request handler defined as asynchronous
    generator yielding objects. This function must be called with the right
    response_type argument, able to handle objects yielded by the method.
    """
    if params_len == 0:
        # the user defined a request handler with no input
        # this should almost never happen as the user should handle
        # request.is_disconnected() for a streaming response
        @wraps(method)
        async def handler(_) -> Response:  # type: ignore
            return response_type(method)

        return handler

    if params_len == 1:
        param_name = list(params)[0]
        # In this case, we
        if param_name == "request" or params[param_name].annotation is Request:

            @wraps(method)
            async def normal_sse_handler(request) -> Response:
                return response_type(partial(method, request))

            return normal_sse_handler

    binders = get_binders(route, services)

    @wraps(method)
    async def handler(request):
        values = [await binder.get_parameter(request) for binder in binders]
        return response_type(partial(method, *values))

    return handler


def normalize_handler(
    route: Route, services: ContainerProtocol, http_method: str = ""
) -> Callable[[Request], Awaitable[Response]]:
    """
    Root function used to normalize a request handler. The objective of this function is
    to improve the developer experience, so developers using BlackSheep have more
    options when defining request handlers.

    When a request handler already has the right signature, it is kept as-is (this
    avoids performance fees when handling request handlers). If a request handler
    instead has an arbitrary signature, it is wrapped inside a normal request handler
    (`async def handler(request) -> Response: ...`).
    """
    method = route.handler

    sig = Signature.from_callable(method)
    params = _get_method_annotations_base(method, sig)
    params_len = len(params)

    if any(
        str(param).startswith("*") or param.kind.value == _ParameterKind.KEYWORD_ONLY
        for param in params.values()
    ):
        raise UnsupportedSignatureError(method)

    return_type = sig.return_annotation

    # normalize input
    if inspect.iscoroutinefunction(method):
        normalized = get_async_wrapper(services, route, method, params, params_len)
    else:
        raise TypeError("Sync function not allowed as handler!")

    # Normalize output. WebSocket handlers must be excluded here because their
    # response is not handled writing a BlackSheep Response object.
    if (
        return_type is _empty or return_type is not Response
    ) and http_method != "GET_WS":
        if return_type is not _empty:
            # this scenario enables a more accurate automatic generation of
            # OpenAPI Documentation, for responses
            setattr(route.handler, "return_type", return_type)

    if normalized is not method:
        setattr(normalized, "root_fn", method)
        copy_special_attributes(method, normalized)

    return normalized


def _is_basic_middleware_signature(parameters: Mapping[str, inspect.Parameter]) -> bool:
    values = list(parameters.values())

    if len(values) != 2:
        return False

    first_one = values[0]
    second_one = values[1]
    if first_one.name == "request" and second_one.name in {"handler", "next_handler"}:
        return True
    return False


def _get_middleware_async_binder(
    method: Callable[..., Awaitable[Response]], services: ContainerProtocol
) -> Callable[[Request, Callable[..., Any]], Awaitable[Response]]:
    binders = get_binders_for_middleware(method, services)

    async def handler(request, next_handler):
        values = []
        for binder in binders:
            if binder is _next_handler_binder:
                values.append(next_handler)
            else:
                values.append(await binder.get_parameter(request))

        if _next_handler_binder in binders:
            # middleware that can continue the chain: control is left to it;
            # for example an authorization middleware can decide to now call
            # the next handler
            return await method(*values)

        # middleware that cannot continue the chain, so we continue it here
        await method(*values)
        return await next_handler(request)

    return handler


def normalize_middleware(
    middleware: Callable[..., Awaitable[Response]], services: ContainerProtocol
) -> Callable[[Request, Callable[..., Any]], Awaitable[Response]]:
    if not inspect.iscoroutinefunction(middleware) and not inspect.iscoroutinefunction(
        getattr(middleware, "__call__", None)
    ):
        raise ValueError("Middlewares must be asynchronous functions")

    params = _get_method_annotations_base(middleware)

    if _is_basic_middleware_signature(params):
        return middleware

    return _get_middleware_async_binder(middleware, services)


def get_asyncgen_yield_type(fn) -> Any:
    """
    Returns the yield type T for an asynchronous generator with a return type annotation
    of AsyncIterable[T].

    async def example(data: str) -> AsyncIterable[int]:
        ...

    get_asyncgen_yield_type(example)
    int
    """
    if not inspect.isasyncgenfunction(fn):
        raise ValueError("The given function is not an async generator.")

    signature = Signature.from_callable(fn)
    return_annotation = signature.return_annotation

    origin = getattr(return_annotation, "__origin__", None)

    if origin is None:
        return None

    args = getattr(return_annotation, "__args__", None)

    if args is not None and len(args) >= 1:
        return args[0]

    return None
