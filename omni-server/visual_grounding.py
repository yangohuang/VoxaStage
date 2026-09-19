"""Server-selected visual evidence prompts for controlled inference experiments."""
from PIL import Image

POLICIES = ('baseline', 'general', 'evidence')
GENERAL_RULE = (
    ' 回答关于图片、画面或视频的问题时，只能依据当前对话实际提供的图片。'
    '没有可查看的图片时，请说明尚未收到画面并请用户提供，不得猜测颜色、形状或场景。'
    '普通非视觉问题正常回答。'
)


def build_system_prompt(base, messages, policy='baseline'):
    if policy not in POLICIES:
        raise ValueError('Unknown visual grounding policy')
    if policy == 'baseline':
        return base
    prompt = base + GENERAL_RULE
    if policy == 'general':
        return prompt
    counts = [sum(isinstance(item, Image.Image) for item in message['content'])
              for message in messages]
    current = counts[-1] if counts else 0
    history = sum(counts[:-1])
    return prompt + (
        f' 服务端视觉证据状态：本轮图片数={current}；历史可见图片数={history}；合计={current + history}。'
        '这些数量只包含仍在模型输入中的实际图片。用户提到“图中”或旧回答中的描述不代表已提供图片。'
    )
