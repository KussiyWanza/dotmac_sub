FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for WeasyPrint (PDF generation)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    iputils-ping \
    iproute2 \
    procps \
    util-linux \
    snmp \
    wireguard-tools \
    && rm -rf /var/lib/apt/lists/*

ENV POETRY_VERSION=2.4.1 \
    POETRY_HOME=/opt/poetry \
    VIRTUAL_ENV=/opt/venv

RUN python -m venv "$POETRY_HOME" \
    && "$POETRY_HOME/bin/pip" install --no-cache-dir "poetry==$POETRY_VERSION" \
    && python -m venv "$VIRTUAL_ENV"

ENV PATH="$VIRTUAL_ENV/bin:$POETRY_HOME/bin:$PATH"

RUN poetry config virtualenvs.create false

COPY pyproject.toml poetry.lock ./
# dotmac-kernel resolves from the private Forgejo index; the read token is a
# BuildKit secret (id=forgejo_token) so it never lands in a layer. Build with:
#   docker build --secret id=forgejo_token,src=<file-with-token> .
RUN --mount=type=secret,id=forgejo_token \
    POETRY_HTTP_BASIC_FORGEJO_USERNAME=ci-reader \
    POETRY_HTTP_BASIC_FORGEJO_PASSWORD="$(cat /run/secrets/forgejo_token)" \
    poetry install --only main --no-interaction --no-ansi

COPY . .

EXPOSE 8001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
