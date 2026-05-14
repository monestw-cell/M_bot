FROM python:3.9-slim
RUN apt-get update && apt-get install -y stockfish
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -r requirements.txt
EXPOSE 10000
CMD ["python", "main.py"]
