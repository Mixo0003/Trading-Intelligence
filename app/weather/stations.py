from dataclasses import dataclass


@dataclass(frozen=True)
class WeatherStation:
    code: str
    name: str
    latitude: float
    longitude: float
    elevation_m: float | None = None
    timezone: str | None = None
    verified: bool = False
    resolution_url: str | None = None


KNOWN_STATIONS: dict[str, WeatherStation] = {
    "KORD": WeatherStation("KORD", "Chicago O'Hare International Airport", 41.9786, -87.9048),
    "KJFK": WeatherStation("KJFK", "John F. Kennedy International Airport", 40.6413, -73.7781),
    "KLAX": WeatherStation("KLAX", "Los Angeles International Airport", 33.9416, -118.4085),
    "KMIA": WeatherStation("KMIA", "Miami International Airport", 25.7959, -80.2870),
    "KDFW": WeatherStation("KDFW", "Dallas/Fort Worth International Airport", 32.8998, -97.0403),
    "KPHX": WeatherStation("KPHX", "Phoenix Sky Harbor International Airport", 33.4352, -112.0101),
    "KLAS": WeatherStation("KLAS", "Harry Reid International Airport", 36.0840, -115.1537),
}


def get_station(code: str | None) -> WeatherStation | None:
    if not code:
        return None
    return KNOWN_STATIONS.get(code.upper())
