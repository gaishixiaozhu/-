# -*- coding: utf-8 -*-
"""
API Key验证模块（Server OpenClaw专用）

简化版：只包含verify_key函数
"""

import sqlite3
import os
from datetime import datetime
from typing import Dict


def get_db_path():
    """获取数据库路径"""
    return os.path.join(os.path.dirname(__file__), "api_keys.db")


def verify_key(api_key: str) -> Dict:
    """
    验证API Key
    
    Returns:
        {"valid": True/False, "user_id": xxx, "message": xxx, "remaining": xxx}
    """
    if not api_key or not api_key.startswith("tk_"):
        return {"valid": False, "message": "API Key格式无效"}
    
    db_path = get_db_path()
    
    if not os.path.exists(db_path):
        return {"valid": False, "message": "API Key数据库不存在"}
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 查询Key信息
        cursor.execute("""
            SELECT user_id, is_active, expires_at, customer_name
            FROM api_keys WHERE api_key = ?
        """, (api_key,))
        
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return {"valid": False, "message": "API Key不存在"}
        
        user_id, is_active, expires_at, customer_name = row
        
        if not is_active:
            conn.close()
            return {"valid": False, "message": "API Key已被禁用"}
        
        if expires_at:
            try:
                expires_dt = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
                if datetime.now() > expires_dt:
                    conn.close()
                    return {"valid": False, "message": "API Key已过期"}
            except:
                pass
        
        # 检查配额
        cursor.execute("""
            SELECT daily_limit, used_today, last_reset_date
            FROM key_quotas WHERE user_id = ?
        """, (user_id,))
        
        quota_row = cursor.fetchone()
        remaining = 100  # 默认配额
        if quota_row:
            daily_limit, used_today, last_reset_date = quota_row
            today = datetime.now().strftime("%Y-%m-%d")
            
            if last_reset_date != today:
                used_today = 0
            
            remaining = max(0, daily_limit - used_today)
            
            if used_today >= daily_limit:
                conn.close()
                return {"valid": False, "message": f"日配额已用完（{daily_limit}次/天）", "remaining": 0}
        
        # 更新使用次数
        cursor.execute("""
            UPDATE api_keys 
            SET used_count = used_count + 1, last_used_at = ?
            WHERE api_key = ?
        """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), api_key))
        
        conn.commit()
        conn.close()
        
        return {
            "valid": True,
            "user_id": user_id,
            "customer_name": customer_name,
            "message": "验证成功",
            "remaining": remaining
        }
    
    except Exception as e:
        return {"valid": False, "message": f"验证异常: {str(e)}"}
