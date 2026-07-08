# Weather Agent Rules

Domain-specific rules for analyzing Polymarket and other prediction markets resolved on weather data.

---

## Resolution Source Priority

1. **Always use the resolution source from the market description and rules first.**
2. Extract the **official station code**, **Wunderground URL**, or **named provider** from rules/description text.
3. Do not substitute a different station or provider unless rules explicitly allow it.

### Source hierarchy

| Market specifies | Primary action | Fallback |
|-----------------|----------------|----------|
| Weather Underground / Wunderground URL | Use that URL and station | Cross-check with official obs if helpful |
| NWS / NOAA / weather.gov | Use api.weather.gov for US locations | None without rule permission |
| Airport station code (e.g., KORD, KJFK, KLAX, KPHX) | Resolve station and pull official forecast | SKIP if station unresolvable |
| Ambiguous or missing | **SKIP** | — |

**US locations:** `weather.gov` / NWS API is appropriate when rules name it or for US airport stations as cross-check.

**Non-US locations:** Use global provider fallback (e.g., national met service, ICAO station databases) when US APIs do not apply.

---

## Data to Collect

When station and forecast are available, consider:

- **Max temperature** (daily high — common resolution field)
- **Hourly forecast** — timing of peak temp relative to resolution window
- **Station location** — airport vs city; elevation; urban heat effects
- **Humidity**
- **Wind** — speed and direction
- **Cloud cover**
- **Rain / precipitation probability**
- **Heat index** — when relevant to threshold markets

Use the **same metric and window** the market rules specify (e.g., daily high at a specific station on a specific date in local time).

---

## Probability Estimation

1. Map forecast distribution to the market threshold (e.g., "≥ 90°F").
2. Account for forecast uncertainty (ensemble spread, model disagreement, time to event).
3. Compare **model probability** to **market implied probability**.
4. Calculate edge and EV per `prompts/strategy.md`.

Document whether probability is **Verified** (direct from official forecast) or **Estimated** (interpolated or uncertainty-adjusted).

---

## SKIP Conditions

Return **SKIP** with a clear reason when:

- Station code or Wunderground URL cannot be extracted from rules/description
- Named resolution source is unavailable or returns no data
- Forecast is unavailable for the resolution date/location
- Rules specify a metric you cannot obtain (e.g., exact Wunderground daily summary field)
- Time zone or measurement window in rules is ambiguous

Example skip reasons:

- `"SKIP: No station code or Wunderground URL in market rules."`
- `"SKIP: Forecast unavailable for resolution date 2026-07-10 at KORD."`
- `"SKIP: Rules require Weather Underground daily high; station page unreachable."`

---

## Supported Identifiers

- **Wunderground URLs** — parse station and history/forecast pages as named in rules
- **ICAO / METAR station codes** — e.g., KORD, KJFK, EGLL, CYYZ
- **weather.gov** — US only; station metadata, observations, daily and hourly forecast

---

## Cross-Check Policy

- If rules name **Weather Underground**, Wunderground remains **primary**; NOAA/NWS is supplementary unless rules name NWS.
- If rules name **NWS/weather.gov**, use official NWS data as primary.
- Never present cross-check data as the resolution source when rules say otherwise.

---

## Output Requirements

Include in every weather analysis:

- Extracted station / URL from rules
- Resolution source (verified or missing)
- Forecast source and timestamp
- Threshold and metric from rules
- Model probability vs market implied probability
- Edge, EV, confidence, recommendation
- Key weather drivers and uncertainty (timing of peak temp, cloud cover, model spread)

Refer to `app/weather/` modules in the codebase for implementation alignment (resolution parsing, station resolver, forecast fetch).
