import socket
import threading
import json
import logging
import argparse
from typing import Dict
from protocol import recv_packet, send_json, MSG_JSON

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RELAY] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)


class HostEntry:
    def __init__(self, host_id: str, control_sock: socket.socket, addr):
        self.host_id = host_id
        self.control_sock = control_sock
        self.addr = addr


class RelayServer:
    """
    Servidor Intermediario (Relay) estilo AnyDesk:
    - Los equipos se registran silenciosamente con su ID de 9 dígitos (sin contraseña).
    - Cuando un Visor solicita conectarse a un ID, el Relay le avisa al Host para que muestre
      la ventana de "Aceptar / Rechazar" al usuario.
    - Si el usuario da clic en "Aceptar", el Relay enlaza ambos sockets en tiempo real.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 5000):
        self.host = host
        self.port = port
        self.hosts: Dict[str, HostEntry] = {}
        self.pending_viewers: Dict[str, socket.socket] = {}
        self.lock = threading.Lock()
        self.running = False
        self.server_sock = None

    def start(self, in_background: bool = False):
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.host, self.port))
        self.server_sock.listen(50)
        self.running = True
        logging.info(f"Servidor Relay activo en {self.host}:{self.port}")

        if in_background:
            t = threading.Thread(target=self._accept_loop, daemon=True)
            t.start()
        else:
            self._accept_loop()

    def stop(self):
        self.running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except OSError:
                pass

    def _accept_loop(self):
        while self.running:
            try:
                client_sock, addr = self.server_sock.accept()
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                threading.Thread(
                    target=self._handle_client, args=(client_sock, addr), daemon=True
                ).start()
            except OSError:
                break

    def _handle_client(self, sock: socket.socket, addr):
        pkt = recv_packet(sock)
        if not pkt or pkt[0] != MSG_JSON:
            sock.close()
            return

        try:
            data = json.loads(pkt[1].decode("utf-8"))
        except Exception:
            sock.close()
            return

        cmd = data.get("cmd")
        if cmd == "register_host":
            self._handle_register_host(sock, addr, data)
        elif cmd == "connect_viewer":
            self._handle_connect_viewer(sock, addr, data)
        elif cmd == "accept_session":
            self._handle_accept_session(sock, data)
        elif cmd == "reject_session":
            self._handle_reject_session(sock, data)
        else:
            sock.close()

    def _handle_register_host(self, sock: socket.socket, addr, data: dict):
        host_id = str(data.get("host_id", "")).strip()
        if not host_id:
            send_json(sock, {"status": "error", "message": "ID inválido"})
            sock.close()
            return

        with self.lock:
            old = self.hosts.get(host_id)
            if old:
                try:
                    old.control_sock.close()
                except OSError:
                    pass
            entry = HostEntry(host_id, sock, addr)
            self.hosts[host_id] = entry

        send_json(sock, {"status": "registered", "host_id": host_id})

        while self.running:
            pkt = recv_packet(sock)
            if not pkt:
                break

        with self.lock:
            if self.hosts.get(host_id) is entry:
                del self.hosts[host_id]
        try:
            sock.close()
        except OSError:
            pass

    def _handle_connect_viewer(self, viewer_sock: socket.socket, addr, data: dict):
        target_id = str(data.get("target_id", "")).strip()
        viewer_name = str(data.get("viewer_name", "Soporte Técnico")).strip()
        viewer_id = str(data.get("viewer_id", "")).strip()
        session_id = f"{target_id}_{addr[0]}_{addr[1]}"

        with self.lock:
            host_entry = self.hosts.get(target_id)

        if not host_entry:
            send_json(
                viewer_sock,
                {"status": "error", "message": f"El equipo con ID {target_id} no está en línea."},
            )
            viewer_sock.close()
            return

        with self.lock:
            self.pending_viewers[session_id] = viewer_sock

        # Avisar al Visor que estamos esperando a que el usuario remoto presione "Aceptar"
        send_json(
            viewer_sock,
            {"status": "waiting_approval", "message": "Esperando que el usuario acepte la solicitud..."},
        )

        # Enviar notificación de solicitud entrante al Host
        ok = send_json(
            host_entry.control_sock,
            {
                "cmd": "incoming_session",
                "session_id": session_id,
                "viewer_name": viewer_name,
                "viewer_id": viewer_id,
                "viewer_addr": f"{addr[0]}",
            },
        )
        if not ok:
            with self.lock:
                self.pending_viewers.pop(session_id, None)
            send_json(
                viewer_sock,
                {"status": "error", "message": "No se pudo contactar al equipo remoto."},
            )
            viewer_sock.close()

    def _handle_accept_session(self, host_session_sock: socket.socket, data: dict):
        session_id = str(data.get("session_id", ""))
        with self.lock:
            viewer_sock = self.pending_viewers.pop(session_id, None)

        if not viewer_sock:
            host_session_sock.close()
            return

        send_json(viewer_sock, {"status": "connected"})
        send_json(host_session_sock, {"status": "connected"})

        t1 = threading.Thread(
            target=self._bridge_stream,
            args=(host_session_sock, viewer_sock),
            daemon=True,
        )
        t2 = threading.Thread(
            target=self._bridge_stream,
            args=(viewer_sock, host_session_sock),
            daemon=True,
        )
        t1.start()
        t2.start()

    def _handle_reject_session(self, host_temp_sock: socket.socket, data: dict):
        session_id = str(data.get("session_id", ""))
        with self.lock:
            viewer_sock = self.pending_viewers.pop(session_id, None)
        if viewer_sock:
            send_json(
                viewer_sock,
                {"status": "rejected", "message": "El usuario rechazó la solicitud de conexión."},
            )
            try:
                viewer_sock.close()
            except OSError:
                pass
        try:
            host_temp_sock.close()
        except OSError:
            pass

    @staticmethod
    def _bridge_stream(src: socket.socket, dst: socket.socket):
        try:
            while True:
                chunk = src.recv(65536)
                if not chunk:
                    break
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                src.close()
            except OSError:
                pass
            try:
                dst.close()
            except OSError:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Servidor Relay para Remote Support")
    parser.add_argument("--host", default="0.0.0.0", help="IP de escucha (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="Puerto TCP (default: 5000)")
    args = parser.parse_args()

    server = RelayServer(args.host, args.port)
    try:
        server.start(in_background=False)
    except KeyboardInterrupt:
        logging.info("Servidor Relay detenido.")
