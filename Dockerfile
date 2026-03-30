# Usamos la imagen oficial de Microsoft que YA TRAE Chrome y las dependencias instaladas
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

# Directorio de trabajo
WORKDIR /app

# Copiamos los archivos y requerimientos
COPY requirements.txt .

# Instalamos FastAPI, Uvicorn, etc.
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos tu archivo main.py
COPY . .

# Comando para encender el servidor Robot
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
