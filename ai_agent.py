# ai_agent.py
import requests
import json

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_bill",
            "description": "添加一笔账单记录，用于记账。amount为金额，正数；type只能是'收入'或'支出'",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "账单日期，格式YYYY-MM-DD"},
                    "merchant": {"type": "string", "description": "商户名称"},
                    "amount": {"type": "number", "description": "金额"},
                    "type": {"type": "string", "description": "收入/支出，二选一"}
                },
                "required": ["date", "merchant", "amount", "type"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_summary",
            "description": "获取用户当前财务汇总数据，总收入、总支出、结余、最近账单",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    }
]

def get_ai_response(messages, bills_data, total_income, total_expense, balance, monthly_living, save_target, city_level, api_key):
    recent_text = bills_data.tail(3).to_string(index=False)
    background_info = f"""【实时财务数据】
总收入：{total_income:.2f}元
总支出：{total_expense:.2f}元
每月生活费设定：{monthly_living:.2f}元
存钱目标：{save_target:.2f}元
所在城市等级：{city_level}
最近3笔账单：
{recent_text}
"""
    system_prompt = f"""你是「明白账」AI财务管家，专为大学生服务，语气亲切、不爹味。
你拥有工具能力：
1. add_bill：用户说要记账、花了钱、收到钱时调用，提取日期、商户、金额、收支类型。
   - 如果用户没写日期，默认使用今天日期。
2. get_summary：用户想查看财务汇总时调用。

规则：
- 需要记账/查询汇总，输出tool_call，不要直接回答文本。
- 普通财务提问，直接输出自然语言回答。
- 必须结合下面用户账单数据回答：
{background_info}
如果用户超支，提醒并给出奶茶、外卖等校园省钱建议。
"""
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    payload = {
        "model": "deepseek-chat",
        "messages": full_messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.7
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    resp = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=20)
    return resp
