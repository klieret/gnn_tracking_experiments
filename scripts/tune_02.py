from __future__ import annotations

from typing import Any

import click
import optuna
from gnn_tracking.training.dynamiclossweights import NormalizeAt
from gnn_tracking.utils.dictionaries import subdict_with_prefix_stripped

from gte.config import auto_suggest_if_not_fixed, get_metadata
from gte.trainable import TCNTrainable, reduced_dbscan_scan, suggest_default_values
from gte.tune import common_options, main


class DynamicTCNTrainable(TCNTrainable):
    def get_loss_weights(self):
        relative_weights = [
            {
                "edge": 10,
            },
            subdict_with_prefix_stripped(self.tc, "rlw_"),
        ]
        return NormalizeAt(
            at=[0, 1],
            relative_weights=relative_weights,
        )

    def get_cluster_functions(self) -> dict[str, Any]:
        return {
            "dbscan": reduced_dbscan_scan,
        }


def suggest_config(
    trial: optuna.Trial, *, test=False, fixed: dict[str, Any] | None = None
) -> dict[str, Any]:
    config = get_metadata(test=test)
    config.update(fixed or {})

    def d(key, *args, **kwargs):
        auto_suggest_if_not_fixed(key, config, trial, *args, **kwargs)

    d("batch_size", 1)
    d("attr_pt_thld", [0.0, 0.4, 0.9])
    d("m_feed_edge_weights", True)
    d("m_h_outdim", [4])
    d("q_min", 0.3, 1)
    d("sb", 0.12, 0.135)
    d("lr", 0.0001, 0.0005)
    d("m_hidden_dim", 116)
    d("m_L_ec", 3)
    d("m_L_hc", 3)
    d("rlw_edge", 1, 10)
    d("rlw_potential_attractive", 1, 10)
    d("rlw_potential_repulsive", 2, 3)

    suggest_default_values(config, trial)
    return config


@click.command()
@common_options
def real_main(**kwargs):
    main(DynamicTCNTrainable, suggest_config, grace_period=4, **kwargs)


if __name__ == "__main__":
    real_main()
