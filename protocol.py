import json
import struct
import socket
from typing import Optional, Tuple, Dict, Any

# Tipos de paquetes binarios (1 byte)
MSG_JSON = 0x01
MSG_FRAME = 0x02


def send_packet(sock: socket.socket, msg_type: int, payload: bytes) -> bool:
    """Envía un paquete con cabecera: [1 byte tipo][4 bytes longitud big-endian][payload]."""
    try:
        header = struct.pack("!BI", msg_type, len(payload))
        sock.sendall(header + payload)
        return True
    except OSError:
        return False


def send_json(sock: socket.socket, data: Dict[str, Any]) -> bool:
    """Serializa un diccionario a JSON y lo envía como paquete MSG_JSON."""
    raw = json.dumps(data).encode("utf-8")
    return send_packet(sock, MSG_JSON, raw)


def send_frame(
    sock: socket.socket,
    monitor_index: int,
    width: int,
    height: int,
    jpeg_bytes: bytes,
) -> bool:
    """
    Envía un frame de video comprimido en JPEG.
    Cabecera interna del frame: [1B monitor_idx][2B width][2B height][jpeg_bytes]
    """
    frame_header = struct.pack("!BHH", monitor_index, width, height)
    return send_packet(sock, MSG_FRAME, frame_header + jpeg_bytes)


def recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    """Lee exactamente n bytes del socket o devuelve None si se cierra la conexión."""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        except OSError:
            return None
    return bytes(buf)


def recv_packet(sock: socket.socket) -> Optional[Tuple[int, bytes]]:
    """Recibe un paquete completo y retorna (msg_type, payload) o None si se desconectó."""
    header = recv_exact(sock, 5)
    if not header:
        return None
    msg_type, length = struct.unpack("!BI", header)
    if length > 20 * 1024 * 1024:  # Límite de seguridad de 20 MB por paquete
        return None
    payload = recv_exact(sock, length)
    if payload is None:
        return None
    return msg_type, payload
