FROM python:3.13-alpine

WORKDIR /app

RUN apk upgrade --no-cache

COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONDONTWRITEBYTECODE=1

# Copy the full backend source tree so intra-module imports work
# (e.g., main.py -> agent_design.py -> prompts/system_prompt.md).
COPY src/ .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
