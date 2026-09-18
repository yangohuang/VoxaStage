"""Build a source-only reviewable release; allowlist excludes local assets."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parent
FILES = '''bot.py backend.py services.py turn_observer.py turn_settings.py turn_strategy.py
avatar_backend.py avatar_session.py avatar_demo.py avatar_providers.py avatar_video_backend.py dinet_backend.py smoke_avatar.py smoke_video.py
asr_server.py test_backend.py test_services.py test_turns.py test_asr_server.py
test_avatar_backend.py test_avatar_session.py test_tts_lifecycle.py
test_avatar_providers.py test_avatar_video_backend.py test_dinet_backend.py
dialogue_backends.py omni_backend.py omni_processor.py
api_backends.py test_api_backends.py test_voice_matching.py
llm_server.py ctl.py asr_ctl.py test_ctl_config.py test_tts_resources.py
test_dialogue_backends.py test_omni_backend.py test_omni_processor.py test_omni_route.py
requirements.in requirements.lock install_demo.sh export_demo.py
.env.example .gitignore LICENSE AVATAR-README.md AVATAR-PLAN.md AVATAR-RESULTS.md
AVATAR-PROTOCOL.md AVATAR-MULTIDRIVER-RESULTS.md'''.split()


def export(output):
    files = [ROOT / name for name in FILES]
    for directory in ('avatar-web', 'avatar-server', 'video-server', 'omni-server', 'tts-compat', 'docs'):
        for path in (ROOT / directory).rglob('*'):
            if (path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts
                    and path.name != 'requirements.local.lock'
                    and (path.suffix in ('.py', '.mjs', '.js', '.html', '.css', '.md', '.txt', '.json', '.patch', '.sh', '.sha256', '.in', '.lock')
                         or path.name in ('LICENSE', 'NOTICE'))):
                files.append(path)
    manifest = []
    for path in sorted(files):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Missing or unsafe release source: {path.name}')
        manifest.append({'path': str(path.relative_to(ROOT)),
                         'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, 'w:gz') as archive:
        for item in manifest:
            archive.add(ROOT / item['path'], arcname='voxastage/' + item['path'], recursive=False)
        archive.add(ROOT / 'AVATAR-README.md', arcname='voxastage/README.md', recursive=False)
    manifest.append({'path': 'README.md', 'sha256': hashlib.sha256((ROOT / 'AVATAR-README.md').read_bytes()).hexdigest()})
    output.with_suffix('.manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'archive': str(output), 'files': len(manifest),
                      'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/voxastage-source.tar.gz')
    export(parser.parse_args().output)
