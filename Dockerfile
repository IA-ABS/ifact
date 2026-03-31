FROM mcr.microsoft.com/playwright/python:v1.39.0-jammy

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Comando para iniciar Streamlit en el puerto de Render
CMD ["sh", "-c", "streamlit run main.py --server.port ${PORT:-10000} --server.address 0.0.0.0"]
