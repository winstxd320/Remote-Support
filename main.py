import sys
import json
import random
import socket
import platform
import ctypes
import threading
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QCheckBox,
    QMessageBox,
    QDialog,
)

from protocol import MSG_JSON, recv_packet, send_json
from relay_server import RelayServer
from host_service import HostAgent, get_monitors_info, resolve_host_by_id
from cloud_relay import connect_via_cloud_relay
from viewer_window import ViewerWindow

def get_resource_path(filename: str) -> Path:
    """Devuelve la ruta de recursos empaquetados dentro del .exe (PyInstaller) o en el directorio actual."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / filename
    return Path(__file__).parent / filename


def get_app_dir() -> Path:
    """Devuelve la carpeta junto al .exe (o script) para guardar la configuración persistente."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


CONFIG_FILE = get_app_dir() / "remote_support_config.json"
ICON_FILE = get_resource_path("icono.png")


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def load_or_create_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if "host_id" in data:
                return data
        except Exception:
            pass

    # Generar un ID único persistente de 9 dígitos estilo AnyDesk
    digits = "".join(str(random.randint(0, 9)) for _ in range(9))
    formatted_id = f"{digits[:3]} {digits[3:6]} {digits[6:]}"
    data = {
        "host_id": formatted_id,
        "relay_host": "127.0.0.1",
        "relay_port": 5000,
        "direct_port": 5001,
    }
    try:
        CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass
    return data


class IncomingRequestDialog(QDialog):
    """
    Ventana emergente estilo AnyDesk cuando alguien solicita conectarse a esta computadora:
    Permite al usuario hacer clic en "✅ Aceptar" o "❌ Rechazar".
    """

    def __init__(self, viewer_info: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Remote Support - Solicitud de Conexión Entrante")
        self.setFixedSize(440, 230)
        if ICON_FILE.exists():
            self.setWindowIcon(QIcon(str(ICON_FILE)))
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
        )
        self.setStyleSheet(
            """
            QDialog {
                background-color: #1e1e2e;
                border: 2px solid #89b4fa;
            }
            QLabel {
                color: #cdd6f4;
                font-family: 'Segoe UI', sans-serif;
            }
            QPushButton {
                border-radius: 6px;
                padding: 10px 20px;
                font-size: 14px;
                font-weight: bold;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        title = QLabel("🔔 Solicitud de Asistencia Remota")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #89b4fa;")
        layout.addWidget(title)

        v_name = viewer_info.get("viewer_name", "Equipo Remoto")
        v_id = viewer_info.get("viewer_id", "")
        v_addr = viewer_info.get("viewer_addr", "")

        desc = QLabel(
            f"<b>{v_name}</b> (ID: {v_id} / {v_addr}) solicita ver tu pantalla y controlar tu equipo.<br><br>"
            "¿Deseas aceptar la conexión?"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("font-size: 14px; color: #f5e0dc;")
        layout.addWidget(desc)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_reject = QPushButton("Rechazar")
        btn_reject.setStyleSheet("background-color: #f38ba8; color: #11111b;")
        btn_reject.clicked.connect(self.reject)

        btn_accept = QPushButton("Aceptar Conexión")
        btn_accept.setStyleSheet("background-color: #a6e3a1; color: #11111b;")
        btn_accept.clicked.connect(self.accept)

        btn_row.addWidget(btn_reject)
        btn_row.addWidget(btn_accept)
        layout.addLayout(btn_row)


class CreditsDialog(QDialog):
    """
    Ventana de Créditos y Atribuciones de recursos gratuitos utilizados en la aplicación.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Créditos y Atribuciones - Remote Support")
        self.setFixedSize(480, 290)
        if ICON_FILE.exists():
            self.setWindowIcon(QIcon(str(ICON_FILE)))

        self.setStyleSheet(
            """
            QDialog {
                background-color: #1e1e2e;
            }
            QLabel {
                color: #cdd6f4;
                font-family: 'Segoe UI', sans-serif;
            }
            QFrame#CreditBox {
                background-color: #181825;
                border: 1px solid #313244;
                border-radius: 8px;
            }
            QPushButton {
                background-color: #89b4fa;
                color: #11111b;
                font-weight: bold;
                border-radius: 6px;
                padding: 8px 20px;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #b4befe;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)

        header = QHBoxLayout()
        if ICON_FILE.exists():
            icon_lbl = QLabel()
            pix = QPixmap(str(ICON_FILE)).scaled(
                42,
                42,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            icon_lbl.setPixmap(pix)
            header.addWidget(icon_lbl)

        title = QLabel("Créditos y Atribuciones")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #89b4fa;")
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)

        subtitle = QLabel(
            "Agradecemos a los creadores y comunidades cuyos recursos gratuitos hicieron posible esta aplicación:"
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("font-size: 13px; color: #a6adc8;")
        layout.addWidget(subtitle)

        credit_box = QFrame()
        credit_box.setObjectName("CreditBox")
        cb_layout = QVBoxLayout(credit_box)
        cb_layout.setContentsMargins(14, 12, 14, 12)
        cb_layout.setSpacing(8)

        lbl_icon_credit = QLabel(
            "🎨 <b>Icono de la aplicación:</b><br>"
            'Diseñado por <a href="https://www.flaticon.es/autores/smashicons" style="color: #89b4fa; text-decoration: none;"><b>Smashicons</b></a> '
            'desde <a href="https://www.flaticon.es/" style="color: #89b4fa; text-decoration: none;">Flaticon</a><br>'
            '<span style="color: #9399b2; font-size: 11px;">https://www.flaticon.es/autores/smashicons</span>'
        )
        lbl_icon_credit.setOpenExternalLinks(True)
        lbl_icon_credit.setWordWrap(True)
        lbl_icon_credit.setStyleSheet("font-size: 13px;")
        cb_layout.addWidget(lbl_icon_credit)

        lbl_libs = QLabel(
            "⚙️ <b>Librerías Open Source:</b> PyQt6, MSS, Pillow, Pynput y Paho-MQTT."
        )
        lbl_libs.setWordWrap(True)
        lbl_libs.setStyleSheet("font-size: 12px; color: #a6adc8;")
        cb_layout.addWidget(lbl_libs)

        layout.addWidget(credit_box)
        layout.addStretch()

        btn_close = QPushButton("Cerrar")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close, alignment=Qt.AlignmentFlag.AlignRight)


class SignalBridge(QObject):
    status_changed = pyqtSignal(str)
    connection_requested = pyqtSignal(dict, object)
    connect_result = pyqtSignal(bool, object, str, str)


class RemoteSupportMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Remote Support")
        self.setMinimumSize(820, 460)
        if ICON_FILE.exists():
            self.setWindowIcon(QIcon(str(ICON_FILE)))

        self.local_ip = get_local_ip()
        self.config = load_or_create_config()
        self.embedded_relay: Optional[RelayServer] = None
        self.viewer_windows = []

        self.bridge = SignalBridge()
        self.bridge.status_changed.connect(self._update_status_label)
        self.bridge.connection_requested.connect(self._show_approval_dialog)
        self.bridge.connect_result.connect(self._on_connect_finished)

        self._setup_ui()
        self._start_services()

        self.monitor_timer = QTimer(self)
        self.monitor_timer.timeout.connect(self._refresh_local_monitors)
        self.monitor_timer.start(2000)
        self._refresh_local_monitors()

    def _setup_ui(self):
        self.setStyleSheet(
            """
            QMainWindow {
                background-color: #11111b;
            }
            QFrame#Card {
                background-color: #1e1e2e;
                border: 1px solid #313244;
                border-radius: 12px;
            }
            QLabel {
                color: #cdd6f4;
                font-family: 'Segoe UI', sans-serif;
            }
            QLineEdit {
                background-color: #181825;
                border: 1px solid #45475a;
                border-radius: 6px;
                padding: 10px 12px;
                color: #f5e0dc;
                font-size: 18px;
            }
            QLineEdit:focus {
                border: 1px solid #89b4fa;
            }
            QPushButton {
                background-color: #89b4fa;
                color: #11111b;
                font-weight: bold;
                border-radius: 6px;
                padding: 12px 18px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #b4befe;
            }
            QPushButton:disabled {
                background-color: #45475a;
                color: #9399b2;
            }
            QCheckBox {
                color: #cdd6f4;
                font-size: 13px;
            }
            """
        )

        root = QWidget()
        self.setCentralWidget(root)
        main_layout = QVBoxLayout(root)
        main_layout.setContentsMargins(24, 24, 24, 16)
        main_layout.setSpacing(18)

        # Cabecera
        header_layout = QHBoxLayout()
        if ICON_FILE.exists():
            logo_lbl = QLabel()
            pix = QPixmap(str(ICON_FILE)).scaled(
                34,
                34,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            logo_lbl.setPixmap(pix)
            header_layout.addWidget(logo_lbl)

        title = QLabel("Remote Support")
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #89b4fa;")
        subtitle = QLabel("Asistencia Remota Instantánea y Multi-Monitor")
        subtitle.setStyleSheet("font-size: 13px; color: #a6adc8; margin-left: 10px;")
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        header_layout.addStretch()

        self.lbl_monitors_local = QLabel("Detectando monitores...")
        self.lbl_monitors_local.setStyleSheet(
            "background-color: #313244; padding: 6px 12px; border-radius: 6px; font-weight: bold; color: #a6e3a1;"
        )
        header_layout.addWidget(self.lbl_monitors_local)

        btn_credits = QPushButton("ℹ️ Créditos")
        btn_credits.setStyleSheet(
            "background-color: #313244; color: #cdd6f4; padding: 6px 12px; font-size: 12px; border-radius: 6px;"
        )
        btn_credits.clicked.connect(self._show_credits_dialog)
        header_layout.addWidget(btn_credits)

        main_layout.addLayout(header_layout)

        # Contenedor de 2 columnas
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(20)

        # --- TARJETA IZQUIERDA: ESTE PUESTO DE TRABAJO ---
        left_card = QFrame()
        left_card.setObjectName("Card")
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(22, 22, 22, 22)
        left_layout.setSpacing(14)

        lbl_host_title = QLabel("Este Puesto de Trabajo")
        lbl_host_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #f5e0dc;")
        left_layout.addWidget(lbl_host_title)

        lbl_id_cap = QLabel("Tu Dirección ID:")
        lbl_id_cap.setStyleSheet("color: #a6adc8; font-size: 13px;")
        left_layout.addWidget(lbl_id_cap)

        self.lbl_my_id = QLabel(self.config["host_id"])
        self.lbl_my_id.setStyleSheet(
            "font-size: 32px; font-weight: bold; color: #89b4fa; background-color: #181825; padding: 14px; border-radius: 8px;"
        )
        self.lbl_my_id.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_my_id.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        left_layout.addWidget(self.lbl_my_id)

        self.chk_allow_control = QCheckBox("Permitir control de mi mouse y teclado al aceptar")
        self.chk_allow_control.setChecked(True)
        self.chk_allow_control.toggled.connect(self._on_allow_control_toggled)
        left_layout.addWidget(self.chk_allow_control)

        self.lbl_local_monitors_detail = QLabel("")
        self.lbl_local_monitors_detail.setStyleSheet("color: #9399b2; font-size: 12px;")
        self.lbl_local_monitors_detail.setWordWrap(True)
        left_layout.addWidget(self.lbl_local_monitors_detail)

        left_layout.addStretch()
        cards_layout.addWidget(left_card, stretch=1)

        # --- TARJETA DERECHA: OTRO PUESTO DE TRABAJO ---
        right_card = QFrame()
        right_card.setObjectName("Card")
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(22, 22, 22, 22)
        right_layout.setSpacing(16)

        lbl_viewer_title = QLabel("Otro Puesto de Trabajo")
        lbl_viewer_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #f5e0dc;")
        right_layout.addWidget(lbl_viewer_title)

        lbl_target_cap = QLabel("Ingresa el ID de la computadora remota:")
        lbl_target_cap.setStyleSheet("color: #a6adc8; font-size: 13px;")
        right_layout.addWidget(lbl_target_cap)

        self.input_target = QLineEdit()
        self.input_target.setPlaceholderText("Ej: 482 910 331")
        self.input_target.returnPressed.connect(self._connect_to_remote)
        right_layout.addWidget(self.input_target)

        self.btn_connect = QPushButton("Conectar")
        self.btn_connect.setStyleSheet("font-size: 16px; padding: 14px;")
        self.btn_connect.clicked.connect(self._connect_to_remote)
        right_layout.addWidget(self.btn_connect)

        lbl_hint = QLabel(
            "Al presionar Conectar, aparecerá un aviso en la pantalla del otro usuario para que haga clic en 'Aceptar Conexión'."
        )
        lbl_hint.setStyleSheet("color: #7f849c; font-size: 12px;")
        lbl_hint.setWordWrap(True)
        right_layout.addWidget(lbl_hint)

        right_layout.addStretch()
        cards_layout.addWidget(right_card, stretch=1)

        main_layout.addLayout(cards_layout, stretch=1)

        # Barra inferior (Estado + Atribución rápida a Smashicons)
        footer_layout = QHBoxLayout()
        self.lbl_status = QLabel("Iniciando Remote Support...")
        self.lbl_status.setStyleSheet("color: #a6adc8; font-size: 12px;")
        footer_layout.addWidget(self.lbl_status)

        footer_layout.addStretch()

        lbl_credit_footer = QLabel(
            'Icono por <a href="https://www.flaticon.es/autores/smashicons" style="color: #89b4fa; text-decoration: none;">Smashicons</a> (Flaticon)'
        )
        lbl_credit_footer.setOpenExternalLinks(True)
        lbl_credit_footer.setStyleSheet("color: #7f849c; font-size: 12px;")
        footer_layout.addWidget(lbl_credit_footer)

        main_layout.addLayout(footer_layout)

    def _start_services(self):
        # Iniciar el Agente Host usando únicamente conexiones salientes Cloud (no abre puertos locales,
        # por lo que Windows Firewall nunca muestra ventana de alerta al abrir el programa)
        clean_id = self.config["host_id"].replace(" ", "")
        self.host_agent = HostAgent(
            host_id=clean_id,
            direct_port=int(self.config.get("direct_port", 5001)),
            relay_host=self.config.get("relay_host", "127.0.0.1"),
            relay_port=int(self.config.get("relay_port", 5000)),
            on_status=lambda msg: self.bridge.status_changed.emit(msg),
            on_connection_request=lambda vinfo, cb: self.bridge.connection_requested.emit(vinfo, cb),
        )
        self.host_agent.start()

    def _show_approval_dialog(self, viewer_info: dict, decision_cb):
        dlg = IncomingRequestDialog(viewer_info, self)
        result = dlg.exec()
        accepted = result == int(QDialog.DialogCode.Accepted)
        decision_cb(accepted)

    def _show_credits_dialog(self):
        dlg = CreditsDialog(self)
        dlg.exec()

    def _refresh_local_monitors(self):
        monitors = get_monitors_info()
        physical = [m for m in monitors if not m.get("is_all", False)]
        count = len(physical)
        if count > 1:
            self.lbl_monitors_local.setText(f"🟢 {count} Monitores Conectados")
        else:
            self.lbl_monitors_local.setText("🖥️ 1 Monitor Conectado")

        details = "Pantallas detectadas en este equipo:\n" + "\n".join(
            f" • {m['name']} [pos: {m['left']},{m['top']}]" for m in physical
        )
        self.lbl_local_monitors_detail.setText(details)

    def _on_allow_control_toggled(self, checked: bool):
        if hasattr(self, "host_agent"):
            self.host_agent.set_allow_control(checked)

    def _update_status_label(self, text: str):
        self.lbl_status.setText(f"Estado: {text}")

    def _connect_to_remote(self):
        raw_target = self.input_target.text().strip()
        if not raw_target:
            QMessageBox.warning(self, "ID requerido", "Ingresa el ID de la computadora a la que deseas conectarte.")
            return

        self.btn_connect.setEnabled(False)
        self.btn_connect.setText("Esperando que el usuario acepte...")
        self.lbl_status.setText(f"Estado: Enviando solicitud a {raw_target}...")

        threading.Thread(
            target=self._connect_worker,
            args=(raw_target,),
            daemon=True,
        ).start()

    def _connect_worker(self, raw_target: str):
        clean_digits = raw_target.replace(" ", "").replace("-", "")
        my_name = platform.node() or "Soporte"
        my_id = self.config["host_id"]

        # Conectar mediante el Servidor Cloud Relay (conexión 100% saliente, sin alertas de Firewall)
        vsock, cloud_err = connect_via_cloud_relay(
            target_id=clean_digits,
            viewer_name=my_name,
            viewer_id=my_id,
        )
        if vsock:
            self.bridge.connect_result.emit(True, vsock, raw_target, "")
        else:
            self.bridge.connect_result.emit(False, None, raw_target, cloud_err)

    def _on_connect_finished(self, success: bool, sock_obj, raw_target: str, error_msg: str):
        self.btn_connect.setEnabled(True)
        self.btn_connect.setText("Conectar")
        if success and sock_obj:
            self.lbl_status.setText(f"Estado: Conectado a {raw_target}")
            viewer = ViewerWindow(sock_obj, raw_target)
            self.viewer_windows.append(viewer)
            viewer.show()
        else:
            self.lbl_status.setText("Estado: En línea - Listo para recibir conexiones")
            QMessageBox.warning(self, "Conexión no establecida", error_msg)


def ensure_admin_privileges():
    """
    Si el programa no se abrió como Administrador, solicita elevación automáticamente
    para poder visualizar y controlar ventanas de Administrador y avisos UAC (Sí/No).
    """
    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        is_admin = False

    if not is_admin and not getattr(sys, "frozen", False):
        try:
            params = " ".join(f'"{arg}"' for arg in sys.argv)
            ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
            sys.exit(0)
        except Exception:
            pass


def main():
    ensure_admin_privileges()

    # Establecer AppUserModelID en Windows para que la barra de tareas muestre icono.png en lugar del icono de Python
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ton.remotesupport.app.v1")
    except Exception:
        pass

    app = QApplication(sys.argv)
    if ICON_FILE.exists():
        app.setWindowIcon(QIcon(str(ICON_FILE)))
    window = RemoteSupportMainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
