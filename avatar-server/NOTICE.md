# Source and attribution

Model project: [StreamingTalker](https://github.com/zju3dv/StreamingTalker), ZJU3DV.
The bundled historical upstream `LICENSE` states `Copyright 2025 ZJU3DV` and
Apache License, Version 2.0; its bytes are preserved unchanged.

This package supplies **local serving extensions**, not an official StreamingTalker
API release. `serving/` adds a model loader, legacy HTTP adapter, bounded incremental
audio clock and inference state, WebSocket transport, and command-line launcher.
The transport and clock tests are local extension tests. They were copied from the
existing local incremental-serving source without importing weights or assets.

`headless-model.patch` modifies upstream `algorithms/models/diff_ar.py` by moving
the optional `utils.demo_utils.animate` import into the visualization method,
adding a prominent local modification notice, and normalizing its final newline.
It does not change the neural network calculations. These modifications are
separate from the upstream project and are not endorsed by its authors.

Export-only changes: the copied protocol example now binds to loopback, and the
CPU state-test docstring no longer names a development host. Serving Python files
and the existing incremental dependency file are copied byte for byte.

The historical baseline is
`25b613ac273624947e5fa51c4c41c4933d8147be`. The later upstream commit
`1f6452752dbd6c11f4bb1e94982e000e68d8f76d` changes the upstream LICENSE to Project
Registration License v1.0. This package retains the historical license and pins
its documented setup to the historical code. It does not describe current
upstream `main` as Apache-2.0 or make claims about licenses of weights or datasets.

Distribution audit manifests cover the selected runtime source files; unused training-only entries are omitted. Setup documentation and audit-manifest hashes are updated for this source distribution. Upstream license bytes and bundled serving source are unchanged.
