#!/bin/bash
# Server OpenClaw 启动脚本

cd "$(dirname "$0")"

# 加载环境变量
if [ -f .env ]; then
    export $(cat .env | grep -v '^#' | xargs)
fi

# 默认值
export DB_TYPE="${DB_TYPE:-mysql}"
export PORT="${PORT:-5007}"

echo "=========================================="
echo "蜻蜓志愿助手 Server OpenClaw"
echo "=========================================="
echo "数据库类型: $DB_TYPE"
echo "端口: $PORT"
echo "=========================================="

# 安装依赖
pip install -r requirements.txt -q

# 启动服务
python3 server_openclaw.py
