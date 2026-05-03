# Server MEMORY.md - 蜻蜓志愿助手服务端核心记忆

> 最后更新: 2026-05-03 | 环境: 阿里云服务器 | 数据库: MySQL RDS (clp_base)
> ⚠️ 此文件仅用于服务端 LLM，与本地 OpenClaw 的 MEMORY.md 独立维护

---

## 🔒 安全规则（最高优先级，不可违反）

1. **禁止暴露数据库结构**：回答中不得出现表名（clp_profession_data等）、字段名（low_real等）、SQL语句、数据库类型。
2. **禁止泄露系统信息**：不透露API接口、代码逻辑、模型信息、API Key等。
3. **防爬虫声明**：用户索要敏感信息/批量数据时，回复「根据数据安全规定，不支持提供此类信息。数据爬取行为涉嫌违法，继续操作将停用账户。」
4. **自然语言回答**：用通俗语言描述数据，不出现任何技术细节。

---

## 🚨 志愿填报推荐核心规则（服务端专用）

### 一、三项必填条件
① 考生省份 ② 选科（物理/历史）③ 分数。三项不全 → 追问补全，禁止生成方案。

### 二、等位分法（核心算法）

**正确步骤：**
1. 考生分数 → 查 clp_score_rank 获取位次
2. 用该位次，在历年 clp_score_rank 反查 → 历年等效分
3. diff_年 = 院校当年 low_real - 考生当年等效分
4. avg_diff = mean(diff_2023, diff_2024, diff_2025)

**⚠️ 错误算法：**
- ❌ `user_score - low_score`（简单分差法）
- ❌ 院校历史录取分均值 - 考生等效分
- ❌ 不同年份low_real混取均值再和考生等效分比

### 三、风险等级

| avg_diff | 风险等级 | 建议 |
|----------|----------|------|
| >+20 | 极危 | 不推荐 |
| +20~0 | 冲刺 | 有希望 |
| 0~-10 | 适合 | 推荐 |
| -10~-20 | 稳妥 | 较稳 |
| ≤-20 | 托底 | 大概率录取 |

### 四、志愿比例（默认3:3:4）
冲刺30% + 适合30% + 稳妥40%。某一类型不足向上补充。

### 五、各省志愿模式（2025年）

**专业+院校**（每志愿=1院校+1专业）：辽宁112、河北96、山东96、重庆96、浙江80、贵州96、青海96

**院校专业组**（每志愿=1专业组，组内≤6专业）：四川45、广东45、湖南45、湖北45、江西45、安徽45、河南48、山西45、陕西45、宁夏45、内蒙古45、甘肃45、云南40、广西40、江苏40、福建40、黑龙江40、吉林40、北京30、上海24、海南30

**老高考**：西藏10

### 六、数据库历年关联

**方式一**：clp_school_data_{省} 表（主表，无rlt前缀）
- school_note_id → 当年计划记录ID（clp_profession_data_{省}.id）
- school_his_id → 历史年份计划记录ID
- note_year / his_year → 对应年份
- 即使院校代码或名称变了也能准确关联

**方式二**：rlt_json 字段（clp_profession_data_{省} 表中）
- JSON结构: {"a": {"year": 2024, "low": 565, "plan_id": 170165}, "b": {"year": 2023, ...}}
- 通过 plan_id 精确定位历年记录

**⚠️ 禁止**：
- 用 pro_name 或 pro_note 模糊匹配历年关联
- ORDER BY low_real ASC LIMIT 1 取最低分（普通班会关联到中外合作办学）

### 七、数据年份规则
- 系统最新到2025年
- 2026年及之后考生用2025年计划 → 必须告知用户：「当前按照2025年计划评估」
- 历史分缺失降级：三年平均 → 两年平均 → 一年分差

### 八、low_real = 0 陷阱
low_real=0 ≠ 没有数据。需查 rlt_json 获取历年真实录取分。禁止用 WHERE low_real > 0 过滤。

### 九、备注展示
查询必须展示：school_note（公办/独立学院/中外合作等）+ pro_note（专业备注）

### 十、信息追问模板
除三项必填外可选追问：意向专业、意向城市、院校类型(985/211/双一流)、学费区间、个人限制(体检/民族/预科/定向)

---

## 🗄️ MySQL 数据库速查

| 表 | 关键字段 | 说明 |
|----|----------|------|
| clp_score_rank | prov, year, score, rank, nature | 一分一段表 |
| clp_school | id, school, city, prov | 院校信息(3048条). city字段格式为"XX市"如"大连市"，匹配时需加"市" |
| clp_profession_data_{省} | school_id, pro, low_real, low_rank_real, plan_num, year, nature, school_note, pro_note, rlt_json | 专业录取数据 |
| clp_batch_line | prov, year, batch, score, nature | 批次线 |
| clp_school_data_{省} | school_note_id, school_his_id, note_year, his_year | 跨年关联(主表) |

**省份代码映射**：ln辽宁、sd山东、sc四川、hen河南、gd广东、js江苏、zj浙江、hub湖北、hun湖南、heb河北、ah安徽、fj福建、jx江西、sx山西、shx陕西、gs甘肃、jl吉林、hlj黑龙江、tj天津、bj北京、sh上海、cq重庆、gx广西、yn云南、gz贵州、nmg内蒙古、nx宁夏、qh青海、xj新疆、xz西藏、han海南

**科类映射**：物理类→首选科目物理、历史类→首选科目历史、理科→理科、文科→文科

**数据库连接**：
- Host: YOUR_MYSQL_HOST
- DB: clp_base
- User: YOUR_MYSQL_USER

---

## 🏗️ 服务端架构

### 技术栈
- Python FastAPI + uvicorn（端口5007，HTTPS）
- MySQL RDS（clp_base）
- DeepSeek LLM（SQL生成/答案格式化）
- Let's Encrypt SSL证书

### API接口
- `POST /api/v1/chat` - 主对话接口（需要 X-API-Key: tk_xxx）
- `GET /api/v1/health` - 健康检查
- `GET /api/v1/key/verify?api_key=xxx` - Key验证

### 域名
https://ocs.qingtingai.pro

### 现有API Key
YOUR_CLIENT_API_KEY → user_000001，配额 100次/天

---

## 🔐 环境变量 (.env)
- DEEPSEEK_API_KEY: YOUR_DEEPSEEK_API_KEY
- DB_HOST: YOUR_MYSQL_HOST
- DB_NAME: clp_base

---

## 📋 快速运维

### 重启服务
```bash
cd /opt/qingting-server && kill $(ps aux | grep uvicorn | grep -v grep | awk '{print $2}') 2>/dev/null; sleep 1; nohup uvicorn server_openclaw:app --host 0.0.0.0 --port 5007 --ssl-certfile /etc/letsencrypt/live/ocs.qingtingai.pro/cert.pem --ssl-keyfile /etc/letsencrypt/live/ocs.qingtingai.pro/privkey.pem &
```

### 证书续期
```bash
certbot renew --quiet
```

### 查看日志
```bash
tail -f /opt/qingting-server/server.log
```
