###----------Borehole----------

gc.collect()

from ringBuffer import RingBuffer, push_all_cal, push_all_raw
from ringBuffer import rb_cal_count, rb_cal_wno, rb_cal_ms, rb_cal_sub
from ringBuffer import rb_raw_rf, rb_raw_ch, rb_raw_count, rb_raw_wno, rb_raw_ms, rb_raw_sub
from ringBuffer import CAPACITY_RAW, CAPACITY_CAL, raw_write_idx, cal_write_idx, cal_count, raw_count

gc.collect()

import wifi
import socket
import ustruct
import time
import sys
import array
import select
import micropython

#from PPS import init_time, pps_irq, ubx_checksum, ubx_send, ubx_recv, poll_gps_time, discipline_rtc,rtc_to_gps_wno_ms_subms
gc.collect()
#---------GPS Variables-----------
UBX_HDR = b'\xb5\x62'
RXM_TM =(2,116)   #b'\x02\x74'
TIM_TM2= (13,3)   #b'\x0d\x03'
NAV_CLOCK= (1,34)       #b'\x01\x22'
NAV_POSLLH= (1,2)       #b'\x01\x02' geodetic position (lat/lon/height + acc)
TIM_SVIN  = (13,4)      #b'\x0d\x04'
CFG_VALGET = (6,0x8b)   #b'\x06\x8b' UBX-CFG-VALGET response (F9 config read-back)
MON_VER = (10, 0x04)    #b'\x0a\x04' UBX-MON-VER  (version strings)
MON_SYS = (10, 0x39)    #b'\x0a\x39' UBX-MON-SYS  (CPU/mem/IO load)
REQUESTED_TIME_WINDOW = 1000000  #returned times (ns) will be within +/- requested_time_window of time of interested
# UBX poll messages
POLL_NAV_CLOCK = b'\xb5\x62\x01\x22\x00\x00\x23\x6a'
POLL_TIM_SVIN  = b'\xb5\x62\x0d\x04\x00\x00\x11\x40'  # poll survey-in status (TIM-SVIN is pollable on F9)

# UBX builders + CFG key constants live in gps_ubx.py — upload it to the borehole ESP too.
from gps_ubx import (build_valget, build_svin_cmd, build_fixed_cmd,
                     build_rst, build_rate_set, build_tp1_cmd,
                     POLL_MON_VER, POLL_MON_SYS, POLL_NAV_POSLLH,
                     CFG_TMODE_MODE, CFG_TMODE_POS_TYPE,
                     CFG_TMODE_ECEF_X, CFG_TMODE_ECEF_Y, CFG_TMODE_ECEF_Z,
                     CFG_TMODE_FIXED_POS_ACC,
                     CFG_RATE_MEAS, CFG_RATE_NAV, CFG_RATE_TIMEREF, RATE_KEYS,
                     TP1_KEYS, CFG_TP_TP1_ENA, CFG_TP_PULSE_DEF, CFG_TP_PULSE_LENGTH_DEF,
                     CFG_TP_POL_TP1, CFG_TP_ALIGN_TO_TOW_TP1, CFG_TP_SYNC_GNSS_TP1,
                     CFG_TP_USE_LOCKED_TP1, CFG_TP_FREQ_TP1, CFG_TP_FREQ_LOCK_TP1,
                     CFG_TP_PERIOD_TP1, CFG_TP_PERIOD_LOCK_TP1,
                     CFG_TP_LEN_TP1, CFG_TP_LEN_LOCK_TP1,
                     CFG_TP_DUTY_TP1, CFG_TP_DUTY_LOCK_TP1, CFG_TP_ANT_CABLEDELAY,
                     TP1_F_ENABLE, TP1_F_ISFREQ, TP1_F_POL, TP1_F_ALIGNTOW,
                     TP1_F_LOCKGPS, TP1_F_USELOCK, TP1_F_ISLENGTH, TP1_DUTY_SCALE)

numMeas=1
global tcoll0
tcoll0=0

# ---------- Detector identity ----------
# Detector number is read from config.txt (set per-board) so the server can map
# this device (sent in the inst=1 info packet and control-channel announce).
try:
    with open('config.txt') as f:
        detector_num = f.read().strip()
        print("This is Detector " + detector_num)
except OSError:
    detector_num = "0"  # default if not yet set
    print("No config.txt found, using default detector number:", detector_num)

# ---------- Wi-Fi Setup ----------
ssid = 'Test_Omada_Wi-Fi'
#ssid = 'AirShower2.4G'
password = 'Air$shower24'

wifi.con_to_wifi(ssid, password)
      
def clear_wifi_rx_buffer():
    global s
    if not s:
        print("No socket to clear.")
        return

    total = 0

    try:
        while True:
            data = s.recv(1024)
            if not data:
                break
            total += len(data)

    except OSError:
        pass  # buffer empty

    print("Wifi RX buffer cleared:", total, "bytes")

# ---------- Wifi and Socket Variables -----------
mac_id = wifi.wlan.config('mac')[-1]  # last byte of MAC
print('mac id:', mac_id)
ip, subnet, gateway, dns = wifi.wlan.ifconfig()
ip_last_byte = int(ip.split('.')[-1])
print("ESP IP:", ip_last_byte)

#HOST = '134.69.77.61' #Karbon Computer
HOST = '192.168.0.27' #Local network
PORT = 12345
PORT_CTRL = 12347   # dedicated slow-control channel (survey/fix/PPS/etc), polled between data sends

# Control-channel state (set up just before the main loop, see below)
ctrl_s = None
ctrl_poller = None
ctrl_rx_buf = bytearray()

# ---------- Socket Functions ----------

def connect_socket(host, port):
    while True:
        try:
            gc.collect()
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.connect((host, port))
            print("Socket connected.")
            gc.collect()

            return s
        except Exception as e:
            print("Failed to connect socket:", e)
            time.sleep(1)
            continue     

s = connect_socket(HOST,PORT)
#s.setblocking(False)
s.settimeout(.05) #50ms timeout

poller = select.poll()
poller.register(s, select.POLLIN)

def reconnect_socket(sock, poller):
    try:
        poller.unregister(sock)
    except:
        pass

    try:
        sock.close()
        gc.collect()
    except:
        pass

    time.sleep(0.1)

    s = connect_socket(HOST, PORT)

    poller.register(s, select.POLLIN)
    return s

# ---------- Send and Receive Functions -----------
def send_data(d):
    global s
    try:
        return s.send(d)

    except OSError as e:
        if e.args[0] in [11, 110]:  # EAGAIN, ETIMEDOUT, ECONNRESET
            print("No data to send")
            return None
        else:
            print("Send error:", e)
            #error_msg = (100, mac_id, 2, 0, 0, 0, 0, 0, 0, 0)
            s = reconnect_socket(s, poller)

            packet = data_packing(send_packet_format, 100, mac_id, 2, 0, 0, 0, 0, 0, 0, 0)
            s.send(packet)

            return None

# ---------- Data Packing -----------
send_packet_format = "!iiiiiiiiii"
# Inbound control packets from the server/GUI are 5 ints (inst, w_num, ms, sub_ms, event_num)
request_packet_format = '!iiiii'
rx_packet_size = ustruct.calcsize(request_packet_format)

def data_packing(packet_format,v0,v1,v2,v3,v4,v5,v6,v7,v8,v9):
    try:
        return ustruct.pack(packet_format,v0,v1,v2,v3,v4,v5,v6,v7,v8,v9)
    except Exception as e:
        print("Error in data packing", e)
        return None

# ---------- GPS Functions ----------
junk=bytearray(1024)

def clearRxBuf():
    print('clearRxBuf')
    gc.collect()
    try:
        #print('clearRxBuf:', uart1.any(),'bytes')
        print('buffer cleared of ',uart1.any(), 'bytes\n')
        while uart1.any()>1024:
            junk=(uart1.read(1024))
        while uart1.any():
            uart1.read()
    except Exception as e:
        print("Error in clear buffer:", e)
        #error_msg = (100, mac_id, 4, 0, 0, 0, 0, 0, 0, 0)
        packet = data_packing(send_packet_format, 100, mac_id, 4, 0, 0, 0, 0, 0, 0, 0)
        send_data(packet)

def maxRxBuf(n,n2):
    try:
        nbuf = uart1.any()
        if nbuf > n:
            nClear=0
            while (uart1.any() > n):
                #nskim = uart1.any()-n + 1000
                nClear += 1
                readData(1)
                readData(1)
                #uart1.readinto(junk, 1024)
                #junk=uart1.read(1024)
            print('buffer cleared of ',nClear, "50ms segments")
        elif nbuf > n2:
            readData(1)  #read and parse 1kb segment of raw data
            readData(1)  #read and parse 60byte segment of cal data
            print('buffer cleared of a 50ms segment')
    except Exception as e:
        print('maxRxbuf exception',e)
        #error_msg = (100, mac_id, 5, 0, 0, 0, 0, 0, 0, 0)
        packet = data_packing(send_packet_format,100, mac_id, 5, 0, 0, 0, 0, 0, 0, 0)
        send_data(packet)

hdr = bytearray(1)
def findUBX_HDR():
    try:
        state = 0  # 0 = looking for 0xB5, 1 = looking for 0x62
        n=0
        i=0
        while True:
            if uart1.any() == 0:
                i=i+1
                time.sleep_ms(1)
                if (i-i//1000*1000) == 0:
                    print('ubx',end='.')
                continue

            uart1.readinto(hdr)  # integer, no bytes object
            b = hdr[0]
            n=n+1
            #print(b,end=' ')
            if state == 0:
                if b == 0xB5:
                    state = 1
            else:
                if b == 0x62:
                    return n # header found
                else:
                    state = 0  
    except Exception as e:
        print("findUBX_HDR error:",e)
        #error_msg = (100, mac_id, 6, 0, 0, 0, 0, 0, 0, 0)
        packet = data_packing(send_packet_format, 100, mac_id, 6, 0, 0, 0, 0, 0, 0, 0)
        send_data(packet)

hdr2 = bytearray(4)
def findHDR2():
    try:
        while uart1.any() < 4:
            time.sleep_ms(0)
        uart1.readinto(hdr2)
        cls  = hdr2[0]
        msg  = hdr2[1]
        leni = hdr2[2] | (hdr2[3] << 8)
        #print('HDR2', cls, msg, leni)
        # optional sanity check
    #     if leni > 2048:
    #         raise ValueError("Invalid UBX length")
        return cls, msg, leni
    except Exception as e:
        print("findUBX_HDR2 error:",e)
        #error_msg = (100, mac_id, 7, 0, 0, 0, 0, 0, 0, 0)
        packet = data_packing(send_packet_format, 100, mac_id, 7, 0, 0, 0, 0, 0, 0, 0)
        send_data(packet)

MAX_TOI=64

toi_RF     = array.array("B", bytearray(MAX_TOI))
toi_valid     = array.array("B", bytearray(MAX_TOI))
toi_ch     = array.array("B", bytearray(MAX_TOI))
toi_wno     = array.array("H", bytearray(MAX_TOI))
toi_Ms     = array.array("I", bytearray(MAX_TOI))
toi_SubMs     = array.array("I", bytearray(MAX_TOI))

plb = bytearray(2048)
ck  = bytearray(2)

def readData(det=None):
    # det is accepted for compatibility with the shared control-channel pollers
    # (handle_control / poll_* call readData(1)); the borehole loop calls readData().
    #print('readData')
    global slope, deltaT, NEvents0, NEvents1

    try:
        # Find UBX sync
        findUBX_HDR()

        cls, msg, leni = findHDR2()
        if leni > 2048:
            return (0, 0, 0, 0, 0)

        # Wait cooperatively for payload + checksum
        needed = leni + 2
        while uart1.any() < needed:
            time.sleep_ms(1)

        # Read payload + checksum without allocating
        uart1.readinto(plb, leni)
        uart1.readinto(ck, 2)

        # ---------- RXM-TM ----------
        if (cls, msg) == RXM_TM:
            version = plb[0]
            numMeas = plb[1]

            # 50 ms RXM-TM windows: accumulate observed-rate counters for inst 98
            deltaT += 50
            base = 8
            for _ in range(numMeas):
                edgeInfo = (
                    plb[base+0] |
                    (plb[base+1] << 8) |
                    (plb[base+2] << 16) |
                    (plb[base+3] << 24)
                )

                RF = (edgeInfo >> 4) & 1
                ch = edgeInfo & 1

                if RF == 0:
                    if ch == 0:
                        NEvents0 += 1
                    elif ch == 1:
                        NEvents1 += 1

                count = plb[base+4] | (plb[base+5] << 8)
                wno   = plb[base+6] | (plb[base+7] << 8)

                towMs = (
                    plb[base+8] |
                    (plb[base+9] << 8) |
                    (plb[base+10] << 16) |
                    (plb[base+11] << 24)
                )

                towSubMs = (
                    plb[base+12] |
                    (plb[base+13] << 8) |
                    (plb[base+14] << 16) |
                    (plb[base+15] << 24)
                )

                push_all_raw(RF, ch, wno, towMs, towSubMs, count)

                base += 24

        # ---------- TIM-TM2 ----------
        elif (cls, msg) == TIM_TM2:
            ch = plb[0]
            edgeInfo = plb[1]

            edgeF     = (edgeInfo >> 2) & 1
            edgeR     = (edgeInfo >> 7) & 1
            timeValid = (edgeInfo >> 6) & 1

            count = plb[2] | (plb[3] << 8)
            wnoR  = plb[4] | (plb[5] << 8)
            wnoF  = plb[6] | (plb[7] << 8)

            towMsR = (
                plb[8] |
                (plb[9] << 8) |
                (plb[10] << 16) |
                (plb[11] << 24)
            )

            towSubMsR = (
                plb[12] |
                (plb[13] << 8) |
                (plb[14] << 16) |
                (plb[15] << 24)
            )

            towMsF = (
                plb[16] |
                (plb[17] << 8) |
                (plb[18] << 16) |
                (plb[19] << 24)
            )

            towSubMsF = (
                plb[20] |
                (plb[21] << 8) |
                (plb[22] << 16) |
                (plb[23] << 24)
            )

            accEst = (
                plb[24] |
                (plb[25] << 8) |
                (plb[26] << 16) |
                (plb[27] << 24)
            )

            push_all_cal(wnoR, towMsR, towSubMsR, count)
            return (wnoR, towMsR, towSubMsR, timeValid, ch)

        # ---------- NAV-CLOCK ----------
        elif (cls, msg) == NAV_CLOCK:
            iTOW = (
                plb[0] |
                (plb[1] << 8) |
                (plb[2] << 16) |
                (plb[3] << 24)
            )

            iclkBias  = ustruct.unpack_from('<i', plb, 4)[0]
            iclkDrift = ustruct.unpack_from('<i', plb, 8)[0]

            tAcc = (
                plb[12] |
                (plb[13] << 8) |
                (plb[14] << 16) |
                (plb[15] << 24)
            )

            fAcc = (
                plb[16] |
                (plb[17] << 8) |
                (plb[18] << 16) |
                (plb[19] << 24)
            )

            slope = iclkDrift

        # ---------- TIM-SVIN (survey-in status) ----------
        elif (cls, msg) == TIM_SVIN:
            # 0 iTOW U4 | 4 dur U4 s | 8 meanX I4 cm | 12 meanY I4 | 16 meanZ I4
            # 20 meanAcc U4 0.1mm | 24 obs U2 | 26 valid U1 | 27 active U1
            dur     = plb[4]  | (plb[5]  << 8) | (plb[6]  << 16) | (plb[7]  << 24)
            meanX   = ustruct.unpack_from('<i', plb, 8)[0]
            meanY   = ustruct.unpack_from('<i', plb, 12)[0]
            meanZ   = ustruct.unpack_from('<i', plb, 16)[0]
            meanAcc = plb[20] | (plb[21] << 8) | (plb[22] << 16) | (plb[23] << 24)
            obs     = plb[24] | (plb[25] << 8)
            valid   = plb[26]
            active  = plb[27]
            return ('svin', dur, meanX, meanY, meanZ, meanAcc, obs, valid, active)

        # ---------- NAV-POSLLH (geodetic position + accuracy) ----------
        elif (cls, msg) == NAV_POSLLH:
            # 0 iTOW U4 | 4 lon I4 1e-7deg | 8 lat I4 | 12 height I4 mm
            # 16 hMSL I4 mm | 20 hAcc U4 mm | 24 vAcc U4 mm
            lon    = ustruct.unpack_from('<i', plb, 4)[0]
            lat    = ustruct.unpack_from('<i', plb, 8)[0]
            height = ustruct.unpack_from('<i', plb, 12)[0]
            hMSL   = ustruct.unpack_from('<i', plb, 16)[0]
            hAcc   = plb[20] | (plb[21] << 8) | (plb[22] << 16) | (plb[23] << 24)
            vAcc   = plb[24] | (plb[25] << 8) | (plb[26] << 16) | (plb[27] << 24)
            return ('posllh', lon, lat, height, hMSL, hAcc, vAcc)

        # ---------- CFG-VALGET (config read-back, F9 interface) ----------
        elif (cls, msg) == CFG_VALGET:
            # version(U1) layer(U1) position(U2), then 4-byte key (LE) + value;
            # value width from key size code bits 28-30: 0x2->1B 0x3->2B 0x4->4B 0x5->8B.
            cfg = {}
            idx = 4
            while idx + 4 <= leni:
                key = ustruct.unpack_from('<I', plb, idx)[0]
                idx += 4
                size_code = (key >> 28) & 0x07
                if   size_code in (1, 2): vlen, fmt = 1, '<b'
                elif size_code == 3:      vlen, fmt = 2, '<h'
                elif size_code == 4:      vlen, fmt = 4, '<i'
                elif size_code == 5:      vlen, fmt = 8, '<q'
                else:                     break
                if idx + vlen > leni:
                    break
                cfg[key] = ustruct.unpack_from(fmt, plb, idx)[0]
                idx += vlen
            return ('valget', cfg)

        # ---------- MON-VER (version strings) ----------
        elif (cls, msg) == MON_VER:
            # swVersion = char[30] at offset 0
            return ('ver', bytes(plb[0:30]))

        # ---------- MON-SYS (CPU / mem / IO load + counts) ----------
        elif (cls, msg) == MON_SYS:
            cpuLoad    = plb[2]
            cpuLoadMax = plb[3]
            memUsage   = plb[4]
            memUsageMax= plb[5]
            ioUsage    = plb[6]
            ioUsageMax = plb[7]
            runTime    = plb[8] | (plb[9] << 8) | (plb[10] << 16) | (plb[11] << 24)
            notice     = plb[12] | (plb[13] << 8)
            warn       = plb[14] | (plb[15] << 8)
            error      = plb[16] | (plb[17] << 8)
            temp       = ustruct.unpack_from('<b', plb, 18)[0] if leni >= 19 else 0
            return ('monsys', cpuLoad, cpuLoadMax, memUsage, memUsageMax,
                    ioUsage, ioUsageMax, runTime, notice, warn, error, temp)

        # Yield once after heavy UART work
        time.sleep_ms(0)
        return (0, 0, 0, 0, 0)

    except MemoryError as e:
        sys.print_exception(e)
        print("Memory Error in ReadData")
        machine.reset()

    except Exception as e:
        sys.print_exception(e)
        print("Error in readData", e)
        packet = data_packing(send_packet_format, 100, mac_id, 9, 0, 0, 0, 0, 0, 0, 0)
        send_data(packet)
        return (0, 0, 0, 0, 0)

# ---------- Control Channel ----------
# Slow-control commands (survey/fix/PPS/rate/version) from the GUI arrive on a
# dedicated socket so they are not stuck behind the inst=99 data stream on the
# main socket. Replies go out on this same control socket via send_ctrl.

def connect_ctrl_socket():
    while True:
        try:
            gc.collect()
            cs = socket.socket()
            cs.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            cs.connect((HOST, PORT_CTRL))
            cs.setblocking(False)
            print("Control socket connected.")
            return cs
        except Exception as e:
            print("Failed to connect control socket:", e)
            time.sleep(1)

def reconnect_ctrl_socket():
    global ctrl_poller
    try:
        ctrl_poller.unregister(ctrl_s)
    except:
        pass
    try:
        ctrl_s.close()
    except:
        pass
    time.sleep(0.1)
    cs = connect_ctrl_socket()
    ctrl_poller.register(cs, select.POLLIN)
    try:  # re-announce mac_id so the server re-maps this control socket
        cs.send(data_packing(send_packet_format, 1, mac_id, ip_last_byte, int(detector_num), 0, 0, 0, 0, 0, 0))
    except Exception:
        pass
    return cs

def send_ctrl(d):
    """Send a full control packet. The control socket is non-blocking, so loop
    until every byte is out, with a bounded EAGAIN backoff."""
    global ctrl_s
    if d is None:
        return None
    try:
        mv = memoryview(d)
        sent = 0
        retries = 0
        while sent < len(d):
            try:
                n = ctrl_s.send(mv[sent:])
                if n:
                    sent += n
                    retries = 0
            except OSError as e:
                if e.args[0] in (11,):       # EAGAIN: send buffer full, wait briefly
                    retries += 1
                    if retries > 100:        # ~200 ms cap so we never stall the loop
                        print("Ctrl send: buffer stuck, dropping packet")
                        return None
                    time.sleep_ms(2)
                    continue
                raise
        return sent
    except OSError as e:
        if e.args[0] in (110,):              # ETIMEDOUT
            return None
        print("Ctrl send error:", e)
        ctrl_s = reconnect_ctrl_socket()
        return None

def _duty_q_to_scaled(raw_q):
    """Reinterpret an R8 duty key's raw 64-bit value as a double, scaled to the
    integer transport: round(percent * TP1_DUTY_SCALE)."""
    try:
        duty = ustruct.unpack('<d', ustruct.pack('<q', raw_q))[0]
    except Exception:
        return 0
    return int(round(duty * TP1_DUTY_SCALE))

def handle_control(inst, w_num, ms, sub_ms, event_num):
    """Service one slow-control command from the GUI (see ServerControlGUI.py)."""
    if inst == 201:   # Survey-in status query
        clearRxBuf()
        uart1.write(POLL_TIM_SVIN)
        svin_result = None
        for _ in range(40):
            res = readData(1)
            if isinstance(res, tuple) and len(res) == 9 and res[0] == 'svin':
                svin_result = res
                break
        if svin_result:
            _, dur, meanX, meanY, meanZ, meanAcc, obs, valid, active = svin_result
            send_ctrl(data_packing(send_packet_format, 201, mac_id, valid, active, dur, meanX, meanY, meanZ, meanAcc, obs))
            print("SVIN:", valid, active, dur, "s  acc:", meanAcc, "0.1mm")
        else:
            print("SVIN poll timed out")
            send_ctrl(data_packing(send_packet_format, 100, mac_id, 17, 0, 0, 0, 0, 0, 0, 0))

    elif inst == 202:  # Start survey-in (w_num=min_dur_s, ms=acc_01mm)
        uart1.write(build_svin_cmd(w_num, ms))
        print("Survey-in started: min_dur=", w_num, "s  acc_limit=", ms, "0.1mm")
        send_ctrl(data_packing(send_packet_format, 202, mac_id, 0, 0, 0, 0, 0, 0, 0, 0))

    elif inst == 203:  # Set FIXED coords (w_num=X_cm, ms=Y_cm, sub_ms=Z_cm, event_num=acc_01mm)
        uart1.write(build_fixed_cmd(w_num, ms, sub_ms, event_num))
        print("Fixed position set:", w_num, ms, sub_ms, "cm  acc:", event_num, "0.1mm")
        send_ctrl(data_packing(send_packet_format, 203, mac_id, 0, 0, 0, 0, 0, 0, 0, 0))

    elif inst == 204:  # Probe configured fixed-position (CFG-VALGET read-back)
        cfg = poll_valget([
            CFG_TMODE_MODE, CFG_TMODE_POS_TYPE,
            CFG_TMODE_ECEF_X, CFG_TMODE_ECEF_Y, CFG_TMODE_ECEF_Z,
            CFG_TMODE_FIXED_POS_ACC], CFG_TMODE_MODE)
        if cfg is not None:
            mode        = cfg.get(CFG_TMODE_MODE, -1)
            pos_type    = cfg.get(CFG_TMODE_POS_TYPE, 0)
            ecefX       = cfg.get(CFG_TMODE_ECEF_X, 0)
            ecefY       = cfg.get(CFG_TMODE_ECEF_Y, 0)
            ecefZ       = cfg.get(CFG_TMODE_ECEF_Z, 0)
            fixedPosAcc = cfg.get(CFG_TMODE_FIXED_POS_ACC, 0)
            send_ctrl(data_packing(send_packet_format, 204, mac_id, mode, pos_type, 0, ecefX, ecefY, ecefZ, fixedPosAcc, 0))
            print("TMODE:", mode, ecefX, ecefY, ecefZ, "acc:", fixedPosAcc, "0.1mm")
        else:
            print("CFG-VALGET (TMODE) poll timed out")
            send_ctrl(data_packing(send_packet_format, 100, mac_id, 18, 0, 0, 0, 0, 0, 0, 0))

    elif inst == 205:  # Restart GPS (w_num=navBbrMask, ms=resetMode). No ACK from receiver.
        uart1.write(build_rst(w_num, ms))
        print("GPS restart sent: navBbrMask=", w_num, " resetMode=", ms)
        send_ctrl(data_packing(send_packet_format, 205, mac_id, 0, 0, 0, 0, 0, 0, 0, 0))

    elif inst == 206:  # Read CFG-RATE (report) via CFG-VALGET
        cfg = poll_valget(RATE_KEYS, CFG_RATE_MEAS)
        if cfg is not None:
            meas_ms = cfg.get(CFG_RATE_MEAS, 0)
            nav     = cfg.get(CFG_RATE_NAV, 0)
            timeref = cfg.get(CFG_RATE_TIMEREF, 0)
            send_ctrl(data_packing(send_packet_format, 206, mac_id, timeref, 0, 0, meas_ms, nav, 0, 0, 0))
            print("RATE: meas=", meas_ms, "ms  nav=", nav, "  timeref=", timeref)
        else:
            print("CFG-VALGET (RATE) poll timed out")
            send_ctrl(data_packing(send_packet_format, 100, mac_id, 19, 0, 0, 0, 0, 0, 0, 0))

    elif inst == 207:  # Set CFG-RATE (w_num=meas_ms, ms=nav_cycles, sub_ms=timeref; <=0 / <0 = skip)
        cmd = build_rate_set(w_num, ms, sub_ms)
        if cmd is not None:
            uart1.write(cmd)
            print("RATE set: meas=", w_num, "ms  nav=", ms, "  timeref=", sub_ms)
        send_ctrl(data_packing(send_packet_format, 207, mac_id, 0, 0, 0, 0, 0, 0, 0, 0))

    elif inst == 208:  # GPS comms test — return MON-VER swVersion (up to 32 chars packed into 8 ints)
        clearRxBuf()
        uart1.write(POLL_MON_VER)
        sw = None
        for _ in range(40):
            res = readData(1)
            if isinstance(res, tuple) and len(res) == 2 and res[0] == 'ver':
                sw = res[1]
                break
        if sw is not None:
            sw = bytes(sw)[:32]
            sw = sw + b'\x00' * (32 - len(sw))
            vi = ustruct.unpack('!8i', sw)   # 32 bytes -> 8 ints (round-trips on the GUI)
            send_ctrl(data_packing(send_packet_format, 208, mac_id,
                                   vi[0], vi[1], vi[2], vi[3], vi[4], vi[5], vi[6], vi[7]))
            print("MON-VER:", sw)
        else:
            print("MON-VER poll timed out")
            send_ctrl(data_packing(send_packet_format, 100, mac_id, 20, 0, 0, 0, 0, 0, 0, 0))

    elif inst == 209:  # Read TP1 (PPS) config via CFG-VALGET
        cfg = poll_valget(TP1_KEYS, CFG_TP_TP1_ENA)
        if cfg is not None:
            flags = 0
            if cfg.get(CFG_TP_TP1_ENA):          flags |= TP1_F_ENABLE
            if cfg.get(CFG_TP_PULSE_DEF):        flags |= TP1_F_ISFREQ
            if cfg.get(CFG_TP_POL_TP1):          flags |= TP1_F_POL
            if cfg.get(CFG_TP_ALIGN_TO_TOW_TP1): flags |= TP1_F_ALIGNTOW
            if cfg.get(CFG_TP_SYNC_GNSS_TP1):    flags |= TP1_F_LOCKGPS
            if cfg.get(CFG_TP_USE_LOCKED_TP1):   flags |= TP1_F_USELOCK
            if flags & TP1_F_ISFREQ:
                val_un   = cfg.get(CFG_TP_FREQ_TP1, 0)
                val_lock = cfg.get(CFG_TP_FREQ_LOCK_TP1, 0)
            else:
                val_un   = cfg.get(CFG_TP_PERIOD_TP1, 0)
                val_lock = cfg.get(CFG_TP_PERIOD_LOCK_TP1, 0)
            if cfg.get(CFG_TP_PULSE_LENGTH_DEF):       # 1 = length (µs)
                flags |= TP1_F_ISLENGTH
                pw_un   = cfg.get(CFG_TP_LEN_TP1, 0)
                pw_lock = cfg.get(CFG_TP_LEN_LOCK_TP1, 0)
            else:                                       # 0 = duty cycle (R8 %)
                pw_un   = _duty_q_to_scaled(cfg.get(CFG_TP_DUTY_TP1, 0))
                pw_lock = _duty_q_to_scaled(cfg.get(CFG_TP_DUTY_LOCK_TP1, 0))
            cable    = cfg.get(CFG_TP_ANT_CABLEDELAY, 0)
            send_ctrl(data_packing(send_packet_format, 209, mac_id, flags, 0, 0,
                                   val_un, val_lock, pw_un, pw_lock, cable))
            print("TP1:", "freq" if flags & TP1_F_ISFREQ else "period", val_un, "/", val_lock, " flags", flags)
        else:
            print("CFG-VALGET (TP1) poll timed out")
            send_ctrl(data_packing(send_packet_format, 100, mac_id, 21, 0, 0, 0, 0, 0, 0, 0))

    elif inst == 210:  # Set TP1 (PPS): w_num=value, ms=pulse_width, sub_ms=flags, event_num=cable_ns
        uart1.write(build_tp1_cmd(w_num, ms, sub_ms, event_num))
        print("TP1 set: value=", w_num, " pulse=", ms, " flags=", sub_ms, " cable=", event_num, "ns")
        send_ctrl(data_packing(send_packet_format, 210, mac_id, 0, 0, 0, 0, 0, 0, 0, 0))

    elif inst == 211:  # Read NAV-POSLLH (lat/lon/height + horizontal/vertical accuracy)
        clearRxBuf()
        uart1.write(POLL_NAV_POSLLH)
        pos = None
        for _ in range(40):
            res = readData(1)
            if isinstance(res, tuple) and len(res) == 7 and res[0] == 'posllh':
                pos = res
                break
        if pos:
            _, lon, lat, height, hMSL, hAcc, vAcc = pos
            send_ctrl(data_packing(send_packet_format, 211, mac_id, vAcc, 0, 0,
                                   lon, lat, height, hMSL, hAcc))
            print("POSLLH: lat", lat, " lon", lon, " h", height, "mm  hAcc", hAcc, " vAcc", vAcc)
        else:
            print("NAV-POSLLH poll timed out")
            send_ctrl(data_packing(send_packet_format, 100, mac_id, 22, 0, 0, 0, 0, 0, 0, 0))

def poll_mon_sys():
    """Poll MON-SYS and return ('monsys', cpuLoad, cpuLoadMax, memUsage,
    memUsageMax, ioUsage, ioUsageMax, runTime, notice, warn, error, temp) or None."""
    uart1.write(POLL_MON_SYS)
    for _ in range(40):
        res = readData(1)
        if isinstance(res, tuple) and res and res[0] == 'monsys':
            return res
    return None

def poll_valget(keys, marker_key):
    """Send a CFG-VALGET for `keys` and return the {key:value} dict once a
    response containing `marker_key` arrives (or None on timeout)."""
    clearRxBuf()
    uart1.write(build_valget(keys))
    for _ in range(40):
        res = readData(1)
        if isinstance(res, tuple) and len(res) == 2 and res[0] == 'valget' and marker_key in res[1]:
            return res[1]
    return None

def service_control():
    """Poll the control socket and run any pending command NOW. Cheap when idle
    (poll(0) returns immediately)."""
    global ctrl_s, ctrl_rx_buf
    if ctrl_s is None:
        return
    try:
        if not ctrl_poller.poll(0):
            return
        data = ctrl_s.recv(256)
    except OSError as e:
        if e.args[0] in (11,):  # EAGAIN
            return
        ctrl_s = reconnect_ctrl_socket()
        return
    if not data:                # server closed the control socket
        ctrl_s = reconnect_ctrl_socket()
        return
    ctrl_rx_buf.extend(data)
    while len(ctrl_rx_buf) >= rx_packet_size:
        pkt = ctrl_rx_buf[:rx_packet_size]
        ctrl_rx_buf = ctrl_rx_buf[rx_packet_size:]
        try:
            inst, w_num, ms, sub_ms, event_num = ustruct.unpack(request_packet_format, pkt)
        except Exception:
            ctrl_rx_buf = bytearray()
            break
        handle_control(inst, w_num, ms, sub_ms, event_num)

# ---------- Info Packets ----------
packet = data_packing(send_packet_format, 100, mac_id, ip_last_byte, 15, time.ticks_ms(), 0, 0, 0, 0, 0)
send_data(packet)

time.sleep(0.1)

packet = data_packing(send_packet_format, 1, mac_id, ip_last_byte, int(detector_num), 0, 0, 0, 0, 0, 0)
send_data(packet)

# ---------- Control channel setup ----------
ctrl_s = connect_ctrl_socket()
ctrl_poller = select.poll()
ctrl_poller.register(ctrl_s, select.POLLIN)
# Announce mac_id so the server maps this control socket to this ESP
send_ctrl(data_packing(send_packet_format, 1, mac_id, ip_last_byte, int(detector_num), 0, 0, 0, 0, 0, 0))

cpu_T0 = time.ticks_ms()   # 60 s timer for autonomous GPS CPU telemetry (MON-SYS)

# ---------- Main Loop ----------

NEvents0 = 0
NEvents1 = 0
deltaT = 0

#initialise Valid, slope  and offset 
uart1.write(POLL_NAV_CLOCK) #Poll Nav Clock
slope=0
tRaw1=None
tCal1 = None
Valid = 0
res=None
oldtowMsR = 0            
oldtowMs = 0            
countdtMs = 0    

while ((slope == 0) or (tRaw1 == None) or (Valid == 0)):
    try:
        print('\ninit while loop', slope, tRaw1, Valid, res)
        uart1.write(POLL_NAV_CLOCK) #Poll Nav Clock
        for i in range(4):
            res=readData()
            if (res[1] > 0):
                if (res[3] > 0):
                    Valid = 1
                else:
                    Valid = 0
                    print("GPS not locked")
                    time.sleep(1)# wait 1 second before trying again
                    break

            lastCount=rb_cal_count.get(0)  #latest cal count
                
            for i in range(raw_count[0]):
                if rb_raw_count.get(i)==lastCount:
                    tCal1=rb_cal_ms.get(0)*1000000+(rb_cal_sub.get(0))
                    tRaw1=rb_raw_ms.get(i)*1000000+(rb_raw_sub.get(i)//1000)
                    break
    except Exception as e: #I believe this is just due to readData memory allocation
        sys.print_exception(e)
        print("Error in gps initialization")
        #error_msg = (100, mac_id, 10, 0, 0, 0, 0, 0, 0, 0)
        packet = data_packing(send_packet_format, 100, mac_id, 10, 0, 0, 0, 0, 0, 0, 0)
        send_data(packet)
        continue

CC=0
reqCount=0
global toi_len

clearRxBuf()

T0=time.ticks_us()
event_num = 0 #Keeps track of borehole events (could be moved to server)
# Reset observed-rate counters after the GPS-init reads (declared above)
NEvents0 = 0
NEvents1 = 0
deltaT = 0

tx_packet_size = ustruct.calcsize(send_packet_format)
send_buffer = bytearray(800)   # +stats packets (inst 98 + heap) packed alongside data
send_buffer_index = 0

#init the PPS interrupt
#init_time(uart1)
RFRaw       = rb_raw_rf.buffer
chRaw       = rb_raw_ch.buffer
countRaw    = rb_raw_count.buffer
towMsRaw    = rb_raw_ms.buffer
towSubMsRaw = rb_raw_sub.buffer
countCal    = rb_cal_count.buffer
towMsCal    = rb_cal_ms.buffer
towSubMsCal = rb_cal_sub.buffer

while True:
    #print(gc.mem_free())
    #print(gc.mem_alloc())
    gc.collect()
    #micropython.mem_info()

    try:
        if not wifi.wlan.isconnected():
            wifi.con_to_wifi(ssid, password, max_retries = 10)

        service_control()   # check for slow-control (GPS comms) commands from the GUI

        # Autonomous GPS CPU/health telemetry once a minute (on the control channel)
        if time.ticks_diff(time.ticks_ms(), cpu_T0) > 60000:
            cpu_T0 = time.ticks_ms()
            cpu = poll_mon_sys()
            if cpu:
                (_, cpuLoad, cpuLoadMax, memUsage, memUsageMax,
                 ioUsage, ioUsageMax, runTime, notice, warn, error, temp) = cpu
                send_ctrl(data_packing(send_packet_format, 91, mac_id,
                                       notice, warn, error, 0, 0, 0, 0, 0))
                send_ctrl(data_packing(send_packet_format, 90, mac_id,
                                       cpuLoad, memUsage, ioUsage, temp,
                                       cpuLoadMax, memUsageMax, ioUsageMax, runTime))

        res = (0,0,0)
        maxRxBuf(15000, 20000)

        # Reset ring buffers, then alias to the names the rest of the loop uses.
        # Replaces: RFRaw=[]; chRaw=[]; countRaw=[]; ... ; toi = []
        raw_write_idx[0] = 0; raw_count[0] = 0
        cal_write_idx[0] = 0; cal_count[0] = 0

        T1=time.ticks_us()
        diff = time.ticks_diff(T1, T0)
        if diff > 5_000_000:
            ##print(gc.mem_free())
            #print(gc.mem_alloc())
            micropython.mem_info()

            uart1.write(POLL_NAV_CLOCK)
            data_msg = (96, mac_id, 0, 0, 0,
                        0, 0, 0, gc.mem_free(), gc.mem_alloc())
            print(data_msg)
            ustruct.pack_into(send_packet_format, send_buffer,
                              send_buffer_index, *data_msg)
            send_buffer_index += tx_packet_size

            # Observed-rate stats for the GUI Data Slow Control tab (inst 98):
            #   NEvents0 / NEvents1 over deltaT(ms) -> Ch0 / Ch1 / total Hz
            stats_msg = (98, mac_id, NEvents0, NEvents1, deltaT,
                         0, 0, 0, 0, 0)
            ustruct.pack_into(send_packet_format, send_buffer,
                              send_buffer_index, *stats_msg)
            send_buffer_index += tx_packet_size

            NEvents0 = 0
            NEvents1 = 0
            deltaT = 0
            T0=T1
    
        while (res[0] == 0) or (res[4] == 1):
            res = readData()
            #print(res)
            time.sleep(0)

        timeValid = res[3]
        wnoToi=res[0]
        lastC=cal_count[0]-1   # was len(countCal)-1
        lastR=raw_count[0]-1   # was len(countRaw)-1

        for i in range(lastR,-1,-1):
            if countRaw[i]==countCal[lastC]:
                tCal1=towMsCal[lastC]*1000000+(towSubMsCal[lastC])
                tRaw1=towMsRaw[i]*1000000+ towSubMsRaw[i]//1000
                break

        lenRaw=raw_count[0]   # was len(towMsRaw)

        # Was: toi.append((RFRaw[i],timeValid,chRaw[i],wnoToi,Ms,SubMs, countRaw[i], i))
        # Now: pack straight into send_buffer
        for i in range(lenRaw):
            if chRaw[i] == 0:
                tRaw=towMsRaw[i]*1000000+ towSubMsRaw[i]//1000
                res=(tRaw-tRaw1)-((tRaw-tRaw1)*slope//1000000000)+tCal1
                Ms=res//1000000
                SubMs = res - Ms * 1000000
                data_msg = (99, mac_id, RFRaw[i], timeValid, chRaw[i],
                            wnoToi, Ms, SubMs, event_num, countRaw[i])
                #print("datamsg1:",data_msg)
                ustruct.pack_into(send_packet_format, send_buffer,
                                  send_buffer_index, *data_msg)
                send_buffer_index += tx_packet_size

        # Was: scan toi for ch==0 rises, append ch==1 matches to toi
        # Now: scan raw for ch==0 rises, pack ch==1 matches into send_buffer
        for i in range(lenRaw):
            if chRaw[i] == 0 and RFRaw[i] == 0:        # rise on ch0
                tRaw = towMsRaw[i]*1_000_000 + towSubMsRaw[i]//1000
                for j in range(lenRaw):
                    if chRaw[j] == 1:
                        tRaw2 = towMsRaw[j]*1_000_000 + towSubMsRaw[j]//1000
                        diff = tRaw2-tRaw
                        if (diff < 750) and (diff >= 0):
                            res=(tRaw2-tRaw1)-((tRaw2-tRaw1)*slope//1000000000)+tCal1
                            Ms=res//1000000
                            SubMs = res - Ms * 1000000
                            data_msg = (99, mac_id, RFRaw[j], timeValid, 1,
                                        wnoToi, Ms, SubMs, event_num, countRaw[i])
                            #print("datamsg2:", data_msg)

                            ustruct.pack_into(send_packet_format, send_buffer,
                                              send_buffer_index, *data_msg)
                            send_buffer_index += tx_packet_size

        data = send_data(send_buffer[:send_buffer_index])
        #if data:
            #print("!!!!!!!!!!!!!!!!!!!!!data sent", len(send_buffer[:send_buffer_index]))
        send_buffer_index = 0
                
        wdt.feed()
        event_num +=1

    except Exception as e:
        sys.print_exception(e)

        print("Error in main loop", e)
        packet = data_packing(send_packet_format, 100, mac_id, ip_last_byte, 1, 0, 0, 0, 0, 0, 0)
        send_data(packet)
        continue
        
    
    

    


