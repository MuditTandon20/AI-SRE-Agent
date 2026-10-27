FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 WORKER_HEARTBEAT_FILE=/tmp/ai-sre-heartbeat
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -c constraints.txt -r requirements.txt
COPY ai_sre_agent ai_sre_agent
COPY main.py pyproject.toml LICENSE ./
COPY examples examples
RUN useradd --uid 10001 --create-home agent && mkdir -p /logs && chown agent:agent /logs
USER 10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
