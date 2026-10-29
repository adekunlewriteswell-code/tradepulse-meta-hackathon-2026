"""Launch the explicitly enabled offline demonstration on IPv4 loopback only."""
import argparse

import uvicorn

from app.config import Settings
from app.main import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['mock', 'simulated'], default='simulated')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Select a port between 1 and 65535.')
    settings = Settings(demo_enabled=True, demo_default_mode=args.mode, use_mock_ai=True,
                        meta_live_enabled=False, model_api_key=None)
    uvicorn.run(create_app(settings), host='127.0.0.1', port=args.port,
                proxy_headers=False, access_log=False)


if __name__ == '__main__':
    main()
