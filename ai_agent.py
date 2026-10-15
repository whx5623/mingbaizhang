# ai_agent.py
import requests
import json
import os
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

ZHIPU_EMBED_URL = "https://open.bigmodel.cn/api/paas/v4/embeddings"


def get_embedding(text: str, zhipu_api_key: str):
    headers = {
        "Authorization": f"Bearer {zhipu_api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "embedding-2",
        "input": text
    }
    resp = requests.post(ZHIPU_EMBED_URL, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    embedding = resp.json()["data"][0]["embedding"]
    return embedding


def search_knowledge_base(query_embedding, top_k=3):
    res = supabase.rpc(
        "match_knowledge",
        {
            "query_embedding": query_embedding,
            "match_count": top_k
        }
    ).execute()
    return res.data


def build_system_prompt(total_income, total_expense, balance, recent_bills_text, retrieved_knowledge):
    knowledge_text = "\n".join([item["content"] for item in retrieved_knowledge])
    prompt = f"""你是大学生AI金融导师，支持自动记账工具调用。
参考知识：
{knowledge_text}
用户财务背景：
总收入：{total_income:.2f}元
总支出：{total_expense:.2f}元
结余：{balance:.2f}元
最近5笔账单：
{recent_bills_text}
结合参考知识与账单数据，给出适合大学生的财务建议。
如果用户提到一笔消费或收入，请调用add_bill工具自动记录账单。
"""
    return prompt


def get_ai_response(messages, api_key, zhipu_api_key, total_income, total_expense, balance, recent_bills_text):
    last_user_msg = ""
    for msg in reversed(messages):
        if msg["role"] == "user":
            last_user_msg = msg["content"]
            break
    emb = get_embedding(last_user_msg, zhipu_api_key)
    kb_results = search_knowledge_base(emb, top_k=3)
    sys_prompt = build_system_prompt(total_income, total_expense, balance, recent_bills_text, kb_results)

    tools = [
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
                        "payment_method": {"type": "string", "description": "支付方式"},
                        "type": {"type": "string", "description": "收入/支出"}
                    },
                    "required": ["date", "merchant", "amount", "payment_method", "type"]
                }
            }
        }
    ]

    new_messages = [{"role": "system", "content": sys_prompt}] + messages
    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "deepseek-chat",
        "messages": new_messages,
        "tools": tools,
        "temperature": 0.7
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    return resp
