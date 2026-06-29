import os
import sys
import subprocess

def get_cmdline(pid: int) -> list[str]:
    """Возвращает список аргументов командной строки процесса по PID."""
    
    platform = sys.platform

    # Linux / Android
    if platform.startswith("linux"):
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            data = f.read()
        # Аргументы разделены нулевыми байтами
        return data.rstrip(b"\x00").split(b"\x00")

    # macOS / BSD
    elif platform == "darwin" or platform.startswith("freebsd"):
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip().split()

    # Windows
    elif platform == "win32":
        # WMIC или PowerShell — оба встроены
        try:
            result = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}",
                 "get", "CommandLine", "/format:value"],
                capture_output=True, text=True, check=True
            )
            for line in result.stdout.splitlines():
                if line.startswith("CommandLine="):
                    cmdline = line[len("CommandLine="):].strip()
                    return cmdline.split() if cmdline else []
        except FileNotFoundError:
            # wmic убран в Windows 11 24H2+, фолбэк на PowerShell
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Process -Id {pid}).CommandLine"],
                capture_output=True, text=True, check=True
            )
            return result.stdout.strip().split()

    else:
        raise NotImplementedError(f"Unsupported platform: {platform}")