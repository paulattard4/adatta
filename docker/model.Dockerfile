# Linux environment for the Bayesian model stack (D13): PyMC + NumPyro/JAX + ArviZ.
# Same platform as the GitHub runners (linux x86_64), so local timings and results match CI.
FROM python:3.12-slim
# git: run manifests record the code SHA of the mounted repo
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* && git config --system --add safe.directory /app
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv
# keep the container's env out of the mounted repo (the Mac has its own .venv there)
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY adatta/__init__.py adatta/__init__.py
RUN uv sync --frozen --group model --no-install-project
