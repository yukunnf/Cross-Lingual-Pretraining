import argparse
import logging
import math
import os
import pickle
import random
import datasets
import numpy as np
import torch
from datasets import load_dataset, load_metric, load_from_disk
from torch.utils.data import DataLoader
from tqdm.auto import tqdm

import transformers
from transformers import (
    CONFIG_MAPPING,
    MODEL_MAPPING,
    AdamW,
    AutoConfig,
    AutoModelForQuestionAnswering,
    AutoTokenizer,
    DataCollatorWithPadding,
    EvalPrediction,
    SchedulerType,
    default_data_collator,
    get_scheduler,
    set_seed,
)
from transformers.file_utils import get_full_repo_name
from transformers.utils import check_min_version
from transformers.utils.versions import require_version
from utils_qa import postprocess_qa_predictions

# all_lang_list  = ["ar","de","el","en","es","hi","ro","ru","th","tr","vi","zh"]
all_lang_list = ["ar", "de", "el", "en", "es", "hi", "ro", "ru", "tr", "vi", "zh"]

# Will error if the minimal version of Transformers is not installed. Remove at your own risks.
check_min_version("4.18.0.dev0")
require_version("datasets>=1.8.0", "To fix: pip install -r examples/pytorch/question-answering/requirements.txt")
logger = logging.getLogger(__name__)
# You should update this to your particular problem to have better documentation of `model_type`
MODEL_CONFIG_CLASSES = list(MODEL_MAPPING.keys())
MODEL_TYPES = tuple(conf.model_type for conf in MODEL_CONFIG_CLASSES)



class ReverseSqrtScheduler:
    def __init__(self, optimizer, lr, n_warmup_steps):
        self._optimizer = optimizer
        self.lr_mul = lr
        self.n_warmup_steps = n_warmup_steps
        self.n_steps = 0

        self.decay_factor = [_lr * n_warmup_steps ** 0.5 for _lr in lr]
        self.lr_step = [(_lr - 0) / n_warmup_steps for _lr in lr]

    def step_and_update_lr(self):
        self._update_learning_rate()
        self._optimizer.step()

    def zero_grad(self):
        self._optimizer.zero_grad()

    def _update_learning_rate(self):
        self.n_steps += 1
        if self.n_steps < self.n_warmup_steps:
            lr = [self.n_steps * _lr for _lr in self.lr_step]
        else:
            lr = [_decay_factor * self.n_steps ** -0.5 for _decay_factor in self.decay_factor]

        for i, param_group in enumerate(self._optimizer.param_groups):
            param_group['lr'] = lr[i]


def parse_args():
    parser = argparse.ArgumentParser(description="Finetune a transformers model on a Question Answering task")
    parser.add_argument('--ratio', required=True, type=int)
    parser.add_argument("--replace_table_file", type=str, default=None)
    parser.add_argument('--loss_interval', required=True, type=int)
    parser.add_argument('--eval_interval', default=-1, type=int)
    parser.add_argument("--output_log_file", type=str, required=True)
    parser.add_argument("--eval_lang", type=str, required=True)
    parser.add_argument(
        "--dataset_name",
        type=str,
        default=None,
        help="The name of the dataset to use (via the datasets library).",
    )
    parser.add_argument(
        "--dataset_config_name",
        type=str,
        default=None,
        help="The configuration name of the dataset to use (via the datasets library).",
    )
    parser.add_argument(
        "--train_file", type=str, default=None, help="A csv or a json file containing the training data."
    )
    parser.add_argument(
        "--preprocessing_num_workers", type=int, default=4, help="A csv or a json file containing the training data."
    )
    parser.add_argument("--do_predict", action="store_true", help="To do prediction on the question answering model")
    parser.add_argument(
        "--validation_file", type=str, default=None, help="A csv or a json file containing the validation data."
    )
    parser.add_argument(
        "--test_file", type=str, default=None, help="A csv or a json file containing the Prediction data."
    )
    parser.add_argument(
        "--max_seq_length", type=int, default=384,
        help="The maximum total input sequence length after tokenization. Sequences longer than this will be truncated,"
             " sequences shorter will be padded if `--pad_to_max_lengh` is passed.",
    )
    parser.add_argument(
        "--pad_to_max_length",
        action="store_true",
        help="If passed, pad all samples to `max_seq_length`. Otherwise, dynamic padding is used.",
    )
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
        required=True,
    )
    parser.add_argument(
        "--config_name",
        type=str,
        default=None,
        help="Pretrained config name or path if not the same as model_name",
    )
    parser.add_argument(
        "--tokenizer_name",
        type=str,
        default=None,
        help="Pretrained tokenizer name or path if not the same as model_name",
    )
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the training dataloader.",
    )
    parser.add_argument(
        "--per_device_eval_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the evaluation dataloader.",
    )
    parser.add_argument(
        "--learning_rate_pre_train",
        type=float,
        default=5e-5,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument(
        "--learning_rate_fine_tune",
        type=float,
        default=5e-5,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument("--weight_decay", type=float, default=0.0, help="Weight decay to use.")
    parser.add_argument("--num_train_epochs", type=int, default=3, help="Total number of training epochs to perform.")
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform. If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--lr_scheduler_type",
        type=SchedulerType,
        default="linear",
        help="The scheduler type to use.",
        choices=["linear", "cosine", "cosine_with_restarts", "polynomial", "constant", "constant_with_warmup"],
    )

    parser.add_argument(
        "--num_warmup_steps", type=int, default=0, help="Number of steps for the warmup in the lr scheduler."
    )
    parser.add_argument("--output_dir", type=str, default=None, help="Where to store the final model.")
    parser.add_argument("--seed", type=int, default=None, help="A seed for reproducible training.")
    parser.add_argument(
        "--model_type",
        type=str,
        default=None,
        help="Model type to use if training from scratch.",
        choices=MODEL_TYPES,
    )
    parser.add_argument(
        "--doc_stride",
        type=int,
        default=128,
        help="When splitting up a long document into chunks how much stride to take between chunks.",
    )
    parser.add_argument(
        "--n_best_size",
        type=int,
        default=20,
        help="The total number of n-best predictions to generate when looking for an answer.",
    )
    parser.add_argument(
        "--null_score_diff_threshold",
        type=float,
        default=0.0,
        help="The threshold used to select the null answer: if the best answer has a score that is less than "
             "the score of the null answer minus this threshold, the null answer is selected for this example. "
             "Only useful when `version_2_with_negative=True`.",
    )
    parser.add_argument(
        "--version_2_with_negative",
        type=bool,
        default=False,
        help="If true, some of the examples do not have an answer.",
    )
    parser.add_argument(
        "--max_answer_length",
        type=int,
        default=30,
        help="The maximum length of an answer that can be generated. This is needed because the start "
             "and end predictions are not conditioned on one another.",
    )
    parser.add_argument(
        "--max_train_samples",
        type=int,
        default=None,
        help="For debugging purposes or quicker training, truncate the number of training examples to this "
             "value if set.",
    )
    parser.add_argument(
        "--max_eval_samples",
        type=int,
        default=None,
        help="For debugging purposes or quicker training, truncate the number of evaluation examples to this "
             "value if set.",
    )
    parser.add_argument(
        "--max_predict_samples",
        type=int,
        default=None,
        help="For debugging purposes or quicker training, truncate the number of prediction examples to this",
    )
    parser.add_argument(
        "--overwrite_cache", type=bool, default=False, help="Overwrite the cached training and evaluation sets"
    )
    args = parser.parse_args()
    if (
            args.dataset_name is None
            and args.train_file is None
            and args.validation_file is None
            and args.test_file is None
    ):
        raise ValueError("Need either a dataset name or a training/validation/test file.")
    else:
        if args.train_file is not None:
            extension = args.train_file.split(".")[-1]
            assert extension in ["csv", "json"], "`train_file` should be a csv or a json file."
        if args.validation_file is not None:
            extension = args.validation_file.split(".")[-1]
            assert extension in ["csv", "json"], "`validation_file` should be a csv or a json file."
        if args.test_file is not None:
            extension = args.test_file.split(".")[-1]
            assert extension in ["csv", "json"], "`test_file` should be a csv or a json file."
    return args


def main():
    args = parse_args()

    if args.ratio == 0:
        check_point_folder = f"../../../drive/MyDrive/CS546/ckpt/mbert/eval-lang{args.eval_lang}_lrft{args.learning_rate_fine_tune}_lrpt{args.learning_rate_fine_tune}_btachsize{args.per_device_train_batch_size}_"
    else:
        check_point_folder = f"../../../drive/MyDrive/CS546/ckpt/mbert_{args.ratio}/eval-lang{args.eval_lang}_lrft{args.learning_rate_fine_tune}_lrpt{args.learning_rate_fine_tune}_btachsize{args.per_device_train_batch_size}_"
    if args.replace_table_file is not None:
        with open(args.replace_table_file, "rb") as fr:
            aligned_tokens_table = pickle.load(fr)
    else:
        aligned_tokens_table = {}

    random_index = [0 for _ in range(args.ratio)] + [1 for _ in range(10 - args.ratio)]
    random.shuffle(random_index)


    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    # Setup logging, we only want one process per machine to log things on the screen.
    # accelerator.is_local_main_process is only True for one process per machine.


        # If passed along, set the training seed now.
    if args.seed is not None:
        set_seed(args.seed)

        # Handle the repository creation

    if args.dataset_name is not None:
        # Downloading and loading a dataset from the hub.
        raw_datasets = load_from_disk("squad_ar_5")
        valid_datasets = load_dataset("xquad", f"xquad.{args.eval_lang}")["validation"]
    else:
        data_files = {}
        if args.train_file is not None:
            data_files["train"] = args.train_file
        if args.validation_file is not None:
            data_files["validation"] = args.validation_file
        if args.test_file is not None:
            data_files["test"] = args.test_file
        extension = args.train_file.split(".")[-1]
        raw_datasets = load_dataset(extension, data_files=data_files, field="data")
    # See more about loading any type of standard or custom dataset (from files, python dict, pandas DataFrame, etc) at
    # https://huggingface.co/docs/datasets/loading_datasets.html.

    # Load pretrained model and tokenizer
    #
    # In distributed training, the .from_pretrained methods guarantee that only one local process can concurrently
    # download model & vocab.
    print("!!!!!!")
    if args.config_name:
        config = AutoConfig.from_pretrained(args.config_name)
    elif args.model_name_or_path:
        config = AutoConfig.from_pretrained(args.model_name_or_path)
    else:
        config = CONFIG_MAPPING[args.model_type]()
        logger.warning("You are instantiating a new config instance from scratch.")

    if args.tokenizer_name:
        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer_name, use_fast=True)
    elif args.model_name_or_path:
        tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path, use_fast=True)
    else:
        raise ValueError(
            "You are instantiating a new tokenizer from scratch. This is not supported by this script."
            "You can do it from another script, save it, and load it from here, using --tokenizer_name."
        )



    # Preprocessing the datasets.
    # Preprocessing is slight different for training and evaluation.
    # column_names = raw_datasets["train"].column_names
    valid_column_names = valid_datasets.column_names
    # question_column_name = "question" if "question" in column_names else column_names[0]
    # context_column_name = "context" if "context" in column_names else column_names[1]
    # answer_column_name = "answers" if "answers" in column_names else column_names[2]
    question_column_name = "question"
    context_column_name = "context"
    answer_column_name = "answers"
    # Padding side determines if we do (question|context) or (context|question).
    pad_on_right = tokenizer.padding_side == "right"

    if args.max_seq_length > tokenizer.model_max_length:
        logger.warning(
            f"The max_seq_length passed ({args.max_seq_length}) is larger than the maximum length for the"
            f"model ({tokenizer.model_max_length}). Using max_seq_length={tokenizer.model_max_length}."
        )

    max_seq_length = min(args.max_seq_length, tokenizer.model_max_length)


    # Training preprocessing
    def prepare_train_features(examples):
        # Some of the questions have lots of whitespace on the left, which is not useful and will make the
        # truncation of the context fail (the tokenized question will take a lots of space). So we remove that
        # left whitespace
        examples[question_column_name] = [q.lstrip() for q in examples[question_column_name]]
        original_questions = []
        original_contexts = []

        for idx in range(len(examples[context_column_name])):
            context = examples[context_column_name][idx]
            start = 0
            end = 0
            context_tokens = []
            context_tokens_idx = []
            is_space = context[start] == ' '
            while start < len(context):
                new_is_space = context[start] == ' '
                if new_is_space == is_space:
                    start += 1
                else:
                    sub_string = context[end:start]
                    if len(sub_string.lstrip()) > 0:
                        context_tokens.append(sub_string)
                        context_tokens_idx.append(end)
                    is_space = new_is_space
                    end = start
                    start += 1
            context_tokens.append(context[end:start])
            context_tokens_idx.append(end)

            answers_text = examples[answer_column_name][idx]["text"][0]
            answers_text_token = answers_text.split()
            answers_text = " ".join(answers_text_token)
            answers_start = examples[answer_column_name][idx]["answer_start"][0]
            answer_range = (answers_start, answers_start + len(answers_text))

            question_tokens = examples[question_column_name][idx].split()
            original_questions.append(" ".join(question_tokens))
            for token_idx in range(len(question_tokens)):
                cur_token = question_tokens[token_idx].lower().strip()
                if cur_token in aligned_tokens_table:
                    if random.choice(random_index) == 0:
                        question_tokens[token_idx] = random.choice(aligned_tokens_table[cur_token])
            examples[question_column_name][idx] = " ".join(question_tokens)

            original_contexts.append(" ".join(context_tokens))
            for token_idx in range(len(context_tokens)):
                cur_token, cur_token_idx = context_tokens[token_idx], context_tokens_idx[token_idx]
                cur_token = cur_token.lower()
                if not (answer_range[0] <= cur_token_idx <= answer_range[1]) and cur_token in aligned_tokens_table:
                    if random.choice(random_index) == 0:
                        context_tokens[token_idx] = random.choice(aligned_tokens_table[cur_token])

            examples[context_column_name][idx] = " ".join(context_tokens)
            new_answer_start = []

            if answers_text in examples[context_column_name][idx]:
                new_answer_start.append(examples[context_column_name][idx].index(answers_text))
            else:
                pass

            examples[answer_column_name][idx]["answer_start"] = new_answer_start

        # Tokenize our examples with truncation and maybe padding, but keep the overflows using a stride. This results
        # in one example possible giving several features when a context is long, each of those features having a
        # context that overlaps a bit the context of the previous feature.
        tokenized_examples = tokenizer(
            examples[question_column_name if pad_on_right else context_column_name],
            examples[context_column_name if pad_on_right else question_column_name],
            truncation="only_second" if pad_on_right else "only_first",
            max_length=max_seq_length,
            return_offsets_mapping=True,
            padding="max_length",
        )
        tokenized_original = tokenizer(
            original_questions if pad_on_right else original_contexts,
            original_contexts if pad_on_right else original_questions,
            truncation="only_second" if pad_on_right else "only_first",
            max_length=max_seq_length,
            padding="max_length",
        )
        tokenized_examples["eng_input_ids"] = tokenized_original['input_ids']
        tokenized_examples["eng_attention_mask"] = tokenized_original['attention_mask']
        tokenized_examples["eng_token_type_ids"] = tokenized_original['token_type_ids']

        # Since one example might give us several features if it has a long context, we need a map from a feature to
        # its corresponding example. This key gives us just that.
        # sample_mapping = tokenized_examples.pop("overflow_to_sample_mapping")
        # The offset mappings will give us a map from token to character position in the original context. This will
        # help us compute the start_positions and end_positions.
        offset_mapping = tokenized_examples.pop("offset_mapping")

        # Let's label those examples!
        tokenized_examples["start_positions"] = []
        tokenized_examples["end_positions"] = []

        for i, offsets in enumerate(offset_mapping):
            # We will label impossible answers with the index of the CLS token.
            input_ids = tokenized_examples["input_ids"][i]
            cls_index = input_ids.index(tokenizer.cls_token_id)
            # Grab the sequence corresponding to that example (to know what is the context and what is the question).
            sequence_ids = tokenized_examples.sequence_ids(i)
            # One example can give several spans, this is the index of the example containing this span of text.
            # sample_index = sample_mapping[i]
            answers = examples[answer_column_name][i]
            # If no answers are given, set the cls_index as answer.
            if len(answers["answer_start"]) == 0:
                tokenized_examples["start_positions"].append(cls_index)
                tokenized_examples["end_positions"].append(cls_index)
            else:
                # Start/end character index of the answer in the text.
                start_char = answers["answer_start"][0]
                end_char = start_char + len(answers["text"][0])
                # Start token index of the current span in the text.
                token_start_index = 0
                while sequence_ids[token_start_index] != (1 if pad_on_right else 0):
                    token_start_index += 1
                # End token index of the current span in the text.
                token_end_index = len(input_ids) - 1
                while sequence_ids[token_end_index] != (1 if pad_on_right else 0):
                    token_end_index -= 1
                # Detect if the answer is out of the span (in which case this feature is labeled with the CLS index).
                if not (offsets[token_start_index][0] <= start_char and offsets[token_end_index][1] >= end_char):
                    tokenized_examples["start_positions"].append(cls_index)
                    tokenized_examples["end_positions"].append(cls_index)
                else:
                    # Otherwise move the token_start_index and token_end_index to the two ends of the answer.
                    # Note: we could go after the last offset if the answer is the last word (edge case).
                    while token_start_index < len(offsets) and offsets[token_start_index][0] <= start_char:
                        token_start_index += 1
                    tokenized_examples["start_positions"].append(token_start_index - 1)
                    while offsets[token_end_index][1] >= end_char:
                        token_end_index -= 1
                    tokenized_examples["end_positions"].append(token_end_index + 1)
        return tokenized_examples


    # if "train" not in raw_datasets:
    #     raise ValueError("--do_train requires a train dataset")
    # train_dataset = raw_datasets["train"]
    train_dataset = raw_datasets
    # train_dataset.save_to_disk("squad_ar_5")

    # Create train feature from dataset

    # train_dataset = train_dataset.map(
    #     prepare_train_features,
    #     batched=True,
    #     num_proc=1,
    #     remove_columns=column_names,
    #     load_from_cache_file=not args.overwrite_cache,
    #     desc="Running tokenizer on train dataset",
    # )
    train_dataset
    if args.max_train_samples != 0:
        # Number of samples might increase during Feature Creation, We select only specified max samples
        train_dataset = train_dataset.select(range(args.max_train_samples))

    # Validation preprocessing
    def prepare_validation_features(examples):
        # Some of the questions have lots of whitespace on the left, which is not useful and will make the
        # truncation of the context fail (the tokenized question will take a lots of space). So we remove that
        # left whitespace
        examples[question_column_name] = [q.lstrip() for q in examples[question_column_name]]

        # Tokenize our examples with truncation and maybe padding, but keep the overflows using a stride. This results
        # in one example possible giving several features when a context is long, each of those features having a
        # context that overlaps a bit the context of the previous feature.
        tokenized_examples = tokenizer(
            examples[question_column_name if pad_on_right else context_column_name],
            examples[context_column_name if pad_on_right else question_column_name],
            truncation="only_second" if pad_on_right else "only_first",
            max_length=max_seq_length,
            stride=args.doc_stride,
            return_overflowing_tokens=True,
            return_offsets_mapping=True,
            padding="max_length" if args.pad_to_max_length else False,
        )

        # Since one example might give us several features if it has a long context, we need a map from a feature to
        # its corresponding example. This key gives us just that.
        sample_mapping = tokenized_examples.pop("overflow_to_sample_mapping")

        # For evaluation, we will need to convert our predictions to substrings of the context, so we keep the
        # corresponding example_id and we will store the offset mappings.
        tokenized_examples["example_id"] = []

        for i in range(len(tokenized_examples["input_ids"])):
            # Grab the sequence corresponding to that example (to know what is the context and what is the question).
            sequence_ids = tokenized_examples.sequence_ids(i)
            context_index = 1 if pad_on_right else 0

            # One example can give several spans, this is the index of the example containing this span of text.
            sample_index = sample_mapping[i]
            tokenized_examples["example_id"].append(examples["id"][sample_index])

            # Set to None the offset_mapping that are not part of the context so it's easy to determine if a token
            # position is part of the context or not.
            tokenized_examples["offset_mapping"][i] = [
                (o if sequence_ids[k] == context_index else None)
                for k, o in enumerate(tokenized_examples["offset_mapping"][i])
            ]

        return tokenized_examples

    # if "validation" not in raw_datasets:
    #     raise ValueError("--do_eval requires a validation dataset")

    eval_examples = valid_datasets
    if args.max_eval_samples != 0 :
        # We will select sample from whole data
        eval_examples = eval_examples.select(range(args.max_eval_samples))
    # Validation Feature Creation

    # with accelerator.main_process_first():

    eval_dataset = eval_examples.map(
        prepare_validation_features,
        batched=True,
        num_proc=args.preprocessing_num_workers,
        remove_columns=valid_column_names,
        load_from_cache_file=not args.overwrite_cache,
        desc="Running tokenizer on validation dataset",
    )

    if args.max_eval_samples != 0 :
        # During Feature creation dataset samples might increase, we will select required samples again
        eval_dataset = eval_dataset.select(range(args.max_eval_samples))



    # Log a few random samples from the training set:
    for index in random.sample(range(len(train_dataset)), 3):
        logger.info(f"Sample {index} of the training set: {train_dataset[index]}.")

    # DataLoaders creation:
    if args.pad_to_max_length:
        # If padding was already done ot max length, we use the default data collator that will just convert everything
        # to tensors.
        data_collator = default_data_collator
    else:
        # Otherwise, `DataCollatorWithPadding` will apply dynamic padding for us (by padding to the maximum length of
        # the samples passed). When using mixed precision, we add `pad_to_multiple_of=8` to pad all tensors to multiple
        # of 8s, which will enable the use of Tensor Cores on NVIDIA hardware with compute capability >= 7.5 (Volta).
        data_collator = DataCollatorWithPadding(tokenizer, pad_to_multiple_of=(8))

    train_dataloader = DataLoader(
        train_dataset, shuffle=True, collate_fn=data_collator, batch_size=args.per_device_train_batch_size
    )

    eval_dataset_for_model = eval_dataset.remove_columns(["example_id", "offset_mapping"])
    eval_dataloader = DataLoader(
        eval_dataset_for_model, collate_fn=data_collator, batch_size=args.per_device_eval_batch_size
    )

    # Post-processing:
    def post_processing_function(examples, features, predictions, stage="eval"):
        # Post-processing: we match the start logits and end logits to answers in the original context.
        predictions = postprocess_qa_predictions(
            examples=examples,
            features=features,
            predictions=predictions,
            version_2_with_negative=args.version_2_with_negative,
            n_best_size=args.n_best_size,
            max_answer_length=args.max_answer_length,
            null_score_diff_threshold=args.null_score_diff_threshold,
            output_dir=args.output_dir,
            prefix=stage,
        )
        # Format the result to the format the metric expects.
        if args.version_2_with_negative:
            formatted_predictions = [
                {"id": k, "prediction_text": v, "no_answer_probability": 0.0} for k, v in predictions.items()
            ]
        else:
            formatted_predictions = [{"id": k, "prediction_text": v} for k, v in predictions.items()]

        references = [{"id": ex["id"], "answers": ex[answer_column_name]} for ex in examples]
        return EvalPrediction(predictions=formatted_predictions, label_ids=references)

    metric = load_metric("squad_v2" if args.version_2_with_negative else "squad")

    # Create and fill numpy array of size len_of_validation_data * max_length_of_output_tensor
    def create_and_fill_np_array(start_or_end_logits, dataset, max_len):
        """
        Create and fill numpy array of size len_of_validation_data * max_length_of_output_tensor

        Args:
            start_or_end_logits(:obj:`tensor`):
                This is the output predictions of the model. We can only enter either start or end logits.
            eval_dataset: Evaluation dataset
            max_len(:obj:`int`):
                The maximum length of the output tensor. ( See the model.eval() part for more details )
        """

        step = 0
        # create a numpy array and fill it with -100.
        logits_concat = np.full((len(dataset), max_len), -100, dtype=np.float64)
        # Now since we have create an array now we will populate it with the outputs gathered using accelerator.gather
        for i, output_logit in enumerate(start_or_end_logits):  # populate columns
            # We have to fill it such that we have to take the whole tensor and replace it on the newly created array
            # And after every iteration we have to change the step

            batch_size = output_logit.shape[0]
            cols = output_logit.shape[1]

            if step + batch_size < len(dataset):
                logits_concat[step: step + batch_size, :cols] = output_logit
            else:
                logits_concat[step:, :cols] = output_logit[: len(dataset) - step]

            step += batch_size

        return logits_concat

    # Optimizer
    pretrained_params = []
    finetune_params = []

    # for (name, p) in model.named_parameters():
    #     if "bert" in name:
    #         pretrained_params.append(p)
    #     else:
    #         finetune_params.append(p)

    optimizer = AdamW(
        [{'params': pretrained_params, 'lr': args.learning_rate_pre_train},
         {'params': finetune_params, 'lr': args.learning_rate_fine_tune}])
    scheduler = ReverseSqrtScheduler(optimizer, [args.learning_rate_pre_train, args.learning_rate_fine_tune],
                                     args.num_warmup_steps)

    # Split weights in two groups, one with weight decay and the other not.

    # Prepare everything with our `accelerator`.


    # Note -> the training dataloader needs to be prepared before we grab his length below (cause its length will be
    # shorter in multiprocess)

    # Scheduler and math around the number of training steps.
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    else:
        args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

    # def run_eval():
    #     # Evaluation
    #     logger.info("\n***** Running Evaluation *****")
    #     logger.info(f"  Num examples = {len(eval_dataset)}")
    #     logger.info(f"  Batch size = {args.per_device_eval_batch_size}")
    #
    #     model.eval()
    #     all_start_logits = []
    #     all_end_logits = []
    #     for step, batch in enumerate(eval_dataloader):
    #         with torch.no_grad():
    #             outputs = model(**batch)
    #             start_logits = outputs.start_logits
    #             end_logits = outputs.end_logits
    #
    #             if not args.pad_to_max_length:  # necessary to pad predictions and labels for being gathered
    #                 start_logits = accelerator.pad_across_processes(start_logits, dim=1, pad_index=-100)
    #                 end_logits = accelerator.pad_across_processes(end_logits, dim=1, pad_index=-100)
    #
    #             all_start_logits.append(accelerator.gather(start_logits).cpu().numpy())
    #             all_end_logits.append(accelerator.gather(end_logits).cpu().numpy())
    #
    #     max_len = max([x.shape[1] for x in all_start_logits])  # Get the max_length of the tensor
    #
    #     # concatenate the numpy array
    #     start_logits_concat = create_and_fill_np_array(all_start_logits, eval_dataset, max_len)
    #     end_logits_concat = create_and_fill_np_array(all_end_logits, eval_dataset, max_len)
    #
    #     # delete the list of numpy arrays
    #     del all_start_logits
    #     del all_end_logits
    #
    #     outputs_numpy = (start_logits_concat, end_logits_concat)
    #     prediction = post_processing_function(eval_examples, eval_dataset, outputs_numpy)
    #     eval_metric = metric.compute(predictions=prediction.predictions, references=prediction.label_ids)
    #     logger.info(f"Evaluation metrics: {eval_metric}")
    #     with open(args.output_log_file, "a") as log_file_fr:
    #         log_file_fr.write(f"eval:-------")
    #         log_file_fr.write(f"\n Evaluation metrics: {eval_metric}")
    #         log_file_fr.write(f"train:-----")
    #
    #     model.train()
    #     f = eval_metric["f1"]
    #
    #     return f

    # Train!
    # total_batch_size = args.per_device_train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps

    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {args.per_device_train_batch_size}")
    # logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {args.max_train_steps}")

    # Only show the progress bar once on each machine.
    progress_bar = tqdm(range(args.max_train_steps))
    completed_steps = 0

    os.makedirs(os.path.dirname(args.output_log_file), exist_ok=True)
    with open(args.output_log_file, "w") as log_file_fr:
        log_file_fr.write("start\n")

    max_f1 = 0.0
    max_patience, current_patience = 3, 0
    if_exit = False
    for epoch in range(args.num_train_epochs):
        if if_exit:
            break

        # model.train()
        epoch_loss = 0
        epoch_step = 0

        for step, batch in enumerate(train_dataloader):
            if if_exit:
                break
            outputs = model(**batch)
            loss = outputs.loss
            loss = loss / args.gradient_accumulation_steps

            epoch_loss += loss.item()
            epoch_step += 1
            accelerator.backward(loss)

            if step % args.gradient_accumulation_steps == 0 or step == len(train_dataloader) - 1:
                scheduler.step_and_update_lr()
                scheduler.zero_grad()
                progress_bar.update(1)
                completed_steps += 1

            if completed_steps % args.loss_interval == 0:
                with open(args.output_log_file, "a") as log_file_fr:
                    log_file_fr.write(f"\n step :{completed_steps} loss: {epoch_loss / epoch_step}")
                print(f"\n step :{completed_steps} loss: {epoch_loss / epoch_step}")

            if args.eval_interval != -1 and completed_steps % args.eval_interval == 0:
                f = run_eval()
                if f > max_f1:
                    max_f1 = f
                    torch.save(model.state_dict(), check_point_folder + f"f1_{max_f1}.pt")
                    current_patience = 0
                else:
                    current_patience += 1
                    if current_patience > max_patience:
                        if_exit = True

            if completed_steps >= args.max_train_steps:
                break

        if args.eval_interval != -1 and completed_steps % args.eval_interval == 0:
            f = run_eval()
            if f > max_f1:
                max_f1 = f
                torch.save(model.state_dict(), check_point_folder + f"f1_{max_f1}.pt")
                print( check_point_folder + f"f1_{max_f1}.pt")
                current_patience = 0
            else:
                current_patience += 1
                if current_patience > max_patience:
                    if_exit = True
        with open(args.output_log_file, "a") as log_file_fr:
            log_file_fr.write(f"epoch {epoch} end \n")
        print(f"epoch {epoch} end \n")

    # Prediction
    if args.do_predict:
        logger.info("***** Running Prediction *****")
        logger.info(f"  Num examples = {len(predict_dataset)}")
        logger.info(f"  Batch size = {args.per_device_eval_batch_size}")

        all_start_logits = []
        all_end_logits = []
        for step, batch in enumerate(predict_dataloader):
            with torch.no_grad():
                outputs = model(**batch)
                start_logits = outputs.start_logits
                end_logits = outputs.end_logits

                if not args.pad_to_max_length:  # necessary to pad predictions and labels for being gathered
                    start_logits = accelerator.pad_across_processes(start_logits, dim=1, pad_index=-100)
                    end_logits = accelerator.pad_across_processes(start_logits, dim=1, pad_index=-100)

                all_start_logits.append(accelerator.gather(start_logits).cpu().numpy())
                all_end_logits.append(accelerator.gather(end_logits).cpu().numpy())

        max_len = max([x.shape[1] for x in all_start_logits])  # Get the max_length of the tensor
        # concatenate the numpy array
        start_logits_concat = create_and_fill_np_array(all_start_logits, predict_dataset, max_len)
        end_logits_concat = create_and_fill_np_array(all_end_logits, predict_dataset, max_len)

        # delete the list of numpy arrays
        del all_start_logits
        del all_end_logits

        outputs_numpy = (start_logits_concat, end_logits_concat)
        prediction = post_processing_function(predict_examples, predict_dataset, outputs_numpy)
        predict_metric = metric.compute(predictions=prediction.predictions, references=prediction.label_ids)
        logger.info(f"Predict metrics: {predict_metric}")

    if args.output_dir is not None:
        accelerator.wait_for_everyone()
        unwrapped_model = accelerator.unwrap_model(model)
        unwrapped_model.save_pretrained(args.output_dir, save_function=accelerator.save)
        if accelerator.is_main_process:
            tokenizer.save_pretrained(args.output_dir)


if __name__ == "__main__":
    main()
