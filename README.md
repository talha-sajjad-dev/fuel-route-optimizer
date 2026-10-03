# Fuel Route Optimizer

A Django + DRF backend that, given a start and destination, returns a real
driving route and the minimum-cost sequence of fuel stops a vehicle needs to
make the trip — built against the supplied OPIS fuel-price CSV, with a real
enriched demo corridor (Oklahoma City → Albuquerque via I-40) to show it
working end to end.

## Contents

- [Architecture](#architecture)
- [Setup](#setup)
- [Database initialization](#database-initialization)
- [Station coordinates: why offline, not live geocoding](#station-coordinates-why-offline-not-live-geocoding)
- [API: `POST /api/v1/routes/plan`](#api-post-apiv1routesplan)
- [Vehicle assumptions](#vehicle-assumptions)
- [Fuel optimization logic](#fuel-optimization-logic)
- [Caching](#caching)
- [Error codes](#error-codes)
- [Demo route](#demo-route)
- [Postman collection](#postman-collection)
- [Running tests](#running-tests)
- [Known limitations](#known-limitations)

## Architecture

```
POST /api/v1/routes/plan
      ↓
DRF serializer            — validates the request; each location is independently
                             a place-name string or {latitude, longitude}
      ↓
RoutePlanService           — pure orchestration, no Django/HTTP import in the class itself
      ↓            ↓                    ↓                      ↓
CachedGeocodingService  CachedRoutingService   FuelStation repository   RouteGeospatialService
      ↓                      ↓                      ↓                      ↓
NominatimGeocodingProvider  OSRMRoutingProvider   (DB read only —        Shapely/pyproj corridor
(skipped for coordinate       (exactly ONE call      never geocoded        filtering + authoritative
 inputs)                     per uncached pair)      at request time)      travel-distance math
                                                                              ↓
                                                                        plan_fuel_stops (pure optimizer)
```

Each layer has exactly one job and doesn't reach into the others:
- The **optimizer** (`routes/domain/fuel_optimizer.py`) is plain Python — no Django, no HTTP, no Shapely. It only ever sees `StationCandidate.travel_distance_miles` (an OSRM-derived, authoritative distance), never the Shapely-projected `route_position_miles` used purely for ordering/corridor math.
- The **geospatial service** (`routes/services/geospatial.py`) does coordinate math only — no HTTP, no ORM.
- The **providers** (`routes/services/geocoding.py`, `routing.py`) know nothing about `FuelStation` or the database.
- **Station coordinates are read from the database, never geocoded during a request.**
- **OSRM is called at most once per request** (not once per candidate station).
- Money (`price_per_gallon`, `fuel_cost`, `total_cost`) is `Decimal` end to end, serialized as strings — never floated.

## Setup

```bash
git clone <this-repo>
cd fuel-route-optimizer
cp .env.example .env
docker compose up --build
```

This starts Postgres and the Django dev server (`http://localhost:8000`).
No Nominatim/OSRM calls happen during startup or `docker compose up` itself —
only once you POST a request.

## Database initialization

Run once, in order, against the running `web` container:

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py import_fuel_prices data/fuel-prices.csv
docker compose exec web python manage.py enrich_station_coordinates --provider file --from-file data/station_coordinates.csv
```

All three are **safe to rerun**: `import_fuel_prices` dedupes by a canonical
(station identity, price) hash, and `enrich_station_coordinates` only
updates a station's coordinates if they've actually changed — rerunning the
full sequence against an already-initialized database updates nothing and
creates nothing new.

```
rows_read: 8151   stations_created: 6738   prices_created: 8023   (first run)
rows_read: 8151   stations_created: 0      prices_created: 0      (rerun)
```

## Station coordinates: why offline, not live geocoding

The supplied CSV has 6,738 stations and **none** come with coordinates.
Live-geocoding all of them against free Nominatim (rate-limited to 1
req/sec) would take hours and isn't something normal setup should ever
require. Instead:

- `data/station_coordinates.csv` is a small, **committed demo coordinate
  fixture** — covering 87 real CSV stations along I-40 through OK/TX/NM,
  generated once offline and checked into the repo like any other fixture.
  It exists to make the demo reproducible, not as a claim of production-grade
  station geolocation (see the disclaimer below).
- The normal setup path (`enrich_station_coordinates --provider file`,
  above) only ever reads this file — zero network calls.
- Building/extending the fixture is a separate, explicit, rate-limited,
  resumable command that never touches the database directly:
  ```bash
  python manage.py enrich_station_coordinates \
    --provider nominatim --out-file data/station_coordinates.csv \
    --states OK,TX,NM --address-contains "I-40"
  ```
- **The runtime API never calls Nominatim for a station, ever.** Station
  geocoding has no code path in the request flow at all.

**Coordinate quality disclaimer:** the source CSV's addresses are
highway-exit descriptions ("I-40, EXIT 158"), not mailable street
addresses, so most don't resolve directly. 83 of the 87 enriched stations
fall back to **city-center precision** (`coordinate_source =
nominatim_city_fallback`); only 4 resolved to the literal address
(`coordinate_source = nominatim`). These are reproducible,
assessment/demo-appropriate coordinates — **not** a claim of exact station
geolocation, and not production-grade. One station (Choctaw, OK) geocoded
to the wrong town entirely and is simply excluded by the 5-mile route
corridor check, which is that filter correctly rejecting a bad geocode, not
a bug. Enrichment (like the EPSG:5070 projection in the geospatial service)
is **CONUS-scoped** — the 112 Canadian CSV rows are intentionally left
unenriched.

## API: `POST /api/v1/routes/plan`

`start` and `destination` are each **independently** either a place-name
string (geocoded) or a `{"latitude", "longitude"}` object (used as-is,
never geocoded) — you can mix the two in one request.

```json
{
  "start": {"latitude": 35.4730, "longitude": -97.5171},
  "destination": {"latitude": 35.0841, "longitude": -106.6510}
}
```

```bash
curl -X POST http://localhost:8000/api/v1/routes/plan \
  -H "Content-Type: application/json" \
  -d '{
    "start": {"latitude": 35.4730, "longitude": -97.5171},
    "destination": {"latitude": 35.0841, "longitude": -106.6510}
  }'
```

**Response — `200 OK`** (this is the real demo route's actual response):

```json
{
  "route": {
    "distance_miles": 544.6851015071979,
    "duration_minutes": 561.815,
    "geometry": { "type": "LineString", "coordinates": ["... 4,951 real road points ..."] }
  },
  "vehicle": {
    "mpg": 10.0,
    "max_range_miles": 500.0,
    "tank_capacity_gallons": 50.0,
    "starting_fuel_gallons": 50.0
  },
  "fuel": {
    "feasible": true,
    "starting_fuel_gallons": 50.0,
    "fuel_consumed_gallons": 54.47,
    "total_gallons_purchased": 4.47,
    "total_cost": "12.82"
  },
  "stops": [
    {
      "station_id": 3628,
      "route_position_miles": 258.05,
      "distance_from_route_miles": 0.98,
      "price_per_gallon": "2.86900000",
      "fuel_before_gallons": 24.2,
      "fuel_purchased_gallons": 4.47,
      "fuel_after_gallons": 0.0,
      "fuel_cost": "12.82"
    }
  ]
}
```

**`fuel_consumed_gallons` (54.47) is not the same as
`total_gallons_purchased` (4.47) — this is intentional, not a bug.** The
vehicle starts with a full 50-gallon tank, so most of the trip's fuel comes
from what it already had; the optimizer only ever buys the gap between
what's left in the tank and what's needed, at the cheapest price reachable.
`stops` can legitimately be `[]` on a feasible plan — the destination
itself is never a fuel stop, and a route shorter than the vehicle's range
needs no stop at all.

`route_position_miles` on a stop is the station's **authoritative**
OSRM-derived distance along the route — never the raw Shapely-projected
ordering coordinate used internally for corridor filtering (see
Architecture above). Money fields are decimal **strings**
(`price_per_gallon` at 8 places, matching how prices are stored;
`fuel_cost`/`total_cost` at 2).

## Vehicle assumptions

| Constant | Value | Configurable via |
|---|---|---|
| Fuel efficiency | 10 MPG | `VEHICLE_MPG` |
| Maximum range | 500 miles | `VEHICLE_MAX_RANGE_MILES` |
| Tank capacity | 50 gallons | `VEHICLE_TANK_CAPACITY_GALLONS` (defaults to `max_range / mpg`) |
| Starting fuel | Full tank | Not independently configurable — it's a trip-start assumption, not a vehicle constant |

## Fuel optimization logic

A two-phase, pure, deterministic algorithm (`routes/domain/fuel_optimizer.py`):

1. **Feasibility** — a one-dimensional reachability scan (no DP/graph
   library): can the destination be reached at all through some chain of
   stations where every hop is ≤ 500 miles? If not, the route is reported
   infeasible with a specific reason, not silently approximated.
2. **Cost minimization** — greedy, over only the stations proven feasible:
   at each stop, if the destination is reachable on the fuel already in the
   tank, stop (buy nothing); else, find the nearest strictly-cheaper
   reachable station and buy just enough fuel to reach it; else, fill the
   tank completely (the current price is the best available before running
   out of options) and drive to the farthest reachable station.

This is the standard "gas station problem" greedy, with one real
correction made during development: the destination itself must be treated
as a reachable waypoint, not just stations — a naive version that only
ever considered stations could crash or misreport a perfectly feasible
route the moment the destination became the only point left to reach.

## Caching

Two small Postgres tables (`GeocodeCacheEntry`, `RouteCacheEntry`) — no
Redis. A repeated location string resolves without a second Nominatim call;
a repeated `(origin, destination)` pair reuses the same route without a
second OSRM call. Cache keys are SHA-256 hashes of a schema version +
normalized input, so bumping `GEOCODE_CACHE_SCHEMA_VERSION` /
`ROUTE_CACHE_SCHEMA_VERSION` invalidates old entries without a data
migration. A failed provider call is never cached.

| Request shape | Geocoding calls (cache miss) | Routing calls (cache miss) |
|---|---|---|
| coordinate / coordinate | 0 | 1 |
| address / address | up to 2 | 1 |
| Any repeat of an identical request | 0 | 0 |

Station data is always a plain DB read — never geocoded or routed
per-station, no matter how many stations are near the route.

## Error codes

| Status | `code` | Meaning |
|---|---|---|
| 400 | *(DRF field errors)* | Request failed validation |
| 422 | `location_not_found` | Geocoder found no match (`field` says which) |
| 422 | `location_out_of_scope` | Resolved location is outside the supported USA service area |
| 422 | `route_not_found` | OSRM found no route between the resolved points |
| 422 | `route_infeasible` | Route is physically impossible for the configured vehicle range |
| 502 | `geocoding_provider_error` | Nominatim timed out, errored, or returned something unparseable |
| 502 | `routing_provider_error` | OSRM timed out, errored, or returned something unparseable |

Every error is translated at the API boundary (`routes/api/views.py`) — no
response ever contains a raw `requests` exception, a provider URL, or a
traceback.

## Demo route

**Oklahoma City, OK → Albuquerque, NM** via I-40 — a real ~545-mile drive
(confirmed against live OSRM), chosen because it's the highest-density real
corridor in the supplied CSV and genuinely exceeds the 500-mile vehicle
range, so the demo needs a real fuel stop without bending any assumption to
force one. See the coordinates in the API example above, or use the
Postman collection below.

## Postman collection

[`postman/fuel-route-optimizer.postman_collection.json`](postman/fuel-route-optimizer.postman_collection.json) —
import into Postman, set the `base_url` variable (defaults to
`http://localhost:8000`), and run in order:

1. **Coordinate/coordinate** — the demo route above.
2. **Address/address** — the same route geocoded from place names.
3. **Validation failure** — a missing field, expects 400.
4. **Cached repeat** — resend request 1, demonstrates the cache (same
   result, no second OSRM call).

## Running tests

```bash
docker compose exec web python -m pytest -q
docker compose exec web python manage.py check
docker compose exec web python manage.py makemigrations --check
docker compose exec web ruff check .
```

No test requires a live Nominatim or OSRM endpoint — every provider/API
test mocks `requests.get`. (Nothing stops you from hitting the real public
endpoints manually, as the demo above does.)

## Known limitations

- **CONUS-only**: the EPSG:5070 projection and the enriched demo corridor
  both assume continental US routes; Alaska, Hawaii, and the 112 Canadian
  CSV rows are out of scope.
- **87 of 6,738 stations are enriched** with coordinates, along one demo
  corridor — this is intentional (see above), not a partial failure.
  Extending coverage means re-running `enrich_station_coordinates
  --provider nominatim` with a different `--states`/`--address-contains`
  scope, not a code change.
- City-fallback coordinates (83 of the 87) are town-center precision, not
  exact station locations.
- The real 8,151-row CSV import adds real time (~30-90s depending on the
  database backend) to the one test file that exercises it
  (`routes/tests/test_real_demo_corridor.py`); everything else runs in
  under a few seconds.
