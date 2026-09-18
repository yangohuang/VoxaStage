# Incremental WebSocket protocol

Run `python -m serving.incremental_server --host 127.0.0.1 --port 12544`.
The process loads one engine. `/v1/animate` remains available, and both routes
share the worker reservation reported by `/health` as `busy`.
`GET /v1/stream/info` reports incremental capabilities, defaults, audio dependency
bounds, and the same busy state. The legacy `/health` response retains its
`input_mode=complete_audio` field for HTTP compatibility.

Connect to `ws://HOST:12544/v1/stream` and send a JSON text message within ten
seconds:

```json
{"type":"start","steps":10,"seed":42,"block_ms":200,"lookahead_ms":200,"audio_context_ms":2000,"history_frames":60}
```

Only `type` is required. The other fields use the defaults shown; `steps` accepts
integers from 1 through 50. The accepted
configuration is echoed in the first server message:

```json
{"type":"metadata","input_mode":"incremental_pcm16","sample_rate":16000,"channels":1,"fps":30,"vertex_count":5023,"dtype":"<f4","faces":[],"subject":"...","config":{}}
```

Actual topology and configuration replace the abbreviated example values.
Then send binary messages containing **raw little-endian signed PCM16**, mono,
16 kHz. Each message must contain 1–16000 samples (2–32000 bytes, even length).
There is no WAV header. Read output concurrently with sending audio; the server
applies backpressure to both directions. Model frames can arrive before `end`
is sent. With the defaults, a 200 ms block plus 200 ms of lookahead requires
400 ms of input for the first six-frame batch. Relative to each frame timestamp,
the future audio dependency ranges from approximately 233 ms to 400 ms within
a full block. The `lookahead_ms=200` setting therefore does not mean 200 ms of
latency per frame. Model computation, queueing, and network transmission add
further latency; the final flush can use a shorter available audio window.

Server events are JSON text messages:

- `frame`: `index`, `pts_seconds`, `available_audio_samples`, and `vertices`.
  Decode `vertices` from base64 as little-endian float32 and reshape to
  `(vertex_count, 3)`. Indices and timestamps are supplied by the retained
  model session. `available_audio_samples` is the absolute end sample of the audio window
  actually used for that frame, which permits auditing lookahead. Total input
  received is `stats.received_samples`; neither value is wall-clock latency.
- `progress`: a `stats` object after each processed input chunk.
- `done`: `cleanup_complete=true` confirms successful session cleanup. Its
  `stats` is the generation-end snapshot taken before cleanup, preserving
  history lengths for auditing; `stats.closed=false` describes that snapshot.
  Cleanup failure produces `error` instead of `done`.
- `error`: terminal `code` and `message`; a competing session receives `busy`.
- `cancelled`: terminal acknowledgement of an explicit cancellation.

Send `{"type":"end"}` once to flush the remaining frames and finish. Continue
reading until `done` or `error`. Audio or another `end` after `end` is invalid;
the socket closes after the terminal event. Once a terminal send begins, later
control messages cannot replace it or produce a second terminal event.
Cancellation can interrupt a terminal send already in flight, so delivery is
not guaranteed when the connection is cancelled. To abort, send
`{"type":"cancel"}` or disconnect. Reconnect creates a fresh session; there is
no resume token or implicit continuation after a network disconnect.

The input queue holds four chunks and the output queue holds eight events.
There can additionally be one input chunk waiting for capacity, one executing
model call and its returned frame batch, and one in-flight socket send. The CLI
also bounds Uvicorn's incoming WebSocket queue to four messages and message
size to 32000 bytes. An oversized message may therefore be rejected at the
WebSocket layer with close code 1009 before an application `error` event.

Cancellation stops queue processing but cannot preempt a running model call.
The reservation stays busy until that call returns and session cleanup finishes.
A cancel message behind buffered audio is subject to incoming backpressure;
clients should stop sending audio when cancelling. No global RNG state is
modified by this transport.

CPU tests in `tests/test_incremental_api.py` exercise the transport with stub
sessions. They do not demonstrate real-model output quality or GPU performance.
