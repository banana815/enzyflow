# JSON Lines 数据格式

项目保留两条独立数据流：ESP32/AS7341 的 `esp32.as7341` 和电脑端 UVC 光谱仪的 `pc.usb_spectrometer`。每条 JSONL 记录占一行。下列字段说明依据现有 [板端协议](../firmware/lib/enzyflow/protocol.py)、[板端应用](../firmware/lib/enzyflow/app.py) 和 [电脑端记录模块](../spectrometer/spectro/recording.py) 整理，表示代码行为，不是本次硬件实测结论。

## 公共标识和时间

| 字段 | ESP32/AS7341 | 电脑端 USB 光谱仪 |
|---|---|---|
| `schema` | `1` | `1` |
| `source` | `esp32.as7341` | `pc.usb_spectrometer` |
| `device_id` | ESP32 `unique_id()` 的十六进制字符串 | `camera.path`，例如 `/dev/video2`；合成源为 `synthetic`，缺失时为 `unknown` |
| `seq` | 每次 `run()` 新建 Reporter 后从 `0` 递增，包含状态事件 | 连续采集从 `0` 递增；单次 capture 默认 `0`，不是跨进程全局编号 |
| `event` | 启动、标定、就绪、测量、停止或错误事件 | 连续流通常为 `measurement`，单次保存为 `capture` |
| `t_ms` | `ticks_ms()` 板载毫秒计数，会回绕，**不是 UTC** | 无此字段 |
| `t_monotonic_ms` | 无此字段 | 电脑 `time.monotonic()` 的毫秒值 |
| `t_wall` | 原始记录无此字段 | 电脑本地时间，带时区偏移的 ISO 8601 字符串 |

电脑端时间是在创建记录时取得，不是硬件曝光触发时间。电脑接收 AS7341 记录时应另行记录接收时间，以便近似对齐；不能直接比较板端 `t_ms` 与电脑 `t_monotonic_ms`，也不能把两条流视为已经同步采样。真正同步需要额外的时钟/触发设计。

## ESP32/AS7341

### 事件

| `event` | 附加字段或含义 |
|---|---|
| `starting` | `version`、`led="off"`、`rgb_method="band_proxy_uncalibrated"` |
| `calibrating` | 自动基线标定，`instruction="Keep sensor uncovered"`；固定基线时跳过此事件 |
| `ready` | `baseline`、`blocked_below`、`light_above`、`gain`、`atime`、`astep`、`units="raw_counts"`、`sequential_groups=true` |
| `measurement` | 下表所列测量字段 |
| `stopped` | 捕获到 `Ctrl+C` 后输出 |
| `error` | `message` 记录错误；异常随后继续抛出，串口可能另有回溯或 REPL 文本 |

### 测量字段

| 字段 | 类型与含义 |
|---|---|
| `channels` | 长度为 8 的数组，依次为 F1、F2、…、F8，未校准 ADC counts；不是以 F1 为键的对象 |
| `clear` / `nir` | 两次分组读取的 Clear / NIR 值分别取整数平均，仍是未校准 counts |
| `saturated` | 任一分组原始读数达到 `65535` 时为 `true` |
| `state` | `LIGHT`、`BLOCKED` 或 `SATURATED`，饱和状态优先显示 |
| `signal` | 遮挡判定为真时为 `1`，否则为 `0`；这是串口数据字段，不是物理 GPIO 输出 |
| `rgb_approx` | `LIGHT` 时为 `[r,g,b]` 数组，其他状态为 `null`；未校准的波段分组近似值，按最强分量归一化 |

两组光谱通道是**顺序采集，不是同时采样**。AS7341 的 8 个离散波段不是连续光谱，`rgb_approx` 也不是校准后的 CIE/sRGB 色度结果。接线、默认阈值及运行方式见 [板端固件](firmware.md)。

下面仅为一条**虚构的格式示例，非实测数据**：

```json
{"schema":1,"source":"esp32.as7341","device_id":"001122334455","seq":3,"t_ms":12345,"event":"measurement","state":"LIGHT","signal":0,"rgb_approx":[255,255,255],"channels":[100,100,100,100,100,100,100,100],"clear":1000,"nir":200,"saturated":false}
```

## 电脑端 USB 光谱仪

### 元数据和数组

| 字段 | 含义 |
|---|---|
| `camera` | 来源信息，包括 `kind`、`path`、分辨率、`fps`、`fourcc`；是否真实光谱仍需光路确认 |
| `roi` | `x`、`y`、`w`、`h`，相对整幅画面的像素坐标 |
| `exposure` | `auto`、`value` 和 `units`；`value` 可为 `null`，单位字符串为 `V4L2 exposure_time_absolute (100 us units on this camera)` |
| `calibration` | 标定对象，包括标定点、多项式及标定标识等；没有加载标定时为 `null` |
| `units` | `wavelength="nm"`，`intensity="camera counts 0..255, gain-corrected, not radiometrically calibrated"` |
| `pixel` | 每个采样点的整幅画面绝对 x 坐标数组 |
| `wavelength_nm` | 标定后的波长数组；未标定时整个字段为 `null`，不是空数组，也不以像素值冒充波长 |
| `r` / `g` / `b` | ROI 行平均后，经可选线性化、多帧平均和通道增益处理的各通道数组 |
| `intensity` | 由 `(r+g+b)/3` 合成的相机计数；启用零参考时还会减去参考值，参考响应校正也可能进一步缩放 |
| `display` | 用于显示的数组，可能叠加平滑、峰值保持等处理 |
| `luminance` | 当前帧 ROI 的 OpenCV 灰度行平均值；启用线性化时也会处理 |
| `stats` | 宽度、起始像素、强度范围、峰位置、饱和状态、缓存帧数、零参考和峰值保持状态 |
| `note` / `files` | 记录备注和关联输出文件；连续采集的 `files` 通常为空对象 |

数组按同一像素位置对应，长度等于 ROI 宽度；`wavelength_nm=null` 是未标定的例外。元数据中的 `0..255` 描述相机计数量级；软件增益、零参考或参考校正后数值可能超出该范围，不应据此裁剪。这里没有绝对辐射定标，不能把数值标为 W/m² 或 lux。

单次保存的 JSON sidecar 使用 `event="capture"` 且不包含上述测量数组，数组保存在对应 CSV 中；单次 capture 输出到 JSONL 时包含数组。连续 `--duration` 采集输出 `event="measurement"`。CSV 未标定的 `wavelength_nm` 单元格为空，与 JSONL 的 `null` 表示不同。

下面仅为**虚构的未标定格式示例，非实测数据**；为便于阅读展开排版，实际 JSONL 仍应压成一行：

```json
{
  "schema": 1,
  "source": "pc.usb_spectrometer",
  "device_id": "/dev/video2",
  "seq": 0,
  "t_monotonic_ms": 1234567,
  "t_wall": "2026-11-03T14:00:00.123+08:00",
  "event": "measurement",
  "note": "stream capture",
  "camera": {"kind": "uvc_camera", "path": "/dev/video2", "width": 1280, "height": 720, "fps": 31.0, "fourcc": "MJPG"},
  "roi": {"x": 100, "y": 356, "w": 2, "h": 8},
  "exposure": {"auto": false, "value": 300, "units": "V4L2 exposure_time_absolute (100 us units on this camera)"},
  "calibration": null,
  "units": {"wavelength": "nm", "intensity": "camera counts 0..255, gain-corrected, not radiometrically calibrated"},
  "stats": {"width": 2, "pixel_start": 100, "intensity_min": 20.0, "intensity_max": 30.0, "peak_pixel": 101, "peak_wavelength_nm": null, "saturated": false, "saturated_ratio": 0.0, "buffer_len": 1, "zero_reference": false, "hold_peaks": false},
  "files": {},
  "pixel": [100, 101],
  "wavelength_nm": null,
  "intensity": [20.0, 30.0],
  "display": [20.0, 30.0],
  "r": [20.0, 30.0],
  "g": [20.0, 30.0],
  "b": [20.0, 30.0],
  "luminance": [20.0, 30.0]
}
```

更多保存与标定操作见 [电脑端完整说明](../spectrometer/README.md)。已有实测记录中的画面是室内灯管，不是狭缝光谱；字段完整或 JSON 解析成功不代表光谱测量已验证。

[返回文档索引](README.md) · [返回项目总览](../README.md)
