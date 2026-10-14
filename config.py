# config.py
import streamlit as st
import os

# DeepSeek API Key
def get_deepseek_api_key():
    try:
        api_key = st.secrets["DEEPSEEK_API_KEY"]
    except Exception:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
    return api_key

# Database URL, priority: st.secrets then env
def get_database_url():
    try:
        db_url = st.secrets["DATABASE_URL"]
    except Exception:
        db_url = os.environ.get("DATABASE_URL", "sqlite:///finance.db")
    return db_url

