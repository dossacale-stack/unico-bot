FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cache busting: cambiar este número fuerza rebuild
ARG CACHEBUST=1

COPY . .

CMD ["python", "main.py", "--dry-run"]
