import socket
import select
import struct
import os
import re
from datetime import datetime, timedelta
import gzip
import shutil
import time
import traceback
import errno
import datetime

#Changelog:
    #11/14/25- Added socket cleanup function to remove deadsockets
    #Better handled nonblocking send and receive functions with appropriate error catching. SHould hopefully reduce errors.
    #Borehole and veto are broadcasting to eachother filling up TCP socket. Added a recv in each esp code to clear the buffer. Too complicated (as I see) to try and deal with it server side.
    #12/2/25- Event numbers now generated on server


from datetime import datetime, timezone

GPS_UTC_OFFSET = 18  # leap seconds (valid as of 2026)

# Precompute GPS epoch in Unix seconds
GPS_EPOCH_UNIX = datetime(1980, 1, 6, tzinfo=timezone.utc).timestamp()

def gps_to_utc_seconds(gps_week: int, ms_of_week: int, ns_remainder: int) -> float:
    """
    Convert GPS time (week, milliseconds of week, nanoseconds remainder)
    to UTC seconds since Unix epoch.
    """

    # Total seconds into GPS time
    gps_seconds = (
        gps_week * 7 * 24 * 3600
        + ms_of_week / 1000.0
        + ns_remainder / 1e9
    )

    # Convert to Unix time
    unix_time_gps = GPS_EPOCH_UNIX + gps_seconds

    # Subtract leap seconds to get UTC
    unix_time_utc = unix_time_gps - GPS_UTC_OFFSET

    return unix_time_utc

HOST = '0.0.0.0'
PORT = 12345
UDP_PORT = 12346
CTRL_PORT = 12347   # dedicated slow-control channel (survey/fix), separate from the data stream

GUI_ID = 1  # Reserved ID for the slow-control GUI

# Data Format:
PACKET_FORMAT = "!iiiiiiiiii"
PACKET_SIZE = struct.calcsize(PACKET_FORMAT)
BCAST_FORMAT = "!iiiii"  # server → ESP trigger/command format
            
class CycleLimitReached(Exception):
    """Raised by the writer when the configured max cycle count is hit, so the
    main loop rolls over into a fresh run."""
    pass

class RotatingFileWriter:
    def __init__(self, base_folder_name = "folder", base_file_name="file", ext=".txt", time_length = 10, gzip_files=False, header = "", max_cycles = 0):
        self.base_data_storage_folder = 'C:\\Users\\alexc\\Desktop\\Claude_GUI_prac\\GPS Data'
        self.base_folder_name = base_folder_name
        self.base_file_name = base_file_name
        self.ext = ext
        self.time_length = time_length
        self.gzip_files = gzip_files
        self.max_cycles = max_cycles   # 0 = unlimited
        self.date = datetime.now().strftime("%Y%m%d")
        self.folder_run_number = self._get_next_run_number()
        #self.run_number = self._get_next_run_number()
        self.cycle_number = 1
        
        self.header = header
        self.folder_dir = self.open_new_folder()
        self.connection_log = os.path.join(self.folder_dir, "connection_log.txt")
        self.error_log = os.path.join(self.folder_dir, "error_log.txt")
        self.unique_esp_log = os.path.join(self.folder_dir, "unique_esp_log.txt")
        self.cpu_log = os.path.join(self.folder_dir, "gps_cpu_log.txt")
        with open(self.cpu_log, 'a') as f:
            f.write("timestamp; ID; cpuLoad; cpuLoadMax; memUsage; memUsageMax; "
                    "ioUsage; ioUsageMax; runTime_s; temp_C; notice; warn; error\n")
        self.open_new_file()

    def _get_next_run_number(self):
        """Find the next available run number across all files."""
        run_pattern = re.compile(
            rf"{self.base_folder_name}_(\d+)_(\d+)"
        )
        max_run = 0
        for folder_name in os.listdir(self.base_data_storage_folder):
            match = run_pattern.match(folder_name)
            folder_path = os.path.join(self.base_data_storage_folder, folder_name)

            if os.path.isdir(folder_path):
                if match:
                    run_num = int(match.group(1))
                    max_run = max(max_run, run_num)
        return max_run + 1
    
    def open_new_folder(self):
        folder_name = (
            f"{self.base_folder_name}_{self.folder_run_number:04d}_{self.date}")

        folder_path = os.path.join(self.base_data_storage_folder, folder_name)
        os.mkdir(folder_path)

        return folder_path

    def open_new_file(self):

        # Cycle limit: cycle_number is the NEXT file to open. Once it exceeds the
        # configured max, signal the main loop to start a fresh run instead.
        if self.max_cycles and self.cycle_number > self.max_cycles:
            raise CycleLimitReached()

        if hasattr(self, "file") and self.file:
            self._close_and_gzip()
        self.filename = (
            f"{self.base_file_name}_{self.date}_run{self.folder_run_number:04d}_cycle{self.cycle_number:04d}{self.ext}"
        )
        
        self.file_path = os.path.join(self.folder_dir, self.filename)
        self.file = open(self.file_path, "w", buffering = 1)#buffering=1024*1024)
        self.start_time = datetime.now()

        if self.header:
            self.file.write(self.header + "\n") 

        print(f"[INFO] Opened {self.filename}")
        with open(self.connection_log, 'a') as cl:
            cl.write(f"\nStart Cycle {self.cycle_number}:{datetime.now()}\n")
        with open(self.error_log, 'a') as el:
            el.write(f"\nStart Cycle {self.cycle_number}:{datetime.now()}\n")

        self.cycle_number += 1
        time.sleep(1)



    def _close_and_gzip(self):
        self.file.close()

        if self.gzip_files:
            gz_path = self.file_path + ".gz"
            with open(self.file_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            os.remove(self.file_path)
            print(f"[INFO] Compressed {self.file_path} -> {gz_path}")

    def write(self, data: str):
        
        if datetime.now() - self.start_time >= timedelta(hours = self.time_length):
            self.open_new_file()

        self.file.write(data)
        #print(self.current_size)

    def close(self):
        if self.file:
            self._close_and_gzip()

"""def cleanup_client_socket(sock, addr, sockets, clients):
    if sock in sockets:
        sockets.remove(sock)
    if sock in clients:
        del clients[sock]
    sock.close()

    with open(writer.connection_log, 'a') as log:
        log.write(f"{time.time()}: Disconnected from {addr}\n")
    print(f"[SERVER] Dead client socket removed: {addr}")
"""
def cleanup_client_socket(sock, addr, sockets, clients, clients_by_id):

    if sock in sockets:
        sockets.remove(sock)

    client_info = clients.get(sock)
    if client_info:
        client_id = client_info.get("id")

        if client_id and clients_by_id.get(client_id) == sock:
            del clients_by_id[client_id]

        del clients[sock]

    sock.close()

    with open(writer.connection_log, 'a') as log:
        log.write(f"{datetime.now()}: Disconnected from {addr}\n")

    print(f"[SERVER] Dead client socket removed: {addr}")

def cleanup_ctrl_socket(sock, addr, sockets, ctrl_clients, ctrl_by_id, pending_slow_ctrl):
    if sock in sockets:
        sockets.remove(sock)

    info = ctrl_clients.get(sock)
    if info:
        cid = info.get("id")
        if cid and ctrl_by_id.get(cid) == sock:
            del ctrl_by_id[cid]
        del ctrl_clients[sock]

    # Drop any pending GUI->ESP routes that pointed at this socket
    for k in [k for k, v in pending_slow_ctrl.items() if v == sock]:
        del pending_slow_ctrl[k]

    try:
        sock.close()
    except OSError:
        pass

    print(f"[SERVER] Control socket removed: {addr}")

def ns_timestamp(ms, sub_ms):

    return (ms*1000000 + sub_ms)

def run_server():

    # Create the server socket
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen()
    server.setblocking(False)

    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_sock.bind((HOST, UDP_PORT))
    udp_sock.setblocking(False)

    # Dedicated control listener (survey/fix commands) — kept off the data stream
    ctrl_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ctrl_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ctrl_server.bind((HOST, CTRL_PORT))
    ctrl_server.listen()
    ctrl_server.setblocking(False)

    print(f"[SERVER] UDP listening on {HOST}:{UDP_PORT}")
    print(f"[SERVER] Server listening on {HOST}:{PORT}")
    print(f"[SERVER] Control listening on {HOST}:{CTRL_PORT}")

    sockets = [server, udp_sock, ctrl_server]  # includes all connected sockets
    clients = {}        # map client socket -> {'buffer': bytearray, 'id': int}
    clients_by_id = {}

    ctrl_clients = {}   # map control socket -> {'buffer': bytearray, 'addr': ..., 'id': int}
    ctrl_by_id = {}     # map esp mac_id -> its control socket
    cpu_extra = {}      # map esp mac_id -> (notice, warn, error) from the latest inst=91

    udp_clients = set()

    last_time = 0
    esp_unique_ID_list = []

    event_num_BH = 1
    event_num_veto = -1

    # Slow-control routing: maps esp_mac_id -> gui_socket waiting for the reply
    pending_slow_ctrl = {}

    rx_buffer = bytearray()
    while True:

        if int(time.time())%60==0 and int(time.time())!=last_time: #To let you know server is running
            print(".")
            last_time = int(time.time())

        readable, _, _ = select.select(sockets, [], [], 0.1)

        for s in readable:
            
            if s is server: #Deals with server connects to client sockets
                try:
                    client_socket, addr = server.accept()
                    client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    time.sleep(0.1)
                    client_socket.setblocking(False)
                    sockets.append(client_socket)
                    clients[client_socket] = { #Dictionary of client raw data and addresses
                        "buffer": bytearray(),
                        "addr": addr
                    }

                    if addr[0] == '134.69.77.98':
                        udp_clients.add((addr[0], 12346))

                    print(f"[SERVER] New ESP32 connected from {addr}")
                    with open(writer.connection_log, 'a') as log:
                        log.write(f"{datetime.now()}: Reconnected from {addr}\n")

                except OSError as e:
                    if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        # No client to accept right now, ignore
                        continue
                    else:
                        print(f"[SERVER] Error accepting connection: {addr}; {e}")
                        with open(writer.error_log, 'a') as f:
                            f.write(f"[SERVER] Error accepting connection: {addr}; {e}\n")
                        continue

            elif s is ctrl_server:  # New control connection (ESP or GUI)
                try:
                    ctrl_socket, addr = ctrl_server.accept()
                    ctrl_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    ctrl_socket.setblocking(False)
                    sockets.append(ctrl_socket)
                    ctrl_clients[ctrl_socket] = {"buffer": bytearray(), "addr": addr}
                    print(f"[SERVER] Control connection from {addr}")
                except OSError as e:
                    if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        continue
                    else:
                        print(f"[SERVER] Error accepting control connection: {e}")
                        continue

            elif s in ctrl_clients:  # Slow-control traffic (survey/fix)
                info = ctrl_clients.get(s)
                caddr = info["addr"] if info else None
                try:
                    data = s.recv(2048)
                    if not data:
                        cleanup_ctrl_socket(s, caddr, sockets, ctrl_clients, ctrl_by_id, pending_slow_ctrl)
                        continue
                except OSError as e:
                    if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        continue
                    else:
                        cleanup_ctrl_socket(s, caddr, sockets, ctrl_clients, ctrl_by_id, pending_slow_ctrl)
                        continue

                info["buffer"].extend(data)
                # Crash-proof: a bad packet or a momentarily-locked log file must
                # never take down run_server (that would silently kill the control channel).
                try:
                  while len(info["buffer"]) >= PACKET_SIZE:
                    packet = info["buffer"][:PACKET_SIZE]
                    info["buffer"] = info["buffer"][PACKET_SIZE:]
                    inst, ID, RF, Cal, ch, w_num, ms, sub_ms, event_num, count = struct.unpack(PACKET_FORMAT, packet)

                    # An ESP registers its control socket by mac_id (sends a hello on connect)
                    if ID != GUI_ID:
                        ctrl_by_id[ID] = s
                        info["id"] = ID

                    if inst in (201, 202, 203, 204, 205, 206, 207, 208, 209, 210, 211):
                        if ID == GUI_ID:
                            # Command from GUI -> forward to the target ESP's control socket
                            target_id   = RF
                            target_sock = ctrl_by_id.get(target_id)
                            if target_sock:
                                if inst in (201, 204, 206, 208, 209, 211):  # status/probe/read/version/TP1-read/posllh: expect a reply
                                    pending_slow_ctrl[target_id] = s
                                    fwd = struct.pack(BCAST_FORMAT, inst, 0, 0, 0, 0)
                                else:                     # 202/203/205/207/210 carry parameters
                                    fwd = struct.pack(BCAST_FORMAT, inst, w_num, ms, sub_ms, event_num)
                                try:
                                    target_sock.sendall(fwd)
                                    print(f"[SERVER] ctrl inst={inst} forwarded to ESP {target_id}")
                                except OSError:
                                    t_info = ctrl_clients.get(target_sock)
                                    t_addr = t_info["addr"] if t_info else "Unknown"
                                    cleanup_ctrl_socket(target_sock, t_addr, sockets, ctrl_clients, ctrl_by_id, pending_slow_ctrl)
                            else:
                                print(f"[SERVER] ctrl inst={inst}: ESP {target_id} not connected")
                                with open(writer.error_log, 'a') as f:
                                    f.write(f"ctrl inst={inst}: ESP {target_id} not connected\n")
                        else:
                            # Reply / confirmation from an ESP
                            writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                            if inst in (201, 204, 206, 208, 209, 211):
                                gui_sock = pending_slow_ctrl.pop(ID, None)
                                if gui_sock and gui_sock in sockets:
                                    response = struct.pack(PACKET_FORMAT, inst, ID, RF, Cal, ch, w_num, ms, sub_ms, event_num, count)
                                    try:
                                        gui_sock.sendall(response)
                                    except OSError:
                                        pass

                    elif inst == 91:  # GPS health counts (sent right before inst=90)
                        cpu_extra[ID] = (RF, Cal, ch)   # notice, warn, error
                        print("Inst 91 found")
                    elif inst == 90:  # autonomous GPS CPU/health telemetry from an ESP
                        print("Inst 90 found")
                        # RF=cpuLoad Cal=memUsage ch=ioUsage w_num=temp
                        # ms=cpuLoadMax sub_ms=memUsageMax event_num=ioUsageMax count=runTime
                        notice, warn, error = cpu_extra.get(ID, (0, 0, 0))
                        try:
                            with open(writer.cpu_log, 'a') as f:
                                # cols: ts; ID; cpuLoad; cpuLoadMax; memUsage; memUsageMax; ioUsage; ioUsageMax; runTime; temp; notice; warn; error
                                f.write(f"{datetime.now()}; {ID}; {RF}; {ms}; {Cal}; {sub_ms}; "
                                        f"{ch}; {event_num}; {count}; {w_num}; {notice}; {warn}; {error}\n")
                        except OSError as e:
                            print(f"[SERVER] cpu_log write skipped: {e}")
                except Exception as e:
                    print(f"[SERVER] ctrl handler error (packet dropped): {e}")
                    info["buffer"] = bytearray()

            else: #Reads client data
                client_info = clients.get(s)
                client_addr = client_info["addr"] if client_info else None
                try:
                    data = s.recv(2048)
                    if not data:  # empty = client closed connection
                        #cleanup_client_socket(s, client_addr, sockets, clients)
                        cleanup_client_socket(s, client_addr, sockets, clients, clients_by_id)
                        continue

                except OSError as e:
                    if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        # Non-blocking socket has no data, this is fine
                        continue
                    else: #ETimedout isnt being used, ECONNRESET means client coneection was disconnected
                        print(f"[SERVER] Error Receiving from {client_addr}: {e}")
                        print("Debug",  s.fileno())
                        with open(writer.error_log, 'a') as f:
                            f.write(f"[SERVER] Error Receiving from {client_addr}: {e}\n")


                        #cleanup_client_socket(s, client_addr, sockets, clients)
                        cleanup_client_socket(s, client_addr, sockets, clients, clients_by_id)

                        continue

                clients[s]["buffer"].extend(data)
                T0 = time.time()
                # Packet buffer to handle multiple packets at the same time
                while len(clients[s]["buffer"]) >= PACKET_SIZE:
                    packet = clients[s]["buffer"][:PACKET_SIZE]
                    clients[s]["buffer"] = clients[s]["buffer"][PACKET_SIZE:]

                    #Unpacks data
                    inst, ID, RF, Cal, ch, w_num, ms, sub_ms, event_num, count = struct.unpack(PACKET_FORMAT, packet)
                    
                    if clients[s].get("id") != ID:
                        existing_sock = clients_by_id.get(ID)

                        if existing_sock is not None and existing_sock != s:
                            old_info = clients.get(existing_sock)
                            old_addr = old_info["addr"] if old_info else None

                            print(f"[SERVER] ID {ID} reconnected. Closing old socket {old_addr}")

                            #cleanup_client_socket(existing_sock, old_addr, sockets, clients)
                            cleanup_client_socket(existing_sock, old_addr, sockets, clients, clients_by_id)
                        clients_by_id[ID] = s
                        clients[s]["id"] = ID

                    if inst == 1: #Info Code
                        with open(writer.connection_log, 'a') as log:
                            log.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                    elif inst == 100: #Error code
                        #writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                        with open(writer.error_log, 'a') as f:
                            f.write(f"[ESP]:{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n") # Ignore labels; RF = Error Code

                    elif inst == 95: #Unreasonable Request
                        writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                    
                    elif inst == 94: #Unreasonable Request
                        writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                    elif inst == 93: #Unreasonable Request
                         writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                    elif inst == 96: #Unreasonable Request
                        writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                    elif inst == 97: #Transit Time Code

                        if ID == 48:
                            try:
                                event_num = prev_event_num_BH 
                                writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                                #gps_to_utc = gps_to_utc_seconds(w_num, ms, sub_ms)
                                #print("BH transit time:", (time.time()-gps_to_utc)*1000)
                            except Exception:
                                pass
                        elif ID == 16:
                            try:
                                event_num = event_num_veto #I think this must be due to how the code is on the esp
                                writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                            except Exception:
                                pass
                        else:
                            writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                    elif inst == 98: #Rate Code
                        writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                        
                    elif inst == 99: #Data Code
                        if ID not in esp_unique_ID_list:
                            esp_unique_ID_list.append(ID)
                            with open(writer.unique_esp_log, 'a') as f:
                                f.write(f"{str(ID)}\n")

                        if ID == 164:
                            
                            if ch == 0 and RF == 0: #Measuring time from rise  
                                event_num = event_num_BH
                                prev_event_num_BH = event_num_BH
                                writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                                event_num_BH+=1

                                rise_BH_timestamp = ns_timestamp(ms, sub_ms)
                                
                                
                                for client_ID in list(clients_by_id.keys()):

                                    if client_ID not in (164, 16, GUI_ID):
                                        client_sock = clients_by_id.get(client_ID)
                                        
                                        if client_sock is None:
                                            continue
                                        
                                        client_info = clients.get(client_sock)
                                        client_addr = client_info["addr"] if client_info else "Unknown"
                                  
                                        try:
                                            broadcast_format = '!iiiii'
                                            broadcast_packet = struct.pack(broadcast_format, inst, w_num, ms, sub_ms, event_num)

                                            broadcast = client_sock.sendall(broadcast_packet)
                
                                        except OSError as e:
                                            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                                                # Error can be skipped
                                                continue

                                            else:  # ECONNRESET or other fatal error
                                                print(f"[SERVER] Send Error to {client_addr}: {e}")
                                                with open(writer.error_log, 'a') as f:
                                                    f.write(f"[SERVER] Send Error to {client_addr}: {e}\n")
                                                
                                                #cleanup_client_socket(client_sock, client_addr, sockets, clients)
                                                cleanup_client_socket(client_sock, client_addr, sockets, clients, clients_by_id)

                                                continue
                                
                                for udp_addr in udp_clients:
                                    
                                    udp_sock.sendto(broadcast_packet, udp_addr)

                            if ch == 0 and RF == 1: 

                                fall_BH_timestamp = ns_timestamp(ms,sub_ms)
                                
                                try:
                                    if abs(rise_BH_timestamp-fall_BH_timestamp)<5000:

                                        event_num = prev_event_num_BH
                                        writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                                except Exception:
                                    pass

                            if ch == 1:
                                try:
                                    event_num = prev_event_num_BH
                                    writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                                except Exception:
                                    pass

                        elif ID == 16:

                            if ch == 0 and RF == 0: #Measuring time from rise     
                                event_num = event_num_veto
                                prev_event_num_veto = event_num_veto
                                writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                                event_num_veto-=1

                                rise_veto_timestamp = ns_timestamp(ms, sub_ms)    

                                for client_ID in list(clients_by_id.keys()):

                                    if client_ID not in (128, 16, GUI_ID):
                                        client_sock = clients_by_id.get(client_ID)
                                        
                                        if client_sock is None:
                                            continue

                                        client_info = clients.get(client_sock)
                                        client_addr = client_info["addr"] if client_info else "Unknown"
                                        try:
                                            broadcast_format = '!iiiii'
                                            broadcast_packet = struct.pack(broadcast_format, inst, w_num, ms, sub_ms, event_num)

                                            broadcast = client_sock.sendall(broadcast_packet)
                                                                                                
                                        except OSError as e:
                                            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                                                # Error can be skipped
                                                continue

                                            else:  # ECONNRESET or other fatal error
                                                print(f"[SERVER] Send Error to {client_addr}: {e}")
                                                with open(writer.error_log, 'a') as f:
                                                    f.write(f"[SERVER] Send Error to {client_addr}: {e}\n")
                                                
                                                #cleanup_client_socket(client_sock, client_addr, sockets, clients)
                                                cleanup_client_socket(client_sock, client_addr, sockets, clients, clients_by_id)

                                                continue

                            if ch == 0 and RF == 1: 

                                fall_veto_timestamp = ns_timestamp(ms,sub_ms)

                                if abs(rise_veto_timestamp-fall_veto_timestamp)<5000:

                                    event_num = prev_event_num_veto
                                    writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                              
                            if ch == 1:
                                try:
                                    event_num = prev_event_num_veto
                                    writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                                except Exception:
                                    pass

                        else: #For other clients
                            writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                    elif inst == 201:  # Survey-in status query / response
                        if ID == GUI_ID:
                            # Request from GUI: forward to target ESP (RF = target mac_id)
                            print("Survey request")
                            target_id   = RF
                            target_sock = clients_by_id.get(target_id)
                            if target_sock:
                                pending_slow_ctrl[target_id] = s
                                fwd = struct.pack(BCAST_FORMAT, 201, 0, 0, 0, 0)
                                try:
                                    target_sock.sendall(fwd)
                                    print(f"[SERVER] inst=201 forwarded to ESP {target_id}")
                                except OSError as e:
                                    t_info = clients.get(target_sock)
                                    t_addr = t_info["addr"] if t_info else "Unknown"
                                    cleanup_client_socket(target_sock, t_addr, sockets, clients, clients_by_id)
                            else:
                                print(f"[SERVER] inst=201: ESP {target_id} not connected")
                                with open(writer.error_log, 'a') as f:
                                    f.write(f"inst=201: ESP {target_id} not connected\n")
                        else:
                            # Response from ESP: log and forward back to waiting GUI
                            print("Survey Reply")
                            writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                            gui_sock = pending_slow_ctrl.pop(ID, None)
                            if gui_sock and gui_sock in sockets:
                                response = struct.pack(PACKET_FORMAT, inst, ID, RF, Cal, ch, w_num, ms, sub_ms, event_num, count)
                                try:
                                    gui_sock.sendall(response)
                                except OSError:
                                    pass

                    elif inst == 202:  # Start survey-in command / confirmation
                        if ID == GUI_ID:
                            # Command from GUI: forward parameters to target ESP
                            target_id   = RF
                            target_sock = clients_by_id.get(target_id)
                            if target_sock:
                                # w_num=min_dur_s, ms=acc_01mm
                                fwd = struct.pack(BCAST_FORMAT, 202, w_num, ms, sub_ms, event_num)
                                try:
                                    target_sock.sendall(fwd)
                                    print(f"[SERVER] inst=202 (start survey-in) forwarded to ESP {target_id}")
                                except OSError as e:
                                    t_info = clients.get(target_sock)
                                    t_addr = t_info["addr"] if t_info else "Unknown"
                                    cleanup_client_socket(target_sock, t_addr, sockets, clients, clients_by_id)
                            else:
                                print(f"[SERVER] inst=202: ESP {target_id} not connected")
                        else:
                            # Confirmation from ESP
                            writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                    elif inst == 203:  # Set fixed coordinates command / confirmation
                        if ID == GUI_ID:
                            # Command from GUI: forward coords to target ESP
                            target_id   = RF
                            target_sock = clients_by_id.get(target_id)
                            if target_sock:
                                # w_num=X_cm, ms=Y_cm, sub_ms=Z_cm, event_num=acc_01mm
                                fwd = struct.pack(BCAST_FORMAT, 203, w_num, ms, sub_ms, event_num)
                                try:
                                    target_sock.sendall(fwd)
                                    print(f"[SERVER] inst=203 (set fixed coords) forwarded to ESP {target_id}")
                                except OSError as e:
                                    t_info = clients.get(target_sock)
                                    t_addr = t_info["addr"] if t_info else "Unknown"
                                    cleanup_client_socket(target_sock, t_addr, sockets, clients, clients_by_id)
                            else:
                                print(f"[SERVER] inst=203: ESP {target_id} not connected")
                                with open(writer.error_log, 'a') as f:
                                    f.write(f"inst=203: ESP {target_id} not connected\n")
                        else:
                            # Confirmation from ESP
                            writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                    elif inst == 204:  # Probe fixed-position query / response
                        if ID == GUI_ID:
                            # Request from GUI: forward to target ESP (RF = target mac_id)
                            print("Fixed-position probe request")
                            target_id   = RF
                            target_sock = clients_by_id.get(target_id)
                            if target_sock:
                                pending_slow_ctrl[target_id] = s
                                fwd = struct.pack(BCAST_FORMAT, 204, 0, 0, 0, 0)
                                try:
                                    target_sock.sendall(fwd)
                                    print(f"[SERVER] inst=204 forwarded to ESP {target_id}")
                                except OSError as e:
                                    t_info = clients.get(target_sock)
                                    t_addr = t_info["addr"] if t_info else "Unknown"
                                    cleanup_client_socket(target_sock, t_addr, sockets, clients, clients_by_id)
                            else:
                                print(f"[SERVER] inst=204: ESP {target_id} not connected")
                                with open(writer.error_log, 'a') as f:
                                    f.write(f"inst=204: ESP {target_id} not connected\n")
                        else:
                            # Response from ESP: log and forward back to waiting GUI
                            print("Fixed-position probe reply")
                            writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                            gui_sock = pending_slow_ctrl.pop(ID, None)
                            if gui_sock and gui_sock in sockets:
                                response = struct.pack(PACKET_FORMAT, inst, ID, RF, Cal, ch, w_num, ms, sub_ms, event_num, count)
                                try:
                                    gui_sock.sendall(response)
                                except OSError:
                                    pass

                    else:
                        #writer.write("Unknown Code\n")
                        #
                        with open(writer.error_log, 'a') as f:
                            f.write(f"Unknown Code from client {client_addr}\n")

                        clients[s]["buffer"].clear()
                        print(f"Buffer cleared from client {client_addr}")

                        break

            

if __name__ == "__main__":

    import argparse
    parser = argparse.ArgumentParser(description="GPS DAQ server")
    parser.add_argument("--file-hours", type=float, default=1.0,
                        help="hours of data per file/cycle (default 1)")
    parser.add_argument("--max-cycles", type=int, default=0,
                        help="cycles before auto-restarting into a new run (0 = unlimited)")
    cli = parser.parse_args()
    print(f"[SERVER] file-hours={cli.file_hours}  max-cycles={cli.max_cycles or 'unlimited'}")

    try:
        while True:
            try:

                HEADER = "Req Code; ID; RF; Cal; Ch; W#; t_ow mil; t_ow submil; Event; GPS Count"
                writer = RotatingFileWriter(base_folder_name= "Run", base_file_name="gps_daq", ext=".txt",
                                            time_length = cli.file_hours, header = HEADER,
                                            max_cycles = cli.max_cycles)
                rise_BH_timestamp = 0
                fall_BH_timestamp = 0
                run_server()

            except CycleLimitReached: # Cycle limit hit -> roll over into a fresh run
                print(f"[SERVER] Cycle limit ({cli.max_cycles}) reached — starting a new run")
                writer.close()

            except Exception as e: #Auto-restart server on any errors
                print(f"[FATAL ERROR] {e}")
                traceback.print_exc()
                with open(writer.error_log, 'a') as f:
                    f.write(f"[SERVER] Fatal error: {e}\n{traceback.format_exc()}\n")

                writer.close()

                print("Restarting server in 3 seconds...")
                time.sleep(3)

    except KeyboardInterrupt:
        print("DAQ Stopped")
    #Note: Im noticing the esp keeps writting to the TCP buffer even after server shutdown, becasue Im not handling closing the sockets on shutdown.
    #May be something to worry about in the future, but right now its not a concern. I can probably just have a for loop through the clients list

    finally:
        writer.close()


"""
            if s is udp_sock:
                try:
                    data, addr = udp_sock.recvfrom(2048)
                    #print(udp_sock)
                    if addr[0] == "134.69.77.82" and data:  # Only accept trigger ESP

                        if len(data) % PACKET_SIZE != 0:
                            print("Corrupt UDP packet size:", len(data))
                            continue
                        
                        for pack_step in range(0, len(data), PACKET_SIZE):
                            packet = data[pack_step:pack_step + PACKET_SIZE]    
                                                  
                            inst, ID, RF, Cal, ch, w_num, ms, sub_ms, event_num, count = struct.unpack(PACKET_FORMAT, packet)

                            if inst == 1: #Info Code
                                with open(writer.connection_log, 'a') as log:
                                    log.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                            elif inst == 100: #Error code
                                #writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                                with open(writer.error_log, 'a') as f:
                                    f.write(f"[ESP]:{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n") # Ignore labels; RF = Error Code

                            elif inst == 97: #Transit Time Code

                                if ID == 48:
                                    try:
                                        event_num = prev_event_num_BH 
                                        writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                                        gps_to_utc = gps_to_utc_seconds(w_num, ms, sub_ms)
                                        #rint("BH transit time:", (time.time()-gps_to_utc)*1000)
                                    except Exception:
                                        pass
                                    
                                elif ID == 16:
                                    try:
                                        event_num = event_num_veto #I think this must be due to how the code is on the esp
                                        writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                                    except Exception:
                                        pass
                                else:
                                    writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                            elif inst == 98: #Rate Code
                                writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                                
                            elif inst == 99: #Data Code
                                if ID not in esp_unique_ID_list:
                                    esp_unique_ID_list.append(ID)
                                    with open(writer.unique_esp_log, 'a') as f:
                                        f.write(f"{str(ID)}\n")

                                if ID == 48:
                                    #event_num = event_num_BH
                                    #print(type(ms))
                                    #writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                                    #print("BH data written to file")
                                    #T0 = time.time()
                                    
                                    if ch == 0 and RF == 0: #Measuring time from rise  
                                        event_num = event_num_BH
                                        prev_event_num_BH = event_num_BH
                                        writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                                        event_num_BH+=1

                                        rise_BH_timestamp = ns_timestamp(ms, sub_ms)
                                        
                                        #T0 = time.time()
                                        for client_sock in list(clients.keys()):
                                            if client_sock != s: #Broadcasts BH timestamp to all other clients except sender
                                                
                                                client_info = clients.get(client_sock)
                                                client_addr = client_info["addr"] if client_info else "Unknown"
                                                #print(client_addr)
                                                if client_addr[0]!="134.69.77.98":                                     
                                                    try:
                                                        #time.sleep(0.1) #delay the broadcast by 100ms
                                                        broadcast_format = '!iiiii'
                                                        broadcast_packet = struct.pack(broadcast_format, inst, w_num, ms, sub_ms, event_num)

                                                    
                                                        
                                                        broadcast = client_sock.sendall(broadcast_packet)
                                                        #print("T0:", T0)
                                                        #print(time.time())
                                                        #print(f'{(time.time()-T0)*1000}, {client_addr}')
                                                                                                            
                                                            #print(f'Request for data sent:{broadcast}')
                            
                                                    except OSError as e:
                                                        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                                                            # Error can be skipped
                                                            continue

                                                        else:  # ECONNRESET or other fatal error
                                                            print(f"[SERVER] Send Error to {client_addr}: {e}")
                                                            with open(writer.error_log, 'a') as f:
                                                                f.write(f"[SERVER] Send Error to {client_addr}: {e}\n")
                                                            
                                                            #cleanup_client_socket(client_sock, client_addr, sockets, clients)
                                                            cleanup_client_socket(client_sock, client_addr, sockets, clients, clients_by_id)

                                                            continue
                                            
                                        for udp_addr in udp_clients:
                                            
                                            udp_sock.sendto(broadcast_packet, udp_addr)
                                            
                                    if ch == 0 and RF == 1: 

                                        fall_BH_timestamp = ns_timestamp(ms,sub_ms)
                                        
                                        try:
                                            if abs(rise_BH_timestamp-fall_BH_timestamp)<5000:

                                                event_num = prev_event_num_BH
                                                writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                                        except Exception:
                                            pass

                                    if ch == 1:
                                        try:
                                            event_num = prev_event_num_BH
                                            writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                                        except Exception:
                                            pass

                                elif ID == 16:
                                #event_num = event_num_veto
                                #writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                                #print("BH data written to file")
                                
                                    if ch == 0 and RF == 0: #Measuring time from rise     
                                        event_num = event_num_veto
                                        prev_event_num_veto = event_num_veto
                                        writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                                        event_num_veto-=1

                                        rise_veto_timestamp = ns_timestamp(ms, sub_ms)    
                                                            
                                        for client_sock in list(clients.keys()):
                                            #if client_sock.fileno() == -1:
                                                
                                            if client_sock != s: #Broadcasts BH timestamp to all other clients except sender
                                                
                                                client_info = clients.get(client_sock)
                                                client_addr = client_info["addr"] if client_info else "Unknown"
                                                try:
                                                    #time.sleep(0.1) #delay the broadcast by 100ms
                                                    broadcast_format = '!iiiii'
                                                    broadcast_packet = struct.pack(broadcast_format, inst, w_num, ms, sub_ms, event_num)
                                                    
                                                    if client_addr[0] == '134.69.77.98':
                                                        udp_sock.sendto(broadcast_packet, (client_addr[0], 12346))
                                                    
                                                    else:
                                                        broadcast = client_sock.sendall(broadcast_packet)
                                                                                                        
                                                    #print(f'Request for data sent:{broadcast}')
                            
                                                except OSError as e:
                                                    if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                                                        # Error can be skipped
                                                        continue

                                                    else:  # ECONNRESET or other fatal error
                                                        print(f"[SERVER] Send Error to {client_addr}: {e}")
                                                        with open(writer.error_log, 'a') as f:
                                                            f.write(f"[SERVER] Send Error to {client_addr}: {e}\n")
                                                        
                                                        #cleanup_client_socket(client_sock, client_addr, sockets, clients)
                                                        cleanup_client_socket(client_sock, client_addr, sockets, clients, clients_by_id)

                                                        continue

                                    if ch == 0 and RF == 1: 

                                        fall_veto_timestamp = ns_timestamp(ms,sub_ms)

                                        if abs(rise_veto_timestamp-fall_veto_timestamp)<5000:

                                            event_num = prev_event_num_veto
                                            writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")
                                        

                                    else: #For other clients
                                        writer.write(f"{inst}; {ID}; {RF}; {Cal}; {ch}; {w_num}; {ms}; {sub_ms}; {event_num}; {count}\n")

                            else:
                                #writer.write("Unknown Code\n")
                                #    
                                with open(writer.error_log, 'a') as f:
                                    f.write(f"Unknown Code from client {client_addr}\n")

                                clients[s]["buffer"].clear()
                                print(f"Buffer cleared from client {client_addr}")


                except OSError:
                    continue"""