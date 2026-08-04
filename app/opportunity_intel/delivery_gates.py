"""Centralized admin/user delivery gates for Market Opportunity Scanner."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OpportunityDeliveryGates:
    """Process-level gates independent of per-user subscription settings.

    Defaults keep non-admin opted-in delivery off until explicitly enabled.
    Admin delivery may be toggled independently.
    """

    admin_delivery_enabled: bool = True
    user_delivery_enabled: bool = False

    def allows(self, *, is_admin_recipient: bool) -> bool:
        if is_admin_recipient:
            return bool(self.admin_delivery_enabled)
        return bool(self.user_delivery_enabled)
