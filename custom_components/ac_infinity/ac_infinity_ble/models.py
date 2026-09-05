from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DeviceInfo:
    """Parsed device state.

    Populated from three sources of differing freshness and completeness:
    advertisements (``parse_manufacturer_data``), get_model_data poll
    responses (``ACInfinityController.update``) and 0x1E/0xFF broadcast
    notifications (``_notification_handler``).  Fields are Optional because
    no single source carries all of them; state merges MUST preserve
    unknown (None) fields or previously learned values are lost.
    """

    type: int  # device model type; 6 = AIRTAP T-series ("D" family)
    name: str
    version: int
    # Temperature display-unit flag (polarity unproven; display-only, does
    # not affect the wire format -- setters send both C and F).
    is_degree: bool | None = None
    tmp_state: int | None = None  # 2-bit sensor status from adv/notification
    hum_state: int | None = None  # 2-bit sensor status from adv/notification
    vpd_state: int | None = None  # 2-bit sensor status (multi-port types)
    choose_port: int | None = None  # selected port (multi-port types 7/9/11/12)
    tmp: float | None = None  # degrees C
    hum: float | None = None  # percent; always 0.0 on AIRTAP type 6 (no sensor)
    vpd: float | None = None  # kPa (multi-port types only)
    fan_type: int | None = None
    fan_state: int | None = None  # 2-bit status from adv/notification
    fan: int | None = None  # live fan level 0-10
    work_type: int | None = None  # mode: 1=OFF, 2=ON, 3=AUTO (protocol.get_mode)
    level_on: int | None = None  # ON-mode level / AUTO maximum (opcode 18)
    level_off: int | None = None  # OFF-mode level / AUTO minimum (opcode 17)
