# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

FROM python:3.14-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.14-slim AS base

COPY --from=builder /install /usr/local

WORKDIR /app

COPY bridge/ bridge/
COPY dashboard/ dashboard/
COPY plugins/ plugins/
COPY main.py maintenance.py ./

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser
USER appuser

WORKDIR /config
