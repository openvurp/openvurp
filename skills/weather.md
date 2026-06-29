---
name: weather
description: "Current weather and forecasts via wttr.in. Use when: user asks about weather, temperature, rain, or forecasts for any location. NOT for: historical data, severe alerts, aviation/marine weather. No API key needed."
homepage: https://wttr.in/:help
triggers: [meteo, tempo, pioggia, temperatura, weather, forecast, rain, vento, neve]
always: false
metadata:
  openvurp:
    emoji: "🌤️"
    requires:
      bins: [curl]
---

# Weather

Get current weather conditions and forecasts via wttr.in.

## When to Use

✅ **USE this skill when:**

- "Che tempo fa?" / "What's the weather?"
- "Pioverà oggi/domani?"
- "Temperatura a Roma/Milano/..."
- "Previsioni per la settimana"
- Weather checks for travel planning

## When NOT to Use

❌ **DON'T use this skill when:**

- Historical weather data → weather archives
- Climate trends analysis → specialized data sources
- Severe weather alerts → official meteorological services
- Aviation/marine weather → METAR, specialized services

## Commands

### Current Weather

```bash
# One-line summary
curl -s "wttr.in/Roma?format=3"

# Detailed current conditions
curl -s "wttr.in/Roma?0"

# Custom format
curl -s "wttr.in/Roma?format=%l:+%c+%t+(percepita+%f),+vento+%w,+umidità+%h"
```

### Forecasts

```bash
# 3-day forecast
curl -s "wttr.in/Roma"

# Week forecast (detailed)
curl -s "wttr.in/Roma?format=v2"

# Specific day (0=today, 1=tomorrow, 2=day after)
curl -s "wttr.in/Roma?1"
```

### Format Codes

- `%c` — Condition emoji
- `%t` — Temperature
- `%f` — "Feels like"
- `%w` — Wind
- `%h` — Humidity
- `%p` — Precipitation
- `%l` — Location

### JSON Output

```bash
curl -s "wttr.in/Roma?format=j1" | python3 -c "
import sys, json
d = json.load(sys.stdin)
c = d['current_condition'][0]
print(f\"Temp: {c['temp_C']}°C, Feels: {c['FeelsLikeC']}°C\")
print(f\"Wind: {c['windspeedKmph']} km/h, Humidity: {c['humidity']}%\")
print(f\"Desc: {c['weatherDesc'][0]['value']}\")
"
```

## Guardrails

- No API key needed (uses wttr.in)
- Rate limited — don't spam requests
- Works for most global cities
- Supports airport codes: `curl wttr.in/FCO`
- Always include a city/location in queries
