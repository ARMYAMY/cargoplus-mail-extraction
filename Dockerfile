FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN (sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
     sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list 2>/dev/null || true) \
    && apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 cargo

COPY requirements.txt requirements.lock /app/
RUN python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ --no-deps -r /app/requirements.lock \
    && python -m pip uninstall -y pip setuptools wheel jaraco.context

COPY --chown=cargo:cargo app /app/app
COPY --chown=cargo:cargo skill_v3 /opt/cargo-mail-extraction-skill-v3

RUN mkdir -p /app/data/uploads \
    && chown -R cargo:cargo /app/data

USER cargo

ENV SKILL_V3_PATH=/opt/cargo-mail-extraction-skill-v3
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
