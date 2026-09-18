"""Launch the optional incremental endpoint alongside the legacy HTTP API."""
import argparse
import os


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=12544)
    args = parser.parse_args()
    os.environ.setdefault('OMP_NUM_THREADS', '4')
    import torch
    import uvicorn
    from serving.engine import StreamingTalkerEngine
    from serving.incremental_api import create_incremental_app
    torch.set_num_threads(4)
    engine = StreamingTalkerEngine()
    uvicorn.run(create_incremental_app(engine), host=args.host, port=args.port,
                workers=1, ws_max_size=32000, ws_max_queue=4)


if __name__ == '__main__':
    main()
