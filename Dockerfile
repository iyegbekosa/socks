FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --shell /bin/bash app
WORKDIR /app

COPY pyproject.toml ./
COPY apps ./apps
COPY config ./config
RUN pip install --no-cache-dir -e ".[dev]"

COPY . .
USER app

EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
