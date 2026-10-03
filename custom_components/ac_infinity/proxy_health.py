"""Which proxy a stalled connection attempt went through, and for how long to skip it.

Deliberately free of Home Assistant and Bluetooth imports (like ``hold.py``):
everything here is bookkeeping over scanner *sources* (the proxy's MAC), so the
decisions are unit-testable on a bare interpreter.

WHY this exists: habluetooth scores connection paths with RSSI minus a penalty
per recorded connect failure (``0.51 x rssi gap`` each), and clears that count
on the next *successful* connect.  An attempt that connects and then stalls in
the notification subscribe is a success to habluetooth, and a connect cut off
by our own step timeout is one failure that the next success wipes.  Two idle
proxies at -50 and -70 dBm: the failed one still scores -60.2 against -70, so
default routing, and the preferred-proxy affinity, send the next attempt
straight back to the proxy that just hung.  The only honest fix is to stop
offering that proxy for a while, which is what ``StalledProxies`` records.

The skip is temporary and never the only route: ``ble_affinity`` applies it only
when another connectable path exists, so a fan reachable through a single proxy
still uses it.
"""
from __future__ import annotations

import time
from collections.abc import Callable

#: How long a proxy that stalled an attempt is left out of the routing.
#: Deliberately short: a stall is as likely a busy proxy as a broken one, and
#: the proxy beside the fan is where this house wants it to live, so it must
#: become eligible again soon.  A link that comes up through it ends the skip
#: at once.
STALL_AVOID_SECONDS = 120.0


class StalledProxies:
    """One fan's proxies that recently stalled a connection attempt."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._stalled_at: dict[str, float] = {}

    def record(self, source: str) -> None:
        """An attempt through ``source`` hung (connect or subscribe step)."""
        self._stalled_at[source] = self._clock()

    def clear(self, source: str) -> None:
        """A link came up through ``source``: it works, stop skipping it."""
        self._stalled_at.pop(source, None)

    def is_excluded(self, source: str | None) -> bool:
        """Whether routing should leave ``source`` out right now."""
        if source is None:
            return False
        stalled_at = self._stalled_at.get(source)
        if stalled_at is None:
            return False
        if self._clock() - stalled_at > STALL_AVOID_SECONDS:
            del self._stalled_at[source]
            return False
        return True
