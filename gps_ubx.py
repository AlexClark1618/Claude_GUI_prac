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


# ---- Monitoring polls (empty-payload UBX-MON requests) ----
POLL_MON_VER = ubx_msg(0x0A, 0x04, b'')   # software/hardware version strings
POLL_MON_SYS = ubx_msg(0x0A, 0x39, b'')   # CPU/mem/IO load (F9 firmware with MON-SYS)
