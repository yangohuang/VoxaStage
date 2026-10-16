# Minimal context: this recipe, original worker/lock, bootstrap lock, and verified
# original distributions. Python installation cannot fetch unpinned build tools.
FROM python@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1=12.2.0-14+deb12u1 sox=14.4.2+git20190427-3.5 \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/worker
COPY asr-server/requirements.lock /opt/requirements.lock
COPY asr-server/deploy/build-bootstrap.lock /opt/build-bootstrap.lock
RUN --network=none --mount=type=bind,source=distributions,target=/opt/distributions,readonly \
    /opt/worker/bin/python -m pip install --no-index --find-links /opt/distributions \
      --require-hashes -r /opt/build-bootstrap.lock \
    && /opt/worker/bin/python -m pip install --no-index --find-links /opt/distributions \
      --no-build-isolation --require-hashes -r /opt/requirements.lock \
    && /opt/worker/bin/python -m pip check
COPY asr_server.py /opt/app/asr_server.py
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONNOUSERSITE=1 PYTHONUNBUFFERED=1
ENV USER=voxastage HOME=/tmp NUMBA_CACHE_DIR=/tmp/numba-cache
ENV OMP_NUM_THREADS=2 NUMBA_NUM_THREADS=2 TOKENIZERS_PARALLELISM=false
USER 65532:65532
ENTRYPOINT ["/opt/worker/bin/python", "-I", "/opt/app/asr_server.py"]
