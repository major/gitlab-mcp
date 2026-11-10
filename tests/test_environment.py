"""Keep test startup quiet without weakening network isolation."""

import socket
import warnings

import pytest
from pytest_socket import SocketBlockedError


def _attempt_network_operation(operation: str, args: tuple[int | str, ...]) -> None:
    """Close any socket unexpectedly created if the guard is broken."""
    result = getattr(socket, operation)(*args)
    if operation == "socket":
        result.close()


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        pytest.param("socket", (socket.AF_INET,), id="ipv4"),
        pytest.param("socket", (socket.AF_INET6,), id="ipv6"),
        pytest.param("getaddrinfo", ("127.0.0.1", 443), id="getaddrinfo"),
        pytest.param("gethostbyname", ("127.0.0.1",), id="gethostbyname"),
    ],
)
def test_network_operations_still_warn_and_fail(operation: str, args: tuple[int | str, ...]) -> None:
    """Socket and DNS guards remain active and their warnings remain visible."""
    # Arrange: retain the configured warning filters, unlike pytest.warns.
    with warnings.catch_warnings(record=True) as caught, pytest.raises(SocketBlockedError):
        # Act
        _attempt_network_operation(operation, args)

    # Assert
    assert len(caught) == 1
    assert caught[0].category is UserWarning
    assert str(caught[0].message) == f"A test tried to use socket.{operation}."


def test_async_fixture_loop_scope_is_explicit(pytestconfig: pytest.Config) -> None:
    """Async fixtures use an explicit function-scoped event loop."""
    # Arrange / Act
    scope = pytestconfig.getini("asyncio_default_fixture_loop_scope")

    # Assert
    assert scope == "function"


@pytest.mark.parametrize(
    ("message", "module"),
    [
        ("A different deprecation", "beartype._util.cls.pep.clspep3119"),
        (
            (
                "<class 'key_value.aio.protocols.key_value.AsyncKeyValue'> isn't explicitly "
                "decorated with @runtime_checkable"
            ),
            "another_module",
        ),
    ],
)
def test_unrelated_deprecation_warnings_remain_visible(message: str, module: str) -> None:
    """Unrelated deprecations remain visible under the configured filters."""
    # Arrange: keep the configured warning filters.
    with warnings.catch_warnings(record=True) as caught:
        # Act
        warnings.warn_explicit(message, DeprecationWarning, "example.py", 1, module=module)

    # Assert
    assert len(caught) == 1
    assert caught[0].category is DeprecationWarning
    assert str(caught[0].message) == message
