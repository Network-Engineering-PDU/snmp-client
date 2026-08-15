"""Value conversion and validation shared by the MIB and trap encoder."""

import math
from typing import Any, Dict, Optional

from .nee_mib_schema import UNAVAILABLE


def finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def scaled_integer(value: Any, factor: float,
                   minimum: int, maximum: int) -> int:
    number = finite_number(value)
    if number is None:
        return UNAVAILABLE
    result = int(round(number * factor))
    return result if minimum <= result <= maximum else UNAVAILABLE


def display_string(value: Any, maximum: int) -> str:
    if value is None:
        return ""
    # pass_persist is line-oriented. Prevent a value from injecting protocol
    # lines and apply DisplayString SIZE constraints in UTF-8 octets.
    text = str(value).replace("\r", " ").replace("\n", " ").replace("\0", "")
    encoded = text.encode("utf-8")[:maximum]
    return encoded.decode("utf-8", errors="ignore")


def valid_display_string(value: str, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and "\r" not in value
        and "\n" not in value
        and "\0" not in value
        and len(value.encode("utf-8")) <= maximum
    )


def stored_integer(values: Dict[str, Any], key: str, minimum: int,
                   maximum: int, default: int = UNAVAILABLE) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if minimum <= value <= maximum else default
