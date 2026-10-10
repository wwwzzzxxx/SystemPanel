"""采集层：复用 verify_metrics.py 的 DLL 直调逻辑，供 3s 轮询调用。"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from dataclasses import dataclass, field


def _dll_path() -> str:
    # 打包后 DLL 随包解压到 _MEIPASS，不在 exe 旁边
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, "_engine", "LibreHardwareMonitor", "LibreHardwareMonitorLib.dll")  # type: ignore[attr-defined]
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "_engine", "LibreHardwareMonitor", "LibreHardwareMonitorLib.dll"))


DLL = _dll_path()

# 延迟导入，冷启动更快
_imported = False
_psutil = None
_pynvml = None


def _ensure_imports():
    global _imported, _psutil, _pynvml
    if _imported:
        return
    import psutil as _psutil_mod
    _psutil = _psutil_mod
    try:
        import pynvml as _pynvml_mod
        _pynvml = _pynvml_mod
        try:
            _pynvml.nvmlInit()
        except Exception:
            pass
    except ImportError:
        _pynvml = None
    _imported = True


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _clean_label(raw: str) -> str:
    """去除 WMI 常见的 ' SCSI Disk Device' 后缀并精简空格。"""
    name = raw.strip()
    suffix = " SCSI Disk Device"
    if name.endswith(suffix):
        name = name[: -len(suffix)].strip()
    return name


_BRIDGE_KEYWORDS = ("realtek", "rtl9210", "jmicron", "asm115", "asm135", "via labs", "norelsys", "ag620")


def _is_bridge_name(name: str) -> bool:
    low = name.lower()
    return any(k in low for k in _BRIDGE_KEYWORDS)


@dataclass
class DiskInfo:
    name: str
    busy: float | None = None  # 0..100, None 表示休眠中
    temp: float | None = None  # 摄氏度
    sleeping: bool = False
    total_gb: float | None = None
    read_bps: float | None = None   # 字节/秒
    write_bps: float | None = None  # 字节/秒


@dataclass
class Metrics:
    # CPU/GPU 圆环
    cpu_load: float | None = None      # 0..100
    cpu_temp: float | None = None
    gpu_load: float | None = None
    gpu_temp: float | None = None
    # 进度条
    mem_percent: float | None = None
    mem_used_gb: float | None = None
    mem_total_gb: float | None = None
    vram_percent: float | None = None
    vram_used_mb: float | None = None
    vram_total_mb: float | None = None
    disks: list[DiskInfo] = field(default_factory=list)
    # 网络：所有物理网卡（有线+无线）合计，单位字节/秒
    net_recv_bps: float | None = None
    net_sent_bps: float | None = None
    is_admin: bool = False
    timestamp: float = 0


class DiskIdleTracker:
    """跟踪硬盘是否在休眠：连续 N 轮无有效 IO 即标记休眠，期间跳过 SMART。

    不向硬盘发任何 ATA/SMART 指令，仅看 psutil 的 read/write 差分，
    因此不会唤醒已休眠的机械盘。微量读（≤4KB/轮，零写）视为空闲——那是 LHM
    自己的 SMART 轮询在 USB 桥接下留下的痕迹，不能当作真实负载。仅 USB-HDD
    参与休眠判定（MediaType == HDD），USB-SSD / NVMe / SATA-SSD 永不判休眠。
    """

    def __init__(self, sleep_grace_rounds: int = 10):
        self.sleep_grace_rounds = sleep_grace_rounds
        self._zero_streak: dict[str, int] = {}
        self._prev_io: dict | None = None
        # 盘名映射的缓存，避免每轮都 WMI
        self._disk_labels: dict[str, str] = {}
        # 上一轮时间戳与读写速率（字节/秒）
        self._prev_time: float | None = None
        self.rates: dict[str, tuple[float, float]] = {}

    def update_busy(
        self,
        per_disk_io: dict,
        bus_map: dict[str, str] | None = None,
        media_map: dict[str, str] | None = None,
    ) -> tuple[dict[str, float], dict[str, bool]]:
        """返回 (busy_pct, sleeping) 两个字典，key 为 PhysicalDrive 名。

        仅 USB+HDD 参与休眠判定（ST16000 这类机械外置盘），USB-SSD/NVMe/SATA-SSD
        永不判休眠，避免闲置零 IO 被误判为休眠。
        bus_map: PhysicalDrive -> BusType（如 NVMe/USB）
        media_map: PhysicalDrive -> MediaType (HDD/SSD/Unspecified)

        这里算出的 busy_pct 仅作兜底；NVMe 上 psutil 的 I/O 时间精度丢失会恒为 0，
        正常情况下由上面 _pdh_busy 的 PDH 值覆盖。rate 走字节差分，NVMe 上也准确。
        """
        busy: dict[str, float] = {}
        sleeping: dict[str, bool] = {}
        now = time.time()
        window = (now - self._prev_time) if self._prev_time else 0.0

        if self._prev_io is None:
            self._prev_io = per_disk_io
            self._prev_time = now
            for k in per_disk_io:
                busy[k] = 0.0
                sleeping[k] = False
                self.rates[k] = (0.0, 0.0)
                self._zero_streak[k] = 0
            return busy, sleeping

        for name, cur in per_disk_io.items():
            prev = self._prev_io.get(name)
            if prev is None:
                busy[name] = 0.0
                sleeping[name] = False
                self.rates[name] = (0.0, 0.0)
                self._zero_streak[name] = 0
                continue
            d_read = cur.read_count - prev.read_count
            d_write = cur.write_count - prev.write_count
            d_rb = cur.read_bytes - prev.read_bytes
            d_wb = cur.write_bytes - prev.write_bytes
            dt = (cur.read_time - prev.read_time) + (cur.write_time - prev.write_time)

            # 读写速率（字节/秒）：字节差分是准的，与 I/O 时间无关
            if window > 0:
                self.rates[name] = (max(0.0, d_rb) / window, max(0.0, d_wb) / window)
            else:
                self.rates[name] = (0.0, 0.0)

            # LHM 自身的 SMART 轮询经 USB 桥接后表现为 1 次读/512B，会被差分误判为
            # "有 IO"，导致休眠计数永远清零、休眠判定永不成立（自我反馈死锁）。
            # 微量读（≤4KB 且零写）按空闲处理；真实文件读写远大于此阈值。
            idle_like = d_wb == 0 and d_rb <= 4096
            if idle_like:
                self._zero_streak[name] = self._zero_streak.get(name, 0) + 1
                pct = 0.0
            else:
                self._zero_streak[name] = 0
                pct = min(100.0, max(0.0, dt / 10.0))
            busy[name] = pct
            bt = (bus_map or {}).get(name, "").lower() if bus_map else ""
            mt = (media_map or {}).get(name, "").lower() if media_map else ""
            # 仅 USB-HDD 允许休眠；其它组合一律不休眠
            can_sleep = (bt == "usb" and mt == "hdd")
            sleeping[name] = can_sleep and self._zero_streak[name] >= self.sleep_grace_rounds

        self._prev_io = per_disk_io
        self._prev_time = now
        return busy, sleeping

    def prime(self, per_disk_io: dict):
        self._prev_io = per_disk_io
        for k in per_disk_io:
            if k not in self._zero_streak:
                self._zero_streak[k] = 0


_tracker = DiskIdleTracker(sleep_grace_rounds=10)
_lhm_computer = None
_lhm_cache: dict[str, float] = {}
_lhm_counter = 0
_LHM_EVERY_N_TICKS = 5  # 硬盘温度变化慢，每 5 轮（约 15s）扫一次，平时用缓存
_msr_reader = None  # PawnIo.IntelMsr 单例（CPU 温度走 MSR 直读，微秒级）
_msr_tjmax: int | None = None
_lhm_ref_added = False


def _ensure_lhm_ref() -> bool:
    """clr.AddReference 只做一次（MSR 和 Computer 都依赖它）。"""
    global _lhm_ref_added
    if _lhm_ref_added:
        return True
    try:
        import clr
        clr.AddReference(DLL)
        _lhm_ref_added = True
        return True
    except Exception as e:
        print(f"[lhm] AddReference 失败: {e}")
        return False
_lhm_ok = False


class _NetRateTracker:
    """物理网卡总吞吐（字节/秒），差分自 psutil.net_io_counters。

    只统计"物理网卡"：WMI Win32_NetworkAdapter 里 PhysicalAdapter=TRUE 且
    AdapterType 为 802.3 的那些（有线 + 无线 + 蓝牙 PAN），用 NetConnectionID
    和 psutil 的网卡名对齐。这样能排除掉：

      - Clash/verge-mihomo 的 tun0（它是隧道口，其字节只是物理网卡的再一层
        计数，算进去就翻倍了；TUN 模式下无 TAP 因此不会二次计数）
      - vEthernet(WSL/Hyper-V)、Loopback、Wi-Fi Direct("本地连接* N") 等

    结果与"有没有开 tun / 走没走 clash"无关：clash 只是把流量从物理网卡中转，
    不管是系统代理还是 TUN 模式，物理网卡上的字节就是本机真实吞吐。

    注意 psutil 的网卡名是系统 ANSI 编码（本机 GBK），用 WMI 名字可直接匹配。
    """

    _CACHE_TTL = 600.0  # 网卡热插拔/改名后 10 分钟自动重新枚举

    def __init__(self):
        self._names: set[str] | None = None
        self._names_at = 0.0
        self._prev: dict[str, tuple[int, int]] = {}
        self._prev_time: float | None = None

    def _refresh_names(self):
        names: set[str] = set()
        try:
            import win32com.client as _wc  # type: ignore

            wmi = _wc.GetObject("winmgmts:")
            q = wmi.ExecQuery(
                "SELECT Name, NetConnectionID, PhysicalAdapter, AdapterType "
                "FROM Win32_NetworkAdapter WHERE PhysicalAdapter=TRUE"
            )
            for r in q:
                if "802.3" not in str(getattr(r, "AdapterType", "") or ""):
                    continue
                conn = getattr(r, "NetConnectionID", None)
                if conn:
                    names.add(str(conn))
        except Exception:
            names = set()
        self._names = names
        self._names_at = time.time()

    def _counter_map(self) -> dict[str, tuple[int, int]]:
        assert _psutil is not None
        return {
            n: (int(c.bytes_recv), int(c.bytes_sent))
            for n, c in (_psutil.net_io_counters(pernic=True) or {}).items()
        }

    def prime(self):
        """首帧无意义，先存基线。"""
        self._refresh_names()
        try:
            self._prev = self._counter_map()
            self._prev_time = time.time()
        except Exception:
            self._prev = {}
            self._prev_time = None

    def update(self) -> tuple[float, float]:
        """返回 (recv_bps, sent_bps)，按两个采样点之间的真实墙钟时间折算。"""
        now = time.time()
        cur = self._counter_map()
        window = (now - self._prev_time) if self._prev_time else 0.0

        if self._names is None or (now - self._names_at) > self._CACHE_TTL:
            self._refresh_names()
        names = self._names or set()

        dr = ds = 0
        if window > 0:
            for name, (rx, tx) in cur.items():
                # WMI 失效时退回名字启发式，至少不把虚拟网卡算进来
                if names:
                    if name not in names:
                        continue
                elif not _looks_physical_net(name):
                    continue
                prx, ptx = self._prev.get(name, (rx, tx))
                # 计数器回绕/网卡重插：只取非负增量，异常值丢弃
                d_rx = rx - prx
                d_tx = tx - ptx
                if d_rx < 0 or d_tx < 0:
                    continue
                dr += d_rx
                ds += d_tx

        self._prev = cur
        self._prev_time = now
        if window <= 0:
            return 0.0, 0.0
        return dr / window, ds / window


_net_tracker = _NetRateTracker()


# 排除"不是物理网卡"的接口名。仅作 WMI 枚举失败时的兜底，宁可少算也不重复计数。
_NET_VIRTUAL_HINTS = (
    "tun", "tap", "wintun", "wireguard", "vpn", "tunnel",
    "loopback", "vethernet", "hyper-v", "wsl", "docker", "vmware",
    "virtualbox", "vbox", "radmin", "npcap", "bluetooth",
)
# Windows 的"Wi-Fi Direct / 承载网络"虚拟口形如 "本地连接* 9"、"Local Area Connection* 9"
_NET_VIRTUAL_PREFIX = ("本地连接*", "local area connection*")


def _looks_physical_net(name: str) -> bool:
    low = (name or "").strip().lower()
    if not low:
        return False
    if low.startswith(_NET_VIRTUAL_PREFIX):
        return False
    return not any(h in low for h in _NET_VIRTUAL_HINTS)


class _PdhDiskBusy:
    """用 Windows 性能计数器读物理盘繁忙率（\\PhysicalDisk(N)\\% Disk Time）。

    psutil 的 read_time/write_time 在 NVMe 上精度丢失（实测写 6GB 只 +2），
    所以繁忙率改用 PDH——就是任务管理器那个"磁盘占用"。

    PDH 的百分比计数需要两次采样求差，所以这里只建一次查询，之后每次 read()
    采一次，返回值即"距上次 read 的这段时间"内的占用率（面板 3s 轮询，正好）。
    """

    def __init__(self):
        self._query = None
        self._counters: dict[str, object] = {}
        self._unavailable = False
        self._primed = False

    def _ensure(self):
        if self._query is not None or self._unavailable:
            return
        try:
            import win32pdh
        except Exception:
            self._unavailable = True
            return
        try:
            query = win32pdh.OpenQuery()
            _counters, instances = win32pdh.EnumObjectItems(
                None, None, "PhysicalDisk", win32pdh.PERF_DETAIL_WIZARD
            )
            handles = {}
            for inst in instances:
                if inst.startswith("_"):  # _Total
                    continue
                try:
                    handles[inst] = win32pdh.AddCounter(
                        query, rf"\PhysicalDisk({inst})\% Disk Time"
                    )
                except Exception:
                    continue
            if not handles:
                win32pdh.CloseQuery(query)
                self._unavailable = True
                return
            # 第一次采样只建立基线，值不可用
            win32pdh.CollectQueryData(query)
            self._query = query
            self._counters = handles
        except Exception:
            self._unavailable = True

    def read(self) -> dict[str, float]:
        """返回 {PhysicalDriveN: busy% 0..100}；不可用时返回 {}。"""
        self._ensure()
        if self._query is None:
            return {}
        try:
            import win32pdh
            win32pdh.CollectQueryData(self._query)
            out: dict[str, float] = {}
            for inst, handle in self._counters.items():
                try:
                    _t, value = win32pdh.GetFormattedCounterValue(
                        handle, win32pdh.PDH_FMT_DOUBLE
                    )
                except Exception:
                    continue
                value = float(value)
                if value != value:  # NaN（首次求差）
                    continue
                # 实例名形如 "0 C:"，前缀数字对应 PhysicalDriveN
                num = inst.split()[0]
                if not num.isdigit():
                    continue
                out[f"PhysicalDrive{num}"] = max(0.0, min(100.0, value))
            return out
        except Exception:
            return {}


_pdh_busy = _PdhDiskBusy()

# collect() 各段耗时（秒），供面板发现卡顿来源；平时不打印，超阈值才报
_last_timing: dict[str, float] = {}
# 最近一轮 CPU 温度来源：lhm/msr | msacpi | smbios-probe | none（约每分钟打一次日志）
_cpu_temp_source = "?"
_collect_count = 0


def _cpu_package_via_msr() -> float | None:
    """CPU Package 温度走 MSR 直读（微秒级，无系统卡顿）。

    LHM 的 hw.Update() 在 CPU 上要 240ms+（且疑似触发系统级 stall），
    而我们其实只要 Package 一个数：TjMax 取自 0x1A2，offset 取自 0x1B1。
    失败（非 Intel / 无驱动 / 非管理员）返回 None，调用方回退旧路径。
    """
    global _msr_reader, _msr_tjmax
    try:
        if not _ensure_lhm_ref():
            return None
        if _msr_reader is None:
            from LibreHardwareMonitor.PawnIo import IntelMsr
            import System
            _msr_reader = IntelMsr()
            _UInt32 = System.UInt32
        else:
            import System
            _UInt32 = System.UInt32
        if _msr_tjmax is None:
            ok, eax, _edx = _msr_reader.ReadMsr(0x1A2, _UInt32(0), _UInt32(0))
            _msr_tjmax = ((int(eax) >> 16) & 0xFF) if ok else 0
            if not _msr_tjmax:
                _msr_tjmax = 100
        ok, eax, _edx = _msr_reader.ReadMsr(0x1B1, _UInt32(0), _UInt32(0))
        eax = int(eax)
        if not ok or not (eax >> 31):
            return None
        return float(_msr_tjmax - ((eax >> 16) & 0x7F))
    except Exception as e:
        global _msr_err_logged
        if not globals().get("_msr_err_logged"):
            globals()["_msr_err_logged"] = True
            print(f"[msr] 直读失败，回退 LHM: {e}")
        return None


def _get_lhm_temps_if_admin(
    sleeping_map: dict | None = None,
    idle_hdd_pds: list | None = None,
) -> dict[str, float]:
    """管理员下一次性扫描 CPU/Storage 温度，非管理员直接返回空字典。
    对已判休眠的硬盘跳过 Update()：LHM 读温度要发 SMART 指令，会唤醒休眠的机械盘。
    对空闲机械盘（本轮无真实 IO，休眠计数仍在累积）同样跳过：SMART 会重置它的
    睡眠计时。被跳过的盘温度沿用缓存显示旧值，不发新查询。
    """
    if not is_admin():
        return {}
    global _lhm_computer, _lhm_ok, _lhm_counter, _lhm_cache
    # CPU 温度每轮都走 MSR 直读（微秒级，常新）；硬盘温度才用 15s 缓存
    msr_temp = _cpu_package_via_msr()
    _lhm_counter += 1
    # 非扫描轮：硬盘温度用缓存 + CPU 用刚读的 MSR 值
    if _lhm_counter % _LHM_EVERY_N_TICKS != 1 and _lhm_cache:
        _cached = {k: v for k, v in _lhm_cache.items() if k.startswith("disk:")}
        if msr_temp is not None:
            _cached["cpu:CPU Package"] = msr_temp
        elif any(k.startswith("cpu:") for k in _lhm_cache):
            _cached.update({k: v for k, v in _lhm_cache.items() if k.startswith("cpu:")})
        return _cached
    try:
        if _lhm_computer is None:
            if not _ensure_lhm_ref():
                return {}
            from LibreHardwareMonitor.Hardware import Computer
            _lhm_computer = Computer()
            # 只开要用的两组：CPU/硬盘温度。主板(EC/SuperIO 的 ISA 访问会卡全系统)、
            # GPU(LHM 扫 GPU 慢且重复，GPU 走 NVML) 等一律不开
            for _prop, _want in (
                ("IsCpuEnabled", True),
                ("IsStorageEnabled", True),
                ("IsGpuEnabled", False),
                ("IsMemoryEnabled", False),
                ("IsMotherboardEnabled", False),
                ("IsControllerEnabled", False),
                ("IsNetworkEnabled", False),
                ("IsPsuEnabled", False),
                ("IsBatteryEnabled", False),
            ):
                try:
                    setattr(_lhm_computer, _prop, _want)
                except Exception:
                    pass
            _lhm_computer.Open()
            _lhm_ok = True
        # 休眠盘的 WMI 标签列表；标签缺失无法匹配时保守跳过所有 Storage 更新
        sleeping_pds = [pd for pd, sl in (sleeping_map or {}).items() if sl]
        sleeping_labels = [
            (_physical_to_label.get(pd) or "").strip()
            for pd in sleeping_pds
        ]
        any_unlabeled = sleeping_pds and any(not lab for lab in sleeping_labels)
        # 空闲机械盘的 WMI 标签：本轮无真实 IO 就不查，睡眠计时不受扰
        idle_labels = [
            (_physical_to_label.get(pd) or "").strip()
            for pd in (idle_hdd_pds or [])
        ]
        idle_unlabeled = bool(idle_hdd_pds) and any(not lab for lab in idle_labels)
        result: dict[str, float] = {}
        import time as _tmod
        _hw_ms: dict[str, float] = {}
        skipped_hw: set[str] = set()
        for hw in _lhm_computer.Hardware:
            ht = str(hw.HardwareType)
            if ht == "Cpu" and msr_temp is not None:
                continue  # MSR 已拿到 Package 温度，不 Update（省 240ms 系统 stall）
            if ht == "Storage" and (sleeping_pds or idle_hdd_pds):
                if any_unlabeled or idle_unlabeled or any(
                    match_disk_name(lab, hw.Name) or match_disk_name(hw.Name, lab)
                    for lab in ([*sleeping_labels, *idle_labels]) if lab
                ):
                    skipped_hw.add(hw.Name)
                    continue
            _t1 = _tmod.perf_counter()
            hw.Update()
            _dt = (_tmod.perf_counter() - _t1) * 1000
            _hw_ms[f"{ht}:{hw.Name}"] = _hw_ms.get(f"{ht}:{hw.Name}", 0.0) + _dt
            if ht == "Cpu":
                for s in hw.Sensors:
                    if str(s.SensorType) == "Temperature" and s.Value is not None:
                        # 取 CPU Package 优先，次取 Core Max
                        result[f"cpu:{s.Name}"] = float(s.Value)
            elif ht == "Storage":
                for s in hw.Sensors:
                    if str(s.SensorType) == "Temperature" and s.Value is not None:
                        result[f"disk:{hw.Name}:{s.Name}"] = float(s.Value)
            for sub in hw.SubHardware:
                sub.Update()
        # 被跳过的盘沿用上轮缓存（显示旧值，不发新 SMART，睡眠计时不受扰）
        if skipped_hw and _lhm_cache:
            for k, v in _lhm_cache.items():
                if not k.startswith("disk:") or k in result:
                    continue
                parts = k.split(":")
                if len(parts) >= 2 and parts[1] in skipped_hw:
                    result[k] = v
        _lhm_cache = dict(result)
        if msr_temp is not None:
            _lhm_cache["cpu:CPU Package"] = msr_temp
            result["cpu:CPU Package"] = msr_temp
        _slow = sorted(_hw_ms.items(), key=lambda kv: -kv[1])[:6]
        print(f"[lhm] scan {sum(_hw_ms.values()):.0f}ms " +
              " ".join(f"{k}={v:.0f}ms" for k, v in _slow))
        return result
    except Exception:
        return dict(_lhm_cache) if _lhm_cache else {}


_wmi_cpu_next_try = 0.0  # WMI 兜底失败后 60s 内不再试（WMI 枚举慢，不能每轮都查）
_wmi_cpu_cache: tuple[float, float | None, str | None] = (0.0, None, None)


def _cpu_temp_wmi_fallback() -> tuple[float | None, str | None]:
    """不走 ring0 的 CPU 温度兜底（HVCI 开着时 LHM/MSR 全灭用它）。

    MSAcpi_ThermalZoneTemperature：ACPI 热区，单位 0.1K；台式机主板经常不提供。
    Win32_TemperatureProbe：SMBIOS 探针， populated 的机器很少。
    返回 (温度, 来源)，都没有返回 (None, None)。
    """
    global _wmi_cpu_next_try
    now = time.time()
    if now < _wmi_cpu_next_try:
        return None, None
    _cts, _ct, _cs = _wmi_cpu_cache
    if _ct is not None and now - _cts < 10.0:
        return _ct, _cs
    try:
        import win32com.client as _wc  # type: ignore

        wmi = _wc.GetObject("winmgmts:root\\wmi")
        best: float | None = None
        try:
            for z in wmi.ExecQuery(
                "SELECT CurrentTemperature FROM MSAcpi_ThermalZoneTemperature"
            ):
                try:
                    raw = int(getattr(z, "CurrentTemperature", 0) or 0)
                except Exception:
                    continue
                if raw <= 0:
                    continue
                c = raw / 10.0 - 273.15
                if -40.0 <= c <= 125.0 and (best is None or c > best):
                    best = c
        except Exception:
            pass
        if best is not None:
            _wmi_cpu_cache = (now, best, "msacpi")
            return best, "msacpi"
        try:
            wmi2 = _wc.GetObject("winmgmts:")
            for p in wmi2.ExecQuery(
                "SELECT CurrentReading FROM Win32_TemperatureProbe"
            ):
                try:
                    raw = int(getattr(p, "CurrentReading", 0) or 0)
                except Exception:
                    continue
                if raw <= 0:
                    continue
                c = raw / 10.0 - 273.15
                if -40.0 <= c <= 125.0 and (best is None or c > best):
                    best = c
        except Exception:
            pass
        if best is not None:
            _wmi_cpu_cache = (now, best, "smbios-probe")
            return best, "smbios-probe"
    except Exception:
        pass
    _wmi_cpu_next_try = now + 60.0
    return None, None


# 盘符与 PhysicalDrive 的粗映射，供 UI 显示友好名称
_physical_to_label: dict[str, str] = {}
_physical_to_bus: dict[str, str] = {}
_physical_to_media: dict[str, str] = {}  # HDD / SSD / Unspecified
_physical_to_size_gb: dict[str, float] = {}
_drive_to_physical: dict[str, str] = {}  # C: -> PhysicalDrive0
_volume_labels: dict[str, str] = {}
_volume_sizes: dict[str, int] = {}


def _refresh_disk_labels():
    # 1) Win32_DiskDrive 的 Model 做初值，并清洗后缀
    try:
        import win32com.client  # type: ignore

        wmi = win32com.client.GetObject("winmgmts:")
        disks = wmi.ExecQuery("SELECT DeviceID, Model FROM Win32_DiskDrive")
        for d in disks:
            did = str(d.DeviceID)  # \\.\PHYSICALDRIVE0
            name = f"PhysicalDrive{did.split('PHYSICALDRIVE')[-1]}"
            raw = str(d.Model).strip()
            _physical_to_label[name] = _clean_label(raw)
            # 若是桥接芯片名，允许后续被 MSFT 的真实盘信息覆盖
    except Exception:
        pass
    try:
        import win32com.client as _wc  # type: ignore

        wmi2 = _wc.GetObject("winmgmts:root\\Microsoft\\Windows\\Storage")
        _bus_names = {0: "Unknown", 1: "SCSI", 4: "USB", 7: "USB", 11: "SATA", 17: "NVMe", 8: "RAID"}
        _media_names = {0: "Unspecified", 3: "HDD", 4: "SSD", 5: "SCM"}
        try:
            q = wmi2.ExecQuery("SELECT DeviceId, BusType, FriendlyName, MediaType, Size FROM MSFT_PhysicalDisk")
            for r in q:
                did = str(r.DeviceId)
                bt = int(r.BusType) if r.BusType is not None else 0
                name = _bus_names.get(bt, str(bt))
                pd = f"PhysicalDrive{did}"
                _physical_to_bus[pd] = name
                mt = int(r.MediaType) if r.MediaType is not None else 0
                _physical_to_media[pd] = _media_names.get(mt, str(mt))
                try:
                    if r.Size is not None:
                        _physical_to_size_gb[pd] = int(r.Size) / (1024**3)
                except Exception:
                    pass
                # 用 FriendlyName 补齐 / 纠正桥接芯片名
                if r.FriendlyName:
                    cand = _clean_label(str(r.FriendlyName).strip())
                    cur = _physical_to_label.get(pd, "")
                    if not cur or _is_bridge_name(cur):
                        _physical_to_label[pd] = cand
        except Exception:
            pass
    except Exception:
        pass

    # 2) 纯 WMI 关联盘符 -> PhysicalDrive（无 powershell，不弹黑窗）
    try:
        import win32com.client as _wc2  # type: ignore
        try:
            wmi3 = _wc2.GetObject("winmgmts:root\Microsoft\Windows\Storage")
            qp = wmi3.ExecQuery("SELECT DiskNumber, DriveLetter FROM MSFT_Partition")
            for _pp in qp:
                try:
                    dl = int(getattr(_pp, "DriveLetter", 0) or 0)
                    dn = getattr(_pp, "DiskNumber", None)
                    if dl and dn is not None:
                        letter = f"{chr(dl).upper()}:"
                        _drive_to_physical[letter] = f"PhysicalDrive{int(dn)}"
                except Exception:
                    continue
        except Exception:
            pass
    except Exception:
        pass
    # 卷标与容量缓存（用于桥接盘显示），同样走 WMI，无 PowerShell 弹窗
    try:
        _volume_labels.clear()
        _volume_sizes.clear()
    except Exception:
        pass
    try:
        import win32com.client as _wc_vol  # type: ignore
        try:
            wmiS2 = _wc_vol.GetObject("winmgmts:root\Microsoft\Windows\Storage")
            qv = wmiS2.ExecQuery("SELECT DriveLetter, FileSystemLabel, Size FROM MSFT_Volume")
            for _vv in qv:
                try:
                    dlc = getattr(_vv, "DriveLetter", None)
                    if dlc is None:
                        continue
                    dl_int = int(dlc)
                    if not dl_int:
                        continue
                    letter = f"{chr(dl_int).upper()}:"
                    lab = str(getattr(_vv, "FileSystemLabel", "") or "").strip()
                    sz = getattr(_vv, "Size", None)
                    _volume_labels[letter] = lab
                    if sz is not None:
                        try:
                            _volume_sizes[letter] = int(sz)
                        except Exception:
                            pass
                except Exception:
                    continue
        except Exception:
            pass
        if not _volume_labels:
            try:
                wmi2_vol = _wc_vol.GetObject("winmgmts:")
                for _v2 in wmi2_vol.ExecQuery("SELECT DeviceID, VolumeName, Size FROM Win32_LogicalDisk WHERE DriveType=3"):
                    try:
                        letter = str(getattr(_v2, "DeviceID", "")).strip().upper()
                        if not letter:
                            continue
                        if not letter.endswith(":"):
                            letter += ":"
                        lab = str(getattr(_v2, "VolumeName", "") or "").strip()
                        if letter not in _volume_labels:
                            _volume_labels[letter] = lab
                        sz = getattr(_v2, "Size", None)
                        if sz is not None and letter not in _volume_sizes:
                            try:
                                _volume_sizes[letter] = int(sz)
                            except Exception:
                                pass
                    except Exception:
                        continue
            except Exception:
                pass
    except Exception:
        pass



def match_disk_name(label: str, hw_name: str) -> bool:
    """检查硬盘标签是否匹配 LHM 硬件名"""
    label_lower = label.lower()
    hw_lower = hw_name.lower()

    # 关键词匹配
    keywords = {
        "pvc10": ["pvc10", "pvc 10", "hynix"],
        "990 evo": ["990 evo", "samsung 990"],
        "st16000": ["st16000", "16t", "seagate"]
    }

    for key, patterns in keywords.items():
        if any(p in label_lower for p in patterns) and any(p in hw_lower for p in patterns):
            return True

    # 简单词匹配
    label_words = set(label_lower.replace("-", " ").split())
    hw_words = set(hw_lower.replace("-", " ").split())
    common = label_words & hw_words
    return len(common) >= 2


def collect(skip_disk_temp: bool = False, skip_temps: bool = False) -> Metrics:
    """采集一轮指标。

    skip_disk_temp=True（面板不可见时由调用方传入）：跳过所有
    Storage hw.Update()（同步 SMART passthrough，会顶系统一下），
    CPU 仍走 MSR 直读（微秒级，不中断），硬盘温度沿用缓存不刷新。

    skip_temps=True（右键菜单关掉温度显示时由调用方传入）：一个温度都不读
    （NVML / MSR / LHM / WMI 全跳过），温度字段留 None，界面不显示 °C。
    """
    global _cpu_temp_source
    _ensure_imports()
    assert _psutil is not None
    m = Metrics(is_admin=is_admin(), timestamp=time.time())
    _t0 = time.perf_counter()

    # CPU 负载 - 不要每次预热，否则会重置计数器导致返回 0
    try:
        m.cpu_load = float(_psutil.cpu_percent(interval=None))
        if m.cpu_load is None:
            m.cpu_load = 0.0
    except Exception:
        m.cpu_load = None

    # 内存
    try:
        vm = _psutil.virtual_memory()
        m.mem_percent = float(vm.percent)
        m.mem_used_gb = vm.used / 1024**3
        m.mem_total_gb = vm.total / 1024**3
    except Exception:
        pass

    # GPU + 显存（pynvml 无需管理员）
    if _pynvml is not None:
        try:
            h = _pynvml.nvmlDeviceGetHandleByIndex(0)
            util = _pynvml.nvmlDeviceGetUtilizationRates(h)
            m.gpu_load = float(util.gpu)
            if not skip_temps:
                m.gpu_temp = float(_pynvml.nvmlDeviceGetTemperature(h, _pynvml.NVML_TEMPERATURE_GPU))
            mem = _pynvml.nvmlDeviceGetMemoryInfo(h)
            m.vram_total_mb = mem.total / 1024**2
            m.vram_used_mb = mem.used / 1024**2
            if m.vram_total_mb:
                m.vram_percent = float(mem.used / mem.total * 100)
        except Exception:
            pass

    # 网络总吞吐（物理网卡合计，与 clash 是否开 TUN 无关）
    try:
        rx_bps, tx_bps = _net_tracker.update()
        m.net_recv_bps = rx_bps
        m.net_sent_bps = tx_bps
    except Exception:
        pass

    # 硬盘繁忙（差分，不唤醒）+ 命名清洗 + 休眠判定细化
    try:
        per_disk = _psutil.disk_io_counters(perdisk=True) or {}

        # 确保硬盘标签/总线/介质类型已加载（供休眠判定与命名）
        if not _physical_to_label or not _physical_to_bus:
            _refresh_disk_labels()

        busy_map, sleeping_map = _tracker.update_busy(per_disk, _physical_to_bus, _physical_to_media)
        # 空闲机械盘本轮不查温度：速率为 0（休眠计数仍在累积）时任何 SMART
        # 都会重置/打断它的睡眠计时。SSD/NVMe 不受影响，照常扫描。
        idle_hdd_pds = [
            pd for pd in per_disk
            if (_physical_to_media.get(pd, "").lower() == "hdd"
                and _tracker._zero_streak.get(pd, 0) > 0)
        ]

        # 繁忙率优先用 PDH（psutil 的 I/O 时间在 NVMe 上恒为 0），取不到再退回差分值
        pdh_busy = _pdh_busy.read()
        if pdh_busy:
            busy_map = {**busy_map, **pdh_busy}

        # 构建盘符标签补充（用于桥接芯片名回退）
        # drive_labels: PhysicalDrive -> "Y: 1TB" / "D: 16T"
        drive_labels: dict[str, str] = {}
        try:
            vl = globals().get("_volume_labels", {}) or {}
            vs = globals().get("_volume_sizes", {}) or {}
            if not vl:
                try:
                    import win32com.client as _wc_tmp  # type: ignore
                    wmi_tmp = _wc_tmp.GetObject("winmgmts:root\Microsoft\Windows\Storage")
                    qvt = wmi_tmp.ExecQuery("SELECT DriveLetter, FileSystemLabel, Size FROM MSFT_Volume")
                    for _vt in qvt:
                        try:
                            dlc = getattr(_vt, "DriveLetter", None)
                            if dlc is None:
                                continue
                            dl_int = int(dlc)
                            if not dl_int:
                                continue
                            lt = f"{chr(dl_int).upper()}:"
                            vl[lt] = str(getattr(_vt, "FileSystemLabel", "") or "").strip()
                            sz = getattr(_vt, "Size", None)
                            if sz is not None:
                                vs[lt] = int(sz)
                        except Exception:
                            continue
                except Exception:
                    pass
            for letter, pd in list(_drive_to_physical.items()):
                if pd not in per_disk:
                    continue
                lab = vl.get(letter, "")
                sz = vs.get(letter)
                size_gb = (sz / (1024**3)) if sz else None
                if lab:
                    drive_labels[pd] = f"{letter} {lab}"
                elif size_gb:
                    drive_labels[pd] = f"{letter} {size_gb/1024:.0f}T" if size_gb >= 900 else f"{letter} {size_gb:.0f}G"
                else:
                    drive_labels[pd] = letter
        except Exception:
            pass
        # 温度（仅非休眠且管理员才读）。
        # 面板不可见时跳过 Storage.Update()：任何 SMART 查询都要过 storport
        # 等盘中断返回，游戏中表现为一次系统级抖动；CPU 走 MSR 不受影响。
        # skip_temps（用户关掉温度显示）时连 MSR/WMI 都不走，字段留 None。
        temp_map: dict[str, float] = {}
        if m.is_admin and not skip_disk_temp and not skip_temps:
            _t_lhm = time.perf_counter()
            raw_temps = _get_lhm_temps_if_admin(sleeping_map, idle_hdd_pds)
            _last_timing["lhm"] = time.perf_counter() - _t_lhm
            pkg = raw_temps.get("cpu:CPU Package")
            if pkg is None:
                pkg = raw_temps.get("cpu:Core Max")
                if pkg is None:
                    for k, v in raw_temps.items():
                        if k.startswith("cpu:"):
                            pkg = v
                            break
            m.cpu_temp = pkg
            if m.cpu_temp is None:
                # ring0 全灭（HVCI）时走 WMI 兜底
                m.cpu_temp, _src = _cpu_temp_wmi_fallback()
                _cpu_temp_source = _src or "none"
            else:
                _cpu_temp_source = "lhm/msr"
            for k, v in raw_temps.items():
                if k.startswith("disk:"):
                    temp_map[k] = v
        elif m.is_admin and skip_disk_temp and not skip_temps:
            # 不可见：CPU 仍走 MSR 直读（微秒级，不碰 IO 栈），盘温沿用空
            # （用户看不到，下一轮可见时自动刷新；不进 LHM，不碰盘）。
            _last_timing["lhm"] = 0.0
            try:
                m.cpu_temp = _cpu_package_via_msr()
            except Exception:
                m.cpu_temp = None
            if m.cpu_temp is None:
                m.cpu_temp, _src = _cpu_temp_wmi_fallback()
                _cpu_temp_source = _src or "none"
            else:
                _cpu_temp_source = "lhm/msr"

        for pd_name in sorted(per_disk.keys(), key=lambda x: int(x.replace("PhysicalDrive", "")) if x.replace("PhysicalDrive", "").isdigit() else 999):
            raw_label = _physical_to_label.get(pd_name, pd_name)
            cleaned = _clean_label(raw_label)
            # 桥接芯片名回退为盘符标签
            if _is_bridge_name(cleaned):
                # 本机实测 PD2 就是 Realtek 桥接->应显示为 Y:
                fallback = drive_labels.get(pd_name)
                if fallback:
                    cleaned = fallback
                else:
                    # 再用 Media/Bus 提示
                    mt = _physical_to_media.get(pd_name, "")
                    cleaned = f"USB-{mt or 'SSD'} ({pd_name})"
            else:
                # 非桥接情况，附加盘符便于识别多盘
                dl_hint = drive_labels.get(pd_name)
                # 仅当名字未含盘符信息时附加，避免 "D: 16T" 重复
                if dl_hint and dl_hint.split()[0] not in cleaned:
                    cleaned = f"{cleaned} ({dl_hint.split()[0]})"

            sleeping = sleeping_map.get(pd_name, False)

            temp = None
            if not sleeping and m.is_admin and temp_map:
                for k, v in temp_map.items():
                    parts = k.split(":")
                    if len(parts) >= 2:
                        hw_name = parts[1]
                        if match_disk_name(raw_label, hw_name) or match_disk_name(cleaned, hw_name):
                            if "Composite" in k or "Temperature =" in k:
                                temp = v
                                break
                            elif temp is None:
                                temp = v

            total_gb = _physical_to_size_gb.get(pd_name)
            rate = _tracker.rates.get(pd_name, (0.0, 0.0))
            m.disks.append(
                DiskInfo(
                    name=cleaned,
                    busy=None if sleeping else busy_map.get(pd_name, 0.0),
                    temp=temp,
                    sleeping=sleeping,
                    total_gb=total_gb,
                    read_bps=rate[0],
                    write_bps=rate[1],
                )
            )
        if not m.disks:
            for part in _psutil.disk_partitions():
                m.disks.append(DiskInfo(name=part.device, busy=0.0, temp=None, sleeping=False))
    except Exception:
        pass

    _last_timing["total"] = time.perf_counter() - _t0
    global _collect_count
    _collect_count += 1
    if _collect_count % 20 == 1:
        if skip_temps:
            print("[temp] off (右键菜单关闭了温度显示，一个温度都不读)")
        else:
            print(f"[temp] cpu={m.cpu_temp} src={_cpu_temp_source} admin={m.is_admin}")
    return m


def warmup():
    """提前做一次无害的 psutil 预热与 WMI 盘名刷新，不弹权限。"""
    _ensure_imports()
    assert _psutil is not None
    try:
        _psutil.cpu_percent(interval=None)
        per = _psutil.disk_io_counters(perdisk=True) or {}
        _tracker.prime(per)
        _net_tracker.prime()
        _refresh_disk_labels()
    except Exception:
        pass
    # MSR 预热：PawnIO 驱动首次打开可能慢，提前建好，首帧就不走 240ms 回退了
    try:
        for _ in range(3):
            if _cpu_package_via_msr() is not None:
                break
    except Exception:
        pass
