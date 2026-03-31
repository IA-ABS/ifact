# 1. Usamos la imagen oficial de Playwright (basada en Ubuntu 22.04 y Python)
# Esta versión coincide con tu requirements.txt (1.39.0)
FROM mcr.microsoft.com/playwright/python:v1.39.0-jammy

# 2. Variables de entorno recomendadas para Python en Docker
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 3. Directorio de trabajo
WORKDIR /app

# 4. Copiamos e instalamos dependencias de Python (FastAPI, etc.)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copiamos el resto de tu código
COPY . .

# 6. Exponemos el puerto de Render y arrancamos
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]
