"""Load the official checkpoint and delegate frame generation to upstream."""
import pickle
from pathlib import Path

import numpy as np
import torch
import trimesh
from omegaconf import OmegaConf
from transformers import Wav2Vec2FeatureExtractor

from algorithms.models import get_model

ROOT = Path(__file__).resolve().parents[1]
SUBJECTS = ['FaceTalk_170728_03272_TA', 'FaceTalk_170904_00128_TA',
            'FaceTalk_170725_00137_TA', 'FaceTalk_170915_00223_TA',
            'FaceTalk_170811_03274_TA', 'FaceTalk_170913_03279_TA',
            'FaceTalk_170904_03276_TA', 'FaceTalk_170912_03278_TA']


class StreamingTalkerEngine:
    fps = 30

    def __init__(self, device='cuda', subject=SUBJECTS[0]):
        self.device = device
        self.subject = subject
        self.cfg = OmegaConf.load(ROOT / 'configs/vocaset/stage2_diffar.yaml')
        self.cfg.MODEL.VAE.pretrained = str(ROOT / 'checkpoints/voca_vae.ckpt')
        self.cfg.DATASET.DATA_ROOT = str(ROOT / 'data/vocaset')
        self.cfg.MODEL.AUDIO_ENCODER.model_name_or_path = str(ROOT / 'checkpoints/hubert-base-ls960')
        with open(ROOT / 'data/vocaset/templates.pkl', 'rb') as f:
            templates = pickle.load(f, encoding='latin1')
        self.template = np.asarray(templates[subject], dtype=np.float32).reshape(-1, 3)
        self.faces = np.asarray(trimesh.load(ROOT / 'data/vocaset/templates/FLAME_sample.ply',
                                            process=False).faces, dtype=np.int32)
        self.model = get_model(self.cfg)
        checkpoint = torch.load(ROOT / 'checkpoints/diffar_voca_241120.ckpt', map_location='cpu')
        self.model.load_state_dict(checkpoint['state_dict'], strict=True)
        del checkpoint
        self.model.to(device).eval()
        self.feature_extractor = Wav2Vec2FeatureExtractor(
            feature_size=1, sampling_rate=16000, padding_value=0.0,
            do_normalize=True, return_attention_mask=False)
        self.identity = torch.zeros(1, len(SUBJECTS), device=device)
        self.identity[0, SUBJECTS.index(subject)] = 1
        self.template_tensor = torch.from_numpy(self.template.reshape(-1)).to(device)

    def generate(self, audio, steps, seed, sink):
        self.cfg.MODEL.num_inference_timesteps = steps
        torch.manual_seed(seed)
        features = self.feature_extractor(audio, sampling_rate=16000, return_tensors='pt').input_values
        inputs = {'audio': features.to(self.device), 'id': self.identity,
                  'template': self.template_tensor}
        with torch.inference_mode():
            self.model.streaming_inference(inputs, sink)
