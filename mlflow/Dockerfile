FROM python:3.12-slim
RUN pip install --no-cache-dir "mlflow[auth]>=3,<4" psycopg2-binary boto3
COPY entrypoint.sh /entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["sh", "/entrypoint.sh"]
