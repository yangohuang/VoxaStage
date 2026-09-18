"""Local Pipecat browser voice agent using the existing Ubuntu02 model services."""
import asyncio
from importlib.metadata import version

import httpx
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.runner.run import app, main
from pipecat.runner.types import SmallWebRTCRunnerArguments
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from backend import LocalBackend
from services import LocalIndexTTS, LocalQwenLLM, LocalASRSTT
from turn_observer import TurnObserver
from turn_settings import TurnSettings
from turn_strategy import LocalTurnStopStrategy
from avatar_demo import register_avatar_routes

SYSTEM = '你是中文语音助手。依据上下文准确回答，保留英文技术术语。简单问题简短回答；解释性问题说清关键原因并给一个具体例子，通常不超过180字。使用自然口语，不用Markdown。不确定时明确说明，听不清或问题含糊时先澄清，不编造事实或实时信息。'
# Ground project-specific answers in verified architecture; this is not retrieval.
SYSTEM += (' 仅在问题涉及本演示项目时参考以下事实：Pipecat是由Daily与社区维护的实时语音和多模态Agent编排框架；'
           'MiniCPM-o是OpenBMB项目的端到端多模态模型。框架与模型处于不同层，不能当作互斥的两种模型。'
           '本演示的级联路径为ASR识别、LLM生成文本、TTS合成声音；另一路MiniCPM-o直接理解语音并生成语音，仍由Pipecat处理会话。'
           '数字人形象驱动独立于对话后端：DINet和FlashHead输出2D视频，StreamingTalker输出3D网格。'
           '音色决定声音，渲染器决定形象；分离后可以换形象而保留声音，或换声音而不重写渲染器。'
           '不要编造厂商归属、已执行的工具动作、联网结果或实时数据。')
TURN_SETTINGS = TurnSettings.from_env()
active_sessions = set()
active_connections = {}
active_workers = {}


@app.post('/admin/cancel-sessions')
async def cancel_sessions():
    """Local controller closes sessions before sending the server SIGTERM."""
    await asyncio.gather(*(worker.cancel() for worker in list(active_workers.values())))
    await asyncio.gather(*(connection.disconnect() for connection in list(active_connections.values())))
    return {'cancelled': True}


@app.get('/health')
async def health():
    async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
        dependencies = await LocalBackend(client).health()
    return {'ready': all(s['ready'] for s in dependencies.values()),
            'pipecat_version': version('pipecat-ai'), 'active_sessions': len(active_sessions),
            'mode': 'local_cascade_webrtc', 'dependencies': dependencies,
            'asr': 'VAD-segmented, continuous speech split into 28-second segments',
            'input_sample_rate': 16000, 'output_sample_rate': 24000,
            'turn_settings': TURN_SETTINGS.as_dict()}


def make_components(backend):
    context = LLMContext([{'role': 'system', 'content': SYSTEM}])
    aggregators = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(sample_rate=16000, params=VADParams(stop_secs=TURN_SETTINGS.vad_stop_secs)),
        user_turn_strategies=UserTurnStrategies(stop=[LocalTurnStopStrategy(user_speech_timeout=TURN_SETTINGS.turn_wait_secs)]),
    ))
    return LocalASRSTT(backend), aggregators, LocalQwenLLM(backend), LocalIndexTTS(backend)


async def bot(runner_args: SmallWebRTCRunnerArguments):
    if active_sessions:
        logger.warning('Local model workers support one active browser session')
        await runner_args.webrtc_connection.disconnect()
        return
    key = runner_args.session_id
    active_sessions.add(key)
    active_connections[key] = runner_args.webrtc_connection
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=120) as client:
            backend = LocalBackend(client)
            stt, aggregators, llm, tts = make_components(backend)
            transport = SmallWebRTCTransport(runner_args.webrtc_connection,
                TransportParams(audio_in_enabled=True, audio_out_enabled=True,
                                audio_in_sample_rate=16000, audio_out_sample_rate=24000))
            worker = PipelineWorker(Pipeline([
                transport.input(), stt, aggregators.user(), llm, tts,
                transport.output(), aggregators.assistant(),
            ]), params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
                idle_timeout_secs=600, observers=[TurnObserver(key, llm=llm)])
            runner = WorkerRunner(handle_sigint=False)
            active_workers[key] = worker
            await runner.add_workers(worker)

            @transport.event_handler('on_client_disconnected')
            async def disconnected(transport, client):
                await worker.cancel()

            await runner.run()
    finally:
        active_workers.pop(key, None)
        active_connections.pop(key, None)
        active_sessions.discard(key)


register_avatar_routes(app, make_components, active_sessions, active_workers)


if __name__ == '__main__':
    main()
