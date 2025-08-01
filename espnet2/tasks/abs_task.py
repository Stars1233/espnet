"""Abstract task module."""

import argparse
import functools
import itertools
import logging
import os
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import humanfriendly
import numpy as np
import torch
import torch.multiprocessing
import torch.nn
import torch.optim
import yaml
from packaging.version import parse as V
from torch.utils.data import DataLoader
from typeguard import typechecked

from espnet import __version__
from espnet2.iterators.abs_iter_factory import AbsIterFactory
from espnet2.iterators.category_chunk_iter_factory import CategoryChunkIterFactory
from espnet2.iterators.category_iter_factory import CategoryIterFactory
from espnet2.iterators.chunk_iter_factory import ChunkIterFactory
from espnet2.iterators.multiple_iter_factory import MultipleIterFactory
from espnet2.iterators.sequence_iter_factory import SequenceIterFactory
from espnet2.layers.create_adapter import create_adapter
from espnet2.main_funcs.collect_stats import collect_stats
from espnet2.optimizers.optim_groups import configure_optimizer
from espnet2.optimizers.sgd import SGD
from espnet2.samplers.build_batch_sampler import BATCH_TYPES, build_batch_sampler
from espnet2.samplers.category_balanced_sampler import CategoryBalancedSampler
from espnet2.samplers.unsorted_batch_sampler import UnsortedBatchSampler
from espnet2.schedulers.cosine_anneal_warmup_restart import (
    CosineAnnealingWarmupRestarts,
)
from espnet2.schedulers.exponential_decay_warmup import ExponentialDecayWarmup
from espnet2.schedulers.noam_lr import NoamLR
from espnet2.schedulers.piecewise_linear_warmup_lr import PiecewiseLinearWarmupLR
from espnet2.schedulers.warmup_lr import WarmupLR
from espnet2.schedulers.warmup_reducelronplateau import WarmupReduceLROnPlateau
from espnet2.schedulers.warmup_step_lr import WarmupStepLR
from espnet2.torch_utils.load_pretrained_model import load_pretrained_model
from espnet2.torch_utils.model_summary import model_summary
from espnet2.torch_utils.pytorch_version import pytorch_cudnn_version
from espnet2.torch_utils.set_all_random_seed import set_all_random_seed
from espnet2.train.abs_espnet_model import AbsESPnetModel
from espnet2.train.class_choices import ClassChoices
from espnet2.train.dataset import (
    DATA_TYPES,
    AbsDataset,
    ESPnetDataset,
    ESPnetMultiTaskDataset,
)
from espnet2.train.distributed_utils import (
    DistributedOption,
    free_port,
    get_master_port,
    get_node_rank,
    get_num_nodes,
    resolve_distributed_mode,
)
from espnet2.train.iterable_dataset import (  # noqa
    IterableESPnetDataset,
    SplicedIterableESPnetDataset,
)
from espnet2.train.trainer import Trainer
from espnet2.utils import config_argparse
from espnet2.utils.build_dataclass import build_dataclass
from espnet2.utils.get_default_kwargs import get_default_kwargs
from espnet2.utils.nested_dict_action import NestedDictAction
from espnet2.utils.types import (
    humanfriendly_parse_size_or_none,
    int_or_none,
    str2bool,
    str2triple_str,
    str_or_int,
    str_or_none,
)
from espnet2.utils.yaml_no_alias_safe_dump import yaml_no_alias_safe_dump
from espnet.utils.cli_utils import get_commandline_args

try:
    import wandb
except Exception:
    wandb = None

if V(torch.__version__) >= V("1.5.0"):
    from torch.multiprocessing.spawn import ProcessContext
else:
    from torch.multiprocessing.spawn import SpawnContext as ProcessContext


optim_classes = dict(
    adam=torch.optim.Adam,
    adamw=torch.optim.AdamW,
    sgd=SGD,
    adadelta=torch.optim.Adadelta,
    adagrad=torch.optim.Adagrad,
    adamax=torch.optim.Adamax,
    asgd=torch.optim.ASGD,
    lbfgs=torch.optim.LBFGS,
    rmsprop=torch.optim.RMSprop,
    rprop=torch.optim.Rprop,
)
if V(torch.__version__) >= V("1.10.0"):
    # From 1.10.0, RAdam is officially supported
    optim_classes.update(
        radam=torch.optim.RAdam,
    )
try:
    import torch_optimizer

    optim_classes.update(
        accagd=torch_optimizer.AccSGD,
        adabound=torch_optimizer.AdaBound,
        adamod=torch_optimizer.AdaMod,
        diffgrad=torch_optimizer.DiffGrad,
        lamb=torch_optimizer.Lamb,
        novograd=torch_optimizer.NovoGrad,
        pid=torch_optimizer.PID,
        # torch_optimizer<=0.0.1a10 doesn't support
        # qhadam=torch_optimizer.QHAdam,
        qhm=torch_optimizer.QHM,
        sgdw=torch_optimizer.SGDW,
        yogi=torch_optimizer.Yogi,
    )
    if V(torch_optimizer.__version__) < V("0.2.0"):
        # From 0.2.0, RAdam is dropped
        optim_classes.update(
            radam=torch_optimizer.RAdam,
        )
    del torch_optimizer
except ImportError:
    pass
try:
    import apex

    optim_classes.update(
        fusedadam=apex.optimizers.FusedAdam,
        fusedlamb=apex.optimizers.FusedLAMB,
        fusednovograd=apex.optimizers.FusedNovoGrad,
        fusedsgd=apex.optimizers.FusedSGD,
    )
    del apex
except ImportError:
    pass
try:
    import fairscale
except ImportError:
    fairscale = None


scheduler_classes = dict(
    ReduceLROnPlateau=torch.optim.lr_scheduler.ReduceLROnPlateau,
    lambdalr=torch.optim.lr_scheduler.LambdaLR,
    steplr=torch.optim.lr_scheduler.StepLR,
    multisteplr=torch.optim.lr_scheduler.MultiStepLR,
    exponentiallr=torch.optim.lr_scheduler.ExponentialLR,
    CosineAnnealingLR=torch.optim.lr_scheduler.CosineAnnealingLR,
    noamlr=NoamLR,
    warmuplr=WarmupLR,
    piecewiselinearwarmuplr=PiecewiseLinearWarmupLR,
    warmupsteplr=WarmupStepLR,
    warmupReducelronplateau=WarmupReduceLROnPlateau,
    cycliclr=torch.optim.lr_scheduler.CyclicLR,
    onecyclelr=torch.optim.lr_scheduler.OneCycleLR,
    CosineAnnealingWarmRestarts=torch.optim.lr_scheduler.CosineAnnealingWarmRestarts,
    CosineAnnealingWarmupRestarts=CosineAnnealingWarmupRestarts,
    ExponentialDecayWarmup=ExponentialDecayWarmup,
)
# To lower keys
optim_classes = {k.lower(): v for k, v in optim_classes.items()}
scheduler_classes = {k.lower(): v for k, v in scheduler_classes.items()}


CONFIG_REPLACE_MAP = [
    ("text_cleaner", "cleaner"),
    ("g2p_type", "g2p"),
    ("aux_task_names", "aux_ctc_tasks"),
]


@dataclass
class IteratorOptions:
    preprocess_fn: callable
    collate_fn: callable
    data_path_and_name_and_type: list
    shape_files: list
    batch_size: int
    batch_bins: int
    batch_type: str
    max_cache_size: float
    max_cache_fd: int
    allow_multi_rates: bool
    distributed: bool
    num_batches: Optional[int]
    num_iters_per_epoch: Optional[int]
    train: bool


class AbsTask(ABC):
    # Use @staticmethod, or @classmethod,
    # instead of instance method to avoid God classes

    # If you need more than one optimizers, change this value in inheritance
    num_optimizers: int = 1
    trainer = Trainer
    class_choices_list: List[ClassChoices] = []

    def __init__(self):
        raise RuntimeError("This class can't be instantiated.")

    @classmethod
    @abstractmethod
    def add_task_arguments(cls, parser: argparse.ArgumentParser):
        pass

    @classmethod
    @abstractmethod
    def build_collate_fn(
        cls, args: argparse.Namespace, train: bool
    ) -> Callable[[Sequence[Dict[str, np.ndarray]]], Dict[str, torch.Tensor]]:
        """Return "collate_fn", which is a callable object and given to DataLoader.

        >>> from torch.utils.data import DataLoader
        >>> loader = DataLoader(collate_fn=cls.build_collate_fn(args, train=True), ...)

        In many cases, you can use our common collate_fn.
        """
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def build_preprocess_fn(
        cls, args: argparse.Namespace, train: bool
    ) -> Optional[Callable[[str, Dict[str, np.array]], Dict[str, np.ndarray]]]:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def required_data_names(
        cls, train: bool = True, inference: bool = False
    ) -> Tuple[str, ...]:
        """Define the required names by Task

        This function is used by
        >>> cls.check_task_requirements()
        If your model is defined as following,

        >>> from espnet2.train.abs_espnet_model import AbsESPnetModel
        >>> class Model(AbsESPnetModel):
        ...     def forward(self, input, output, opt=None):  pass

        then "required_data_names" should be as

        >>> required_data_names = ('input', 'output')
        """
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def optional_data_names(
        cls, train: bool = True, inference: bool = False
    ) -> Tuple[str, ...]:
        """Define the optional names by Task

        This function is used by
        >>> cls.check_task_requirements()
        If your model is defined as follows,

        >>> from espnet2.train.abs_espnet_model import AbsESPnetModel
        >>> class Model(AbsESPnetModel):
        ...     def forward(self, input, output, opt=None):  pass

        then "optional_data_names" should be as

        >>> optional_data_names = ('opt',)
        """
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def build_model(cls, args: argparse.Namespace) -> AbsESPnetModel:
        raise NotImplementedError

    @classmethod
    @typechecked
    def get_parser(cls) -> config_argparse.ArgumentParser:
        class ArgumentDefaultsRawTextHelpFormatter(
            argparse.RawTextHelpFormatter,
            argparse.ArgumentDefaultsHelpFormatter,
        ):
            pass

        parser = config_argparse.ArgumentParser(
            description="base parser",
            formatter_class=ArgumentDefaultsRawTextHelpFormatter,
        )

        # NOTE(kamo): Use '_' instead of '-' to avoid confusion.
        #  I think '-' looks really confusing if it's written in yaml.

        # NOTE(kamo): add_arguments(..., required=True) can't be used
        #  to provide --print_config mode. Instead of it, do as
        parser.set_defaults(required=["output_dir"])

        group = parser.add_argument_group("Common configuration")

        group.add_argument(
            "--print_config",
            action="store_true",
            help="Print the config file and exit",
        )
        group.add_argument(
            "--log_level",
            type=lambda x: x.upper(),
            default="INFO",
            choices=("ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"),
            help="The verbose level of logging",
        )
        group.add_argument(
            "--drop_last_iter",
            type=str2bool,
            default=False,
            help="Exclude the minibatch with leftovers.",
        )
        group.add_argument(
            "--dry_run",
            type=str2bool,
            default=False,
            help="Perform process without training",
        )
        group.add_argument(
            "--iterator_type",
            type=str,
            choices=["sequence", "category", "chunk", "task", "none"],
            default="sequence",
            help="Specify iterator type",
        )
        group.add_argument(
            "--valid_iterator_type",
            type=str,
            choices=["sequence", "category", "chunk", "task", "none"],
            default=None,
            help="Specify iterator type",
        )

        group.add_argument("--output_dir", type=str_or_none, default=None)
        group.add_argument(
            "--ngpu",
            type=int,
            default=0,
            help="The number of gpus. 0 indicates CPU mode",
        )
        group.add_argument("--seed", type=int, default=0, help="Random seed")
        group.add_argument(
            "--num_workers",
            type=int,
            default=1,
            help="The number of workers used for DataLoader",
        )
        group.add_argument(
            "--num_att_plot",
            type=int,
            default=3,
            help="The number images to plot the outputs from attention. "
            "This option makes sense only when attention-based model. "
            "We can also disable the attention plot by setting it 0",
        )

        group = parser.add_argument_group("distributed training related")
        group.add_argument(
            "--dist_backend",
            default="nccl",
            type=str,
            help="distributed backend",
        )
        group.add_argument(
            "--dist_init_method",
            type=str,
            default="env://",
            help='if init_method="env://", env values of "MASTER_PORT", "MASTER_ADDR", '
            '"WORLD_SIZE", and "RANK" are referred.',
        )
        group.add_argument(
            "--dist_world_size",
            default=None,
            type=int_or_none,
            help="number of nodes for distributed training",
        )
        group.add_argument(
            "--dist_rank",
            type=int_or_none,
            default=None,
            help="node rank for distributed training",
        )
        group.add_argument(
            # Not starting with "dist_" for compatibility to launch.py
            "--local_rank",
            type=int_or_none,
            default=None,
            help="local rank for distributed training. This option is used if "
            "--multiprocessing_distributed=false",
        )
        group.add_argument(
            "--dist_master_addr",
            default=None,
            type=str_or_none,
            help="The master address for distributed training. "
            "This value is used when dist_init_method == 'env://'",
        )
        group.add_argument(
            "--dist_master_port",
            default=None,
            type=int_or_none,
            help="The master port for distributed training"
            "This value is used when dist_init_method == 'env://'",
        )
        group.add_argument(
            "--dist_launcher",
            default=None,
            type=str_or_none,
            choices=["slurm", "mpi", None],
            help="The launcher type for distributed training",
        )
        group.add_argument(
            "--multiprocessing_distributed",
            default=False,
            type=str2bool,
            help="Use multi-processing distributed training to launch "
            "N processes per node, which has N GPUs. This is the "
            "fastest way to use PyTorch for either single node or "
            "multi node data parallel training",
        )
        group.add_argument(
            "--unused_parameters",
            type=str2bool,
            default=False,
            help="Whether to use the find_unused_parameters in "
            "torch.nn.parallel.DistributedDataParallel ",
        )
        group.add_argument(
            "--sharded_ddp",
            default=False,
            type=str2bool,
            help="Enable sharded training provided by fairscale",
        )
        group.add_argument(
            "--use_deepspeed",
            default=False,
            type=str2bool,
            help="Enable deepspeed for training",
        )
        group.add_argument(
            "--deepspeed_config",
            default=None,
            type=str,
            help="deepspeed training config",
        )
        group.add_argument(
            "--gradient_as_bucket_view",
            default=True,
            type=str2bool,
            help="Enable gradient_as_bucket_view in DDP",
        )
        group.add_argument(
            "--ddp_comm_hook",
            default=None,
            type=str_or_none,
            choices=["none", "fp16_compress_hook", "bf16_compress_hook"],
            help="DDP communication hook from "
            "torch.distributed.algorithms.ddp_comm_hooks.default_hooks",
        )

        group = parser.add_argument_group("cudnn mode related")
        group.add_argument(
            "--cudnn_enabled",
            type=str2bool,
            default=torch.backends.cudnn.enabled,
            help="Enable CUDNN",
        )
        group.add_argument(
            "--cudnn_benchmark",
            type=str2bool,
            default=torch.backends.cudnn.benchmark,
            help="Enable cudnn-benchmark mode",
        )
        group.add_argument(
            "--cudnn_deterministic",
            type=str2bool,
            default=True,
            help="Enable cudnn-deterministic mode",
        )
        group.add_argument(
            "--use_tf32",
            type=str2bool,
            default=False,
            help="Enable TensorFloat32 on CUDA and CUDNN",
        )

        group = parser.add_argument_group("collect stats mode related")
        group.add_argument(
            "--collect_stats",
            type=str2bool,
            default=False,
            help='Perform on "collect stats" mode',
        )
        group.add_argument(
            "--write_collected_feats",
            type=str2bool,
            default=False,
            help='Write the output features from the model when "collect stats" mode',
        )

        group = parser.add_argument_group("Trainer related")
        group.add_argument(
            "--max_epoch",
            type=int,
            default=40,
            help="The maximum number epoch to train",
        )
        group.add_argument(
            "--patience",
            type=int_or_none,
            default=None,
            help="Number of epochs to wait without improvement "
            "before stopping the training",
        )
        group.add_argument(
            "--val_scheduler_criterion",
            type=str,
            nargs=2,
            default=("valid", "loss"),
            help="The criterion used for the value given to the lr scheduler. "
            'Give a pair referring the phase, "train" or "valid",'
            'and the criterion name. The mode specifying "min" or "max" can '
            "be changed by --scheduler_conf",
        )
        group.add_argument(
            "--early_stopping_criterion",
            type=str,
            nargs=3,
            default=("valid", "loss", "min"),
            help="The criterion used for judging of early stopping. "
            'Give a pair referring the phase, "train" or "valid",'
            'the criterion name and the mode, "min" or "max", e.g. "acc,max".',
        )
        group.add_argument(
            "--best_model_criterion",
            type=str2triple_str,
            nargs="+",
            default=[
                ("train", "loss", "min"),
                ("valid", "loss", "min"),
                ("train", "acc", "max"),
                ("valid", "acc", "max"),
            ],
            help="The criterion used for judging of the best model. "
            'Give a pair referring the phase, "train" or "valid",'
            'the criterion name, and the mode, "min" or "max", e.g. "acc,max".',
        )
        group.add_argument(
            "--keep_nbest_models",
            type=int,
            nargs="+",
            default=[10],
            help="Remove previous snapshots excluding the n-best scored epochs",
        )
        group.add_argument(
            "--nbest_averaging_interval",
            type=int,
            default=0,
            help="The epoch interval to apply model averaging and save nbest models",
        )
        group.add_argument(
            "--grad_clip",
            type=float,
            default=5.0,
            help="Gradient norm threshold to clip",
        )
        group.add_argument(
            "--grad_clip_type",
            type=float,
            default=2.0,
            help="The type of the used p-norm for gradient clip. Can be inf",
        )
        group.add_argument(
            "--grad_noise",
            type=str2bool,
            default=False,
            help="The flag to switch to use noise injection to "
            "gradients during training",
        )
        group.add_argument(
            "--accum_grad",
            type=int,
            default=1,
            help="The number of gradient accumulation",
        )
        group.add_argument(
            "--no_forward_run",
            type=str2bool,
            default=False,
            help="Just only iterating data loading without "
            "model forwarding and training",
        )
        group.add_argument(
            "--resume",
            type=str2bool,
            default=False,
            help="Enable resuming if checkpoint is existing",
        )
        group.add_argument(
            "--train_dtype",
            default="float32",
            choices=["float16", "float32", "float64"],
            help="Data type for training.",
        )
        group.add_argument(
            "--use_amp",
            type=str2bool,
            default=False,
            help="Enable Automatic Mixed Precision. This feature requires pytorch>=1.6",
        )
        group.add_argument(
            "--log_interval",
            type=int_or_none,
            default=None,
            help="Show the logs every the number iterations in each epochs at the "
            "training phase. If None is given, it is decided according the number "
            "of training samples automatically .",
        )
        group.add_argument(
            "--use_matplotlib",
            type=str2bool,
            default=True,
            help="Enable matplotlib logging",
        )
        group.add_argument(
            "--use_tensorboard",
            type=str2bool,
            default=True,
            help="Enable tensorboard logging",
        )
        group.add_argument(
            "--create_graph_in_tensorboard",
            type=str2bool,
            default=False,
            help="Whether to create graph in tensorboard",
        )
        group.add_argument(
            "--use_wandb",
            type=str2bool,
            default=False,
            help="Enable wandb logging",
        )
        group.add_argument(
            "--wandb_project",
            type=str,
            default=None,
            help="Specify wandb project",
        )
        group.add_argument(
            "--wandb_id",
            type=str,
            default=None,
            help="Specify wandb id",
        )
        group.add_argument(
            "--wandb_entity",
            type=str,
            default=None,
            help="Specify wandb entity",
        )
        group.add_argument(
            "--wandb_name",
            type=str,
            default=None,
            help="Specify wandb run name",
        )
        group.add_argument(
            "--wandb_model_log_interval",
            type=int,
            default=-1,
            help="Set the model log period",
        )
        group.add_argument(
            "--detect_anomaly",
            type=str2bool,
            default=False,
            help="Set torch.autograd.set_detect_anomaly",
        )
        group.add_argument(
            "--use_adapter",
            type=str2bool,
            default=False,
            help="Enable efficient finetuning, see (https://arxiv.org/abs/2106.09685) "
            "for large pre-trained foundation models, like Whisper and SSL models",
        )
        group.add_argument(
            "--adapter",
            type=str,
            default="lora",
            help="Adapter Name",
            choices=["lora", "houlsby"],
        )
        group.add_argument(
            "--save_strategy",
            type=str,
            default="all",
            help="The strategy to save parameters. Default: 'all' \n"
            "'all': save all parameters\n"
            "'adapter_only': save only adapter parameters,"
            " without other parameters like downstream model\n"
            "'required_grad_only': save only parameters with requires_grad=True\n",
            choices=["all", "adapter_only", "required_grad_only"],
        )
        group.add_argument(
            "--adapter_conf",
            action=NestedDictAction,
            default=dict(),
            help="Configuration for efficient finetuning",
        )

        group = parser.add_argument_group("Pretraining model related")
        group.add_argument("--pretrain_path", help="This option is obsoleted")
        group.add_argument(
            "--init_param",
            type=str,
            default=[],
            nargs="*",
            help="Specify the file path used for initialization of parameters. "
            "The format is '<file_path>:<src_key>:<dst_key>:<exclude_keys>', "
            "where file_path is the model file path, "
            "src_key specifies the key of model states to be used in the model file, "
            "dst_key specifies the attribute of the model to be initialized, "
            "and exclude_keys excludes keys of model states for the initialization."
            "e.g.\n"
            "  # Load all parameters"
            "  --init_param some/where/model.pth\n"
            "  # Load only decoder parameters"
            "  --init_param some/where/model.pth:decoder:decoder\n"
            "  # Load only decoder parameters excluding decoder.embed"
            "  --init_param some/where/model.pth:decoder:decoder:decoder.embed\n"
            "  --init_param some/where/model.pth:decoder:decoder:decoder.embed\n",
        )
        group.add_argument(
            "--ignore_init_mismatch",
            type=str2bool,
            default=False,
            help="Ignore size mismatch when loading pre-trained model",
        )
        group.add_argument(
            "--freeze_param",
            type=str,
            default=[],
            nargs="*",
            help="Freeze parameters",
        )

        group = parser.add_argument_group("BatchSampler related")
        group.add_argument(
            "--num_iters_per_epoch",
            type=int_or_none,
            default=None,
            help="Restrict the number of iterations for training per epoch",
        )
        group.add_argument(
            "--batch_size",
            type=int,
            default=20,
            help="The mini-batch size used for training. Used if batch_type='unsorted',"
            " 'sorted', or 'folded', or 'catbel'.",
        )
        group.add_argument(
            "--valid_batch_size",
            type=int_or_none,
            default=None,
            help="If not given, the value of --batch_size is used",
        )
        group.add_argument(
            "--batch_bins",
            type=int,
            default=1000000,
            help="The number of batch bins. Used if batch_type='length' or 'numel'",
        )
        group.add_argument(
            "--valid_batch_bins",
            type=int_or_none,
            default=None,
            help="If not given, the value of --batch_bins is used",
        )
        group.add_argument(
            "--category_sample_size",
            type=int,
            default=10,
            help="The sample size for category chunk iterator",
        )

        group.add_argument("--train_shape_file", type=str, action="append", default=[])
        group.add_argument("--valid_shape_file", type=str, action="append", default=[])

        group = parser.add_argument_group("Sequence iterator related")
        _batch_type_help = ""
        for key, value in BATCH_TYPES.items():
            _batch_type_help += f'"{key}":\n{value}\n'
        group.add_argument(
            "--batch_type",
            type=str,
            default="folded",
            choices=list(BATCH_TYPES),
            help=_batch_type_help,
        )
        group.add_argument(
            "--valid_batch_type",
            type=str_or_none,
            default=None,
            choices=list(BATCH_TYPES) + [None],
            help="If not given, the value of --batch_type is used",
        )
        group.add_argument("--fold_length", type=int, action="append", default=[])
        group.add_argument(
            "--sort_in_batch",
            type=str,
            default="descending",
            choices=["descending", "ascending"],
            help="Sort the samples in each mini-batches by the sample "
            'lengths. To enable this, "shape_file" must have the length information.',
        )
        group.add_argument(
            "--shuffle_within_batch",
            type=str2bool,
            default=False,
            help="Shuffles wholes batches in sample-wise. Required for"
            "Classification tasks normally.",
        )
        group.add_argument(
            "--sort_batch",
            type=str,
            default="descending",
            choices=["descending", "ascending"],
            help="Sort mini-batches by the sample lengths",
        )
        group.add_argument(
            "--multiple_iterator",
            type=str2bool,
            default=False,
            help="Use multiple iterator mode",
        )

        group = parser.add_argument_group("Chunk iterator related")
        group.add_argument(
            "--chunk_length",
            type=str_or_int,
            default=500,
            help="Specify chunk length. e.g. '300', '300,400,500', or '300-400'."
            "If multiple numbers separated by command are given, "
            "one of them is selected randomly for each samples. "
            "If two numbers are given with '-', it indicates the range of the choices. "
            "Note that if the sequence length is shorter than the all chunk_lengths, "
            "the sample is discarded. ",
        )
        group.add_argument(
            "--chunk_shift_ratio",
            type=float,
            default=0.5,
            help="Specify the shift width of chunks. If it's less than 1, "
            "allows the overlapping and if bigger than 1, there are some gaps "
            "between each chunk.",
        )
        group.add_argument(
            "--num_cache_chunks",
            type=int,
            default=1024,
            help="Shuffle in the specified number of chunks and generate mini-batches "
            "More larger this value, more randomness can be obtained.",
        )
        group.add_argument(
            "--chunk_excluded_key_prefixes",
            type=str,
            nargs="+",
            default=[],
            help="List of key prefixes. Keys that satisfy either condition below "
            "will be excluded from the length consistency check in ChunkIterFactory:\n"
            "  - exactly match one of the prefixes in `chunk_excluded_key_prefixes`\n"
            "  - have one of the prefixes in `chunk_excluded_key_prefixes` and "
            "end with numbers",
        )
        group.add_argument(
            "--chunk_default_fs",
            type=int_or_none,
            default=None,
            help="Default sampling rate used for the chunk length. Will be used to "
            "adaptively adjust the chunk length for data of different sampling rates. "
            "(If None, the chunk length will be fixed.)",
        )
        group.add_argument(
            "--chunk_max_abs_length",
            type=int_or_none,
            default=None,
            help="Maximum number of samples per chunk for all sampling rates",
        )
        group.add_argument(
            "--chunk_discard_short_samples",
            type=str2bool,
            default=True,
            help="Discard samples shorter than the minimum chunk length",
        )

        group = parser.add_argument_group("Dataset related")
        _data_path_and_name_and_type_help = (
            "Give three words splitted by comma. It's used for the training data. "
            "e.g. '--train_data_path_and_name_and_type some/path/a.scp,foo,sound'. "
            "The first value, some/path/a.scp, indicates the file path, "
            "and the second, foo, is the key name used for the mini-batch data, "
            "and the last, sound, decides the file type. "
            "This option is repeatable, so you can input any number of features "
            "for your task. Supported file types are as follows:\n\n"
        )
        for key, dic in DATA_TYPES.items():
            _data_path_and_name_and_type_help += f'"{key}":\n{dic["help"]}\n\n'

        group.add_argument(
            "--train_data_path_and_name_and_type",
            type=str2triple_str,
            action="append",
            default=[],
            help=_data_path_and_name_and_type_help,
        )
        group.add_argument(
            "--valid_data_path_and_name_and_type",
            type=str2triple_str,
            action="append",
            default=[],
        )
        group.add_argument(
            "--multi_task_dataset",
            type=str2bool,
            default=False,
            help="If true, input data is organized by json file. "
            "This is usually used for multi-task training, like SpeechLM task"
            "e.g., --train_data_path_and_name_and_type foo.json,foo_task,json",
        )
        group.add_argument(
            "--allow_variable_data_keys",
            type=str2bool,
            default=False,
            help="Allow the arbitrary keys for mini-batch with ignoring "
            "the task requirements",
        )
        group.add_argument(
            "--max_cache_size",
            type=humanfriendly.parse_size,
            default=0.0,
            help="The maximum cache size for data loader. e.g. 10MB, 20GB.",
        )
        group.add_argument(
            "--max_cache_fd",
            type=int,
            default=32,
            help="The maximum number of file descriptors to be kept "
            "as opened for ark files. "
            "This feature is only valid when data type is 'kaldi_ark'.",
        )
        group.add_argument(
            "--allow_multi_rates",
            type=str2bool,
            default=False,
            help="Whether to allow audios to have different sampling rates",
        )
        group.add_argument(
            "--valid_max_cache_size",
            type=humanfriendly_parse_size_or_none,
            default=None,
            help="The maximum cache size for validation data loader. e.g. 10MB, 20GB. "
            "If None, the 5 percent size of --max_cache_size",
        )

        group = parser.add_argument_group("Optimizer related")
        group.add_argument(
            "--exclude_weight_decay",
            type=str2bool,
            default=False,
            help="Exclude weight decay in optimizer for model bias, normalization, "
            "or other special parameters",
        )
        group.add_argument(
            "--exclude_weight_decay_conf",
            action=NestedDictAction,
            default=dict(),
            help="The keyword arguments for configuring weight decay in optimizer. "
            "e.g., 'bias_weight_decay': False will set zero weight decay for bias "
            "params. See also espnet2.optimizers.optim_groups.configure_optimizer.",
        )
        for i in range(1, cls.num_optimizers + 1):
            suf = "" if i == 1 else str(i)
            group.add_argument(
                f"--optim{suf}",
                type=lambda x: x.lower(),
                default="adadelta",
                choices=list(optim_classes),
                help="The optimizer type",
            )
            group.add_argument(
                f"--optim{suf}_conf",
                action=NestedDictAction,
                default=dict(),
                help="The keyword arguments for optimizer",
            )
            group.add_argument(
                f"--scheduler{suf}",
                type=lambda x: str_or_none(x.lower()),
                default=None,
                choices=list(scheduler_classes) + [None],
                help="The lr scheduler type",
            )
            group.add_argument(
                f"--scheduler{suf}_conf",
                action=NestedDictAction,
                default=dict(),
                help="The keyword arguments for lr scheduler",
            )

        cls.trainer.add_arguments(parser)
        cls.add_task_arguments(parser)

        return parser

    @classmethod
    def build_optimizers(
        cls,
        args: argparse.Namespace,
        model: torch.nn.Module,
    ) -> List[torch.optim.Optimizer]:
        if cls.num_optimizers != 1:
            raise RuntimeError(
                "build_optimizers() must be overridden if num_optimizers != 1"
            )

        optim_class = optim_classes.get(args.optim)
        if optim_class is None:
            raise ValueError(f"must be one of {list(optim_classes)}: {args.optim}")
        if args.sharded_ddp:
            if fairscale is None:
                raise RuntimeError("Requiring fairscale. Do 'pip install fairscale'")
            optim = fairscale.optim.oss.OSS(
                params=model.parameters(), optim=optim_class, **args.optim_conf
            )
        else:
            if args.exclude_weight_decay:
                optim = configure_optimizer(
                    model,
                    optim_class,
                    args.optim_conf,
                    args.exclude_weight_decay_conf,
                )
            else:
                optim = optim_class(model.parameters(), **args.optim_conf)

        optimizers = [optim]
        return optimizers

    @classmethod
    def exclude_opts(cls) -> Tuple[str, ...]:
        """The options not to be shown by --print_config"""
        return "required", "print_config", "config", "ngpu"

    @classmethod
    @typechecked
    def get_default_config(cls) -> Dict[str, Any]:
        """Return the configuration as dict.

        This method is used by print_config()
        """

        def get_class_type(name: str, classes: dict):
            _cls = classes.get(name)
            if _cls is None:
                raise ValueError(f"must be one of {list(classes)}: {name}")
            return _cls

        # This method is used only for --print_config
        parser = cls.get_parser()
        args, _ = parser.parse_known_args()
        config = vars(args)
        # Excludes the options not to be shown
        for k in AbsTask.exclude_opts():
            config.pop(k)

        for i in range(1, cls.num_optimizers + 1):
            suf = "" if i == 1 else str(i)
            name = config[f"optim{suf}"]
            optim_class = get_class_type(name, optim_classes)
            conf = get_default_kwargs(optim_class)
            # Overwrite the default by the arguments,
            conf.update(config[f"optim{suf}_conf"])
            # and set it again
            config[f"optim{suf}_conf"] = conf

            name = config[f"scheduler{suf}"]
            if name is not None:
                scheduler_class = get_class_type(name, scheduler_classes)
                conf = get_default_kwargs(scheduler_class)
                # Overwrite the default by the arguments,
                conf.update(config[f"scheduler{suf}_conf"])
                # and set it again
                config[f"scheduler{suf}_conf"] = conf

        for class_choices in cls.class_choices_list:
            if getattr(args, class_choices.name) is not None:
                class_obj = class_choices.get_class(getattr(args, class_choices.name))
                conf = get_default_kwargs(class_obj)

                # remove duplicate arguments
                for k in CONFIG_REPLACE_MAP:
                    if k[0] in conf and k[1] in config:
                        conf.pop(k[0])
                    elif k[0] in conf:
                        conf[k[1]] = conf.pop(k[0])

                remove_keys = []
                for k in conf:
                    if k in config:
                        remove_keys.append(k)
                for k in remove_keys:
                    conf.pop(k)

                name = class_choices.name
                # Overwrite the default by the arguments,
                conf.update(config[f"{name}_conf"])
                # and set it again
                config[f"{name}_conf"] = conf
        return config

    @classmethod
    @typechecked
    def check_required_command_args(cls, args: argparse.Namespace):
        for k in vars(args):
            if "-" in k:
                raise RuntimeError(f'Use "_" instead of "-": parser.get_parser("{k}")')

        required = ", ".join(
            f"--{a}" for a in args.required if getattr(args, a) is None
        )

        if len(required) != 0:
            parser = cls.get_parser()
            parser.print_help(file=sys.stderr)
            p = Path(sys.argv[0]).name
            print(file=sys.stderr)
            print(
                f"{p}: error: the following arguments are required: " f"{required}",
                file=sys.stderr,
            )
            sys.exit(2)

    @classmethod
    @typechecked
    def check_task_requirements(
        cls,
        dataset: Union[AbsDataset, IterableESPnetDataset],
        allow_variable_data_keys: bool,
        train: bool,
        inference: bool = False,
    ) -> None:
        """Check if the dataset satisfy the requirement of current Task"""
        mes = (
            f"If you intend to use an additional input, modify "
            f'"{cls.__name__}.required_data_names()" or '
            f'"{cls.__name__}.optional_data_names()". '
            f"Otherwise you need to set --allow_variable_data_keys true "
        )

        for k in cls.required_data_names(train, inference):
            if not dataset.has_name(k):
                raise RuntimeError(
                    f'"{cls.required_data_names(train, inference)}" are required for'
                    f' {cls.__name__}. but "{dataset.names()}" are input.\n{mes}'
                )
        if not allow_variable_data_keys:
            task_keys = cls.required_data_names(
                train, inference
            ) + cls.optional_data_names(train, inference)
            for k in dataset.names():
                if k not in task_keys:
                    raise RuntimeError(
                        f"The data-name must be one of {task_keys} "
                        f'for {cls.__name__}: "{k}" is not allowed.\n{mes}'
                    )

    @classmethod
    @typechecked
    def print_config(cls, file=sys.stdout) -> None:
        # Shows the config: e.g. python train.py asr --print_config
        config = cls.get_default_config()
        file.write(yaml_no_alias_safe_dump(config, indent=4, sort_keys=False))

    @classmethod
    @typechecked
    def main(
        cls,
        args: Optional[argparse.Namespace] = None,
        cmd: Optional[Sequence[str]] = None,
    ):
        print(get_commandline_args(), file=sys.stderr)
        if args is None:
            parser = cls.get_parser()
            args = parser.parse_args(cmd)
        args.version = __version__
        if args.pretrain_path is not None:
            raise RuntimeError("--pretrain_path is deprecated. Use --init_param")
        if args.print_config:
            cls.print_config()
            sys.exit(0)
        cls.check_required_command_args(args)

        # "distributed" is decided using the other command args
        resolve_distributed_mode(args)
        if not args.distributed or not args.multiprocessing_distributed:
            cls.main_worker(args)

        else:
            assert args.ngpu > 1, args.ngpu
            # Multi-processing distributed mode: e.g. 2node-4process-4GPU
            # |   Host1     |    Host2    |
            # |   Process1  |   Process2  |  <= Spawn processes
            # |Child1|Child2|Child1|Child2|
            # |GPU1  |GPU2  |GPU1  |GPU2  |

            # See also the following usage of --multiprocessing-distributed:
            # https://github.com/pytorch/examples/blob/master/imagenet/main.py
            num_nodes = get_num_nodes(args.dist_world_size, args.dist_launcher)
            if num_nodes == 1:
                args.dist_master_addr = "localhost"
                args.dist_rank = 0
                # Single node distributed training with multi-GPUs
                if (
                    args.dist_init_method == "env://"
                    and get_master_port(args.dist_master_port) is None
                ):
                    # Get the unused port
                    args.dist_master_port = free_port()

            # Assume that nodes use same number of GPUs each other
            args.dist_world_size = args.ngpu * num_nodes
            node_rank = get_node_rank(args.dist_rank, args.dist_launcher)

            # The following block is copied from:
            # https://github.com/pytorch/pytorch/blob/master/torch/multiprocessing/spawn.py
            error_files = []
            processes = []
            mp = torch.multiprocessing.get_context("spawn")
            for i in range(args.ngpu):

                # Each process is assigned a file to write tracebacks to.  We
                # use the file being non-empty to indicate an exception
                # occurred (vs an expected shutdown).  Note: this previously
                # used a multiprocessing.Queue but that can be prone to
                # deadlocks, so we went with a simpler solution for a one-shot
                # message between processes.
                tf = tempfile.NamedTemporaryFile(
                    prefix="pytorch-errorfile-", suffix=".pickle", delete=False
                )
                tf.close()
                os.unlink(tf.name)

                # Copy args
                local_args = argparse.Namespace(**vars(args))

                local_args.local_rank = i
                local_args.dist_rank = args.ngpu * node_rank + i
                local_args.ngpu = 1

                process = mp.Process(
                    target=cls.main_worker,
                    args=(local_args,),
                    daemon=False,
                )
                process.start()
                processes.append(process)
                error_files.append(tf.name)
            # Loop on join until it returns True or raises an exception.
            while not ProcessContext(processes, error_files).join():
                pass

    @classmethod
    @typechecked
    def main_worker(cls, args: argparse.Namespace):

        # 0. Init distributed process
        distributed_option = build_dataclass(DistributedOption, args)
        # Setting distributed_option.dist_rank, etc.
        distributed_option.init_options()

        # NOTE(kamo): Don't use logging before invoking logging.basicConfig()
        if not distributed_option.distributed or distributed_option.dist_rank == 0:
            if not distributed_option.distributed:
                _rank = ""
            else:
                _rank = (
                    f":{distributed_option.dist_rank}/"
                    f"{distributed_option.dist_world_size}"
                )

            # NOTE(kamo):
            # logging.basicConfig() is invoked in main_worker() instead of main()
            # because it can be invoked only once in a process.
            # FIXME(kamo): Should we use logging.getLogger()?
            logging.basicConfig(
                level=args.log_level,
                format=f"[{os.uname()[1].split('.')[0]}{_rank}]"
                f" %(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s",
            )
        else:
            # Suppress logging if RANK != 0
            logging.basicConfig(
                level="ERROR",
                format=f"[{os.uname()[1].split('.')[0]}"
                f":{distributed_option.dist_rank}/{distributed_option.dist_world_size}]"
                f" %(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s",
            )
        # Invoking torch.distributed.init_process_group
        distributed_option.init_torch_distributed()

        # 1. Set random-seed
        set_all_random_seed(args.seed)
        torch.backends.cudnn.enabled = args.cudnn_enabled
        torch.backends.cudnn.benchmark = args.cudnn_benchmark
        torch.backends.cudnn.deterministic = args.cudnn_deterministic
        if args.detect_anomaly:
            logging.info("Invoking torch.autograd.set_detect_anomaly(True)")
            torch.autograd.set_detect_anomaly(args.detect_anomaly)

        if args.use_tf32:
            # Accelerate matmul at the cost of precision.
            # Only effective with Ampere GPUs and above
            # https://pytorch.org/docs/stable/notes/cuda.html
            assert not args.use_amp, "amp is not compatible with tf32"
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
            logging.info("Using TensorFloat32 at the cost of matmul precision")

        if (
            args.collect_stats
            and getattr(args, "model_conf", None) is not None
            and not args.model_conf.get("extract_feats_in_collect_stats", True)
        ):
            model = None
            logging.info("Skipping model building in collect_stats stage.")
        else:
            # 2. Build model
            model = cls.build_model(args=args)
            if not isinstance(model, AbsESPnetModel):
                raise RuntimeError(
                    f"model must inherit {AbsESPnetModel.__name__},"
                    f" but got {type(model)}"
                )
            model = model.to(
                dtype=getattr(torch, args.train_dtype),
                device="cuda" if args.ngpu > 0 else "cpu",
            )
            for t in args.freeze_param:
                for k, p in model.named_parameters():
                    if k.startswith(t + ".") or k == t:
                        logging.info(f"Setting {k}.requires_grad = False")
                        p.requires_grad = False

            # Use adapter to finetune the large pre-trained foundation models
            if getattr(args, "use_adapter", False):
                create_adapter(model, args.adapter, args.adapter_conf)

            # 3. Build optimizer
            optimizers = cls.build_optimizers(args, model=model)

            # 4. Build schedulers
            schedulers = []
            for i, optim in enumerate(optimizers, 1):
                suf = "" if i == 1 else str(i)
                name = getattr(args, f"scheduler{suf}")
                conf = getattr(args, f"scheduler{suf}_conf")
                if name is not None:
                    cls_ = scheduler_classes.get(name)
                    if cls_ is None:
                        raise ValueError(
                            f"must be one of {list(scheduler_classes)}: {name}"
                        )
                    scheduler = cls_(optim, **conf)
                else:
                    scheduler = None

                schedulers.append(scheduler)

            logging.info(pytorch_cudnn_version())
            logging.info(model_summary(model))
            for i, (o, s) in enumerate(zip(optimizers, schedulers), 1):
                suf = "" if i == 1 else str(i)
                logging.info(f"Optimizer{suf}:\n{o}")
                logging.info(f"Scheduler{suf}: {s}")

        # 5. Dump "args" to config.yaml
        # NOTE(kamo): "args" should be saved after object-buildings are done
        #  because they are allowed to modify "args".
        output_dir = Path(args.output_dir)
        if not distributed_option.distributed or distributed_option.dist_rank == 0:
            output_dir.mkdir(parents=True, exist_ok=True)
            with (output_dir / "config.yaml").open("w", encoding="utf-8") as f:
                logging.info(
                    f'Saving the configuration in {output_dir / "config.yaml"}'
                )
                yaml_no_alias_safe_dump(vars(args), f, indent=4, sort_keys=False)

        if args.dry_run:
            pass
        elif args.collect_stats:
            # Perform on collect_stats mode. This mode has two roles
            # - Derive the length and dimension of all input data
            # - Accumulate feats, square values, and the length for whitening
            logging.info(args)

            if args.valid_batch_size is None:
                args.valid_batch_size = args.batch_size

            if len(args.train_shape_file) != 0:
                train_key_file = args.train_shape_file[0]
            else:
                train_key_file = None
            if len(args.valid_shape_file) != 0:
                valid_key_file = args.valid_shape_file[0]
            else:
                valid_key_file = None

            if model and not getattr(model, "extract_feats_in_collect_stats", True):
                model = None
                logging.info("Skipping collect_feats in collect_stats stage.")

            collect_stats(
                model=model,
                train_iter=cls.build_streaming_iterator(
                    data_path_and_name_and_type=args.train_data_path_and_name_and_type,
                    key_file=train_key_file,
                    batch_size=args.batch_size,
                    dtype=args.train_dtype,
                    num_workers=args.num_workers,
                    allow_variable_data_keys=args.allow_variable_data_keys,
                    ngpu=args.ngpu,
                    preprocess_fn=cls.build_preprocess_fn(args, train=False),
                    collate_fn=cls.build_collate_fn(args, train=False),
                    mode="train",
                    multi_task_dataset=args.multi_task_dataset,
                ),
                valid_iter=cls.build_streaming_iterator(
                    data_path_and_name_and_type=args.valid_data_path_and_name_and_type,
                    key_file=valid_key_file,
                    batch_size=args.valid_batch_size,
                    dtype=args.train_dtype,
                    num_workers=args.num_workers,
                    allow_variable_data_keys=args.allow_variable_data_keys,
                    ngpu=args.ngpu,
                    preprocess_fn=cls.build_preprocess_fn(args, train=False),
                    collate_fn=cls.build_collate_fn(args, train=False),
                    mode="valid",
                    multi_task_dataset=args.multi_task_dataset,
                ),
                output_dir=output_dir,
                ngpu=args.ngpu,
                log_interval=args.log_interval,
                write_collected_feats=args.write_collected_feats,
            )
        else:
            # 6. Loads pre-trained model
            for p in args.init_param:
                logging.info(f"Loading pretrained params from {p}")
                load_pretrained_model(
                    model=model,
                    init_param=p,
                    ignore_init_mismatch=args.ignore_init_mismatch,
                    # NOTE(kamo): "cuda" for torch.load always indicates cuda:0
                    #   in PyTorch<=1.4
                    map_location=(
                        f"cuda:{torch.cuda.current_device()}"
                        if args.ngpu > 0
                        else "cpu"
                    ),
                )

            # 7. Build iterator factories
            if args.multiple_iterator:
                train_iter_factory = cls.build_multiple_iter_factory(
                    args=args,
                    distributed_option=distributed_option,
                    mode="train",
                )
            else:
                train_iter_factory = cls.build_iter_factory(
                    args=args,
                    distributed_option=distributed_option,
                    mode="train",
                )
            valid_iter_factory = cls.build_iter_factory(
                args=args,
                distributed_option=distributed_option,
                mode="valid",
            )
            if not args.use_matplotlib and args.num_att_plot != 0:
                args.num_att_plot = 0
                logging.info("--use_matplotlib false => Changing --num_att_plot to 0")

            if args.num_att_plot != 0:
                plot_attention_iter_factory = cls.build_iter_factory(
                    args=args,
                    distributed_option=distributed_option,
                    mode="plot_att",
                )
            else:
                plot_attention_iter_factory = None

            # 8. Start training
            if args.use_wandb:
                if wandb is None:
                    raise RuntimeError("Please install wandb")

                try:
                    wandb.login()
                except wandb.errors.UsageError:
                    logging.info("wandb not configured! run `wandb login` to enable")
                    args.use_wandb = False

            if args.use_wandb:
                if (
                    not distributed_option.distributed
                    or distributed_option.dist_rank == 0
                ):
                    if args.wandb_project is None:
                        project = "ESPnet_" + cls.__name__
                    else:
                        project = args.wandb_project

                    # Wandb server generates a random name, if args.wandb_name is None
                    name = args.wandb_name

                    wandb.init(
                        entity=args.wandb_entity,
                        project=project,
                        name=name,
                        dir=str(output_dir),
                        id=args.wandb_id,
                        resume=args.resume,
                    )
                    wandb.config.update(args)
                else:
                    # wandb also supports grouping for distributed training,
                    # but we only log aggregated data,
                    # so it's enough to perform on rank0 node.
                    args.use_wandb = False

            # Don't give args to trainer.run() directly!!!
            # Instead of it, define "Options" object and build here.

            if args.use_deepspeed:
                if not distributed_option.distributed:
                    logging.warning(
                        "DeepSpeed is for distributed training. E.g., --ngpu > 1 "
                        "Switch back to the normal trainer."
                    )
                elif cls.trainer != Trainer:
                    raise ValueError(
                        "only default trainer is compatible with deepspeed"
                    )
                else:
                    from espnet2.train.deepspeed_trainer import DeepSpeedTrainer

                    cls.trainer = DeepSpeedTrainer
                    distributed_option.init_deepspeed()

            trainer_options = cls.trainer.build_options(args)
            cls.trainer.run(
                model=model,
                optimizers=optimizers,
                schedulers=schedulers,
                train_iter_factory=train_iter_factory,
                valid_iter_factory=valid_iter_factory,
                plot_attention_iter_factory=plot_attention_iter_factory,
                trainer_options=trainer_options,
                distributed_option=distributed_option,
            )

            if args.use_wandb and wandb.run:
                wandb.finish()

    @classmethod
    def build_iter_options(
        cls,
        args: argparse.Namespace,
        distributed_option: DistributedOption,
        mode: str,
    ):
        if mode == "train":
            preprocess_fn = cls.build_preprocess_fn(args, train=True)
            collate_fn = cls.build_collate_fn(args, train=True)
            data_path_and_name_and_type = args.train_data_path_and_name_and_type
            shape_files = args.train_shape_file
            batch_size = args.batch_size
            batch_bins = args.batch_bins
            batch_type = args.batch_type
            max_cache_size = args.max_cache_size
            max_cache_fd = args.max_cache_fd
            allow_multi_rates = args.allow_multi_rates
            distributed = distributed_option.distributed
            num_batches = None
            num_iters_per_epoch = args.num_iters_per_epoch
            train = True

        elif mode == "valid":
            preprocess_fn = cls.build_preprocess_fn(args, train=False)
            collate_fn = cls.build_collate_fn(args, train=False)
            data_path_and_name_and_type = args.valid_data_path_and_name_and_type
            shape_files = args.valid_shape_file

            if args.valid_batch_type is None:
                batch_type = args.batch_type
            else:
                batch_type = args.valid_batch_type
            if args.valid_batch_size is None:
                batch_size = args.batch_size
            else:
                batch_size = args.valid_batch_size
            if args.valid_batch_bins is None:
                batch_bins = args.batch_bins
            else:
                batch_bins = args.valid_batch_bins
            if args.valid_max_cache_size is None:
                # Cache 5% of maximum size for validation loader
                max_cache_size = 0.05 * args.max_cache_size
            else:
                max_cache_size = args.valid_max_cache_size
            max_cache_fd = args.max_cache_fd
            allow_multi_rates = args.allow_multi_rates
            distributed = distributed_option.distributed
            num_batches = None
            num_iters_per_epoch = None
            train = False

        elif mode == "plot_att":
            preprocess_fn = cls.build_preprocess_fn(args, train=False)
            collate_fn = cls.build_collate_fn(args, train=False)
            data_path_and_name_and_type = args.valid_data_path_and_name_and_type
            shape_files = args.valid_shape_file
            batch_type = "unsorted"
            batch_size = 1
            batch_bins = 0
            num_batches = args.num_att_plot
            max_cache_fd = args.max_cache_fd
            allow_multi_rates = args.allow_multi_rates
            # num_att_plot should be a few sample ~ 3, so cache all data.
            max_cache_size = np.inf if args.max_cache_size != 0.0 else 0.0
            # always False because plot_attention performs on RANK0
            distributed = False
            num_iters_per_epoch = None
            train = False
        else:
            raise NotImplementedError(f"mode={mode}")

        return IteratorOptions(
            preprocess_fn=preprocess_fn,
            collate_fn=collate_fn,
            data_path_and_name_and_type=data_path_and_name_and_type,
            shape_files=shape_files,
            batch_type=batch_type,
            batch_size=batch_size,
            batch_bins=batch_bins,
            num_batches=num_batches,
            max_cache_size=max_cache_size,
            max_cache_fd=max_cache_fd,
            allow_multi_rates=allow_multi_rates,
            distributed=distributed,
            num_iters_per_epoch=num_iters_per_epoch,
            train=train,
        )

    @classmethod
    @typechecked
    def build_iter_factory(
        cls,
        args: argparse.Namespace,
        distributed_option: DistributedOption,
        mode: str,
        kwargs: Optional[dict] = None,
    ) -> AbsIterFactory:
        """Build a factory object of mini-batch iterator.

        This object is invoked at every epochs to build the iterator for each epoch
        as following:

        >>> iter_factory = cls.build_iter_factory(...)
        >>> for epoch in range(1, max_epoch):
        ...     for keys, batch in iter_fatory.build_iter(epoch):
        ...         model(**batch)

        The mini-batches for each epochs are fully controlled by this class.
        Note that the random seed used for shuffling is decided as "seed + epoch" and
        the generated mini-batches can be reproduces when resuming.

        Note that the definition of "epoch" doesn't always indicate
        to run out of the whole training corpus.
        "--num_iters_per_epoch" option restricts the number of iterations for each epoch
        and the rest of samples for the originally epoch are left for the next epoch.
        e.g. If The number of mini-batches equals to 4, the following two are same:

        - 1 epoch without "--num_iters_per_epoch"
        - 4 epoch with "--num_iters_per_epoch" == 1

        """
        iter_options = cls.build_iter_options(args, distributed_option, mode)

        # Overwrite iter_options if any kwargs is given
        if kwargs is not None:
            for k, v in kwargs.items():
                setattr(iter_options, k, v)

        if mode == "valid" and args.valid_iterator_type is not None:
            iterator_type = args.valid_iterator_type
        else:
            iterator_type = args.iterator_type

        if iterator_type == "sequence":
            return cls.build_sequence_iter_factory(
                args=args,
                iter_options=iter_options,
                mode=mode,
            )
        elif iterator_type == "category":
            return cls.build_category_iter_factory(
                args=args,
                iter_options=iter_options,
                mode=mode,
            )
        elif iterator_type == "chunk":
            return cls.build_chunk_iter_factory(
                args=args,
                iter_options=iter_options,
                mode=mode,
            )
        elif iterator_type == "category_chunk":
            return cls.build_category_chunk_iter_factory(
                args=args,
                iter_options=iter_options,
                mode=mode,
            )
        elif iterator_type == "task":
            return cls.build_task_iter_factory(
                args=args,
                iter_options=iter_options,
                mode=mode,
            )
        else:
            raise RuntimeError(f"Not supported: iterator_type={iterator_type}")

    @classmethod
    @typechecked
    def build_dataset(
        cls,
        args: argparse.Namespace,
        iter_options: IteratorOptions,
        keys_to_load: Optional[Set[Union[int, str]]] = None,
    ) -> AbsDataset:
        if args.multi_task_dataset:
            dataset_class = ESPnetMultiTaskDataset
        else:
            dataset_class = ESPnetDataset

        dataset = dataset_class(
            iter_options.data_path_and_name_and_type,
            float_dtype=args.train_dtype,
            preprocess=iter_options.preprocess_fn,
            max_cache_size=iter_options.max_cache_size,
            max_cache_fd=iter_options.max_cache_fd,
            allow_multi_rates=iter_options.allow_multi_rates,
            keys_to_load=keys_to_load,
        )
        cls.check_task_requirements(
            dataset, args.allow_variable_data_keys, train=iter_options.train
        )
        return dataset

    @classmethod
    @typechecked
    def build_sequence_iter_factory(
        cls, args: argparse.Namespace, iter_options: IteratorOptions, mode: str
    ) -> AbsIterFactory:

        utt2category_file = (
            Path(iter_options.data_path_and_name_and_type[0][0]).parent / "utt2category"
        )
        if utt2category_file.exists():
            utt2category_file = str(utt2category_file)
            logging.warning("Reading " + utt2category_file)
        else:
            utt2category_file = None

        batch_sampler = build_batch_sampler(
            type=iter_options.batch_type,
            shape_files=iter_options.shape_files,
            fold_lengths=args.fold_length,
            batch_size=iter_options.batch_size,
            batch_bins=iter_options.batch_bins,
            sort_in_batch=args.sort_in_batch,
            sort_batch=args.sort_batch,
            drop_last=args.drop_last_iter,
            min_batch_size=(
                torch.distributed.get_world_size() if iter_options.distributed else 1
            ),
            utt2category_file=utt2category_file,
        )

        batches = list(batch_sampler)
        if iter_options.num_batches is not None:
            batches = batches[: iter_options.num_batches]

        bs_list = [len(batch) for batch in batches]

        logging.info(f"[{mode}] Batch sampler: {batch_sampler}")
        logging.info(
            f"[{mode}] mini-batch sizes summary: N-batch={len(bs_list)}, "
            f"mean={np.mean(bs_list):.1f}, min={np.min(bs_list)}, max={np.max(bs_list)}"
        )

        # Shard mini-batches for distributed training
        if iter_options.distributed:
            world_size = torch.distributed.get_world_size()
            rank = torch.distributed.get_rank()
            for batch in batches:
                if len(batch) < world_size:
                    raise RuntimeError(
                        f"The batch-size must be equal or more than world_size: "
                        f"{len(batch)} < {world_size}"
                    )
            batches = [batch[rank::world_size] for batch in batches]

        # Build dataset after sharding to reduce memory usage
        # This is very helpful for large-scale training
        dataset = cls.build_dataset(
            args,
            iter_options,
            set(itertools.chain(*batches)),
        )
        logging.info(f"[{mode}] dataset:\n{dataset}")

        return SequenceIterFactory(
            dataset=dataset,
            batches=batches,
            seed=args.seed,
            num_iters_per_epoch=iter_options.num_iters_per_epoch,
            shuffle=iter_options.train,
            shuffle_within_batch=args.shuffle_within_batch,
            num_workers=args.num_workers,
            collate_fn=iter_options.collate_fn,
            pin_memory=args.ngpu > 0,
        )

    @classmethod
    @typechecked
    def build_category_iter_factory(
        cls, args: argparse.Namespace, iter_options: IteratorOptions, mode: str
    ) -> AbsIterFactory:

        dataset = ESPnetDataset(
            iter_options.data_path_and_name_and_type,
            float_dtype=args.train_dtype,
            preprocess=iter_options.preprocess_fn,
            max_cache_size=iter_options.max_cache_size,
            max_cache_fd=iter_options.max_cache_fd,
            allow_multi_rates=iter_options.allow_multi_rates,
        )
        cls.check_task_requirements(
            dataset, args.allow_variable_data_keys, train=iter_options.train
        )

        if Path(
            Path(iter_options.data_path_and_name_and_type[0][0]).parent, "category2utt"
        ).exists():
            category2utt_file = str(
                Path(
                    Path(iter_options.data_path_and_name_and_type[0][0]).parent,
                    "category2utt",
                )
            )
            logging.warning("Reading " + category2utt_file)
        else:
            category2utt_file = None
            raise ValueError(
                "category2utt mandatory for category iterator, but not found"
            )

        sampler_args = dict(
            batch_size=iter_options.batch_size,
            min_batch_size=(
                torch.distributed.get_world_size() if iter_options.distributed else 1
            ),
            drop_last=args.drop_last_iter,
            category2utt_file=category2utt_file,
            epoch=1,
            num_batches=iter_options.num_batches,
            distributed=iter_options.distributed,
        )
        batch_sampler = CategoryBalancedSampler(**sampler_args)

        batches = list(batch_sampler)

        if iter_options.num_batches is not None:
            batches = batches[: iter_options.num_batches]

        bs_list = [len(batch) for batch in batches]

        logging.info(f"[{mode}] dataset:\n{dataset}")
        logging.info(f"[{mode}] Batch sampler: {batch_sampler}")
        logging.info(
            f"[{mode}] mini-batch sizes summary: N-batch={len(bs_list)}, "
            f"mean={np.mean(bs_list):.1f}, min={np.min(bs_list)}, max={np.max(bs_list)}"
        )

        if iter_options.distributed:
            world_size = torch.distributed.get_world_size()
            rank = torch.distributed.get_rank()
            for batch in batches:
                if len(batch) < world_size:
                    raise RuntimeError(
                        f"The batch-size must be equal or more than world_size: "
                        f"{len(batch)} < {world_size}"
                    )
            batches = [batch[rank::world_size] for batch in batches]

        return CategoryIterFactory(
            dataset=dataset,
            batches=batches,
            seed=args.seed,
            num_iters_per_epoch=iter_options.num_iters_per_epoch,
            sampler_args=sampler_args,
            shuffle=iter_options.train,
            num_workers=args.num_workers,
            collate_fn=iter_options.collate_fn,
            pin_memory=args.ngpu > 0,
        )

    @classmethod
    @typechecked
    def build_chunk_iter_factory(
        cls,
        args: argparse.Namespace,
        iter_options: IteratorOptions,
        mode: str,
    ) -> AbsIterFactory:

        dataset = ESPnetDataset(
            iter_options.data_path_and_name_and_type,
            float_dtype=args.train_dtype,
            preprocess=iter_options.preprocess_fn,
            max_cache_size=iter_options.max_cache_size,
            max_cache_fd=iter_options.max_cache_fd,
            allow_multi_rates=iter_options.allow_multi_rates,
        )
        cls.check_task_requirements(
            dataset, args.allow_variable_data_keys, train=iter_options.train
        )

        if len(iter_options.shape_files) == 0:
            key_file = iter_options.data_path_and_name_and_type[0][0]
        else:
            key_file = iter_options.shape_files[0]

        batch_sampler = UnsortedBatchSampler(batch_size=1, key_file=key_file)
        batches = list(batch_sampler)
        if iter_options.num_batches is not None:
            batches = batches[: iter_options.num_batches]
        logging.info(f"[{mode}] dataset:\n{dataset}")

        if iter_options.distributed:
            world_size = torch.distributed.get_world_size()
            rank = torch.distributed.get_rank()
            if len(batches) < world_size:
                raise RuntimeError("Number of samples is smaller than world_size")
            if iter_options.batch_size < world_size:
                raise RuntimeError("batch_size must be equal or more than world_size")

            if rank < iter_options.batch_size % world_size:
                batch_size = iter_options.batch_size // world_size + 1
            else:
                batch_size = iter_options.batch_size // world_size
            num_cache_chunks = args.num_cache_chunks // world_size
            # NOTE(kamo): Split whole corpus by sample numbers without considering
            #   each of the lengths, therefore the number of iteration counts are not
            #   always equal to each other and the iterations are limitted
            #   by the fewest iterations.
            #   i.e. the samples over the counts are discarded.
            batches = batches[rank::world_size]
        else:
            batch_size = iter_options.batch_size
            num_cache_chunks = args.num_cache_chunks

        return ChunkIterFactory(
            dataset=dataset,
            batches=batches,
            seed=args.seed,
            batch_size=batch_size,
            # For chunk iterator,
            # --num_iters_per_epoch doesn't indicate the number of iterations,
            # but indicates the number of samples.
            num_samples_per_epoch=iter_options.num_iters_per_epoch,
            shuffle=iter_options.train,
            num_workers=args.num_workers,
            collate_fn=iter_options.collate_fn,
            pin_memory=args.ngpu > 0,
            chunk_length=args.chunk_length,
            chunk_shift_ratio=args.chunk_shift_ratio,
            num_cache_chunks=num_cache_chunks,
            excluded_key_prefixes=args.chunk_excluded_key_prefixes,
            default_fs=args.chunk_default_fs,
            chunk_max_abs_length=args.chunk_max_abs_length,
            discard_short_samples=args.chunk_discard_short_samples,
        )

    @classmethod
    @typechecked
    def build_category_chunk_iter_factory(
        cls,
        args: argparse.Namespace,
        iter_options: IteratorOptions,
        mode: str,
    ) -> AbsIterFactory:

        dataset = ESPnetDataset(
            iter_options.data_path_and_name_and_type,
            float_dtype=args.train_dtype,
            preprocess=iter_options.preprocess_fn,
            max_cache_size=iter_options.max_cache_size,
            max_cache_fd=iter_options.max_cache_fd,
            allow_multi_rates=iter_options.allow_multi_rates,
        )
        cls.check_task_requirements(
            dataset, args.allow_variable_data_keys, train=iter_options.train
        )

        if Path(
            Path(iter_options.data_path_and_name_and_type[0][0]).parent, "category2utt"
        ).exists():
            category2utt_file = str(
                Path(
                    Path(iter_options.data_path_and_name_and_type[0][0]).parent,
                    "category2utt",
                )
            )
            logging.warning("Reading " + category2utt_file)
        else:
            category2utt_file = None

        sampler_args = dict(
            batch_size=args.category_sample_size,
            min_batch_size=(
                torch.distributed.get_world_size() if iter_options.distributed else 1
            ),
            drop_last=args.drop_last_iter,
            category2utt_file=category2utt_file,
            epoch=1,
            num_batches=iter_options.num_batches,
            distributed=iter_options.distributed,
        )
        batch_sampler = CategoryBalancedSampler(**sampler_args)

        batches = list(batch_sampler)
        if iter_options.num_batches is not None:
            batches = batches[: iter_options.num_batches]
        logging.info(f"[{mode}] dataset:\n{dataset}")

        if iter_options.distributed:
            world_size = torch.distributed.get_world_size()
            rank = torch.distributed.get_rank()
            if len(batches) < world_size:
                raise RuntimeError("Number of samples is smaller than world_size")
            if iter_options.batch_size < world_size:
                raise RuntimeError("batch_size must be equal or more than world_size")

            if rank < iter_options.batch_size % world_size:
                batch_size = iter_options.batch_size // world_size + 1
            else:
                batch_size = iter_options.batch_size // world_size
            num_cache_chunks = args.num_cache_chunks // world_size
            # NOTE(kamo): Split whole corpus by sample numbers without considering
            #   each of the lengths, therefore the number of iteration counts are not
            #   always equal to each other and the iterations are limitted
            #   by the fewest iterations.
            #   i.e. the samples over the counts are discarded.
            batches = batches[rank::world_size]
        else:
            batch_size = iter_options.batch_size
            num_cache_chunks = args.num_cache_chunks

        return CategoryChunkIterFactory(
            dataset=dataset,
            batches=batches,
            seed=args.seed,
            batch_size=batch_size,
            # For chunk iterator,
            # --num_iters_per_epoch doesn't indicate the number of iterations,
            # but indicates the number of samples.
            num_samples_per_epoch=iter_options.num_iters_per_epoch,
            shuffle=iter_options.train,
            num_workers=args.num_workers,
            collate_fn=iter_options.collate_fn,
            pin_memory=args.ngpu > 0,
            chunk_length=args.chunk_length,
            chunk_shift_ratio=args.chunk_shift_ratio,
            num_cache_chunks=num_cache_chunks,
            excluded_key_prefixes=args.chunk_excluded_key_prefixes,
            default_fs=args.chunk_default_fs,
            chunk_max_abs_length=args.chunk_max_abs_length,
            discard_short_samples=args.chunk_discard_short_samples,
        )

    # NOTE(kamo): Not abstract class
    @classmethod
    def build_task_iter_factory(
        cls,
        args: argparse.Namespace,
        iter_options: IteratorOptions,
        mode: str,
    ) -> AbsIterFactory:
        """Build task specific iterator factory

        Example:

            >>> class YourTask(AbsTask):
            ... @classmethod
            ... def add_task_arguments(cls, parser: argparse.ArgumentParser):
            ...     parser.set_defaults(iterator_type="task")
            ...
            ... @classmethod
            ... def build_task_iter_factory(
            ...     cls,
            ...     args: argparse.Namespace,
            ...     iter_options: IteratorOptions,
            ...     mode: str,
            ... ):
            ...     return FooIterFactory(...)
            ...
            ... @classmethod
            ... def build_iter_options(
            ....    args: argparse.Namespace,
            ...     distributed_option: DistributedOption,
            ...     mode: str
            ... ):
            ...     # if you need to customize options object
        """
        raise NotImplementedError

    @classmethod
    @typechecked
    def build_multiple_iter_factory(
        cls, args: argparse.Namespace, distributed_option: DistributedOption, mode: str
    ):
        iter_options = cls.build_iter_options(args, distributed_option, mode)
        assert len(iter_options.data_path_and_name_and_type) > 0, len(
            iter_options.data_path_and_name_and_type
        )

        # 1. Sanity check
        num_splits = None
        for path in [
            path for path, _, _ in iter_options.data_path_and_name_and_type
        ] + list(iter_options.shape_files):
            if not Path(path).is_dir():
                raise RuntimeError(f"{path} is not a directory")
            p = Path(path) / "num_splits"
            if not p.exists():
                raise FileNotFoundError(f"{p} is not found")
            with p.open() as f:
                _num_splits = int(f.read())
                if num_splits is not None and num_splits != _num_splits:
                    raise RuntimeError(
                        f"Number of splits are mismathed: "
                        f"{iter_options.data_path_and_name_and_type[0][0]} and {path}"
                    )
                num_splits = _num_splits

            for i in range(num_splits):
                p = Path(path) / f"split.{i}"
                if not p.exists():
                    raise FileNotFoundError(f"{p} is not found")

        # 2. Create functions to build an iter factory for each splits
        data_path_and_name_and_type_list = [
            [
                (str(Path(p) / f"split.{i}"), n, t)
                for p, n, t in iter_options.data_path_and_name_and_type
            ]
            for i in range(num_splits)
        ]
        shape_files_list = [
            [str(Path(s) / f"split.{i}") for s in iter_options.shape_files]
            for i in range(num_splits)
        ]
        num_iters_per_epoch_list = [
            (
                (iter_options.num_iters_per_epoch + i) // num_splits
                if iter_options.num_iters_per_epoch is not None
                else None
            )
            for i in range(num_splits)
        ]
        max_cache_size = iter_options.max_cache_size / num_splits

        # Note that iter-factories are built for each epoch at runtime lazily.
        build_funcs = [
            functools.partial(
                cls.build_iter_factory,
                args,
                distributed_option,
                mode,
                kwargs=dict(
                    data_path_and_name_and_type=_data_path_and_name_and_type,
                    shape_files=_shape_files,
                    num_iters_per_epoch=_num_iters_per_epoch,
                    max_cache_size=max_cache_size,
                ),
            )
            for (
                _data_path_and_name_and_type,
                _shape_files,
                _num_iters_per_epoch,
            ) in zip(
                data_path_and_name_and_type_list,
                shape_files_list,
                num_iters_per_epoch_list,
            )
        ]

        # 3. Build MultipleIterFactory
        return MultipleIterFactory(
            build_funcs=build_funcs, shuffle=iter_options.train, seed=args.seed
        )

    @classmethod
    @typechecked
    def build_streaming_iterator(
        cls,
        data_path_and_name_and_type,
        preprocess_fn,
        collate_fn,
        key_file: Optional[str] = None,
        batch_size: int = 1,
        dtype: Optional[Any] = np.float32,
        num_workers: int = 1,
        allow_variable_data_keys: bool = False,
        ngpu: int = 0,
        inference: bool = False,
        mode: Optional[str] = None,
        multi_task_dataset: bool = False,
    ) -> DataLoader:
        """Build DataLoader using iterable dataset"""
        # For backward compatibility for pytorch DataLoader
        if collate_fn is not None:
            kwargs = dict(collate_fn=collate_fn)
        else:
            kwargs = {}

        if multi_task_dataset:
            dataset_class = ESPnetMultiTaskDataset
        else:
            dataset_class = IterableESPnetDataset
        dataset = dataset_class(
            data_path_and_name_and_type,
            float_dtype=dtype,
            preprocess=preprocess_fn,
            key_file=key_file,
        )

        if dataset.apply_utt2category:
            kwargs.update(batch_size=1)
        else:
            kwargs.update(batch_size=batch_size)

        cls.check_task_requirements(
            dataset, allow_variable_data_keys, train=False, inference=inference
        )

        return DataLoader(
            dataset=dataset,
            pin_memory=ngpu > 0,
            num_workers=num_workers,
            sampler=getattr(dataset, "example_list", None),
            **kwargs,
        )

    # ~~~~~~~~~ The methods below are mainly used for inference ~~~~~~~~~
    @classmethod
    @typechecked
    def build_model_from_file(
        cls,
        config_file: Optional[Union[Path, str]] = None,
        model_file: Optional[Union[Path, str]] = None,
        device: str = "cpu",
    ) -> Tuple[AbsESPnetModel, argparse.Namespace]:
        """Build model from the files.

        This method is used for inference or fine-tuning.

        Args:
            config_file: The yaml file saved when training.
            model_file: The model file saved when training.
            device: Device type, "cpu", "cuda", or "cuda:N".

        """
        if config_file is None:
            assert model_file is not None, (
                "The argument 'model_file' must be provided "
                "if the argument 'config_file' is not specified."
            )
            config_file = Path(model_file).parent / "config.yaml"
        else:
            config_file = Path(config_file)

        logging.info("config file: {}".format(config_file))
        with config_file.open("r", encoding="utf-8") as f:
            args = yaml.safe_load(f)
        args = argparse.Namespace(**args)
        model = cls.build_model(args)
        if not isinstance(model, AbsESPnetModel):
            raise RuntimeError(
                f"model must inherit {AbsESPnetModel.__name__}, but got {type(model)}"
            )
        model.to(device)

        # For finetuned model, create adapter
        use_adapter = getattr(args, "use_adapter", False)
        if use_adapter:
            create_adapter(model, args.adapter, args.adapter_conf)

        if model_file is not None:
            if device == "cuda":
                # NOTE(kamo): "cuda" for torch.load always indicates cuda:0
                #   in PyTorch<=1.4
                device = f"cuda:{torch.cuda.current_device()}"
            try:
                state_dict = torch.load(
                    model_file, map_location=device, weights_only=False
                )
                # for deepspeed checkpoints
                if "module" in state_dict:
                    state_dict = state_dict["module"]
                model.load_state_dict(
                    state_dict,
                    strict=False,
                )
            except RuntimeError:
                # Note(simpleoier): the following part is to be compatible with
                #   pretrained model using earlier versions before `0a625088`
                state_dict = torch.load(model_file, map_location=device)
                if any(["frontend.upstream.model" in k for k in state_dict.keys()]):
                    if any(
                        [
                            "frontend.upstream.upstream.model" in k
                            for k in dict(model.named_parameters())
                        ]
                    ):
                        state_dict = {
                            k.replace(
                                "frontend.upstream.model",
                                "frontend.upstream.upstream.model",
                            ): v
                            for k, v in state_dict.items()
                        }
                        model.load_state_dict(state_dict, strict=not use_adapter)
                    else:
                        if any(["postdecoder" in k for k in state_dict.keys()]):
                            model.load_state_dict(
                                state_dict,
                                strict=False,
                            )
                        else:
                            raise
                else:
                    if any(["postdecoder" in k for k in state_dict.keys()]):
                        model.load_state_dict(
                            state_dict,
                            strict=False,
                        )
                    else:
                        raise

        return model, args
