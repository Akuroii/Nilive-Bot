FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ensure the persistent-volume mount point exists even without a volume
RUN mkdir -p /app/data

RUN chmod +x start.sh

CMD ["bash", "start.sh"]
