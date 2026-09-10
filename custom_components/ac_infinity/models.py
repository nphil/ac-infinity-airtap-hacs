from __future__ import annotations

from dataclasses import dataclass

from .device import ACInfinityDevice
from .coordinator import ACInfinityDataUpdateCoordinator, ACInfinityLinkWatchdog


@dataclass
class ACInfinityData:
    title: str
    device: ACInfinityDevice
    coordinator: ACInfinityDataUpdateCoordinator
    # Reached by repairs.py: the recovery wizard asks the entry's own
    # watchdog to reconcile once an action worked, so the repair disappears
    # from the integration's side too instead of only being closed by the
    # flow finishing.
    watchdog: ACInfinityLinkWatchdog
