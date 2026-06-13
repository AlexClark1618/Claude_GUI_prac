"""
gps_ubx.py — u-blox UBX message builders + config-key constants for the F9T.

Pure helpers (depend only on ustruct), shared by the ESP firmware. Upload this
file to the ESP filesystem alongside esp_main.py / ringBuffer.py / wifi.py.

F9 generation: survey/fixed/rate config use the CFG-VALSET / CFG-VALGET
key-value interface (legacy CFG-TMODE3 etc. are NOT honored). CFG-RST is the
exception — it is still a classic message.
"""
import ustruct

UBX_HDR = b'\xb5\x62'

# ---- CFG-TMODE keys (survey-in / fixed position). Size code in bits 28-30. ----
CFG_TMODE_MODE           = 0x20030001  # U1: 0=disabled, 1=survey-in, 2=fixed
CFG_TMODE_POS_TYPE       = 0x20030002  # U1: 0=ECEF, 1=LLH
CFG_TMODE_ECEF_X         = 0x40030003  # I4 cm
CFG_TMODE_ECEF_Y         = 0x40030004  # I4 cm
CFG_TMODE_ECEF_Z         = 0x40030005  # I4 cm
CFG_TMODE_FIXED_POS_ACC  = 0x4003000f  # U4 0.1 mm
CFG_TMODE_SVIN_MIN_DUR   = 0x40030010  # U4 s
CFG_TMODE_SVIN_ACC_LIMIT = 0x40030011  # U4 0.1 mm

# ---- CFG-RATE keys ----
CFG_RATE_MEAS    = 0x30210001  # U2: measurement period (ms)
CFG_RATE_NAV     = 0x30210002  # U2: nav rate (measurements per nav solution)
CFG_RATE_TIMEREF = 0x20210003  # U1/E1: 0=UTC,1=GPS,2=GLONASS,3=BeiDou,4=Galileo
RATE_KEYS = [CFG_RATE_MEAS, CFG_RATE_NAV, CFG_RATE_TIMEREF]

# ---- CFG-TP keys (time-pulse / PPS).  Only TP1 (tpIdx 0) is exposed. ----
# Size code in bits 28-30 of the key: 0x1->bit(1B), 0x2->U1(1B), 0x3->I2(2B),
# 0x4->U4/I4(4B), 0x5->R8(8B).  Duty (R8) is intentionally NOT used — the GUI
# transport is integer-only, so pulse width is always sent as LENGTH (µs).
CFG_TP_PULSE_DEF         = 0x20050023  # E1: 0=period (µs), 1=frequency (Hz)
CFG_TP_PULSE_LENGTH_DEF  = 0x20050030  # E1: 0=ratio/duty, 1=length (µs)
CFG_TP_ANT_CABLEDELAY    = 0x30050001  # I2: antenna cable delay (ns)
CFG_TP_PERIOD_TP1        = 0x40050002  # U4: pulse period (µs), unlocked
CFG_TP_PERIOD_LOCK_TP1   = 0x40050003  # U4: pulse period (µs), GNSS-locked
CFG_TP_FREQ_TP1          = 0x40050024  # U4: pulse frequency (Hz), unlocked
CFG_TP_FREQ_LOCK_TP1     = 0x40050025  # U4: pulse frequency (Hz), GNSS-locked
CFG_TP_LEN_TP1           = 0x40050004  # U4: pulse length (µs), unlocked
CFG_TP_LEN_LOCK_TP1      = 0x40050005  # U4: pulse length (µs), GNSS-locked
CFG_TP_DUTY_TP1          = 0x5005002a  # R8: pulse duty cycle (%), unlocked
CFG_TP_DUTY_LOCK_TP1     = 0x5005002b  # R8: pulse duty cycle (%), GNSS-locked
CFG_TP_TP1_ENA           = 0x10050007  # L: 1=enable TP1 output
CFG_TP_SYNC_GNSS_TP1     = 0x10050008  # L: 1=use locked params once GNSS time is in
CFG_TP_USE_LOCKED_TP1    = 0x10050009  # L: 1=use *_LOCK values when locked
CFG_TP_ALIGN_TO_TOW_TP1  = 0x1005000a  # L: 1=align pulse to top-of-second
CFG_TP_POL_TP1           = 0x1005000b  # L: 1=rising edge at top-of-second

TP1_KEYS = [CFG_TP_TP1_ENA, CFG_TP_PULSE_DEF, CFG_TP_PULSE_LENGTH_DEF, CFG_TP_POL_TP1,
            CFG_TP_ALIGN_TO_TOW_TP1, CFG_TP_SYNC_GNSS_TP1, CFG_TP_USE_LOCKED_TP1,
            CFG_TP_FREQ_TP1, CFG_TP_FREQ_LOCK_TP1,
            CFG_TP_PERIOD_TP1, CFG_TP_PERIOD_LOCK_TP1,
            CFG_TP_LEN_TP1, CFG_TP_LEN_LOCK_TP1,
            CFG_TP_DUTY_TP1, CFG_TP_DUTY_LOCK_TP1, CFG_TP_ANT_CABLEDELAY]

# TP1 flag bits — shared convention between the GUI, the server and the ESP.
TP1_F_ENABLE   = 1 << 0   # output enabled
TP1_F_ISFREQ   = 1 << 1   # value is frequency (Hz); else period (µs)
TP1_F_POL      = 1 << 2   # polarity: rising edge at top-of-second
TP1_F_ALIGNTOW = 1 << 3   # align to time-of-week
TP1_F_LOCKGPS  = 1 << 4   # sync/use GNSS-locked params
TP1_F_USELOCK  = 1 << 5   # use *_LOCK values when locked
TP1_F_ISLENGTH = 1 << 6   # pulse width is length (µs); else duty cycle (%)

# Duty cycle (%) is an R8 double on the receiver, but the GUI transport is
# integer-only — so duty is carried as round(percent * TP1_DUTY_SCALE).
TP1_DUTY_SCALE = 1000


def ubx_msg(cls, id_, payload):
    """Build a complete UBX frame: sync + header + payload + Fletcher checksum."""
    length = len(payload)
    CK_A = CK_B = 0
    for b in (cls, id_, length & 0xFF, (length >> 8) & 0xFF):
        CK_A = (CK_A + b) & 0xFF
        CK_B = (CK_B + CK_A) & 0xFF
    for b in payload:
        CK_A = (CK_A + b) & 0xFF
        CK_B = (CK_B + CK_A) & 0xFF
    msg = bytearray(6 + length + 2)
    msg[0] = 0xB5; msg[1] = 0x62
    msg[2] = cls;  msg[3] = id_
    msg[4] = length & 0xFF; msg[5] = (length >> 8) & 0xFF
    msg[6:6 + length] = payload
    msg[6 + length] = CK_A
    msg[7 + length] = CK_B
    return msg


def build_valset(items, layers=0x07):
    """
    UBX-CFG-VALSET (0x06, 0x8A) — F9 key-value config interface.
    items  : list of (key_id, value, size_bytes); size_bytes in {1,2,4,8}.
    layers : bitmask 0x01=RAM, 0x02=BBR, 0x04=Flash.
             Default 0x07 = RAM|BBR|Flash: applies immediately AND persists
             across power cycles (deliberate, infrequent config changes).
    """
    payload = bytearray(4)
    payload[0] = 0x00              # version 0 (no transaction)
    payload[1] = layers & 0xFF     # config layer(s)
    for key, value, size in items:
        payload += ustruct.pack('<I', key & 0xFFFFFFFF)
        if size == 1:
            payload += ustruct.pack('<b', value)
        elif size == 2:
            payload += ustruct.pack('<h', value)
        elif size == 4:
            payload += ustruct.pack('<i', value)
        elif size == 8:
            # 8-byte values are R8 doubles when float, else I8 integers.
            if isinstance(value, float):
                payload += ustruct.pack('<d', value)
            else:
                payload += ustruct.pack('<q', value)
    return ubx_msg(0x06, 0x8A, payload)


def build_valget(keys, layer=0x00):
    """
    UBX-CFG-VALGET (0x06, 0x8B) request — read config keys back.
    layer : 0=RAM, 1=BBR, 2=Flash, 7=Default (single layer, not a bitmask).
    """
    payload = bytearray(4)
    payload[0] = 0x00              # version 0 = request
    payload[1] = layer & 0xFF
    for key in keys:
        payload += ustruct.pack('<I', key & 0xFFFFFFFF)
    return ubx_msg(0x06, 0x8B, payload)


def build_svin_cmd(min_dur_s, acc_01mm):
    """Put GPS into SURVEY-IN mode via CFG-VALSET."""
    return build_valset([
        (CFG_TMODE_MODE,           1,         1),   # 1 = survey-in
        (CFG_TMODE_SVIN_MIN_DUR,   min_dur_s, 4),   # seconds
        (CFG_TMODE_SVIN_ACC_LIMIT, acc_01mm,  4),   # 0.1 mm
    ])


def build_fixed_cmd(ecefX_cm, ecefY_cm, ecefZ_cm, acc_01mm):
    """
    Put GPS into FIXED ECEF position mode via CFG-VALSET.
    ecefX/Y/Z : cm (signed). acc_01mm : 0.1 mm. HP sub-cm components left unset.
    """
    return build_valset([
        (CFG_TMODE_MODE,          2,        1),   # 2 = fixed
        (CFG_TMODE_POS_TYPE,      0,        1),   # 0 = ECEF
        (CFG_TMODE_ECEF_X,        ecefX_cm, 4),
        (CFG_TMODE_ECEF_Y,        ecefY_cm, 4),
        (CFG_TMODE_ECEF_Z,        ecefZ_cm, 4),
        (CFG_TMODE_FIXED_POS_ACC, acc_01mm, 4),
    ])


def build_rst(nav_bbr_mask=0x0001, reset_mode=0x01):
    """
    CFG-RST (0x06, 0x04) — restart the receiver. Returns NO ACK.
    nav_bbr_mask : 0x0000 = hot start, 0x0001 = warm start, 0xFFFF = cold start.
    reset_mode   : 0x01 = controlled software reset (default).
    Note: a reset reverts RAM-only config (survey/fixed mode set on the RAM layer).
    """
    payload = bytearray(4)
    ustruct.pack_into('<H', payload, 0, nav_bbr_mask & 0xFFFF)
    payload[2] = reset_mode & 0xFF
    payload[3] = 0
    return ubx_msg(0x06, 0x04, payload)


def build_rate_set(meas_ms=0, nav_cycles=0, timeref=-1):
    """
    CFG-VALSET of CFG-RATE keys. A field is left unchanged when omitted:
    meas_ms / nav_cycles <= 0, or timeref < 0.
    Returns None if nothing to set.
    """
    items = []
    if meas_ms and meas_ms > 0:
        items.append((CFG_RATE_MEAS, meas_ms, 2))
    if nav_cycles and nav_cycles > 0:
        items.append((CFG_RATE_NAV, nav_cycles, 2))
    if timeref is not None and timeref >= 0:
        items.append((CFG_RATE_TIMEREF, timeref, 1))
    if not items:
        return None
    return build_valset(items)


def build_tp1_cmd(value, pulse_width, flags, cable_delay_ns):
    """
    CFG-VALSET of the TP1 (PPS) keys from the GUI's compact transport.
      value         : frequency (Hz) if TP1_F_ISFREQ set, else period (µs)
      pulse_width   : length (µs) if TP1_F_ISLENGTH set, else duty scaled by
                      TP1_DUTY_SCALE (i.e. round(duty_percent * TP1_DUTY_SCALE))
      flags         : TP1_F_* bitfield
      cable_delay_ns: antenna cable delay (ns)
    Locked and unlocked values are set the same (the integer transport only
    carries one of each); adjust the *_LOCK split here if that ever changes.
    """
    is_freq   = bool(flags & TP1_F_ISFREQ)
    is_length = bool(flags & TP1_F_ISLENGTH)
    items = [
        (CFG_TP_PULSE_DEF,        1 if is_freq   else 0,              1),
        (CFG_TP_PULSE_LENGTH_DEF, 1 if is_length else 0,              1),
        (CFG_TP_TP1_ENA,          1 if flags & TP1_F_ENABLE   else 0, 1),
        (CFG_TP_POL_TP1,          1 if flags & TP1_F_POL      else 0, 1),
        (CFG_TP_ALIGN_TO_TOW_TP1, 1 if flags & TP1_F_ALIGNTOW else 0, 1),
        (CFG_TP_SYNC_GNSS_TP1,    1 if flags & TP1_F_LOCKGPS  else 0, 1),
        (CFG_TP_USE_LOCKED_TP1,   1 if flags & TP1_F_USELOCK  else 0, 1),
        (CFG_TP_ANT_CABLEDELAY,   cable_delay_ns,                     2),
    ]
    if is_freq:
        items.append((CFG_TP_FREQ_TP1,      value, 4))
        items.append((CFG_TP_FREQ_LOCK_TP1, value, 4))
    else:
        items.append((CFG_TP_PERIOD_TP1,      value, 4))
        items.append((CFG_TP_PERIOD_LOCK_TP1, value, 4))
    if is_length:
        items.append((CFG_TP_LEN_TP1,      pulse_width, 4))
        items.append((CFG_TP_LEN_LOCK_TP1, pulse_width, 4))
    else:
        duty = pulse_width / float(TP1_DUTY_SCALE)   # percent, packed as R8
        items.append((CFG_TP_DUTY_TP1,      duty, 8))
        items.append((CFG_TP_DUTY_LOCK_TP1, duty, 8))
    return build_valset(items)


# ---- Monitoring polls (empty-payload UBX-MON requests) ----
POLL_MON_VER = ubx_msg(0x0A, 0x04, b'')   # software/hardware version strings
POLL_MON_SYS = ubx_msg(0x0A, 0x39, b'')   # CPU/mem/IO load (F9 firmware with MON-SYS)

# ---- Position poll (NAV-POSLLH: lat/lon/height + 2D/vertical accuracy) ----
POLL_NAV_POSLLH = ubx_msg(0x01, 0x02, b'')
