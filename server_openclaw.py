# -*- coding: utf-8 -*-
"""
Server OpenClaw v5.1 - 志愿填报数据服务API

功能：提供精准的志愿填报数据库查询服务
- 查一分一段表
- 查招生计划
- 查历史分数
- 查院校信息
- 查专业信息
- 志愿推荐

特点：
- LLM生成SQL，精准查询数据库
- 返回结构化数据，供客户端LLM进一步处理
- 支持多种查询类型

作者：蜻蜓生涯
"""

import os
import re
import time
import json
import requests
from typing import Dict, List, Optional, Any
from collections import OrderedDict, defaultdict

from fastapi import FastAPI, Header, HTTPException, File, UploadFile, Form
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import concurrent.futures
import threading
import uuid
import logging
import threading
import pymysql
from pymysql import cursors
try:
    from dbutils.pooled_db import PooledDB
    HAS_DBUTILS = True
except ImportError:
    HAS_DBUTILS = False

# ============ 配置 ============
def load_env():
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip()

load_env()

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "5007"))

# v6.0 连接池/并发配置
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "20"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "60"))
MAX_DB_CONCURRENT = int(os.getenv("MAX_DB_CONCURRENT", "100"))
MAX_LLM_CONCURRENT = int(os.getenv("MAX_LLM_CONCURRENT", "100"))
MAX_LLM_QPS = int(os.getenv("MAX_LLM_QPS", "100"))

# v6.0 结构化日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] qingting - %(message)s")
logger = logging.getLogger("qingting-server")

# 数据库配置
DB_TYPE = os.getenv("DB_TYPE", "mysql")
MYSQL_HOST = os.getenv("MYSQL_HOST", "YOUR_MYSQL_HOST")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "YOUR_MYSQL_USER")
MYSQL_PASS = os.getenv("MYSQL_PASS", "YOUR_MYSQL_PASSWORD")
MYSQL_DB = os.getenv("MYSQL_DB", "clp_base")

# LLM配置
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
LLM_BASE_URL = "https://api.deepseek.com/v1"
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")  # 可配置为 deepseek-reasoner (V4pro)

# ============ FastAPI App ============
from fastapi.responses import FileResponse, HTMLResponse
app = FastAPI(title="蜻蜓志愿数据服务 v5.1", version="5.1.0")

# 静态文件服务（蜻蜓聊天网页端）

# PWA files for Android/HarmonyOS install
@app.get("/manifest.json")
async def pwa_manifest():
    return FileResponse("static/manifest.json", media_type="application/manifest+json")

@app.get("/sw.js")
async def pwa_sw():
    return FileResponse("static/sw.js", media_type="application/javascript")

@app.get("/icon-{size}.png")
async def pwa_icons(size: str):
    import os
    path = f"static/icon-{size}.png"
    if os.path.exists(path):
        return FileResponse(path, media_type="image/png")
    return FileResponse("static/icon-192.png", media_type="image/png")

@app.get("/api/v1/history/{session_id}")
async def get_history(session_id: str):
    with history_lock:
        turns = conversation_history.get(session_id, [])
    return {
        "session_id": session_id, "turn_count": len(turns),
        "turns": [{"question": q, "answer": a[:200], "conditions": c} for q, a, c in turns]
    }

@app.delete("/api/v1/history/{session_id}")
async def clear_history(session_id: str):
    with history_lock:
        if session_id in conversation_history:
            del conversation_history[session_id]
    return {"success": True, "message": f"已清除session {session_id}的历史"}

@app.get("/api/v1/stats")
async def get_stats():
    with history_lock:
        return {"active_sessions": len(conversation_history), "sessions": list(conversation_history.keys())}

# Custom chat route with no-cache headers (fixes DingTalk browser caching)
@app.get("/", response_class=HTMLResponse)
async def chat_root():
    """Root route - redirect to chat"""
    return RedirectResponse(url="/chat/")

@app.get("/chat/", response_class=HTMLResponse)
async def chat_index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(content=html, media_type="text/html; charset=utf-8", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    })

# Serve static files
app.mount("/static", StaticFiles(directory="static", html=False), name="static")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


# ============ 数据模型 ============
class ChatRequest(BaseModel):
    user_id: str
    session_id: str
    question: str

class ChatResponse(BaseModel):
    success: bool
    user_id: str
    session_id: str
    answer: str          # 纯文本答案（兼容旧版）
    intent: str = ""
    conditions: Dict = {}
    data: List[Dict] = []     # 原始数据
    display: Dict = {}         # 🆕 结构化展示数据（给客户端LLM排版用）
    sql_info: List = []
    sources: List[str] = []
    timestamp: str




# ============ 多轮对话历史存储 ============
# 每个session保留最近10轮对话
conversation_history = defaultdict(list)  # session_id -> [(question, answer, conditions), ...]
MAX_HISTORY_TURNS = 10
history_lock = threading.Lock()

def get_conversation_context(session_id: str, max_turns: int = 5) -> str:
    with history_lock:
        turns = conversation_history.get(session_id, [])[-max_turns:]
    if not turns:
        return ""
    ctx = "【历史对话】\n"
    for i, (q, a_short, conds) in enumerate(turns, 1):
        ctx += f"第{i}轮:\n"
        ctx += f"  用户: {q}\n"
        if conds:
            ctx += f"  查询条件: {json.dumps(conds, ensure_ascii=False)}\n"
        a_brief = a_short[:300] + "..." if len(a_short) > 300 else a_short
        ctx += f"  助手: {a_brief}\n\n"
    return ctx

def add_to_history(session_id: str, question: str, answer: str, conditions: Dict = None):
    with history_lock:
        conversation_history[session_id].append((question, answer, conditions or {}))
        if len(conversation_history[session_id]) > MAX_HISTORY_TURNS:
            conversation_history[session_id] = conversation_history[session_id][-MAX_HISTORY_TURNS:]

# ============ 任务状态存储 ============
job_status = {}
job_lock = threading.Lock()

def update_job(job_id: str, status: str, message: str, progress: int):
    with job_lock:
        job_status[job_id] = {
            "status": status, "message": message, "progress": progress,
            "answer": None, "data": [], "intent": "", "conditions": {}
        }

def set_job_result(job_id: str, answer: str, data: List[Dict] = None, intent: str = "", conditions: Dict = None, display: Dict = None):
    with job_lock:
        if job_id in job_status:
            job_status[job_id].update({
                "status": "completed", "message": "查询完成", "progress": 100,
                "answer": answer, "data": data or [], "intent": intent,
                "conditions": conditions or {}, "display": display or {}
            })


# ============ 数据库连接 ============
# v6.0 数据库连接池
class DatabasePool:
    _instance = None
    _lock = threading.Lock()
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init = False
        return cls._instance
    def __init__(self):
        if self._init:
            return
        self._init = True
        if HAS_DBUTILS:
            self._pool = PooledDB(
                creator=pymysql, mincached=DB_POOL_MIN, maxcached=DB_POOL_MAX,
                maxconnections=DB_POOL_MAX + 10, blocking=True, maxusage=1000,
                setsession=['SET NAMES utf8mb4'],
                host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
                password=MYSQL_PASS, database=MYSQL_DB, charset='utf8mb4',
                cursorclass=cursors.DictCursor,
                connect_timeout=5, read_timeout=30, write_timeout=30
            )
            logger.info(f"[DBPool] min={DB_POOL_MIN} max={DB_POOL_MAX}")
        else:
            self._pool = None
    def get_connection(self):
        if self._pool:
            return self._pool.connection()
        return pymysql.connect(
            host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
            password=MYSQL_PASS, database=MYSQL_DB, charset='utf8mb4',
            cursorclass=cursors.DictCursor,
            connect_timeout=5, read_timeout=30, write_timeout=30
        )
    def stats(self):
        return {"type": "PooledDB" if HAS_DBUTILS else "simple", "min": DB_POOL_MIN, "max": DB_POOL_MAX}

db_pool = DatabasePool()
db_semaphore = threading.Semaphore(MAX_DB_CONCURRENT)

def get_db_connection():
    return db_pool.get_connection()


# ============ 数据库Schema ============
SCHEMA = """
## 数据库表结构

⚠️ 本数据库是MySQL，不是SQLite！
- JOIN方式：clp_profession_data_xx.school_id = clp_school.id（不需要CAST）
- 数值比较：直接用 > < = BETWEEN，不需要CAST
- 字符串：单引号包裹，如'辽宁'

### 1. clp_school（院校表）
- id: 院校ID
- school: 院校名称
- school_code: 院校代码
- city: 所在城市
- prov: 所在省份（中文名，如"辽宁"）⚠️ 字段名是prov，不是province！
- school_type: 院校类型（综合/理工/师范/医药等）
- school_level: 办学层次（本科/专科）

### 2. clp_profession_data_{省}（专业录取表）
- school_id: 关联clp_school.id（直接用ON s.id = p.school_id JOIN）
- school_note: ⚠️ 院校备注（区分独立学院/中外合作等），在clp_profession_data表中，不在clp_school中！
- pro: 专业名称
- pro_code: 专业代码
- pro_note: 专业备注
- pro_group: 专业组（新高考省份）
- low_real: 最低录取分
- low_rank_real: 最低录取位次
- avg_real: 平均分
- high_real: 最高分
- plan_num: 招生计划数
- enroll_num: 录取人数
- nature: 科类（首选科目物理/首选科目历史）
- batch: 录取批次
- year: 年份
- tuition: 学费
- edu_system: 学制
- is_real: 是否有实际录取分（1=有）

### 3. clp_score_rank（一分一段表）
- prov: ⚠️ 省份名（中文，如"辽宁""山东"，不是代码！）
- year: 年份
- score: 分数
- rank: 位次
- nature: 科类

### 4. clp_batch_line（批次线）
- prov: ⚠️ 省份名（中文，如"辽宁""山东"，不是代码！）
- year: 年份
- batch: 批次名称
- score: 批次线分数
- nature: 科类

### 省份代码
ln=辽宁, sd=山东, sc=四川, hen=河南, gd=广东, js=江苏, zj=浙江,
heb=河北, hub=湖北, hun=湖南, ah=安徽, fj=福建, jx=江西,
sx=山西, shx=陕西, gs=甘肃, jl=吉林, hlj=黑龙江, bj=北京,
sh=上海, cq=重庆, gz=贵州, yn=云南, gx=广西, han=海南,
nmg=内蒙古, nx=宁夏, qh=青海, xj=新疆, xz=西藏

### 科类字段值（按省份不同！）
- 首选科目物理/首选科目历史：辽宁、四川、河南、广东、江苏、河北、湖北、湖南、安徽、福建、江西、山西、陕西、甘肃、吉林、黑龙江、重庆、贵州、云南、广西、内蒙古、宁夏、青海
- 3+3（不分文理）：山东、浙江、北京、上海、海南
- 文科/理科：新疆、西藏
"""


# ============ SQL执行器 ============
def execute_sql(sql: str, limit: int = 200) -> Dict[str, Any]:
    """执行SQL并返回结果"""
    
    # MySQL保留关键字转义（rank在PolarDB中会报语法错误）
    # 简单替换：rank在SELECT列和WHERE中的情况
    sql = sql.replace(' rank ', ' `rank` ').replace(' rank,', ' `rank`,').replace(' rank\n', ' `rank`\n')
    sql = sql.replace(',rank ', ',`rank` ').replace(',rank,', ',`rank`,').replace(',rank\n', ',`rank`\n')
    sql = sql.replace('SELECT rank ', 'SELECT `rank` ').replace('SELECT rank,', 'SELECT `rank`,')
    
    sql_upper = sql.upper().strip()
    
    # 安全检查
    dangerous = ['DROP', 'DELETE', 'UPDATE', 'INSERT', 'ALTER', 'CREATE', 'TRUNCATE']
    for kw in dangerous:
        if kw in sql_upper:
            return {"success": False, "error": f"禁止执行: {kw}", "data": [], "row_count": 0}
    
    # 添加LIMIT
    if 'LIMIT' not in sql_upper and sql_upper.startswith('SELECT'):
        sql = sql.rstrip(';') + f" LIMIT {limit}"
    
    # v6.0: DB 并发保护
    acquired = db_semaphore.acquire(timeout=30)
    if not acquired:
        return {"success": False, "error": "DB busy, retry later", "data": [], "row_count": 0}
    conn = None
    try:
        conn = get_db_connection()
        import pymysql, sys
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(sql)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        
        # 转换数据
        clean_rows = []
        for row in rows[:limit]:
            clean_row = {}
            for k, v in row.items():
                if isinstance(v, (int, float, str, type(None))):
                    clean_row[k] = v
                elif isinstance(v, bytes):
                    clean_row[k] = v.decode('utf-8', errors='replace')
                else:
                    clean_row[k] = str(v)
            clean_rows.append(clean_row)
        
        return {"success": True, "sql": sql, "row_count": len(clean_rows), "data": clean_rows}
        
    except Exception as e:
        import sys
        print(f"[ERROR] execute_sql failed: {e}", flush=True)
        if conn:
            conn.close()
        return {"success": False, "error": str(e), "data": [], "row_count": 0}


# ============ LLM调用 ============
# v6.0 LLM 限流器
class LLMRateLimiter:
    def __init__(self):
        self._sem = threading.Semaphore(MAX_LLM_CONCURRENT)
        self._window = []
        self._lock = threading.Lock()
    def acquire(self, timeout=30):
        with self._lock:
            now = time.time()
            self._window = [t for t in self._window if now - t < 1.0]
            if len(self._window) >= MAX_LLM_QPS:
                time.sleep(max(0, 1.0 - (now - self._window[0])))
            self._window.append(time.time())
        return self._sem.acquire(timeout=timeout)
    def release(self):
        self._sem.release()
    def stats(self):
        return {"max_concurrent": MAX_LLM_CONCURRENT, "qps_limit": MAX_LLM_QPS}

llm_limiter = LLMRateLimiter()

def call_llm(messages: List[Dict], temperature: float = 0.3, max_retries: int = 3) -> str:
    """v6.0: 限流+重试"""
    if not DEEPSEEK_API_KEY:
        return "⚠️ LLM未配置"
    for attempt in range(max_retries):
        if not llm_limiter.acquire(timeout=60):
            return "⚠️ LLM busy"
        try:
            resp = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                json={"model": LLM_MODEL, "messages": messages, "temperature": temperature, "max_tokens": 4000},
                timeout=90
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
            if resp.status_code == 429:
                time.sleep(min((attempt + 1) * 3, 15))
                continue
            return f"⚠️ LLM: {resp.status_code}"
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(min((attempt + 1) * 2, 10))
                continue
            return f"⚠️ LLM error: {e}"
        finally:
            llm_limiter.release()
    return "⚠️ LLM retries exhausted"


# ============ 意图识别 + SQL生成 ============
def load_memory_as_context() -> str:
    """加载MEMORY.md作为LLM上下文"""
    memory_file = os.path.join(os.path.dirname(__file__), "memory", "MEMORY.md")
    if os.path.exists(memory_file):
        try:
            with open(memory_file, 'r', encoding='utf-8') as f:
                content = f.read()
                # 提取核心规则部分（志愿填报相关），截取前8000字符确保完整覆盖
                if len(content) > 8000:
                    return content[:8000] + "\n...（更多规则请查看完整MEMORY.md）"
                return content
        except Exception as e:
            return f"# MEMORY.md加载失败: {e}"
    return ""

INTENT_TYPES = """
## 支持的意图类型

### 1. query_rank - 查一分一段
用户问：位次、一分一段表、分数对应排名、排名对应分数
SQL: SELECT score, rank FROM clp_score_rank WHERE prov='辽宁' AND year=2025 AND nature=?
-- ⚠️ prov是中文名不是代码！

### 2. query_plan - 查招生计划
用户问：招生计划、招多少人、某校招什么专业、某专业在哪些学校有
SQL: SELECT school, pro, plan_num, tuition FROM clp_profession_data_{省} WHERE ...

### 3. query_score - 查历史分数
用户问：某专业多少分、某校录取分、历年分数、分数走势
SQL: SELECT year, low_real, avg_real FROM clp_profession_data_{省} WHERE school_id=? AND pro=?

### 4. query_school - 查院校信息
用户问：某学校在哪、院校代码、学校类型
SQL: SELECT * FROM clp_school WHERE school LIKE ?

### 5. query_major - 查专业信息
用户问：某专业介绍、某专业学什么、某专业就业方向
（需要外部知识，数据库只有录取数据）

### 6. query_batch - 查批次线
用户问：一本线、二本线、省控线、批次线
SQL: SELECT batch, score FROM clp_batch_line WHERE prov='辽宁' AND year=2025
-- ⚠️ prov是中文名不是代码！

### 7. recommend - 志愿推荐
用户问：推荐志愿、生成方案、能上什么学校、冲稳保
SQL: 多个SQL组合查询
**重要：山东(sd)/浙江(zj)/北京(bj)/上海(sh)/海南(han)不需要nature过滤条件！**
```sql
-- 分数附近院校（山东等3+3省份不加nature条件）
-- ⚠️ 必须查询以下字段：school, school_note(院校备注), pro, pro_note(专业备注), low_real(2025最低分), low_rank_real(2025最低位次), plan_num(计划数), tuition(学费), rlt_json(历年数据JSON，含2024/2023/2022的分和位次)
-- ⚠️ school_note在clp_profession_data_{省}表中，不在clp_school表中！
SELECT s.school, p.school_note, p.pro, p.pro_note, p.low_real, p.low_rank_real, p.plan_num, p.tuition, p.rlt_json, p.school_code, p.pro_code, p.pro_group
FROM clp_profession_data_sd p
JOIN clp_school s ON s.id = p.school_id
WHERE p.year = 2025 AND p.is_real = 1
ORDER BY ABS(p.low_real - 580) ASC LIMIT 300
```
"""


def step1_understand_and_generate_sql(question: str, session_id: str = "") -> Dict:
    """LLM理解问题并生成SQL"""
    memory_context = load_memory_as_context()
    
    history_context = get_conversation_context(session_id) if session_id else ""
    history_section = f"## 历史对话（理解指代和上下文）\n{history_context}\n" if history_context else ""
    
    prompt = f"""你是一个SQL生成专家。请根据用户问题生成SQL。

{SCHEMA}

{INTENT_TYPES}

# MEMORY.md核心规则
{memory_context}

{history_section}## 用户问题
{question}

## 关键规则（必须遵守！）
- sd(山东)/zj(浙江)/bj(北京)/sh(上海)/han(海南)是3+3省份，SQL**禁止**加nature条件
- xj(新疆)/xz(西藏)nature用"文科"或"理科"
- 其他省份nature用"首选科目物理"或"首选科目历史"

## 输出JSON
```json
{{"intent":"...","conditions":{{...}},"sqls":["SQL1"],"missing":[]}}
```
请直接输出JSON：
"""
    
    response = call_llm([{"role": "user", "content": prompt}])
    
    # 调试日志
    import sys
    print(f"[DEBUG SQL] response_preview={response[:500]}", flush=True)
    
    
    try:
        json_match = re.search(r'```json\s*([\s\S]*?)\s*```', response)
        if json_match:
            return json.loads(json_match.group(1))
        return json.loads(response)
    except:
        return {"intent": "unknown", "sqls": [], "missing": ["无法理解问题"]}


def step2_execute_queries(sqls: List[str], conditions: Dict = None) -> List[Dict]:
    """执行SQL查询，自动适配3+3省份"""
    results = []
    prov_code = (conditions or {}).get("province_code", "")
    is_3p3 = prov_code in ("sd", "zj", "bj", "sh", "han")
    
    for sql in sqls:
        import sys
        print(f"[DEBUG] SQL={sql[:200]}", flush=True)
        result = execute_sql(sql, limit=300)
        print(f"[DEBUG] result rows={result.get('row_count',0)}", flush=True)
        
        # 如果3+3省份查询返回0条，自动去除nature条件重试
        if is_3p3 and result.get("success") and result.get("row_count", 0) == 0:
            fixed_sql = remove_nature_condition(sql)
            if fixed_sql != sql:
                result = execute_sql(fixed_sql, limit=300)
                import sys
                print(f"[DEBUG] 3+3省份自动修正SQL: {result.get('row_count',0)}条", flush=True)
        
        results.append(result)
    return results

def remove_nature_condition(sql: str) -> str:
    """移除SQL中的nature条件"""
    sql = re.sub(r"AND\s+(p\.)?nature\s*=\s*'[^']*'", "", sql)
    sql = re.sub(r"AND\s+(p\.)?nature\s*=\s*\"[^\"]*\"", "", sql)
    sql = re.sub(r"AND\s+(p\.)?nature\s*LIKE\s*'[^']*'", "", sql)
    sql = re.sub(r"WHERE\s+(p\.)?nature\s*=\s*'[^']*'\s*AND", "WHERE", sql)
    sql = re.sub(r"WHERE\s+(p\.)?nature\s*=\s*\"[^\"]*\"\s*AND", "WHERE", sql)
    # 如果nature是WHERE后唯一条件
    sql = re.sub(r"WHERE\s+(p\.)?nature\s*=\s*'[^']*'\s*$", "", sql)
    return sql


def _intent_name(intent: str) -> str:
    names = {"query_rank": "一分一段", "query_plan": "招生计划", "query_score": "历史分数",
             "query_school": "院校", "query_batch": "批次线", "recommend": "可填报"}
    return names.get(intent, intent)


# ============ 主流程 ============
def process_question(question: str, session_id: str = "") -> Dict:
    """处理用户问题，返回结构化结果"""
    
    # Step 1: LLM理解 + 生成SQL
    # Debug multi-turn
    history_preview = get_conversation_context(session_id)
    if history_preview:
        import sys
        print(f"[MULTITURN] sid={session_id[:16]} history={len(conversation_history.get(session_id,[]))}turns ctx={len(history_preview)}chars", flush=True)
    else:
        import sys
        print(f"[MULTITURN] sid={session_id[:16]} NO_HISTORY", flush=True)
    
    step1_result = step1_understand_and_generate_sql(question, session_id)
    
    intent = step1_result.get("intent", "unknown")
    conditions = step1_result.get("conditions", {})
    # 修复：如果province_code为空但province有值，用province作为code
    if not conditions.get("province_code") and conditions.get("province"):
        conditions["province_code"] = conditions["province"]
    missing = step1_result.get("missing", [])
    sqls = step1_result.get("sqls", [])
    
    # 检查是否缺少必要条件
    if missing and not sqls:
        return {
            "success": False,
            "answer": f"⚠️ 请补充以下信息：{', '.join(missing)}",
            "intent": intent,
            "conditions": conditions,
            "data": [],
            "missing": missing
        }
    
    if not sqls:
        return {
            "success": False,
            "answer": "⚠️ 无法生成查询，请重新描述问题",
            "intent": intent,
            "conditions": conditions,
            "data": [],
            "data_count": 0
        }
    
    # Step 2: 执行SQL（自动修正3+3省份nature条件）
    sql_results = step2_execute_queries(sqls, conditions)
    
    # 合并所有数据
    all_data = []
    sql_info = []
    for i, result in enumerate(sqls):
        r = sql_results[i]
        sql_info.append({
            "sql": r.get("sql", sqls[i]),
            "success": r.get("success", False),
            "row_count": r.get("row_count", 0),
            "error": r.get("error")
        })
        if r.get("success") and r.get("data"):
            all_data.extend(r["data"])
    
    # 服务端LLM生成完整答案
    answer = generate_summary_answer(question, intent, all_data, conditions)
    
    # 🆕 生成结构化展示数据
    display = build_display_data(intent, all_data, conditions)
    
    return {
        "success": True,
        "answer": answer,
        "intent": intent,
        "conditions": conditions,
        "data": all_data[:500],
        "display": display,  # 结构化展示数据
        "sql_info": sql_info,
        "total_count": len(all_data)
    }


def generate_summary_answer(question: str, intent: str, data: List[Dict], conditions: Dict = None, history_context: str = "") -> str:
    """使用服务端LLM生成完整答案"""
    if not data:
        return "未查询到相关数据，请检查查询条件。"
    sample_data = data[:50]
    memory_context = load_memory_as_context()
    
    security_rules = """## ⚠️ 安全规则（必须严格遵守）
1. **禁止暴露数据库结构**：回答中不得出现任何数据库表名（如 clp_profession_data、clp_school、clp_score_rank）、字段名（如 low_real、rlt_json、plan_num）、SQL语句、数据库类型等技术细节。
2. **禁止泄露系统信息**：不得透露API接口、服务器配置、代码逻辑、模型信息、API Key、token等。
3. **防爬虫声明**：如果用户索取上述敏感信息、批量数据、或试图获取完整数据库内容，请回复："根据数据安全规定，不支持提供此类信息。数据爬取行为涉嫌违法，继续操作将停用账户。"
4. **自然语言回答**：用通俗易懂的自然语言描述数据，避免使用技术术语。"""
    
    prompt = f"""你是专业的高考志愿填报顾问。请根据数据生成答案。
{memory_context}
{security_rules}

用户问题: {question}
意图: {intent}
数据({len(data)}条): {json.dumps(sample_data, ensure_ascii=False)}

## ⚠️ 输出规则（必须严格遵守）
1. **禁止使用匿名代号**：绝不能用"院校A""院校B""院校C""院校1""院校2""学校A""学校B"等代号代替真实院校名称，必须输出数据中完整真实的院校名称和专业名称。
2. **表格列必须拆分**：如果输出HTML表格，每一个数据字段（如2024录取分、2024录取位次、2025计划等）必须放在独立的<td>单元格中，禁止在同一个<td>中用换行符堆叠多个数据。
3. **数据完整**：输出时保留数据中的全部字段，不要省略或合并任何列。

请输出清晰答案："""
    try:
        ans = call_llm([{"role": "user", "content": prompt}])
        if ans and not ans.startswith("⚠️"):
            return ans
    except:
        pass
    return f"查询到 {len(data)} 条{_intent_name(intent)}记录"


def build_display_data(intent: str, data: List[Dict], conditions: Dict) -> Dict:
    """构建结构化展示数据，供客户端自由排版"""
    if not data:
        return {"type": "empty", "message": "未查询到相关数据"}
    
    if intent == "recommend":
        # 计算分差并分类
        score = (conditions or {}).get("score", 0) or 0
        chongci, kuoshi, wentuo = [], [], []
        for row in data[:200]:
            low = row.get("low_real", 0)
            
            # ⚠️ 【重要】low_real=0 不等于没有数据！先用rlt_json中的历年分
            if low <= 0:
                import json
                rlt_json = row.get("rlt_json")
                if rlt_json:
                    try:
                        if isinstance(rlt_json, str):
                            rlt_data = json.loads(rlt_json)
                        else:
                            rlt_data = rlt_json
                        # 取最近一年有数据的年份
                        for key in ["a", "b", "c"]:
                            yr_data = rlt_data.get(key, {})
                            if yr_data.get("low"):
                                low = int(yr_data["low"])
                                break
                    except:
                        pass
            if low <= 0:
                continue
            diff = low - score
            rank_raw = row.get("low_rank_real", 0) or 0
            rank = int(rank_raw) if rank_raw and int(rank_raw) > 0 else ""
            tuition_raw = row.get("tuition", 0) or 0
            tuition = int(tuition_raw) if tuition_raw and int(tuition_raw) > 0 else ""
            
            # 解析 rlt_json 历年数据
            rlt_json = row.get("rlt_json")
            rlt_2024, rlt_2023, rlt_2022 = {}, {}, {}
            if rlt_json:
                import json
                try:
                    if isinstance(rlt_json, str):
                        rlt_data = json.loads(rlt_json)
                    else:
                        rlt_data = rlt_json
                    rlt_2024 = rlt_data.get("a", {})  # 2024年
                    rlt_2023 = rlt_data.get("b", {})  # 2023年
                    rlt_2022 = rlt_data.get("c", {})  # 2022年
                except:
                    pass
            
            def rlt_val(rlt, field):
                v = rlt.get(field, 0) if rlt else 0
                return int(v) if v and int(v) > 0 else ""
            
            entry = {
                "school": row.get("school", ""),
                "pro": row.get("pro", ""),
                "school_note": row.get("school_note", "") or "",
                "pro_note": row.get("pro_note", "") or "",
                "plan": int(row.get("plan_num", 0) or 0),
                "tuition": tuition,
                "score": int(low),
                "rank": rank,
                "diff": int(diff),
                # 历年数据
                "score_2024": rlt_val(rlt_2024, "low"),
                "rank_2024": rlt_val(rlt_2024, "low_rank"),
                "score_2023": rlt_val(rlt_2023, "low"),
                "rank_2023": rlt_val(rlt_2023, "low_rank"),
                "score_2022": rlt_val(rlt_2022, "low"),
                "rank_2022": rlt_val(rlt_2022, "low_rank"),
            }
            if diff > 0:
                chongci.append(entry)
            elif diff >= -10:
                kuoshi.append(entry)
            else:
                wentuo.append(entry)
        
        chongci.sort(key=lambda x: x["diff"])
        kuoshi.sort(key=lambda x: x["diff"], reverse=True)
        wentuo.sort(key=lambda x: x["diff"])
        
        # 固定表头字段顺序（按老板要求 + 历年分/位次）
        columns = ["院校名称", "专业名称", "备注", "计划数", "学费",
                   "2025分", "2025位次", "2024分", "2024位次", "2023分", "2023位次",
                   "等效分差"]
        
        return {
            "type": "recommend",
            "title": f"🎯 志愿推荐方案",
            "subtitle": f"{conditions.get('province','')} | {conditions.get('nature','')} | {score}分",
            "total": len(chongci) + len(kuoshi) + len(wentuo),
            "columns": columns,
            "chongci": {"label": "🚀 冲刺", "desc": "分差+20~0", "count": len(chongci), "items": chongci[:33]},
            "kuoshi": {"label": "✅ 适合", "desc": "分差0~-10", "count": len(kuoshi), "items": kuoshi[:33]},
            "wentuo": {"label": "🛡️ 稳妥", "desc": "分差<-10", "count": len(wentuo), "items": wentuo[:46]}
        }
    
    elif intent == "query_plan":
        items = []
        for row in data[:100]:
            items.append({
                "school": row.get("school", ""),
                "pro": row.get("pro", ""),
                "plan": int(row.get("plan_num", 0) or 0),
                "tuition": int(row.get("tuition", 0) or 0),
                "edu_system": str(row.get("edu_system", "") or "")
            })
        return {"type": "table", "title": "招生计划", "columns": ["院校", "专业", "计划数", "学费", "学制"], "items": items}
    
    elif intent == "query_rank":
        items = [{"score": r.get("score",""), "rank": r.get("rank","")} for r in data[:50]]
        return {"type": "table", "title": "一分一段表", "columns": ["分数", "位次"], "items": items}
    
    elif intent == "query_score":
        items = [{"school": r.get("school",""), "pro": r.get("pro",""), "year": r.get("year",""), "score": r.get("low_real","")} for r in data[:50]]
        return {"type": "table", "title": "历史录取分数", "columns": ["院校", "专业", "年份", "最低分"], "items": items}
    
    elif intent == "query_school":
        items = [{"school": r.get("school",""), "province": r.get("province",""), "city": r.get("city","")} for r in data[:20]]
        return {"type": "table", "title": "院校信息", "columns": ["院校", "省份", "城市"], "items": items}
    
    elif intent == "query_batch":
        items = [{"batch": r.get("batch",""), "score": r.get("score","")} for r in data[:20]]
        return {"type": "table", "title": "批次线", "columns": ["批次", "分数线"], "items": items}
    
    else:
        return {"type": "list", "title": "查询结果", "items": [str(r) for r in data[:50]]}
    """使用服务端LLM生成完整答案"""
    if not data:
        return "未查询到相关数据，请检查查询条件。"
    
    # 限制数据量给LLM
    sample_data = data[:50]
    
    # 加载MEMORY.md上下文
    memory_context = load_memory_as_context()
    
    answer_prompt = f"""你是一个专业的高考志愿填报顾问。请根据查询数据回答用户问题。

{memory_context}

## 志愿推荐核心规则（必须遵守）
1. 等位分法：diff = 院校历年low_real - 考生历年等效分（同位次在不同年份对应的分）
2. 风险等级：+20~0冲刺 | 0~-10适合 | -10~-20稳妥 | ≤-20托底 | >+20不推荐
3. 默认比例3:3:4（冲刺30%+适合30%+稳妥40%），不足时向上补充
4. 所有分数必须来自数据库，禁止估算
5. ⚠️ low_real=0不等于没有数据！先查rlt_json中的历年分
6. 必须展示school_note（区分公办/独立学院/中外合作）和pro_note

## 科类字段值（因省份而异！）
- 首选科目物理/首选科目历史：辽宁、四川、河南、广东、江苏、河北、湖北、湖南、安徽、福建、江西、山西、陕西、甘肃、吉林、黑龙江、重庆、贵州、云南、广西、内蒙古、宁夏、青海
- 3+3（不分文理）：山东、浙江、北京、上海、海南
- 文科/理科：新疆、西藏

{history_section}## 用户问题
{question}

## 查询意图
{intent}

## 查询数据（共{len(data)}条，展示前50条）
{json.dumps(sample_data, ensure_ascii=False, indent=2)}

## 任务
根据数据生成一个清晰、专业、结构化的答案。

## 输出要求（志愿推荐 recommend 严格遵循）
如果是志愿推荐(recommend)，必须按以下固定表头输出表格：
| 院校名称 | 专业名称 | 备注 | 计划数 | 学费 | 2025分数 | 2025位次 | 2024分 | 2024位次 | 2023分 | 2023位次 | 等效分差 |
按冲刺(分差+20~0)、适合(分差0~-10)、稳妥(分差<-10)三层分类展示

如果是招生计划(query_plan)：
表格格式显示专业、计划数、学费等

其他类型：简要清晰地展示数据

每个部分末尾添加简要专家建议。

请直接输出答案：
"""
    
    try:
        answer = call_llm([{"role": "user", "content": answer_prompt}])
        if answer and not answer.startswith("⚠️"):
            return answer
    except:
        pass
    
    # LLM失败时的备用答案
    return f"查询到 {len(data)} 条{_intent_name(intent)}记录"


# ============ v6.0 异步执行器 ============
import asyncio
from concurrent.futures import ThreadPoolExecutor
_executor = ThreadPoolExecutor(max_workers=100)

# ============ API接口 ============
@app.post("/api/v1/chat")
async def chat(request: ChatRequest, x_api_key: Optional[str] = Header(None)):
    """主对话接口 - 返回结构化数据"""
    
    if x_api_key:
        from api_key_validator import verify_key
        if not verify_key(x_api_key).get("valid"):
            raise HTTPException(status_code=401, detail="API Key无效")
    
    loop = asyncio.get_event_loop()
    try:
                result = await loop.run_in_executor(_executor, process_question, request.question, request.session_id)
    except Exception as e:
        result = {"success": False, "answer": f"处理出错: {str(e)}", "data": []}
    
    # 多轮对话：记录本轮到历史
    add_to_history(
        request.session_id,
        request.question,
        result.get("answer", ""),
        result.get("conditions", {})
    )
    
    return ChatResponse(
        success=result.get("success", False),
        user_id=request.user_id,
        session_id=request.session_id,
        answer=result.get("answer", ""),
        intent=result.get("intent", ""),
        conditions=result.get("conditions", {}),
        data=result.get("data", []),
        display=result.get("display", {}),  # structured display data for client
        sql_info=result.get("sql_info", []),
        sources=["clp_profession_data", "clp_school", "clp_score_rank"],
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ")
    )




class LLMRequest(BaseModel):
    prompt: str
    temperature: float = 0.7
    max_tokens: int = 4096

class LLMResponse(BaseModel):
    success: bool
    content: str = ""
    error: str = ""

@app.post("/api/v1/llm")
async def llm_call(request: LLMRequest, x_api_key: Optional[str] = Header(None)):
    """通用LLM调用端点 - 不经过意图识别，直接调LLM"""
    if x_api_key:
        from api_key_validator import verify_key
        if not verify_key(x_api_key).get("valid"):
            raise HTTPException(status_code=401, detail="API Key无效")
    
    if not DEEPSEEK_API_KEY:
        return {"success": False, "content": "", "error": "服务端未配置LLM"}
    
    try:
        messages = [{"role": "user", "content": request.prompt}]
        response = call_llm(messages, temperature=request.temperature, max_retries=2)
        return {"success": True, "content": response, "error": ""}
    except Exception as e:
        logger.error(f"LLM调用失败: {e}")
        return {"success": False, "content": "", "error": str(e)}

@app.post("/api/v1/chat/async")
async def chat_async(request: ChatRequest, x_api_key: Optional[str] = Header(None)):
    """异步对话接口"""
    if x_api_key:
        from api_key_validator import verify_key
        if not verify_key(x_api_key).get("valid"):
            raise HTTPException(status_code=401, detail="API Key无效")
    
    job_id = str(uuid.uuid4())
    update_job(job_id, "pending", "正在查询数据库...", 10)
    
    def background_process():
        try:
            update_job(job_id, "querying", "正在查询数据库...", 30)
            result = process_question(request.question, request.session_id)
            
            # 调试日志
            import sys
            print(f"[DEBUG] job={job_id} intent={result.get('intent')} data_count={len(result.get('data',[]))} display_type={result.get('display',{}).get('type')}", flush=True)
            
            # 多轮对话：记录本轮到历史
            add_to_history(
                request.session_id,
                request.question,
                result.get("answer", ""),
                result.get("conditions", {})
            )
            
            update_job(job_id, "done", "查询完成", 90)
            set_job_result(
                job_id,
                result.get("answer", ""),
                result.get("data", []),
                result.get("intent", ""),
                result.get("conditions", {}),
                result.get("display", {})  # 🆕 结构化数据
            )
        except Exception as e:
            with job_lock:
                job_status[job_id] = {
                    "status": "error", "message": str(e), "progress": 0,
                    "answer": None, "data": []
                }
    
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=50)
    executor.submit(background_process)
    
    return {
        "success": True,
        "job_id": job_id,
        "user_id": request.user_id,
        "session_id": request.session_id,
        "status": "pending",
        "message": "正在处理...",
        "progress": 10
    }


@app.get("/api/v1/job/{job_id}")
async def get_job_status(job_id: str):
    """获取任务状态"""
    with job_lock:
        status = job_status.get(job_id, {"status": "not_found", "message": "任务不存在"})
    return {"job_id": job_id, **status}


@app.get("/api/v1/health")
async def health():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
        conn.close()
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {e}"
    
    return {
        "status": "ok",
        "version": "6.0.0",
        "db_type": DB_TYPE,
        "db_status": db_status,
        "llm_configured": bool(DEEPSEEK_API_KEY),
        "mode": "enterprise v6.0"
    }


@app.get("/api/v1/key/verify")
async def verify(x_api_key: str):
    from api_key_validator import verify_key
    return verify_key(x_api_key)


# ============ 志愿单风险评估 ============

@app.post("/api/v1/risk-assessment")
async def risk_assessment(
    file: UploadFile = File(...),
    province: str = Form(...),
    subject: str = Form(""),
    score_lo: str = Form(""),
    score_hi: str = Form(""),
    x_api_key: Optional[str] = Header(None)
):
    if x_api_key:
        from api_key_validator import verify_key
        if not verify_key(x_api_key).get("valid"):
            raise HTTPException(status_code=401, detail="API Key invalid")
    
    score = int(score_lo) if score_lo else 0
    if not score:
        raise HTTPException(status_code=400, detail="Missing score")
    
    file_bytes = await file.read()
    file_ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""

    # Extract school/pro pairs
    items = []
    if file_ext in ("csv", "txt"):
        text = file_bytes.decode("utf-8", errors="replace")
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("school") or line.startswith("序号"):
                continue
            parts = line.replace("\t", ",").split(",")
            if len(parts) >= 2:
                s, p = parts[0].strip(), parts[1].strip()
                if s and len(s) >= 2:
                    items.append({"school": s, "pro": p})
    elif file_ext in ("xlsx", "xls"):
        # Parse Excel
        try:
            import openpyxl
            from io import BytesIO
            wb = openpyxl.load_workbook(BytesIO(file_bytes))
            ws = wb.active
            for row in ws.iter_rows(min_row=1, values_only=True):
                if not row or not row[0]: continue
                school = str(row[0]).strip() if row[0] else ""
                # Skip header rows
                if school in ("序号", "院校", "院校名称", "学校", "专业", "专业名称", "school"): continue
                if len(school) < 2: continue
                pro = str(row[1]).strip() if len(row) > 1 and row[1] else ""
                items.append({"school": school, "pro": pro})
        except Exception as e:
            print(f"Excel parse error: {e}", flush=True)
    elif file_ext in ("jpg", "jpeg", "png", "gif", "webp", "bmp"):
        import base64
        b64 = base64.b64encode(file_bytes).decode()
        dp_key = os.getenv("DEEPSEEK_API_KEY", "")
        if dp_key:
            try:
                resp = requests.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {dp_key}", "Content-Type": "application/json"},
                    json={"model": "deepseek-chat", "messages": [{"role": "user", "content": f"用户上传了一张高考志愿表截图（{province}省）。请输出这张志愿表中可能存在的院校和专业名称列表，JSON格式：[{{\"school\":\"院校\",\"pro\":\"专业\"}}]。无法判断返回[]。"}], "max_tokens": 2000, "temperature": 0.3},
                    timeout=30
                )
                if resp.status_code == 200:
                    import re
                    txt = resp.json()["choices"][0]["message"]["content"]
                    m = re.search(r'\[[\s\S]*?\]', txt)
                    if m: items = json.loads(m.group())
            except: pass

    if not items:
        return {"success": False, "answer": "未能从上传的文件中识别出院校和专业信息。请确认：1) 文件内容包含院校和专业名称 2) 图片清晰不模糊。支持格式：CSV/TXT/Excel（.xlsx/.xls）。", "data": [], "display": None}

    # Query database
    prov_map = {"辽宁":"ln","山东":"sd","四川":"sc","河南":"hen","广东":"gd","江苏":"js","浙江":"zj","湖北":"hub","湖南":"hun","河北":"heb","安徽":"ah","福建":"fj","江西":"jx","山西":"sx","陕西":"shx","甘肃":"gs","吉林":"jl","黑龙江":"hlj","北京":"bj","上海":"sh","天津":"tj","重庆":"cq","广西":"gx","云南":"yn","贵州":"gz","内蒙古":"nmg","宁夏":"nx","青海":"qh","新疆":"xj","西藏":"xz","海南":"han"}
    prov_code = prov_map.get(province.replace("省","").replace("市","").replace("自治区",""), "")
    db = get_db_connection()
    cursor = db.cursor()
    results = []
    for item in items[:30]:
        school, pro = item.get("school","").strip(), item.get("pro","").strip()
        if not school: continue
        try:
            if prov_code:
                cursor.execute(f"SELECT p.school, p.pro, p.low_real, p.low_rank_real, p.plan_num, p.tuition, p.school_note, p.pro_note, p.rlt_json FROM clp_profession_data_{prov_code} p JOIN clp_school s ON s.id=p.school_id WHERE s.school LIKE %s AND p.pro LIKE %s AND p.is_real=1 LIMIT 1", (f"%{school}%", f"%{pro}%"))
                row = cursor.fetchone()
                if row:
                    low = int(row[2]) if row[2] else 0
                    import json as _json
                    rlt = row[8]
                    s24=s23=s22=None
                    if rlt:
                        try:
                            d=_json.loads(rlt) if isinstance(rlt,str) else rlt
                            s24=int(d.get("a",{}).get("low",0)) or None
                            s23=int(d.get("b",{}).get("low",0)) or None
                            s22=int(d.get("c",{}).get("low",0)) or None
                        except: pass
                    diff=low-score
                    results.append({"school":row[0]or school,"pro":row[1]or pro,"score_2025":low,"rank_2025":int(row[3])if row[3]else "","score_2024":s24,"score_2023":s23,"score_2022":s22,"plan":int(row[4])if row[4]else "","tuition":int(row[5])if row[5]else "","school_note":row[6]or "","pro_note":row[7]or "","diff":diff,"matched":True})
                else:
                    results.append({"school":school,"pro":pro,"matched":False})
            else:
                results.append({"school":school,"pro":pro,"matched":False})
        except:
            results.append({"school":school,"pro":pro,"matched":False})
    cursor.close()
    db.close()

    matched=[r for r in results if r.get("matched")]
    unmatched=[r for r in results if not r.get("matched")]
    total=len(results)
    sprint=[r for r in matched if r["diff"]>0]
    suitable=[r for r in matched if -10<=r["diff"]<=0]
    safe=[r for r in matched if -20<=r["diff"]<-10]
    bottom=[r for r in matched if r["diff"]<-20]
    danger=[r for r in matched if r["diff"]>20]

    lines=["## 志愿单风险评估报告","",f"**考生**: {province} | {subject} | {score}分",f"**志愿数**: {total}条 (匹配{len(matched)}条, 未匹配{len(unmatched)}条)",""]
    if matched:
        def pct(n): return f"{n/(total or 1)*100:.1f}%"
        lines+=["### 风险分布","","| 风险等级 | 数量 | 占比 |","|----------|------|------|",
                f"| 极危 (>+20) | {len(danger)} | {pct(len(danger))} |",
                f"| 冲刺 (+20~0) | {len(sprint)} | {pct(len(sprint))} |",
                f"| 适合 (0~-10) | {len(suitable)} | {pct(len(suitable))} |",
                f"| 稳妥 (-10~-20) | {len(safe)} | {pct(len(safe))} |",
                f"| 托底 (<-20) | {len(bottom)} | {pct(len(bottom))} |",""]
        sp=len(sprint)/(total or 1); dp=len(danger)/(total or 1)
        spct=len(suitable)/(total or 1); sfpct=(len(safe)+len(bottom))/(total or 1)
        lines+=["### 整体评价",""]
        if dp>0.2: lines.append(f"- {pct(len(danger))}极危志愿，建议替换")
        if sp>0.5: lines.append(f"- 冲刺{pct(len(sprint))}偏高，有滑档风险")
        if spct>=0.3 and sp<=0.4: lines.append("- 冲稳保比例合理，风险可控")
        if spct+sfpct>0.7: lines.append("- 偏保守，可适度冲刺")
        lines+=["","### 冲稳保比例","| 类型 | 实际 | 推荐 |","|------|------|------|",
                f"| 冲刺 | {pct(sp)} | 30% |",
                f"| 适合 | {pct(spct)} | 30% |",
                f"| 稳妥+托底 | {pct(sfpct)} | 40% |","",
                "### 逐条详情","",
                "| # | 院校 | 专业 | 2025录取分 | 分差 | 风险 |",
                "|---|------|------|------------|------|------|"]
        for i,r in enumerate(matched,1):
            d=r["diff"]; tag="极危" if d>20 else "冲刺" if d>0 else "适合" if d>=-10 else "稳妥" if d>=-20 else "托底"
            lines.append(f"| {i} | {r['school']} | {r['pro']} | {r['score_2025']} | {'+' if d>0 else ''}{d} | {tag} |")
        if unmatched:
            lines+=["",f"### 未匹配({len(unmatched)}条)"]
            for r in unmatched: lines.append(f"- {r['school']} - {r['pro']}")
        lines+=["","### 优化建议"]
        if dp>0: lines.append(f"1. 替换{len(danger)}条极危志愿")
        if sp>0.5: lines.append("2. 增加稳妥类志愿")
        if spct+sfpct>0.8: lines.append("3. 适度增加冲刺志愿")
        lines.append("4. 数据基于2025年录取，仅供参考")

    return {"success":True,"answer":"\n".join(lines),"data":matched,
            "stats":{"total":total,"matched":len(matched),"unmatched":len(unmatched),
                     "danger":len(danger),"sprint":len(sprint),"suitable":len(suitable),
                     "safe":len(safe),"bottom":len(bottom)}}



# ============ 启动 ============
if __name__ == "__main__":
    logger.info(f"v6.0 Enterprise | DB Pool: {DB_POOL_MIN}-{DB_POOL_MAX} | DB Sem: {MAX_DB_CONCURRENT} | LLM Sem: {MAX_LLM_CONCURRENT}")
    print("=" * 60)
    print("蜻蜓志愿数据服务 v5.1")
    print(f"模式: 数据服务API（返回结构化数据）")
    print(f"LLM: {LLM_MODEL}")
    print("=" * 60)
    
    ssl_cert = os.getenv("SSL_CERT")
    ssl_key = os.getenv("SSL_KEY")
    ssl_config = {}
    if ssl_cert and ssl_key:
        ssl_config = {"ssl_certfile": ssl_cert, "ssl_keyfile": ssl_key}
    
    uvicorn.run("server_openclaw:app", host=HOST, port=PORT, log_level="info", **ssl_config)
