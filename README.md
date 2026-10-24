# Wireless Device Battery Monitor - Valkyrie M5 自用定制版

> **声明：本项目为个人自用定制版（unofficial personal fork），仅供学习交流与个人使用，严禁任何商业用途。**
> 上游项目：[ZGMFX01A/Wireless-Device-Battery-Monitor](https://github.com/ZGMFX01A/Wireless-Device-Battery-Monitor)（LICENSE: Non-Commercial），感谢原作者的优秀工作。
> 本仓库版本号跟随上游（v2.7.1），不单独发版，不提供构建产物。

## 本定制版改了什么

1. **新增 瓦尔基里 M5 Loki（Valkyrie M5，2.4G 接收器 `VID 0x3554 / PID 0xFA09`）电量显示**
   - 自研驱动：`mouse_battery_core/valkyrie_hid.py`
   - 协议通过 USBPcap 抓包逆向（原作者未覆盖该品牌，社区通用 VGN/Nordic52 协议与 M5 不兼容）：
     - 通道：接收器接口 1 的 vendor 集合（usage page `0xFF02`）
     - 帧格式：报告 ID `0x13`、20 字节定长、校验和 = 前 19 字节求和 mod 256
     - 主机 ping：`13 07 01 00 ...`（中断 OUT 直写，设备秒回应答）
     - 电量：设备主动推送 `13 0A 01 00 04 05 <电量%> <电压原始值> ...`（无按需查询命令，故驱动为
       **常驻监听线程 + 本地缓存 + ping 在线探测** 模型，电量推送约数分钟一次）
2. **托盘面板常驻**：点击托盘图标弹出的电量面板不再因失焦自动收起（配置键 `flyout_keep_visible`，
   默认开启；改回 `false` 可恢复上游"失焦收起"行为），再次点击托盘图标或面板关闭按钮可手动收起。

## 重要：私有核心不在本仓库

上游是"公开壳 + 私有核心"结构：所有协议实现在作者闭源的 `mouse_battery_core` 包里（本仓库仅含
自研的 `valkyrie_hid.py`）。**运行前需要你本地从官方 Release 的 exe 中提取私有核心**（个人学习用途）：

```bash
# 1) 下载 pyinstxtractor.py（https://github.com/extremecoders-re/pyinstxtractor）
python pyinstxtractor.py WirelessDeviceBatteryMonitor-v2.7.1.exe

# 2) 把私有核心拷进本仓库（需 Python 3.12，与官方打包版本一致才能直接无源码导入）
cp WirelessDeviceBatteryMonitor-v2.7.1.exe_extracted/PYZ.pyz_extracted/mouse_battery_core/*.pyc \
   mouse_battery_core/
```

## 运行

```bash
pip install hidapi Pillow pystray pywebview
# 蓝牙功能依赖 pywinrt（版本必须互相配套，混装会报 _C_API 错误）：
pip install "winrt-runtime==3.2.1.0" \
    "winrt-Windows.Devices.Bluetooth==3.2.1" \
    "winrt-Windows.Devices.Bluetooth.GenericAttributeProfile==3.2.1" \
    "winrt-Windows.Devices.Enumeration==3.2.1" \
    "winrt-Windows.Foundation==3.2.1" \
    "winrt-Windows.Foundation.Collections==3.2.1" \
    "winrt-Windows.Storage.Streams==3.2.1"

python main.py            # 托盘常驻
python main.py --settings # 打开设置窗口
python main.py --smoke-test # 无头自检
```

## 已知限制（自用备注）

- 瓦尔基里 M5 电量为"推送缓存"模型：首次显示需等设备推送（约数分钟，与使用/唤醒相关），缓存
  存于临时目录 `valkyrie_battery_cache.json`，重启不丢。
- M5 的 `0xFF04` feature report（`06 00 02 64 64 64 A5 94`）经实测为静态出厂数据，不是电量。
- Leobog HI75C 键盘、漫步者 HECATE G4S PRO 耳机：官方软件不支持 / 使用非标准厂商控制请求
  （bRequest 0x81），用户态无法读取，未支持；两者均可通过蓝牙模式的 GATT 180F 标准电量服务显示。

## 隐私说明

本仓库不含任何令牌、密钥、个人配置与本地路径；`config.json` 等运行时数据全部存于用户 AppData/临时目录，不入库。
