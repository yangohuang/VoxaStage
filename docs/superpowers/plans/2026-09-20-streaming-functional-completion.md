# Streaming Functional Completion Implementation Plan

> **For agentic workers:** Use subagent-driven-development for bounded independent modules, then integrate and review each milestone.

**Goal:** Complete the user-approved everyday visual dialogue, real tool tasks, interruption-aware context, selectable character profiles and resumable local conversations, prioritizing streaming inference. Human quality testing is deferred by the user; multi-user, public deployment and native full-duplex/video expansion are out of scope.

**Architecture:** Retain Pipecat and the two dialogue backends. Update the everyday application to the verified visual-capable version and enable the existing MiniCPM visual worker. Add explicit bounded tools and task events to the streaming cascade. Use playback confirmations to distinguish generated from heard text, and store bounded local session snapshots. Character profiles map configured resources to backend, voice and idle without loading unknown assets.

**Tech Stack:** Existing Python/FastAPI/Pipecat, NDJSON/WebSocket, browser WebAudio/Canvas/WebGL; local JSON/SQLite storage; no new cloud requirement.

## 1. Everyday visual streaming
- [x] Inspect existing deployed worker/controller and source differences; preserve current Kanghui256/DINet container configuration and three driver endpoints.
- [x] Deploy fixed application/worker snapshots with visual input and verify real image+text and image+audio responses, first incremental output before completion, cancellation and subsequent reuse.
- [x] Capability reporting must match the actual worker. Keep VAD-turn boundary explicit; never call it native full duplex.

## 2. Real tools and task lifecycle
- [x] Add `agent_tools.py`, `agent_runtime.py`, tests: allowlisted project document search/read, bounded arithmetic and configured service status. No arbitrary shell, path or URL execution.
- [x] Use a bounded model-driven action protocol with incremental visible answer text; tool payloads never enter TTS. A tool result returns to the model before it answers. Limit turns, bytes, runtime and tool iterations.
- [x] Integrate in `services.py`, `backend.py`, `avatar_demo.py` and browser task status. New input/interrupt cancels previous work; generation/task IDs gate every result. Test delayed old results, slow task cancel, timeout and failure.
- [x] Real model must select and execute at least a document query and a calculation, returning grounded results; record first text/media and tool phases. Start with cascade tools; MiniCPM retains its genuine end-to-end path and capability UI must state the distinction.

## 3. Heard context and local sessions
- [x] Add isolated bounded `conversation_store.py` and tests: create/list/load/save/delete with atomic persistence, strict IDs, no arbitrary file paths, limits and restart recovery.
- [x] Bind playback acknowledgements to generation and clip. Keep confirmed spoken segments; mark an interrupted remainder explicitly. Do not invent word-level timing where no TTS alignment exists.
- [x] Integrate history into both supported backends; add explicit new/resume/delete session UI and local-storage notice. Raw audio/images, if necessary for MiniCPM context, remain bounded local data and outside exports.
- [x] Test resume after disconnect/application restart, stale playback acknowledgements, selected backend/profile consistency and cleanup. Real browser two-turn recall after reconnect must pass.

## 4. Character profiles
- [x] Add `avatar_profiles.py` and tests: deployment-owned profile ID/label/provider/voice/idle URL, strict schema and defaults. Browser cannot supply arbitrary service URLs.
- [x] Populate real deployed Kanghui, FlashHead and StreamingTalker profiles. Show a character selector with matching voice/idle; permit additional same-driver resources through configuration without claiming undeployed models are ready.
- [x] Verify selected profile survives session restore, live selections lock during connection, and each real profile produces matching output/idle.

## 5. Integration and delivery
- [x] Run focused module tests, cross-feature regression, real models and browser checks; preserve failures and scope limits. Independent requirements/code review before publication.
- [x] Update README/attribution/deployment/demo instructions and source export. Commit/push to the existing private PR without assistant branding.
- [x] Report implemented/verified functions separately from deferred listening quality, physical acoustics, multi-user and public operation. Do not mark the older full-quality goal achieved.

## Verification record

- 2026-09-20: 240 application tests and 18 MiniCPM worker tests passed; all 9 frontend test files passed.
- Real local Qwen selected calculation and document-search tools, then streamed grounded text.
- Real browser: 8 completed scenarios covering reconnection recall, tools, interruption/new goal, all three visual drivers, and resumed image comparison; naturally played segments persisted as heard.
- Visual worker image+text/image+audio emitted first audio before completion; cancellation followed by another successful response passed.
- Independent review caught and verified fixes for MiniCPM context/message bounds, restored image IDs, save cancellation revisions and generation-scoped TTS queues.
- Human quality assessment, physical microphone/acoustics and multi-user operation remain deferred; this record does not close the older quality goal.
