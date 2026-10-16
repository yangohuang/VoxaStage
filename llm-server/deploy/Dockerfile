FROM python@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1=12.2.0-14+deb12u1 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/worker
COPY llm-server/deploy/requirements-standalone.lock /opt/requirements.lock
RUN /opt/worker/bin/python -m pip install --timeout 120 --no-cache-dir --require-hashes -r /opt/requirements.lock \
    && /opt/worker/bin/python -m pip check

COPY llm_server.py /opt/app/llm_server.py
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONNOUSERSITE=1 PYTHONUNBUFFERED=1
USER 65532:65532
ENTRYPOINT ["/opt/worker/bin/python", "-I", "/opt/app/llm_server.py"]
