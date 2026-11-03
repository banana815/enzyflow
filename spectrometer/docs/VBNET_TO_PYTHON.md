# 从 VB.NET 到 Python/OpenCV 的对照表

这份文档写给“原来有一套 VB.NET 光谱仪软件，现在想在 Linux 电脑上做二次
开发”的情况。当前 `enzyflow/` 里没有找到 `.vb` / `.vbproj` / `.sln`，
所以下面不是逐行翻译，而是把 VB.NET 软件里常见的模块映射到本项目的
Python 模块。把源码放进 `enzyflow/vbnet_source/` 后，可以按这张表逐项
核对。

## 0. 先确认你的 VB.NET 软件属于哪一类

| 类型 | 典型特征 | 本项目的对应 |
|---|---|---|
| Theremino Spectrometer 或其派生 | 窗口标题 Theremino，GPL-3.0，使用摄像头/线阵传感器，功能多 | 功能模型参考；**不要直接复制 GPL 代码到闭源项目** |
| 小圃光谱仪配套软件 | 中文界面，B 站作者“窗台小圃”的教程，可能基于 Theremino | 先确认许可证和文件格式，再决定导入/重写 |
| 自研 VB.NET 采集程序 | 自己写的 `Form1.vb`、`SerialPort`、`Bitmap` | 按下面的表逐项移植 |

## 1. 界面/采集

| VB.NET 里常见写法 | Python/OpenCV 对应 | 本项目位置 |
|---|---|---|
| `PictureBox.Image = bitmap` | `cv2.imshow()` + `np.ndarray` | `app.py` |
| `Bitmap.LockBits` / `GetPixel` | `frame[y:y+h, x:x+w]` 切片，NumPy 向量化 | `roi.py`, `spectrum.py` |
| `Timer1_Tick` + `Application.DoEvents()` | `while running:` + `cv2.waitKey(1)` | `app.py::run` |
| `OpenFileDialog` 选视频 | `--video path` / `VideoFileSource` | `camera.py` |
| `SaveFileDialog` 保存图片 | `--out` / `cv2.imwrite` | `recording.py` |
| `DataGridView` 显示数据 | CSV 用 pandas/LibreOffice 打开 | `recording.py` |
| `Chart` / `MSChart` 画曲线 | OpenCV `cv2.line` 画彩色曲线 | `app.py`, `plot.py` |
| `My.Settings` 保存设置 | `runtime/config.json` | `config.py` |
| `Color.FromArgb(r,g,b)` | `wavelength_to_rgb()` 返回 RGB | `color.py` |
| `SerialPort` 读传感器 | 需要新建 `serial_source.py`（本项目暂未接入 ESP32 串口） | 待扩展 |

## 2. 光谱处理

| VB.NET 概念 | Python 对应 | 本项目位置 |
|---|---|---|
| 每列取平均/多行平均 | `crop.mean(axis=0)` | `spectrum.py::_extract` |
| 多帧平均 | `collections.deque(maxlen=N)` | `spectrum.py::SpectrumProcessor` |
| 暗背景/零参考 | 逐像素减法 | `z` 键 / `set_zero_reference()` |
| 峰值保持 | `np.maximum(hold, current)` | `h` 键 |
| 平滑 | Savitzky-Golay | `peaks.py::savitzky_golay` |
| 峰查找 | 局部极大值 + 最小间距 + 阈值 | `peaks.py::find_peaks` |
| 波形显示颜色 | 波长 → 近似 RGB | `color.py` |
| 积分时间/增益 | V4L2 `exposure_time_absolute`、`gain` | `camera.py` |
| 饱和判断 | 像素 ≥ 250 的比例 | `spectrum.py` |

## 3. 标定

| VB.NET 概念 | Python 对应 | 本项目位置 |
|---|---|---|
| 两个标定点做线性插值 | `Calibration.fit(..., degree=1)` | `calibration.py` |
| 三个点二次拟合 | 默认 2 阶 | `calibration.py` |
| 四点以上三次拟合 | 默认 3 阶 | `calibration.py` |
| 在图上点击峰记录像素 | `p` 键 + 鼠标回调 | `app.py::_mouse` |
| 输入已知波长 | `c` 键后在终端输入 | `app.py::_calibrate` |
| 标定文件 | `runtime/calibration.json` | `calibration.py` |
| 旧格式 `caldata.txt` | `from_caldata_text` / `to_caldata_text` | `calibration.py` |
| 像素→波长数组 | `np.polyval(coeff, pixels)` | `Calibration.wavelengths` |

注意：本项目的标定点是**整幅画面绝对 x**；PySpectrometer2 的
`caldata.txt` 是相对采集宽度的 x。导入/导出时用 `roi_x` 参数换算。

## 4. 颜色/色度

| VB.NET/Theremino 功能 | 当前状态 | 建议扩展 |
|---|---|---|
| 波长 → 显示颜色 | 已有近似算法 | `color.py` |
| CIE 1931 XYZ / xy 色坐标 | 未实现 | 新建 `colorimetry.py` |
| 色温 CCT | 未实现 | 用 McCamy 近似或查表 |
| 显色指数 CRI/Ra | 未实现 | 需要 CIE 1995 方法 + TCS 光谱 |
| 绝对光谱辐照度 | 未实现，也不建议用 8 位摄像头做 | 需要标准灯 + 积分球/余弦校正 |

做色度计算前必须明确：相机 RGB 不是 CIE XYZ，必须先把光谱功率分布
（相对值即可）乘以 CIE 颜色匹配函数再积分。没有波长标定时不要做色度。

## 5. 移植顺序（建议）

1. **先对齐采集**：确认 VB.NET 里的分辨率、曝光、增益、平均帧数；
2. **再对齐像素坐标**：原来的 x 是否包含黑边/裁剪；
3. **再对齐标定**：把原软件的标定数组/多项式抄出来，比较同一张图的
   波长轴；
4. **再对齐处理**：平滑窗口、峰阈值、峰间距；
5. **最后对齐输出**：CSV 列名、单位、文件名、小数位数。

每一步都用同一张保存下来的原始帧做对比，不要只凭肉眼看看窗口。

## 6. 许可证提醒

- Theremino Spectrometer 的公开源码包通常标注为 **GPL-3.0**（以你拿到的
  源码包里的 LICENSE 为准）。如果你把它的 VB.NET 代码翻译成 Python 并
  分发，通常属于派生作品，需要按 GPL 发布。
- PySpectrometer2 是 **Apache-2.0**，允许商用/修改，但要保留 NOTICE/
  许可证声明。
- spectro-cam-rs 是 **Apache-2.0 或 MIT** 双许可。
- 本项目的 `spectro/` 是独立实现，不包含上述项目的完整源码；算法来源
  已在 `THIRD_PARTY_NOTICES.md` 标注。若你后续直接复制 GPL 代码，请
  自行调整项目许可证。
