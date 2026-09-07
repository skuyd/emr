FROM python:3.11.16-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    EXPORT_TEMP_DIRECTORY=/var/lib/phr/export-tmp \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True

ARG DEBIAN_MIRROR=
RUN if [ -n "$DEBIAN_MIRROR" ]; then \
      sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install --yes --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 phr \
    && useradd --uid 10001 --gid phr --create-home --shell /usr/sbin/nologin phr

RUN install -d -m 0700 -o phr -g phr /var/lib/phr/export-tmp

WORKDIR /app
COPY requirements-prod.lock ./
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN python -m pip install --require-hashes --requirement requirements-prod.lock --index-url "$PIP_INDEX_URL"

COPY manage.py pyproject.toml ./
COPY apps ./apps
COPY config ./config
COPY static ./static
COPY docs/licenses/noto-sans-sc-ofl.txt ./licenses/noto-sans-sc-ofl.txt
COPY templates ./templates
COPY tools ./tools
COPY deploy/gunicorn.conf.py ./deploy/gunicorn.conf.py

RUN DJANGO_SETTINGS_MODULE=config.settings.build \
    python manage.py collectstatic --noinput --skip-checks \
    && chown -R phr:phr /app/staticfiles

USER 10001:10001

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,urllib.request; request=urllib.request.Request('http://127.0.0.1:8000/health/live/', headers={'Host':os.environ['APP_DOMAIN'],'X-Forwarded-Proto':'https'}); urllib.request.urlopen(request, timeout=3).read()"

CMD ["gunicorn", "--config", "deploy/gunicorn.conf.py", "config.wsgi:application"]
