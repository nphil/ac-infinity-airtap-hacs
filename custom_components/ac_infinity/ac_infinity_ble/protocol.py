"""Wire protocol for AC Infinity BLE controllers.

CAPABILITY INVENTORY -- what this code PROVABLY speaks
======================================================

This vendored library is the integration's only source of BLE truth.  Every
statement below is cited from code in this repository; nothing comes from
reverse-engineering the vendor app beyond what these builders/parsers
already encode.  Inferences are labelled [INFERENCE].  Do NOT add new
command bytes without live-hardware verification: these frames drive
physical fans.

Frame layout (``Protocol._add_head(payload, b, sequence)``)
-----------------------------------------------------------
::

    offset 0-1    header 0xA5 0x00 (``_head``)
    offset 2-3    payload length, big-endian
    offset 4-5    sequence number, big-endian (wraps at 65535)
    offset 6-7    CRC16 over offsets 0-5 (``util.crc16``, CCITT-FALSE)
    offset 8      0x00
    offset 9      command class ``b``: 1 = read (get_model_data),
                  3 = write (set_level and the integration's setters)
    offset 10..   payload
    last 2        CRC16 over offset 8 through the end of the payload

Payload grammar (proven by the five in-tree builders)
-----------------------------------------------------
Payloads are ``[opcode, value_length, value...]`` groups::

    [16, 1, work_type]   mode        (set_level; integration set_mode_auto)
    [17, 1, level]       OFF-mode / minimum level
                                     (set_level wt=1; async_set_min_speed)
    [18, 1, level]       ON-mode / maximum level
                                     (set_level wt=2; async_set_max_speed)
    [19, 7, switches, highF, highC, lowF, lowC, highHum, lowHum]
                         AUTO thresholds (async_set_auto_mode_config);
                         temperatures are sent in BOTH Fahrenheit and
                         Celsius; ``switches`` bits: 0x08 high temp,
                         0x04 low temp, 0x02 high hum, 0x01 low hum
    [255, port]          appended for multi-port device types 7/9/11/12
                         only (the same types whose advertisements carry
                         ``choose_port``); every in-tree caller passes
                         port=0

``get_model_data`` queries opcodes ``[16..23]`` with command class 1.

Modes: 12 named, 3 drivable
---------------------------
``get_mode`` names modes 1-12 (OFF, ON, AUTO, TIMER ON, TIMER OFF, CYCLE,
SCHEDULE, VPD, TEMPERATURE PARAM, HUMIDITY PARAM, ADVANCE, AI).  Command
builders exist ONLY for:

- 1 OFF   -- ``set_level(type, 1, level, ...)``.  OFF is a mode with its
  own stored level (``level_off``); the state model reports that level as
  the fan level while in mode 1, and ``turn_off`` preserves it rather than
  forcing zero.
- 2 ON    -- ``set_level(type, 2, level, ...)``
- 3 AUTO  -- the integration's ``ACInfinityDevice.set_mode_auto`` composes
  payload ``[16, 1, 3]`` via ``_add_head`` (no builder in this module).

Modes 4-12 have NO command builder anywhere in this repository and CANNOT
be selected over BLE by this integration.  Their numeric values may still
appear in ``work_type`` if the vendor app set them; treat them as
read-only observations.

``get_model_data`` response, byte-by-byte (as far as parsing proves)
--------------------------------------------------------------------
Parsed by ``ACInfinityController.update`` (requires len >= 19) and the
integration's ``ACInfinityDevice.update`` (requires len >= 28)::

    data[12]   work_type (1=OFF, 2=ON, 3=AUTO, others per get_mode)
    data[15]   level_off: OFF-mode level / AUTO minimum, 0-10
    data[18]   level_on:  ON-mode level  / AUTO maximum, 0-10
    data[21]   AUTO enable bitmask: 0x08 high temp, 0x04 low temp,
               0x02 high humidity, 0x01 low humidity
    data[23]   AUTO high temperature threshold, degrees C
    data[25]   AUTO low temperature threshold, degrees C
    data[26]   AUTO high humidity threshold, percent
    data[27]   AUTO low humidity threshold, percent

[INFERENCE] These offsets line up exactly with the response echoing the
same opcode groups the request queried, starting at offset 10:
``16 01 <wt> 17 01 <level_off> 18 01 <level_on> 19 07 <switches> <highF>
<highC> <lowF> <lowC> <highHum> <lowHum> ...`` -- which also implies
data[22]/data[24] are the Fahrenheit twins of data[23]/data[25].  Opcodes
20-23 are queried, but nothing in this repository parses beyond data[27];
their content is unknown.  Responses are NOT sequence-correlated by any
parser here, which is why callers length-guard instead of trusting every
frame.

Advertisement (``parse_manufacturer_data``, manufacturer id 2306/0x0902)
------------------------------------------------------------------------
::

    data[6:11]   ASCII device id; name is "<family>-<id>" (get_type:
                 type 6 = "D" = AIRTAP T-series)
    data[11]     version
    data[12]     device type
    data[13]     bit flags, MSB-indexed: bit1 is_degree, bits2-3
                 fan_state, bits4-5 tmp_state, bits6-7 hum_state
    data[14:16]  temperature * 100, signed big-endian
    data[16:18]  humidity * 100, signed big-endian (always 0 on AIRTAP
                 type 6: that hardware has no humidity sensor)
    data[18]     current fan level, 0-10
    -- only when version >= 3 and type in 7/9/11/12:
    data[19]     choose_port
    data[20]     bits 0-1 vpd_state
    data[21:23]  vpd * 100

Advertisements do NOT carry work_type, level_on, or level_off; those are
learned from polls/notifications, and the None-filtering merge in
``set_ble_device_and_advertisement_data`` preserves them across
advertisement updates.

Broadcast notification frame (first bytes 0x1E 0xFF, parsed in
``ACInfinityController._notification_handler``, len >= 18): carries
tmp/hum/vpd/fan_type/fan_state and work_type (high nibble of data[17]).
The fan-level nibble in that frame is explicitly NOT trusted (upstream
comment: "Not accurate"); the live level comes from advertisements.
"""
from .models import DeviceInfo
from .util import crc16, get_bit, get_bits, get_short


def get_type(type: int) -> str:
    """Map the numeric device type to its model-family letter."""
    if type == 2:
        return "B"
    if type in [3, 4, 5, 14, 15]:
        return "C"
    if type == 6:
        return "D"
    if type in [7, 8]:
        return "E"
    if type in [9, 12]:
        return "F"
    if type == 11:
        return "G"
    return "A"


def get_mode(mode: int) -> str:
    """Name a work_type value; see the module docstring for drivability."""
    if mode == 1:
        return "OFF"
    if mode == 2:
        return "ON"
    if mode == 3:
        return "AUTO"
    if mode == 4:
        return "TIMER ON"
    if mode == 5:
        return "TIMER OFF"
    if mode == 6:
        return "CYCLE"
    if mode == 7:
        return "SCHEDULE"
    if mode == 8:
        return "VPD"
    if mode == 9:
        return "TEMPERATURE PARAM"
    if mode == 10:
        return "HUMIDITY PARAM"
    if mode == 11:
        return "ADVANCE"
    if mode == 12:
        return "AI"
    return ""


def parse_manufacturer_data(data: bytes) -> DeviceInfo:
    """Parse a manufacturer-data advertisement (layout in module docstring)."""
    device = DeviceInfo(
        type=data[12],
        version=data[11],
        name=f"{get_type(data[12])}-{data[6:11].decode('ascii')}",
        is_degree=True ^ get_bit(data[13], 1),
        fan_state=get_bits(data[13], 2, 2),
        tmp_state=get_bits(data[13], 4, 2),
        hum_state=get_bits(data[13], 6, 2),
        tmp=get_short(data, 14) / 100,
        hum=get_short(data, 16) / 100,
        fan=data[18],
    )
    if device.version >= 3 and device.type in [7, 9, 11, 12]:
        device.choose_port = data[19]
        device.vpd_state = get_bits(data[20], 0, 2)
        device.vpd = get_short(data, 21) / 100
    return device


def parse_model_data(frame: bytes) -> dict[int, bytes]:
    """Split a ``get_model_data`` response into its ``{opcode: value}`` groups.

    The response is the request's own payload grammar echoed back —
    ``[opcode, length, value...]`` repeated — inside the standard frame, so
    the payload runs from offset 10 for the length declared at offsets 2-3.
    Walking it is strictly better than the fixed offsets the parsers used
    before: it tolerates a model whose group lengths differ, and it is the
    only way to read the groups after the variable-length AUTO block.

    Verified against live AIRTAP T-series (type 6) responses captured on
    2026-09-10; all six fans answered with the same eight groups::

        16:1  work_type          19:7  AUTO threshold block
        17:1  level_off          20:4  TIMER TO ON  duration
        18:1  level_on           21:4  TIMER TO OFF duration
                                 22:8  CYCLE on + off durations
                                 23:0  absent on this model

    Returns an empty dict for anything that is not a complete, exactly
    consumed response: responses are not sequence-correlated, so a caller
    can be handed an ack or a stale frame from an earlier command, and
    parsing one of those would poison the state.
    """
    if len(frame) < 12:
        return {}
    length = (frame[2] << 8) | frame[3]
    end = 10 + length
    # +2 for the trailing CRC; a frame shorter than that is truncated.
    if length <= 0 or len(frame) < end + 2:
        return {}
    groups: dict[int, bytes] = {}
    i = 10
    while i < end:
        if i + 2 > end:
            return {}
        opcode, size = frame[i], frame[i + 1]
        if i + 2 + size > end:
            return {}
        groups[opcode] = frame[i + 2 : i + 2 + size]
        i += 2 + size
    return groups


class Protocol:
    """Protocol for AC Infinity Controllers."""

    def __init__(self) -> None:
        self._head = [165, 0]
        self._scan_record_length = 27

    def _add_init(self, bytes: list[int], i: int, i2: int) -> None:
        """Write ``i2`` as a big-endian 16-bit value at offset ``i``."""
        bytes[i] = (i2 >> 8) & 255
        bytes[i + 1] = i2 & 255

    def _add_head(self, data: list[int], b: int, i: int) -> bytes:
        """Wrap ``data`` in the framed layout described in the module docstring.

        ``b`` is the command class (1=read, 3=write), ``i`` the sequence
        number.
        """
        result = [0] * (len(data) + 12)
        result[0 : len(self._head)] = self._head  # noqa: E203
        self._add_init(result, 2, len(data))
        self._add_init(result, 4, i)
        result[6:8] = crc16(result, 0, 6)
        result[8] = 0
        result[9] = b
        result[10 : 10 + len(data)] = data  # noqa: E203
        result[len(data) + 10 : len(data) + 12] = crc16(  # noqa: E203
            result, 8, len(data) + 2
        )
        return bytes(result)

    def get_model_data(self, type: int, b: int, sequence: int) -> bytes:
        """Build the read command querying opcodes 16-23.

        ``b`` is the port selector byte, only appended (after a 0xFF marker)
        for multi-port device types 7/9/11/12; every in-tree caller passes 0.
        """
        command = [16, 17, 18, 19, 20, 21, 22, 23]
        if type in [7, 9, 11, 12]:
            command += [255, b]
        return self._add_head(command, 1, sequence)

    def set_level(
        self, type: int, work_type: int, level: int, b: int, sequence: int
    ) -> bytes:
        """Build the write command setting mode AND its level in one frame.

        ``work_type`` 1 (OFF) or 2 (ON) only -- these are the only modes
        this builder can set; ``level`` 0-10 is stored as level_off for
        work_type 1 (opcode 17) or level_on for work_type 2 (opcode 18).
        ``b`` is the port selector as in ``get_model_data``.
        """
        if work_type not in [1, 2]:
            raise ValueError("Work type must be 1 (off) or 2 (on)")
        if level not in range(0, 11):
            raise ValueError("Level must be between 0 and 10")

        command = [16, 1, work_type, work_type + 16, 1, level]
        if type in [7, 9, 11, 12]:
            command += [255, b]
        return self._add_head(command, 3, sequence)
