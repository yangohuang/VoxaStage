"""Trace DINet receive/encode/input pacing on one fixed PCM case; local diagnostics only."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time
from avatar_eval_metrics import MediaMeasurement
from avatar_providers import ProviderRegistry
from benchmark_avatar import close_stream, load_cases, provenance
from dinet_backend import DINetBackend


async def run(args, *, backend_factory=None):
    output=Path(args.output);output.mkdir(parents=True,exist_ok=False)
    report=dict(status='running',created_at=datetime.now(timezone.utc).isoformat(),trials=[],
                scope='single monotonic clock: adapter input/native receive/encode/paired media; not browser or perceptual quality')
    def save():
        temp=output/'.results.tmp';temp.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n');temp.replace(output/'results.json')
    save()
    try:
        if (type(args.rounds) is not int or not 1<=args.rounds<=20
                or type(args.timeout) not in (int,float) or not math.isfinite(args.timeout) or not 0<args.timeout<=300
                or not isinstance(args.deployment_label,str) or not 1<=len(args.deployment_label)<=500):
            raise ValueError('Invalid trace parameters')
        with Path(args.manifest).open('rb') as source:raw=source.read(2_000_001)
        case=next((c for c in load_cases(args.manifest,manifest_bytes=raw) if c.id==args.case),None)
        if case is None:raise ValueError('Unknown case')
        report.update(case=case.public(),manifest_sha256=hashlib.sha256(raw).hexdigest(),deployment_label=args.deployment_label,provenance=provenance())
        report['provenance']['source_sha256']['trace_dinet.py']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        url=ProviderRegistry(args.config).resolve('dinet').url if backend_factory is None else None
        for index in range(args.rounds+1):
            row=dict(index=index,phase='warmup' if index==0 else 'measured',status='running',events=[])
            report['trials'].append(row);save();started=time.monotonic()
            def observe(event):
                if len(row['events'])>=10000:raise ValueError('Trace event limit')
                row['events'].append(event|dict(at_ms=(time.monotonic()-started)*1000))
            async def source():
                for offset in range(0,len(case.pcm),12000):yield case.pcm[offset:offset+12000]
            try:
                backend=backend_factory(observe) if backend_factory else DINetBackend(url,observer=observe)
                stream=backend.stream(source());meter=MediaMeasurement(case.pcm)
                try:
                    async with asyncio.timeout(args.timeout):
                        async for event in stream:
                            meter.add(event,time.monotonic()-started)
                            if event['type']=='media':observe(dict(type='paired_media',frame=event['frame_index']))
                finally:await close_stream(stream)
                row.update(status='passed',metrics=meter.finish(time.monotonic()-started))
            except BaseException as exc:
                row.update(status='failed',error_type=type(exc).__name__)
                if not isinstance(exc,Exception):raise
            save();print(json.dumps({key:row[key] for key in ('index','phase','status')}),flush=True)
        report['status']='passed' if all(row['status']=='passed' for row in report['trials']) else 'failed'
    except BaseException as exc:
        report.update(status='failed',error_type=type(exc).__name__);save();raise
    save();return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--case',required=True)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--deployment-label',required=True)
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--timeout',type=float,default=90)
    try:report=asyncio.run(run(parser.parse_args()))
    except Exception as exc:parser.exit(1,f'Trace failed ({type(exc).__name__}); inspect results.json.\n')
    raise SystemExit(0 if report['status']=='passed' else 1)


if __name__=='__main__':main()
