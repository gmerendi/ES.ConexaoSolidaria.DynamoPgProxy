FROM python:3.12-alpine

WORKDIR /app

COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/pg_dynamo_proxy.py .

EXPOSE 5450

CMD ["python", "-u", "pg_dynamo_proxy.py"]
