# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description: Create Sentence-BERT model for text matching task
"""

import os
from typing import Dict, List, Union
from loguru import logger
import math
import numpy as np
import pandas as pd
import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset
from tqdm.auto import tqdm, trange
from transformers.optimization import AdamW, get_linear_schedule_with_warmup
from text2vec.sentence_model import SentenceModel, EncoderType, device
from text2vec.sentence_bert.sentencebert_dataset import SentenceBertTrainDataset, SentenceBertTestDataset
from text2vec.utils.stats_util import compute_spearmanr, compute_pearsonr, set_seed


class SentenceBertModel(SentenceModel):
    def __init__(
            self,
            model_name_or_path: str = "hfl/chinese-macbert-base",
            encoder_type: EncoderType = EncoderType.FIRST_LAST_AVG,
            max_seq_length: int = 128,
            num_classes: int = 2,
    ):
        """
        Initializes a CoSENT Model.

        Args:
            model_name_or_path: Default Transformer model name or path to a directory containing Transformer model file (pytorch_nodel.bin).
            encoder_type: EncoderType.FIRST_LAST_AVG or EncoderType.LAST_AVG or EncoderType.POOLER
            max_seq_length: The maximum total input sequence length after tokenization.
            num_classes: Number of classes for classification.
        """
        super().__init__(model_name_or_path, encoder_type, max_seq_length)
        self.fc = nn.Linear(self.model.config.hidden_size * 3, num_classes)
        self.results = {}

    def train_model(
            self,
            train_file: str,
            output_dir: str,
            eval_file: str = None,
            verbose: bool = True,
            batch_size: int = 32,
            num_epochs: int = 1,
            weight_decay: float = 0.01,
            seed: int = 42,
            warmup_ratio: float = 0.1,
            lr: float = 2e-5,
            eps: float = 1e-6,
            gradient_accumulation_steps: int = 1,
            max_grad_norm: float = 1.0,
            max_steps: int = -1
    ):
        """
        Trains the model on 'train_file'

        Args:
            train_file: Path to text file containing the text to _train the language model on.
            output_dir: The directory where model files will be saved. If not given, self.args.output_dir will be used.
            eval_file (optional): Path to eval file containing the text to _evaluate the language model on.
            verbose (optional): Print logger or not.
            batch_size (optional): Batch size for training.
            num_epochs (optional): Number of epochs for training.
            weight_decay (optional): Weight decay for optimization.
            seed (optional): Seed for initialization.
            warmup_ratio (optional): Warmup ratio for learning rate.
            lr (optional): Learning rate.
            eps (optional): Adam epsilon.
            gradient_accumulation_steps (optional): Number of updates steps to accumulate before performing a backward/update pass.
            max_grad_norm (optional): Max gradient norm.
            max_steps (optional): If > 0: set total number of training steps to perform. Override num_epochs.
        Returns:
            global_step: Number of global steps trained
            training_details: Average training loss if evaluate_during_training is False or full training progress scores if evaluate_during_training is True
        """
        train_dataset = SentenceBertTrainDataset(self.tokenizer, train_file, self.max_seq_length)

        global_step, training_details = self.train(
            train_dataset,
            output_dir,
            eval_file=eval_file,
            verbose=verbose,
            batch_size=batch_size,
            num_epochs=num_epochs,
            weight_decay=weight_decay,
            seed=seed,
            warmup_ratio=warmup_ratio,
            lr=lr,
            eps=eps,
            gradient_accumulation_steps=gradient_accumulation_steps,
            max_grad_norm=max_grad_norm,
            max_steps=max_steps
        )
        logger.info(f" Training model done. Saved to {output_dir}.")

        return global_step, training_details

    def concat_embeddings(self, source_embeddings, target_embeddings):
        """
        Output the bert sentence embeddings, pass to classifier module. Applies different
        concats and finally the linear layer to produce class scores
        :param source_embeddings:
        :param target_embeddings:
        :return: embeddings
        """
        # (u, v, |u - v|)
        embs = [source_embeddings, target_embeddings, torch.abs(source_embeddings - target_embeddings)]
        input_embs = torch.cat(embs, 1)
        # softmax
        outputs = self.fc(input_embs)
        return outputs

    def calc_loss(self, y_true, y_pred):
        """
        Calc loss with two sentence embeddings
        """
        loss = nn.CrossEntropyLoss()(y_pred, y_true)
        return loss

    def train(
            self,
            train_dataset: Dataset,
            output_dir: str,
            eval_file: str = None,
            verbose: bool = True,
            batch_size: int = 8,
            num_epochs: int = 1,
            weight_decay: float = 0.01,
            seed: int = 42,
            warmup_ratio: float = 0.1,
            lr: float = 2e-5,
            eps: float = 1e-6,
            gradient_accumulation_steps: int = 1,
            max_grad_norm: float = 1.0,
            max_steps: int = -1
    ):
        """
        Trains the model on train_dataset.

        Utility function to be used by the train_model() method. Not intended to be used directly.
        """
        os.makedirs(output_dir, exist_ok=True)
        logger.debug("Use pytorch device: {}".format(device))
        self.model.to(device)
        set_seed(seed)

        train_dataloader = DataLoader(train_dataset, shuffle=False, batch_size=batch_size)
        total_steps = len(train_dataloader) * num_epochs
        param_optimizer = list(self.model.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)],
             'weight_decay': weight_decay},
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]

        warmup_steps = math.ceil(total_steps * warmup_ratio)  # by default 10% of _train data for warm-up
        optimizer = AdamW(optimizer_grouped_parameters, lr=lr, eps=eps, correct_bias=False)
        scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=warmup_steps,
                                                    num_training_steps=total_steps)
        logger.info("***** Running training *****")
        logger.info(f"  Num examples = {len(train_dataset)}")
        logger.info(f"  Batch size = {batch_size}")
        logger.info(f"  Num steps = {total_steps}")
        logger.info(f"  Warmup-steps: {warmup_steps}")

        logger.info("  Training started")
        global_step = 0
        tr_loss, logging_loss = 0.0, 0.0
        self.model.zero_grad()
        epoch_number = 0
        best_eval_metric = 1e3
        steps_trained_in_current_epoch = 0
        epochs_trained = 0

        if self.model_name_or_path and os.path.exists(self.model_name_or_path):
            try:
                # set global_step to global_step of last saved checkpoint from model path
                checkpoint_suffix = self.model_name_or_path.split("/")[-1].split("-")
                if len(checkpoint_suffix) > 2:
                    checkpoint_suffix = checkpoint_suffix[1]
                else:
                    checkpoint_suffix = checkpoint_suffix[-1]
                global_step = int(checkpoint_suffix)
                epochs_trained = global_step // (len(train_dataloader) // gradient_accumulation_steps)
                steps_trained_in_current_epoch = global_step % (len(train_dataloader) // gradient_accumulation_steps)
                logger.info("   Continuing training from checkpoint, will skip to saved global_step")
                logger.info("   Continuing training from epoch %d" % epochs_trained)
                logger.info("   Continuing training from global step %d" % global_step)
                logger.info("   Will skip the first %d steps in the current epoch" % steps_trained_in_current_epoch)
            except ValueError:
                logger.info("   Starting fine-tuning.")

        training_progress_scores = {
            "global_step": [],
            "train_loss": [],
            "eval_spearman": [],
            "eval_pearson": [],
        }
        for current_epoch in trange(int(num_epochs), desc="Epoch", disable=False, mininterval=0):
            self.model.train()
            current_loss = 0
            if epochs_trained > 0:
                epochs_trained -= 1
                continue
            batch_iterator = tqdm(train_dataloader,
                                  desc=f"Running Epoch {epoch_number + 1} of {num_epochs}",
                                  disable=False,
                                  mininterval=0)
            for step, batch in enumerate(batch_iterator):
                if steps_trained_in_current_epoch > 0:
                    steps_trained_in_current_epoch -= 1
                    continue
                source, target, labels = batch
                # source        [batch, 1, seq_len] -> [batch, seq_len]
                source_input_ids = source.get('input_ids').squeeze(1).to(device)
                source_attention_mask = source.get('attention_mask').squeeze(1).to(device)
                source_token_type_ids = source.get('token_type_ids').squeeze(1).to(device)
                # target        [batch, 1, seq_len] -> [batch, seq_len]
                target_input_ids = target.get('input_ids').squeeze(1).to(device)
                target_attention_mask = target.get('attention_mask').squeeze(1).to(device)
                target_token_type_ids = target.get('token_type_ids').squeeze(1).to(device)
                labels = labels.to(device)

                # get sentence embeddings of BERT encoder
                source_embeddings = self.get_sentence_embeddings(
                    self.model(source_input_ids, source_attention_mask, source_token_type_ids,
                               output_hidden_states=True)
                )
                target_embeddings = self.get_sentence_embeddings(
                    self.model(target_input_ids, target_attention_mask, target_token_type_ids,
                               output_hidden_states=True)
                )
                outputs = self.concat_embeddings(source_embeddings, target_embeddings)
                loss = self.calc_loss(labels, outputs)
                current_loss = loss.item()
                if verbose:
                    batch_iterator.set_description(
                        f"Epoch: {epoch_number + 1}/{num_epochs}, Batch:{step}/{len(train_dataloader)}, Loss: {current_loss:9.4f}")

                if gradient_accumulation_steps > 1:
                    loss = loss / gradient_accumulation_steps

                loss.backward()
                tr_loss += loss.item()
                if (step + 1) % gradient_accumulation_steps == 0:
                    torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_grad_norm)
                    optimizer.step()
                    scheduler.step()  # Update learning rate schedule
                    optimizer.zero_grad()
                    global_step += 1
            epoch_number += 1
            output_dir_current = os.path.join(output_dir, "checkpoint-{}-epoch-{}".format(global_step, epoch_number))
            results = self.eval_model(eval_file, output_dir_current, verbose=verbose, batch_size=batch_size)
            self.save_model(output_dir_current, model=self.model, results=results)
            training_progress_scores["global_step"].append(global_step)
            training_progress_scores["train_loss"].append(current_loss)
            for key in results:
                training_progress_scores[key].append(results[key])
            report = pd.DataFrame(training_progress_scores)
            report.to_csv(os.path.join(output_dir, "training_progress_scores.csv"), index=False)

            eval_spearman = results["eval_spearman"]
            if eval_spearman < best_eval_metric:
                best_eval_metric = eval_spearman
                self.save_model(output_dir, model=self.model, results=results)

            if 0 < max_steps < global_step:
                return global_step, training_progress_scores

        return global_step, training_progress_scores

    def eval_model(self, eval_file: str, output_dir: str = None, verbose: bool = True, batch_size: int = 16):
        """
        Evaluates the model on eval_df. Saves results to args.output_dir
            result: Dictionary containing evaluation results.
        """
        eval_dataset = SentenceBertTestDataset(self.tokenizer, eval_file, self.max_seq_length)
        result = self.evaluate(eval_dataset, output_dir, batch_size=batch_size)
        self.results.update(result)

        if verbose:
            logger.info(self.results)

        return result

    def evaluate(self, eval_dataset, output_dir: str = None, batch_size: int = 16):
        """
        Evaluates the model on eval_dataset.

        Utility function to be used by the eval_model() method. Not intended to be used directly.
        """
        results = {}

        eval_dataloader = DataLoader(eval_dataset, batch_size=batch_size)
        self.model.to(device)
        self.model.eval()

        batch_labels = []
        batch_preds = []
        for batch in tqdm(eval_dataloader, disable=False, desc="Running Evaluation"):
            source, target, labels = batch
            labels = labels.to(device)
            batch_labels.extend(labels.cpu().numpy())
            # source        [batch, 1, seq_len] -> [batch, seq_len]
            source_input_ids = source.get('input_ids').squeeze(1).to(device)
            source_attention_mask = source.get('attention_mask').squeeze(1).to(device)
            source_token_type_ids = source.get('token_type_ids').squeeze(1).to(device)

            # target        [batch, 1, seq_len] -> [batch, seq_len]
            target_input_ids = target.get('input_ids').squeeze(1).to(device)
            target_attention_mask = target.get('attention_mask').squeeze(1).to(device)
            target_token_type_ids = target.get('token_type_ids').squeeze(1).to(device)

            with torch.no_grad():
                source_embeddings = self.get_sentence_embeddings(
                    self.model(source_input_ids, source_attention_mask, source_token_type_ids,
                               output_hidden_states=True)
                )
                target_embeddings = self.get_sentence_embeddings(
                    self.model(target_input_ids, target_attention_mask, target_token_type_ids,
                               output_hidden_states=True)
                )
                preds = torch.cosine_similarity(source_embeddings, target_embeddings)
            batch_preds.extend(preds.cpu().numpy())

        spearman = compute_spearmanr(batch_labels, batch_preds)
        pearson = compute_pearsonr(batch_labels, batch_preds)
        logger.debug(f"labels: {batch_labels[:10]}")
        logger.debug(f"preds:  {batch_preds[:10]}")
        logger.debug(f"pearson: {pearson}, spearman: {spearman}")

        results["eval_spearman"] = spearman
        results["eval_pearson"] = pearson
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            with open(os.path.join(output_dir, "eval_results.txt"), "w") as writer:
                for key in sorted(results.keys()):
                    writer.write("{} = {}\n".format(key, str(results[key])))

        return results

    def save_model(self, output_dir, model, results=None):
        """
        Saves the model to output_dir.
        :param output_dir:
        :param model:
        :param results:
        :return:
        """
        logger.info("Saving model checkpoint to %s", output_dir)
        os.makedirs(output_dir, exist_ok=True)
        model_to_save = model.module if hasattr(model, "module") else model
        model_to_save.save_pretrained(output_dir)
        self.tokenizer.save_pretrained(output_dir)
        if results:
            output_eval_file = os.path.join(output_dir, "eval_results.txt")
            with open(output_eval_file, "w") as writer:
                for key in sorted(results.keys()):
                    writer.write("{} = {}\n".format(key, str(results[key])))