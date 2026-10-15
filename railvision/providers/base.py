"""Provider adapter contract.

These method names are INTERNAL contracts. They are not claims that any official endpoint
with these names exists. Each concrete adapter maps one authorized source onto them.
"""
from __future__ import annotations

from abc import ABC
from datetime import date, datetime


class ProviderError(Exception):
    """Transient upstream failure (timeout, 5xx, malformed payload). Retry with backoff."""


class NotSupported(Exception):
    """This source does not provide the requested data. `official_url` = manual fallback."""

    def __init__(self, msg: str, official_url: str | None = None):
        super().__init__(msg)
        self.official_url = official_url


class NotAuthorized(Exception):
    """Credentials missing/invalid, or programmatic use not permitted."""


# Feature status labels required by the master prompt
VERIFIED_NOW = "VERIFIED AND IMPLEMENTABLE NOW"
NEEDS_APPROVAL = "REQUIRES OFFICIAL CREDENTIALS/APPROVAL"
NEEDS_LICENSE = "REQUIRES THIRD-PARTY LICENSE"
MANUAL_LINK = "MANUAL-LINK FALLBACK"
UNSUPPORTED = "NOT CURRENTLY SUPPORTED"


class ProviderAdapter(ABC):
    source_id: str = "base"
    display_name: str = "Base"
    is_demo: bool = False
    official: bool = False
    capabilities: dict[str, str] = {}

    def get_train_status(self, train_number: str, service_date: date) -> list:
        """Return list[LiveObservation] for the service."""
        raise NotSupported("train status not provided by this source")

    def get_train_route(self, train_number: str, service_date: date):
        """Return core.models.Route."""
        raise NotSupported("route not provided by this source")

    def get_station_board(self, station_code: str, start: datetime, end: datetime) -> list[dict]:
        raise NotSupported("station board not provided by this source")

    def find_trains_between(self, from_station: str, to_station: str, on: date) -> list[dict]:
        raise NotSupported("trains-between not provided by this source")

    def get_chart_vacancy(self, train_number: str, journey_date: date, boarding_station: str, travel_class: str):
        raise NotSupported("chart vacancy not provided by this source",
                           "https://www.irctc.co.in/online-charts/")

    def get_reservation_availability(self, **params) -> dict:
        raise NotSupported("reservation availability not provided by this source", "https://www.irctc.co.in/")

    def search_trains(self, query: str) -> list[dict]:
        raise NotSupported("train search not provided by this source")

    def get_source_health(self) -> dict:
        return {"source_id": self.source_id, "status": "UNKNOWN"}
