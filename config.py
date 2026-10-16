import os
import tomllib

def get_secret(key):
    try:
        import streamlit as st
        val = st.secrets.get(key)
        if val is not None:
            return val
    except Exception:
        pass

    try:
        secret_path = ".streamlit/secrets.toml"
        if os.path.exists(secret_path):
            with open(secret_path, "rb") as f:
                secrets_data = tomllib.load(f)
                val = secrets_data.get(key)
                if val is not None:
                    return val
    except Exception:
        pass

    return os.environ.get(key)

DEEPSEEK_API_KEY = get_secret("DEEPSEEK_API_KEY")
DATABASE_URL = get_secret("DATABASE_URL")
def get_deepseek_api_key():
    return get_secret("DEEPSEEK_API_KEY")
