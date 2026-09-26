"""Resting speed that follows the home's HVAC blower.

In AUTO the fan runs at its minimum (register 17) while the air is inside
its trigger range and ramps toward its maximum once the air turns cold or
hot. That covers conditioned air on its own, inside the fan, with no help
from Home Assistant. What the fan cannot know is whether room-temperature
air is being moved at all: its thermistor reads 70-73 F with the blower
circulating and 68-72 F with the blower off (15 days of history,
2026-09-10..25), so only the thermostat can tell the two apart.

This module lets the thermostat decide the minimum: the circulation speed
while the blower runs, the resting speed once it has been off for
``circulation_hold`` minutes. Every change is a write the fan keeps in its
own memory (its settings survive a power cut), and that memory's endurance
is unpublished, so the design spends writes carefully:

- The hold absorbs the blower's short gaps. Measured over the same 15 days
  the blower started 66 times a day, mostly 5-minute circulation bursts
  10-15 minutes apart; a 20-minute hold cuts that to about 11 on/off pairs a
  day (22 writes per fan) instead of 133.
- A write happens only when the fan's own register differs from the target,
  and the same target is not retried for WRITE_RETRY seconds, so a fan that
  refuses a value cannot turn into a write loop.
- An unavailable thermostat (the ecobee drops off Wi-Fi a few times a week,
  up to an hour at a time) changes nothing: the last decision stands.
- Only a fan in AUTO is touched. Register 17 is also OFF mode's running
  speed, so writing it to a fan that is off would start it.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from time import monotonic
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, State, callback
from homeassistant.helpers.event import (async_call_later,
                                         async_track_state_change_event)

from .const import (CONF_CIRCULATION_HOLD, CONF_CIRCULATION_SPEED,
                    CONF_REST_SPEED, CONF_THERMOSTAT, DEFAULT_CIRCULATION_HOLD)
from .device import WORK_TYPE_AUTO, ACInfinityDevice

_LOGGER = logging.getLogger(__name__)

# hvac_action values that mean air is moving through the ducts.
BLOWER_RUNNING = frozenset(
    {"cooling", "heating", "fan", "drying", "defrosting", "preheating"}
)
BLOWER_STOPPED = frozenset({"idle", "off"})

# Seconds before the same target is written again after a write the fan's
# read-back did not confirm.
WRITE_RETRY = 600


def blower_running(state: State | None) -> bool | None:
    """True/False from the thermostat's hvac_action; None when it cannot say."""
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    action = state.attributes.get("hvac_action")
    if action in BLOWER_RUNNING:
        return True
    if action in BLOWER_STOPPED or (action is None and state.state == "off"):
        return False
    return None


class VentSettings:
    """One fan's Home Assistant side settings, kept in its entry's options."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry

    @property
    def thermostat(self) -> str | None:
        return self._entry.options.get(CONF_THERMOSTAT) or None

    @property
    def rest_speed(self) -> int | None:
        """The minimum while the blower is off; None leaves the fan's own."""
        return self._entry.options.get(CONF_REST_SPEED)

    @property
    def circulation_speed(self) -> int | None:
        """The minimum while the blower runs; None means no circulation boost."""
        return self._entry.options.get(CONF_CIRCULATION_SPEED)

    @property
    def circulation_hold(self) -> int:
        """Minutes after the blower stops before the resting speed returns."""
        return self._entry.options.get(CONF_CIRCULATION_HOLD, DEFAULT_CIRCULATION_HOLD)

    @callback
    def async_set(self, key: str, value: Any) -> None:
        # Merge: the options also carry the integration's own bookkeeping.
        self._hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, key: value}
        )


class CirculationController:
    """Keeps an AUTO fan's minimum in step with the HVAC blower."""

    def __init__(
        self,
        hass: HomeAssistant,
        settings: VentSettings,
        device: ACInfinityDevice,
        notify: Callable[[], None],
    ) -> None:
        self._hass = hass
        self._settings = settings
        self._device = device
        self._notify = notify
        # The thermostat this controller subscribed to; a different one in
        # the options means the entry has to be set up again.
        self.thermostat = settings.thermostat
        # True: blower running (or within the hold). False: resting.
        # None: not known yet, so nothing is written.
        self.circulating: bool | None = None if self.thermostat else False
        self._cancel_hold: CALLBACK_TYPE | None = None
        self._unsubscribes: list[Callable[[], None]] = []
        self._writing = False
        self._last_write: tuple[int, float] | None = None

    @callback
    def async_start(self) -> None:
        # Every notification (about once a second while the link is held)
        # and every poll re-checks, so a link that comes back or a switch
        # into AUTO is caught within a second. The check itself is free;
        # only a mismatch writes.
        self._unsubscribes.append(
            self._device.register_callback(lambda _state, _kind: self.async_reconcile())
        )
        self._unsubscribes.append(
            self._device.hold_status.add_listener(self.async_reconcile)
        )
        if self.thermostat:
            self._unsubscribes.append(
                async_track_state_change_event(
                    self._hass, [self.thermostat], self._async_thermostat_changed
                )
            )
            self._async_blower(blower_running(self._hass.states.get(self.thermostat)))
        self.async_reconcile()

    @callback
    def async_stop(self) -> None:
        while self._unsubscribes:
            self._unsubscribes.pop()()
        self._async_cancel_hold()

    @callback
    def _async_thermostat_changed(self, event: Any) -> None:
        self._async_blower(blower_running(event.data.get("new_state")))

    @callback
    def _async_blower(self, running: bool | None) -> None:
        if running is None:
            return
        if running:
            self._async_cancel_hold()
            self.circulating = True
        elif self.circulating is not False and self._cancel_hold is None:
            self._cancel_hold = async_call_later(
                self._hass,
                self._settings.circulation_hold * 60,
                self._async_hold_expired,
            )
        self.async_reconcile()

    @callback
    def _async_hold_expired(self, _now: datetime) -> None:
        self._cancel_hold = None
        self.circulating = False
        self.async_reconcile()

    @callback
    def _async_cancel_hold(self) -> None:
        if self._cancel_hold is not None:
            self._cancel_hold()
            self._cancel_hold = None

    @property
    def target(self) -> int | None:
        """The minimum the fan should have now; None when nothing is owed."""
        rest = self._settings.rest_speed
        if rest is None or self.circulating is None:
            return None
        circulation = self._settings.circulation_speed
        if self.circulating and circulation is not None and self.thermostat:
            return circulation
        return rest

    @callback
    def async_reconcile(self) -> None:
        target = self.target
        device = self._device
        if (
            target is None
            or self._writing
            or device.state.work_type != WORK_TYPE_AUTO
            or device.min_speed is None
            or device.min_speed == target
            or not device.is_connected
        ):
            return
        if (
            self._last_write is not None
            and self._last_write[0] == target
            and monotonic() - self._last_write[1] < WRITE_RETRY
        ):
            return
        self._last_write = (target, monotonic())
        self._writing = True
        self._hass.async_create_background_task(
            self._async_write(target), f"ac_infinity minimum {device.address}"
        )

    async def _async_write(self, target: int) -> None:
        try:
            await self._device.async_set_min_speed(target)
            _LOGGER.debug("%s: minimum set to %s", self._device.name, target)
        except Exception as err:  # noqa: BLE001 - retried on a later check
            _LOGGER.warning(
                "%s: could not set the minimum to %s: %s", self._device.name, target, err
            )
        finally:
            self._writing = False
        self._notify()
