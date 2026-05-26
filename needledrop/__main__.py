import argparse
import asyncio
import logging
from pathlib import Path

from needledrop.app import run
from needledrop.config import load_config

parser = argparse.ArgumentParser(description="needledrop — vinyl record fingerprinting")
parser.add_argument("--config", required=True, help="path to config.yaml")
args = parser.parse_args()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

config = load_config(Path(args.config))
asyncio.run(run(config))
