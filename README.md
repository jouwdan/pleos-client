# pleos-client

An asynchronous Python client for the official Pleos **personal Vehicle Data API**, used by [Pleos Vehicle Data for Home Assistant](https://github.com/jouwdan/ha-pleos). It has no Home Assistant dependency.

The client handles authentication, one retry for expired access tokens, rate-limit backoff, bounded JSON responses and vehicle-list validation. It returns API data without creating entities, converting readings or storing vehicle history.

## Installation

Requires Python 3.11 or later.

```sh
python -m pip install pleos-client
```

## Usage

Create a personal Client ID and Client Secret in [Pleos Playground](https://pleos.ai/), using the vehicle's primary account. See the [official personal-key guide](https://document.pleos.ai/en/api-reference/vehicle-data-api/getting-started/for-private-users/request-my-vehicle-api-key).

```python
import asyncio
import os
from urllib.parse import quote

import aiohttp

from pleos_client import PleosClient


async def main():
    async with aiohttp.ClientSession() as session:
        client = PleosClient(
            session,
            brand="kia",  # "hyundai", "kia" or "genesis"
            client_id=os.environ["PLEOS_CLIENT_ID"],
            client_secret=os.environ["PLEOS_CLIENT_SECRET"],
        )
        for vehicle in await client.vehicles():
            vin = quote(vehicle["vin"], safe="")
            batteries = await client.get(f"/v1/vehicles/{vin}/batteries")
            # Use batteries locally; avoid logging vehicle data or credentials.


asyncio.run(main())
```

The caller supplies and closes the `aiohttp.ClientSession`. Reuse one client per account and await each request before starting another. The client performs no background polling and does not close your session.

- `await client.vehicles()` returns a list of vehicle dictionaries, including VINs. An empty list is a valid response.
- `await client.get(path)` accepts a relative API path, including any encoded query parameters, and returns the response's `data` value. Use the documented common snapshot and trip endpoints; the client does not implement Fleet API or business-user authentication.
- `client.response_meta` contains selected response metadata, keyed by path without query parameters. It can contain VINs in its keys and request identifiers; review it before sharing.
- `client.retry_at` is a `time.monotonic()` deadline. Requests made before it raise `ApiError` without network traffic.

## Errors and limits

Catch `AuthError` when the personal key has expired, been revoked or is rejected. Renew the personal key manually; only short-lived access tokens renew automatically. Catch `ApiError` for transport, permission, rate-limit and malformed-response failures. It exposes `status` and `code`; status `0` means a connection failure or timeout, not an HTTP response from Pleos. Persistent 403 responses remain permission errors after one token renewal.

HTTP 429 honours `Retry-After` (seconds or an HTTP date), with a minimum backoff of five minutes. Each request has a 30-second timeout and rejects bodies above 16 MiB, redirects, malformed JSON and non-finite JSON numbers. Failed data requests are not repeatedly retried, except for one access-token renewal.

This API is read-only. Polling cannot wake the car or force an upload. Returned values may be old or absent, and trip data can arrive later. Availability depends on the account, region, vehicle and consent settings. See the [Pleos API documentation](https://document.pleos.ai/en/api-reference/vehicle-data-api).

Access tokens stay in memory. Exceptions omit request paths, credentials and response contents. Returned data can contain VINs and locations; protect it and do not publish raw responses.

## Development

```sh
python -m pip install -e '.[dev]'
python -m coverage run -m unittest discover -s tests -v
python -m coverage report
python -m mypy
python -m ruff check .
python -m ruff format --check .
python -m build
python -m twine check dist/*
```

Tests use synthetic responses and need no Pleos account. CI checks Python 3.11 and 3.14 and builds both a wheel and a source distribution.

## Releasing

Releases require maintainer approval.

After approval, tag the reviewed commit as `v<version>` matching `pyproject.toml` and publish its GitHub release. The public release workflow verifies that match, runs validation, builds distributions and publishes those artifacts to PyPI through OIDC. Ordinary pushes and pull requests only validate and build; they do not publish packages. No PyPI token is stored in this repository.

The initial client was extracted from [ha-pleos v0.5.2](https://github.com/jouwdan/ha-pleos/tree/v0.5.2). Licensed under the MIT license.
