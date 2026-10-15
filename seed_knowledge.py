# seed_knowledge.py
import os
import requests
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
EMBED_URL = "https://open.bigmodel.cn/api/paas/v4/embeddings"

def get_embedding(text):
    headers = {
        "Authorization": f"Bearer {ZHIPU_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "embedding-2",
        "input": text
    }
    resp = requests.post(EMBED_URL, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]

def main():
    knowledge_list = [
        "国家助学贷款最高额度为每年12000元，在校期间利息由财政全额补贴，毕业后开始计息。",
        "校园贷常见套路是收取砍头息，实际到手金额低于合同金额，叠加高额违约金与催收风险。",
        "花呗分期真实年化利率通常高达15%以上，页面展示的月费率不等于真实年化IRR。",
        "大学生每月消费建议预留10%结余，优先储蓄，减少高频小额奶茶、外卖等非必要支出。",
        "信用卡不建议大学生盲目办理，逾期会留下征信污点，影响后续助学贷款与求职。"
    ]
    supabase.table("knowledge_base").delete().neq("id", 0).execute()
    for content in knowledge_list:
        emb = get_embedding(content)
        supabase.table("knowledge_base").insert({
            "content": content,
            "embedding": emb
        }).execute()
    print("知识库数据写入完成")

if __name__ == "__main__":
    main()
