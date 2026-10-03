import csv
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from fuel.coordinate_providers import NominatimCoordinateProvider, is_valid_coordinate
from fuel.models import FuelStation

FIXTURE_FIELDNAMES = ["normalized_key", "latitude", "longitude", "coordinate_source"]


class Command(BaseCommand):
    help = (
        "Populate FuelStation.latitude/longitude. Default path reads a "
        "pre-computed fixture file (no network calls, required for normal "
        "setup). --provider nominatim is optional and offline-only: it "
        "builds/extends that fixture file (never writes to FuelStation "
        "directly), scoped by --states/--address-contains/--limit, and is "
        "resumable. Never required to run or grade the project."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--from-file",
            type=str,
            default=None,
            help="Path to a pre-computed CSV fixture "
            "(normalized_key,latitude,longitude,coordinate_source).",
        )
        parser.add_argument(
            "--provider",
            type=str,
            choices=["file", "nominatim"],
            default="file",
            help="Enrichment source. 'file' (default) requires --from-file. "
            "'nominatim' is optional/offline-only and rate-limited.",
        )
        parser.add_argument(
            "--out-file",
            type=str,
            default=None,
            help="--provider nominatim: fixture CSV to create/extend "
            "(appended to, so reruns resume rather than restart).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="--provider nominatim: cap how many NEW stations to geocode this run.",
        )
        parser.add_argument(
            "--states",
            type=str,
            default=None,
            help="--provider nominatim: comma-separated state/province codes to "
            "scope geocoding to (e.g. 'OK,TX,NM'). Omit to consider all states.",
        )
        parser.add_argument(
            "--address-contains",
            type=str,
            default=None,
            help="--provider nominatim: only geocode stations whose address "
            "contains this substring (e.g. 'I-40'), case-insensitive.",
        )

    def handle(self, from_file, provider, out_file, limit, states, address_contains, **options):
        if provider == "file":
            if not from_file:
                raise CommandError("--from-file is required when --provider=file")
            self._enrich_from_file(from_file)
        else:
            if not out_file:
                raise CommandError("--out-file is required when --provider=nominatim")
            self._enrich_from_nominatim(out_file, limit, states, address_contains)

    # -- file-backed import (the normal setup path) --------------------

    def _enrich_from_file(self, csv_path: str):
        if not os.path.exists(csv_path):
            raise CommandError(f"Fixture file not found: {csv_path}")

        stations_by_key = {s.normalized_key: s for s in FuelStation.objects.all()}

        stats = {
            "rows_read": 0,
            "matched_stations": 0,
            "coordinates_updated": 0,
            "coordinates_unchanged": 0,
            "unmatched_identities": 0,
            "invalid_coordinates": 0,
        }
        now = timezone.now()

        with open(csv_path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            with transaction.atomic():
                for row in reader:
                    stats["rows_read"] += 1
                    key = row.get("normalized_key")
                    station = stations_by_key.get(key)
                    if station is None:
                        stats["unmatched_identities"] += 1
                        continue
                    stats["matched_stations"] += 1

                    try:
                        latitude = float(row["latitude"])
                        longitude = float(row["longitude"])
                    except (KeyError, TypeError, ValueError):
                        stats["invalid_coordinates"] += 1
                        continue

                    if not is_valid_coordinate(latitude, longitude):
                        # Never overwrite a station's existing coordinates
                        # (valid or not) with invalid fixture data.
                        stats["invalid_coordinates"] += 1
                        continue

                    already_set = (
                        station.latitude is not None
                        and abs(station.latitude - latitude) < 1e-9
                        and station.longitude is not None
                        and abs(station.longitude - longitude) < 1e-9
                    )
                    if already_set:
                        stats["coordinates_unchanged"] += 1
                        continue

                    station.latitude = latitude
                    station.longitude = longitude
                    station.coordinate_source = row.get("coordinate_source") or "precomputed_file"
                    station.coordinates_updated_at = now
                    station.save(
                        update_fields=[
                            "latitude",
                            "longitude",
                            "coordinate_source",
                            "coordinates_updated_at",
                        ]
                    )
                    stats["coordinates_updated"] += 1

        self.stdout.write(self.style.SUCCESS("Coordinate enrichment (file) complete:"))
        for key, value in stats.items():
            self.stdout.write(f"  {key}: {value}")

    # -- offline Nominatim fixture building (optional, never automatic) -

    def _enrich_from_nominatim(self, out_file, limit, states, address_contains):
        from django.conf import settings

        already_geocoded = self._read_existing_keys(out_file)
        self.stdout.write(f"Resuming: {len(already_geocoded)} keys already in {out_file}")

        queryset = FuelStation.objects.all().order_by("id")
        if states:
            state_list = [s.strip().upper() for s in states.split(",") if s.strip()]
            queryset = queryset.filter(state__in=state_list)
        if address_contains:
            queryset = queryset.filter(address__icontains=address_contains)

        targets = [s for s in queryset if s.normalized_key not in already_geocoded]
        if limit:
            targets = targets[:limit]

        self.stdout.write(f"Geocoding up to {len(targets)} station(s) this run...")

        provider = NominatimCoordinateProvider(
            base_url=settings.NOMINATIM_BASE_URL,
            user_agent=settings.NOMINATIM_USER_AGENT,
        )

        geocoded, failed = 0, 0
        file_exists = os.path.exists(out_file)
        with open(out_file, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIXTURE_FIELDNAMES)
            if not file_exists:
                writer.writeheader()

            for station in targets:
                result = provider.enrich_station(station.address, station.city, station.state)
                if result is None:
                    failed += 1
                    self.stderr.write(
                        self.style.WARNING(f"No geocode result for station {station.id}")
                    )
                    continue
                writer.writerow(
                    {
                        "normalized_key": station.normalized_key,
                        "latitude": result.latitude,
                        "longitude": result.longitude,
                        "coordinate_source": result.source,
                    }
                )
                fh.flush()  # persist progress safely, one row at a time
                geocoded += 1

        self.stdout.write(self.style.SUCCESS("Nominatim fixture extension complete:"))
        self.stdout.write(f"  geocoded: {geocoded}")
        self.stdout.write(f"  failed: {failed}")
        self.stdout.write(f"  fixture file: {out_file}")
        self.stdout.write(
            "Run 'manage.py enrich_station_coordinates --provider file "
            f"--from-file {out_file}' to import these into the database."
        )

    @staticmethod
    def _read_existing_keys(csv_path: str) -> set[str]:
        if not csv_path or not os.path.exists(csv_path):
            return set()
        with open(csv_path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            return {row["normalized_key"] for row in reader if row.get("normalized_key")}
