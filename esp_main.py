#Airshower Main
#Updated- 3/23/26

#Changelog
    #Testing new maxbuf + req changes
import micropython

gc.collect()

from ringBuffer import RingBuffer, push_all_cal, push_all_raw
from ringBuffer import rb_cal_count, rb_cal_wno, rb_cal_ms, rb_cal_sub
from ringBuffer import rb_raw_rf, rb_raw_ch, rb_raw_count, rb_raw_wno, rb_raw_ms, rb_raw_sub
from ringBuffer import CAPACITY_RAW, CAPACITY_CAL, raw_write_idx, cal_write_idx, cal_count, raw_count

gc.collect()

import wifi
import socket
import ota_listener
import _thread
#from PPS import init_time,rtc_to_gps_wno_ms_subms #I dont think we need all the extra functions
import ustruct
import time
import array
import sys
import select

gc.collect()
    

#=====================================================================================
#                                   OTA FUNCTION
#=====================================================================================
try:
    with open('config.txt') as f:
        detector_num = f.read().strip()
        print("This is Detector " + detector_num)
except OSError:
    detector_num = "0"  # default if not yet set
    print("No config.txt found, using default detector number:", detector_num)

version_num = "0.31"
# wdt = None
t = None
ota_listener.version_num = version_num
ota_listener.detector_num = detector_num
ota_listener.uart1 = uart1
ota_listener.wdt = wdt


#=====================================================================================

#---------GPS Variables-----------
UBX_HDR = b'\xb5\x62' 
RXM_TM =(2,116)   #b'\x02\x74'
TIM_TM2= (13,3)   #b'\x0d\x03'
NAV_CLOCK= (1,34)       #b'\x01\x22'
NAV_POSLLH= (1,2)       #b'\x01\x02' geodetic position (lat/lon/height + acc)
TIM_SVIN  = (13,4)      #b'\x0d\x04'
CFG_TMODE3 = (6,0x71)   #b'\x06\x71' legacy Time Mode 3 (NOT honored on F9 — kept for reference only)
CFG_VALGET = (6,0x8b)   #b'\x06\x8b' UBX-CFG-VALGET response (F9 config read-back)
REQUESTED_TIME_WINDOW = 1000000  #returned times (ns) will be within +/- requested_time_window of time of interested
# UBX poll messages
POLL_NAV_CLOCK = b'\xb5\x62\x01\x22\x00\x00\x23\x6a'
POLL_TIM_SVIN  = b'\xb5\x62\x0d\x04\x00\x00\x11\x40'  # poll survey-in status (TIM-SVIN is pollable on F9)

# UBX builders + CFG key constants now live in gps_ubx.py — upload it to the ESP too.
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

MON_VER = (10, 0x04)   # b'\x0a\x04' UBX-MON-VER  (version strings)
MON_SYS = (10, 0x39)   # b'\x0a\x39' UBX-MON-SYS  (CPU/mem/IO load)

numMeas=1
global tcoll0
tcoll0=0

# ---------- Wi-Fi Setup ----------
ssid = 'TP-Link_FB80'
password = 'Beau&River'

if wifi.con_to_wifi(ssid, password):
    wdt.feed()
    _thread.start_new_thread(ota_listener.start_listener, ())
    print("OTA listener started in background.")           

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

HOST = '192.168.0.247' #Home
PORT = 12345
PORT_CTRL = 12347   # dedicated slow-control channel (survey/fix), polled between data requests

# Control-socket state (set up after the helper functions below are defined)
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
ota_listener.s = s
ota_listener.poller = poller

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
            ota_listener.s = s

            packet = data_packing(send_packet_format, 100, mac_id, 2, 0, 0, 0, 0, 0, 0, 0)
            s.send(packet)

            return None

def receive(num_bytes, timeout):
    global s
    events = poller.poll(timeout)
    if not events:
        #time.sleep_ms(1)
        return None
    
    try:
        return memoryview(s.recv(num_bytes))
    
    except Exception as e:
        print("Receive error:", e)
        #error_msg = (100, mac_id, 3, 0, 0, 0, 0, 0, 0, 0)
        packet = data_packing(send_packet_format, 100, mac_id, 3, 0, 0, 0, 0, 0, 0, 0)
        send_data(packet)
        s = reconnect_socket(s, poller)
        ota_listener.s = s
        return None

# ---------- Data Packing -----------
send_packet_format = "!iiiiiiiiii"
request_packet_format = '!iiiii'

'''
def data_packing(packet_format: str, msg: tuple):
    try:
        packet = ustruct.pack(packet_format, 
            msg[0],#inst,            # char (1 byte)
            msg[1],#ID,
            msg[2],#RF,              # char (1 byte)
            msg[3],#cal,              # uint8 (1 byte)
            msg[4],#ch,          # uint8 (1 byte)
            msg[5],#w_num,      	 # uint32 (4 bytes) #Q for 8 byte uint64
            msg[6],#ms,         # uint32 (4 bytes)
            msg[7],#sub_ms,
            msg[8],#event_num                  # uint32 (4 bytes)
            msg[9] #count
        )
        
        return packet
    except Exception as e:
        print("Error in data packing", {e})
        #Cant send to server if error need data packing
'''
def data_packing(packet_format,v0,v1,v2,v3,v4,v5,v6,v7,v8,v9):
    try:
        return ustruct.pack(packet_format,v0,v1,v2,v3,v4,v5,v6,v7,v8,v9)
    except Exception as e:
        print("Error in data packing", e)
        return None

# (UBX builders moved to gps_ubx.py and imported above)

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
#     global RFRaw, chRaw, countRaw, countCal, towMsRaw, towMsCal,towSubMsRaw, towSubMsCal
    #print('maxRxBuf')
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
            #print('buffer cleared of ',nClear, "50ms segments")
        elif nbuf > n2:
            readData(1)  #read and parse 1kb segment of raw data
            readData(1)  #read and parse 60byte segment of cal data
            #print('buffer cleared of a 50ms segment')
    except Exception as e:
        print('maxRxbuf exception',e)
        #error_msg = (100, mac_id, 5, 0, 0, 0, 0, 0, 0, 0)
        packet = data_packing(send_packet_format,100, mac_id, 5, 0, 0, 0, 0, 0, 0, 0)
        send_data(packet)
'''
def maxRxBuf(n):
    try:
        if uart1.any() > n:
            nClear=0
            while (uart1.any() > n-1000):
                #nskim = uart1.any()-n + 1000
                nClear += 1
                junk=uart1.read(1024)
            #print('buffer cleared of ',nClear, 'kB\n')
    except Exception as e:
        print('maxRxbuf exception',e)
        #error_msg = (100, mac_id, 5, 0, 0, 0, 0, 0, 0, 0)
        packet = data_packing(send_packet_format,100, mac_id, 5, 0, 0, 0, 0, 0, 0, 0)
        send_data(packet)
'''

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

def request(wnoToi,MsToi,subMsToi):
    global slope, toi_len, tRaw1, tCal1, t, unreas_count
    try:
        #print('100')
        res=(0,0,0,0)
        
        if (wnoToi == -1):
          #New request
          #print('request(-1,0,0) called')
          while (res[0] == 0):
              #print('res[0] ==0')
              res=readData(1)
              #print('readData(1)')
#          print('res[1]:',res[1])
          MsToi = res[1]+MsToi
          subMsToi = res[2]
          #print('end if')
        
        while (res[0] == 0):
            res=readData(1)

        resToi=MsToi*1000000+subMsToi

        #wno, Ms, subMs = rtc_to_gps_wno_ms_subms()
        #oldestMsCal=rb_cal_ms.get_oldest()
        #if ((MsToi < oldestMsCal)) or ((res[0] != wnoToi) and (wnoToi != -1)):
            #print('##################################Unreasonable request', "Cal:", towMsCal[0], "PPS:", Ms, "Toi:", MsToi)
            #print("Unreasonable Request")
            #unreas_count += 1
            #ur_msg = (93, mac_id, towMsCal[0], MsToi, gc.mem_free(), buf_size, trans_time, req_diff, event_num, 0) ###
            #print(ur_msg)
            #send_packet = data_packing(send_packet_format, 93, mac_id, 0, MsToi, gc.mem_free(), buf_size, transit_time, req_diff, event_num, 0) ###
            #send_data(send_packet) 

        diff=0
        while (diff < REQUESTED_TIME_WINDOW):  # | (bytehdr2 == RXM_TM): #Exceed the time of interest by at least 1 ms and capture an extra RXM data packet    
            res=readData(1)

            if res[1] > 0:
                diff=(res[1]-MsToi)*1000000+(res[2]-subMsToi)
            
        timeValid = res[3]
        
                #Find cal index near toi
        calIdx=-1
        for i in range(cal_count[0]):
            rbCal = rb_cal_ms.get(i)
            if rbCal < MsToi:
                calIdx = i
                if i > 0:
                    calIdx=i-1   #Cal index near toi 
                break

        #find index where rawcount = calcount
        #print(calIdx)
            
        if (calIdx == -1):
            print("No cal in buffer")
            print("Unreasonable Request")
            unreas_count += 1
            send_packet = data_packing(send_packet_format, 93, mac_id, 0, MsToi, gc.mem_free(), buf_size, transit_time, req_diff, event_num, 0) ###
            send_data(send_packet) 
            return(toi_RF,toi_valid,toi_ch,toi_wno,toi_Ms,toi_SubMs)

        rawIdx=-1
        CCoI = rb_cal_count.get(calIdx)
        for i in range(raw_count[0]):            
            RC = rb_raw_count.get(i)
            if RC == 0:
                continue
            if RC < CCoI:
                rawIdx = i
                break
            if RC == CCoI:  # matching raw count found
                tCal1 = rb_cal_ms.get(calIdx)*1000000 + rb_cal_sub.get(calIdx)
                tRaw1 = rb_raw_ms.get(i)*1000000 + rb_raw_sub.get(i)//1000
                rawIdx = i
                #print('cal',rb_cal_ms.get(calIdx),rb_cal_sub.get(calIdx),rb_raw_ms.get(i),rb_raw_sub.get(i))
                break

        toi_len=0
        
        if (rawIdx == -1):
            print("Unreasonable request")
            print("Unreasonable Request")
            unreas_count += 1
            send_packet = data_packing(send_packet_format, 93, mac_id, 0, MsToi, gc.mem_free(), buf_size, transit_time, req_diff, event_num, 0) ###
            send_data(send_packet)         
            return(toi_RF,toi_valid,toi_ch,toi_wno,toi_Ms,toi_SubMs)
        
        for i in range(cal_count[0]):
            rbCal = rb_cal_ms.get(i)
            if rbCal < MsToi:
                if i > 0:
                    calIdx=i-1   #Cal index near toi 
                break

        #find index where rawcount = calcount
        #print(calIdx)
        CCoI = rb_cal_count.get(calIdx)
        rawIdx=0
        for i in range(raw_count[0]):
            RC = rb_raw_count.get(i)
            if RC == 0:
                continue
            if RC < CCoI:
                break
            if RC == CCoI:  # matching raw count found
                tCal1 = rb_cal_ms.get(calIdx)*1000000 + rb_cal_sub.get(calIdx)
                tRaw1 = rb_raw_ms.get(i)*1000000 + rb_raw_sub.get(i)//1000
                rawIdx = i
                break

        toi_len=0
        rawIdx=rawIdx-40  #starts about 50ms before toi
        rawIdx = max(rawIdx,0)

        #print(slope)
        #calibrate and select toi
        for i in range(rawIdx, raw_count[0]):
            tRaw=rb_raw_ms.get(i)*1000000+ rb_raw_sub.get(i)//1000
            #print(tRaw1)
            dtRaw=tRaw-tRaw1
            res=dtRaw-dtRaw*slope//1000000000 + tCal1
            diff = res - resToi
#            print("tRaw, dtRaw, res, resToi, diff", tRaw, dtRaw, res, resToi, diff)
            if diff < (-REQUESTED_TIME_WINDOW):
                # Reached t < toi
                break
            diff = abs(diff)
            if diff < REQUESTED_TIME_WINDOW:
                toi_RF[toi_len]=rb_raw_rf.get(i)
                toi_valid[toi_len]=timeValid
                toi_ch[toi_len]=rb_raw_ch.get(i)
                toi_wno[toi_len]=rb_raw_wno.get(i)
                toi_Ms[toi_len]=res//1000000
                toi_SubMs[toi_len]=res-toi_Ms[toi_len]*1000000
                toi_len +=1

        return(toi_RF,toi_valid,toi_ch,toi_wno,toi_Ms,toi_SubMs)
    
    except Exception as e:
        sys.print_exception(e)

        print("Request error")
        #error_msg = (100, mac_id, 8, 0, 0, 0, 0, 0, 0, 0)
        packet = data_packing(send_packet_format, 100, mac_id, 8, 0, 0, 0, 0, 0, 0, 0)
        send_data(packet)
        #return None

plb = bytearray(2048)
ck  = bytearray(2)
oldcount=0

def readData(det):
    global raw_write_idx, raw_count, cal_write_idx, cal_count
    global slope , deltaT, NEvents0, NEvents1,oldcount, oldtowMsR,oldtowMs
#    global RFRaw, chRaw, countRaw, countCal, towMsRaw, towMsCal,towSubMsRaw, towSubMsCal
    #print('0 readData free mem:',gc.mem_free())
#    print(raw_write_idx[0])
    try:
        # Find UBX sync
        n=findUBX_HDR()
        if n>2:
            pass
            #print("findUBX",n)
        #print('1 readData free mem:',gc.mem_free())

        cls, msg, leni = findHDR2()
        if leni > 2048:
            #print("leni >2048" )
            return (0, 0, 0, 0)

        # Wait cooperatively for payload + checksum
        needed = leni + 2
        i=0
        while uart1.any() < needed:
            i=i+1
            if (i-i//1000*1000) == 0:
                pass
                #print('readData',end='.')
            time.sleep_ms(1)

        # Read payload + checksum without allocating
        uart1.readinto(plb, leni)
        uart1.readinto(ck, 2)
        #print('2 readData free mem:',gc.mem_free())

        # ---------- RXM-TM ----------
        if (cls, msg) == RXM_TM:
            #print('RXM_TM')
            version = plb[0]
            numMeas = plb[1]
            
            #using 50ms windows
            deltaT += 50            
            base = 8
            for ii in range(numMeas):
                edgeInfo = (
                    plb[base+0] |
                    (plb[base+1] << 8) |
                    (plb[base+2] << 16) |
                    (plb[base+3] << 24)
                )

                RF = (edgeInfo >> 4) & 1
                ch = edgeInfo & 1

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

                #print('3 readData free mem:',gc.mem_free())
                #oldcount=count

                if RF==0:
                    if ch==0:
                        NEvents0 +=1
                    elif ch==1:
                        NEvents1 +=1
                dtMs=towMs-oldtowMs
                if (dtMs)>75:
                    #countdtMs += 1
                    #print ("count, towMs, oldtowMs, dt", count, towMs, oldtowMs,dtMs)
                    pass
                oldtowMs=towMs

                push_all_raw(rf=RF, ch=ch, wno=wno, ms=towMs, sub=towSubMs, count=count)

                base += 24
        # ---------- TIM-TM2 ----------
        elif (cls, msg) == TIM_TM2:
            #print('5 readData free mem:',gc.mem_free())

            #print('TIM_TM2')
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

            if det == 1:
                #print('TIM-TM',count)
                dtMs=towMsR-oldtowMsR
                if (dtMs)>75:
                    pass
                    #countdtMs += 1
                    #print ("count, toMsR, oldtowMsR, dt", count, towMsR, oldtowMsR,dtMs)
                oldtowMsR=towMsR
                push_all_cal(wno= wnoR, ms= towMsR, sub= towSubMsR, count = count)

                return (wnoR, towMsR, towSubMsR, timeValid)

            return [
                (0, 1, ch, wnoR, towMsR, towSubMsR),
                (1, 1, ch, wnoF, towMsF, towSubMsF)
            ]

        # ---------- NAV-CLOCK ----------
        elif (cls, msg) == NAV_CLOCK:
            #print('7 readData free mem:',gc.mem_free())

            #print('NAV_CLOCK')
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
            #print('** slope =', slope)
            #print('1 readData free mem:',gc.mem_free())

        # ---------- TIM-SVIN ----------
        elif (cls, msg) == TIM_SVIN:
            # Payload layout (28 bytes):
            #   0  iTOW   U4  ms
            #   4  dur    U4  seconds elapsed
            #   8  meanX  I4  cm  (ECEF)
            #  12  meanY  I4  cm
            #  16  meanZ  I4  cm
            #  20  meanAcc U4 0.1 mm
            #  24  obs    U2  observations
            #  26  valid  U1  position valid flag
            #  27  active U1  survey-in active flag
            dur     = plb[4]  | (plb[5]  << 8) | (plb[6]  << 16) | (plb[7]  << 24)
            meanX   = ustruct.unpack_from('<i', plb, 8)[0]   # signed
            meanY   = ustruct.unpack_from('<i', plb, 12)[0]
            meanZ   = ustruct.unpack_from('<i', plb, 16)[0]
            meanAcc = plb[20] | (plb[21] << 8) | (plb[22] << 16) | (plb[23] << 24)
            obs     = plb[24] | (plb[25] << 8)
            valid   = plb[26]
            active  = plb[27]
            return ('svin', dur, meanX, meanY, meanZ, meanAcc, obs, valid, active)

        # ---------- NAV-POSLLH (geodetic position + accuracy) ----------
        elif (cls, msg) == NAV_POSLLH:
            # Payload layout (28 bytes):
            #   0  iTOW   U4  ms
            #   4  lon    I4  1e-7 deg
            #   8  lat    I4  1e-7 deg
            #  12  height I4  mm  (above ellipsoid)
            #  16  hMSL   I4  mm  (above mean sea level)
            #  20  hAcc   U4  mm  (horizontal accuracy estimate)
            #  24  vAcc   U4  mm  (vertical accuracy estimate)
            lon    = ustruct.unpack_from('<i', plb, 4)[0]
            lat    = ustruct.unpack_from('<i', plb, 8)[0]
            height = ustruct.unpack_from('<i', plb, 12)[0]
            hMSL   = ustruct.unpack_from('<i', plb, 16)[0]
            hAcc   = plb[20] | (plb[21] << 8) | (plb[22] << 16) | (plb[23] << 24)
            vAcc   = plb[24] | (plb[25] << 8) | (plb[26] << 16) | (plb[27] << 24)
            return ('posllh', lon, lat, height, hMSL, hAcc, vAcc)

        # ---------- CFG-VALGET (config read-back, F9 interface) ----------
        elif (cls, msg) == CFG_VALGET:
            # Payload: version(U1) layer(U1) position(U2), then key/value pairs.
            # Each pair = 4-byte key (LE) + value whose width is encoded in
            # the key's size code (bits 28-30): 0x2->1B, 0x3->2B, 0x4->4B, 0x5->8B.
            # Return a generic {key: value} dict — caller picks out TMODE or RATE.
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
                else:                     break   # unknown size, stop parsing
                if idx + vlen > leni:
                    break
                cfg[key] = ustruct.unpack_from(fmt, plb, idx)[0]
                idx += vlen
            return ('valget', cfg)

        # ---------- MON-VER (version strings) ----------
        elif (cls, msg) == MON_VER:
            # swVersion = char[30] at offset 0, hwVersion = char[10] at offset 30
            return ('ver', bytes(plb[0:30]))

        # ---------- MON-SYS (CPU / mem / IO load + counts) ----------
        elif (cls, msg) == MON_SYS:
            #  2 cpuLoad U1 %   3 cpuLoadMax U1   4 memUsage U1   5 memUsageMax U1
            #  6 ioUsage U1     7 ioUsageMax U1   8 runTime U4 s
            # 12 noticeCount U2 14 warnCount U2  16 errorCount U2  18 tempValue I1 °C
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
        #print("return (0,0,0,0)")
        return (0, 0, 0, 0)

    except MemoryError as e:
        sys.print_exception(e)
        print("Memory Error in ReadData")
        gc.collect()

    except Exception as e:
        sys.print_exception(e)
        print("Error in readData")
        #error_msg = (100, mac_id, 9, 0, 0, 0, 0, 0, 0, 0)
        packet = data_packing(send_packet_format, 100, mac_id, 9, 0, 0, 0, 0, 0, 0, 0)
        send_data(packet)

# ---------- Control Channel ----------
# Slow-control commands (survey/fix) arrive on a dedicated socket so they are
# not stuck behind the inst=99 data backlog on the main socket. Replies go out
# on this same control socket via send_ctrl (NOT send_data).

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
    """Send a full control packet. The control socket is non-blocking, so a
    single .send() may transmit only part of the buffer (or raise EAGAIN) when
    the TCP send buffer is filling — the old code dropped the remainder, which
    truncated replies/telemetry (e.g. inst=90 lost right after inst=91). Loop
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
    """The generic CFG-VALGET parser reads an R8 duty key as a signed 64-bit
    int (the raw bytes). Reinterpret those bytes as a double and return it
    scaled to the integer transport: round(percent * TP1_DUTY_SCALE)."""
    try:
        duty = ustruct.unpack('<d', ustruct.pack('<q', raw_q))[0]
    except Exception:
        return 0
    return int(round(duty * TP1_DUTY_SCALE))

def handle_control(inst, w_num, ms, sub_ms, event_num):
    """Service one slow-control command. inst 12/13 are fast (write a UBX
    config and confirm); inst 11/14 poll the GPS (clearRxBuf + read frames)
    and so briefly interrupt data collection — acceptable for manual probes."""
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
            # inst=206, id, RF=timeref, Cal=0, ch=0, w_num=meas_ms, ms=nav, ...
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
            # inst=209, RF=flags, w_num=val_un, ms=val_lock, sub_ms=pw_un, event_num=pw_lock, count=cable
            send_ctrl(data_packing(send_packet_format, 209, mac_id, flags, 0, 0,
                                   val_un, val_lock, pw_un, pw_lock, cable))
            print("TP1:", "freq" if flags & TP1_F_ISFREQ else "period",
                  val_un, "/", val_lock,
                  (" len" if flags & TP1_F_ISLENGTH else " duty*scale"),
                  pw_un, "/", pw_lock, " flags", flags)
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
            # inst=211, RF=vAcc, w_num=lon, ms=lat, sub_ms=height, event_num=hMSL, count=hAcc
            send_ctrl(data_packing(send_packet_format, 211, mac_id, vAcc, 0, 0,
                                   lon, lat, height, hMSL, hAcc))
            print("POSLLH: lat", lat, " lon", lon, " h", height, "mm  hAcc", hAcc, " vAcc", vAcc)
        else:
            print("NAV-POSLLH poll timed out")
            send_ctrl(data_packing(send_packet_format, 100, mac_id, 22, 0, 0, 0, 0, 0, 0, 0))

def poll_mon_sys():
    """Poll MON-SYS and return ('monsys', cpuLoad, cpuLoadMax, memUsage,
    ioUsage, runTime, temp) or None. Does NOT clear the UART — frames read
    while waiting are still parsed into the ring buffers (no data dropped)."""
    uart1.write(POLL_MON_SYS)
    for _ in range(40):
        res = readData(1)
        if isinstance(res, tuple) and res and res[0] == 'monsys':
            return res
    return None

def poll_valget(keys, marker_key):
    """Send a CFG-VALGET for `keys` and return the {key:value} dict once a
    response containing `marker_key` arrives (or None on timeout). Clears the
    UART first, so it briefly interrupts data collection."""
    clearRxBuf()
    uart1.write(build_valget(keys))
    for _ in range(40):
        res = readData(1)
        if isinstance(res, tuple) and len(res) == 2 and res[0] == 'valget' and marker_key in res[1]:
            return res[1]
    return None

def service_control():
    """Poll the control socket and run any pending command NOW. Cheap when idle
    (poll(0) returns immediately). Called frequently — including between data
    requests — so commands are not blocked by the inst=99 backlog."""
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

#restart_msg = (100, mac_id, 12, time.ticks_ms(), 0, 0, 0, 0, 0, 0) # This tells me when the board restarted how long it took to reach the main loop since booting
packet = data_packing(send_packet_format, 100, mac_id, 12, time.ticks_ms(), 0, 0, 0, 0, 0, 0)
send_data(packet)

time.sleep(0.1)

#info_msg = (1, mac_id, ip_last_byte, int(detector_num), int(version_num), 0, 0, 0, 0, 0) # This tells me when the board restarted how long it took to reach the main loop since booting
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

'''
while ((slope == 0) or (tRaw1 == None) or (Valid == 0)):
    try:
        #print('\ninit while loop', slope, tRaw1, Valid, res)
        uart1.write(POLL_NAV_CLOCK) #Poll Nav Clock
        for i in range(4):
            res=readData(1)
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
        #sys.print_exception(e)
        print("Error in gps initialization")
        #error_msg = (100, mac_id, 10, 0, 0, 0, 0, 0, 0, 0)
        packet = data_packing(send_packet_format, 100, mac_id, 10, 0, 0, 0, 0, 0, 0, 0)
        send_data(packet)
        continue
'''
NEventsSent0=0
NEventsSent1=0
NEventsSentBoth = 0
T0=time.ticks_us()

array_count = 0
Rate0=0
Rate1=0
R0index=[]
R1index=[]

CC=0
reqCount=0
global toi_len

clearRxBuf()
clear_wifi_rx_buffer()

send_buffer = bytearray(640)
send_mv = memoryview(send_buffer)
send_buffer_index = 0
stats_send_buffer = bytearray(200)
send_stats_mv = memoryview(stats_send_buffer)
stats_send_buffer_index = 0

buf_size = 0
unreas_count = 0
ch0_null_count= 0
ch1_null_count= 0

#init_time(uart1)

request_bunching_avg = 0
request_bunching_max = 0
recv_chunk_count = 0

transit_time_avg= 0
transit_time_max= 0

loop_time_avg = 0
loop_time_max = 0

proc_time_avg = 0
proc_time_max = 0

#----- Telemetry for wifi integrity -----
rx_count = 0 #Number of requests for data received
data_count = 0
null_count = 0

req_diff = 0
prev_req = 0

rx_packet_size =  ustruct.calcsize(request_packet_format)
tx_packet_size = ustruct.calcsize(send_packet_format)

gc.collect() #Free memory

while True:
    #gc.collect()
    if ota_listener.ota_in_progress:
           print("OTA in progress: closing gps data socket")
           try:
               s.close() # changed from t.close() during merge with updated OTA code
               while True:
                   wdt.feed()
                   time.sleep_ms(100)
           except Exception as e:
               print("Socket close error:", e)
           time.sleep(1_000_000)  # effectively idle until reset

    if ota_listener.prepare_mode:
        wdt.feed()
        time.sleep_ms(100)
        continue

    try:
        # Ensure Wi-Fi stays connected
        if not wifi.wlan.isconnected():
            print("Wi-Fi disconnected. Reconnecting...")
            wifi.con_to_wifi(ssid, password)

        service_control()   # check for slow-control commands even when idle

        # Autonomous GPS CPU/health telemetry once a minute (sent on the control channel)
        if time.ticks_diff(time.ticks_ms(), cpu_T0) > 60000:
            cpu_T0 = time.ticks_ms()
            cpu = poll_mon_sys()
            if cpu:
                (_, cpuLoad, cpuLoadMax, memUsage, memUsageMax,
                 ioUsage, ioUsageMax, runTime, notice, warn, error, temp) = cpu
                # inst=91 (counts) first so the server has them when inst=90 arrives
                send_ctrl(data_packing(send_packet_format, 91, mac_id,
                                       notice, warn, error, 0, 0, 0, 0, 0))
                # inst=90 main: RF=cpuLoad Cal=memUsage ch=ioUsage w_num=temp
                #               ms=cpuLoadMax sub_ms=memUsageMax event_num=ioUsageMax count=runTime
                send_ctrl(data_packing(send_packet_format, 90, mac_id,
                                       cpuLoad, memUsage, ioUsage, temp,
                                       cpuLoadMax, memUsageMax, ioUsageMax, runTime))

        buf_size = uart1.any()
        if uart1.any()>22000:
            print("Buffer Overload")
            #error_msg = (100, mac_id, 16, 0, 0, 0, 0, 0, 0, 0)
            packet = data_packing(send_packet_format, 100, mac_id, 16, 0, 0, 0, 0, 0, 0, 0)
            send_data(packet)
            
        maxRxBuf(15000, 20000)        
        #print('buffer size ',uart1.any(), 'bytes\n')
        recv_chunk = receive(1024, 50)

        time.sleep_ms(0)
       
        if recv_chunk: #I dont think its a good idea to feed on rx
            T_loop_s = time.ticks_us()
            wdt.feed()
            #print('len of rx buf', len(recv_chunk))
            recv_bunch = (len(recv_chunk))//rx_packet_size
            if recv_bunch > request_bunching_max:
                request_bunching_max = recv_bunch
            request_bunching_avg+=recv_bunch
            recv_chunk_count += 1
            
            recv_index = 0

            while recv_index + rx_packet_size <= len(recv_chunk):
                service_control()   # service control between each (slow ~1.2s) data request

                rx_count +=1 #Counts number of receives

                recv_packet = recv_chunk[recv_index:recv_index+rx_packet_size]
                recv_index += rx_packet_size
                
                ch0_flag = 0
                ch1_flag = 0
                ch0_data_flag = 0
                ch1_data_flag = 0
                
                #timeStamp=rtc_to_gps_wno_ms_subms()
                try:
                    inst, w_num, ms, sub_ms, event_num = ustruct.unpack(request_packet_format, recv_packet) 
                    print('Request', inst, w_num, ms, sub_ms, event_num)
                
                    req_diff = ms -prev_req
                    prev_req = ms
                    
                    transit_time = 0#timeStamp[1] - ms
                    if transit_time > transit_time_max:
                        transit_time_max = transit_time
                    transit_time_avg+=transit_time
                
                except Exception as e:
                    print("Error unpacking request:", e)
                    packet = data_packing(send_packet_format, 100, mac_id, 11, 0, 0, 0, 0, 0, 0, 0)
                    send_data(packet)
                    continue

                if inst == 99:

                    #print("Processing GPS request...")
                    #print('buffer size ',uart1.any(), 'bytes\n')
                    
                    T_req_s = time.ticks_us()
                    timesofinterest= request(w_num, ms, sub_ms)
                    T_req_e = time.ticks_us()
                    
                    proc_time = (time.ticks_diff(T_req_e, T_req_s))//1000
                    
                    if proc_time > proc_time_max:
                        proc_time_max = proc_time
                    proc_time_avg += proc_time                 
                    
                    if toi_len == 0 or timesofinterest is None:
                        null_count += 1 #Counts nulls per request
                        pass
                    
                    else:
                        RF,cal,ch,w_num,ms,sub_ms = timesofinterest
                        
                        for i in range(toi_len):

                            data_msg = (99, mac_id, RF[i], cal[i], ch[i], w_num[i], ms[i], sub_ms[i], event_num, buf_size)

                            try:
                                ustruct.pack_into(send_packet_format, send_mv, send_buffer_index, *data_msg)
                                send_buffer_index += tx_packet_size
                            except ValueError: #Buffer Over fill
                                print("Send Buffer Overfill")
                                packet = data_packing(send_packet_format, 100, mac_id, 13, 0, 0, 0, 0, 0, 0, 0)
                                send_data(packet)
                                pass #Just try to send what you have

                            if RF[i]==0 and ch[i]==0:
                                NEventsSent0 +=1
                                ch0_data_flag = 1

                            if RF[i]==0 and ch[i]==1:
                                NEventsSent1 +=1
                                ch1_data_flag = 1
                                    
                            if ch[i]==0 and ch0_flag == 0:
                                #print('ch0_event')
                                ch0_flag = 1
                                    
                            if ch[i]==1 and ch1_flag == 0:
                                #print('ch1_event')
                                ch1_flag = 1
                        
                        if ch0_flag == 0:
                            ch0_null_count += 1

                        if ch1_flag == 0:
                            ch1_null_count += 1

                        if ch0_data_flag == 1 and ch1_data_flag == 1:
                            NEventsSentBoth += 1

                        data = send_data(send_mv[:send_buffer_index])
                        if data:
                            data_count += 1 #Counts how many requests result in data sent
                            #print("!!!!!!!!!!!!!!!!!!!!!data sent", data)
                        send_buffer_index = 0

                # Slow-control (inst 11-17) is handled on the dedicated control
                # channel via service_control()/handle_control — not here.

                T1=time.ticks_us()
            
                if time.ticks_diff(T1, T0) > 5000000:
                    stats_send_buffer_index = 0
                    print('.')
                    uart1.write(POLL_NAV_CLOCK)
                    wno= Ms= subMs = 0#rtc_to_gps_wno_ms_subms()

                    try:
                        stats_msg1 = (98, mac_id, NEvents0, NEvents1, deltaT, wno, Ms, subMs, ch0_null_count, ch1_null_count)
                        ustruct.pack_into(send_packet_format, send_stats_mv, stats_send_buffer_index, *stats_msg1)
                        stats_send_buffer_index += tx_packet_size
                       
                        if rx_count>0 and recv_chunk_count>0:
                            stats_msg2 = (97, mac_id, transit_time_avg//rx_count, transit_time_max, request_bunching_avg//recv_chunk_count, request_bunching_max, proc_time_avg//rx_count, proc_time_max, loop_time_avg//recv_chunk_count, loop_time_max)
                            ustruct.pack_into(send_packet_format, send_stats_mv, stats_send_buffer_index, *stats_msg2)
                            stats_send_buffer_index += tx_packet_size

                            '''
                            stats_msg3 = (96, mac_id, proc_time_avg//rx_count, proc_time_max, loop_time_avg//recv_chunk_count, loop_time_max, 0, 0, 0, 0)
                            ustruct.pack_into(send_packet_format, send_stats_mv, stats_send_buffer_index, *stats_msg3)
                            stats_send_buffer_index += tx_packet_size
                            '''

                        stats_msg4 = (96, mac_id, rx_count, data_count, null_count, unreas_count, 0, 0, 0, 0, 0, 0)
                        ustruct.pack_into(send_packet_format, send_stats_mv, stats_send_buffer_index, *stats_msg4)
                        stats_send_buffer_index += tx_packet_size
                       
                        stats_msg5 = (95, mac_id, NEventsSent0, NEventsSent1, deltaT, wno, Ms, subMs, NEventsSentBoth, 0)
                        ustruct.pack_into(send_packet_format, send_stats_mv, stats_send_buffer_index, *stats_msg5)
                        stats_send_buffer_index += tx_packet_size
                    except Exception:
                        print("Error in stats messages")
                        pass

                    #Clear variables
                    NEvents0=NEvents1=NEventsSent0=NEventsSent1=deltaT=ch0_null_count=ch1_null_count=NEventsSentBoth= rx_count = unreas_count = null_count= data_count = 0
                    request_bunching_avg = 0
                    request_bunching_max = 0
                    recv_chunk_count = 0

                    transit_time_avg= 0
                    transit_time_max= 0

                    loop_time_avg = 0
                    loop_time_max = 0

                    proc_time_avg = 0
                    proc_time_max = 0
                    if len(send_stats_mv)>0: #If not none
                        send_data(send_stats_mv[:stats_send_buffer_index])
                        T0=T1
                    
                T_loop_e = time.ticks_us()
                loop_time = (time.ticks_diff(T_loop_e, T_loop_s))//1000
                
                if loop_time > loop_time_max:
                    loop_time_max = loop_time
                loop_time_avg += loop_time

        else:
            continue # Continue main loop if data not recieved

    except Exception as e:
        sys.print_exception(e)
        print("Main loop exception:", e)
        if ota_listener.ota_in_progress:
            continue  # Don't try to send on closed socket during OTA
        #error_msg = (100, mac_id, 1, 0, 0, 0, 0, 0, 0, 0)
        packet = data_packing(send_packet_format, 100, mac_id, 1, 0, 0, 0, 0, 0, 0, 0)
        send_data(packet)
        gc.collect()
        continue

# END_OF_FILE        
