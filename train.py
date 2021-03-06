import argparse
import datetime
import glob
import os
import time
from math import ceil
from shutil import copyfile

import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.optim as optim
from tensorboardX import SummaryWriter
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data import DataLoader

from dataset import TrainData, TrainDataset, TestData
from deeplab_resnet import DeepLabv3_plus
from drn_unet import UNetDrn
from ensemble import Ensemble
from evaluate import analyze, calculate_predictions, calculate_prediction_masks, calculate_predictions_cc, \
    calculate_best_prediction_masks
from losses import LovaszLoss, RobustFocalLoss2d, SoftDiceLoss, BCELovaszLoss
from metrics import precision_batch
from models import UNetResNet
from swa_utils import moving_average, bn_update
from unet_hc import UNetResNetHc
from unet_senet import UNetSeNet
from unet_senet_hc import UNetSeNetHc
from unet_senet_hc_cat import UNetSeNetHcCat
from unet_senet_hc_ds import UNetSeNetHcDs
from unet_senet_hc_scale import UNetSeNetHcScale
from utils import get_learning_rate, write_submission

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
cudnn.benchmark = True


def str2bool(v):
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def create_model(type, input_size, pretrained, parallel):
    if type == "unet_resnet":
        model = UNetResNet(1, input_size, num_filters=32, dropout_2d=0.2, pretrained=pretrained)
    elif type == "unet_resnet_hc":
        model = UNetResNetHc(1, input_size, num_filters=32, dropout_2d=0.2, pretrained=pretrained)
    elif type == "unet_drn":
        model = UNetDrn(1, input_size, pretrained=pretrained)
    elif type == "unet_seresnet":
        model = UNetSeNet(backbone="se_resnet50", num_classes=1, input_size=input_size, pretrained=pretrained)
    elif type == "unet_seresnext":
        model = UNetSeNet(backbone="se_resnext50", num_classes=1, input_size=input_size, pretrained=pretrained)
    elif type == "unet_senet":
        model = UNetSeNet(backbone="senet154", num_classes=1, input_size=input_size, pretrained=pretrained)
    elif type == "unet_seresnext50_hc":
        model = UNetSeNetHc("se_resnext50", 1, input_size, num_filters=32, dropout_2d=0.2, pretrained=pretrained)
    elif type == "unet_seresnext50_hc_scale":
        model = UNetSeNetHcScale("se_resnext50", 1, num_filters=32, dropout_2d=0.2, pretrained=pretrained)
    elif type == "unet_seresnext101_hc":
        model = UNetSeNetHc("se_resnext101", 1, input_size, num_filters=32, dropout_2d=0.2, pretrained=pretrained)
    elif type == "unet_senet_hc":
        model = UNetSeNetHc("senet154", 1, input_size, num_filters=32, dropout_2d=0.5, pretrained=pretrained)
    elif type == "unet_seresnext_hc_cat":
        model = UNetSeNetHcCat("se_resnext50", 1, input_size, num_filters=32, dropout_2d=0.2, pretrained=pretrained)
    elif type == "unet_senet_hc_cat":
        model = UNetSeNetHcCat("senet154", 1, input_size, num_filters=32, dropout_2d=0.2, pretrained=pretrained)
    elif type == "unet_seresnext50_hc_ds":
        model = UNetSeNetHcDs("se_resnext50", 1, input_size, num_filters=32, dropout_2d=0.2, pretrained=pretrained)
    elif type == "deeplab":
        model = DeepLabv3_plus(n_classes=1, pretrained=pretrained)
    else:
        raise Exception("Unsupported model type: '{}".format(type))

    return nn.DataParallel(model) if parallel else model


def evaluate(model, data_loader, criterion):
    model.eval()

    loss_sum = 0.0
    precision_sum = 0.0
    step_count = 0

    has_salt_criterion = nn.BCEWithLogitsLoss()

    with torch.no_grad():
        for batch in data_loader:
            images, masks, mask_weights, has_salt = \
                batch[0].to(device, non_blocking=True), \
                batch[1].to(device, non_blocking=True), \
                batch[2].to(device, non_blocking=True), \
                batch[3].to(device, non_blocking=True)

            mask_prediction_logits = model(images)
            mask_predictions = torch.sigmoid(mask_prediction_logits)
            criterion.weight = mask_weights
            loss = criterion(mask_prediction_logits, masks)

            loss_sum += loss.item()
            precision_sum += np.mean(precision_batch(mask_predictions, masks))

            step_count += 1

    loss_avg = loss_sum / step_count
    precision_avg = precision_sum / step_count

    return loss_avg, precision_avg


def load_ensemble_model(ensemble_model_count, base_dir, val_set_data_loader, criterion, swa_enabled, model_type,
                        input_size, use_parallel_model):
    score_to_model = {}
    ensemble_model_candidates = glob.glob("{}/model-*.pth".format(base_dir))
    if swa_enabled and os.path.isfile("{}/swa_model.pth".format(base_dir)):
        ensemble_model_candidates.append("{}/swa_model.pth".format(base_dir))

    best_model = None
    for model_file_path in ensemble_model_candidates:
        model_file_name = os.path.basename(model_file_path)
        m = create_model(type=model_type, input_size=input_size, pretrained=False, parallel=use_parallel_model).to(
            device)
        m.load_state_dict(torch.load(model_file_path, map_location=device))
        val_loss_avg, val_precision_avg = evaluate(m, val_set_data_loader, criterion)
        print("ensemble '%s': val_loss=%.4f, val_precision=%.4f" % (model_file_name, val_loss_avg, val_precision_avg))
        if len(score_to_model) < ensemble_model_count or min(score_to_model.keys()) < val_precision_avg:
            if len(score_to_model) >= ensemble_model_count:
                del score_to_model[min(score_to_model.keys())]
            score_to_model[val_precision_avg] = m
        if best_model is None or val_precision_avg > max(score_to_model.keys()):
            best_model = m
    ensemble_models = list(score_to_model.values())

    for ensemble_model in ensemble_models:
        val_loss_avg, val_precision_avg = evaluate(ensemble_model, val_set_data_loader, criterion)
        print("ensemble: val_loss=%.4f, val_precision=%.4f" % (val_loss_avg, val_precision_avg))

    return best_model, Ensemble(ensemble_models)


def create_optimizer(type, model, lr):
    if type == "adam":
        return optim.Adam(model.parameters(), lr=lr)
    elif type == "sgd":
        return optim.SGD(model.parameters(), lr=lr, weight_decay=1e-4, momentum=0.9, nesterov=True)
    else:
        raise Exception("Unsupported optimizer type: '{}".format(type))


def main():
    args = argparser.parse_args()
    print("Arguments:")
    for arg in vars(args):
        print("  {}: {}".format(arg, getattr(args, arg)))
    print()

    input_dir = args.input_dir
    output_dir = args.output_dir
    base_model_dir = args.base_model_dir
    image_size_target = args.image_size
    batch_size = args.batch_size
    batch_iters = args.batch_iters
    num_workers = args.num_workers
    epochs_to_train = args.epochs
    max_epoch_iterations = args.max_epoch_iterations
    lr_min = args.lr_min  # 0.0001, 0.001
    lr_max = args.lr_max  # 0.001, 0.03
    lr_min_decay = args.lr_min_decay
    lr_max_decay = args.lr_max_decay
    optimizer_type = args.optimizer
    loss_type = args.loss
    bce_loss_weight = args.bce_loss_weight
    augment = args.augment
    model_type = args.model
    use_parallel_model = args.parallel_model
    pin_memory = args.pin_memory
    patience = args.patience
    sgdr_cycle_epochs = args.sgdr_cycle_epochs
    sgdr_cycle_epochs_mult = args.sgdr_cycle_epochs_mult
    sgdr_cycle_end_prolongation = args.sgdr_cycle_end_prolongation
    sgdr_cycle_end_patience = args.sgdr_cycle_end_patience
    max_sgdr_cycles = args.max_sgdr_cycles
    ensemble_model_count = args.ensemble_model_count
    swa_enabled = args.swa_enabled
    swa_epoch_to_start = args.swa_epoch_to_start
    fold_count = args.fold_count
    fold_index = args.fold_index
    train_set_scale_factor = args.train_set_scale_factor
    use_val_set = args.use_val_set
    pseudo_labeling_enabled = args.pl_enabled
    pseudo_labeling_submission_csv = args.pl_submission_csv
    pseudo_labeling_test_fold_count = args.pl_test_fold_count
    pseudo_labeling_test_fold_index = args.pl_test_fold_index
    pseudo_labeling_extend_val_set = args.pl_extend_val_set
    pseudo_labeling_loss_weight_factor = args.pl_loss_weight_factor
    submit = args.submit

    train_data = TrainData(
        input_dir,
        fold_count,
        fold_index,
        use_val_set,
        pseudo_labeling_enabled,
        pseudo_labeling_submission_csv,
        pseudo_labeling_test_fold_count,
        pseudo_labeling_test_fold_index,
        pseudo_labeling_extend_val_set)

    train_set = TrainDataset(
        train_data.train_set_df,
        image_size_target,
        augment=augment,
        train_set_scale_factor=train_set_scale_factor,
        pseudo_mask_weight_scale_factor=pseudo_labeling_loss_weight_factor)

    train_set_data_loader = \
        DataLoader(train_set, batch_size=batch_size, shuffle=True, num_workers=num_workers, pin_memory=pin_memory)

    val_set = TrainDataset(
        train_data.val_set_df,
        image_size_target,
        augment=False,
        train_set_scale_factor=1.0,
        pseudo_mask_weight_scale_factor=pseudo_labeling_loss_weight_factor)

    val_set_data_loader = \
        DataLoader(val_set, batch_size=batch_size, shuffle=False, num_workers=2, pin_memory=pin_memory)

    if base_model_dir:
        for model_file_path in glob.glob("{}/model*.pth".format(base_model_dir)):
            copyfile(model_file_path, "{}/{}".format(output_dir, os.path.basename(model_file_path)))
        model = create_model(type=model_type, input_size=image_size_target, pretrained=False,
                             parallel=use_parallel_model).to(device)
        model.load_state_dict(torch.load("{}/model.pth".format(output_dir), map_location=device))
    else:
        model = create_model(type=model_type, input_size=image_size_target, pretrained=True,
                             parallel=use_parallel_model).to(device)

    torch.save(model.state_dict(), "{}/model.pth".format(output_dir))

    swa_model = create_model(type=model_type, input_size=image_size_target, pretrained=False,
                             parallel=use_parallel_model).to(device)

    if pseudo_labeling_submission_csv:
        copyfile(pseudo_labeling_submission_csv, "{}/{}".format(output_dir, "base_pseudo_labeling_submission.csv"))

    epoch_iterations = ceil(len(train_set) / (batch_size * batch_iters))
    if max_epoch_iterations > 0:
        epoch_iterations = min(epoch_iterations, max_epoch_iterations)

    print("train_set_samples: {}, val_set_samples: {}, samples_per_epoch: {}".format(
        len(train_set),
        len(val_set),
        min(len(train_set), epoch_iterations * batch_size * batch_iters)),
        flush=True)
    print()

    global_val_precision_best_avg = float("-inf")
    global_swa_val_precision_best_avg = float("-inf")
    sgdr_cycle_val_precision_best_avg = float("-inf")

    optimizer = create_optimizer(optimizer_type, model, lr_max)
    lr_scheduler = CosineAnnealingLR(optimizer, T_max=sgdr_cycle_epochs, eta_min=lr_min)

    optim_summary_writer = SummaryWriter(log_dir="{}/logs/optim".format(output_dir))
    train_summary_writer = SummaryWriter(log_dir="{}/logs/train".format(output_dir))
    val_summary_writer = SummaryWriter(log_dir="{}/logs/val".format(output_dir))
    swa_val_summary_writer = SummaryWriter(log_dir="{}/logs/swa_val".format(output_dir))

    current_sgdr_cycle_epochs = sgdr_cycle_epochs
    sgdr_next_cycle_end_epoch = current_sgdr_cycle_epochs + sgdr_cycle_end_prolongation
    sgdr_iterations = 0
    sgdr_cycle_count = 0
    batch_count = 0
    epoch_of_last_improval = 0
    swa_update_count = 0

    ensemble_model_index = 0
    for model_file_path in glob.glob("{}/model-*.pth".format(output_dir)):
        model_file_name = os.path.basename(model_file_path)
        model_index = int(model_file_name.replace("model-", "").replace(".pth", ""))
        ensemble_model_index = max(ensemble_model_index, model_index + 1)

    print('{"chart": "best_val_precision", "axis": "epoch"}')
    print('{"chart": "val_precision", "axis": "epoch"}')
    print('{"chart": "val_loss", "axis": "epoch"}')
    print('{"chart": "sgdr_cycle", "axis": "epoch"}')
    print('{"chart": "precision", "axis": "epoch"}')
    print('{"chart": "loss", "axis": "epoch"}')
    if swa_enabled:
        print('{"chart": "swa_val_precision", "axis": "epoch"}')
        print('{"chart": "swa_val_loss", "axis": "epoch"}')
    print('{"chart": "lr_scaled", "axis": "epoch"}')

    train_start_time = time.time()

    if loss_type == "bce":
        criterion = nn.BCEWithLogitsLoss()
    elif loss_type == "lovasz":
        criterion = LovaszLoss()
    elif loss_type == "bce_lovasz":
        criterion = BCELovaszLoss(bce_weight=bce_loss_weight)
    elif loss_type == "dice":
        criterion = SoftDiceLoss()
    elif loss_type == "focal":
        criterion = RobustFocalLoss2d(gamma=1)
    else:
        raise Exception("Unsupported loss type: '{}".format(loss_type))

    # image_sizes = [64, 80, 96, 112, 128, 144, 160, 176, 192, 208, 224]

    for epoch in range(epochs_to_train):
        epoch_start_time = time.time()

        model.train()

        train_loss_sum = 0.0
        train_precision_sum = 0.0

        # image_size_target = image_sizes[min(epoch // 8, len(image_sizes) - 1)]
        # train_set.image_size_target = image_size_target
        # val_set.image_size_target = image_size_target

        train_set_data_loader_iter = iter(train_set_data_loader)

        for _ in range(epoch_iterations):
            lr_scheduler.step(epoch=min(current_sgdr_cycle_epochs, sgdr_iterations / epoch_iterations))

            optimizer.zero_grad()

            batch_loss_sum = 0.0
            batch_precision_sum = 0.0

            batch_iter_count = 0
            for _ in range(batch_iters):
                try:
                    batch = next(train_set_data_loader_iter)
                except StopIteration:
                    break

                images, masks, mask_weights, has_salt = \
                    batch[0].to(device, non_blocking=True), \
                    batch[1].to(device, non_blocking=True), \
                    batch[2].to(device, non_blocking=True), \
                    batch[3].to(device, non_blocking=True)

                mask_prediction_logits = model(images)
                criterion.weight = mask_weights
                loss = criterion(mask_prediction_logits, masks)
                loss.backward()

                with torch.no_grad():
                    batch_loss_sum += loss.item()
                    mask_predictions = torch.sigmoid(mask_prediction_logits)
                    batch_precision_sum += np.mean(precision_batch(mask_predictions, masks))

                batch_iter_count += 1

            optimizer.step()

            train_loss_sum += batch_loss_sum / batch_iter_count
            train_precision_sum += batch_precision_sum / batch_iter_count

            sgdr_iterations += 1
            batch_count += 1

            optim_summary_writer.add_scalar("lr", get_learning_rate(optimizer), batch_count + 1)

        train_loss_avg = train_loss_sum / epoch_iterations
        train_precision_avg = train_precision_sum / epoch_iterations

        if use_val_set:
            val_loss_avg, val_precision_avg = evaluate(model, val_set_data_loader, criterion)
        else:
            val_loss_avg, val_precision_avg = train_loss_avg, train_precision_avg

        model_improved_within_sgdr_cycle = val_precision_avg > sgdr_cycle_val_precision_best_avg
        if model_improved_within_sgdr_cycle:
            torch.save(model.state_dict(), "{}/model-{}.pth".format(output_dir, ensemble_model_index))
            sgdr_cycle_val_precision_best_avg = val_precision_avg

        model_improved = val_precision_avg > global_val_precision_best_avg
        ckpt_saved = False
        if model_improved:
            torch.save(model.state_dict(), "{}/model.pth".format(output_dir))
            global_val_precision_best_avg = val_precision_avg
            epoch_of_last_improval = epoch
            ckpt_saved = True

        sgdr_reset = False
        if (epoch + 1 >= sgdr_next_cycle_end_epoch) and (epoch - epoch_of_last_improval >= sgdr_cycle_end_patience):
            if swa_enabled and epoch + 1 >= swa_epoch_to_start:
                m = create_model(type=model_type, input_size=image_size_target, pretrained=False,
                                 parallel=use_parallel_model).to(device)
                m.load_state_dict(
                    torch.load("{}/model-{}.pth".format(output_dir, ensemble_model_index), map_location=device))
                swa_update_count += 1
                moving_average(swa_model, m, 1.0 / swa_update_count)
                bn_update(train_set_data_loader, swa_model)

                swa_val_loss_avg, swa_val_precision_avg = evaluate(swa_model, val_set_data_loader, criterion)

                swa_model_improved = swa_val_precision_avg > global_swa_val_precision_best_avg
                if swa_model_improved:
                    torch.save(swa_model.state_dict(), "{}/swa_model.pth".format(output_dir))
                    global_swa_val_precision_best_avg = swa_val_precision_avg

                swa_val_summary_writer.add_scalar("loss", swa_val_loss_avg, epoch + 1)
                swa_val_summary_writer.add_scalar("precision", swa_val_precision_avg, epoch + 1)

                print('{"chart": "swa_val_precision", "x": %d, "y": %.4f}' % (epoch + 1, swa_val_precision_avg))
                print('{"chart": "swa_val_loss", "x": %d, "y": %.4f}' % (epoch + 1, swa_val_loss_avg))

            sgdr_iterations = 0
            current_sgdr_cycle_epochs = int(current_sgdr_cycle_epochs * sgdr_cycle_epochs_mult)
            sgdr_next_cycle_end_epoch = epoch + 1 + current_sgdr_cycle_epochs + sgdr_cycle_end_prolongation

            ensemble_model_index += 1
            sgdr_cycle_val_precision_best_avg = float("-inf")
            sgdr_cycle_count += 1
            sgdr_reset = True

            new_lr_min = lr_min * (lr_min_decay ** sgdr_cycle_count)
            new_lr_max = lr_max * (lr_max_decay ** sgdr_cycle_count)

            optimizer = create_optimizer(optimizer_type, model, new_lr_max)
            lr_scheduler = CosineAnnealingLR(optimizer, T_max=current_sgdr_cycle_epochs, eta_min=new_lr_min)

        optim_summary_writer.add_scalar("sgdr_cycle", sgdr_cycle_count, epoch + 1)

        train_summary_writer.add_scalar("loss", train_loss_avg, epoch + 1)
        train_summary_writer.add_scalar("precision", train_precision_avg, epoch + 1)

        val_summary_writer.add_scalar("loss", val_loss_avg, epoch + 1)
        val_summary_writer.add_scalar("precision", val_precision_avg, epoch + 1)

        epoch_end_time = time.time()
        epoch_duration_time = epoch_end_time - epoch_start_time

        print(
            "[%03d/%03d] %ds, lr: %.6f, loss: %.4f, val_loss: %.4f, prec: %.4f, val_prec: %.4f, ckpt: %d, rst: %d" % (
                epoch + 1,
                epochs_to_train,
                epoch_duration_time,
                get_learning_rate(optimizer),
                train_loss_avg,
                val_loss_avg,
                train_precision_avg,
                val_precision_avg,
                int(ckpt_saved),
                int(sgdr_reset)),
            flush=True)

        print('{"chart": "best_val_precision", "x": %d, "y": %.4f}' % (epoch + 1, global_val_precision_best_avg))
        print('{"chart": "val_precision", "x": %d, "y": %.4f}' % (epoch + 1, val_precision_avg))
        print('{"chart": "val_loss", "x": %d, "y": %.4f}' % (epoch + 1, val_loss_avg))
        print('{"chart": "sgdr_cycle", "x": %d, "y": %d}' % (epoch + 1, sgdr_cycle_count))
        print('{"chart": "precision", "x": %d, "y": %.4f}' % (epoch + 1, train_precision_avg))
        print('{"chart": "loss", "x": %d, "y": %.4f}' % (epoch + 1, train_loss_avg))
        print('{"chart": "lr_scaled", "x": %d, "y": %.4f}' % (epoch + 1, 1000 * get_learning_rate(optimizer)))

        if sgdr_reset and sgdr_cycle_count >= ensemble_model_count and epoch - epoch_of_last_improval >= patience:
            print("early abort due to lack of improval")
            break

        if max_sgdr_cycles is not None and sgdr_cycle_count >= max_sgdr_cycles:
            print("early abort due to maximum number of sgdr cycles reached")
            break

    optim_summary_writer.close()
    train_summary_writer.close()
    val_summary_writer.close()

    train_end_time = time.time()
    print()
    print("Train time: %s" % str(datetime.timedelta(seconds=train_end_time - train_start_time)))

    eval_start_time = time.time()

    print()
    print("evaluation of the training model")

    if use_val_set:
        best_model, ensemble_model = load_ensemble_model(
            ensemble_model_count, output_dir, val_set_data_loader, criterion, swa_enabled, model_type,
            image_size_target, use_parallel_model=use_parallel_model)

        no_pseudo_labels_val_set_df = \
            train_data.val_set_df.drop(train_data.val_set_df.index[train_data.val_set_df.pseudo_masked]).copy()

        print()
        print("analyze validation set using ensemble model and w/ TTA")
        print()
        mask_threshold, best_mask_per_cc = analyze(ensemble_model, no_pseudo_labels_val_set_df, use_tta=True)
    else:
        best_model, ensemble_model = load_ensemble_model(
            ensemble_model_count, output_dir, train_set_data_loader, criterion, swa_enabled, model_type,
            image_size_target, use_parallel_model=use_parallel_model)

        print()
        print("analyze validation set using ensemble model and w/ TTA")
        print()
        mask_threshold, best_mask_per_cc = analyze(ensemble_model, train_data.train_set_df, use_tta=True)

    eval_end_time = time.time()
    print()
    print("Eval time: %s" % str(datetime.timedelta(seconds=eval_end_time - eval_start_time)))

    if not submit:
        return

    print()
    print("submission preparation")

    submission_start_time = time.time()

    test_data = TestData(input_dir)
    calculate_predictions(test_data.df, ensemble_model, use_tta=True)
    calculate_predictions_cc(test_data.df, mask_threshold)
    calculate_prediction_masks(test_data.df, mask_threshold)
    calculate_best_prediction_masks(test_data.df, best_mask_per_cc)

    print()
    print(test_data.df.groupby("predictions_cc").agg({"predictions_cc": "count"}))

    write_submission(test_data.df, "prediction_masks", "{}/{}".format(output_dir, "submission.csv"))
    write_submission(test_data.df, "prediction_masks_best", "{}/{}".format(output_dir, "submission_best.csv"))
    write_submission(test_data.df, "prediction_masks_best_pp", "{}/{}".format(output_dir, "submission_best_pp.csv"))

    submission_end_time = time.time()
    print()
    print("Submission time: %s" % str(datetime.timedelta(seconds=submission_end_time - submission_start_time)))


if __name__ == "__main__":
    argparser = argparse.ArgumentParser()
    argparser.add_argument("--input_dir", default="/storage/kaggle/tgs")
    argparser.add_argument("--output_dir", default="/artifacts")
    argparser.add_argument("--base_model_dir")
    argparser.add_argument("--image_size", default=128, type=int)
    argparser.add_argument("--epochs", default=500, type=int)
    argparser.add_argument("--max_epoch_iterations", default=0, type=int)
    argparser.add_argument("--batch_size", default=32, type=int)
    argparser.add_argument("--batch_iters", default=1, type=int)
    argparser.add_argument("--num_workers", default=8, type=int)
    argparser.add_argument("--lr_min", default=0.0001, type=float)
    argparser.add_argument("--lr_max", default=0.001, type=float)
    argparser.add_argument("--lr_min_decay", default=1.0, type=float)
    argparser.add_argument("--lr_max_decay", default=1.0, type=float)
    argparser.add_argument("--model", default="unet_seresnext50_hc")
    argparser.add_argument("--parallel_model", default=True, type=str2bool)
    argparser.add_argument("--pin_memory", default=False, type=str2bool)
    argparser.add_argument("--patience", default=30, type=int)
    argparser.add_argument("--optimizer", default="adam")
    argparser.add_argument("--loss", default="bce")
    argparser.add_argument("--bce_loss_weight", default=0.3, type=float)
    argparser.add_argument("--augment", default=True, type=str2bool)
    argparser.add_argument("--sgdr_cycle_epochs", default=20, type=int)
    argparser.add_argument("--sgdr_cycle_epochs_mult", default=1.0, type=float)
    argparser.add_argument("--sgdr_cycle_end_prolongation", default=3, type=int)
    argparser.add_argument("--sgdr_cycle_end_patience", default=3, type=int)
    argparser.add_argument("--max_sgdr_cycles", default=None, type=int)
    argparser.add_argument("--ensemble_model_count", default=3, type=int)
    argparser.add_argument("--swa_enabled", default=False, type=str2bool)
    argparser.add_argument("--swa_epoch_to_start", default=0, type=int)
    argparser.add_argument("--fold_count", default=5, type=int)
    argparser.add_argument("--fold_index", default=3, type=int)
    argparser.add_argument("--train_set_scale_factor", default=2.0, type=float)
    argparser.add_argument("--use_val_set", default=True, type=str2bool)
    argparser.add_argument("--pl_enabled", default=False, type=str2bool)
    argparser.add_argument("--pl_submission_csv")
    argparser.add_argument("--pl_test_fold_count", default=3, type=int)
    argparser.add_argument("--pl_test_fold_index", default=0, type=int)
    argparser.add_argument("--pl_extend_val_set", default=False, type=str2bool)
    argparser.add_argument("--pl_loss_weight_factor", default=0.6, type=float)
    argparser.add_argument("--submit", default=True, type=str2bool)

    main()
