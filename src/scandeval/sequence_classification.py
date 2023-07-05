"""Sequence classification benchmark dataset."""

import itertools as it
import logging
import random
from functools import partial
from typing import Any

import Levenshtein
import numpy as np
import torch
from datasets.arrow_dataset import Dataset
from transformers import BatchEncoding, PreTrainedModel
from transformers.data.data_collator import DataCollatorWithPadding
from transformers.modeling_utils import ModelOutput

from .benchmark_dataset import BenchmarkDataset
from .config import DatasetConfig
from .exceptions import InvalidBenchmark
from .generation import extract_raw_predictions
from .model_setups import GenerativeModel, Tokenizer
from .utils import get_special_token_metadata

logger = logging.getLogger(__package__)


class SequenceClassification(BenchmarkDataset):
    """Sequence classification benchmark dataset.

    Args:
        dataset_config (DatasetConfig):
            The dataset configuration.
        benchmark_config (BenchmarkConfig):
            The benchmark configuration.

    Attributes:
        dataset_config (DatasetConfig):
            The configuration of the dataset.
        benchmark_config (BenchmarkConfig):
            The configuration of the benchmark.
    """

    def _preprocess_data(self, dataset: Dataset, **kwargs) -> Dataset:
        """Preprocess a dataset by tokenizing and aligning the labels.

        Args:
            dataset (Hugging Face dataset):
                The dataset to preprocess.
            kwargs:
                Extra keyword arguments containing objects used in preprocessing the
                dataset.

        Returns:
            Hugging Face dataset:
                The preprocessed dataset.
        """
        tokenizer: Tokenizer = kwargs["tokenizer"]

        # Extract special token metadata from the tokenizer
        special_token_metadata = get_special_token_metadata(tokenizer=tokenizer)
        has_cls_token = special_token_metadata["has_cls_token"]
        has_sep_token = special_token_metadata["has_sep_token"]
        cls_token = special_token_metadata["cls_token"]
        sep_token = special_token_metadata["sep_token"]

        def tokenise(examples: dict) -> BatchEncoding:
            # If the tokenizer is not adding special tokens, then we add them manually.
            # We don't need this when performing few-shot evaluations, so in that case
            # we don't add the special tokens.
            if (
                not has_cls_token
                and not has_sep_token
                and cls_token is not None
                and sep_token is not None
                and kwargs["model_config"].task != "text-generation"
            ):
                examples["text"] = [
                    f"{cls_token}{doc}{sep_token}" for doc in examples["text"]
                ]

            return tokenizer(text=examples["text"], truncation=True, padding=False)

        tokenised = dataset.map(tokenise, batched=True, load_from_cache_file=False)

        if kwargs["model_config"].task != "text-generation":
            numericalise = partial(
                self._create_numerical_labels,
                label2id=kwargs["hf_model_config"].label2id,
            )
            return tokenised.map(
                numericalise, batched=True, load_from_cache_file=False
            ).remove_columns(["text"])
        else:
            return tokenised

    def _create_numerical_labels(self, examples: dict, label2id: dict) -> dict:
        try:
            examples["label"] = [label2id[lbl.lower()] for lbl in examples["label"]]
        except KeyError:
            raise InvalidBenchmark(
                f"One of the labels in the dataset, {examples['label'].lower()}, does "
                f"not occur in the label2id dictionary {label2id}."
            )
        return examples

    def _load_data_collator(
        self,
        tokenizer: Tokenizer | None = None,
        model: PreTrainedModel | GenerativeModel | None = None,
    ):
        """Load the data collator used to prepare samples during finetuning.

        Args:
            tokenizer (Tokenizer or None, optional):
                A pretrained tokenizer. Can be None if the tokenizer is not used in the
                initialisation of the data collator. Defaults to None.
            model (PreTrainedModel or GenerativeModel or None, optional):
                A pretrained model. Can be None if the model is not used in the
                initialisation of the data collator. Defaults to None.

        Returns:
            Hugging Face data collator:
                The data collator.
        """
        return DataCollatorWithPadding(tokenizer, padding="longest")

    def _compute_metrics(
        self,
        model_outputs_and_labels: tuple[list, list],
        id2label: list[str],
    ) -> dict[str, float]:
        """Compute the metrics needed for evaluation.

        Args:
            model_outputs_and_labels (pair of sequences):
                The first sequence contains the model outputs and the second sequence
                contains the true labels.
            id2label (list of str):
                Conversion of indices to labels.

        Returns:
            dict:
                A dictionary with the names of the metrics as keys and the metric
                values as values.
        """
        model_outputs, labels = model_outputs_and_labels

        model_output_dtype = np.asarray(model_outputs).dtype
        if model_output_dtype in [np.float16, np.float32, np.float64]:
            predictions = np.asarray(model_outputs).argmax(axis=-1)
        else:
            predictions = model_outputs

        prompt_label_to_label_mapping = {
            prompt_label: label
            for label, prompt_label in self.dataset_config.prompt_label_mapping.items()
        }
        predictions = [
            id2label.index(prompt_label_to_label_mapping[pred.lower()])
            if isinstance(pred, str)
            else pred
            for pred in predictions
        ]

        labels = [
            id2label.index(label.lower()) if isinstance(label, str) else label
            for label in labels
        ]

        results: dict[str, float] = dict()
        for cfg in self.dataset_config.task.metrics:
            metric = self._metrics[cfg.name]
            score_dict: dict[str, float] | None = metric.compute(
                predictions=predictions,
                references=labels,
                **cfg.compute_kwargs,
            )
            if score_dict is not None:
                scores = score_dict[cfg.results_key]
                results[cfg.name] = scores
        return results

    def _extract_few_shot_examples(
        self, train_dataset: Dataset, random_seed: int
    ) -> list[dict[str, Any]]:
        """Extract few-shot examples from the training dataset.

        Args:
            train_dataset (Hugging Face dataset):
                The training dataset.
            random_seed (int):
                The random seed to use when extracting the few-shot examples.

        Returns:
            list[dict[str, Any]]:
                The few-shot examples.
        """
        shuffled_train = train_dataset.shuffle(seed=random_seed)
        num_few_shots = self.dataset_config.num_few_shot_examples
        labels = it.cycle(self.dataset_config.task.labels)
        few_shot_examples: list[dict[str, Any]] = list()
        while len(few_shot_examples) < num_few_shots:
            label = next(labels)
            example = shuffled_train.filter(
                lambda x: x["label"].lower() == label.lower()
            ).select(range(1))[0]
            few_shot_examples.append(example)
            shuffled_train = shuffled_train.filter(
                lambda x: x["text"] != example["text"]
            )
        random.seed(random_seed)
        random.shuffle(few_shot_examples)
        return few_shot_examples

    def _apply_few_shot_prompt(
        self, examples: dict, few_shot_examples: list[dict]
    ) -> dict:
        """Apply a few-shot prompt to the examples.

        Args:
            examples (dict):
                The examples to apply the prompt to.
            few_shot_examples (list of dict):
                The examples to be included in the few-shot prompt.

        Returns:
            dict:
                The examples with the few-shot prompt applied.
        """
        # Build the few-shot part of the prompt
        label_mapping = self.dataset_config.prompt_label_mapping
        few_shot_prompts = [
            self.dataset_config.prompt_template.format(
                text=example["text"].replace("\n", " ").strip(),
                label=label_mapping[example["label"].lower()],
            )
            for example in few_shot_examples
        ]
        prompt_prefix = ""
        if self.dataset_config.prompt_prefix:
            prompt_prefix = self.dataset_config.prompt_prefix + "\n\n"
        few_shot_prompt = prompt_prefix + "\n\n".join(few_shot_prompts)

        # Add the texts from the examples to the prompts. We remove newlines from the
        # examples as they have the special function to separate the few-shot examples
        # from one another
        new_prompts = [
            self.dataset_config.prompt_template.format(
                text=text.replace("\n", " ").strip(), label=""
            ).strip()
            for text in examples["text"]
        ]

        examples["text"] = [
            few_shot_prompt + "\n\n" + new_prompt for new_prompt in new_prompts
        ]

        return examples

    def _extract_labels_from_generation(
        self,
        input_batch: dict[str, list],
        model_output: ModelOutput,
        tokenizer: Tokenizer,
    ) -> list[Any]:
        """Extract the predicted labels from the generated output.

        Args:
            input_batch (dict):
                The input batch, where the keys are the feature names and the values
                are lists with the feature values.
            model_output (ModelOutput):
                The raw generated output of the model.
            tokenizer (Tokenizer):
                The tokenizer used together with the model.

        Returns:
            list:
                The predicted labels.
        """
        if "scores" in model_output:
            return get_closest_logprobs_labels(
                generation_logprobs=model_output["scores"],
                tokenizer=tokenizer,
                dataset_config=self.dataset_config,
            )
        else:
            return get_closest_word_edit_labels(
                generated_sequences=model_output["sequences"],
                tokenizer=tokenizer,
                dataset_config=self.dataset_config,
            )


def get_closest_logprobs_labels(
    generation_logprobs: tuple[torch.Tensor],
    tokenizer: Tokenizer,
    dataset_config: DatasetConfig,
) -> list[str]:
    """Get the labels with the highest predicted logprob value.

    In case a candidate label is split into multiple tokens, we only use the first
    token to compute the logprob value. E.g., if the candidate label "positive" is
    tokenised as ["pos", "itive"], we only use the logprob value of "pos" to
    represent the logprob value of the entire label.

    Args:
        generation_logprobs (tuple[torch.Tensor]):
            The logprobs of the generated tokens.
        tokenizer (Tokenizer):
            The tokenizer used to generate the tokens.
        dataset_config (DatasetConfig):
            The configuration of the dataset.
        benchmark_config (BenchmarkConfig):
            The configuration of the benchmark.

    Returns:
        list[str]:
            The predicted labels.
    """
    candidate_labels = [
        dataset_config.prompt_label_mapping[lbl] for lbl in dataset_config.id2label
    ]

    # Shape: [batch_size, num_generated_tokens, vocab_size]
    all_logprobs = torch.stack(generation_logprobs, dim=1)

    # Shape: [batch_size, num_candidate_labels]
    pred_logprobs = torch.empty(
        all_logprobs.shape[0], len(candidate_labels), device=all_logprobs.device
    )

    for idx, candidate_label in enumerate(candidate_labels):
        # We only use the first token to represent the logprob value of the entire
        # label.
        candidate_label_ids: list[list[int]] = tokenizer(
            [candidate_label.lower()], add_special_tokens=False
        )["input_ids"]
        candidate_label_id: int = candidate_label_ids[0][0]
        pred_logprobs[:, idx] = all_logprobs[:, 0, candidate_label_id]

    # Shape: [batch_size,]
    predicted_label_ids = pred_logprobs.argmax(dim=1)

    return [candidate_labels[idx] for idx in predicted_label_ids]


def get_closest_word_edit_labels(
    generated_sequences: list[list[int]],
    tokenizer: Tokenizer,
    dataset_config: DatasetConfig,
) -> list[str]:
    """Get the labels with the smallest edit distance to the predicted labels.

    Args:
        generated_sequences (list of list of int):
            The generated sequences from the model. The outer-most list is the
            batch dimension, the inner-most list is the sequence dimension,
            consisting of token IDs.
        tokenizer (Tokenizer):
            The tokenizer used to generate the tokens.
        dataset_config (DatasetConfig):
            The configuration of the dataset.

    Returns:
        list of str:
            The candidate labels with the smallest edit distance to the predicted
            labels.
    """
    raw_predictions = extract_raw_predictions(
        generated_sequences=generated_sequences,
        tokenizer=tokenizer,
        dataset_config=dataset_config,
    )

    candidate_labels = dataset_config.id2label
    new_predicted_labels: list[str] = list()
    for predicted_label in raw_predictions:
        edit_distances = [
            Levenshtein.distance(s1=predicted_label.lower(), s2=candidate_label.lower())
            for candidate_label in candidate_labels
        ]
        closest_label = candidate_labels[np.argmin(edit_distances).item()]
        new_predicted_labels.append(closest_label)
    return new_predicted_labels
