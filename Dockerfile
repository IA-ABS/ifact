# Usamos una imagen ligera de Python
FROM python:3.10-slim

# Directorio de trabajo
WORKDIR /app

# Copiamos los requerimientos primero para aprovechar el caché de Docker
COPY requirements.txt .

# Instalamos FastAPI, Playwright, etc.
RUN pip install --no-cache-dir -r requirements.txt

# ESTO ES LO MÁS IMPORTANTE: Instala Chromium y TODAS sus dependencias de Linux
RUN playwright install --with-deps chromium

# Copiamos el resto de tu código (main.py, etc.)
COPY . .

# Comando para iniciar FastAPI escuchando en el puerto dinámico de Render
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]
