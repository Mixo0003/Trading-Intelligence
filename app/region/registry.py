"""Maintained station, ICAO-prefix, and city-to-country mappings for region classification."""

from __future__ import annotations

from app.region.types import ScanRegion

# ISO 3166-1 alpha-2 country codes.
USA_COUNTRIES = frozenset({"US", "PR", "GU", "VI", "AS", "MP"})
EUROPE_COUNTRIES = frozenset({
    "GB", "IE", "FR", "DE", "ES", "IT", "NL", "BE", "CH", "AT", "SE", "NO", "DK",
    "FI", "PT", "GR", "PL", "CZ", "HU", "RO", "SK", "SI", "HR", "BG", "LT", "LV",
    "EE", "LU", "IS", "MT", "CY", "TR",  # Turkey treated as Europe for weather markets
})
ASIA_COUNTRIES = frozenset({
    "CN", "JP", "KR", "TW", "HK", "SG", "IN", "TH", "MY", "ID", "PH", "VN", "PK",
    "BD", "LK", "NP", "MM", "KH", "LA", "MN", "KZ", "UZ", "AE", "SA", "IL", "QA",
    "KW", "BH", "OM", "JO", "LB",
})

# Transcontinental Russia: European-side hubs vs Asian-side (documented split).
RUSSIA_EUROPE_STATIONS = frozenset({
    "UUEE", "ULLI", "UUWW", "UUDD", "USMM", "URSS", "URWW", "UWGG", "UWWW",
})
RUSSIA_ASIA_STATIONS = frozenset({
    "UHHH", "UHSS", "UNNT", "UUOO", "USSS", "UWUU", "UWWW",
})

STATION_COUNTRY: dict[str, str] = {
    # USA
    "KORD": "US", "KJFK": "US", "KLGA": "US", "KMIA": "US", "KATL": "US",
    "KSEA": "US", "KLAX": "US", "KDFW": "US", "KPHX": "US", "KLAS": "US",
    "KBOS": "US", "KDCA": "US", "KIAD": "US", "KSFO": "US", "KDEN": "US",
    "KIAH": "US", "KMSP": "US", "KPDX": "US", "KSLC": "US", "KPHL": "US",
    "TJSJ": "PR", "PHNL": "US",
    # Europe
    "EGLL": "GB", "EGKK": "GB", "EGSS": "GB", "EGPH": "GB",
    "LFPB": "FR", "LFPG": "FR", "LFPO": "FR",
    "EDDM": "DE", "EDDF": "DE", "EDDL": "DE",
    "LEMD": "ES", "LEBL": "ES",
    "LIRF": "IT", "LIMC": "IT",
    "EHAM": "NL", "EBBR": "BE", "LSZH": "CH", "LOWW": "AT",
    "EKCH": "DK", "ENGM": "NO", "ESSA": "SE", "EFHK": "FI",
    "EIDW": "IE", "LPPT": "ES", "LGAV": "GR",
    # Asia
    "VHHH": "HK", "RCTP": "TW", "RJTT": "JP", "RJAA": "JP", "RKSI": "KR",
    "ZGGG": "CN", "ZHHH": "CN", "ZSPD": "CN", "ZSSS": "CN", "ZBAA": "CN",
    "WSSS": "SG", "VTBS": "TH", "VIDP": "IN", "RKSS": "KR",
}

CITY_COUNTRY: dict[str, str] = {
    "Chicago": "US", "New York City": "US", "Miami": "US", "Atlanta": "US",
    "Seattle": "US", "Los Angeles": "US", "Boston": "US", "Washington": "US",
    "London": "GB", "Paris": "FR", "Munich": "DE", "Madrid": "ES", "Rome": "IT",
    "Berlin": "DE", "Amsterdam": "NL", "Brussels": "BE", "Zurich": "CH",
    "Hong Kong": "HK", "Taipei": "TW", "Wuhan": "CN", "Tokyo": "JP", "Seoul": "KR",
    "Singapore": "SG", "Bangkok": "TH", "Mumbai": "IN", "Delhi": "IN",
}

# ICAO prefix (1-2 chars) -> country when unambiguous.
ICAO_PREFIX_COUNTRY: dict[str, str] = {
    "K": "US",
    "C": "CA",
    "EG": "GB", "EI": "IE", "LF": "FR", "ED": "DE", "LE": "ES", "LI": "IT",
    "EH": "NL", "EB": "BE", "LS": "CH", "LO": "AT", "EK": "DK", "EN": "NO",
    "ES": "SE", "EF": "FI", "EP": "PL", "LK": "CZ", "LH": "HU", "LG": "GR",
    "LP": "PT", "LT": "TR",
    "RJ": "JP", "RK": "KR", "RC": "TW", "VH": "HK", "Z": "CN", "V": "IN",
    "W": "ID", "O": "AE",
}


def country_to_region(country_code: str | None) -> ScanRegion | None:
    if not country_code:
        return None
    code = country_code.upper()
    if code in USA_COUNTRIES:
        return ScanRegion.USA
    if code in EUROPE_COUNTRIES:
        return ScanRegion.EUROPE
    if code in ASIA_COUNTRIES:
        return ScanRegion.ASIA
    return None


def station_to_country(station_code: str | None) -> str | None:
    if not station_code:
        return None
    code = station_code.upper()
    if code in STATION_COUNTRY:
        return STATION_COUNTRY[code]
    if code in RUSSIA_EUROPE_STATIONS:
        return "RU_EU"
    if code in RUSSIA_ASIA_STATIONS:
        return "RU_AS"
    for prefix_len in (2, 1):
        prefix = code[:prefix_len]
        if prefix in ICAO_PREFIX_COUNTRY:
            return ICAO_PREFIX_COUNTRY[prefix]
    return None


def country_code_to_region(country_code: str | None, station_code: str | None = None) -> ScanRegion | None:
    if not country_code:
        return None
    code = country_code.upper()
    if code == "RU_EU":
        return ScanRegion.EUROPE
    if code == "RU_AS":
        return ScanRegion.ASIA
    if code == "CA":
        return None  # Canada is neither USA nor in explicit EU/Asia lists
    return country_to_region(code)


def city_to_country(city: str | None) -> str | None:
    if not city:
        return None
    normalized = city.strip().title()
    if normalized in CITY_COUNTRY:
        return CITY_COUNTRY[normalized]
    for name, country in CITY_COUNTRY.items():
        if name.lower() == city.strip().lower():
            return country
    return None
