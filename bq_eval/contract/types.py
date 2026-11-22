"""Bounded finite numeric type aliases for strict Pydantic v2 contracts."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
NonNegFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
PosFloat = Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
UnitFloat = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
