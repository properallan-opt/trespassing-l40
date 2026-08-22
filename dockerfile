FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Etc/UTC \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 \
        python3-pip \
        git \
        openssh-client \
        supervisor \
        ca-certificates \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN python3.10 -m pip install --upgrade pip setuptools wheel

WORKDIR /opt/trespassing-source
COPY requirements.txt /tmp/requirements.txt
RUN python3.10 -m pip install -r /tmp/requirements.txt

COPY . /opt/trespassing-source
RUN chmod +x /opt/trespassing-source/entrypoint.sh /opt/trespassing-source/main_trespassing.sh /opt/trespassing-source/main_oclusao.sh \
    && mkdir -p /config /app/logs /var/log/supervisord

ENV APP_DIR="/app/source" \
    BUNDLED_SOURCE="/opt/trespassing-source" \
    DEPLOY_SOURCE="git" \
    GIT_USER="gitlab+deploy-token-7" \
    GIT_PASS="" \
    GIT_REPO="git@gitlab.t4stecnologia.com.br:anjoscarga/imageengineprocessorengines/trespassing.git" \
    GIT_REF="main"

    

ENTRYPOINT ["/opt/trespassing-source/entrypoint.sh"]
CMD ["trespassing", "hml", "1"]
