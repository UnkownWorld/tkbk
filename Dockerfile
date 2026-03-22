FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_API_HOST=https://ai.wsocket.xyz/v1 \
    APP_MODEL=gpt-5.4 \
    APP_TEMPERATURE=0.7 \
    APP_TOP_P=0.65 \
    APP_MAX_TOKENS=1000000 \
    APP_MAX_OUTPUT_TOKENS=50000 \
    APP_CONTEXT_ROUNDS=100 \
    APP_MAX_CONCURRENT_TASKS=10 \
    APP_MAX_THREAD_WORKERS=10

# 敏感配置 - 通过 HF Space Secrets 设置:
# APP_API_KEY       → API 密钥
# APP_HF_TOKEN      → HuggingFace Token
# APP_HF_DATASET    → 数据集名称（不填则自动使用 username/bk1）
# APP_SYSTEM_PROMPT → 聊天功能默认系统提示词
# APP_BATCH_SYSTEM_PROMPT → 批处理功能默认系统提示词
# APP_BATCH_USER_PROMPT_TEMPLATE → 批处理功能用户提示词模板

WORKDIR /app

RUN pip install --no-cache-dir flask flask-cors requests huggingface_hub

COPY . /app

EXPOSE 7860

CMD ["python", "run.py"]
