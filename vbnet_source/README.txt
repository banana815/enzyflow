VB.NET 源码放置目录

本次适配时（2026-11-03）在 enzyflow/ 下没有找到 .vb、.vbproj 或 .sln
文件。因此 spectrometer/ 是按 PySpectrometer2、spectro-cam-rs 和
Theremino/小圃类软件的功能模型独立实现的 Linux 版本。

如果要把你原来的 VB.NET 软件逐项移植过来，请把以下任意一种放到本目录：

  - 完整的 Visual Studio 解决方案（.sln + .vbproj + 所有 .vb/.resx）
  - 或者只放关键窗体/模块（Form1.vb、采集模块、标定模块、文件格式模块）
  - 以及原软件的说明、示例数据、标定文件（如果有）

放好后可以对照：
  ../spectrometer/docs/VBNET_TO_PYTHON.md

注意：如果原软件是 Theremino 的派生版本，请同时保留它的 LICENSE；
GPL 代码不能直接闭源分发。
