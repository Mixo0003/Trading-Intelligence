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
    "KLGA": WeatherStation("KLGA", "LaGuardia Airport", 40.7772, -73.8726),
    "KLAX": WeatherStation("KLAX", "Los Angeles International Airport", 33.9416, -118.4085),
    "KMIA": WeatherStation("KMIA", "Miami International Airport", 25.7959, -80.2870),
    "KATL": WeatherStation("KATL", "Hartsfield-Jackson Atlanta International Airport", 33.6407, -84.4277),
    "KSEA": WeatherStation("KSEA", "Seattle-Tacoma International Airport", 47.4502, -122.3088),
    "KAUS": WeatherStation("KAUS", "Austin-Bergstrom International Airport", 30.1975, -97.6664),
    "KHOU": WeatherStation("KHOU", "William P. Hobby Airport", 29.6454, -95.2789),
    "KDFW": WeatherStation("KDFW", "Dallas/Fort Worth International Airport", 32.8998, -97.0403),
    "KPHX": WeatherStation("KPHX", "Phoenix Sky Harbor International Airport", 33.4352, -112.0101),
    "KLAS": WeatherStation("KLAS", "Harry Reid International Airport", 36.0840, -115.1537),
    "PHNL": WeatherStation("PHNL", "Daniel K. Inouye International Airport", 21.3187, -157.9225),
}


def get_station(code: str | None) -> WeatherStation | None:
    if not code:
        return None
    return KNOWN_STATIONS.get(code.upper())
