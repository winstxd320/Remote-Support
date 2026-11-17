import os
import sys
import ctypes
import shutil
import zipfile
import subprocess
from pathlib import Path


def get_bundled_zip() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "app_bundle.zip"
    return Path(__file__).parent / "app_bundle.zip"


def main():
    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        is_admin = False

    if not is_admin:
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, " ".join(f'"{a}"' for a in sys.argv), None, 1
        )
        sys.exit(0)

    install_dir = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Remote Support"
    target_exe = install_dir / "Remote Support.exe"

    # Cerrar instancia previa si estuviera abierta
    subprocess.run(
        ["taskkill", "/F", "/IM", "Remote Support.exe"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if install_dir.exists():
        try:
            shutil.rmtree(install_dir, ignore_errors=True)
        except Exception:
            pass
    install_dir.mkdir(parents=True, exist_ok=True)

    # Extraer el paquete --onedir directamente en C:\Program Files\Remote Support
    bundle_zip = get_bundled_zip()
    with zipfile.ZipFile(bundle_zip, "r") as zf:
        zf.extractall(install_dir)

    # Crear certificado de firma de código, instalarlo en Root y TrustedPublisher,
    # firmar C:\Program Files\Remote Support\Remote Support.exe (para que uiAccess="true" funcione sin error de referencia)
    # y crear el acceso directo en el Escritorio.
    ps_script = f"""
    $ErrorActionPreference = 'SilentlyContinue'
    $exePath = '{str(target_exe)}'

    $cert = Get-ChildItem Cert:\\LocalMachine\\My -CodeSigningCert | Where-Object {{ $_.Subject -eq 'CN=Remote Support UIAccess' }} | Select-Object -First 1
    if (-not $cert) {{
        $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject 'CN=Remote Support UIAccess' -CertStoreLocation 'Cert:\\LocalMachine\\My' -KeyUsage DigitalSignature -NotAfter (Get-Date).AddYears(10)
    }}

    if ($cert) {{
        $tmpCer = Join-Path $env:TEMP 'remote_support_uiaccess.cer'
        Export-Certificate -Cert $cert -FilePath $tmpCer -Force | Out-Null
        Import-Certificate -FilePath $tmpCer -CertStoreLocation 'Cert:\\LocalMachine\\Root' | Out-Null
        Import-Certificate -FilePath $tmpCer -CertStoreLocation 'Cert:\\LocalMachine\\TrustedPublisher' | Out-Null
        Set-AuthenticodeSignature -FilePath $exePath -Certificate $cert | Out-Null
        Remove-Item $tmpCer -Force -ErrorAction SilentlyContinue
    }}

    $WshShell = New-Object -ComObject WScript.Shell
    $desktopPath = [Environment]::GetFolderPath('Desktop')
    $shortcut = $WshShell.CreateShortcut((Join-Path $desktopPath 'Remote Support.lnk'))
    $shortcut.TargetPath = $exePath
    $shortcut.WorkingDirectory = '{str(install_dir)}'
    $shortcut.Description = 'Remote Support - Asistencia Remota'
    $shortcut.Save()
    """

    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    # Abrir la aplicación firmada desde C:\Program Files\Remote Support\Remote Support.exe
    ctypes.windll.shell32.ShellExecuteW(
        None, "open", str(target_exe), None, str(install_dir), 1
    )


if __name__ == "__main__":
    main()
