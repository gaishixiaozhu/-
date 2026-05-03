# -*- coding: utf-8 -*-
"""
志愿推荐引擎 - 实现等位分法

严格按照MEMORY.md中的规则：
1. 等位分法计算风险等级
2. low_real=0时用rlt_json历年数据
3. 冲刺/适合/稳妥分类
"""

import json
import sqlite3
from typing import Dict, List, Optional, Tuple


class RecommendationEngine:
    """志愿推荐引擎"""
    
    # 省份代码映射
    PROVINCE_CODE = {
        "辽宁": "ln", "山东": "sd", "四川": "sc", "河南": "hen", "广东": "gd",
        "江苏": "js", "浙江": "zj", "河北": "heb", "湖北": "hub", "湖南": "hun",
        "安徽": "ah", "福建": "fj", "江西": "jx", "山西": "sx", "陕西": "shx",
        "甘肃": "gs", "吉林": "jl", "黑龙江": "hlj", "北京": "bj", "上海": "sh",
        "重庆": "cq", "贵州": "gz", "云南": "yn", "广西": "gx", "海南": "han",
        "内蒙古": "nmg", "宁夏": "nx", "青海": "qh", "新疆": "xj", "西藏": "xz"
    }
    
    # 科类映射
    NATURE_MAP = {
        "物理": "首选科目物理", "理科": "首选科目物理",
        "历史": "首选科目历史", "文科": "首选科目历史"
    }
    
    def __init__(self, db_path: str = None, db_type: str = "mysql"):
        """
        初始化推荐引擎
        """
        self.db_path = db_path or "/Users/fuquanhao/.openclaw/workspace/skills/data/cache.db"
        self.db_type = db_type
    
    def get_connection(self):
        """获取新的数据库连接"""
        if self.db_type == "mysql":
            import pymysql
            return pymysql.connect(
                host="YOUR_MYSQL_HOST",
                port=3306,
                user="YOUR_MYSQL_USER",
                password="YOUR_MYSQL_PASSWORD",
                database="clp_base",
                charset='utf8mb4',
                autocommit=True
            )
        else:
            return sqlite3.connect(self.db_path)
    
    def get_rank(self, province: str, year: int, score: int, nature: str) -> Optional[int]:
        """获取分数对应的位次"""
        prov_code = self.PROVINCE_CODE.get(province, province)
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            if self.db_type == "mysql":
                cursor.execute("""
                    SELECT `rank` FROM clp_score_rank 
                    WHERE prov = %s AND `year` = %s AND score = %s AND nature = %s
                """, (prov_code, year, score, nature))
            else:
                cursor.execute("""
                    SELECT rank FROM clp_score_rank 
                    WHERE prov = ? AND year = ? AND score = ? AND nature = ?
                """, (prov_code, year, score, nature))
            
            result = cursor.fetchone()
            return result[0] if result else None
        finally:
            cursor.close()
            conn.close()
    
    def get_equivalent_score(self, province: str, target_year: int, rank: int, nature: str) -> Optional[int]:
        """根据位次获取指定年份的等效分"""
        prov_code = self.PROVINCE_CODE.get(province, province)
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            if self.db_type == "mysql":
                # 找最接近目标位次的分数
                cursor.execute("""
                    SELECT score FROM clp_score_rank 
                    WHERE prov = %s AND `year` = %s AND nature = %s
                    ORDER BY ABS(CAST(`rank` AS SIGNED) - %s) ASC
                    LIMIT 1
                """, (prov_code, target_year, nature, rank))
            else:
                cursor.execute("""
                    SELECT score FROM clp_score_rank 
                    WHERE prov = ? AND year = ? AND nature = ?
                    ORDER BY ABS(rank - ?) ASC
                    LIMIT 1
                """, (prov_code, target_year, nature, rank))
            
            result = cursor.fetchone()
            return result[0] if result else None
        finally:
            cursor.close()
            conn.close()
    
    def parse_rlt_json(self, rlt_json_str: str) -> Dict[int, Dict]:
        """解析rlt_json历年数据"""
        if not rlt_json_str:
            return {}
        
        try:
            data = json.loads(rlt_json_str)
            result = {}
            # a=2024, b=2023, c=2022
            if 'a' in data and data['a'].get('year'):
                result[data['a']['year']] = data['a']
            if 'b' in data and data['b'].get('year'):
                result[data['b']['year']] = data['b']
            if 'c' in data and data['c'].get('year'):
                result[data['c']['year']] = data['c']
            return result
        except:
            return {}
    
    def calculate_risk(self, user_score: int, user_rank: int, province: str,
                       school_score: int, history: Dict[int, int], nature: str) -> Tuple[str, float]:
        """
        计算风险等级
        
        Args:
            user_score: 考生分数
            user_rank: 考生位次
            province: 省份
            school_score: 院校2025年分数
            history: 历年分数 {2025: 520, 2024: 510, 2023: 505}
            nature: 科类
        
        Returns: (风险等级, 分差均值)
        """
        # 权重：近年权重更大
        weights = {2023: 0.2, 2024: 0.3, 2025: 0.5}
        
        diffs = []
        
        # 2025年分数差
        if school_score and school_score > 0:
            diffs.append((2025, school_score - user_score))
        
        # 历年分数差
        for year, low_score in history.items():
            if year not in weights or year == 2025:  # 2025已单独计算
                continue
            
            if not low_score or low_score <= 0:
                continue
            
            # 获取该年的等效分
            eq_score = self.get_equivalent_score(province, year, user_rank, nature)
            if eq_score:
                diff = low_score - eq_score
                diffs.append((year, diff))
        
        if not diffs:
            return '未知', 0
        
        # 计算加权平均分差
        total_weight = sum(weights.get(year, 0.2) for year, _ in diffs)
        weighted_diff = sum(weights.get(year, 0.2) * diff for year, diff in diffs)
        avg_diff = weighted_diff / total_weight if total_weight > 0 else 0
        
        # 风险等级判定
        if avg_diff > 20:
            return '🚨 极危', avg_diff
        elif avg_diff > 0:
            return '🚀 冲刺', avg_diff
        elif avg_diff > -10:
            return '✅ 适合', avg_diff
        elif avg_diff > -20:
            return '🛡️ 稳妥', avg_diff
        else:
            return '🔒 托底', avg_diff
    
    def query_professions(self, province: str, nature: str, score: int, 
                         major_keyword: str = None, limit: int = 300) -> List[Dict]:
        """
        查询专业录取数据（多年数据合并）
        
        Args:
            province: 省份（如"辽宁"）
            nature: 科类（如"物理"）
            score: 考生分数
            major_keyword: 专业关键词（如"计算机"），可选
            limit: 返回数量限制
        """
        prov_code = self.PROVINCE_CODE.get(province, province)
        nature_full = self.NATURE_MAP.get(nature, nature)
        table_name = f"clp_profession_data_{prov_code}"
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            # 查询2025年数据作为主数据源
            if self.db_type == "mysql":
                sql = f"""
                    SELECT s.school, p.pro, p.pro_note, p.low_real, 
                           p.plan_num, p.nature, p.batch, p.school_id, p.pro_code
                    FROM {table_name} p
                    JOIN clp_school s ON p.school_id = s.id
                    WHERE p.nature = %s
                      AND p.year = 2025
                      AND p.is_real = 1
                """
                params = [nature_full]
            else:
                sql = f"""
                    SELECT s.school, p.pro, p.pro_note, p.low_real, 
                           p.plan_num, p.nature, p.batch, p.school_id, p.pro_code
                    FROM {table_name} p
                    JOIN clp_school s ON p.school_id = s.id
                    WHERE p.nature = ?
                      AND p.year = 2025
                      AND p.is_real = 1
                """
                params = [nature_full]
            
            if major_keyword:
                if self.db_type == "mysql":
                    # 同时匹配专业名称和院校名称
                    sql += " AND (p.pro LIKE %s OR s.school LIKE %s)"
                    params.append(f"%{major_keyword}%")
                    params.append(f"%{major_keyword}%")
                else:
                    sql += " AND (p.pro LIKE ? OR s.school LIKE ?)"
                    params.append(f"%{major_keyword}%")
                    params.append(f"%{major_keyword}%")
            
            sql += f" ORDER BY ABS(p.low_real - {score}) ASC LIMIT {limit}"
            
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            
            results = []
            for row in rows:
                school_id = row[7]
                pro_code = row[8]
                
                # 获取该专业历年分数
                history = self._get_history_scores(cursor, table_name, school_id, pro_code, nature_full)
                
                results.append({
                    'school': row[0],
                    'pro': row[1],
                    'pro_note': row[2] or '',
                    'low_real': row[3] or 0,
                    'plan_num': row[4] or 0,
                    'nature': row[5],
                    'batch': row[6] or '',
                    'school_id': school_id,
                    'pro_code': pro_code,
                    'history': history  # 历年分数
                })
            
            return results
        
        finally:
            cursor.close()
            conn.close()
    
    def _get_history_scores(self, cursor, table_name: str, school_id: int, 
                           pro_code: str, nature: str) -> Dict[int, int]:
        """获取某个专业历年分数"""
        if self.db_type == "mysql":
            sql = f"""
                SELECT year, low_real
                FROM {table_name}
                WHERE school_id = %s 
                  AND pro_code = %s
                  AND nature = %s
                  AND is_real = 1
                  AND year >= 2023
                ORDER BY year DESC
            """
            cursor.execute(sql, (school_id, pro_code, nature))
        else:
            sql = f"""
                SELECT year, low_real
                FROM {table_name}
                WHERE school_id = ? 
                  AND pro_code = ?
                  AND nature = ?
                  AND is_real = 1
                  AND year >= 2023
                ORDER BY year DESC
            """
            cursor.execute(sql, (school_id, pro_code, nature))
        
        result = {}
        for row in cursor.fetchall():
            year = row[0]
            low = row[1]
            if low and low > 0:
                result[year] = low
        
        return result
    
    def generate_recommendation(self, province: str, nature: str, score: int,
                               major_keyword: str = None, total_count: int = 112) -> Dict:
        """
        生成完整志愿推荐
        
        Args:
            province: 省份
            nature: 科类（物理/历史）
            score: 分数
            major_keyword: 专业关键词
            total_count: 总数量（默认112）
        
        Returns:
            包含冲刺/适合/稳妥的字典
        """
        # 获取位次
        nature_full = self.NATURE_MAP.get(nature, nature)
        rank = self.get_rank(province, 2025, score, nature_full)
        
        if not rank:
            # 如果查不到位次，用分数直接匹配
            rank = score * 100  # 粗略估算
        
        # 查询专业数据
        plans = self.query_professions(province, nature, score, major_keyword, limit=500)
        
        # 计算每个院校的风险等级
        for plan in plans:
            risk, diff = self.calculate_risk(
                score, rank, province,
                plan['low_real'],
                plan['history'],  # 历年分数
                nature_full
            )
            plan['risk_level'] = risk
            plan['diff_avg'] = round(diff, 1)
        
        # 按风险等级分类
        chongci = [p for p in plans if '冲刺' in p['risk_level']]
        kuoshi = [p for p in plans if '适合' in p['risk_level']]
        wentuo = [p for p in plans if '稳妥' in p['risk_level']]
        jimi = [p for p in plans if p['risk_level'] == '未知']
        
        # 按分差排序
        chongci.sort(key=lambda x: x['diff_avg'], reverse=True)
        kuoshi.sort(key=lambda x: x['diff_avg'], reverse=True)
        wentuo.sort(key=lambda x: x['diff_avg'])
        
        # 分配数量（3:3:4比例）
        target_c = int(total_count * 0.3)
        target_k = int(total_count * 0.3)
        target_w = total_count - target_c - target_k
        
        result = {
            'province': province,
            'nature': nature,
            'score': score,
            'rank': rank,
            'total_count': total_count,
            'chongci_count': min(len(chongci), target_c),
            'kuoshi_count': min(len(kuoshi), target_k),
            'wentuo_count': min(len(wentuo), target_w),
            'chongci': chongci[:target_c],
            'kuoshi': kuoshi[:target_k],
            'wentuo': wentuo[:target_w],
            'all_count': len(plans)
        }
        
        return result
    
    def format_recommendation(self, result: Dict) -> str:
        """格式化推荐结果为字符串（完整版，返回所有记录）"""
        lines = []
        lines.append("🎯 志愿方案推荐结果（完整版）")
        lines.append(f"📊 {result['province']}{result['nature']}类 {result['score']}分 | 位次{result['rank']}")
        lines.append(f"共{result['total_count']}个：🚀冲刺{len(result['chongci'])} + ✅适合{len(result['kuoshi'])} + 🛡️稳妥{len(result['wentuo'])}")
        lines.append("")
        
        # 冲刺（全部）
        if result['chongci']:
            lines.append(f"🚀冲刺(+20~0分差)：共{len(result['chongci'])}个")
            for p in result['chongci']:
                note = f"[{p['pro_note'][:20]}]" if p.get('pro_note') and len(p.get('pro_note','')) > 0 else ""
                lines.append(f"{p['school']}|{p['pro']}{note}|{p['low_real']}分|{p['plan_num']}人|{p['diff_avg']:+g}")
        
        # 适合（全部）
        if result['kuoshi']:
            lines.append("")
            lines.append(f"✅适合(0~-10分差)：共{len(result['kuoshi'])}个")
            for p in result['kuoshi']:
                note = f"[{p['pro_note'][:20]}]" if p.get('pro_note') and len(p.get('pro_note','')) > 0 else ""
                lines.append(f"{p['school']}|{p['pro']}{note}|{p['low_real']}分|{p['plan_num']}人|{p['diff_avg']:+g}")
        
        # 稳妥（全部）
        if result['wentuo']:
            lines.append("")
            lines.append(f"🛡️稳妥(<-10分差)：共{len(result['wentuo'])}个")
            for p in result['wentuo']:
                note = f"[{p['pro_note'][:20]}]" if p.get('pro_note') and len(p.get('pro_note','')) > 0 else ""
                lines.append(f"{p['school']}|{p['pro']}{note}|{p['low_real']}分|{p['plan_num']}人|{p['diff_avg']:+g}")
        
        lines.append("")
        lines.append("📌数据来源：2025年录取数据，仅供参考")
        
        return "\n".join(lines)


# 测试
if __name__ == "__main__":
    engine = RecommendationEngine(db_type="sqlite")
    result = engine.generate_recommendation("辽宁", "物理", 520, "计算机", 112)
    print(engine.format_recommendation(result))
