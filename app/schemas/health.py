"""GET /health model (API_SPEC.md §1). First three fields are contractual; the rest are extras."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"]
    keys_configured: int
    cache_enabled: bool
    # --- extra diagnostics -------------------------------------------------------
    cache_type: str
    keys_unique: int  # distinct credentials (duplicates are not independent)
    keys_healthy: int  # distinct credentials whose last real call did not fail
    min_keys_required: int
    failure_simulation_enabled: bool
    database: Literal["ok"]
    model: str
