FROM python:3.13-alpine

WORKDIR /app

RUN apk upgrade --no-cache

COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONDONTWRITEBYTECODE=1

COPY src/main.py .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
