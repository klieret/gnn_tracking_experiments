from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import optuna

from gnn_tracking_hpo.config import get_metadata
from gnn_tracking_hpo.trainable import TCNTrainable, suggest_default_values
from gnn_tracking_hpo.tune import main

DATA_DIR = Path(__file__).resolve().parent.parent / "test_data" / "data" / "graphs"


def suggest_config(trial: optuna.Trial, *args, **kwargs) -> dict[str, Any]:
    config = get_metadata(test=True)
    suggest_default_values(config, trial)
    return config


def test_tune():
    os.environ["DATA_DIR"] = str(DATA_DIR)
    result = main(
        TCNTrainable,
        suggest_config,
        test=True,
    )
    assert not result.errors
