from __future__ import annotations

import logging

from .ui.application import App


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return App().run(None)
