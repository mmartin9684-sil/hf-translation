#!/usr/bin/env python
# coding=utf-8
# Copyright The HuggingFace Team and The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Fine-tuning the library models for sequence to sequence.
"""
# You can also adapt this script on your own sequence to sequence task. Pointers for this are left as comments.

import argparse
import logging
import os
from pathlib import Path
import re
import shutil
import sys
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse
from tempfile import mkdtemp

import datasets
import numpy as np
from datasets import load_dataset, load_metric, Dataset, DatasetDict
from iso639 import Lang
from sacremoses import MosesPunctNormalizer
from torch.utils.checkpoint import checkpoint
from clearml import Task, StorageManager
from clearml.storage.helper import StorageHelper

import transformers
from transformers import (
    AutoConfig,
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    DataCollatorForSeq2Seq,
    HfArgumentParser,
    M2M100Tokenizer,
    MBart50Tokenizer,
    MBart50TokenizerFast,
    MBartTokenizer,
    MBartTokenizerFast,
    NllbTokenizer,
    NllbTokenizerFast,
    Seq2SeqTrainer,
    Seq2SeqTrainingArguments,
    default_data_collator,
    set_seed,
)
from transformers.trainer_callback import EarlyStoppingCallback
from transformers.trainer_utils import get_last_checkpoint, PREFIX_CHECKPOINT_DIR


logger = logging.getLogger(__name__)

# A list of all multilingual tokenizer which require src_lang and tgt_lang attributes.
MULTILINGUAL_TOKENIZERS = [MBartTokenizer, MBartTokenizerFast, MBart50Tokenizer, MBart50TokenizerFast, M2M100Tokenizer, NllbTokenizer, NllbTokenizerFast]

def get_extension(file):
    if isinstance(file, list):
        file = file[0]
    return file.split(".")[-1]


def get_direct_access(url):
    helper = StorageHelper.get(url)
    if helper.base_url == "file://":
        full_url = StorageHelper.conform_url(url)
        # now get rid of the file:// prefix
        path = Path(full_url[7:])
        return path.as_posix()
    return None


def conform_url(url):
    helper = StorageHelper.get(url)
    return helper.conform_url(url)


def is_absolute(path):
    return urlparse(path).scheme != "" or os.path.isabs(path)


def make_absolute(config_url, path):
    if config_url is None:
        return path

    config_path = get_direct_access(config_url)

    if config_path is None:
        index = config_url.rfind("/")
        config_dir_url = config_url[:index + 1]
        if isinstance(path, str):
            return path if is_absolute(path) else config_dir_url + path

        return [p if is_absolute(p) else config_dir_url + p for p in path]

    config_dir = os.path.dirname(config_path)
    if isinstance(path, str):
        return path if is_absolute(path) else os.path.join(config_dir, path)

    return [p if is_absolute(p) else os.path.join(config_dir, p) for p in path]


def get_local_dataset_file(config_url, path):
    url = make_absolute(config_url, path)
    if isinstance(url, str):
        return StorageManager.get_local_copy(url, force_download=True)
    return [StorageManager.get_local_copy(u, force_download=True) for u in url]


def load_text_dataset(src_lang, trg_lang, data_files, datasets, split):
    src_path, trg_path = data_files[split]
    data = []
    with open(src_path, "r", encoding="utf-8-sig") as src_file, open(trg_path, "r", encoding="utf-8-sig") as trg_file:
        for src_line, trg_line in zip(src_file, trg_file):
            data.append({src_lang: src_line.strip(), trg_lang: trg_line.strip()})
    datasets[split] = Dataset.from_dict({"translation": data})


def delete_url(url):
    parsed = urlparse(url)
    helper = StorageHelper.get(f"{parsed.scheme}://{parsed.netloc}")
    helper.delete(parsed.path)


@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.
    """

    model_name_or_path: str = field(
        metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models"}
    )
    config_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"}
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={"help": "Where to store the pretrained models downloaded from huggingface.co"},
    )
    use_fast_tokenizer: bool = field(
        default=True,
        metadata={"help": "Whether to use one of the fast tokenizer (backed by the tokenizers library) or not."},
    )
    model_revision: str = field(
        default="main",
        metadata={"help": "The specific model version to use (can be a branch name, tag name or commit id)."},
    )
    use_auth_token: bool = field(
        default=False,
        metadata={
            "help": (
                "Will use the token generated when running `transformers-cli login` (necessary to use this script "
                "with private models)."
            )
        },
    )
    add_new_lang_code: bool = field(
        default=True, metadata={"help": "Will add a new language code to the vocabulary."}
    )
    project_name: str = field(default=None, metadata={"help": "ClearML project name."})
    task_name: str = field(default=None, metadata={"help": "ClearML task name."})


@dataclass
class DataTrainingArguments:
    """
    Arguments pertaining to what data we are going to input our model for training and eval.
    """

    source_lang: str = field(default=None, metadata={"help": "Source language id for translation."})
    target_lang: str = field(default=None, metadata={"help": "Target language id for translation."})

    dataset_name: Optional[str] = field(
        default=None, metadata={"help": "The name of the dataset to use (via the datasets library)."}
    )
    dataset_config_name: Optional[str] = field(
        default=None, metadata={"help": "The configuration name of the dataset to use (via the datasets library)."}
    )
    train_file: Optional[str] = field(default=None, metadata={"help": "The input training data file (a jsonlines)."})
    validation_file: Optional[str] = field(
        default=None,
        metadata={
            "help": "An optional input evaluation data file to evaluate the metrics (sacreblue) on a jsonlines file."
        },
    )
    test_file: Optional[str] = field(
        default=None,
        metadata={"help": "An optional input test data file to evaluate the metrics (sacreblue) on a jsonlines file."},
    )
    overwrite_cache: bool = field(
        default=False, metadata={"help": "Overwrite the cached training and evaluation sets"}
    )
    preprocessing_num_workers: Optional[int] = field(
        default=None,
        metadata={"help": "The number of processes to use for the preprocessing."},
    )
    max_source_length: Optional[int] = field(
        default=1024,
        metadata={
            "help": (
                "The maximum total input sequence length after tokenization. Sequences longer "
                "than this will be truncated, sequences shorter will be padded."
            )
        },
    )
    max_target_length: Optional[int] = field(
        default=128,
        metadata={
            "help": (
                "The maximum total sequence length for target text after tokenization. Sequences longer "
                "than this will be truncated, sequences shorter will be padded."
            )
        },
    )
    val_max_target_length: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "The maximum total sequence length for validation target text after tokenization. Sequences longer "
                "than this will be truncated, sequences shorter will be padded. Will default to `max_target_length`."
                "This argument is also used to override the ``max_length`` param of ``model.generate``, which is used "
                "during ``evaluate`` and ``predict``."
            )
        },
    )
    pad_to_max_length: bool = field(
        default=False,
        metadata={
            "help": (
                "Whether to pad all samples to model maximum sentence length. "
                "If False, will pad the samples dynamically when batching to the maximum length in the batch. More "
                "efficient on GPU but very bad for TPU."
            )
        },
    )
    max_train_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "For debugging purposes or quicker training, truncate the number of training examples to this "
                "value if set."
            )
        },
    )
    max_eval_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
                "value if set."
            )
        },
    )
    max_predict_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "For debugging purposes or quicker training, truncate the number of prediction examples to this "
                "value if set."
            )
        },
    )
    num_beams: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "Number of beams to use for evaluation. This argument will be passed to ``model.generate``, "
                "which is used during ``evaluate`` and ``predict``."
            )
        },
    )
    ignore_pad_token_for_loss: bool = field(
        default=True,
        metadata={
            "help": "Whether to ignore the tokens corresponding to padded labels in the loss computation or not."
        },
    )
    source_prefix: Optional[str] = field(
        default=None, metadata={"help": "A prefix to add before every source text (useful for T5 models)."}
    )
    forced_bos_token: Optional[str] = field(
        default=None,
        metadata={
            "help": (
                "The token to force as the first generated token after the :obj:`decoder_start_token_id`.Useful for"
                " multilingual models like :doc:`mBART <../model_doc/mbart>` where the first generated token needs to"
                " be the target language token.(Usually it is the target language token)"
            )
        },
    )
    early_stopping_patience: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "Use with :doc:`metric_for_best_model` to stop training when the specified metric worsens for"
                " :doc:`early_stopping_patience` evaluation calls."
            )
        }
    )
    early_stopping_threshold: Optional[float] = field(
        default=None,
        metadata={
            "help": (
                "Use with :doc:`metric_for_best_model` and :doc:`early_stopping_patience` to denote how much the"
                " specified metric must improve to satisfy early stopping conditions."
            )
        }
    )
    delete_checkpoints_at_end: Optional[bool] = field(
        default=False, metadata={"help": "Whether to delete checkpoints at end of training."}
    )

    def __post_init__(self):
        if self.dataset_name is None and self.train_file is None and self.validation_file is None:
            raise ValueError("Need either a dataset name or a training/validation file.")
        elif self.source_lang is None or self.target_lang is None:
            raise ValueError("Need to specify the source language and the target language.")

        # accepting both json and jsonl file extensions, as
        # many jsonlines files actually have a .json extension
        valid_extensions = ["json", "jsonl", "txt"]

        if self.train_file is not None:
            extension = get_extension(self.train_file)
            assert extension in valid_extensions, "`train_file` should be a jsonlines file or text files."
        if self.validation_file is not None:
            extension = get_extension(self.validation_file)
            assert extension in valid_extensions, "`validation_file` should be a jsonlines file or text files."
        if self.val_max_target_length is None:
            self.val_max_target_length = self.max_target_length


def main():
    # See all possible arguments in src/transformers/training_args.py
    # or by passing the --help flag to this script.
    # We now keep distinct sets of args, for a cleaner separation of concerns.

    simple_parser = argparse.ArgumentParser()
    simple_parser.add_argument("--config_file", type=str, help="The JSON config file.")
    simple_parser.add_argument("--extra_tokens", type=str, default=None, help="File of extra tokens for the tokenizer")
    simple_parser.add_argument("--do_train", default=False, action="store_true", help="Whether to run training.")
    simple_parser.add_argument("--do_eval", default=False, action="store_true", help="Whether to run eval on the dev set.")
    simple_parser.add_argument("--do_predict", default=False, action="store_true", help="Whether to run predictions on the test set.")
    simple_parser.add_argument("--enable_clearml", default=False, action="store_true", help="Enable ClearML.")
    simple_parser.add_argument("--project_name", type=str, help="ClearML project name.")
    simple_parser.add_argument("--task_name", type=str, help="ClearML task name.")
    args = simple_parser.parse_args()

    if not args.do_train and not args.do_eval and not args.do_predict:
        args.do_train = True
        args.do_eval = True
        args.do_predict = True

    config_url = None
    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, Seq2SeqTrainingArguments))
    if args.config_file is not None:
        config_path = StorageManager.get_local_copy(args.config_file, force_download=True)
        # If we pass only one argument to the script and it's the path to a json file,
        # let's parse it to get our arguments.
        model_args, data_args, training_args = parser.parse_json_file(json_file=config_path)
        config_url = conform_url(args.config_file)
    else:
        model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    extra_tokens = None
    if args.extra_tokens is not None:
        tokens_path = StorageManager.get_local_copy(args.extra_tokens, force_download=True)
        with open(tokens_path, 'r', encoding='utf-8') as t:
            extra_tokens = [chr(int(d)) for d in t.read().splitlines()]

    training_args.do_train = args.do_train
    training_args.do_eval = args.do_eval
    training_args.do_predict = args.do_predict
    if args.project_name is not None:
        model_args.project_name = args.project_name
    if args.task_name is not None:
        model_args.task_name = args.task_name

    if model_args.project_name is None:
        model_args.project_name = model_args.task_name
    if args.enable_clearml:
        Task.init(project_name=model_args.project_name, task_name=model_args.task_name)

    # Sending telemetry. Tracking the example usage helps us better allocate resources to maintain them. The
    # information sent is the one passed as arguments along with your Python/PyTorch versions.
    # send_example_telemetry("run_translation", model_args, data_args)

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    datasets.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    # Log on each process the small summary:
    logger.warning(
        f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}, "
        + f"distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}"
    )
    logger.info(f"Training/evaluation parameters {training_args}")

    if data_args.source_prefix is None and model_args.model_name_or_path in [
        "t5-small",
        "t5-base",
        "t5-large",
        "t5-3b",
        "t5-11b",
    ]:
        logger.warning(
            "You're running a t5 model but didn't provide a source prefix, which is expected, e.g. with "
            "`--source_prefix 'translate English to German: ' `"
        )

    # Detecting last checkpoint.
    last_checkpoint = None
    output_url = make_absolute(config_url, training_args.output_dir)
    output_dir = get_direct_access(output_url)
    temp_dir = None
    if output_dir is None:
        temp_dir = mkdtemp()
        StorageManager.download_folder(output_url, temp_dir, overwrite=True)
        parsed = urlparse(output_url)
        output_dir = os.path.join(temp_dir, parsed.path[1:])
    if training_args.logging_dir.startswith(training_args.output_dir):
        logging_dir = training_args.logging_dir
        logging_dir = output_dir + logging_dir[len(training_args.output_dir):]
        training_args.logging_dir = logging_dir
    training_args.output_dir = output_dir
    try:
        if os.path.isdir(training_args.output_dir) and training_args.do_train and not training_args.overwrite_output_dir:
            last_checkpoint = get_last_checkpoint(training_args.output_dir)
            if last_checkpoint is None and any(os.path.isfile(p) for p in os.listdir(training_args.output_dir)):
                raise ValueError(
                    f"Output directory ({training_args.output_dir}) already exists and is not empty. "
                    "Use --overwrite_output_dir to overcome."
                )
            elif last_checkpoint is not None and training_args.resume_from_checkpoint is None:
                logger.info(
                    f"Checkpoint detected, resuming training at {last_checkpoint}. To avoid this behavior, change "
                    "the `--output_dir` or add `--overwrite_output_dir` to train from scratch."
                )

        # Set seed before initializing model.
        set_seed(training_args.seed)

        # Get the language codes for input/target.
        source_lang = data_args.source_lang.split("_")[0]
        source_codes = Lang(source_lang)
        source_lang = source_codes.pt1 if source_codes.pt1 != "" else source_codes.pt3
        target_lang = data_args.target_lang.split("_")[0]
        target_codes = Lang(target_lang)
        target_lang = target_codes.pt1 if target_codes.pt1 != "" else target_codes.pt3

        # Get the datasets: you can either provide your own JSON training and evaluation files (see below)
        # or just provide the name of one of the public datasets available on the hub at https://huggingface.co/datasets/
        # (the dataset will be downloaded automatically from the datasets Hub).
        #
        # For translation, only JSON files are supported, with one field named "translation" containing two keys for the
        # source and target languages (unless you adapt what follows).
        #
        # In distributed training, the load_dataset function guarantee that only one local process can concurrently
        # download the dataset.
        if data_args.dataset_name is not None:
            # Downloading and loading a dataset from the hub.
            raw_datasets = load_dataset(
                data_args.dataset_name,
                data_args.dataset_config_name,
                cache_dir=model_args.cache_dir,
                use_auth_token=True if model_args.use_auth_token else None,
            )
        else:
            data_files = {}
            if data_args.train_file is not None:
                data_files["train"] = get_local_dataset_file(config_url, data_args.train_file)
                extension = get_extension(data_args.train_file)
            if data_args.validation_file is not None:
                data_files["validation"] = get_local_dataset_file(config_url, data_args.validation_file)
                extension = get_extension(data_args.validation_file)
            if data_args.test_file is not None:
                data_files["test"] = get_local_dataset_file(config_url, data_args.test_file)
                extension = get_extension(data_args.test_file)

            if extension == "txt":
                ds = {}
                if "train" in data_files:
                    load_text_dataset(source_lang, target_lang, data_files, ds, "train")
                if "validation" in data_files:
                    load_text_dataset(source_lang, target_lang, data_files, ds, "validation")
                if "test" in data_files:
                    load_text_dataset(source_lang, target_lang, data_files, ds, "test")
                raw_datasets = DatasetDict(ds)
            else:
                raw_datasets = load_dataset(
                    extension,
                    data_files=data_files,
                    cache_dir=model_args.cache_dir,
                    use_auth_token=True if model_args.use_auth_token else None,
                )
        # See more about loading any type of standard or custom dataset (from files, python dict, pandas DataFrame, etc) at
        # https://huggingface.co/docs/datasets/loading_datasets.html.

        # Load pretrained model and tokenizer
        #
        # Distributed training:
        # The .from_pretrained methods guarantee that only one local process can concurrently
        # download model & vocab.
        config = AutoConfig.from_pretrained(
            model_args.config_name if model_args.config_name else model_args.model_name_or_path,
            cache_dir=model_args.cache_dir,
            revision=model_args.model_revision,
            use_auth_token=True if model_args.use_auth_token else None,
            use_cache=not training_args.gradient_checkpointing,
        )
        tokenizer = AutoTokenizer.from_pretrained(
            model_args.tokenizer_name if model_args.tokenizer_name else model_args.model_name_or_path,
            cache_dir=model_args.cache_dir,
            use_fast=model_args.use_fast_tokenizer,
            revision=model_args.model_revision,
            use_auth_token=True if model_args.use_auth_token else None,
        )
        model = AutoModelForSeq2SeqLM.from_pretrained(
            model_args.model_name_or_path,
            from_tf=bool(".ckpt" in model_args.model_name_or_path),
            config=config,
            cache_dir=model_args.cache_dir,
            revision=model_args.model_revision,
            use_auth_token=True if model_args.use_auth_token else None,
        )

        def add_lang_code_to_tokenizer(tokenizer, lang_code):
            if lang_code in tokenizer.lang_code_to_id:
                return
            tokenizer.add_special_tokens({"additional_special_tokens": tokenizer.additional_special_tokens + [lang_code]})
            lang_id = tokenizer.convert_tokens_to_ids(lang_code)
            tokenizer.lang_code_to_id[lang_code] = lang_id
            if isinstance(tokenizer, (NllbTokenizer, MBart50Tokenizer, MBartTokenizer)):
                tokenizer.id_to_lang_code[lang_id] = lang_code
                tokenizer.fairseq_tokens_to_ids[lang_code] = lang_id
                tokenizer.fairseq_ids_to_tokens[lang_id] = lang_code
            elif isinstance(tokenizer, M2M100Tokenizer):
                tokenizer.lang_token_to_id[lang_code] = lang_id
                tokenizer.id_to_lang_token[lang_id] = lang_code

        if model_args.add_new_lang_code and isinstance(tokenizer, tuple(MULTILINGUAL_TOKENIZERS)):
            add_lang_code_to_tokenizer(tokenizer, data_args.source_lang)
            add_lang_code_to_tokenizer(tokenizer, data_args.target_lang)

        if extra_tokens is not None:
            # Drop any extra tokens that are already in the tokenizer's vocab
            extra_tokens = set(extra_tokens) - set(tokenizer.vocab.keys())
            # Add the extra tokens to the vocab
            tokenizer.add_tokens(list(extra_tokens))

        model.resize_token_embeddings(len(tokenizer))

        # Set decoder_start_token_id
        if model.config.decoder_start_token_id is None and isinstance(tokenizer, (MBartTokenizer, MBartTokenizerFast)):
            if isinstance(tokenizer, MBartTokenizer):
                model.config.decoder_start_token_id = tokenizer.lang_code_to_id[data_args.target_lang]
            else:
                model.config.decoder_start_token_id = tokenizer.convert_tokens_to_ids(data_args.target_lang)

        if model.config.decoder_start_token_id is None:
            raise ValueError("Make sure that `config.decoder_start_token_id` is correctly defined")

        prefix = data_args.source_prefix if data_args.source_prefix is not None else ""

        # Preprocessing the datasets.
        # We need to tokenize inputs and targets.
        if training_args.do_train:
            column_names = raw_datasets["train"].column_names
        elif training_args.do_eval:
            column_names = raw_datasets["validation"].column_names
        elif training_args.do_predict:
            column_names = raw_datasets["test"].column_names
        else:
            logger.info("There is nothing to do. Please pass `do_train`, `do_eval` and/or `do_predict`.")
            return

        # For translation we set the codes of our source and target languages (only useful for mBART, the others will
        # ignore those attributes).
        if isinstance(tokenizer, tuple(MULTILINGUAL_TOKENIZERS)):
            assert data_args.target_lang is not None and data_args.source_lang is not None, (
                f"{tokenizer.__class__.__name__} is a multilingual tokenizer which requires --source_lang and "
                "--target_lang arguments."
            )

            tokenizer.src_lang = data_args.source_lang
            tokenizer.tgt_lang = data_args.target_lang

            # For multilingual translation models like mBART-50 and M2M100 we need to force the target language token
            # as the first generated token. We ask the user to explicitly provide this as --forced_bos_token argument.
            forced_bos_token_id = (
                tokenizer.lang_code_to_id[data_args.forced_bos_token] if data_args.forced_bos_token is not None else None
            )
            model.config.forced_bos_token_id = forced_bos_token_id

        # Temporarily set max_target_length for training.
        max_target_length = data_args.max_target_length
        padding = "max_length" if data_args.pad_to_max_length else False

        if training_args.label_smoothing_factor > 0 and not hasattr(model, "prepare_decoder_input_ids_from_labels"):
            logger.warning(
                "label_smoothing is enabled but the `prepare_decoder_input_ids_from_labels` method is not defined for"
                f"`{model.__class__.__name__}`. This will lead to loss being calculated twice and will take up more memory"
            )

        mpn = MosesPunctNormalizer()
        mpn.substitutions = [(re.compile(r), sub) for r, sub in mpn.substitutions]

        def preprocess_function(examples):
            inputs = [mpn.normalize(ex[source_lang]) for ex in examples["translation"]]
            targets = [mpn.normalize(ex[target_lang]) for ex in examples["translation"]]
            inputs = [prefix + inp for inp in inputs]
            model_inputs = tokenizer(inputs, max_length=data_args.max_source_length, padding=padding, truncation=True)

            # Setup the tokenizer for targets
            with tokenizer.as_target_tokenizer():
                labels = tokenizer(targets, max_length=max_target_length, padding=padding, truncation=True)

            # If we are padding here, replace all tokenizer.pad_token_id in the labels by -100 when we want to ignore
            # padding in the loss.
            if padding == "max_length" and data_args.ignore_pad_token_for_loss:
                labels["input_ids"] = [
                    [(l if l != tokenizer.pad_token_id else -100) for l in label] for label in labels["input_ids"]
                ]

            model_inputs["labels"] = labels["input_ids"]
            return model_inputs

        if training_args.do_train:
            if "train" not in raw_datasets:
                raise ValueError("--do_train requires a train dataset")
            train_dataset = raw_datasets["train"]
            if data_args.max_train_samples is not None:
                max_train_samples = min(len(train_dataset), data_args.max_train_samples)
                train_dataset = train_dataset.select(range(max_train_samples))
            with training_args.main_process_first(desc="train dataset map pre-processing"):
                train_dataset = train_dataset.map(
                    preprocess_function,
                    batched=True,
                    num_proc=data_args.preprocessing_num_workers,
                    remove_columns=column_names,
                    load_from_cache_file=not data_args.overwrite_cache,
                    desc="Running tokenizer on train dataset",
                )

        if training_args.do_eval:
            max_target_length = data_args.val_max_target_length
            if "validation" not in raw_datasets:
                raise ValueError("--do_eval requires a validation dataset")
            eval_dataset = raw_datasets["validation"]
            if data_args.max_eval_samples is not None:
                max_eval_samples = min(len(eval_dataset), data_args.max_eval_samples)
                eval_dataset = eval_dataset.select(range(max_eval_samples))
            with training_args.main_process_first(desc="validation dataset map pre-processing"):
                eval_dataset = eval_dataset.map(
                    preprocess_function,
                    batched=True,
                    num_proc=data_args.preprocessing_num_workers,
                    remove_columns=column_names,
                    load_from_cache_file=not data_args.overwrite_cache,
                    desc="Running tokenizer on validation dataset",
                )

        if training_args.do_predict:
            max_target_length = data_args.val_max_target_length
            if "test" not in raw_datasets:
                raise ValueError("--do_predict requires a test dataset")
            predict_dataset = raw_datasets["test"]
            if data_args.max_predict_samples is not None:
                max_predict_samples = min(len(predict_dataset), data_args.max_predict_samples)
                predict_dataset = predict_dataset.select(range(max_predict_samples))
            with training_args.main_process_first(desc="prediction dataset map pre-processing"):
                predict_dataset = predict_dataset.map(
                    preprocess_function,
                    batched=True,
                    num_proc=data_args.preprocessing_num_workers,
                    remove_columns=column_names,
                    load_from_cache_file=not data_args.overwrite_cache,
                    desc="Running tokenizer on prediction dataset",
                )

        # Data collator
        label_pad_token_id = -100 if data_args.ignore_pad_token_for_loss else tokenizer.pad_token_id
        if data_args.pad_to_max_length:
            data_collator = default_data_collator
        else:
            data_collator = DataCollatorForSeq2Seq(
                tokenizer,
                model=model,
                label_pad_token_id=label_pad_token_id,
                pad_to_multiple_of=8 if training_args.fp16 else None,
            )

        # Metric
        metric = load_metric("sacrebleu")

        def postprocess_text(preds, labels):
            preds = [pred.strip() for pred in preds]
            labels = [[label.strip()] for label in labels]

            return preds, labels

        def compute_metrics(eval_preds):
            preds, labels = eval_preds
            if isinstance(preds, tuple):
                preds = preds[0]
            decoded_preds = tokenizer.batch_decode(preds, skip_special_tokens=True)
            if data_args.ignore_pad_token_for_loss:
                # Replace -100 in the labels as we can't decode them.
                labels = np.where(labels != -100, labels, tokenizer.pad_token_id)
            decoded_labels = tokenizer.batch_decode(labels, skip_special_tokens=True)

            # Some simple post-processing
            decoded_preds, decoded_labels = postprocess_text(decoded_preds, decoded_labels)

            result = metric.compute(predictions=decoded_preds, references=decoded_labels, lowercase=True)
            result = {"bleu": result["score"]}

            prediction_lens = [np.count_nonzero(pred != tokenizer.pad_token_id) for pred in preds]
            result["gen_len"] = np.mean(prediction_lens)
            result = {k: round(v, 4) for k, v in result.items()}
            return result

        # Initialize our Trainer
        trainer = Seq2SeqTrainer(
            model=model,
            args=training_args,
            train_dataset=train_dataset if training_args.do_train else None,
            eval_dataset=eval_dataset if training_args.do_eval else None,
            tokenizer=tokenizer,
            data_collator=data_collator,
            compute_metrics=compute_metrics if training_args.predict_with_generate else None,
        )
        if data_args.early_stopping_patience > 0:
            trainer.add_callback(
                EarlyStoppingCallback(
                    early_stopping_patience=data_args.early_stopping_patience,
                    early_stopping_threshold=data_args.early_stopping_threshold
                )
            )

        # Training
        if training_args.do_train:
            checkpoint = None
            if training_args.resume_from_checkpoint is not None:
                checkpoint = training_args.resume_from_checkpoint
            elif last_checkpoint is not None:
                checkpoint = last_checkpoint
            train_result = trainer.train(resume_from_checkpoint=checkpoint)
            trainer.save_model()  # Saves the tokenizer too for easy upload

            metrics = train_result.metrics
            max_train_samples = (
                data_args.max_train_samples if data_args.max_train_samples is not None else len(train_dataset)
            )
            metrics["train_samples"] = min(max_train_samples, len(train_dataset))

            trainer.log_metrics("train", metrics)
            trainer.save_metrics("train", metrics)
            trainer.save_state()

            if data_args.delete_checkpoints_at_end:
                logger.info("Deleting checkpoints")
                checkpoints = [str(x) for x in Path(training_args.output_dir).glob(f"{PREFIX_CHECKPOINT_DIR}-*") if os.path.isdir(x)]
                for checkpoint in checkpoints:
                    shutil.rmtree(checkpoint)

        # Evaluation
        results = {}
        max_length = (
            training_args.generation_max_length
            if training_args.generation_max_length is not None
            else data_args.val_max_target_length
        )
        num_beams = data_args.num_beams if data_args.num_beams is not None else training_args.generation_num_beams
        if training_args.do_eval:
            logger.info("*** Evaluate ***")

            metrics = trainer.evaluate(max_length=max_length, num_beams=num_beams, metric_key_prefix="eval")
            max_eval_samples = data_args.max_eval_samples if data_args.max_eval_samples is not None else len(eval_dataset)
            metrics["eval_samples"] = min(max_eval_samples, len(eval_dataset))

            trainer.log_metrics("eval", metrics)
            trainer.save_metrics("eval", metrics)

        if training_args.do_predict:
            logger.info("*** Predict ***")

            predict_results = trainer.predict(
                predict_dataset, metric_key_prefix="predict", max_length=max_length, num_beams=num_beams
            )
            trainer.state.global_step
            metrics = predict_results.metrics
            max_predict_samples = (
                data_args.max_predict_samples if data_args.max_predict_samples is not None else len(predict_dataset)
            )
            metrics["predict_samples"] = min(max_predict_samples, len(predict_dataset))

            trainer.log_metrics("predict", metrics)
            trainer.save_metrics("predict", metrics)

            if trainer.is_world_process_zero():
                if training_args.predict_with_generate:
                    predictions = tokenizer.batch_decode(
                        predict_results.predictions, skip_special_tokens=True, clean_up_tokenization_spaces=True
                    )
                    predictions = [pred.strip() for pred in predictions]
                    output_prediction_file = os.path.join(training_args.output_dir, "generated_predictions.txt")
                    with open(output_prediction_file, "w", encoding="utf-8") as writer:
                        writer.write("\n".join(predictions))

        kwargs = {"finetuned_from": model_args.model_name_or_path, "tasks": "translation"}
        if data_args.dataset_name is not None:
            kwargs["dataset_tags"] = data_args.dataset_name
            if data_args.dataset_config_name is not None:
                kwargs["dataset_args"] = data_args.dataset_config_name
                kwargs["dataset"] = f"{data_args.dataset_name} {data_args.dataset_config_name}"
            else:
                kwargs["dataset"] = data_args.dataset_name

        languages = [l for l in [data_args.source_lang, data_args.target_lang] if l is not None]
        if len(languages) > 0:
            kwargs["language"] = languages

        if training_args.push_to_hub:
            trainer.push_to_hub(**kwargs)
        else:
            trainer.create_model_card(**kwargs)
    finally:
        if temp_dir is not None:
            urls = StorageManager.list(output_url, return_full_path=True)
            for url in urls:
                if url[len(output_url):].startswith(f"/{PREFIX_CHECKPOINT_DIR}-"):
                    delete_url(url)
            StorageManager.upload_folder(training_args.output_dir, output_url)
            shutil.rmtree(temp_dir)

    return results


def _mp_fn(index):
    # For xla_spawn (TPUs)
    main()


if __name__ == "__main__":
    main()
