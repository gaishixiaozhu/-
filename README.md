# 蜻蜓龙虾助手 (Qingting Lobster Assistant)

高考志愿填报 AI 助手 - 基于 DeepSeek + FastAPI + Capacitor 的全栈应用。

## 功能
- 🎯 智能志愿推荐（冲/稳/保三段式）
- 📊 结构化表格展示（历年录取分/位次/计划/风险等级）
- 🧠 多轮对话上下文记忆
- 📱 PWA + Android APK 双端支持
- 📥 志愿单导出

## 技术栈
- **后端**: FastAPI + uvicorn + MySQL (RDS)
- **AI**: DeepSeek API (deepseek-chat / deepseek-v4-flash)
- **前端**: 单文件 HTML/CSS/JS (无框架)
- **移动端**: Capacitor (Android 7.0+)

## 快速部署
```bash
pip install -r requirements.txt
python server_openclaw.py
```

## 文件说明
| 文件 | 说明 |
|------|------|
| `server_openclaw.py` | FastAPI 服务端主程序 |
| `index.html` | 前端页面（SPA） |
| `api_key_validator.py` | API Key 验证模块 |
| `recommendation_engine.py` | 志愿推荐算法 |
| `start.sh` | 服务启动脚本 |
| `pwa/` | PWA manifest + Service Worker |
| `app-build/` | Capacitor Android 构建工程 |
