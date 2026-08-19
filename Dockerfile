FROM python:3.11-slim@sha256:9c900dea9e8fb7e16277c179b555cc72d29a352dbc33cff48ad5a0412fd5bfc7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 cargo

COPY cargo_service/requirements.txt cargo_service/requirements.lock /app/
RUN python -m pip install --no-deps -r /app/requirements.lock \
    && python -m pip uninstall -y pip setuptools wheel jaraco.context

COPY --chown=cargo:cargo cargo_service/app /app/app
COPY --chown=cargo:cargo cargo-mail-extraction-skill-v3 /opt/cargo-mail-extraction-skill-v3

RUN mkdir -p /app/data/uploads \
    && chown -R cargo:cargo /app/data

USER cargo

ENV SKILL_V3_PATH=/opt/cargo-mail-extraction-skill-v3
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
