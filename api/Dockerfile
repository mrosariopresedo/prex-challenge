FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
# Usuario sin privilegios (no correr como root)
RUN useradd -m -u 1000 appuser && mkdir -p /app/data && chown appuser /app/data
USER appuser
ENV DATA_DIR=/app/data
EXPOSE 5000
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "2", "app:app"]
