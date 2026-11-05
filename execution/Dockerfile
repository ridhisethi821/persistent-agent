FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash \
    && rm -rf /var/lib/apt/lists/*

RUN pip install \
    pandas \
    openpyxl \
    python-docx \
    pymupdf \
    pillow

WORKDIR /workspace

RUN useradd --create-home --uid 10001 agent \
    && chown -R agent:agent /workspace

USER agent

CMD ["bash"]
