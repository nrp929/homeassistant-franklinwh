"""DataUpdateCoordinator for FranklinWH."""

from __future__ import annotations

from datetime import timedelta
import logging

import franklinwh
import httpx

from homeassistant.const import (
    MAJOR_VERSION as HASS_MAJOR_VERSION,
    MINOR_VERSION as HASS_MINOR_VERSION,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_LOCAL_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


def supports_http2() -> bool:
    """Check if the current HA version supports HTTP/2 via ALPN."""
    if HASS_MAJOR_VERSION > 2026:
        return True
    elif HASS_MAJOR_VERSION == 2026 and HASS_MINOR_VERSION >= 2:
        return True
    return False


class StaleDataCache:
    """Cache data fetch for stale data tolerance."""

    def __init__(self) -> None:
        """Empty cache."""
        self.last_data: FranklinWHData | None = None

    def store(self, data: FranklinWHData) -> None:
        """Cache data fetch."""
        self.last_data = data

    def is_populated(self) -> bool:
        """Is cache populated?"""
        return self.last_data is not None

    def data(self) -> FranklinWHData:
        """Retrieve cached data."""
        assert self.last_data is not None, "Cache is not populated"
        return self.last_data


class FranklinWHData:
    """Class to hold FranklinWH data."""

    def __init__(
        self, stats: franklinwh.Stats, switch_state: tuple[bool, bool, bool] | None = None
    ) -> None:
        """Initialize the data class."""
        self.stats = stats
        self.switch_state = switch_state or (False, False, False)


class FranklinWHCoordinator(DataUpdateCoordinator[FranklinWHData]):
    """Class to manage fetching FranklinWH data."""

    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password: str,
        gateway_id: str,
        use_local_api: bool = False,
        local_host: str | None = None,
        tolerate_stale_data: bool = False,
    ) -> None:
        """Initialize the coordinator."""
        self.hass_ref = hass
        self.username = username
        self.password = password
        self.gateway_id = gateway_id
        self.use_local_api = use_local_api
        self.local_host = local_host
        self.tolerate_stale_data = tolerate_stale_data

        # Store credentials for lazy client initialization
        # Client will be created in executor during first update to avoid blocking
        self.token_fetcher: franklinwh.TokenFetcher | None = None
        self.client: franklinwh.Client | None = None
        self._client_lock = False

        # Stale data cache
        self.cache = StaleDataCache()

        # Set update interval based on API type
        update_interval = (
            DEFAULT_LOCAL_SCAN_INTERVAL if use_local_api else DEFAULT_SCAN_INTERVAL
        )

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=update_interval),
            # Keep entities available during temporary failures
            # Only mark unavailable after 3 consecutive failures (3 minutes)
            always_update=False,
        )

        # Track consecutive failures
        self._consecutive_failures = 0
        self._max_failures = 3

    async def _async_update_data(self) -> FranklinWHData:
        """Fetch data from FranklinWH API with enhanced retry logic."""
        max_retries = 3
        retry_delay = 2  # seconds

        # Initialize client on first run (in executor to avoid blocking)
        if self.client is None and not self._client_lock:
            self._client_lock = True
            try:
                def create_client():
                    token_fetcher = franklinwh.TokenFetcher(self.username, self.password)
                    return franklinwh.Client(token_fetcher, self.gateway_id)

                self.client = await self.hass.async_add_executor_job(create_client)
                self.token_fetcher = self.client.fetcher
            except Exception as err:
                self._client_lock = False
                raise UpdateFailed(f"Failed to initialize client: {err}") from err

        # Enhanced retry loop with specific exception handling
        for attempt in range(max_retries):
            if attempt > 0:
                _LOGGER.warning("Retry attempt %d/%d for FranklinWH data", attempt + 1, max_retries)
                await __import__("asyncio").sleep(retry_delay)

            try:
                # franklinwh 0.6.0+ methods are now async
                stats = await self.client.get_stats()

                if stats is None:
                    raise UpdateFailed("Failed to fetch stats from FranklinWH API")

                # Fetch switch state
                try:
                    switch_state = await self.client.get_smart_switch_state()
                except Exception as err:
                    _LOGGER.debug("Failed to fetch switch state: %s", err)
                    switch_state = None

                # Reset failure counter on success
                self._consecutive_failures = 0

                data = FranklinWHData(stats=stats, switch_state=switch_state)
                self.cache.store(data)

                if attempt > 0:
                    _LOGGER.warning(
                        "Successfully fetched data from FranklinWH after retry"
                    )
                else:
                    _LOGGER.debug("Fetched latest data from FranklinWH: %s", data)

                return data

            except franklinwh.client.DeviceTimeoutException as err:
                _LOGGER.warning(
                    "Error getting data from FranklinWH - Device Timeout: %s", err
                )
            except franklinwh.client.GatewayOfflineException as err:
                _LOGGER.warning(
                    "Error getting data from FranklinWH - Gateway Offline: %s", err
                )
            except franklinwh.client.AccountLockedException as err:
                _LOGGER.warning(
                    "Error getting data from FranklinWH - Account Locked: %s", err
                )
            except franklinwh.client.InvalidCredentialsException as err:
                _LOGGER.warning(
                    "Error getting data from FranklinWH - Invalid Credentials: %s", err
                )
                raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
            except franklinwh.client.InvalidDataException as err:
                _LOGGER.warning(
                    "Error getting data from FranklinWH - Invalid Body Returned: %s", err
                )
            except httpx.ReadTimeout as err:
                _LOGGER.warning(
                    "Timeout fetching data from FranklinWH: %s", err
                )
            except AttributeError as err:
                # Handle case where AuthenticationError doesn't exist in franklinwh
                if "AuthenticationError" in str(type(err)):
                    raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
                # Fall through to generic handler below
                raise

        # All retries exhausted
        _LOGGER.warning(
            "Failed to fetch data from FranklinWH after %s attempts", max_retries
        )

        # Return stale data if tolerated and cache is populated
        if self.tolerate_stale_data and self.cache.is_populated():
            _LOGGER.debug("Returning stale data from cache")
            return self.cache.data()

        raise UpdateFailed(
            f"Failed to fetch data from FranklinWH after {max_retries} attempts."
        )

    async def async_set_switch_state(self, switches: tuple[bool, bool, bool]) -> None:
        """Set the state of smart switches."""
        try:
            # franklinwh 0.6.0+ methods are now async
            await self.client.set_smart_switch_state(switches)
            # Request immediate refresh
            await self.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to set switch state: %s", err)
            raise

    async def async_set_mode_direct(self, mode) -> None:
        """Set the operation mode using franklinwh.Mode object.

        This is the j4m3z0r-style implementation that accepts a Mode object
        directly from the franklinwh library.

        Args:
            mode: franklinwh.Mode object (e.g., franklinwh.Mode.self_consumption(20))
        """
        if self.client is None:
            raise UpdateFailed("Client not initialized")

        try:
            # franklinwh 0.6.0+ methods are now async
            await self.client.set_mode(mode)

            _LOGGER.info("Successfully set mode: %s", mode)

            # Request immediate refresh to update state
            await self.async_request_refresh()

        except AttributeError as err:
            _LOGGER.error(
                "The franklinwh library does not support set_mode. "
                "Please update to a version that includes franklinwh.Mode. Error: %s",
                err,
            )
            raise NotImplementedError(
                "set_mode is not available in the current franklinwh library version. "
                "Update franklinwh-python to a version that includes the Mode class."
            ) from err
        except Exception as err:
            _LOGGER.error("Failed to set mode: %s", err)
            raise

    async def async_set_operation_mode(self, mode: str) -> None:
        """Set the operation mode of the system.

        [DEPRECATED] Use async_set_mode_direct with a franklinwh.Mode object instead.
        """
        _LOGGER.warning(
            "async_set_operation_mode is deprecated. Use async_set_mode_direct instead."
        )
        raise NotImplementedError("Use the set_mode service instead")

    async def async_set_battery_reserve(self, reserve_percent: int) -> None:
        """Set the battery reserve percentage.

        [DEPRECATED] Use async_set_mode_direct with a franklinwh.Mode object instead.
        """
        _LOGGER.warning(
            "async_set_battery_reserve is deprecated. Use async_set_mode_direct instead."
        )
        raise NotImplementedError("Use the set_mode service instead")
