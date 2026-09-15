FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LLM_WIKI_DATABASE=/var/lib/llm-wiki/lw.sqlite3

WORKDIR /app
COPY plugins/llm-wiki /app/plugins/llm-wiki
RUN pip install --no-cache-dir /app/plugins/llm-wiki \
    && useradd --create-home --uid 10001 lw \
    && mkdir -p /var/lib/llm-wiki \
    && chown -R lw:lw /app /var/lib/llm-wiki

USER lw
VOLUME ["/var/lib/llm-wiki"]
EXPOSE 8000 4310

CMD ["llm-wiki-web", "--host", "0.0.0.0", "--port", "8000"]
