# RapidOCR 游戏更新器

面向 Windows 的游戏更新工具：通过 UI Automation 与 RapidOCR 识别官方启动器中的按钮和更新状态，并提供 MuMu 模拟器的 APK 更新流程。

## 功能

- 按配置串行处理米哈游、鸣潮、鹰角和异环启动器。
- UIA、OCR 与可选模板匹配配合，识别启动器更新弹窗、下载进度和完成状态。
- MuMu 手游从所配置的官方接口探测版本、下载 APK 并通过 ADB 安装。
- 支持 Windows / APK 分开运行、预演模式和可选 Server 酱结果通知。

实际支持情况取决于启动器版本、语言和界面布局。异环采用观察模式，但启动器更新弹窗仍可能确认；不保证后续版本持续兼容。

## 安装

Windows 10/11，建议 Python 3.13 x64。在仓库目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item updater/win-games.example.yaml updater/win-games.yaml
Copy-Item apkupd/game-update.example.yaml apkupd/game-update.yaml
```

两个 YAML 中的 `C:/Games/REPLACE_ME/` 均须改为自己的路径。配置 `plan`、`vendors` 和 `mumu`，只保留需要处理的游戏；通知默认关闭，真实 SendKey 不要提交。

依赖清单来自原工作环境，包含 `onnxruntime-gpu`。CUDA/cuDNN 需与运行时匹配；OCR 会尝试可用执行器，必要时回退 CPU，可设置 `$env:OCR_ACCELERATOR='cpu'`。不附带 Python 环境或模型文件；首次 OCR 初始化可能需要下载模型。依赖尚未完成全新机器安装验证。

## 使用

```powershell
.\.venv\Scripts\python.exe run_all.py --dry --win-only
.\.venv\Scripts\python.exe run_all.py --win-only
.\.venv\Scripts\python.exe run_all.py --apk-only
.\.venv\Scripts\python.exe run_all.py
```

先预演检查路径，再执行更新。正式执行会操作鼠标、启动或关闭相关进程，并可能安装 APK；请在已登录的交互式桌面运行。将鼠标移到左上角可触发 PyAutoGUI 的紧急停止。程序和启动器权限等级应一致。

`Template/` 中不附带游戏图标。需要模板兜底时，可自行截取自己的启动器图标，按配置中的文件名放入该目录；未配置素材时该兜底不可用，参数切页与 OCR/UIA 仍是主要路径。

## 验证

复制配置后执行 `.\.venv\Scripts\python.exe -m unittest discover -s tests`。测试验证状态机逻辑，不等于真实启动器升级实测。

## 隐私、许可与致谢

本项目原创部分采用 [MIT License](LICENSE)。第三方依赖和素材遵守各自许可，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。

感谢 **OpenAI GPT** 与 **DeepSeek** 在开发、排查问题和文档整理中的帮助，详见 [致谢](ACKNOWLEDGEMENTS.md)。本项目为个人工具，与游戏厂商、联想或 AI 模型提供方无官方关联。

真实配置、密钥、日志和截图请留在本机，详见 [隐私说明](PRIVACY.md)。首次发布为源码版本，不包含虚拟环境、游戏文件、驱动或预编译程序。
