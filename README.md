# WakeUpMarshall 🎵

自动发现附近的 **Marshall 蓝牙音箱**（如 Woburn III），每隔一段时间（默认 **10 分钟**）
自动把它从休眠状态唤醒，并提供一个**简约的本地 Web 界面**：

- 当前机器的**蓝牙状态**（适配器是否开启）
- 扫描并**绑定 Marshall 音箱**；绑定信息在本机重启应用后仍保留
- 后续手动/自动唤醒直接连接已绑定设备，不必先重新扫描
- 默认仅显示 Marshall/已绑定设备，可按需展开全部扫描结果
- 显示扫描、唤醒的执行阶段，以及连接、可发现和 Linux A2DP 音频流状态
- **一键开关**定时唤醒，可调唤醒间隔
- 显示**最近一次唤醒时间与执行结果**
- **唤醒历史**查看与清空

跨平台：**Linux**（BlueZ `bluetoothctl`）与 **Windows**（BLE，`bleak`）。

---

## 🚀 一键安装

### Linux（Debian/Ubuntu/Fedora/Arch…）

```bash
curl -fsSL https://raw.githubusercontent.com/firelake/WakeUpMarshall/main/install.sh | bash
```

脚本会：克隆仓库 → 创建虚拟环境 → 安装依赖 → 注册 **systemd 用户服务**（开机自启，崩溃自动重启）
→ 启动服务。完成后浏览器打开 <http://127.0.0.1:8756> 即可。

> 没有 systemd 用户会话时自动退化为 `nohup` 后台运行。

### Windows（PowerShell）

```powershell
irm https://raw.githubusercontent.com/firelake/WakeUpMarshall/main/install.ps1 | iex
```

脚本会：获取源码 → 创建虚拟环境 → 安装依赖 → 注册**登录自启计划任务** → 启动并打开浏览器。

> Windows 依赖 `bleak`（BLE）。请确保系统蓝牙适配器已开启。

### 从本地代码安装

```bash
bash install.sh /path/to/WakeUpMarshall        # Linux
powershell -File install.ps1 C:\path\WakeUpMarshall   # Windows
```

---

## 🧑‍💻 手动使用（开发者 / 无安装脚本时）

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install .
wakeupmarshall serve          # 启动调度器 + Web UI（默认 http://127.0.0.1:8756）
```

常用命令：

| 命令 | 说明 |
|---|---|
| `wakeupmarshall serve` | 启动调度器与 Web UI（默认命令） |
| `wakeupmarshall once` | 立即执行一次唤醒并退出（exit 0 = 成功/已唤醒） |
| `wakeupmarshall scan` | 扫描一次并列出设备 |
| `wakeupmarshall status` | 查看适配器 / 设备 / 调度器 / 最近唤醒 |
| `wakeupmarshall history [--clear]` | 查看 / 清空唤醒历史 |
| `wakeupmarshall toggle on\|off` | 开启 / 关闭定时唤醒 |
| `wakeupmarshall settings --interval 15 --enabled yes` | 修改设置 |

常用选项：`--port 9000`、`--interval 15`、`--no-wake`、`--backend auto|bluetoothctl|bleak|fake`、`--data-dir PATH`。

## ⚙️ 工作原理

1. 首次使用时在 Web UI 点击**重新扫描**，找到 Marshall 音箱后点击**绑定**。绑定记录写入本机数据目录。
2. 后台调度线程每 N 分钟（默认 10）执行一轮唤醒：
   - 读取适配器状态（`bluetoothctl show` / bleak）
   - 已有绑定设备时，按保存的设备地址直接执行**连接**（`bluetoothctl connect` / BLE connect），不扫描
   - 没有绑定设备时，保留三轮扫描和名称匹配作为兼容回退
3. 每轮结果写入历史（`~/.wakeupmarshall/history.json`，最多保留 500 条）。
4. Web 界面每 1.5 秒轮询 `/api/status` 展示执行阶段和设备状态。

### 设备状态能力

- **连接状态**：Linux/BlueZ 可通过 `org.bluez.Device1.Connected`（由 `bluetoothctl info` 暴露）定期刷新。
- **正在输出音频**：Linux 且系统提供 `busctl` 时，可读取 BlueZ `org.bluez.MediaTransport1.State`；`active` 显示为正在输出音频。Windows 使用的 Bleak 仅支持 BLE/GATT，无法读取 Bluetooth Classic A2DP 状态。
- **休眠/激活**：通用蓝牙接口没有设备电源或休眠属性。设备不可见/连接失败也可能表示关机、离线或超出范围，因此界面不会把不可达误报为“休眠”。

参考：[BlueZ Device API](https://manpages.ubuntu.com/manpages/noble/man5/org.bluez.Device.5.html)、[BlueZ MediaTransport API](https://man.archlinux.org/man/extra/bluez-utils/org.bluez.MediaTransport.5.en)、[Bleak 文档](https://bleak.readthedocs.io/en/latest/)。

## 🔌 HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | Web UI |
| GET | `/api/status` | 适配器 / 设备 / 调度器 / 最近唤醒 |
| POST | `/api/wake` | 立即唤醒（异步） |
| POST | `/api/scan` | 立即扫描（异步） |
| POST | `/api/devices/bind` | 绑定最近扫描发现的 Marshall 设备（JSON：`address`） |
| DELETE | `/api/devices/{address}` | 删除已保存的设备绑定 |
| GET | `/api/settings` | 读取设置 |
| PUT | `/api/settings` | 更新设置（`enabled`、`interval_minutes`、`scan_timeout`…） |
| GET | `/api/history` | 唤醒历史（最新在前） |
| DELETE | `/api/history` | 清空历史 |

## 📁 数据目录（默认 `~/.wakeupmarshall`）

- `settings.json` — 设置（开关、间隔、关键字、端口、后端）
- `history.json` — 唤醒历史
- `devices.json` — 已绑定设备的地址、名称、绑定时间和最近发现时间
- `app/` — 源码（安装脚本克隆）
- `venv/` — Python 虚拟环境
- `server.log` — 运行日志（非 systemd 时）

## 🧪 测试与演示

```bash
pip install -e .[test]
python -m pytest tests/
wakeupmarshall serve --backend fake    # 无硬件演示 UI / 调度器
```

## 🩺 故障排查

- **界面显示"蓝牙已关闭"**：`rfkill unblock bluetooth`、`systemctl start bluetooth`，或在系统设置里开启蓝牙。
- **扫描不到音箱**：Marshall 音箱深度待机时蓝牙射频关闭，需要先按电源键 / 顶部**蓝牙按钮约 2 秒**进入配对模式（LED 闪烁），应用会自动发现并连接唤醒。唤醒后音箱会记住连接，后续定时任务即可在浅休眠下直接连回。
- **Windows 下扫描不到**：确认适配器支持 BLE 且已开启；部分老适配器仅支持经典蓝牙。
- **端口被占用**：`wakeupmarshall settings --port 9000` 后重启。

## 📄 License

MIT — 详见 [LICENSE](LICENSE)。
