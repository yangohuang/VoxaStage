# Build context: verified distributions, punkt_tab.zip, generated locks, and
# source/ extracted from export_demo.py. No runtime configuration or models.
FROM python@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1=12.2.0-14+deb12u1 \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/application
COPY build-bootstrap.lock requirements.container.lock /opt/locks/
RUN --network=none --mount=type=bind,source=distributions,target=/opt/distributions,readonly \
    /opt/application/bin/python -m pip install --no-index --find-links /opt/distributions \
      --require-hashes -r /opt/locks/build-bootstrap.lock \
    && /opt/application/bin/python -m pip install --no-index --find-links /opt/distributions \
      --no-build-isolation --require-hashes -r /opt/locks/requirements.container.lock \
    && /opt/application/bin/python -m pip check
COPY punkt_tab.zip /opt/punkt_tab.zip
RUN /opt/application/bin/python -c "import hashlib,zipfile; from pathlib import Path; p=Path('/opt/punkt_tab.zip'); assert hashlib.sha256(p.read_bytes()).hexdigest()=='e57f64187974277726a3417ca6f181ec5403676c717672eef6a748a7b20e0106'; zipfile.ZipFile(p).extractall('/opt/application/nltk_data/tokenizers'); p.unlink()"
COPY source/ /opt/app/
COPY source.manifest.json /opt/source.manifest.json
RUN mkdir /opt/app/runtime && chown 65532:65532 /opt/app/runtime
ENV USER=voxastage HOME=/tmp PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV NLTK_DATA=/opt/application/nltk_data NUMBA_CACHE_DIR=/tmp/numba-cache OMP_NUM_THREADS=2 NUMBA_NUM_THREADS=2
WORKDIR /opt/app
USER 65532:65532
ENTRYPOINT ["/opt/application/bin/python", "-m", "uvicorn", "bot:app", "--host", "0.0.0.0", "--port", "18314"]
