"""POST /api/v1/routes/plan — thin DRF view.

All orchestration lives in RoutePlanService; all provider-failure/not-found
translation happens here, at the API boundary, exactly once. This view does
not construct providers, call the optimizer, or touch the database itself.

`build_route_plan_service` is imported as a module-level name specifically
so tests can replace it with `@patch("routes.api.views.build_route_plan_service")`
without touching Django settings or real HTTP/DB — see
routes/tests/test_api_route_plan.py.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from routes.api.serializers import RoutePlanRequestSerializer, serialize_route_plan_result
from routes.services.exceptions import (
    GeocodingProviderError,
    RouteNotFoundError,
    RoutingProviderError,
)
from routes.services.factory import build_route_plan_service
from routes.services.route_plan_exceptions import (
    RoutePlanLocationNotFoundError,
    RoutePlanLocationOutOfScopeError,
)


class RoutePlanView(APIView):
    def post(self, request, *args, **kwargs):
        serializer = RoutePlanRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)  # -> 400 via DRF's default exception handler

        start = serializer.validated_data["start"]
        destination = serializer.validated_data["destination"]

        service = build_route_plan_service()

        try:
            result = service.plan(start, destination)
        except RoutePlanLocationNotFoundError as exc:
            return Response(
                {
                    "detail": f"Unable to resolve {exc.field} location.",
                    "code": "location_not_found",
                    "field": exc.field,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except RoutePlanLocationOutOfScopeError as exc:
            return Response(
                {
                    "detail": (
                        f"The {exc.field} location is outside the supported USA service area."
                    ),
                    "code": "location_out_of_scope",
                    "field": exc.field,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except RouteNotFoundError:
            return Response(
                {
                    "detail": "No route could be found between the given locations.",
                    "code": "route_not_found",
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except GeocodingProviderError:
            # Caught AFTER RoutePlanLocationNotFoundError (a distinct type,
            # not a subclass) and is itself a broader class than the
            # provider's own LocationNotFoundError, which RoutePlanService
            # already translated — so by construction, anything still
            # raising this here is a genuine provider failure, not a
            # not-found case.
            return Response(
                {
                    "detail": "The geocoding provider is currently unavailable.",
                    "code": "geocoding_provider_error",
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except RoutingProviderError:
            # RouteNotFoundError (a subclass) is caught above first.
            return Response(
                {
                    "detail": "The routing provider is currently unavailable.",
                    "code": "routing_provider_error",
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if not result.fuel_plan.feasible:
            return Response(
                {
                    "detail": result.fuel_plan.infeasibility_reason
                    or "Route is not feasible with the configured vehicle range.",
                    "code": "route_infeasible",
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        return Response(serialize_route_plan_result(result), status=status.HTTP_200_OK)
