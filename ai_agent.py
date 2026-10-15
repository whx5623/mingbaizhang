import requests
import json

CAMPUS_FINANCE_KNOWLEDGE = {
    "国家助学贷款": {
        "最高额度": "本专科生每年12000元，研究生每年16000元",
        "利息起算时间": "在校期间由财政贴息，毕业当年9月1日起开始计收利息",
        "还款宽限期": "毕业后5年还本宽限期，宽限期内只需偿还利息"
    },
    "校园贷防骗": {
        "常见套路": "低息诱导、砍头息、虚假合同、暴力催收、冒用身份借贷",
        "如何辨别": "无正规资质平台、手续简单仅凭身份证放款、利息与服务费叠加远超法定上限",
        "遇到威胁怎么处理": "保留聊天、合同、转账证据，拒绝私下转账，及时报警并向学校资助中心报备"
    },
    "信用卡与花呗": {
        "分期真实年化利率的计算方法": "真实年化IRR，不等于表面月费率×12；每期持续归还本金，资金不断减少，真实年化显著高于宣传名义利率"
    },
    "理财入门": {
        "大学生适合的极简理财方式": "货币基金，流动性强风险极低；优先预留3-6个月应急资金，禁止使用生活费借贷、杠杆投资，不碰高收益理财、虚拟币"
    }
}

def get_ai_response(chat_history, df, total_income, total_expense, balance, monthly_living, save_target, city_level, api_key):
    system_content = f"""
你是大学生AI金融导师。
校园金融知识库：
{json.dumps(CAMPUS_FINANCE_KNOWLEDGE, ensure_ascii=False, indent=2)}
当用户问及校园金融政策时，请结合知识库的内容给出精确回答，不要自己瞎编。
用户账单情况：
总收入：{total_income:.2f}元，总支出：{total_expense:.2f}元，结余：{balance:.2f}元
每月生活费设定：{monthly_living}元，每月存钱目标：{save_target}元，城市等级：{city_level}
根据账单给大学生友好、务实的财务建议，语言简洁易懂。
"""
    messages = [{"role": "system", "content": system_content}] + chat_history
    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "add_bill",
                    "description": "新增一笔账单记录",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "description": "日期，格式YYYY-MM-DD"},
                            "merchant": {"type": "string", "description": "商户名称"},
                            "amount": {"type": "number", "description": "金额"},
                            "type": {"type": "string", "enum": ["收入", "支出"], "description": "类型：收入/支出"}
                        },
                        "required": ["merchant", "amount", "type"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "get_summary",
                    "description": "获取用户财务汇总信息",
                    "parameters": {"type": "object", "properties": {}}
                }
            }
        ],
        "temperature": 0.7
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    return resp
