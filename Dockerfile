FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --system django \
    && useradd --system --gid django --create-home django \
    && mkdir -p /vol/web/static \
    && chown -R django:django /vol/web

COPY --chown=django:django . /app
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/partsmall-entrypoint

USER django
EXPOSE 8000
ENTRYPOINT ["partsmall-entrypoint"]
CMD ["gunicorn", "--config", "config/gunicorn.conf.py", "config.wsgi:application"]
