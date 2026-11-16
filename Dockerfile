FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY migrations ./migrations
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home manager && install -d -m 0700 -o manager -g manager /app/data
USER manager
CMD ["sh", "-c", "umask 077 && exec python -m app.main serve --migrate"]
