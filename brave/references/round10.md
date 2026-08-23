# Round-10: Rich Search API + Local POIs / Local Descriptions

Round 10 adds **three new API surfaces** to the Brave skill:

1. **Rich Search API** — `/res/v1/web/rich`
2. **Local POIs API** — `/res/v1/local/pois`
3. **Local Descriptions API** — `/res/v1/local/descriptions`

All are **verified live** against `https://api.search.brave.com` (2025-08-07).

---

## Rich Search

### The two-step flow

1. **Web search** with `enable_rich_callback=1` in the query parameters.
   The response carries a `rich` hint:
   ```json
   {"type": "rich", "hint": {"vertical": "weather",
                              "callback_key": "7bd1d377af214fd08a9764ae8dc39d9a"}}
   ```
2. **Fetch** `GET /res/v1/web/rich?callback_key=...` with the callback key
   to receive the real-time structured payload.

### Verticals (all verified live)

| subtype | key | data | provider |
|---------|-----|------|----------|
| `weather`     | `weather`         | location, current_weather, daily[8], hours3[40], alerts | OpenWeatherMap |
| `stocks`      | `stock`           | quote, asset_info, exchange_info, time_range | FMP |
| `cryptocurrency`| `cryptocurrency` | quote [price, mcap, rank, change], timeseries, top100 | CoinGecko |
| `currency`    | `currency`        | conversion query→result, rate, supported_currencies | Fixer |
| `calculator`  | `calculator`      | expression, answer | Brave |
| `definitions` | `definitions`     | word, pronunciation, part-of-speech, meanings, examples, related | Wordnik |
| `unitconversion`| `unitconversion` | amount, from/to units, dimensionality | Brave |
| `unixtimestamp`| `unixtimestamp`   | conversion intent, ts|date |
| `sports`      | `american_football`/`baseball`/`basketball`/… | league, games (teams, scores, status), standings | API Sports |
| `formula1`   | `formula1`        | calendar, next/prev race, standings | Stats Perform |
| `packagetracker`| `package_tracker` | carrier, tracking number, status, events | — |

### Python surface

```python
d = await brave.rich("weather in london")
d["vertical"]          # "weather"
d["results"][0]["data"]["current"]["temp"]        # 23.11
d["results"][0]["data"]["daily"]                # 8-day forecast

s = await brave.rich("AAPL stock")
s["results"][0]["data"]["latest_price"]         # 312.41

c = await brave.rich("100 usd to eur")
c["results"][0]["data"]["result"]               # 86.747
```

Typed one-liners:

```python
await brave.weather("los angeles")
await brave.stock_quote("TSLA")
await brave.definition("serendipity")
await brave.crypto("ethereum")
await brave.currency_x(100, "USD", "EUR")
await brave.convert_values(100, "km", "mi")    # computed client-side
await brave.unix_time(1700000000)              # computed client-side
```

`fetch=False` keeps just the hint (no second HTTP call).

---

## Local POIs (`pois(ids)`)

After a web search surfaces `locations`, take the `id` fields and fetch
the **deep business record** with `/res/v1/local/pois`.

```python
d = await brave.search("coffee shop san francisco")
ids = [l["id"] for l in d["locations"]]
p = await brave.pois(ids)
p["results"]           # [{title, address, phone, email, rating, reviews,
                       #   pictures, price_range, distance, profiles, week}]
p["render"]            # readable text
```

The POI record adds fields absent from `search()["locations"]`:
`reviews` (actual user reviews with ratings/authors), `pictures` (up to 5),
`email`, `distance`, `profiles` (website, Facebook, etc).

### Local Descriptions
`poi_descriptions(ids)` fetches AI-generated place blurbs for the same IDs.

```python
await brave.poi_descriptions(ids)
# → "Taylor Street Coffee Shop is a popular breakfast and brunch spot …"
```

---

## Enabling the rich callback in `search()` / `run()`

Set `enable_rich_callback=True` on `search()` / `run()`:

```python
d = await brave.search("weather in rome", enable_rich_callback=True)
d["rich"]       # the hint {type, hint:{vertical, callback_key}}
```

`run()` renders a `[Rich] VERTICAL — use brave.rich(...)` hint when available.
