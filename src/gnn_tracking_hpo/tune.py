#!/usr/bin/env python3

from __future__ import annotations

from argparse import ArgumentParser
from functools import cached_property, partial
from pathlib import Path
from typing import Any, Callable

import optuna
import pytimeparse
from ray import logger, tune
from ray.air import CheckpointConfig, FailureConfig, RunConfig
from ray.air.integrations.wandb import WandbLoggerCallback
from ray.tune import Callback, ResultGrid, Stopper, SyncConfig, Trainable
from ray.tune.schedulers import ASHAScheduler
from ray.tune.search.optuna import OptunaSearch
from ray.tune.stopper import CombinedStopper, MaximumIterationStopper, TimeoutStopper
from rt_stoppers_contrib.no_improvement import NoImprovementTrialStopper
from wandb_osh.ray_hooks import TriggerWandbSyncRayHook

from gnn_tracking_hpo.cli import (
    add_enqueue_option,
    add_gpu_option,
    add_local_option,
    add_test_option,
    add_wandb_options,
)
from gnn_tracking_hpo.config import della, get_points_to_evaluate, read_json
from gnn_tracking_hpo.orchestrate import maybe_run_distributed, maybe_run_wandb_offline

server = della


def add_common_options(parser: ArgumentParser):
    add_test_option(parser)
    add_gpu_option(parser)
    add_enqueue_option(parser)
    add_local_option(parser)
    parser.add_argument(
        "--restore",
        help="Restore previous training search state from this directory",
        default=None,
    )
    parser.add_argument(
        "--only-enqueued",
        help="Only run enqueued points, do not tune any parameters",
        action="store_true",
    )
    parser.add_argument(
        "--fixed",
        help="Read config values from file and fix these values in all trials.",
    )
    parser.add_argument(
        "--timeout",
        help="Stop all trials after certain time. Natural time specifications "
        "supported.",
    )
    parser.add_argument(
        "--fail-slow",
        help="Do not abort tuning after trial fails.",
        action="store_true",
    )
    parser.add_argument(
        "--dname",
        help="Name of ray output directory",
        default="tcn",
    )
    parser.add_argument(
        "--no-tune",
        help="Do not run tuner, simply train (useful for debugging)",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--n-trials",
        dest="num_samples",
        help="Maximum number of trials to run",
        default=None,
        type=int,
    )
    parser.add_argument(
        "--no-scheduler",
        action="store_true",
        help="Do not use scheduler, run all trials, only stopping them on plateaus.",
    )
    add_wandb_options(parser)


def get_timeout_stopper(timeout: str | None = None) -> TimeoutStopper | None:
    """Interpret timeout string as seconds."""
    if timeout is None:
        return None
    else:
        timeout_seconds = pytimeparse.parse(timeout)
        if timeout_seconds is None:
            raise ValueError(
                "Could not parse timeout. Try specifying a unit, " "e.g., 1h13m"
            )
        return TimeoutStopper(timeout_seconds)


def simple_run_without_tune(trainable, suggest_config: Callable) -> None:
    """Simple run without tuning for testing purposes."""
    study = optuna.create_study()
    trial = study.ask()
    config = suggest_config(trial, test=True)
    config = {**config, **trial.params}
    assert config["test"]
    train_instance = trainable(config)
    for _ in range(2):
        train_instance.trainer.step(max_batches=1)
    raise SystemExit(0)


class Dispatcher:
    def __init__(
        self,
        *,
        # ---- Supplied fom CLI
        test=False,
        gpu=False,
        restore=None,
        enqueue: None | list[str] = None,
        only_enqueued=False,
        fixed: None | str = None,
        timeout: None | str = None,
        tags=None,
        group=None,
        note=None,
        fail_slow=False,
        dname="tcn",
        metric="trk.double_majority_pt1.5",
        no_tune=False,
        num_samples: None | int = None,
        no_scheduler=False,
        local=False,
        # ----
        grace_period=3,
        no_improvement_patience=10,
        additional_stoppers=None,
    ):
        """For most arguments, see corresponding command line interface.

        Args:
            grace_period: Grace period for ASHA scheduler.
            no_improvement_patience: Number of iterations without improvement before
                stopping
        """
        self.test = test
        self.gpu = gpu
        self.restore = restore
        self.enqueue = enqueue
        self.only_enqueued = only_enqueued
        self.fixed = fixed
        self.grace_period = grace_period
        self.timeout = timeout
        self.tags = tags
        self.group = group
        self.note = note
        self.fail_slow = fail_slow
        self.dname = dname
        self.metric = metric
        self.no_improvement_patience = no_improvement_patience
        self.no_tune = no_tune
        self.num_samples = num_samples
        self.no_scheduler = no_scheduler
        self.local = local
        if self.test and not self.dname.endswith("_test"):
            self.dname += "_test"
        if additional_stoppers is None:
            self.additional_stoppers = []
        else:
            self.additional_stoppers = additional_stoppers

    def __call__(
        self,
        trainable: type[Trainable],
        suggest_config: Callable,
    ) -> ResultGrid:
        """
        Args:
            trainable: The trainable to run.
            suggest_config: A function that returns a config dictionary.

        Returns:

        """
        if self.no_tune:
            assert self.test
            simple_run_without_tune(trainable, suggest_config)

        maybe_run_wandb_offline()
        maybe_run_distributed(local=self.local)
        tuner = self.get_tuner(trainable, suggest_config)
        return tuner.fit()

    def get_tuner(
        self, trainable: type[Trainable], suggest_config: Callable
    ) -> tune.Tuner:
        return tune.Tuner(
            tune.with_resources(
                trainable,
                {
                    "gpu": 1 if self.gpu else 0,
                    "cpu": server.cpus_per_gpu if not self.test else 1,
                },
            ),
            tune_config=self.get_tune_config(suggest_config),
            run_config=self.get_run_config(),
        )

    def get_no_improvement_stopper(self) -> NoImprovementTrialStopper:
        return NoImprovementTrialStopper(
            metric=self.metric,
            patience=self.no_improvement_patience,
            mode="max",
            grace_period=self.grace_period,
        )

    def get_stoppers(self) -> list[Stopper]:
        # For easier subclassing, methods can be overridden to return None
        # to disable
        stoppers: list[Stopper] = [
            self.get_no_improvement_stopper(),
            *self.additional_stoppers,
        ]
        if timeout_stopper := get_timeout_stopper(self.timeout):
            stoppers.append(timeout_stopper)
        if self.test:
            stoppers.append(MaximumIterationStopper(1))
        return [stopper for stopper in stoppers if stopper is not None]

    def get_wandb_callbacks(self) -> list[Callback]:
        return [
            WandbLoggerCallback(
                api_key_file="~/.wandb_api_key",
                project="gnn_tracking",
                tags=self.tags,
                group=self.group,
                notes=self.note,
            ),
            TriggerWandbSyncRayHook(),
        ]

    def get_callbacks(self) -> list[Callback]:
        callbacks: list[Callback] = []
        if not self.test:
            callbacks.extend(self.get_wandb_callbacks())
        return callbacks

    @cached_property
    def points_to_evaluate(self) -> list[dict[str, Any]]:
        return get_points_to_evaluate(self.enqueue)

    def get_optuna_search(self, suggest_config: Callable) -> OptunaSearch:
        fixed_config: None | dict[str, Any] = None
        if self.fixed is not None:
            fixed_config = read_json(Path(self.fixed))

        optuna_search = OptunaSearch(
            partial(suggest_config, test=self.test, fixed=fixed_config),
            metric=self.metric,
            mode="max",
            points_to_evaluate=self.points_to_evaluate,
        )
        if self.restore:
            logger.info(f"Restoring previous state from {self.restore}")
            optuna_search.restore_from_dir(self.restore)
        return optuna_search

    def get_num_samples(self) -> int:
        """Return number of samples/trials to run"""
        if self.test:
            return 1
        if self.only_enqueued:
            return len(self.points_to_evaluate)
        if self.num_samples is not None:
            return self.num_samples
        logger.warning("No n-samples specified, defaulting to only 20")
        return 20

    def get_scheduler(self) -> None | ASHAScheduler:
        if self.no_scheduler:
            # FIFO scheduler
            return None
        return ASHAScheduler(
            metric=self.metric,
            mode="max",
            grace_period=self.grace_period,
        )

    def get_tune_config(
        self,
        suggest_config: Callable,
    ) -> tune.TuneConfig:
        return tune.TuneConfig(
            scheduler=self.get_scheduler(),
            num_samples=self.get_num_samples(),
            search_alg=self.get_optuna_search(suggest_config),
        )

    def get_checkpoint_config(self) -> CheckpointConfig:
        return CheckpointConfig(
            checkpoint_score_attribute=self.metric,
            checkpoint_score_order="max",
            num_to_keep=5,
            checkpoint_frequency=1,
        )

    def get_run_config(self) -> RunConfig:
        return RunConfig(
            name=self.dname,
            callbacks=self.get_callbacks(),
            sync_config=SyncConfig(syncer=None),
            stop=CombinedStopper(*self.get_stoppers()),
            checkpoint_config=self.get_checkpoint_config(),
            log_to_file=True,
            failure_config=FailureConfig(
                fail_fast=not self.fail_slow,
            ),
        )


def main(trainable, suggest_config, *args, **kwargs) -> ResultGrid:
    """Dispatch with ray tune Arguments see Dispater.__call__."""
    logger.warning("Deprecated, use Dispatcher class directly")
    dispatcher = Dispatcher(*args, **kwargs)
    return dispatcher(trainable, suggest_config)
