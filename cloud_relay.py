import json
import uuid
import time
import socket
import threading
import logging
from typing import Callable, Optional, Tuple

import paho.mqtt.client as mqtt

CLOUD_BROKERS = [
    ("broker.emqx.io", 1883),
    ("broker.hivemq.com", 1883),
    ("mqtt.eclipseprojects.io", 1883),
]

TOPIC_PREFIX = "remotesupport_v1_ton_2026"


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def create_mqtt_client() -> mqtt.Client:
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        return mqtt.Client()


def connect_to_available_broker(client: mqtt.Client) -> Optional[str]:
    for host, port in CLOUD_BROKERS:
        try:
            client.connect(host, port, keepalive=30)
            return f"{host}:{port}"
        except Exception:
            continue
    return None


class MqttVirtualSocket:
    """
    Socket virtual sobre MQTT optimizado para baja latencia entre ciudades:
    - Control de flujo (Flow Control): evita que se acumulen frames viejos en la cola de envío,
      eliminando el retraso progresivo (lag) cuando la conexión de Internet es más lenta.
    """

    is_cloud_tunnel = True

    def __init__(self, mqtt_client: mqtt.Client, tx_topic: str, on_close_cb: Optional[Callable] = None):
        self.client = mqtt_client
        self.tx_topic = tx_topic
        self.on_close_cb = on_close_cb

        self.buf = bytearray()
        self.cond = threading.Condition()
        self.closed = False
        self.timeout: Optional[float] = None
        self.last_ack_time = time.perf_counter()
        self.frames_in_flight = 0

    def feed_data(self, data: bytes):
        with self.cond:
            if not self.closed:
                self.buf.extend(data)
                self.cond.notify_all()

    def feed_ack(self):
        with self.cond:
            self.frames_in_flight = max(0, self.frames_in_flight - 1)
            self.last_ack_time = time.perf_counter()

    def send_ack(self):
        if not self.closed:
            try:
                self.client.publish(f"{self.tx_topic}/ack", payload=b"1", qos=0)
            except Exception:
                pass

    def can_send_frame(self) -> bool:
        """Evita saturar el broker en la nube si aún hay más de 2 frames en tránsito."""
        if self.frames_in_flight <= 2:
            return True
        if time.perf_counter() - self.last_ack_time > 1.2:
            self.frames_in_flight = 0
            return True
        return False

    def mark_closed(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def settimeout(self, value: Optional[float]):
        self.timeout = value

    def setsockopt(self, *args, **kwargs):
        pass

    def recv(self, n: int) -> bytes:
        with self.cond:
            while len(self.buf) == 0 and not self.closed:
                notified = self.cond.wait(timeout=self.timeout)
                if not notified and self.timeout is not None and len(self.buf) == 0:
                    raise TimeoutError("Tiempo de espera agotado en túnel Cloud")
            if len(self.buf) == 0 and self.closed:
                return b""
            chunk = bytes(self.buf[:n])
            del self.buf[:n]
            return chunk

    def sendall(self, data: bytes):
        if self.closed:
            raise OSError("El túnel Cloud está cerrado")
        # Si es un paquete de frame de video (0x02), contabilizarlo en control de flujo
        if len(data) > 0 and data[0] == 0x02:
            self.frames_in_flight += 1
        info = self.client.publish(self.tx_topic, payload=data, qos=0)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            self.mark_closed()
            raise OSError("No se pudo transmitir por el túnel Cloud")

    def close(self):
        if not self.closed:
            self.mark_closed()
            try:
                self.client.publish(f"{self.tx_topic}/close", payload=b"1", qos=0)
            except Exception:
                pass
            if self.on_close_cb:
                self.on_close_cb()


class CloudHostRelay:
    """
    Registra el ID de 9 dígitos del Host en la Nube.
    Cuando un Visor solicita conectarse:
    1) Muestra la ventana de "Aceptar / Rechazar" en el Host.
    2) Si el usuario acepta:
       - Primero intenta establecer una **Conexión TCP Inversa Directa (Host -> Visor)** hacia la IP del Visor.
         Al ser una conexión SALIENTE desde el Host hacia el Visor, ofrece velocidad máxima instantánea (LAN)
         sin que el Host tenga que abrir puertos de escucha.
       - Si están en distintas ciudades y el router no permite conexión directa, usa automáticamente el
         `MqttVirtualSocket` optimizado con control de flujo.
    """

    def __init__(
        self,
        host_id: str,
        on_status: Optional[Callable[[str], None]] = None,
        on_connection_request: Optional[Callable[[dict, Callable[[bool], None]], None]] = None,
        on_session_ready: Optional[Callable[[object, dict], None]] = None,
    ):
        self.host_id = host_id.replace(" ", "").replace("-", "")
        self.on_status = on_status
        self.on_connection_request = on_connection_request
        self.on_session_ready = on_session_ready

        self.running = False
        self.client: Optional[mqtt.Client] = None
        self.active_tunnels = {}

    def start(self):
        self.running = True
        threading.Thread(target=self._run_loop, daemon=True).start()

    def _run_loop(self):
        req_topic = f"{TOPIC_PREFIX}/host/{self.host_id}/request"

        while self.running:
            try:
                client = create_mqtt_client()
                self.client = client

                def on_connect(c, userdata, flags, rc, *args):
                    c.subscribe(req_topic, qos=1)
                    if self.on_status:
                        self.on_status("🌐 En línea - Listo para recibir conexiones")

                def on_message(c, userdata, msg):
                    topic = msg.topic
                    if topic == req_topic:
                        self._handle_incoming_request(msg.payload)
                    elif topic.endswith("/v2h"):
                        parts = topic.split("/")
                        if len(parts) >= 3:
                            sid = parts[-2]
                            vsock = self.active_tunnels.get(sid)
                            if vsock:
                                vsock.feed_data(msg.payload)
                    elif topic.endswith("/v2h/ack"):
                        parts = topic.split("/")
                        if len(parts) >= 4:
                            sid = parts[-3]
                            vsock = self.active_tunnels.get(sid)
                            if vsock:
                                vsock.feed_ack()
                    elif topic.endswith("/v2h/close"):
                        parts = topic.split("/")
                        if len(parts) >= 4:
                            sid = parts[-3]
                            vsock = self.active_tunnels.pop(sid, None)
                            if vsock:
                                vsock.mark_closed()

                client.on_connect = on_connect
                client.on_message = on_message

                broker_label = connect_to_available_broker(client)
                if not broker_label:
                    if self.on_status:
                        self.on_status("Sin conexión al servidor de señalización")
                    time.sleep(5)
                    continue

                client.loop_forever()
            except Exception as e:
                logging.debug(f"Reconectando CloudHostRelay: {e}")
                time.sleep(4)

    def _handle_incoming_request(self, payload: bytes):
        if not self.client:
            return
        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception:
            return

        session_id = data.get("session_id", "")
        if not session_id:
            return

        viewer_lan_ip = data.get("viewer_lan_ip", "")
        viewer_tcp_port = int(data.get("viewer_tcp_port", 0))

        resp_topic = f"{TOPIC_PREFIX}/session/{session_id}/response"
        self.client.publish(
            resp_topic,
            json.dumps({"status": "waiting_approval"}).encode("utf-8"),
            qos=1,
        )

        viewer_info = {
            "viewer_name": data.get("viewer_name", "Equipo Remoto"),
            "viewer_id": data.get("viewer_id", ""),
            "viewer_addr": viewer_lan_ip or "Remoto",
        }

        def decision_cb(accepted: bool):
            if not self.client:
                return
            if not accepted:
                self.client.publish(
                    resp_topic,
                    json.dumps(
                        {
                            "status": "rejected",
                            "message": "El usuario rechazó la solicitud de conexión.",
                        }
                    ).encode("utf-8"),
                    qos=1,
                )
                return

            # 1) Intentar primero Conexión TCP Inversa Directa (Host se conecta hacia el Visor)
            # Es ultra rápida (0 lag) y como es SALIENTE desde el Host, el Host no abre puertos.
            if viewer_lan_ip and viewer_tcp_port > 0:
                try:
                    direct_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    direct_sock.settimeout(0.8)
                    direct_sock.connect((viewer_lan_ip, viewer_tcp_port))
                    direct_sock.settimeout(None)
                    direct_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    direct_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)

                    self.client.publish(
                        resp_topic,
                        json.dumps({"status": "connected", "mode": "direct_reverse"}).encode("utf-8"),
                        qos=1,
                    )
                    if self.on_session_ready:
                        self.on_session_ready(direct_sock, viewer_info)
                    return
                except Exception:
                    pass

            # 2) Respaldo: Túnel Cloud optimizado con Control de Flujo (para equipos en distintas ciudades)
            tx_topic = f"{TOPIC_PREFIX}/session/{session_id}/h2v"
            rx_topic = f"{TOPIC_PREFIX}/session/{session_id}/v2h"

            def cleanup():
                self.active_tunnels.pop(session_id, None)
                try:
                    self.client.unsubscribe(rx_topic)
                    self.client.unsubscribe(f"{rx_topic}/ack")
                    self.client.unsubscribe(f"{rx_topic}/close")
                except Exception:
                    pass

            vsock = MqttVirtualSocket(self.client, tx_topic, on_close_cb=cleanup)
            self.active_tunnels[session_id] = vsock
            self.client.subscribe(rx_topic, qos=0)
            self.client.subscribe(f"{rx_topic}/ack", qos=0)
            self.client.subscribe(f"{rx_topic}/close", qos=0)

            self.client.publish(
                resp_topic,
                json.dumps({"status": "connected", "mode": "cloud"}).encode("utf-8"),
                qos=1,
            )

            if self.on_session_ready:
                self.on_session_ready(vsock, viewer_info)

        if self.on_connection_request:
            self.on_connection_request(viewer_info, decision_cb)
        else:
            decision_cb(True)


def connect_via_cloud_relay(
    target_id: str,
    viewer_name: str,
    viewer_id: str,
    approval_timeout: float = 60.0,
) -> Tuple[Optional[object], str]:
    """
    Inicia la conexión hacia `target_id`.
    Prepara un receptor TCP local para recibir la conexión directa inversa de alta velocidad si están en red
    alcanzable, o usa el túnel Cloud con control de flujo si están en ciudades distintas detrás de NAT.
    """
    clean_target = target_id.replace(" ", "").replace("-", "")
    session_id = uuid.uuid4().hex[:12]
    my_lan_ip = get_lan_ip()

    # Preparar socket temporal para conexión inversa directa de alta velocidad
    reverse_listener = None
    reverse_port = 0
    accepted_direct_sock = {"sock": None}
    try:
        reverse_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        reverse_listener.bind(("0.0.0.0", 0))
        reverse_listener.listen(1)
        reverse_port = reverse_listener.getsockname()[1]

        def accept_reverse():
            try:
                reverse_listener.settimeout(approval_timeout + 5.0)
                conn, _ = reverse_listener.accept()
                conn.settimeout(None)
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
                accepted_direct_sock["sock"] = conn
            except Exception:
                pass

        threading.Thread(target=accept_reverse, daemon=True).start()
    except Exception:
        reverse_port = 0

    req_topic = f"{TOPIC_PREFIX}/host/{clean_target}/request"
    resp_topic = f"{TOPIC_PREFIX}/session/{session_id}/response"
    tx_topic = f"{TOPIC_PREFIX}/session/{session_id}/v2h"
    rx_topic = f"{TOPIC_PREFIX}/session/{session_id}/h2v"

    client = create_mqtt_client()
    state_event = threading.Event()
    decision_event = threading.Event()
    state = {"online": False, "status": "", "mode": "cloud", "message": ""}

    def on_cleanup():
        try:
            if reverse_listener:
                reverse_listener.close()
        except Exception:
            pass
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass

    vsock = MqttVirtualSocket(client, tx_topic, on_close_cb=on_cleanup)

    def on_message(c, userdata, msg):
        t = msg.topic
        if t == resp_topic:
            try:
                resp = json.loads(msg.payload.decode("utf-8"))
                st = resp.get("status", "")
                if st == "waiting_approval":
                    state["online"] = True
                    state_event.set()
                elif st in ("connected", "rejected", "error"):
                    state["online"] = True
                    state["status"] = st
                    state["mode"] = resp.get("mode", "cloud")
                    state["message"] = resp.get("message", "")
                    state_event.set()
                    decision_event.set()
            except Exception:
                pass
        elif t == rx_topic:
            vsock.feed_data(msg.payload)
            # Enviar confirmación ligera (ACK) cuando llega un bloque de frame para regular el flujo
            if len(msg.payload) > 1000:
                vsock.send_ack()
        elif t == f"{rx_topic}/close":
            vsock.mark_closed()

    client.on_message = on_message
    broker = connect_to_available_broker(client)
    if not broker:
        on_cleanup()
        return None, "No se pudo conectar al servidor de señalización en Internet."

    client.subscribe(resp_topic, qos=1)
    client.subscribe(rx_topic, qos=0)
    client.subscribe(f"{rx_topic}/close", qos=0)
    client.loop_start()

    req_payload = json.dumps(
        {
            "session_id": session_id,
            "viewer_name": viewer_name,
            "viewer_id": viewer_id,
            "viewer_lan_ip": my_lan_ip,
            "viewer_tcp_port": reverse_port,
        }
    ).encode("utf-8")
    client.publish(req_topic, req_payload, qos=1)

    if not state_event.wait(timeout=4.5):
        on_cleanup()
        return None, f"El equipo con ID {target_id} no se encuentra en línea."

    if not decision_event.wait(timeout=approval_timeout):
        on_cleanup()
        return None, "El usuario remoto no respondió a la solicitud a tiempo."

    if state["status"] == "connected":
        if state["mode"] == "direct_reverse":
            # Esperar hasta 0.5s si el socket directo aún está terminando de aceptarse
            for _ in range(25):
                if accepted_direct_sock["sock"] is not None:
                    break
                time.sleep(0.02)
            if accepted_direct_sock["sock"] is not None:
                on_cleanup()
                return accepted_direct_sock["sock"], ""
        return vsock, ""

    on_cleanup()
    return None, state["message"] or "El usuario rechazó la solicitud de conexión."
