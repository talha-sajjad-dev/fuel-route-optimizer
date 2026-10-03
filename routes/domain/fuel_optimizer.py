"""Pure fuel-stop planning. No Django, DRF, requests, Shapely, pyproj, or
database access anywhere in this module — it operates only on
`VehicleProfile`, `StationCandidate`, and plain numbers, and is unit-tested
entirely with synthetic data.

The algorithm is deliberately split into two phases, for a reason that
matters more than it looks:

1. PHYSICAL FEASIBILITY (`_compute_destination_reachability`) — a pure
   one-dimensional reachability question: ignoring price entirely, does
   *some* chain START -> station -> station -> ... -> DESTINATION exist
   where every hop is <= max_range_miles?

2. COST OPTIMIZATION (`plan_fuel_stops`) — given that at least one feasible
   chain exists, greedily minimize purchase cost while only ever moving
   between points that are themselves part of a feasible chain.

Why not just run the cost-minimizing greedy and let it also decide
feasibility? An earlier version of this design did exactly that: "when no
cheaper station is reachable, fill the tank and drive to the *farthest*
reachable station," on the unstated assumption that the farthest reachable
station is automatically a safe choice. Two independent problems with that:

1. It conflates "farthest reachable STATION" with "farthest reachable
   POINT." The destination itself is also a point the vehicle can drive
   to, and it is never a station — a greedy that only ever looks at
   stations for its next waypoint can run out of candidates and either
   crash or misreport a perfectly feasible route as infeasible the moment
   the destination becomes the only remaining reachable point (no stations
   left, or none needed). This is a real bug this implementation hit and
   fixed during development — see `_greedy_minimum_cost_plan`'s (C1) branch
   and `test_single_required_station_completes_without_crashing`.
2. It never independently verifies the chosen station can itself continue
   to the destination. For a general routing problem that is a genuine
   risk. For THIS specific problem — one-dimensional positions and a single
   uniform `max_range_miles` for every hop — it can be shown that choosing
   the farthest reachable point can never be a worse choice than a nearer
   one for pure reachability: any point a nearer candidate could eventually
   reach is, by the same hop budget, also reachable from a farther
   candidate positioned ahead of it (the remaining distance is strictly
   smaller). The one exception is a point lying strictly BETWEEN the nearer
   and farther candidate, which the farther one has already passed —
   that's the actual mechanism by which a naive, destination-unaware
   greedy can go wrong, not "farthest is inherently unsafe."

Both issues are closed by the same fix: separate feasibility (which
candidates can reach the destination at all — computed once, up front,
over ALL candidates including the implicit destination endpoint) from cost
optimization (which only ever moves between the current position and
candidates already proven feasible, with the destination always treated as
a first-class, always-reachable-at-the-end waypoint rather than an
afterthought). See `_compute_destination_reachability` and the test suite's
`test_feasibility_excludes_a_genuine_dead_end_candidate`.
"""
from __future__ import annotations

from decimal import Decimal

from routes.domain.types import FuelPlan, FuelStop, StationCandidate, VehicleProfile

# Tolerance for floating-point distance/fuel comparisons. Physical distance
# and fuel quantities are plain floats per the agreed design (Decimal is
# reserved for money); this tolerance absorbs floating-point noise from
# upstream geospatial projection and repeated arithmetic, not the optimizer's
# own compounding error (each stop's arithmetic is exact given its inputs).
# 1e-6 miles is ~5mm and 1e-6 gallons is a fraction of a drop — both are far
# below any value that could change which station is chosen.
TOLERANCE = 1e-6


class InvalidOptimizerInputError(ValueError):
    """Raised for malformed/contradictory domain inputs (e.g. mpg <= 0, a
    station positioned beyond the route's own length). This is distinct
    from an infeasible `FuelPlan`: an invalid input is a contract violation
    by the caller, never a legitimate "no route exists" outcome, so it is
    raised rather than returned.
    """


def plan_fuel_stops(
    vehicle: VehicleProfile,
    route_distance_miles: float,
    candidates: list[StationCandidate],
) -> FuelPlan:
    """Build a minimum-cost fuel plan for a route of `route_distance_miles`,
    given `candidates` already filtered to the route corridor and sorted by
    `route_position_miles` (Phase 5's job, not this function's).

    All fuel/feasibility arithmetic in this module uses
    `StationCandidate.travel_distance_miles` exclusively — the OSRM-derived
    authoritative distance — never `route_position_miles` (which is only
    the Shapely-projected ordering coordinate Phase 5 used to sort
    `candidates` in the first place). See routes/services/geospatial.py
    for why these two are kept separate.
    """
    _validate_inputs(vehicle, route_distance_miles, candidates)

    reachable = _compute_destination_reachability(
        candidates, route_distance_miles, vehicle.max_range_miles
    )
    feasible_candidates = [c for c, ok in zip(candidates, reachable) if ok]

    if not _destination_reachable_from_start(
        feasible_candidates, route_distance_miles, vehicle.max_range_miles
    ):
        gap_reason = _describe_infeasibility(
            candidates, route_distance_miles, vehicle.max_range_miles
        )
        return FuelPlan(
            feasible=False,
            stops=[],
            starting_fuel_gallons=vehicle.starting_fuel_gallons,
            fuel_consumed_gallons=0.0,
            total_gallons_purchased=0.0,
            total_cost=Decimal("0"),
            infeasibility_reason=gap_reason,
        )

    return _greedy_minimum_cost_plan(vehicle, route_distance_miles, feasible_candidates)


# ---------------------------------------------------------------------
# Phase 1: physical feasibility (price-blind)
# ---------------------------------------------------------------------


def _compute_destination_reachability(
    candidates: list[StationCandidate], route_distance_miles: float, max_range_miles: float
) -> list[bool]:
    """For each candidate (by index), can the DESTINATION be reached from it
    through some chain of further candidates, each hop <= max_range_miles?

    Computed with a single backward scan: a candidate can reach the
    destination either directly (the remaining distance to the destination
    is within one tank) or by reaching some later candidate that itself can.
    This is plain one-dimensional reachability — no DP library, no graph
    library, just a backward loop over an already-ordered list, kept
    deliberately simple enough to read and explain in one sitting.
    """
    n = len(candidates)
    can_reach = [False] * n

    for i in range(n - 1, -1, -1):
        position_i = candidates[i].travel_distance_miles
        if route_distance_miles - position_i <= max_range_miles + TOLERANCE:
            can_reach[i] = True
            continue
        for j in range(i + 1, n):
            position_j = candidates[j].travel_distance_miles
            if position_j - position_i > max_range_miles + TOLERANCE:
                break
            if can_reach[j]:
                can_reach[i] = True
                break

    return can_reach


def _destination_reachable_from_start(
    feasible_candidates: list[StationCandidate],
    route_distance_miles: float,
    max_range_miles: float,
) -> bool:
    if route_distance_miles <= max_range_miles + TOLERANCE:
        return True
    return any(
        c.travel_distance_miles <= max_range_miles + TOLERANCE for c in feasible_candidates
    )


def _describe_infeasibility(
    candidates: list[StationCandidate], route_distance_miles: float, max_range_miles: float
) -> str:
    """Best-effort human-readable reason, pointing at the specific gap that
    exceeds max_range_miles, for debugging/API error messages."""
    positions = [0.0] + sorted(c.travel_distance_miles for c in candidates) + [route_distance_miles]
    for a, b in zip(positions, positions[1:]):
        gap = b - a
        if gap > max_range_miles + TOLERANCE:
            return (
                f"No station or destination is reachable within "
                f"{max_range_miles} miles of route position {a:.1f} "
                f"(next point is {gap:.1f} miles away)"
            )
    return "No feasible fuel plan exists for this route"


# ---------------------------------------------------------------------
# Phase 2: minimum-cost greedy over the FEASIBLE candidates only
# ---------------------------------------------------------------------


def _greedy_minimum_cost_plan(
    vehicle: VehicleProfile,
    route_distance_miles: float,
    feasible_candidates: list[StationCandidate],
) -> FuelPlan:
    """Greedy cost minimization over candidates already proven feasible.

    Each iteration evaluates exactly one purchase decision at the vehicle's
    CURRENT position (START, or a station it previously drove to), then
    picks the next waypoint to drive toward. The waypoint is either another
    station, or — critically — the DESTINATION itself. An earlier version
    of this function only ever considered stations as waypoints, which is
    a real bug, not a hypothetical one: if the destination becomes the only
    remaining reachable point (no more stations exist, or none are needed),
    failing to treat it as a selectable target makes a perfectly feasible
    route (e.g. one station, then a plain run to the finish) crash instead
    of completing. See `test_single_required_station_completes_without_crashing`
    for the regression case this guards against — it is a trivial-looking
    scenario that a naive "stations only" `ahead` set gets wrong.

    Decision order at each step, matching items 7/12 exactly:
      (A) destination reachable on fuel already in the tank -> stop, buy nothing.
      (B) a strictly cheaper station is reachable within one tank -> buy only
          enough to reach it.
      (C1) no cheaper station, but the destination is reachable within one
           tank -> buy only enough to finish (there's nothing beyond the
           destination to plan fuel for, so topping off would be an
           unnecessary purchase at a non-minimal price).
      (C2) no cheaper station, destination not yet in range -> fill the tank
           completely (this price is the best available before running out
           of options) and drive to the farthest reachable station.
    """
    mpg = vehicle.mpg
    max_range = vehicle.max_range_miles
    tank_capacity = vehicle.tank_capacity_gallons

    position = 0.0
    fuel = vehicle.starting_fuel_gallons
    current_price: Decimal | None = None  # None == "at START, nothing to compare against"
    current_station_id: int | None = None
    current_distance_from_route: float = 0.0
    remaining = list(feasible_candidates)
    stops: list[FuelStop] = []

    # Defensive bound only — overall feasibility is already proven before
    # this loop runs, and each iteration strictly consumes at least one
    # candidate from `remaining` (see the position filter at the loop's
    # end) unless it terminates by reaching the destination, so this can
    # only trip if that invariant is somehow broken.
    max_iterations = len(feasible_candidates) + 2

    for _ in range(max_iterations):
        fuel = _clamp(fuel, 0.0, tank_capacity)

        # (A)
        if route_distance_miles - position <= fuel * mpg + TOLERANCE:
            return _build_plan(vehicle, route_distance_miles, stops)

        station_ahead = [
            c
            for c in remaining
            if c.travel_distance_miles > position + TOLERANCE
            and c.travel_distance_miles <= position + max_range + TOLERANCE
        ]

        if current_price is None:
            cheaper = station_ahead
        else:
            cheaper = [c for c in station_ahead if c.price_per_gallon < current_price]

        destination_within_one_tank = route_distance_miles - position <= max_range + TOLERANCE

        if cheaper:
            # (B)
            nearest_cheaper = min(cheaper, key=lambda c: (c.travel_distance_miles, c.station_id))
            station = _resolve_co_located(station_ahead, nearest_cheaper)
            target_position = station.travel_distance_miles
            fuel_required = (target_position - position) / mpg
            purchase = max(0.0, fuel_required - fuel)
            moving_to_destination = False
        elif destination_within_one_tank:
            # (C1) — strictly cheaper is not an option, so buy exactly what
            # the final leg costs at the current (best-available) price.
            fuel_required = (route_distance_miles - position) / mpg
            purchase = max(0.0, fuel_required - fuel)
            station = None
            moving_to_destination = True
        elif station_ahead:
            # (C2)
            farthest = max(station_ahead, key=lambda c: (c.travel_distance_miles, c.station_id))
            station = _resolve_co_located(station_ahead, farthest)
            target_position = station.travel_distance_miles
            fuel_required = (target_position - position) / mpg
            purchase = max(0.0, tank_capacity - fuel)
            moving_to_destination = False
        else:
            # Cannot happen: feasibility was proven before this loop started.
            # A defensive exception beats silently returning a wrong plan.
            raise AssertionError(
                "fuel optimizer found no reachable feasible candidate despite "
                "proven overall feasibility — this indicates a bug, not a "
                "legitimately infeasible route"
            )

        purchase = _clamp_small(purchase)
        fuel_after = _clamp(fuel + purchase - fuel_required, 0.0, tank_capacity)

        # The purchase happens AT the position we are currently sitting at
        # (a real station) — never at START, which has no price and is
        # never recorded as a stop.
        if purchase > TOLERANCE and current_price is not None:
            stops.append(
                FuelStop(
                    station_id=current_station_id,
                    route_position_miles=position,
                    distance_from_route_miles=current_distance_from_route,
                    price_per_gallon=current_price,
                    fuel_before_gallons=fuel,
                    fuel_purchased_gallons=purchase,
                    fuel_after_gallons=fuel_after,
                    fuel_cost=(Decimal(str(purchase)) * current_price),
                )
            )

        fuel = fuel_after

        if moving_to_destination:
            position = route_distance_miles
            continue  # next loop's (A) check will now trivially succeed

        position = station.travel_distance_miles
        current_price = station.price_per_gallon
        current_station_id = station.station_id
        current_distance_from_route = station.distance_from_route_miles
        # Station reuse guard (item 13): once passed, a candidate (and any
        # candidate at or behind the new position, including ones skipped
        # over this iteration) can never be reconsidered. Position is
        # strictly non-decreasing each iteration, which combined with this
        # filter guarantees termination.
        remaining = [c for c in remaining if c.travel_distance_miles > position + TOLERANCE]

    raise AssertionError("fuel optimizer exceeded its iteration safety bound")


def _resolve_co_located(
    station_ahead: list[StationCandidate], target: StationCandidate
) -> StationCandidate:
    """Duplicate-position guard (item 14): if more than one candidate sits
    at (effectively) the same route position as `target`, treat them as the
    same physical stop and use whichever is cheapest — the vehicle can
    choose which pump to use at a single location."""
    target_position = target.travel_distance_miles
    co_located = [
        c for c in station_ahead if abs(c.travel_distance_miles - target_position) <= TOLERANCE
    ]
    return min(co_located, key=lambda c: (c.price_per_gallon, c.station_id))


def _build_plan(
    vehicle: VehicleProfile, route_distance_miles: float, stops: list[FuelStop]
) -> FuelPlan:
    fuel_consumed = route_distance_miles / vehicle.mpg
    total_gallons_purchased = sum(s.fuel_purchased_gallons for s in stops)
    total_cost = sum((s.fuel_cost for s in stops), Decimal("0"))
    return FuelPlan(
        feasible=True,
        stops=stops,
        starting_fuel_gallons=vehicle.starting_fuel_gallons,
        fuel_consumed_gallons=fuel_consumed,
        total_gallons_purchased=total_gallons_purchased,
        total_cost=total_cost,
    )


# ---------------------------------------------------------------------
# Validation and small numeric helpers
# ---------------------------------------------------------------------


def _validate_inputs(
    vehicle: VehicleProfile, route_distance_miles: float, candidates: list[StationCandidate]
) -> None:
    if route_distance_miles < 0:
        raise InvalidOptimizerInputError(
            f"route_distance_miles must be >= 0, got {route_distance_miles}"
        )
    if vehicle.mpg <= 0:
        raise InvalidOptimizerInputError(f"mpg must be > 0, got {vehicle.mpg}")
    if vehicle.max_range_miles <= 0:
        raise InvalidOptimizerInputError(
            f"max_range_miles must be > 0, got {vehicle.max_range_miles}"
        )
    if vehicle.tank_capacity_gallons <= 0:
        raise InvalidOptimizerInputError(
            f"tank_capacity_gallons must be > 0, got {vehicle.tank_capacity_gallons}"
        )
    if vehicle.starting_fuel_gallons < 0:
        raise InvalidOptimizerInputError(
            f"starting_fuel_gallons must be >= 0, got {vehicle.starting_fuel_gallons}"
        )
    if vehicle.starting_fuel_gallons > vehicle.tank_capacity_gallons + TOLERANCE:
        raise InvalidOptimizerInputError(
            "starting_fuel_gallons "
            f"({vehicle.starting_fuel_gallons}) cannot exceed tank_capacity_gallons "
            f"({vehicle.tank_capacity_gallons})"
        )

    for c in candidates:
        if c.travel_distance_miles < 0:
            raise InvalidOptimizerInputError(
                f"station {c.station_id} has a negative travel_distance_miles: "
                f"{c.travel_distance_miles}"
            )
        if c.travel_distance_miles > route_distance_miles + TOLERANCE:
            raise InvalidOptimizerInputError(
                f"station {c.station_id} travel_distance_miles "
                f"({c.travel_distance_miles}) exceeds route_distance_miles "
                f"({route_distance_miles})"
            )
        if c.price_per_gallon is None or c.price_per_gallon <= 0:
            raise InvalidOptimizerInputError(
                f"station {c.station_id} has an invalid price_per_gallon: "
                f"{c.price_per_gallon!r}"
            )


def _clamp(value: float, lo: float, hi: float) -> float:
    """Hard-clamp to [lo, hi]. Any value landing outside this range by more
    than TOLERANCE indicates an arithmetic bug upstream, not a legitimate
    physical state — this function absorbs only floating-point noise."""
    return max(lo, min(hi, value))


def _clamp_small(value: float) -> float:
    return 0.0 if abs(value) < TOLERANCE else value
