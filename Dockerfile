FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml .
COPY ghrec ghrec
COPY service service
RUN pip install --no-cache-dir ".[service,aws]"
# image = version: CD copies the promoted champion.pkl into ./artifacts before build (the API needs it, jobs do not).
# Jobs reuse this image with a different command (python -m ghrec.ingest / ghrec.train).
RUN mkdir -p artifacts
CMD ["uvicorn", "service.app:app", "--host", "0.0.0.0", "--port", "8000"]
