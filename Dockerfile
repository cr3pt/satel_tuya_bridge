# Jeden obraz dla obu procesów (bridge + panel webowy) - dzielą ten sam
# kod i zależności, różni je tylko polecenie uruchomieniowe (patrz
# docker-compose.yml). Working directory z kodem to /app; katalog z danymi
# (config.db, bridge_history.db, backupy) to zamontowany wolumen /config -
# dokładnie tak samo jak przy uruchomieniu bez Dockera (main.py i
# dashboard/app.py trzymają ścieżki względne wobec katalogu roboczego).
# python:3.12+ WYMAGANE przez satel-integra>=1.0.0 (requires-python >=3.12,
# patrz requirements.txt i obszerny docstring bridge/satel_client.py);
# obraz używa 3.13, bo pysmartthings 4.x wymaga Pythona >=3.13.
#
# Multi-stage build: gcc/libffi-dev są potrzebne TYLKO do zbudowania kilku
# zależności (np. bcrypt używany przez werkzeug.security) na architekturach
# bez gotowych wheeli (np. RPi armv7/arm64) - na amd64 pip i tak najczęściej
# weźmie gotowy wheel. Kompilator instalujemy w osobnym etapie budowania i
# kopiujemy do finalnego obrazu tylko zainstalowane pakiety, nie sam gcc -
# mniejszy obraz i mniejsza powierzchnia ataku (brak kompilatora w obrazie
# uruchomieniowym).
FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.13-slim AS base

COPY --from=builder /install /usr/local

WORKDIR /app

COPY bridge/ bridge/
COPY dashboard/ dashboard/
COPY plugins/ plugins/
COPY main.py maintenance.py ./

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Użytkownik bez uprawnień roota - katalog /config (wolumen z hosta) musi
# być zapisywalny dla UID:GID 1000:1000, patrz README (sekcja "Docker").
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser
USER appuser

WORKDIR /config
