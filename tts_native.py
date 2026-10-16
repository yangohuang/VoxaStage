"""Adapter for an administrator-installed, pinned official IndexTTS 2.5 checkout."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

from tts_runtime import Engine, load_voices

REVISION = 'a371df7d0746a0ae7fdf075798b6b04e34a0132e'
SOURCE_HASHES = {
    'indextts/infer_v2_5.py': 'f7cb47ffa9610e4980784d61a8bbb98c2b787b5901ef37d2d49fc54eb9684914',
    'pyproject.toml': '1da589548c52d8169bad728e479f70c50a43a95ae51fffa4f25f2250ac79458b',
    'uv.lock': '2bcec9c6bd4d20d733bdc0537a2f52b39fd67a2e88ad2308836f56fd72da1283',
}
REQUIRED_MODEL_FILES = (
    'config.yaml', 'gpt.pth', 'codec.pth', 's2mel.pth', 'wav2vec2bert_stats.pt',
    'feat1.pt', 'feat2.pt', 'multilingual_zh_ja_yue_char_del.tiktoken',
    'hf_cache/w2v-bert-2.0/model.safetensors', 'hf_cache/w2v-bert-2.0/config.json',
    'hf_cache/w2v-bert-2.0/preprocessor_config.json', 'hf_cache/campplus_cn_common.bin',
    'hf_cache/bigvgan/config.json', 'hf_cache/bigvgan/bigvgan_generator.pt',
)


def verify_source(source):
    root = Path(source).resolve(strict=True)
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], check=True,
                              capture_output=True, text=True, timeout=20).stdout.strip()
    if Path(git('rev-parse', '--show-toplevel')) != root:
        raise ValueError('Source must be a dedicated official checkout')
    if git('rev-parse', 'HEAD') != REVISION:
        raise ValueError('Unexpected IndexTTS source revision')
    if git('status', '--porcelain', '--untracked-files=no'):
        raise ValueError('IndexTTS tracked sources have local modifications')
    if git('ls-files', '--others', '--exclude-standard', 'indextts'):
        raise ValueError('Untracked files in the inference package')
    for name, expected in SOURCE_HASHES.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError('IndexTTS source checksum mismatch')
    return root


def validate_bundle(model):
    root = Path(model).resolve(strict=True)
    if any(not (root / name).is_file() or not (root / name).stat().st_size
           for name in REQUIRED_MODEL_FILES):
        raise ValueError('Incomplete local model bundle, including auxiliary models')
    config = yaml.safe_load((root / 'config.yaml').read_text())
    if (not isinstance(config, dict) or str(config.get('version')) != '2.5'
            or config.get('s2mel', {}).get('preprocess_params', {}).get('sr') != 22050):
        raise ValueError('Expected IndexTTS 2.5 with 22050 Hz decoder')
    return root


def verify_model_files(model, manifest=None):
    root = Path(model).resolve(strict=True)
    if manifest is None:
        manifest = json.loads((Path(__file__).parent / 'tts-server/model-files.json').read_text())
    files = [item for group in manifest['groups'] for item in group['files']]
    if (len(files) != len(REQUIRED_MODEL_FILES)
            or {item['local_name'] for item in files} != set(REQUIRED_MODEL_FILES)):
        raise ValueError('Model manifest coverage mismatch')
    # This optional upstream override changes text processing; it is not part of
    # the fixed baseline. An intentional variant needs its own input manifest.
    if (root / 'glossary.yaml').exists():
        raise ValueError('Untracked glossary changes the fixed synthesis baseline')
    for item in files:
        path = root / item['local_name']
        if path.stat().st_size != item['bytes']:
            raise ValueError('Model checksum mismatch: ' + item['local_name'])
        digest = hashlib.sha256()
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(8 * 1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != item['upstream_sha256']:
            raise ValueError('Model checksum mismatch: ' + item['local_name'])


class NativeGenerator:
    def __init__(self, model):
        self.model = model

    def __call__(self, voice, text):
        # Upstream decodes each text segment fully before yielding waveform samples.
        # Preserve the upstream silence chunks; do not invent a token-stream claim.
        yield from self.model.infer(
            spk_audio_prompt=str(voice), text=text, output_path=None,
            lang='zh', stream_return=True, max_text_tokens_per_segment=80,
            num_beams=1, do_sample=True, verbose=False, use_emo_text=False,
        )


def load_engine(source, model, voices):
    root = verify_source(source)
    bundle = validate_bundle(model)
    verify_model_files(bundle)
    mapping = load_voices(voices)
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    if any(name == 'indextts' or name.startswith('indextts.') for name in sys.modules):
        raise RuntimeError('Start the worker in a fresh process without another IndexTTS import')
    sys.path.insert(0, str(root))
    import torch
    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise RuntimeError('This deployment profile requires a BF16-capable CUDA GPU')
    from indextts.infer_v2_5 import IndexTTS2
    model = IndexTTS2(cfg_path=str(bundle / 'config.yaml'), model_dir=str(bundle),
                      use_bf16=True, use_gpt_latent=False, use_cuda_kernel=False,
                      use_deepspeed=False, use_accel=False, use_torch_compile=False,
                      use_qwen_emo=False)
    return Engine(NativeGenerator(model), mapping)
