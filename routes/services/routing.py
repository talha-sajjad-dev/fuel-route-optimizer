"""Routing provider abstraction.

Exactly one OSRM call per uncached (origin, destination) pair — station
candidate selection (Phase 5) and fuel optimization (Phase 6) never trigger
additional routing calls; they operate on the single Route this returns.
"""
from __future__ import annotations

from typing import Protocol

import requests

from routes.domain.types import Coordinates, Route
from routes.services.exceptions import RouteNotFoundError, RoutingProviderError

METERS_PER_MILE = 1609.344


class RoutingProvider(Protocol):
    def route(self, origin: Coordinates, destination: Coordinates) -> Route:
        ...


class OSRMRoutingProvider:
    """OSRM implementation using the public `/route/v1/{profile}/...` API.

    Requests full-resolution GeoJSON geometry (`overview=full`,
    `geometries=geojson`) in a single call, and validates the response
    thoroughly before constructing a `Route` — callers (including the cache
    layer in Phase 4) only ever see a valid `Route` or a raised exception,
    never a partially-malformed payload.
    """

    def __init__(self, base_url: str, profile: str = "driving", timeout_seconds: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.profile = profile
        self.timeout_seconds = timeout_seconds

    def route(self, origin: Coordinates, destination: Coordinates) -> Route:
        url = (
            f"{self.base_url}/route/v1/{self.profile}/"
            f"{origin.longitude},{origin.latitude};"
            f"{destination.longitude},{destination.latitude}"
        )
        try:
            response = requests.get(
                url,
                params={
                    "overview": "full",
                    "geometries": "geojson",
                    "annotations": "distance",
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.exceptions.Timeout as exc:
            raise RoutingProviderError("OSRM request timed out") from exc
        except requests.exceptions.RequestException as exc:
            raise RoutingProviderError(f"OSRM request failed: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise RoutingProviderError("OSRM returned malformed JSON") from exc

        if not isinstance(data, dict):
            raise RoutingProviderError("OSRM returned an unexpected response shape")

        code = data.get("code")
        if code == "NoRoute":
            raise RouteNotFoundError("OSRM found no route between the given points")
        if code != "Ok":
            raise RoutingProviderError(f"OSRM returned non-Ok code: {code!r}")

        routes = data.get("routes")
        if not routes or not isinstance(routes, list):
            raise RouteNotFoundError("OSRM response contained no routes")

        route_data = routes[0]

        try:
            distance_meters = float(route_data["distance"])
            duration_seconds = float(route_data["duration"])
            geometry = route_data["geometry"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RoutingProviderError(
                "OSRM route is missing distance, duration, or geometry"
            ) from exc

        if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
            raise RoutingProviderError("OSRM geometry is missing or not a LineString")

        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            raise RoutingProviderError("OSRM geometry has empty or insufficient coordinates")

        segment_distances_miles = self._extract_segment_distances(route_data, len(coordinates))

        return Route(
            distance_miles=distance_meters / METERS_PER_MILE,
            duration_minutes=duration_seconds / 60.0,
            geometry=geometry,
            segment_distances_miles=segment_distances_miles,
        )

    @staticmethod
    def _extract_segment_distances(route_data: dict, coordinate_count: int) -> list[float]:
        """Authoritative per-segment road distances, one entry between each
        consecutive pair of geometry coordinates — requested via
        `annotations=distance` on the SAME single route call (no extra HTTP
        request). This is what lets travel distance along the route be
        computed without a route-wide scale factor and without a routing
        call per station (see routes/services/geospatial.py).
        """
        legs = route_data.get("legs")
        if not isinstance(legs, list) or not legs:
            raise RoutingProviderError("OSRM route is missing legs/annotation distance data")

        segment_meters: list[float] = []
        for leg in legs:
            annotation = leg.get("annotation") if isinstance(leg, dict) else None
            distances = annotation.get("distance") if isinstance(annotation, dict) else None
            if not isinstance(distances, list):
                raise RoutingProviderError(
                    "OSRM route is missing legs[].annotation.distance"
                )
            try:
                segment_meters.extend(float(d) for d in distances)
            except (TypeError, ValueError) as exc:
                raise RoutingProviderError(
                    "OSRM route has non-numeric segment distances"
                ) from exc

        if len(segment_meters) != coordinate_count - 1:
            raise RoutingProviderError(
                f"OSRM segment distance count ({len(segment_meters)}) does not match "
                f"geometry coordinate count ({coordinate_count}) minus one"
            )

        return [m / METERS_PER_MILE for m in segment_meters]
