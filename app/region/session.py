"""Persist selected scan region per Telegram user for the current bot session."""

from __future__ import annotations

from telegram.ext import ContextTypes

from app.region.types import ScanRegion

USER_REGION_KEY = "selected_scan_region"
DEFAULT_REGION = ScanRegion.ALL


def get_user_scan_region(context: ContextTypes.DEFAULT_TYPE) -> ScanRegion:
    stored = context.user_data.get(USER_REGION_KEY)
    if isinstance(stored, ScanRegion):
        return stored
    if isinstance(stored, str):
        try:
            return ScanRegion(stored)
        except ValueError:
            pass
    return DEFAULT_REGION


def set_user_scan_region(context: ContextTypes.DEFAULT_TYPE, region: ScanRegion) -> None:
    context.user_data[USER_REGION_KEY] = region
