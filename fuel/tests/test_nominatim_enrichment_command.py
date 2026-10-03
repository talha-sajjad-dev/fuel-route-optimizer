import csv
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from fuel.models import FuelStation
from routes.tests.helpers import make_response

IMPORT_FIXTURE = Path(__file__).parent / "fixtures" / "sample_fuel_prices.csv"


def nominatim_ok(lat, lon):
    return make_response(200, json_body=[{"lat": str(lat), "lon": str(lon)}])


@pytest.mark.django_db
def test_nominatim_path_requires_out_file():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("enrich_station_coordinates", "--provider", "nominatim")


@pytest.mark.django_db
@patch("fuel.coordinate_providers.requests.get")
def test_nominatim_path_writes_fixture_not_database(mock_get, tmp_path):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))
    mock_get.return_value = nominatim_ok(36.5, -95.2)

    out_file = tmp_path / "station_coordinates.csv"
    call_command(
        "enrich_station_coordinates",
        "--provider", "nominatim",
        "--out-file", str(out_file),
        "--limit", "1",
    )

    # the fixture file was written...
    assert out_file.exists()
    with open(out_file) as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["coordinate_source"] == "nominatim"

    # ...but FuelStation itself was NOT touched — this path never writes
    # to the database directly, only to the fixture file.
    assert FuelStation.objects.filter(latitude__isnull=False).count() == 0


@pytest.mark.django_db
@patch("fuel.coordinate_providers.requests.get")
def test_nominatim_path_respects_limit(mock_get, tmp_path):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))
    mock_get.return_value = nominatim_ok(36.5, -95.2)

    out_file = tmp_path / "station_coordinates.csv"
    call_command(
        "enrich_station_coordinates",
        "--provider", "nominatim",
        "--out-file", str(out_file),
        "--limit", "2",
    )

    with open(out_file) as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2


@pytest.mark.django_db
@patch("fuel.coordinate_providers.requests.get")
def test_nominatim_path_resumes_skipping_already_geocoded_keys(mock_get, tmp_path):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))
    out_file = tmp_path / "station_coordinates.csv"

    mock_get.return_value = nominatim_ok(36.5, -95.2)
    call_command(
        "enrich_station_coordinates",
        "--provider", "nominatim",
        "--out-file", str(out_file),
        "--limit", "1",
    )
    with open(out_file) as fh:
        first_run_keys = {row["normalized_key"] for row in csv.DictReader(fh)}
    assert len(first_run_keys) == 1

    # second run with a higher limit must not re-request the already-done key
    mock_get.reset_mock()
    mock_get.return_value = nominatim_ok(37.0, -96.0)
    call_command(
        "enrich_station_coordinates",
        "--provider", "nominatim",
        "--out-file", str(out_file),
        "--limit", "1",
    )

    with open(out_file) as fh:
        all_rows = list(csv.DictReader(fh))
    all_keys = {row["normalized_key"] for row in all_rows}
    assert len(all_rows) == 2
    assert first_run_keys.issubset(all_keys)
    # the originally-geocoded row's coordinates must be untouched (no
    # duplicate/overwritten row for the same key)
    first_key = next(iter(first_run_keys))
    matching = [r for r in all_rows if r["normalized_key"] == first_key]
    assert len(matching) == 1


@pytest.mark.django_db
@patch("fuel.coordinate_providers.requests.get")
def test_nominatim_path_filters_by_states(mock_get, tmp_path):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))
    mock_get.return_value = nominatim_ok(36.5, -95.2)

    out_file = tmp_path / "station_coordinates.csv"
    call_command(
        "enrich_station_coordinates",
        "--provider", "nominatim",
        "--out-file", str(out_file),
        "--states", "TX",  # none of the sample fixture's stations are in TX
    )

    with open(out_file) as fh:
        rows = list(csv.DictReader(fh))
    assert rows == []
    assert mock_get.call_count == 0


@pytest.mark.django_db
@patch("fuel.coordinate_providers.requests.get")
def test_nominatim_path_rejects_out_of_range_result(mock_get, tmp_path):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))
    mock_get.return_value = make_response(200, json_body=[{"lat": "200.0", "lon": "-95.2"}])

    out_file = tmp_path / "station_coordinates.csv"
    call_command(
        "enrich_station_coordinates",
        "--provider", "nominatim",
        "--out-file", str(out_file),
        "--limit", "1",
    )

    with open(out_file) as fh:
        rows = list(csv.DictReader(fh))
    assert rows == []  # invalid result never written to the fixture
