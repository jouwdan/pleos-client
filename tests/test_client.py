"""Personal-token handling and HTTP response validation in the API client."""

import asyncio
import json
import time
import unittest
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock

import aiohttp

from pleos_client import ApiError, AuthError, PleosClient


class ApiClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = PleosClient(MagicMock(), "kia", "fixture-id", "fixture-secret")

    def response(self, body, status=200, headers=None):
        response = MagicMock(status=status, headers=headers or {})

        async def chunks(size):
            yield body

        response.content.iter_chunked = chunks
        self.client.session.request.return_value.__aenter__.return_value = response
        return response

    async def test_invalid_response_shapes_and_private_error_text(self):
        for body in (
            b"not-json",
            b"\xff",
            b"[]",
            b'{"meta": [1]}',
            b'{"error": "fixture-secret-and-vin"}',
            b'{"error": {"code": "fixture-secret-and-vin"}}',
        ):
            with self.subTest(body=body):
                self.response(body)
                with self.assertRaises(ApiError) as caught:
                    await self.client._request("/v1/auth/personal-token", self.client.credentials)
                self.assertNotIn("fixture-secret", str(caught.exception))
                self.assertIn(caught.exception.code, ("invalid_response", "unknown"))

    async def test_request_metadata_and_session_ownership(self):
        self.client.token = "fixture-token"
        payload = {
            "data": {"charge": 50},
            "meta": {
                "code": "200",
                "success": True,
                "requestId": "fixture-request",
                "ignored": "private",
            },
        }
        self.response(json.dumps(payload).encode())
        self.assertEqual(
            await self.client.get("/v1/vehicles/fixture/speed?startDate=fixture"), {"charge": 50}
        )
        self.assertEqual(
            self.client.response_meta,
            {
                "/v1/vehicles/fixture/speed": {
                    "code": "200",
                    "success": True,
                    "requestId": "fixture-request",
                }
            },
        )
        call = self.client.session.request.call_args
        self.assertEqual(
            call.args, ("GET", "https://api.pleos.ai/v1/vehicles/fixture/speed?startDate=fixture")
        )
        self.assertEqual(
            call.kwargs["headers"], {"Brand": "kia", "Authorization": "Bearer fixture-token"}
        )
        self.assertEqual(call.kwargs["timeout"].total, 30)
        self.assertFalse(call.kwargs["allow_redirects"])
        self.client.session.close.assert_not_called()

    async def test_invalid_vehicle_lists(self):
        for data in (
            None,
            [],
            {},
            {"vehicles": {}},
            {"vehicles": [None]},
            {"vehicles": [{"vin": ""}]},
            {"vehicles": [{"vin": 1}]},
        ):
            with self.subTest(data=data):
                self.client.get = AsyncMock(return_value=data)
                with self.assertRaises(ApiError) as caught:
                    await self.client.vehicles()
                self.assertEqual(caught.exception.code, "invalid_vehicle_list")

    async def test_missing_or_invalid_access_token(self):
        for data in (None, [], {}, {"accessToken": ""}, {"accessToken": 1}):
            with self.subTest(data=data):
                self.client._request = AsyncMock(return_value=data)
                with self.assertRaises(ApiError) as caught:
                    await self.client.vehicles()
                self.assertEqual(caught.exception.code, "missing_token")
                self.assertIsNone(self.client.token)
                self.client._request.assert_awaited_once()

    async def test_auth_endpoint_connection_error_is_not_invalid_credentials(self):
        self.client._request = AsyncMock(side_effect=ApiError(503, "503"))
        with self.assertRaises(ApiError) as caught:
            await self.client.vehicles()
        self.assertEqual(caught.exception.status, 503)
        self.client._request.assert_awaited_once()

    async def test_transport_errors_are_sanitized_and_cancellation_propagates(self):
        for error in (aiohttp.ClientError("fixture-private-request"), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                self.client.session.request.side_effect = error
                with self.assertRaises(ApiError) as caught:
                    await self.client._request("/v1/auth/personal-token", self.client.credentials)
                self.assertEqual(
                    (caught.exception.status, caught.exception.code), (0, "connection_failed")
                )
                self.assertNotIn("fixture-private", str(caught.exception))
        self.client.session.request.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.client._request("/v1/auth/personal-token", self.client.credentials)

    async def test_oversized_response_is_rejected(self):
        self.response(b" " * (16 * 1024 * 1024 + 1))
        with self.assertRaises(ApiError) as caught:
            await self.client._request("/v1/auth/personal-token", self.client.credentials)
        self.assertEqual(caught.exception.code, "oversized_response")

    async def test_retry_after_dates_invalid_values_and_minimum(self):
        future = format_datetime(datetime.now(UTC) + timedelta(seconds=1200), usegmt=True)
        for header, lower, upper in (
            (future, 1190, 1210),
            ("invalid", 299, 301),
            ("-5", 299, 301),
            ("1", 299, 301),
            ("Wed, 01 Jan 2020 00:00:00 GMT", 299, 301),
            ("Wed, 01 Jan 2020 00:00:00", 299, 301),
        ):
            with self.subTest(header=header):
                self.client.retry_at = 0
                self.response(b'{"error":{"code":"429"}}', 429, {"Retry-After": header})
                with self.assertRaises(ApiError):
                    await self.client._request("/v1/auth/personal-token", self.client.credentials)
                remaining = self.client.retry_at - time.monotonic()
                self.assertGreater(remaining, lower)
                self.assertLess(remaining, upper)

    async def test_http_401_renews_token_once(self):
        self.client.token = "old"
        self.client._request = AsyncMock(
            side_effect=[
                ApiError(401, "unknown"),
                {"accessToken": "new"},
                {"vehicles": [{"vin": "fixture-vin"}]},
            ]
        )
        self.assertEqual(await self.client.vehicles(), [{"vin": "fixture-vin"}])
        self.assertEqual(self.client.token, "new")
        self.assertEqual(self.client._request.await_count, 3)

    async def test_short_lived_token_refreshes_once(self):
        self.client._request = AsyncMock(
            side_effect=[
                {"accessToken": "old"},
                ApiError(400, "40003"),
                {"accessToken": "new"},
                {"vehicles": []},
            ]
        )
        self.assertEqual(await self.client.vehicles(), [])
        self.assertEqual(self.client._request.await_count, 4)
        self.assertEqual(self.client.token, "new")

    async def test_expired_token_403_refreshes_and_reuses_new_token(self):
        self.client.token = "expired-fixture-token"

        def response(status, payload):
            result = MagicMock()
            result.status = status

            async def chunks(size):
                yield json.dumps(payload).encode()

            result.content.iter_chunked = chunks
            context = MagicMock()
            context.__aenter__.return_value = result
            return context

        self.client.session.request.side_effect = [
            response(403, {"error": {"code": 403}}),
            response(200, {"data": {"accessToken": "renewed-fixture-token"}}),
            response(200, {"data": {"vehicles": []}}),
            response(200, {"data": {"vehicles": []}}),
        ]
        self.assertEqual(await self.client.vehicles(), [])
        self.assertEqual(await self.client.vehicles(), [])
        self.assertEqual(self.client.token, "renewed-fixture-token")
        requests = self.client.session.request.call_args_list
        self.assertEqual([call.args[0] for call in requests], ["GET", "POST", "GET", "GET"])
        self.assertEqual(
            requests[0].kwargs["headers"]["Authorization"], "Bearer expired-fixture-token"
        )
        self.assertEqual(
            requests[2].kwargs["headers"]["Authorization"], "Bearer renewed-fixture-token"
        )

    async def test_persistent_403_is_permission_error_and_keeps_fresh_token(self):
        self.client.token = "old"
        self.client._request = AsyncMock(
            side_effect=[
                ApiError(403, "403"),
                {"accessToken": "fresh"},
                ApiError(403, "403"),
                {"vehicles": []},
            ]
        )
        with self.assertRaises(ApiError) as caught:
            await self.client.get("/v1/vehicles/SYNTHETIC-CAR-001/locations")
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.client._request.await_count, 3)
        self.assertEqual(self.client.token, "fresh")
        self.assertEqual(await self.client.vehicles(), [])
        self.assertEqual(self.client._request.await_count, 4)

    async def test_expired_key_after_403_requires_reauth(self):
        self.client.token = "expired"
        self.client._request = AsyncMock(side_effect=[ApiError(403, "403"), ApiError(404, "40404")])
        with self.assertRaises(AuthError):
            await self.client.vehicles()
        self.assertEqual(self.client._request.await_count, 2)

    async def test_invalid_key_requires_reauth(self):
        self.client._request = AsyncMock(side_effect=ApiError(404, "40404"))
        with self.assertRaises(AuthError):
            await self.client.vehicles()
        self.assertEqual(self.client._request.await_count, 1)

    async def test_repeated_token_rejection_stops_retry(self):
        self.client._request = AsyncMock(
            side_effect=[
                {"accessToken": "one"},
                ApiError(400, "4011"),
                {"accessToken": "two"},
                ApiError(400, "4011"),
            ]
        )
        with self.assertRaises(AuthError):
            await self.client.vehicles()
        self.assertEqual(self.client._request.await_count, 4)

    async def test_nonfinite_json_is_rejected(self):
        response = MagicMock()
        response.status = 200

        async def chunks(size):
            yield b'{"data": {"temperature": NaN}}'

        response.content.iter_chunked = chunks
        self.client.session.request.return_value.__aenter__.return_value = response
        with self.assertRaises(ApiError) as caught:
            await self.client._request("/v1/auth/personal-token", self.client.credentials)
        self.assertEqual(caught.exception.code, "invalid_response")

    async def test_rate_limit_does_not_make_network_requests(self):
        self.client.retry_at = time.monotonic() + 300
        with self.assertRaises(ApiError) as caught:
            await self.client._request("/v1/vehicles/consent")
        self.assertEqual(caught.exception.status, 429)
        self.client.session.request.assert_not_called()

    async def test_http_errors_and_rate_limit_header(self):
        response = MagicMock()
        response.status = 429
        response.headers = {"Retry-After": "1200"}

        async def chunks(size):
            yield json.dumps({"meta": {"success": False, "code": "429"}}).encode()

        response.content.iter_chunked = chunks
        self.client.session.request.return_value.__aenter__.return_value = response
        with self.assertRaises(ApiError) as caught:
            await self.client._request("/v1/auth/personal-token", self.client.credentials)
        self.assertEqual(caught.exception.status, 429)
        self.assertGreater(self.client.retry_at, time.monotonic() + 1190)

    async def test_http_schema_and_redirect_boundary(self):
        response = MagicMock()
        response.status = 200
        response.headers = {}

        async def chunks(size):
            yield b'{"meta": "invalid", "data": null}'

        response.content.iter_chunked = chunks
        self.client.session.request.return_value.__aenter__.return_value = response
        with self.assertRaises(ApiError):
            await self.client._request("/v1/auth/personal-token", self.client.credentials)
        self.assertFalse(self.client.session.request.call_args.kwargs["allow_redirects"])


if __name__ == "__main__":
    unittest.main()
