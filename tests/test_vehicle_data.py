"""Common endpoints preserve authentication and expose the effective trip query."""

import time
import unittest
from unittest.mock import AsyncMock, MagicMock, call

from pleos_client import ApiError, AuthError, PleosClient, VehicleData


class VehicleDataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = PleosClient(MagicMock(), "kia", "fixture-id", "fixture-secret")
        self.query = {"startDate": "20261027", "endDate": "20261102"}

    async def test_snapshot_encodes_vin_and_returns_payload_and_metadata(self):
        path = "/v1/vehicles/CAR%2F%3F%20%23%C3%A9/batteries"
        payload = {"charge": {"stateOfCharge": 50}}
        metadata = {"requestId": "fixture-request"}
        self.client.get = AsyncMock(return_value=payload)
        self.client.response_meta[path] = metadata
        result = await self.client.vehicle_data("CAR/? #é", "batteries")
        self.assertEqual(result, VehicleData(payload, metadata, None))
        self.client.get.assert_awaited_once_with(path)

    async def test_common_endpoints_and_missing_metadata(self):
        for endpoint in (
            "batteries",
            "powertrains",
            "locations",
            "driving",
            "status",
            "freezer",
            "powertrains/data",
            "speed",
            "driving/data",
            "status/data",
        ):
            with self.subTest(endpoint=endpoint):
                self.client.get = AsyncMock(return_value=None)
                result = await self.client.vehicle_data("CAR", endpoint)
                self.assertEqual(result, VehicleData(None, {}, None))
                self.client.get.assert_awaited_once_with(f"/v1/vehicles/CAR/{endpoint}")

    async def test_trip_query_is_canonical_and_caller_mapping_is_unchanged(self):
        query = {"endDate": "20261102", "startDate": "20261027"}
        self.client.get = AsyncMock(return_value={"trips": []})
        result = await self.client.vehicle_data("CAR", "driving/data", query)
        self.assertEqual(result.query, self.query)
        self.assertEqual(query, {"endDate": "20261102", "startDate": "20261027"})
        self.client.get.assert_awaited_once_with(
            "/v1/vehicles/CAR/driving/data?startDate=20261027&endDate=20261102"
        )

    async def test_refused_long_range_retries_once_and_remembers_successful_limit(self):
        for status in (400, 422):
            with self.subTest(status=status):
                self.client.trip_max_days = None
                self.client.get = AsyncMock(side_effect=[ApiError(status, "4001"), {}, {}, {}])
                result = await self.client.vehicle_data("CAR", "speed", self.query)
                shorter = {"startDate": "20261101", "endDate": "20261102"}
                self.assertEqual(result.query, shorter)
                self.assertEqual(self.client.trip_max_days, 2)
                self.assertEqual(self.query["startDate"], "20261027")
                # The learned limit applies to another car and endpoint on the same client.
                repeated = await self.client.vehicle_data("CAR2", "status/data", self.query)
                self.assertEqual(repeated.query, shorter)
                one_day = {"startDate": "20261102", "endDate": "20261102"}
                self.assertEqual(
                    (await self.client.vehicle_data("CAR", "speed", one_day)).query, one_day
                )
                self.assertEqual(
                    self.client.get.await_args_list,
                    [
                        call("/v1/vehicles/CAR/speed?startDate=20261027&endDate=20261102"),
                        call("/v1/vehicles/CAR/speed?startDate=20261101&endDate=20261102"),
                        call("/v1/vehicles/CAR2/status/data?startDate=20261101&endDate=20261102"),
                        call("/v1/vehicles/CAR/speed?startDate=20261102&endDate=20261102"),
                    ],
                )

    async def test_failed_fallback_exposes_actual_query_without_learning_limit(self):
        for failure in (ApiError(404, "4045"), ApiError(429, "429"), ApiError(422, "4001")):
            with self.subTest(failure=failure):
                self.client.get = AsyncMock(side_effect=[ApiError(400, "4001"), failure])
                with self.assertRaises(ApiError) as caught:
                    await self.client.vehicle_data("CAR", "speed", self.query)
                self.assertIs(caught.exception, failure)
                self.assertEqual(failure.query, {"startDate": "20261101", "endDate": "20261102"})
                self.assertIsNone(self.client.trip_max_days)
                self.assertEqual(self.client.get.await_count, 2)

    async def test_optional_zone_is_preserved_when_successful_clipped_or_failed(self):
        for zone, encoded in (("UTC", "UTC"), ("Europe/Dublin", "Europe%2FDublin")):
            with self.subTest(zone=zone):
                query = self.query | {"zoneId": zone}
                shorter = query | {"startDate": "20261101"}
                self.client.trip_max_days = None
                self.client.get = AsyncMock(side_effect=[{}, ApiError(400, "4001"), {}, {}])
                self.assertEqual(
                    (await self.client.vehicle_data("CAR", "speed", query)).query, query
                )
                self.assertEqual(
                    (await self.client.vehicle_data("CAR", "speed", query)).query, shorter
                )
                self.assertEqual(
                    (await self.client.vehicle_data("CAR", "speed", query)).query, shorter
                )
                self.assertEqual(
                    self.client.get.await_args_list,
                    [
                        call(
                            f"/v1/vehicles/CAR/speed?startDate=20261027&endDate=20261102&zoneId={encoded}"
                        ),
                        call(
                            f"/v1/vehicles/CAR/speed?startDate=20261027&endDate=20261102&zoneId={encoded}"
                        ),
                        call(
                            f"/v1/vehicles/CAR/speed?startDate=20261101&endDate=20261102&zoneId={encoded}"
                        ),
                        call(
                            f"/v1/vehicles/CAR/speed?startDate=20261101&endDate=20261102&zoneId={encoded}"
                        ),
                    ],
                )
                self.client.trip_max_days = None
                failure = ApiError(404, "4045")
                self.client.get = AsyncMock(side_effect=[ApiError(422, "4001"), failure])
                with self.assertRaises(ApiError):
                    await self.client.vehicle_data("CAR", "speed", query)
                self.assertEqual(failure.query, shorter)
                self.assertEqual(query, self.query | {"zoneId": zone})

    async def test_unrelated_errors_and_short_ranges_are_not_retried(self):
        for query, endpoint, error in (
            (self.query, "speed", ApiError(400, "4045")),
            (self.query, "speed", ApiError(0, "connection_failed")),
            (self.query, "speed", ApiError(403, "403")),
            (self.query, "speed", ApiError(500, "500")),
            ({"startDate": "20261101", "endDate": "20261102"}, "speed", ApiError(422, "4001")),
            (None, "batteries", ApiError(400, "4001")),
        ):
            with self.subTest(query=query, error=error):
                self.client.get = AsyncMock(side_effect=error)
                with self.assertRaises(ApiError) as caught:
                    await self.client.vehicle_data("CAR", endpoint, query)
                self.assertIs(caught.exception, error)
                self.assertEqual(error.query, query)
                self.client.get.assert_awaited_once()

    async def test_authentication_failure_propagates_without_date_retry(self):
        self.client.get = AsyncMock(side_effect=AuthError("Personal key rejected"))
        with self.assertRaises(AuthError):
            await self.client.vehicle_data("CAR", "speed", self.query)
        self.client.get.assert_awaited_once()

    async def test_endpoint_request_still_renews_an_expired_token(self):
        self.client.token = "old"
        self.client._request = AsyncMock(
            side_effect=[ApiError(401, "401"), {"accessToken": "new"}, {"trips": []}]
        )
        result = await self.client.vehicle_data("CAR", "speed", self.query)
        self.assertEqual(result.data, {"trips": []})
        self.assertEqual(self.client.token, "new")
        self.assertEqual(self.client._request.await_count, 3)

    async def test_rate_limit_preserves_query_without_network_or_retry(self):
        self.client.token = "fixture-token"
        self.client.retry_at = time.monotonic() + 300
        with self.assertRaises(ApiError) as caught:
            await self.client.vehicle_data("CAR", "speed", self.query)
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.query, self.query)
        self.assertIsNone(self.client.trip_max_days)
        self.client.session.request.assert_not_called()

    async def test_invalid_endpoint_and_date_inputs_do_not_request_data(self):
        self.client.get = AsyncMock()
        for vin, endpoint, query in (
            ("", "batteries", None),
            ("..", "batteries", None),
            ("CAR", "../other", None),
            ("CAR", "speed?key=private", None),
            ("CAR", "locations", self.query),
            ("CAR", "speed", {"startDate": "20261101"}),
            ("CAR", "speed", self.query | {"extra": "private"}),
            ("CAR", "speed", self.query | {"zoneId": ""}),
            ("CAR", "speed", self.query | {"zoneId": "  "}),
            ("CAR", "speed", self.query | {"zoneId": 1}),
            ("CAR", "speed", {"startDate": "2026111", "endDate": "20261102"}),
            ("CAR", "speed", {"startDate": "２０２６１１０１", "endDate": "20261102"}),
            ("CAR", "speed", {"startDate": "2026a101", "endDate": "20261102"}),
            ("CAR", "speed", {"startDate": "20260230", "endDate": "20261102"}),
            ("CAR", "speed", {"startDate": "20261103", "endDate": "20261102"}),
        ):
            with self.subTest(vin=vin, endpoint=endpoint, query=query):
                with self.assertRaises(ValueError):
                    await self.client.vehicle_data(vin, endpoint, query)
        self.client.get.assert_not_awaited()

    def test_vehicle_list_metadata_requires_no_api_path_knowledge(self):
        self.assertEqual(self.client.vehicles_metadata, {})
        self.client.response_meta["/v1/vehicles/consent"] = {"requestId": "fixture-request"}
        self.assertEqual(self.client.vehicles_metadata, {"requestId": "fixture-request"})
