"""Runtime override markers for test methods on Python 3.11 and newer.

The matching stub uses the type checker's PEP 698 decorator contract. Runtime
tests need only the standard marker and do not import typing_extensions.
"""

from typing import TypeVar

_Method = TypeVar("_Method")


def override(method: _Method) -> _Method:
    setattr(method, "__override__", True)
    return method
