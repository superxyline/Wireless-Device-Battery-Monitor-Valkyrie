"""Valkyrie (VGN 子品牌) 2.4G 接收器电量读取驱动。

协议为 USBPcap 抓包逆向结果（2026-10-24，硬件实测）：

- 通道：接收器接口 1 上 usage page 0xFF02 / usage 0x0002 的 vendor 集合；
- 帧格式：报告 ID 0x13，定长 20 字节，最后一字节 = 前 19 字节求和 mod 256；
- 主机查询：cmd 0x07（`13 07 01 00 ... + 校验和`），设备 2ms 内原样回带
  `13 07 01 00 01 ...`，仅用于在线探测，不含电量；
- 设备推送（cmd 0x0A）：
  - `13 0A 01 00 04 02 <0/1> ...` 链路状态（休眠/唤醒翻转）；
  - `13 0A 01 00 04 05 <电量%> <电压原始值> ...` 电量事件；
    电压原始值实测 0x11 -> 0x10 连续回落，按 250mV/步 换算约 4.25V/4.0V。
- 电量没有按需查询命令，只能监听推送并缓存（实测约数分钟一次，
  与鼠标使用/唤醒事件相关）。

因此本驱动采用"常驻监听线程 + 缓存 + ping 探测在线"模型。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass

import hid

logger = logging.getLogger(__name__)

VALKYRIE_VID = 0x3554
VALKYRIE_DONGLE_PIDS = {0xFA09: "Valkyrie M5"}
_USAGE_PAGE = 0xFF02
_USAGE = 0x0002
_FRAME_ID = 0x13

_CACHE_FILE = os.path.join(tempfile.gettempdir(), "valkyrie_battery_cache.json")


@dataclass
class ValkyrieBatteryInfo:
    """与 RazerBatteryInfo 形状兼容的电量快照。"""

    percentage: int = 0
    charging: bool = False
    status_text: str = "未知"
    voltage_mv: int | None = None
    online: bool = True


def _checksum(frame19) -> int:
    return sum(frame19) & 0xFF


def _build_ping() -> bytes:
    f = [0x13, 0x07, 0x01] + [0] * 17
    f[19] = _checksum(f[:19])
    return bytes(f)


def _cache_load() -> dict:
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return {}


def _cache_save(data: dict) -> None:
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as fp:
            json.dump(data, fp)
    except Exception:
        pass


def find_valkyrie_devices() -> list[dict]:
    """枚举在线的瓦尔基里接收器（0xFF02 vendor 集合）。"""
    out = []
    for d in hid.enumerate(VALKYRIE_VID):
        if d.get("product_id") not in VALKYRIE_DONGLE_PIDS:
            continue
        if d.get("usage_page") != _USAGE_PAGE or d.get("usage") != _USAGE:
            continue
        out.append(d)
    return out


class ValkyrieDevice:
    """单个瓦尔基里接收器后端，监听电量推送并缓存。"""

    def __init__(self, dev_info: dict):
        self.device_info = {
            "vendor_id": dev_info.get("vendor_id"),
            "product_id": dev_info.get("product_id"),
            "path": dev_info.get("path"),
            "serial_number": dev_info.get("serial_number"),
        }
        self.path = dev_info.get("path")
        self.product_string = VALKYRIE_DONGLE_PIDS.get(dev_info.get("product_id"), "Valkyrie")
        self._reader: hid.device | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # 缓存：last_push 为最近一次电量事件帧（list[int]），last_time 为收到时刻
        self._last_push: list[int] | None = None
        self._last_push_time: float = 0.0
        self._last_link: int | None = None
        self._link_time: float = 0.0
        self._presence: bool | None = None  # ping 结果
        self._presence_time: float = 0.0
        self._load_cache()

    # -- 缓存 ---------------------------------------------------------------
    def _load_cache(self) -> None:
        data = _cache_load()
        push = data.get("last_push")
        ts = float(data.get("last_push_time") or 0)
        if isinstance(push, list) and len(push) == 20 and _checksum(push[:19]) == push[19]:
            self._last_push = push
            self._last_push_time = ts

    def _save_cache(self) -> None:
        if self._last_push is not None:
            _cache_save({
                "last_push": self._last_push,
                "last_push_time": self._last_push_time,
            })

    # -- 生命周期 -----------------------------------------------------------
    def open(self) -> bool:
        try:
            self._reader = hid.device()
            self._reader.open_path(self.path)
            self._reader.set_nonblocking(True)
        except Exception as e:
            logger.warning(f"打开瓦尔基里接收器失败: {e}")
            self._reader = None
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._listen_loop, daemon=True,
                                        name="valkyrie-battery-listener")
        self._thread.start()
        logger.info(f"瓦尔基里监听线程已启动: {self.product_string}")
        return True

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._reader is not None:
            try:
                self._reader.close()
            except Exception:
                pass
            self._reader = None

    # -- 协议 ---------------------------------------------------------------
    def _send_output(self, data: bytes) -> bool:
        """中断 OUT 管道直写（0x13 帧在该集合有定义的 OUTPUT 报告）。"""
        if self._reader is None:
            return False
        try:
            return self._reader.write(list(data)) > 0
        except Exception as e:
            logger.debug(f"瓦尔基里写入失败: {e}")
            return False

    def _listen_loop(self) -> None:
        while not self._stop.is_set():
            try:
                r = self._reader.read(64, timeout_ms=200)
            except Exception:
                if self._stop.is_set():
                    break
                time.sleep(0.5)
                continue
            if not r or r[0] != _FRAME_ID or len(r) < 8:
                continue
            frame = list(r)[:20]
            if _checksum(frame[:19]) != frame[19]:
                continue
            with self._lock:
                if frame[1] == 0x07:
                    self._presence = True
                    self._presence_time = time.time()
                elif frame[1] == 0x0A and frame[4] == 0x04:
                    if frame[5] == 0x05:
                        self._last_push = frame
                        self._last_push_time = time.time()
                        self._save_cache()
                        logger.info(f"瓦尔基里电量推送: {frame[6]}%")
                    elif frame[5] == 0x02:
                        self._last_link = frame[6]
                        self._link_time = time.time()

    # -- 对外接口 -----------------------------------------------------------
    def ping(self, timeout: float = 0.8) -> bool:
        """发送 0x07 在线探测。"""
        if not self._send_output(_build_ping()):
            return False
        start = time.time()
        while time.time() - start < timeout:
            with self._lock:
                if self._presence_time >= start:
                    return bool(self._presence)
            time.sleep(0.05)
        return False

    def get_battery(self) -> ValkyrieBatteryInfo | None:
        """返回缓存电量；离线返回 None（公开壳按无响应处理）。"""
        online = self.ping()
        with self._lock:
            push, push_time = self._last_push, self._last_push_time
        if not online:
            return None
        if push is None:
            return None
        percent = push[6]
        volt_raw = push[7]
        voltage_mv = volt_raw * 250 if 0 < volt_raw < 0x40 else None
        stale_min = (time.time() - push_time) / 60 if push_time else 1e9
        if stale_min > 60:
            status = "电量数据较旧（等待新推送）"
        else:
            status = "电量正常"
        return ValkyrieBatteryInfo(
            percentage=percent if 0 <= percent <= 100 else 0,
            charging=False,
            status_text=status,
            voltage_mv=voltage_mv,
            online=True,
        )
