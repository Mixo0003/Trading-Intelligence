from enum import Enum


class ScanRegion(str, Enum):
    USA = "USA"
    EUROPE = "EUROPE"
    ASIA = "ASIA"
    ALL = "ALL"

    @property
    def display_label(self) -> str:
        return {
            ScanRegion.USA: "USA",
            ScanRegion.EUROPE: "Europe",
            ScanRegion.ASIA: "Asia",
            ScanRegion.ALL: "All Regions",
        }[self]

    @property
    def menu_button(self) -> str:
        return {
            ScanRegion.USA: "🇺🇸 USA",
            ScanRegion.EUROPE: "🇪🇺 Europe",
            ScanRegion.ASIA: "🌏 Asia",
            ScanRegion.ALL: "🌐 All Regions",
        }[self]

    @property
    def scan_button(self) -> str:
        if self is ScanRegion.ALL:
            return "🔍 Scan All Regions"
        return f"🔍 Scan {self.display_label}"


def parse_scan_region(value: str | None, default: ScanRegion = ScanRegion.ALL) -> ScanRegion:
    if not value:
        return default
    normalized = value.strip().upper().replace(" ", "_")
    for region in ScanRegion:
        if normalized == region.value or normalized == region.name:
            return region
    return default
