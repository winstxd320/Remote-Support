import io
import os
import json
import time
import socket
import ctypes
import subprocess
import threading
import logging
import winreg
from typing import List, Dict, Any, Callable, Optional, Tuple

import mss
from PIL import Image
from pynput.mouse import Controller as MouseController, Button
from pynput.keyboard import Controller as KeyboardController, Key

from protocol import (
    MSG_JSON,
    recv_packet,
    send_json,
    send_frame,
)
from cloud_relay import CloudHostRelay

SPECIAL_KEYS = {
    "Key_Backspace": Key.backspace,
    "Key_Tab": Key.tab,
    "Key_Return": Key.enter,
    "Key_Enter": Key.enter,
    "Key_Escape": Key.esc,
    "Key_Space": Key.space,
    "Key_Delete": Key.delete,
    "Key_Up": Key.up,
    "Key_Down": Key.down,
    "Key_Left": Key.left,
    "Key_Right": Key.right,
    "Key_Home": Key.home,
    "Key_End": Key.end,
    "Key_PageUp": Key.page_up,
    "Key_PageDown": Key.page_down,
    "Key_Shift": Key.shift,
    "Key_Control": Key.ctrl,
    "Key_Alt": Key.alt,
    "Key_Meta": Key.cmd,
    "Key_CapsLock": Key.caps_lock,
    "Key_F1": Key.f1,
    "Key_F2": Key.f2,
    "Key_F3": Key.f3,
    "Key_F4": Key.f4,
    "Key_F5": Key.f5,
    "Key_F6": Key.f6,
    "Key_F7": Key.f7,
    "Key_F8": Key.f8,
    "Key_F9": Key.f9,
    "Key_F10": Key.f10,
    "Key_F11": Key.f11,
    "Key_F12": Key.f12,
}

MOUSE_BUTTONS = {
    "left": Button.left,
    "right": Button.right,
    "middle": Button.middle,
}

DISCOVERY_PORT = 5002


def auto_approve_uac_window_if_present() -> bool:
    """
    Si en el equipo remoto está abierta la ventana de UAC / Seguridad de Windows ("Sí / No")
    y el proceso Host tiene privilegios elevados, envía la pulsación Alt+S / Enter hacia la ventana activa.
    """
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return False
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
        cls_name = buf.value
        if "Credential Dialog" in cls_name or "#32770" in cls_name:
            # Enviar Alt+S (Sí en español) o Alt+Y
            VK_MENU = 0x12
            VK_S = 0x53
            KEYEVENTF_KEYUP = 0x0002
            ctypes.windll.user32.keybd_event(VK_MENU, 0, 0, 0)
            ctypes.windll.user32.keybd_event(VK_S, 0, 0, 0)
            ctypes.windll.user32.keybd_event(VK_S, 0, KEYEVENTF_KEYUP, 0)
            ctypes.windll.user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            return True
    except Exception:
        pass
    return False
UAC_REG_PATH = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


def run_command_as_admin(
    command: str,
    admin_user: str = "",
    admin_password: str = "",
    admin_domain: str = ".",
) -> Tuple[bool, str]:
    """
    Ejecuta un programa o comando con privilegios de Administrador desde el Visor Remoto:
    1) Si se proporcionan `admin_user` y `admin_password` (ej. cuando la sesión remota está en una
       cuenta de Usuario Estándar), utiliza la API de Windows `CreateProcessWithLogonW` para iniciar
       el proceso directamente bajo esa cuenta de Administrador sin trabarse en la ventana UAC.
    2) Si no se especifica usuario (ya corre como Administrador), lo lanza con verbo `runas`.
    """
    cmd_str = command.strip() or "cmd.exe"
    if admin_user and admin_password:
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(STARTUPINFOW)
        pi = PROCESS_INFORMATION()
        LOGON_WITH_PROFILE = 0x00000001
        CREATE_DEFAULT_ERROR_MODE = 0x04000000

        user_part = admin_user.strip()
        domain_part = admin_domain.strip() or "."
        if "\\" in user_part:
            domain_part, user_part = user_part.split("\\", 1)

        cmd_buf = ctypes.create_unicode_buffer(cmd_str, 1024)
        ok = ctypes.windll.advapi32.CreateProcessWithLogonW(
            ctypes.c_wchar_p(user_part),
            ctypes.c_wchar_p(domain_part),
            ctypes.c_wchar_p(admin_password),
            LOGON_WITH_PROFILE,
            None,
            cmd_buf,
            CREATE_DEFAULT_ERROR_MODE,
            None,
            None,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if ok:
            ctypes.windll.kernel32.CloseHandle(pi.hProcess)
            ctypes.windll.kernel32.CloseHandle(pi.hThread)
            return True, f"Se inició '{cmd_str}' como Administrador ({user_part})."
        else:
            err_code = ctypes.windll.kernel32.GetLastError()
            return False, f"Error de credenciales o comando (Código Windows {err_code})."
    else:
        try:
            parts = cmd_str.split(" ", 1)
            exe = parts[0]
            params = parts[1] if len(parts) > 1 else ""
            ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
            if ret > 32:
                return True, f"Se ejecutó '{cmd_str}' con permisos de Administrador."
            return False, f"No se pudo elevar '{cmd_str}' (código {ret})."
        except Exception as e:
            return False, str(e)


def set_uac_normal_desktop(enable_normal_desktop: bool, prev_values: Optional[Dict[str, int]] = None) -> Optional[Dict[str, int]]:
    """
    Configura UAC en Windows mientras dura la sesión remota para que SÍ aparezca la ventana
    de credenciales de Administrador (o de Sí/No) en el escritorio normal (`PromptOnSecureDesktop = 0`)
    y pueda visualizarse e interactuar remotamente.
    Al finalizar la sesión remota, restaura los valores originales que tenía el equipo.
    """
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            UAC_REG_PATH,
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        )
        if enable_normal_desktop:
            saved = {}
            for name, default_val in (
                ("PromptOnSecureDesktop", 1),
                ("ConsentPromptBehaviorAdmin", 5),
                ("ConsentPromptBehaviorUser", 3),
            ):
                try:
                    val, _ = winreg.QueryValueEx(key, name)
                    saved[name] = int(val)
                except OSError:
                    saved[name] = default_val

            # 1) Mostrar siempre en el escritorio normal (no en el escritorio oscuro bloqueado)
            winreg.SetValueEx(key, "PromptOnSecureDesktop", 0, winreg.REG_DWORD, 0)
            # 2) Asegurar que sí aparezca el cuadro de diálogo de credenciales / confirmación
            if saved.get("ConsentPromptBehaviorAdmin", 5) == 0:
                winreg.SetValueEx(key, "ConsentPromptBehaviorAdmin", 0, winreg.REG_DWORD, 5)
            # 3) Para usuarios estándar, pedir usuario y contraseña de Administrador en el escritorio normal
            winreg.SetValueEx(key, "ConsentPromptBehaviorUser", 0, winreg.REG_DWORD, 3)

            winreg.CloseKey(key)
            return saved
        else:
            if prev_values:
                for name, val in prev_values.items():
                    try:
                        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(val))
                    except OSError:
                        pass
            winreg.CloseKey(key)
            return None
    except Exception as e:
        logging.debug(f"No se pudo configurar UAC temporal: {e}")
        return None


def get_monitors_info() -> List[Dict[str, Any]]:
    """
    Detecta todos los monitores conectados usando mss.
    """
    monitors_list = []
    with mss.mss() as sct:
        raw_monitors = sct.monitors
        for idx in range(1, len(raw_monitors)):
            m = raw_monitors[idx]
            monitors_list.append(
                {
                    "index": idx,
                    "name": f"Monitor {idx} ({m['width']}x{m['height']})",
                    "left": m["left"],
                    "top": m["top"],
                    "width": m["width"],
                    "height": m["height"],
                    "is_all": False,
                }
            )
        if len(raw_monitors) > 2:
            m0 = raw_monitors[0]
            monitors_list.append(
                {
                    "index": 0,
                    "name": f"Todos los monitores ({m0['width']}x{m0['height']})",
                    "left": m0["left"],
                    "top": m0["top"],
                    "width": m0["width"],
                    "height": m0["height"],
                    "is_all": True,
                }
            )
    return monitors_list


def resolve_host_by_id(target_id: str, timeout: float = 0.8) -> Optional[tuple]:
    """
    Busca en la red local (mediante UDP broadcast) la IP y puerto del equipo que tenga `target_id`.
    """
    clean_target = target_id.replace(" ", "").replace("-", "")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        query = json.dumps({"cmd": "discover_id", "target_id": clean_target}).encode("utf-8")
        sock.sendto(query, ("255.255.255.255", DISCOVERY_PORT))
        sock.sendto(query, ("127.0.0.1", DISCOVERY_PORT))
        while True:
            data, addr = sock.recvfrom(1024)
            resp = json.loads(data.decode("utf-8"))
            if resp.get("cmd") == "discover_response" and resp.get("host_id") == clean_target:
                return addr[0], int(resp.get("port", 5001))
    except Exception:
        return None
    finally:
        sock.close()


class HostSession:
    """
    Maneja una sesión activa donde este equipo actúa como Host (comparte su pantalla,
    detecta monitores en tiempo real y ejecuta los eventos de mouse y teclado del Visor).
    """

    def __init__(
        self,
        sock: socket.socket,
        allow_control: bool = True,
        on_status_change: Optional[Callable[[str], None]] = None,
    ):
        self.sock = sock
        self.on_status_change = on_status_change
        self.running = True
        self.current_monitor_idx = 1
        self.quality = 65
        self.max_width = 1600
        self.fps = 22
        self.allow_control = allow_control

        self.mouse = MouseController()
        self.keyboard = KeyboardController()
        self.send_lock = threading.Lock()
        self.monitors_cache: List[Dict[str, Any]] = []
        self._prev_uac_val: Optional[Dict[str, int]] = None
        self._uac_restored = False

    def start(self):
        # Al iniciar la sesión remota, activar la visualización de avisos de Administrador (UAC Sí/No) en el escritorio normal
        self._prev_uac_val = set_uac_normal_desktop(True)
        threading.Thread(target=self._capture_loop, daemon=True).start()
        threading.Thread(target=self._receive_loop, daemon=True).start()

    def stop(self):
        self.running = False
        if not self._uac_restored:
            self._uac_restored = True
            set_uac_normal_desktop(False, self._prev_uac_val)
        try:
            self.sock.close()
        except OSError:
            pass

    def _safe_send_json(self, data: dict) -> bool:
        with self.send_lock:
            return send_json(self.sock, data)

    def _safe_send_frame(self, monitor_idx: int, w: int, h: int, jpeg_bytes: bytes) -> bool:
        with self.send_lock:
            return send_frame(self.sock, monitor_idx, w, h, jpeg_bytes)

    def _capture_loop(self):
        with mss.mss() as sct:
            last_monitor_check = 0.0
            last_monitors_signature = ""
            is_cloud = getattr(self.sock, "is_cloud_tunnel", False)

            while self.running:
                t_start = time.perf_counter()

                # Si es túnel en la nube, esperar confirmación de flujo para nunca acumular lag
                if is_cloud and hasattr(self.sock, "can_send_frame"):
                    if not self.sock.can_send_frame():
                        time.sleep(0.015)
                        continue

                if t_start - last_monitor_check > 1.5:
                    last_monitor_check = t_start
                    monitors = get_monitors_info()
                    sig = json.dumps(monitors, sort_keys=True)
                    if sig != last_monitors_signature:
                        last_monitors_signature = sig
                        self.monitors_cache = monitors
                        if not self._safe_send_json(
                            {
                                "type": "monitors_info",
                                "monitors": monitors,
                                "active_monitor": self.current_monitor_idx,
                            }
                        ):
                            break

                raw_monitors = sct.monitors
                idx = self.current_monitor_idx
                if idx < 0 or idx >= len(raw_monitors):
                    idx = 1 if len(raw_monitors) > 1 else 0
                    self.current_monitor_idx = idx

                mon_rect = raw_monitors[idx]
                try:
                    sct_img = sct.grab(mon_rect)
                    img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

                    orig_w, orig_h = img.size
                    if orig_w > self.max_width:
                        ratio = self.max_width / float(orig_w)
                        new_w = int(orig_w * ratio)
                        new_h = int(orig_h * ratio)
                        img = img.resize((new_w, new_h), Image.Resampling.BILINEAR)

                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=self.quality, optimize=False)
                    jpeg_data = buf.getvalue()

                    if not self._safe_send_frame(idx, orig_w, orig_h, jpeg_data):
                        break
                except Exception as e:
                    logging.debug(f"Error capturando pantalla: {e}")

                elapsed = time.perf_counter() - t_start
                sleep_time = max(0.005, (1.0 / self.fps) - elapsed)
                time.sleep(sleep_time)

        self.stop()
        if self.on_status_change:
            self.on_status_change("En línea - Listo para recibir conexiones")

    def _receive_loop(self):
        while self.running:
            pkt = recv_packet(self.sock)
            if not pkt:
                break
            msg_type, payload = pkt
            if msg_type == MSG_JSON:
                try:
                    event = json.loads(payload.decode("utf-8"))
                    self._handle_event(event)
                except Exception as e:
                    logging.debug(f"Error procesando evento remoto: {e}")

        self.stop()

    def _get_active_monitor_bounds(self) -> Dict[str, int]:
        for m in self.monitors_cache:
            if m["index"] == self.current_monitor_idx:
                return m
        if self.monitors_cache:
            return self.monitors_cache[0]
        return {"left": 0, "top": 0, "width": 1920, "height": 1080}

    def _handle_event(self, event: Dict[str, Any]):
        etype = event.get("type")

        if etype == "select_monitor":
            new_idx = int(event.get("monitor_index", 1))
            self.current_monitor_idx = new_idx
            return

        if etype == "set_quality":
            self.quality = max(20, min(95, int(event.get("quality", 65))))
            self.fps = max(5, min(60, int(event.get("fps", 22))))
            return

        if not self.allow_control:
            return

        bounds = self._get_active_monitor_bounds()
        if etype in ("mouse_move", "mouse_press", "mouse_release", "mouse_scroll"):
            nx = max(0.0, min(1.0, float(event.get("nx", 0.0))))
            ny = max(0.0, min(1.0, float(event.get("ny", 0.0))))
            abs_x = int(bounds["left"] + nx * bounds["width"])
            abs_y = int(bounds["top"] + ny * bounds["height"])

            try:
                if etype == "mouse_move":
                    ctypes.windll.user32.SetCursorPos(abs_x, abs_y)
                    self.mouse.position = (abs_x, abs_y)
                elif etype == "mouse_press":
                    ctypes.windll.user32.SetCursorPos(abs_x, abs_y)
                    self.mouse.position = (abs_x, abs_y)
                    btn = MOUSE_BUTTONS.get(event.get("button", "left"), Button.left)
                    self.mouse.press(btn)
                elif etype == "mouse_release":
                    ctypes.windll.user32.SetCursorPos(abs_x, abs_y)
                    self.mouse.position = (abs_x, abs_y)
                    btn = MOUSE_BUTTONS.get(event.get("button", "left"), Button.left)
                    self.mouse.release(btn)
                elif etype == "mouse_scroll":
                    ctypes.windll.user32.SetCursorPos(abs_x, abs_y)
                    self.mouse.position = (abs_x, abs_y)
                    dx = int(event.get("dx", 0))
                    dy = int(event.get("dy", 0))
                    self.mouse.scroll(dx, dy)
            except Exception:
                pass

        elif etype in ("key_press", "key_release"):
            key_name = event.get("key_name", "")
            text = event.get("text", "")
            target_key = SPECIAL_KEYS.get(key_name)
            if target_key is None and text and len(text) == 1:
                target_key = text
            if target_key is not None:
                try:
                    if etype == "key_press":
                        self.keyboard.press(target_key)
                    else:
                        self.keyboard.release(target_key)
                except Exception:
                    pass


class HostAgent:
    """
    Servicio en segundo plano estilo AnyDesk:
    1) Se registra automáticamente en el Servidor Relay con su ID de 9 dígitos (sin contraseña).
    2) Escucha también conexiones directas por LAN y descubrimiento UDP.
    3) Cuando alguien intenta conectarse, invoca `ask_user_approval(viewer_info, decision_callback)`
       para que el usuario en pantalla haga clic en "Aceptar" o "Rechazar".
    """

    def __init__(
        self,
        host_id: str,
        direct_port: int = 5001,
        relay_host: str = "127.0.0.1",
        relay_port: int = 5000,
        on_status: Optional[Callable[[str], None]] = None,
        on_connection_request: Optional[Callable[[dict, Callable[[bool], None]], None]] = None,
    ):
        self.host_id = host_id.replace(" ", "").replace("-", "")
        self.direct_port = direct_port
        self.relay_host = relay_host
        self.relay_port = relay_port
        self.on_status = on_status
        self.on_connection_request = on_connection_request
        self.allow_control = True

        self.running = False
        self.direct_sock: Optional[socket.socket] = None
        self.relay_control_sock: Optional[socket.socket] = None
        self.cloud_relay: Optional[CloudHostRelay] = None
        self.active_sessions: List[HostSession] = []

    def start(self):
        self.running = True
        # Usamos exclusivamente conexiones salientes hacia el Cloud Relay:
        # Esto funciona tanto en la misma red como entre distintas ciudades y evita por completo
        # abrir puertos entrantes (0.0.0.0), por lo que el Firewall de Windows nunca muestra alertas.
        self.cloud_relay = CloudHostRelay(
            host_id=self.host_id,
            on_status=self._notify,
            on_connection_request=self.on_connection_request,
            on_session_ready=self._on_cloud_session_ready,
        )
        self.cloud_relay.start()

    def _on_cloud_session_ready(self, session_sock, viewer_info: dict):
        is_cloud = getattr(session_sock, "is_cloud_tunnel", False)
        mode_txt = "Cloud" if is_cloud else "Velocidad Directa"
        self._notify(f"Sesión activa ({mode_txt}) con {viewer_info.get('viewer_name', 'Remoto')}")
        session = HostSession(
            session_sock,
            allow_control=self.allow_control,
            on_status_change=self._notify,
        )
        if is_cloud:
            # En modo Cloud entre ciudades: imagen compacta y fluida para respuesta inmediata sin lag
            session.max_width = 1024
            session.quality = 45
            session.fps = 15
        else:
            # En modo Directo: alta definición y máxima fluidez (30 FPS)
            session.max_width = 1600
            session.quality = 68
            session.fps = 28
        self.active_sessions.append(session)
        session.start()

    def set_allow_control(self, allow: bool):
        self.allow_control = allow
        for s in self.active_sessions:
            s.allow_control = allow

    def _notify(self, msg: str):
        if self.on_status:
            self.on_status(msg)

    def _discovery_listen_loop(self):
        while self.running:
            try:
                udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                udp_sock.bind(("0.0.0.0", DISCOVERY_PORT))
                while self.running:
                    data, addr = udp_sock.recvfrom(1024)
                    try:
                        msg = json.loads(data.decode("utf-8"))
                        if msg.get("cmd") == "discover_id" and msg.get("target_id") == self.host_id:
                            resp = json.dumps(
                                {
                                    "cmd": "discover_response",
                                    "host_id": self.host_id,
                                    "port": self.direct_port,
                                }
                            ).encode("utf-8")
                            udp_sock.sendto(resp, addr)
                    except Exception:
                        pass
            except OSError:
                time.sleep(2)

    def _direct_listen_loop(self):
        while self.running:
            try:
                self.direct_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.direct_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.direct_sock.bind(("0.0.0.0", self.direct_port))
                self.direct_sock.listen(5)
                while self.running:
                    client_sock, addr = self.direct_sock.accept()
                    client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    threading.Thread(
                        target=self._handle_direct_viewer,
                        args=(client_sock, addr),
                        daemon=True,
                    ).start()
            except OSError:
                time.sleep(2)

    def _handle_direct_viewer(self, sock: socket.socket, addr):
        pkt = recv_packet(sock)
        if not pkt or pkt[0] != MSG_JSON:
            sock.close()
            return

        try:
            req_data = json.loads(pkt[1].decode("utf-8"))
        except Exception:
            req_data = {}

        send_json(
            sock,
            {
                "status": "waiting_approval",
                "message": "Esperando que el usuario acepte la solicitud...",
            },
        )

        viewer_info = {
            "viewer_name": req_data.get("viewer_name", "Equipo Remoto"),
            "viewer_id": req_data.get("viewer_id", addr[0]),
            "viewer_addr": addr[0],
        }

        def on_decision(accepted: bool):
            if accepted:
                send_json(sock, {"status": "connected"})
                self._notify(f"Sesión activa con {viewer_info['viewer_name']} ({addr[0]})")
                session = HostSession(
                    sock,
                    allow_control=self.allow_control,
                    on_status_change=self._notify,
                )
                self.active_sessions.append(session)
                session.start()
            else:
                send_json(
                    sock,
                    {"status": "rejected", "message": "El usuario rechazó la solicitud de conexión."},
                )
                try:
                    sock.close()
                except OSError:
                    pass

        if self.on_connection_request:
            self.on_connection_request(viewer_info, on_decision)
        else:
            on_decision(True)

    def _relay_control_loop(self):
        while self.running:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect((self.relay_host, self.relay_port))
                sock.settimeout(None)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.relay_control_sock = sock

                send_json(
                    sock,
                    {
                        "cmd": "register_host",
                        "host_id": self.host_id,
                    },
                )
                pkt = recv_packet(sock)
                if not pkt or pkt[0] != MSG_JSON:
                    sock.close()
                    time.sleep(3)
                    continue

                resp = json.loads(pkt[1].decode("utf-8"))
                if resp.get("status") == "registered":
                    self._notify("En línea - Listo para recibir conexiones")

                while self.running:
                    pkt = recv_packet(sock)
                    if not pkt:
                        break
                    if pkt[0] == MSG_JSON:
                        msg = json.loads(pkt[1].decode("utf-8"))
                        if msg.get("cmd") == "incoming_session":
                            session_id = msg.get("session_id", "")
                            viewer_info = {
                                "viewer_name": msg.get("viewer_name", "Equipo Remoto"),
                                "viewer_id": msg.get("viewer_id", ""),
                                "viewer_addr": msg.get("viewer_addr", "Internet"),
                            }

                            def make_relay_decision(sid=session_id, vinfo=viewer_info):
                                def decision_cb(accepted: bool):
                                    threading.Thread(
                                        target=self._respond_relay_session,
                                        args=(sid, vinfo, accepted),
                                        daemon=True,
                                    ).start()

                                return decision_cb

                            if self.on_connection_request:
                                self.on_connection_request(viewer_info, make_relay_decision())
                            else:
                                make_relay_decision()(True)
            except OSError:
                self._notify("En línea (Red Local activa)")
            finally:
                if self.relay_control_sock:
                    try:
                        self.relay_control_sock.close()
                    except OSError:
                        pass
                    self.relay_control_sock = None
            time.sleep(3)

    def _respond_relay_session(self, session_id: str, viewer_info: dict, accepted: bool):
        try:
            s_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s_sock.settimeout(5.0)
            s_sock.connect((self.relay_host, self.relay_port))
            s_sock.settimeout(None)
            s_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            if not accepted:
                send_json(s_sock, {"cmd": "reject_session", "session_id": session_id})
                s_sock.close()
                return

            send_json(s_sock, {"cmd": "accept_session", "session_id": session_id})
            pkt = recv_packet(s_sock)
            if not pkt or pkt[0] != MSG_JSON:
                s_sock.close()
                return
            resp = json.loads(pkt[1].decode("utf-8"))
            if resp.get("status") == "connected":
                self._notify(f"Sesión activa con {viewer_info.get('viewer_name', 'Remoto')}")
                session = HostSession(
                    s_sock,
                    allow_control=self.allow_control,
                    on_status_change=self._notify,
                )
                self.active_sessions.append(session)
                session.start()
        except Exception:
            pass
