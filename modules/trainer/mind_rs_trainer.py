import copy
import math
import os
from collections import defaultdict
from pathlib import Path
import wandb
import torch
import torch.distributed
import pandas as pd
import numpy as np
from torch.utils.data import DataLoader
from tqdm import tqdm

from modules.config.configuration import Configuration
from modules.dataset import ImpressionDataset
from modules.trainer import NCTrainer
from modules.utils import (
    gather_dict,
    load_batch_data,
    get_news_embeds,
    gpu_stat,
    group_auc,
)


class MindRSTrainer(NCTrainer):
    """
    Trainer class
    """

    def __init__(self, model, config: Configuration, data_loader, **kwargs):
        super().__init__(model, config, data_loader, **kwargs)
        self.valid_interval = config.get("valid_interval", 0.6)
        self.add_l2norm = config.get("add_l2norm", False)
        self.l2_lambda = config.get("l2_lambda", 1e-7)
        self.fast_evaluation = config.get("fast_evaluation", True)
        self.log_kl_div = config.get("log_kl_div", False)
        self.topic_variant = config.get("topic_variant", "base")
        self.train_strategy = config.get("train_strategy", "pair_wise")
        self.mind_loader = data_loader
        self.behaviors = data_loader.valid_set.behaviors

    def _validation(self, epoch, batch_idx, do_monitor=True):
        # do validation when reach the interval
        self.writer.set_step(self.step, "valid")
        log = {"epoch/step": f"{epoch}/{batch_idx}"}
        val_log = self._valid_epoch(middle_name=f"valid_{epoch}_{batch_idx}")
        log.update({"val_" + k: v for k, v in val_log.items()})
        wandb.define_metric("val/*", step_metric="val/step")
        wandb.log({"val/step": self.step})
        wandb.log({"val/" + k: v for k, v in val_log.items() if v and v != 0})
        for k, v in val_log.items():
            self.writer.add_scalar(k, v)
        if do_monitor:
            self._monitor(log, epoch)
        self.model.train()  # reset to training mode
        return log

    def _train_epoch(self, epoch):
        """
        Training logic for an epoch
        :param epoch: Integer, current training epoch.
        :return: A log that contains average loss and metric in this epoch.
        """
        self.model.train()
        self.train_metrics.reset()
        bar = tqdm(
            enumerate(self.train_loader),
            total=self.len_epoch,
            disable=self.config.get("disable_tqdm", True),
        )
        # self._validation(epoch, 0)
        for batch_idx, batch_dict in bar:
            # set step for tensorboard
            self.step = (epoch - 1) * self.len_epoch + batch_idx
            self.writer.set_step(self.step)
            label = copy.deepcopy(batch_dict["label"].cpu().numpy())
            # load data to device
            batch_dict = load_batch_data(batch_dict, self.device)
            # setup model and train model
            self.optimizer.zero_grad()
            output = self.model(batch_dict)
            loss = self.criterion(output["pred"], batch_dict["label"])
            self.train_metrics.update(
                "auc", group_auc(label, output["pred"].cpu().detach().numpy())
            )
            # gpu_used = torch.cuda.memory_allocated() / 1024 ** 3
            bar_description = f"Epoch: {epoch} {gpu_stat()}"
            if self.add_l2norm:
                l2_norm = sum(p.pow(2.0).sum() for p in self.model.parameters())
                loss += self.l2_lambda * l2_norm
                self.train_metrics.update("l2_norm", l2_norm.item())
            if self.with_entropy or self.show_entropy:
                if self.entropy_mode == "static":
                    entropy_loss = self.alpha * output["entropy"]
                else:  # dynamic change based on the magnitude of entropy and loss
                    magnitude = int(np.log10((output["entropy"] / loss).cpu().item()))
                    entropy_loss = (
                        self.alpha * (1 / (10**magnitude)) * output["entropy"]
                    )
                loss += entropy_loss
                bar_description += f" Entropy(Scaled): {round(entropy_loss.item(), 4)}"
                bar_description += (
                    f" Entropy(Origin): {round(output['entropy'].item(), 4)}"
                )
                self.train_metrics.update("entropy_origin", output["entropy"].item())
                self.train_metrics.update("entropy_loss", entropy_loss.item())
            if self.topic_variant == "variational_topic":
                loss += self.beta * output["kl_divergence"]
                bar_description += f" KL divergence: {output['kl_divergence'].item()}"
            bar_description += f" Loss: {round(loss.item(), 4)}"
            self.accelerator.backward(loss)
            self.optimizer.step()
            # record loss
            self.train_metrics.update("loss", loss.item())
            if batch_idx % self.log_step == 0:
                if self.log_kl_div:
                    self.model.eval()
                    middle_name = f"train_{epoch}_{batch_idx}"
                    topic_results = self.topic_evaluator.result(
                        model=self.model, middle_name=middle_name, use_post_dict=False
                    )
                    for key, value in topic_results.items():
                        self.train_metrics.update(key, value)
                    self.model.train()
                bar.set_description(bar_description)
                train_log = self.train_metrics.result()
                wandb.define_metric("train/*", step_metric="train/step")
                wandb.log({"train/step": self.step})
                wandb.log(
                    {f"train/{k}": v for k, v in train_log.items() if v and v != 0}
                )
                self.train_metrics.reset()
            if (batch_idx + 1) % math.ceil(self.len_epoch * self.valid_interval) == 0:
                self._validation(epoch, batch_idx)
        if self.lr_scheduler is not None:
            self.lr_scheduler.step()
        log = self.train_metrics.result()
        log.update(self._validation(epoch, self.len_epoch, False))
        return log

    def _valid_epoch(self, model=None, valid_set=None, middle_name=None):
        """
        Validate after training an epoch
        :param model: evaluated model object
        :param valid_set: valid_set
        :return: A log that contains information about validation
        """
        result_dict = {}
        impression_bs = self.config.get("impression_batch_size", 128)
        valid_method = self.config.get("valid_method", "fast_evaluation")
        if torch.distributed.is_initialized():
            model = self.model.module if model is None else model.module
        else:
            model = self.model if model is None else model
        valid_set = self.mind_loader.valid_set if valid_set is None else valid_set
        model.eval()
        weight_dict = defaultdict(lambda: [])
        topic_variant = self.config.get("topic_variant", "base")
        return_weight = self.config.get("return_weight", False)

        saved_weight_num = self.config.get("saved_weight_num", 250)
        with torch.no_grad():
            try:  # try to do fast evaluation: cache news embeddings
                if (
                    valid_method == "fast_evaluation"
                    and not return_weight
                    and not self.with_entropy
                    and not topic_variant == "variational_topic"
                ):
                    news_loader = self.mind_loader.news_loader
                    news_embeds = get_news_embeds(
                        model,
                        news_loader,
                        device=self.device,
                        accelerator=self.accelerator,
                        num_processes=self.config.get("num_processes", None),
                    )
                else:
                    news_embeds = None
            except KeyError or RuntimeError:  # slow evaluation: re-calculate news embeddings every time
                news_embeds = None
            imp_set = ImpressionDataset(
                valid_set,
                news_embeds,
                selected_imp=self.config.get("selected_imp", None),
            )
            valid_loader = DataLoader(
                imp_set, impression_bs, collate_fn=self.mind_loader.fn
            )
            valid_loader = self.accelerator.prepare_data_loader(valid_loader)
            bar = tqdm(
                enumerate(valid_loader),
                total=len(valid_loader),
                disable=self.config.get("disable_tqdm", True),
            )
            for vi, batch_dict in bar:
                batch_dict = load_batch_data(batch_dict, self.device)
                label = batch_dict["label"].cpu().numpy()
                out_dict = model(batch_dict)  # run model
                pred = out_dict["pred"].cpu().numpy()
                can_len = batch_dict["candidate_length"].cpu().numpy()
                his_len = batch_dict["history_length"].cpu().numpy()
                for i in range(len(label)):
                    bar.set_description(f"Validating: {gpu_stat()}")
                    index = (
                        batch_dict["impression_index"][i].cpu().tolist()
                    )  # record impression index
                    result_dict[index] = {
                        m.__name__: m(label[i][: can_len[i]], pred[i][: can_len[i]])
                        * 100
                        for m in self.metric_funcs
                    }  # convert to percentage
                    if return_weight:
                        saved_items = {
                            "impression_index": index,
                            "results": result_dict[index],
                            "label": label[i][: can_len[i]],
                            "candidate_index": batch_dict["candidate_index"][i][
                                : can_len[i]
                            ]
                            .cpu()
                            .tolist(),
                            "history_index": batch_dict["history_index"][i][
                                : his_len[i]
                            ]
                            .cpu()
                            .numpy(),
                            "pred_score": pred[i][: can_len[i]],
                        }
                        for name, indices in saved_items.items():
                            weight_dict[name].append(indices)
                        for name, weight in out_dict.items():
                            if "weight" in name:
                                if "candidate" in name:
                                    length = can_len[i]
                                else:
                                    length = his_len[i]
                                weight_dict[name].append(
                                    weight[i][:length].cpu().numpy()
                                )
                if vi >= saved_weight_num and return_weight:
                    break
            result_dict = gather_dict(
                result_dict, num_processes=self.config.get("num_processes", None)
            )
            eval_result = dict(
                np.round(pd.DataFrame.from_dict(result_dict, orient="index").mean(), 4)
            )  # average
            if self.config.get("evaluate_topic_by_epoch", False) and self.config.get(
                "topic_evaluation_method", None
            ):
                eval_result.update(self.topic_evaluation(model, middle_name))
                # self.accelerator.wait_for_everyone()
        if return_weight and self.accelerator.is_main_process:
            weight_dir = Path(self.config["model_dir"], "weight")
            os.makedirs(weight_dir, exist_ok=True)
            weight_path = weight_dir / f"{self.config.get('head_num')}.pt"
            if weight_path.exists():
                old_weights = torch.load(weight_path)
                for key in weight_dict.keys():
                    weight_dict[key].extend(old_weights[key])
            torch.save(dict(weight_dict), weight_path)
            self.logger.info(f"Saved weight to {weight_path}")
        del batch_dict
        torch.cuda.empty_cache()
        return eval_result

    def evaluate(self, dataset, model, epoch=0, prefix="val"):
        """call this method after training"""
        model.eval()
        log = self._valid_epoch(model, dataset)
        # for (name, p,) in (model.named_parameters()):  # add histogram of model parameters to the tensorboard
        #     self.writer.add_histogram(name, p, bins="auto")
        if prefix:
            log = {f"{prefix}_{k}": v for k, v in log.items()}
        return log  # return log with prefix
