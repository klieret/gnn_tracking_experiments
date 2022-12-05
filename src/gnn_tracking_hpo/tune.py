#!/usr/bin/env python3

from __future__ import annotations

import functools
from functools import partial
from pathlib import Path

import click
import pytimeparse
from ray import tune
from ray.air import CheckpointConfig, FailureConfig, RunConfig
from ray.air.callbacks.wandb import WandbLoggerCallback
from ray.tune import Callback, Stopper, SyncConfig
from ray.tune.schedulers import ASHAScheduler
from ray.tune.search.optuna import OptunaSearch
from ray.tune.stopper import (  # TrialPlateauStopper,
    CombinedStopper,
    MaximumIterationStopper,
    TimeoutStopper,
)
from wandb_osh.ray_hooks import TriggerWandbSyncRayHook

from gnn_tracking_hpo.cli import enqueue_option, gpu_option, test_option, wandb_options
from gnn_tracking_hpo.config import della, get_points_to_evaluate, read_json
from gnn_tracking_hpo.orchestrate import maybe_run_distributed, maybe_run_wandb_offline
from gnn_tracking_hpo.util.stoppers import NoImprovementStopper

server = della


def common_options(f):
    @test_option
    @gpu_option
    @click.option(
        "--restore",
        help="Restore previous training state from this directory",
        default=None,
    )
    @enqueue_option
    @click.option(
        "--only-enqueued",
        help="Only run enqueued points, do not tune any parameters",
        is_flag=True,
    )
    @click.option(
        "--fixed",
        help="Read config values from file and fix these values in all trials.",
    )
    @click.option(
        "--timeout",
        help="Stop all trials after certain time. Natural time specifications "
        "supported.",
    )
    @click.option(
        "--fail-slow",
        help="Do not abort tuning after trial fails.",
        is_flag=True,
    )
    @click.option(
        "--dname",
        help="Name of ray output directory",
        default="tcn",
    )
    @wandb_options
    @functools.wraps(f)
    def wrapper_common_options(*args, **kwargs):
        return f(*args, **kwargs)

    return wrapper_common_options


def main(
    trainable,
    suggest_config,
    *,
    test=False,
    gpu=False,
    restore=None,
    enqueue: None | list[str] = None,
    only_enqueued=False,
    fixed: None | str = None,
    grace_period=3,
    timeout=None,
    tags=None,
    group=None,
    note=None,
    fail_slow=False,
    dname="tcn",
):
    """
    For most argument, see corresponding command line interface.

    Args:
        trainable: The trainable to run.
        suggest_config: A function that returns a config dictionary.
        grace_period: Grace period for ASHA scheduler.
    """
    maybe_run_wandb_offline()

    maybe_run_distributed()

    timeout_seconds = pytimeparse.parse(timeout) if timeout else None
    del timeout

    points_to_evaluate = get_points_to_evaluate(enqueue)

    fixed_config = None
    if fixed:
        fixed_config = read_json(Path(fixed))

    optuna_search = OptunaSearch(
        partial(suggest_config, test=test, fixed=fixed_config),
        metric="trk.double_majority_pt1.5",
        mode="max",
        points_to_evaluate=points_to_evaluate,
    )
    if restore:
        print(f"Restoring previous state from {restore}")
        optuna_search.restore_from_dir(restore)

    num_samples = 50
    if test:
        num_samples = 1
    if only_enqueued:
        num_samples = len(points_to_evaluate)

    stoppers: list[Stopper] = [
        NoImprovementStopper(
            metric="trk.double_majority_pt1.5",
            patience=10,
            mode="max",
            grace_period=grace_period,
        ),
    ]
    if timeout_seconds is not None:
        stoppers.append(TimeoutStopper(timeout_seconds))
    if test:
        stoppers.append(MaximumIterationStopper(1))
    stopper = CombinedStopper(*stoppers)

    callbacks: list[Callback] = []
    if not test:
        callbacks = [
            WandbLoggerCallback(
                api_key_file="~/.wandb_api_key",
                project="gnn_tracking",
                tags=tags,
                group=group,
                notes=note,
            ),
            TriggerWandbSyncRayHook(),
        ]

    if test:
        dname += "_test"

    tuner = tune.Tuner(
        tune.with_resources(
            trainable,
            {"gpu": 1 if gpu else 0, "cpu": server.cpus_per_gpu if not test else 1},
        ),
        tune_config=tune.TuneConfig(
            scheduler=ASHAScheduler(
                metric="trk.double_majority_pt1.5",
                mode="max",
                grace_period=grace_period,
            ),
            num_samples=num_samples,
            search_alg=optuna_search,
        ),
        run_config=RunConfig(
            name=dname,
            callbacks=callbacks,
            sync_config=SyncConfig(syncer=None),
            stop=stopper,
            checkpoint_config=CheckpointConfig(
                checkpoint_score_attribute="trk.double_majority_pt1.5",
                checkpoint_score_order="max",
                num_to_keep=5,
            ),
            log_to_file=True,
            # verbose=1,  # Only status reports, no results
            failure_config=FailureConfig(
                fail_fast=not fail_slow,
            ),
        ),
    )
    tuner.fit()
