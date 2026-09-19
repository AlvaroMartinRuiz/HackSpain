FROM python:3.12.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 libgomp1
COPY v2/requirements.txt /app/v2/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/v2/requirements.txt
COPY data/catalog.json /app/data/catalog.json
COPY v2 /app/v2
RUN mkdir -p /app/v2/.data && chown -R 10001:10001 /app/v2/.data
USER 10001
EXPOSE 7861
CMD ["python", "-m", "uvicorn", "v2.api:app", "--host", "0.0.0.0", "--port", "7861", "--ws-max-size", "65536"]
