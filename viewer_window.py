import ctypes
import sys
import json
import struct
import socket
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QPoint
from PyQt6.QtGui import QImage, QPixmap, QPainter, QColor, QIcon, QMouseEvent, QWheelEvent, QKeyEvent
from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QComboBox,
    QPushButton,
    QCheckBox,
    QSizePolicy,
    QMessageBox,
)

from protocol import MSG_JSON, MSG_FRAME, recv_packet, send_json


class ViewerReceiverThread(QThread):
    frame_received = pyqtSignal(int, int, int, bytes)  # monitor_idx, orig_w, orig_h, jpeg_bytes
    monitors_updated = pyqtSignal(list, int)  # monitors_list, active_idx
    disconnected = pyqtSignal(str)

    def __init__(self, sock: socket.socket):
        super().__init__()
        self.sock = sock
        self.running = True

    def run(self):
        while self.running:
            pkt = recv_packet(self.sock)
            if not pkt:
                break
            msg_type, payload = pkt
            if msg_type == MSG_FRAME and len(payload) > 5:
                mon_idx, orig_w, orig_h = struct.unpack("!BHH", payload[:5])
                jpeg_bytes = payload[5:]
                self.frame_received.emit(mon_idx, orig_w, orig_h, jpeg_bytes)
            elif msg_type == MSG_JSON:
                try:
                    data = json.loads(payload.decode("utf-8"))
                    if data.get("type") == "monitors_info":
                        self.monitors_updated.emit(
                            data.get("monitors", []),
                            int(data.get("active_monitor", 1)),
                        )
                except Exception:
                    pass

        self.running = False
        self.disconnected.emit("Se cerró la conexión con el equipo remoto.")

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass


class RemoteCanvas(QWidget):
    """
    Lienzo interactivo que dibuja el video remoto manteniendo la relación de aspecto
    y captura eventos de mouse y teclado traduciéndolos a coordenadas normalizadas [0.0..1.0].
    """

    def __init__(self, send_event_callback):
        super().__init__()
        self.send_event = send_event_callback
        self.pixmap: Optional[QPixmap] = None
        self.control_enabled = True
        self.draw_rect = (0, 0, 1, 1)  # x, y, w, h donde se pintó la imagen

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def update_frame(self, jpeg_bytes: bytes):
        img = QImage.fromData(jpeg_bytes, "JPEG")
        if not img.isNull():
            self.pixmap = QPixmap.fromImage(img)
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(18, 18, 24))

        if not self.pixmap or self.pixmap.isNull():
            painter.setPen(QColor(180, 180, 190))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "Recibiendo señal de video del escritorio remoto...",
            )
            return

        scaled = self.pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        self.draw_rect = (x, y, max(1, scaled.width()), max(1, scaled.height()))
        painter.drawPixmap(x, y, scaled)

    def _map_to_normalized(self, pos: QPoint):
        rx, ry, rw, rh = self.draw_rect
        nx = (pos.x() - rx) / float(rw)
        ny = (pos.y() - ry) / float(rh)
        if 0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0:
            return nx, ny
        return None

    def _qt_button_name(self, button: Qt.MouseButton) -> str:
        if button == Qt.MouseButton.LeftButton:
            return "left"
        if button == Qt.MouseButton.RightButton:
            return "right"
        if button == Qt.MouseButton.MiddleButton:
            return "middle"
        return "left"

    def mouseMoveEvent(self, event: QMouseEvent):
        if not self.control_enabled:
            return
        coords = self._map_to_normalized(event.position().toPoint())
        if coords:
            self.send_event({"type": "mouse_move", "nx": coords[0], "ny": coords[1]})

    def mousePressEvent(self, event: QMouseEvent):
        self.setFocus()
        if not self.control_enabled:
            return
        coords = self._map_to_normalized(event.position().toPoint())
        if coords:
            self.send_event(
                {
                    "type": "mouse_press",
                    "nx": coords[0],
                    "ny": coords[1],
                    "button": self._qt_button_name(event.button()),
                }
            )

    def mouseReleaseEvent(self, event: QMouseEvent):
        if not self.control_enabled:
            return
        coords = self._map_to_normalized(event.position().toPoint())
        if coords:
            self.send_event(
                {
                    "type": "mouse_release",
                    "nx": coords[0],
                    "ny": coords[1],
                    "button": self._qt_button_name(event.button()),
                }
            )

    def wheelEvent(self, event: QWheelEvent):
        if not self.control_enabled:
            return
        coords = self._map_to_normalized(event.position().toPoint())
        if coords:
            delta = event.angleDelta()
            dx = 1 if delta.x() > 0 else (-1 if delta.x() < 0 else 0)
            dy = 1 if delta.y() > 0 else (-1 if delta.y() < 0 else 0)
            if dx != 0 or dy != 0:
                self.send_event(
                    {
                        "type": "mouse_scroll",
                        "nx": coords[0],
                        "ny": coords[1],
                        "dx": dx,
                        "dy": dy,
                    }
                )

    def keyPressEvent(self, event: QKeyEvent):
        if not self.control_enabled or event.isAutoRepeat():
            return
        key_enum = Qt.Key(event.key())
        key_name = key_enum.name if hasattr(key_enum, "name") else ""
        self.send_event(
            {
                "type": "key_press",
                "key_name": key_name,
                "text": event.text(),
            }
        )

    def keyReleaseEvent(self, event: QKeyEvent):
        if not self.control_enabled or event.isAutoRepeat():
            return
        key_enum = Qt.Key(event.key())
        key_name = key_enum.name if hasattr(key_enum, "name") else ""
        self.send_event(
            {
                "type": "key_release",
                "key_name": key_name,
                "text": event.text(),
            }
        )


class ViewerWindow(QMainWindow):
    """
    Ventana del Visor Remoto:
    Incluye barra superior con selector dinámico de Monitores (detecta si hay 1, 2 o más monitores),
    control de calidad, bloqueo/activación de mouse y teclado, y modo pantalla completa.
    """

    def __init__(self, sock: socket.socket, target_label: str):
        super().__init__()
        self.sock = sock
        self.send_lock = threading.Lock()
        self.setWindowTitle(f"Remote Support - Conectado a {target_label}")
        self.resize(1180, 720)
        base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        icon_path = base_dir / "icono.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Barra de herramientas superior
        toolbar = QWidget()
        toolbar.setStyleSheet(
            """
            QWidget {
                background-color: #1e1e2e;
                color: #cdd6f4;
                font-size: 13px;
            }
            QComboBox, QPushButton {
                background-color: #313244;
                border: 1px solid #45475a;
                border-radius: 5px;
                padding: 4px 10px;
                color: #cdd6f4;
            }
            QPushButton:hover {
                background-color: #45475a;
            }
            """
        )
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(12, 6, 12, 6)
        tb_layout.setSpacing(12)

        self.lbl_monitors_badge = QLabel("Monitores detectados: ...")
        self.lbl_monitors_badge.setStyleSheet("font-weight: bold; color: #89b4fa;")
        tb_layout.addWidget(self.lbl_monitors_badge)

        tb_layout.addWidget(QLabel("Pantalla:"))
        self.combo_monitors = QComboBox()
        self.combo_monitors.setMinimumWidth(230)
        self.combo_monitors.currentIndexChanged.connect(self._on_monitor_selected)
        tb_layout.addWidget(self.combo_monitors)

        tb_layout.addWidget(QLabel("Calidad:"))
        self.combo_quality = QComboBox()
        self.combo_quality.addItems(["Alta (85% - 30 FPS)", "Media (65% - 22 FPS)", "Rápida (45% - 15 FPS)"])
        self.combo_quality.setCurrentIndex(1)
        self.combo_quality.currentIndexChanged.connect(self._on_quality_selected)
        tb_layout.addWidget(self.combo_quality)

        self.chk_control = QCheckBox("Controlar Mouse y Teclado")
        self.chk_control.setChecked(True)
        self.chk_control.toggled.connect(self._on_toggle_control)
        tb_layout.addWidget(self.chk_control)

        tb_layout.addStretch()

        self.lbl_res = QLabel("0x0 px")
        self.lbl_res.setStyleSheet("color: #a6adc8;")
        tb_layout.addWidget(self.lbl_res)

        btn_fullscreen = QPushButton("Pantalla Completa")
        btn_fullscreen.clicked.connect(self._toggle_fullscreen)
        tb_layout.addWidget(btn_fullscreen)

        btn_disconnect = QPushButton("Desconectar")
        btn_disconnect.setStyleSheet("background-color: #f38ba8; color: #11111b; font-weight: bold;")
        btn_disconnect.clicked.connect(self.close)
        tb_layout.addWidget(btn_disconnect)

        main_layout.addWidget(toolbar)

        # Lienzo de proyección remota
        self.canvas = RemoteCanvas(self._send_event)
        main_layout.addWidget(self.canvas, stretch=1)

        # Hilo receptor de frames y metadatos de monitores
        self._updating_combo = False
        self.receiver = ViewerReceiverThread(self.sock)
        self.receiver.frame_received.connect(self._on_frame)
        self.receiver.monitors_updated.connect(self._on_monitors_updated)
        self.receiver.disconnected.connect(self._on_disconnected)
        self.receiver.start()

        # Excluir esta ventana de la captura de pantalla en Windows para evitar el "efecto espejo infinito"
        self._exclude_from_screen_capture()

    def _exclude_from_screen_capture(self):
        try:
            WDA_EXCLUDEFROMCAPTURE = 0x00000011
            hwnd = int(self.winId())
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        self._exclude_from_screen_capture()

    def _send_event(self, data: Dict[str, Any]):
        with self.send_lock:
            send_json(self.sock, data)

    def _on_frame(self, mon_idx: int, orig_w: int, orig_h: int, jpeg_bytes: bytes):
        self.lbl_res.setText(f"{orig_w}x{orig_h} px")
        self.canvas.update_frame(jpeg_bytes)

    def _on_monitors_updated(self, monitors: List[Dict[str, Any]], active_idx: int):
        physical_count = sum(1 for m in monitors if not m.get("is_all", False))
        if physical_count > 1:
            self.lbl_monitors_badge.setText(f"🟢 {physical_count} Monitores Conectados")
            self.lbl_monitors_badge.setStyleSheet("font-weight: bold; color: #a6e3a1;")
        else:
            self.lbl_monitors_badge.setText("🖥️ 1 Monitor Conectado")
            self.lbl_monitors_badge.setStyleSheet("font-weight: bold; color: #89b4fa;")

        self._updating_combo = True
        self.combo_monitors.clear()
        selected_row = 0
        for row, m in enumerate(monitors):
            self.combo_monitors.addItem(m["name"], m["index"])
            if m["index"] == active_idx:
                selected_row = row
        self.combo_monitors.setCurrentIndex(selected_row)
        self._updating_combo = False

    def _on_monitor_selected(self, row: int):
        if self._updating_combo or row < 0:
            return
        mon_idx = self.combo_monitors.itemData(row)
        if mon_idx is not None:
            self._send_event({"type": "select_monitor", "monitor_index": int(mon_idx)})

    def _on_quality_selected(self, idx: int):
        presets = [
            {"quality": 85, "fps": 30},
            {"quality": 65, "fps": 22},
            {"quality": 45, "fps": 15},
        ]
        if 0 <= idx < len(presets):
            p = presets[idx]
            self._send_event({"type": "set_quality", "quality": p["quality"], "fps": p["fps"]})

    def _on_toggle_control(self, checked: bool):
        self.canvas.control_enabled = checked

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _on_disconnected(self, reason: str):
        if self.isVisible():
            QMessageBox.information(self, "Sesión Finalizada", reason)
            self.close()

    def closeEvent(self, event):
        self.receiver.stop()
        self.receiver.wait(1000)
        super().closeEvent(event)
