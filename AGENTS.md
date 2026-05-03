# AGENTS.md - 蜻蜓志愿助手服务端

## Session Startup

1. Read `MEMORY.md` — 核心业务规则（志愿填报算法、数据库表结构、风险等级）
2. Read `SOUL.md` — 行为准则
3. Read `USER.md` — 用户信息

## Memory

- **MEMORY.md** — 核心规则+数据库速查（唯一记忆文件）
- 这是服务端环境，不维护 daily notes，所有知识沉淀在 MEMORY.md

## Core Rules

1. **数据库优先** — 所有分数/位次/计划数问题必须查 MySQL，禁止凭记忆回答
2. **等位分法** — 严格按 MEMORY.md 中的等位分算法计算风险等级
3. **三项必填** — 省份+选科+分数不全时追问，不生成方案
4. **数据标注** — 每次查询标明数据年份（最新2025年）
5. **备注必展** — school_note 和 pro_note 必须展示

## API 行为

- 接收 POST /api/v1/chat 请求
- 返回 JSON: {success, answer, intent, conditions, sources, timestamp}
- 错误时返回 success=false + answer 中说明原因

## 禁止事项

- 不暴露数据库密码/API Key给用户
- 不执行 SELECT 以外的SQL（DROP/DELETE/UPDATE/INSERT）
- 不凭记忆估算分数/位次/计划数
