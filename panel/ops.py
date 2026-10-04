"""exe 运行支撑：单实例互斥、文件日志、开机自启（计划任务，最高权限）。

开机自启不用 HKCU Run 键，而是和 install.bat 同款的计划任务，
保证自启后也是提权状态（CPU/硬盘温度可用）。
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys

TASK_NAME = "SystemPanel"
MUTEX_NAME = r"Local\SystemPanel-SingleInstance"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".task_manager")
LOG_FILE = os.path.join(CONFIG_DIR, "panel.log")

_fault_file = None  # faulthandler 文件句柄必须常驻，否则崩溃时无 dump

_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>System Resource Monitor (admin, background)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def setup_logging() -> str:
    """日志落盘（windowed exe 没有控制台，全靠它排错），返回日志路径。"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        import faulthandler
        global _fault_file
        _fault_file = open(LOG_FILE, "a", encoding="utf-8")
        faulthandler.enable(file=_fault_file)
    except Exception:
        pass
    if getattr(sys, "frozen", False) or sys.stdout is None:
        # 冻结 exe 无控制台：print 重定向到日志，否则 print 会炸
        f = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        sys.stdout = f  # type: ignore[assignment]
        sys.stderr = f  # type: ignore[assignment]
    return LOG_FILE


def ensure_single_instance():
    """单实例互斥。返回句柄（调用方持有到进程结束）；已有实例返回 None。"""
    try:
        kernel32 = ctypes.windll.kernel32
        h = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            if h:
                kernel32.CloseHandle(h)
            return None
        return h
    except Exception:
        return True  # 拿不到互斥也不挡路（降级为允许多开）


def _launch_target() -> tuple[str, str]:
    """返回 (可执行路径, 工作目录)。冻结 exe 用自身；源码跑用 python -m panel。"""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return exe, os.path.dirname(exe)
    py = sys.executable
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return py, root


def _run_schtasks(*args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["schtasks", *args],
            capture_output=True, text=True,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            timeout=30,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)


def autostart_enabled() -> bool:
    rc, _ = _run_schtasks("/query", "/tn", TASK_NAME)
    return rc == 0


def autostart_set(enable: bool) -> tuple[bool, str]:
    """开关开机自启。返回 (成功, 信息)。需提权（exe 默认提权，直接调即可）。"""
    if not enable:
        rc, out = _run_schtasks("/delete", "/tn", TASK_NAME, "/f")
        ok = rc == 0 or "找不到" in out or "cannot find" in out.lower()
        return ok, "已关闭开机启动" if ok else f"删除任务失败: {out.strip()[:200]}"
    exe, workdir = _launch_target()
    user = os.environ.get("USERNAME", "")
    if getattr(sys, "frozen", False):
        command = exe
    else:
        command = f"{exe} -m panel"
        workdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    xml = _TASK_XML.format(user=user, command=command, workdir=workdir)
    tmp = os.path.join(CONFIG_DIR, "SystemPanel.task.xml")
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-16") as f:
            f.write(xml)
        _run_schtasks("/delete", "/tn", TASK_NAME, "/f")  # 幂等：先清旧的
        rc, out = _run_schtasks("/create", "/tn", TASK_NAME, "/xml", tmp, "/f")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if rc == 0:
        return True, "已开启开机启动（最高权限）"
    return False, f"创建任务失败: {out.strip()[:200]}"
