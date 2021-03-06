import os
import warnings
import datetime
import argparse
import pathlib

import pdb

import numpy as np

# Ignore annoying warnings
os.environ["TF_CPP_MIN_LOG_LEVEL"]="3"
warnings.filterwarnings('ignore')

import torch
import torch.nn.functional as F

import pytorch_lightning as pl

import catalyst
import catalyst.utils
import catalyst.contrib.nn

import sklearn

import segmentation_models_pytorch as smp

# Local Imports
import tools
import lib

import logger as pll
import callbacks as plc

#-------------------------------------------------------------------------------
# Documentation

"""
# How to view tensorboard in the Lambda machine

Do the following in Lamda machine: 

    tensorboard --logdir=logs --port 6006 --host=localhost

    tensorboard --logdir=lib/logs --port 6006 --host=localhost

Then run this on the local machine

    ssh -NfL 6006:localhost:6006 edavalos@dp.stmarytx.edu

Then open this on your browser

    http://localhost:6006

To delete hanging Python processes use the following:

    killall -9 python

To delete hanging Tensorboard processes use the following:

    pkill -9 tensorboard

"""

#-------------------------------------------------------------------------------
# File Constants

# Run hyperparameters
class DEFAULT_SEG_HPARAM(argparse.Namespace):
    DATASET_NAME = 'NOCS'
    BATCH_SIZE = 4
    NUM_WORKERS = 8
    NUM_GPUS = 1
    LEARNING_RATE = 0.001
    ENCODER_LEARNING_RATE = 0.0005
    BACKBONE_ARCH = 'FPN'
    ENCODER = 'resnext50_32x4d'
    ENCODER_WEIGHTS = 'imagenet'
    NUM_EPOCHS = 3
    DISTRIBUTED_BACKEND = None if NUM_GPUS <= 1 else 'ddp'

HPARAM = DEFAULT_SEG_HPARAM()

#-------------------------------------------------------------------------------
# Classes

class SegmentationTask(pl.LightningModule):

    def __init__(self, model, criterion, metrics):
        super().__init__()

        # Saving parameters
        self.model = model

        # Saving the criterion
        self.criterion = criterion

        # Saving the metrics
        self.metrics = metrics

    def forward(self, x):
        return self.model(x)

    def training_step(self, batch, batch_idx):

        # Calculate the loss
        losses, metrics = self.shared_step('train', batch, batch_idx)
        
        # Placing the main loss into Train Result to perform backprog
        result = pl.TrainResult(minimize=losses['total_loss'])
        
        # Logging the train loss
        result.log('train_loss', losses['loss'])

        # Logging the train metrics
        result.log_dict(metrics)

        return result

    def validation_step(self, batch, batch_idx):

        # Calculate the loss
        losses, metrics = self.shared_step('valid', batch, batch_idx)
        
        # Log the batch loss inside the pl.TrainResult to visualize in the
        # progress bar
        result = pl.EvalResult(checkpoint_on=losses['total_loss'])

        # Logging the val loss
        result.log('val_loss', losses['loss'])

        # Logging the val metrics
        result.log_dict(metrics)

        return result
    
    def shared_step(self, mode, batch, batch_idx):
        
        # Forward pass the input and generate the prediction of the NN
        logits = self.model(batch['image'])
        
        # Calculate the loss based on self.loss_function
        losses, metrics = self.loss_function(logits, batch['mask'])

        # Logging the batch loss to Tensorboard
        for loss_name, loss_value in losses.items():
            self.logger.log_metrics(mode, {f'{loss_name}/batch':loss_value}, batch_idx, self)

        # Logging the metric loss to Tensorboard
        for metric_name, metric_value in metrics.items():
            self.logger.log_metrics(mode, {f'{metric_name}/batch':metric_value}, batch_idx, self) 

        return losses, metrics

    def loss_function(self, pred, gt):

        # Calculate the loss of each criterion and the metrics
        losses = {
            k: v['F'](pred, gt) for k,v in self.criterion.items()
        }
        metrics = {
            k: v(pred, gt) for k,v in self.metrics.items()
        }

        # Calculate total loss
        total_loss = torch.sum(torch.stack(list(losses.values())))

        # Calculate the loss multiplied by its corresponded weight
        weighted_losses = [losses[key] * self.criterion[key]['weight'] for key in losses.keys()]
        
        # Now calculate the weighted sum
        weighted_sum = torch.sum(torch.stack(weighted_losses))

        # Save the calculated sum in the losses
        losses['loss'] = weighted_sum

        # Saving the total loss
        losses['total_loss'] = total_loss

        return losses, metrics

    def configure_optimizers(self):

        # Since we use a pre-trained encoder, we will reduce the learning rate on it.
        layerwise_params = {"encoder*": dict(lr=HPARAM.ENCODER_LEARNING_RATE, weight_decay=0.00003)}

        # This function removes weight_decay for biases and applies our layerwise_params
        model_params = catalyst.utils.process_model_params(self.model, layerwise_params=layerwise_params)

        # Catalyst has new SOTA optimizers out of box
        base_optimizer = catalyst.contrib.nn.RAdam(model_params, lr=HPARAM.LEARNING_RATE, weight_decay=0.0003)
        optimizer = catalyst.contrib.nn.Lookahead(base_optimizer)

        # Solution from here:
        # https://github.com/PyTorchLightning/pytorch-lightning/issues/1598#issuecomment-702038244
        lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer)
        scheduler = {
            'scheduler': lr_scheduler,
            'reduce_on_plateau': True,
            'monitor': 'val_checkpoint_on',
            'patience': 2,
            'mode': 'min',
            'factor': 0.25
        }
        
        return [optimizer], [scheduler]

class SegmentationDataModule(pl.LightningDataModule):

    def __init__(self, dataset_name='CAMVID', batch_size=1, num_workers=0):
        super().__init__()

        # Saving parameters
        self.dataset_name = dataset_name
        self.batch_size = batch_size
        self.num_workers = num_workers

    def setup(self, stage=None):

        # Obtaining the preprocessing_fn depending on the encoder and the encoder
        # weights
        preprocessing_fn = smp.encoders.get_preprocessing_fn(HPARAM.ENCODER, HPARAM.ENCODER_WEIGHTS)

        # NOCS
        if self.dataset_name == 'NOCS':
            crop_size = 224
            train_dataset = tools.ds.NOCSSegDataset(
                dataset_dir=tools.pj.cfg.CAMERA_TRAIN_DATASET, 
                max_size=1000,
                classes=tools.pj.constants.NOCS_CLASSES,
                augmentation=tools.transforms.seg.get_training_augmentation(height=crop_size, width=crop_size),
                preprocessing=tools.transforms.seg.get_preprocessing(preprocessing_fn),
                balance=True,
                crop_size=crop_size,
                mask_dataformat='HW'
            )

            valid_dataset = tools.ds.NOCSSegDataset(
                dataset_dir=tools.pj.cfg.CAMERA_VALID_DATASET, 
                max_size=100,
                classes=tools.pj.constants.NOCS_CLASSES,
                augmentation=tools.transforms.seg.get_validation_augmentation(height=crop_size, width=crop_size),
                preprocessing=tools.transforms.seg.get_preprocessing(preprocessing_fn),
                balance=False,
                crop_size=crop_size,
                mask_dataformat='HW'
            )
            
            self.datasets = {'train': train_dataset,
                             'valid': valid_dataset}
        
        # VOC
        if self.dataset_name == 'VOC':
            
            train_dataset = tools.ds.VOCDataset(
                voc_dir=tools.pj.cfg.VOC_DATASET,
                is_train=True,
                classes=tools.pj.constants.VOC_CLASSES,
                augmentation=tools.transforms.seg.get_training_augmentation(),
                preprocessing=tools.transforms.seg.get_preprocessing(preprocessing_fn)
            )

            valid_dataset = tools.ds.VOCDataset(
                voc_dir=tools.pj.cfg.VOC_DATASET,
                is_train=False,
                classes=tools.pj.constants.VOC_CLASSES,
                augmentation=tools.transforms.seg.get_validation_augmentation(),
                preprocessing=tools.transforms.seg.get_preprocessing(preprocessing_fn)
            )
            
            self.datasets = {'train': train_dataset,
                             'valid': valid_dataset}

        # CAMVID
        if self.dataset_name == 'CAMVID':

            train_dataset = tools.ds.CAMVIDSegDataset(
                tools.pj.cfg.CAMVID_DATASET,
                train_valid_test='train', 
                classes=tools.pj.constants.CAMVID_CLASSES,
                augmentation=tools.transforms.seg.get_training_augmentation(), 
                preprocessing=tools.transforms.seg.get_preprocessing(preprocessing_fn),
                mask_dataformat='HW'
            )

            valid_dataset = tools.ds.CAMVIDSegDataset(
                tools.pj.cfg.CAMVID_DATASET,
                train_valid_test='val',
                classes=tools.pj.constants.CAMVID_CLASSES,
                augmentation=tools.transforms.seg.get_validation_augmentation(), 
                preprocessing=tools.transforms.seg.get_preprocessing(preprocessing_fn),
                mask_dataformat='HW'
            )

            test_dataset = tools.ds.CAMVIDSegDataset(
                tools.pj.cfg.CAMVID_DATASET,
                train_valid_test='test',
                classes=tools.pj.constants.CAMVID_CLASSES,
                augmentation=tools.transforms.seg.get_validation_augmentation(), 
                preprocessing=tools.transforms.seg.get_preprocessing(preprocessing_fn),
                mask_dataformat='HW'
            )

            test_dataset_vis = tools.ds.CAMVIDSegDataset(
                tools.pj.cfg.CAMVID_DATASET,
                train_valid_test='test',
                classes=tools.pj.constants.CAMVID_CLASSES,
                mask_dataformat='HW'
            )

            self.datasets = {'train': train_dataset,
                             'valid': valid_dataset,
                             'test': test_dataset}

        # CARVANA
        if self.dataset_name == 'CARVANA':

            train_image_path = pathlib.Path(tools.pj.cfg.CARVANA_DATASET) / 'train'
            train_mask_path = pathlib.Path(tools.pj.cfg.CARVANA_DATASET) / 'train_masks'
            test_image_path = pathlib.Path(tools.pj.cfg.CARVANA_DATASET) / 'test'

            ALL_IMAGES = sorted(train_image_path.glob("*.jpg"))
            ALL_MASKS = sorted(train_mask_path.glob("*.gif"))

            indices = np.arange(len(ALL_IMAGES))
            valid_size=0.2
            random_state = 42

            # Let's divide the data set into train and valid parts.
            train_indices, valid_indices = sklearn.model_selection.train_test_split(
                indices, test_size=valid_size, random_state=random_state, shuffle=True
            )

            np_images = np.array(ALL_IMAGES)
            np_masks = np.array(ALL_MASKS)

            # Creates our train dataset
            train_dataset = tools.ds.CARVANASegDataset(
                images = np_images[train_indices].tolist(),
                masks = np_masks[train_indices].tolist(),
                transforms = tools.transforms.seg.train_transforms
            )

            # Creates our valid dataset
            valid_dataset = tools.ds.CARVANASegDataset(
                images = np_images[valid_indices].tolist(),
                masks = np_masks[valid_indices].tolist(),
                transforms = tools.transforms.seg.valid_transforms
            )

            self.datasets = {'train': train_dataset,
                             'valid': valid_dataset}

    def get_loader(self, dataset_key):

        if dataset_key in self.datasets.keys():        
            
            dataloader = torch.utils.data.DataLoader(
                self.datasets[dataset_key],
                num_workers=self.num_workers,
                batch_size=self.batch_size,
                shuffle=True
            )
            return dataloader

        else:

            return None

    def train_dataloader(self):
        return self.get_loader('train')

    def val_dataloader(self):
        return self.get_loader('valid')

    def test_dataloader(self):
        return self.get_loader('test')

#-------------------------------------------------------------------------------
# File Main

if __name__ == '__main__':

    # Parse arguments and replace global variables if needed
    parser = argparse.ArgumentParser(description='Train with PyTorch Lightning framework')
    parser.add_argument('-d', '--DATASET_NAME', type=str, default=HPARAM.DATASET_NAME, help='Name of the dataset')
    parser.add_argument('-b', '--BATCH_SIZE', type=int, default=HPARAM.BATCH_SIZE, help='Batch size')
    parser.add_argument('-nw', '--NUM_WORKERS', type=int, default=HPARAM.NUM_WORKERS, help='Number of CPU workers')
    parser.add_argument('-ng', '--NUM_GPUS', type=int, default=HPARAM.NUM_GPUS, help='Number of GPUS')
    parser.add_argument('-e', '--NUM_EPOCHS', type=int, default=HPARAM.NUM_EPOCHS, help='Number of epochs')
    parser.add_argument('-db', '--DISTRIBUTED_BACKEND', type=str, default=HPARAM.DISTRIBUTED_BACKEND, choices=['dp','ddp','ddp_spawn','ddp2','horovod'], help='Type of distributed backend')
    parser.add_argument('-lr', '--LEARNING_RATE', type=float, default=HPARAM.LEARNING_RATE, help='Learning rate of the model')
    parser.add_argument('-elr', '--ENCODER_LEARNING_RATE', default=HPARAM.ENCODER_LEARNING_RATE, type=float, help='Encoder learning rate')
    parser.add_argument('-enc', '--ENCODER', type=str, default=HPARAM.ENCODER, help='Type of encoder')
    parser.add_argument('-bba', '--BACKBONE_ARCH', type=str, default=HPARAM.BACKBONE_ARCH, help='Type of backbone architecture')
    parser.add_argument('-ew', '--ENCODER_WEIGHTS', type=str, default=HPARAM.ENCODER_WEIGHTS, help='encoder pre-trained weights')

    # Updating the HPARAMs
    parser.parse_args(namespace=HPARAM)
    
    # Ensuring that DISTRIBUTED_BACKEND doesn't cause problems
    HPARAM.DISTRIBUTED_BACKEND = None if HPARAM.NUM_GPUS <= 1 else HPARAM.DISTRIBUTED_BACKEND

    # Creating data module
    dataset = SegmentationDataModule(
        dataset_name=HPARAM.DATASET_NAME,
        batch_size=HPARAM.BATCH_SIZE,
        num_workers=HPARAM.NUM_WORKERS
    )

    # Creating base model
    base_model = smp.__dict__[HPARAM.BACKBONE_ARCH](
        encoder_name=HPARAM.ENCODER, 
        encoder_weights=HPARAM.ENCODER_WEIGHTS, 
        classes=tools.pj.constants.NUM_CLASSES[HPARAM.DATASET_NAME]
    )

    # Selecting the criterion
    criterion = {
        'loss_ce': {'F': torch.nn.CrossEntropyLoss(), 'weight': 0.8},
        'loss_cce': {'F': lib.loss.CCE(), 'weight': 0.8},
        'loss_focal': {'F': lib.loss.Focal(), 'weight': 1.0}
    }

    # Selecting metrics
    metrics = {
        'dice': pl.metrics.functional.dice_score,
        'iou': pl.metrics.functional.iou,
        'f1': pl.metrics.functional.f1_score
    }

    # Noting what are the items that we want to see as the training develops
    tracked_data = {
        'minimize': list(criterion.keys()) + ['loss'],
        'maximize': list(metrics.keys())
    }

    # Attaching PyTorch Lightning logic to base model
    model = SegmentationTask(base_model, criterion, metrics)

    # Saving the run
    model_name = f"{HPARAM.BACKBONE_ARCH}-{HPARAM.ENCODER}-{HPARAM.ENCODER_WEIGHTS}"
    now = datetime.datetime.now().strftime('%d-%m-%y--%H-%M')
    run_name = f"seg-{now}-{HPARAM.DATASET_NAME}-{model_name}"

    # Construct hparams data to send it to MyCallback
    runs_hparams = {
        'model': model_name,
        'dataset': HPARAM.DATASET_NAME,
        'number of GPUS': HPARAM.NUM_GPUS,
        'batch size': HPARAM.BATCH_SIZE,
        'number of workers': HPARAM.NUM_WORKERS,
        'ML abs library': 'pl',
        'distributed_backend': HPARAM.DISTRIBUTED_BACKEND,
    }

    # Creating my own logger
    tb_logger = pll.MyLogger(
        HPARAM,
        pl_module=model,
        save_dir=tools.pj.cfg.LOGS,
        name=run_name
    )

    # Creating my own callback
    custom_callback = plc.MyCallback(
        task='segmentation',
        hparams=runs_hparams,
        tracked_data=tracked_data
    )

    # Training
    trainer = pl.Trainer(
        max_epochs=HPARAM.NUM_EPOCHS,
        gpus=HPARAM.NUM_GPUS,
        num_processes=HPARAM.NUM_WORKERS,
        distributed_backend=HPARAM.DISTRIBUTED_BACKEND, # required to work
        logger=tb_logger,
        callbacks=[custom_callback]
    )

    # Train
    trainer.fit(
        model,
        dataset)
