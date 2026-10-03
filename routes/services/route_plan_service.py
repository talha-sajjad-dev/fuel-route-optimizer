"""RoutePlanService — orchestrates one route-plan request.

This is an APPLICATION service, not a pure domain module like
routes/domain/fuel_optimizer.py: it coordinates other services (which do
touch Django/HTTP) by calling them through plain, injectable interfaces.
The class itself holds no Django import, no `requests` import, and no
hardcoded configuration — everything it depends on is passed into its
constructor, which is what makes it fully testable with fakes/mocks and
is what the factory module (routes/services/factory.py) exists to wire up
with real implementations read from Django settings.

    DRF view -> RoutePlanService -> {geocoding, routing, stations, geospatial, optimizer}

Responsibilities kept OUT of this class on purpose:
- HTTP concerns (status codes, serialization) — that's the view's job.
- USA-scope business validation IS done here (see
  `_validate_within_supported_scope`), not inside the providers: the
  providers are kept geographically generic/reusable, while "this
  assessment only supports USA routes" is an application-level rule.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from routes.domain.fuel_optimizer import plan_fuel_stops
from routes.domain.types import Coordinates, FuelPlan, Route, StationLocation, VehicleProfile
from routes.services.exceptions import LocationNotFoundError
from routes.services.geospatial import RouteGeospatialService
from routes.services.route_plan_exceptions import (
    RoutePlanLocationNotFoundError,
    RoutePlanLocationOutOfScopeError,
)

# Continental US bounding box (deliberately excludes Alaska/Hawaii/Canada),
# consistent with the CONUS-scoped EPSG:5070 projection decision in Phase 5.
# This is an application-level rule, not a provider concern.
US_LATITUDE_RANGE = (24.0, 50.0)
US_LONGITUDE_RANGE = (-125.0, -66.0)


class GeocodingServiceProtocol(Protocol):
    def geocode(self, location: str) -> Coordinates:
        ...


class RoutingServiceProtocol(Protocol):
    def route(self, origin: Coordinates, destination: Coordinates) -> Route:
        ...


@dataclass(frozen=True)
class RoutePlanResult:
    route: Route
    vehicle: VehicleProfile
    fuel_plan: FuelPlan


class RoutePlanService:
    def __init__(
        self,
        geocoding_service: GeocodingServiceProtocol,
        routing_service: RoutingServiceProtocol,
        station_repository: Callable[[], list[StationLocation]],
        geospatial_service: RouteGeospatialService,
        vehicle: VehicleProfile,
    ):
        self._geocoding_service = geocoding_service
        self._routing_service = routing_service
        self._station_repository = station_repository
        self._geospatial_service = geospatial_service
        self._vehicle = vehicle

    def plan(self, start: "str | Coordinates", destination: "str | Coordinates") -> RoutePlanResult:
        origin_coords = self._resolve_location(start, field="start")
        destination_coords = self._resolve_location(destination, field="destination")

        # Station coordinates/prices are never touched during the request —
        # only read, via the repository, exactly as persisted.
        route = self._routing_service.route(origin_coords, destination_coords)
        stations = self._station_repository()

        candidates = self._geospatial_service.select_candidates(
            route.geometry, stations, route.segment_distances_miles
        )

        # route.distance_miles (OSRM's authoritative figure) feeds the
        # optimizer directly — never a value derived from projected geometry.
        fuel_plan = plan_fuel_stops(self._vehicle, route.distance_miles, candidates)

        return RoutePlanResult(route=route, vehicle=self._vehicle, fuel_plan=fuel_plan)

    def _resolve_location(self, location: "str | Coordinates", field: str) -> Coordinates:
        if isinstance(location, Coordinates):
            coordinates = location
        else:
            try:
                coordinates = self._geocoding_service.geocode(location)
            except LocationNotFoundError as exc:
                raise RoutePlanLocationNotFoundError(field, str(exc)) from exc

        self._validate_within_supported_scope(coordinates, field)
        return coordinates

    @staticmethod
    def _validate_within_supported_scope(coordinates: Coordinates, field: str) -> None:
        lat_ok = US_LATITUDE_RANGE[0] <= coordinates.latitude <= US_LATITUDE_RANGE[1]
        lon_ok = US_LONGITUDE_RANGE[0] <= coordinates.longitude <= US_LONGITUDE_RANGE[1]
        if not (lat_ok and lon_ok):
            raise RoutePlanLocationOutOfScopeError(
                field, f"{field} location is outside the supported USA service area"
            )
