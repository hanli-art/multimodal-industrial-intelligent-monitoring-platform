# 多模态工业智能监控平台

面向工业厂区安全生产的智能监控系统：在 AI 识别核心之上，逐步实现实时监控、AI 告警、违规取证、设备管理、统计报表、权限与系统配置等功能。

## 技术栈

- 后端：FastAPI + SQLite（Python 标准库 `sqlite3`）
- 前端：原生 HTML / CSS / JavaScript，图表用 ECharts（CDN 引入）
- 视频源：本地视频文件 / PC 摄像头
- AI 能力：YOLOv8m 目标检测 + 通义千问 VL 大模型场景安全分析

## 环境准备

1. 安装 Python 3.10+，创建并激活虚拟环境：

```bash
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux
```

2. 安装依赖：

```bash
pip install -r requirements.txt
```

3. 获取 YOLO 模型权重 `yolov8m.pt`，放到项目根目录：

```bash
pip install ultralytics
yolo predict model=yolov8m.pt    # 会自动下载权重到当前目录
```

   或手动从 [Ultralytics](https://docs.ultralytics.com/models/yolov8/) 下载 `yolov8m.pt` 后拷贝到项目根目录。

4. 配置千问 API Key（可选，仅 `/ws/qwen` 需要）：

   复制 `.env.example` 为 `.env`，填入 `DASHSCOPE_API_KEY`；或在系统环境变量中设置同名变量。未配置时服务仍可启动，但 `/ws/qwen` 大模型分析不可用。

## 启动

```bash
python server.py
```

服务默认监听 `0.0.0.0:8200`，浏览器访问 <http://localhost:8200> 打开监控页。

## 现有接口

| 接口 | 类型 | 说明 |
|---|---|---|
| `/` | HTTP GET | 监控首页 `index.html` |
| `/ws/detect` | WebSocket | 接收 `base64_image`，YOLOv8m 检测（car / truck / person），返回框坐标与置信度 |
| `/ws/qwen` | WebSocket | 接收 `base64_image`，千问 VL 分析，返回违规文本、违规项列表（抽烟 / 未戴安全帽 / 打架斗殴 / 火灾 / 攀爬围墙）、风险等级（高 / 中 / 低） |
| `/detect` | HTTP POST | 兼容接口，请求体 `{"base64_image": "..."}`，返回 YOLO 检测结果 |
