"""
train.py
    defines command line arguments, training procedure
    should import from model.py and loader.py
    models should save in the following structure:
    {modality}-{task}/
        models/
            {experiment_type}/
                {timestamp in s}/
                    args.json
                    log.txt
                    val{val_loss}_train{train_loss}_epoch{epoch_num}
                    ...
                ...
            ...
"""
import os, sys

from pathlib import Path
sys.path.append(str(Path(__file__).absolute().parent.parent))

import argparse, time, json

import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim import lr_scheduler

from tensorboardX import SummaryWriter

from datetime import datetime
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

import psutil

from data.loader import load_data
from models import *
from utils_toy import transform_data, getPredMetrics

optimizers = {'adam': optim.Adam, 'rmsprop': optim.RMSprop}
seq_models = ['max', 'mean', 'lstm']
def get_parser():
    parser = argparse.ArgumentParser()

    # Experiment Parameters
    parser.add_argument('--datadir', type=str, required=True)
    parser.add_argument('--rundir', type=str, required=True)
    parser.add_argument('--comment', type=str, required=False)
    parser.add_argument('--view', default='sagittal', type=str)
    parser.add_argument('--multilabel', action='store_true')
    parser.add_argument('--no_fours', action='store_true')
    parser.add_argument('--seed', default=123, type=int)
    # Model Parameters
    parser.add_argument('--model', default='alexnet', type=str)
    parser.add_argument('--pretrained', action="store_true")
    parser.add_argument('--weighted_loss', action="store_true")
    parser.add_argument('--seq', type=str)
    parser.add_argument('--hidden_dim', type=int)
    # Training Parameters
    parser.add_argument('--optimizer', default='adam', type=str)
    parser.add_argument('--learning_rate', default=0.001, type=float)
    parser.add_argument('--weight_decay', default=0.01, type=float)
    parser.add_argument('--dropout', default=0.000, type=float)
    parser.add_argument('--batch_size', default=1, type=int)
    parser.add_argument('--epochs', default=30, type=int)
    parser.add_argument('--max_patience', default=5, type=int)
    parser.add_argument('--verbose', action="store_false")
    parser.add_argument('--plot', action="store_true")
    parser.add_argument('--factor', default=0.3, type=float)
    # Data Loading Parameters
    parser.add_argument('--toy', action="store_true")
    parser.add_argument('--rgb', action="store_true")
    parser.add_argument('--workers', default=8, type=int)
    parser.add_argument('--extension', default="npy", type=str)
    parser.add_argument('--fix_num_slices', action="store_true")
    parser.add_argument('--num_slices', default=35, type=int) # should be ignored if fix_num_slices = False
    parser.add_argument('--fixing_method', default='inner', type=str) # should be ignored if fix_num_slices = False
    parser.add_argument('--normalize', default='none', type=str)
    # Data Augmentation Parameters
    parser.add_argument('--scale', default=256, type=int)
    parser.add_argument('--horizontal_flip', action="store_true")
    parser.add_argument('--rotate', default=0, type=int)
    parser.add_argument('--shift', default=0, type=int)
    parser.add_argument('--reverse', action="store_true")

    return parser


def run_model(model, loader, criterion, writer=None, epoch=None,
              train=False, optimizer=None, verbose=False, seq=None,
              multilabel=False):


    if train:
        model.train()
    else:
        model.eval()

    avg_loss = 0.0
    num_batches = 0
    complete_predictions = None
    # TODO: cleaner way to get all labels?
    complete_labels = None
    
    for batch in loader:
     
        if train:
            optimizer.zero_grad()

        if seq == 'lstm':
            model.hidden = model.init_hidden()

        img_dict, labels = transform_data(batch, True, train) # use_gpu = True
        inputs = img_dict[loader.dataset.view]
        # print(inputs.size()) # [1, num_slices, 3, 256, 256]
        predictions = model.forward(inputs.float())

        if complete_predictions is None:
            complete_predictions = predictions
            complete_labels = labels
        else:
            complete_predictions = torch.cat((complete_predictions, predictions))
            complete_labels = torch.cat((complete_labels, labels))
        # print("predictions shape: {}".format(predictions.size()))
        # BCEWithLogitsLoss averages over the batch by default.
        batch_loss = criterion(predictions, labels)
        avg_loss += batch_loss

        if train: 
            if verbose:
                print(f"Training batch loss: {batch_loss.data[0]:0.4f}")
            batch_loss.backward()
            optimizer.step()
        # print(psutil.virtual_memory())
        num_batches += 1

    avg_loss /= num_batches
    
    
    return avg_loss.data[0]


def train(args, writer=None):
    # Initialize data loaders
    train_loader, valid_loader, test_loader, rad_loader = load_data(args)

    # Initialize desired model
    if args.model not in model_dict:
        raise ValueError(f"{args.model} model not supported")

    #if args.seq not in seq_models:
    #    raise ValueError(f"{args.seq} sequential model not supported")
    if args.model == 'lrcn' or args.model == 'mtolstm':
        args.seq = 'lstm'
    
    assert train_loader.dataset.num_classes == \
           valid_loader.dataset.num_classes == \
           test_loader.dataset.num_classes, \
           "Different number of classes in data splits"

    if args.model == 'lrcn':
        model = model_dict[args.model](args, train_loader.dataset.num_classes, args.hidden_dim, args.dropout)
    else:
        model = model_dict[args.model](args, train_loader.dataset.num_classes)
    print('num classes: ', train_loader.dataset.num_classes)
    model = model.cuda()
    # Initialize loss function
    if args.weighted_loss:
        # NOTE: think about weighted loss here - during validation,
        # should we use weights computed from training or validation?
        train_criterion = train_loader.dataset.weighted_loss
        valid_criterion = valid_loader.dataset.weighted_loss
    elif args.multilabel:
        train_criterion = nn.MultiLabelSoftMarginLoss()
        valid_criterion = nn.MultiLabelSoftMarginLoss()
    else:
        train_criterion = nn.BCEWithLogitsLoss()
        valid_criterion = nn.BCEWithLogitsLoss()

    # Initialize optimizer and learning rate annealer
    if args.optimizer not in optimizers:
        raise ValueError(f"{args.optimizer} optimizer not supported")
    optimizer = optimizers[args.optimizer](model.parameters(),
                                           args.learning_rate,
                                           weight_decay=args.weight_decay)
    
    scheduler = lr_scheduler.ReduceLROnPlateau(optimizer,
                                               patience=args.max_patience,
                                               factor=args.factor,
                                               threshold=1e-4) # changed from 1e-3 to 1e-4"""
    # Training loop
    train_losses = []
    valid_losses = []
    epochs = []

    best_val_loss = float('Inf')

    start_time = datetime.now()
    for epoch in range(args.epochs):
        change = datetime.now() - start_time
        print("Starting epoch {}. Time passed: {}".format(epoch, str(change)))
        train_loss = run_model(model, train_loader, train_criterion, epoch=epoch, writer=writer,
                               train=True, optimizer=optimizer, verbose=args.verbose, seq=args.seq,
                               multilabel=args.multilabel)
        print(f"Average training loss {train_loss:0.4f}")

        val_loss = run_model(model, valid_loader, valid_criterion, epoch=epoch, writer=writer, seq=args.seq,
                             multilabel=args.multilabel)

        print(f"Average validation loss {val_loss:0.4f}")
        
        train_losses.append(train_loss)
        valid_losses.append(val_loss)
        epochs.append(epoch)
        

        if args.plot:
            
            plot_dir = Path(args.rundir) / "plots"
            plt.plot(epochs, train_losses, label="train")
            plt.plot(epochs, valid_losses, label="valid")
            plt.legend()
            plt.savefig(str(plot_dir / "loss"))
            plt.close()

        scheduler.step(val_loss)

        if val_loss < best_val_loss:
            best_val_loss = val_loss

        file_name = f'val{val_loss}_train{train_loss}_epoch{epoch+1}'
        save_path = Path(args.rundir) / file_name
        torch.save(model.state_dict(), save_path)
                

if __name__ == "__main__":

    args = get_parser().parse_args()
    args.verbose = False
    
    np.random.seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    torch.manual_seed(args.seed)
    
    with open(Path(args.rundir) / "args.json", 'w') as out:
        json.dump(vars(args), out, indent=4)

    if args.plot:
        plot_dir = Path(args.rundir) / "plots"
        plot_dir.mkdir(exist_ok=True)
        
    comment = args.comment if args.comment else ("_" + str(os.path.basename(os.path.normpath(args.rundir))))
        
    writer = SummaryWriter(comment=comment)
    
    #if args.multilabel:
    #    raise ValueError("Multilabel not yet implemented for training")
    
    if args.view == 'all':
        raise ValueError("Multiview not yet implemented for train script")
    
    train(args, writer)