"""Training script for the ASR model."""
import os
from typing import TypedDict

import click
import torch
import torch.nn.functional as F
from tokenizers import Tokenizer
from torch import nn, optim
from torch.utils.data import DataLoader

from swr2_asr.model_deep_speech import SpeechRecognitionModel
from swr2_asr.tokenizer import train_bpe_tokenizer
from swr2_asr.utils import MLSDataset, Split

from .loss_scores import cer, wer


class HParams(TypedDict):
    """Type for the hyperparameters of the model."""

    n_cnn_layers: int
    n_rnn_layers: int
    rnn_dim: int
    n_class: int
    n_feats: int
    stride: int
    dropout: float
    learning_rate: float
    batch_size: int
    epochs: int


def greedy_decoder(output, labels, label_lengths, blank_label=28, collapse_repeated=True):
    """Greedily decode a sequence."""
    arg_maxes = torch.argmax(output, dim=2)  # pylint: disable=no-member
    decodes = []
    targets = []
    for i, args in enumerate(arg_maxes):
        decode = []
        targets.append(text_transform.decode([int(x) for x in labels[i][: label_lengths[i]].tolist()]))
        for j, index in enumerate(args):
            if index != blank_label:
                if collapse_repeated and j != 0 and index == args[j - 1]:
                    continue
                decode.append(index.item())
        decodes.append(text_transform.decode(decode))
    return decodes, targets


class IterMeter:
    """keeps track of total iterations"""

    def __init__(self):
        self.val = 0

    def step(self):
        """step"""
        self.val += 1

    def get(self):
        """get"""
        return self.val


def train(
    model,
    device,
    train_loader,
    criterion,
    optimizer,
    scheduler,
    epoch,
    iter_meter,
):
    """Train"""
    model.train()
    data_len = len(train_loader.dataset)
    for batch_idx, _data in enumerate(train_loader):
        _, spectrograms, input_lengths, labels, label_lengths, *_ = _data
        spectrograms, labels = spectrograms.to(device), labels.to(device)
        optimizer.zero_grad()

        output = model(spectrograms)  # (batch, time, n_class)
        output = F.log_softmax(output, dim=2)
        output = output.transpose(0, 1)  # (time, batch, n_class)

        loss = criterion(output, labels, input_lengths, label_lengths)
        loss.backward()

        optimizer.step()
        scheduler.step()
        iter_meter.step()
        if batch_idx % 100 == 0 or batch_idx == data_len:
            print(
                f"Train Epoch: \
                    {epoch} \
                    [{batch_idx * len(spectrograms)}/{data_len} \
                    ({100.0 * batch_idx / len(train_loader)}%)]\t \
                    Loss: {loss.item()}"
            )
        return loss.item()


def test(model, device, test_loader, criterion):
    """Test"""
    print("\nevaluating...")
    model.eval()
    test_loss = 0
    test_cer, test_wer = [], []
    with torch.no_grad():
        for _data in test_loader:
            spectrograms, labels, input_lengths, label_lengths = _data
            spectrograms, labels = spectrograms.to(device), labels.to(device)

            output = model(spectrograms)  # (batch, time, n_class)
            output = F.log_softmax(output, dim=2)
            output = output.transpose(0, 1)  # (time, batch, n_class)

            loss = criterion(output, labels, input_lengths, label_lengths)
            test_loss += loss.item() / len(test_loader)

            decoded_preds, decoded_targets = greedy_decoder(output.transpose(0, 1), labels, label_lengths)
            for j, pred in enumerate(decoded_preds):
                test_cer.append(cer(decoded_targets[j], pred))
                test_wer.append(wer(decoded_targets[j], pred))

    avg_cer = sum(test_cer) / len(test_cer)
    avg_wer = sum(test_wer) / len(test_wer)

    print(
        f"Test set: Average loss:\
            {test_loss}, Average CER: {avg_cer} Average WER: {avg_wer}\n"
    )


def run(
    learning_rate: float,
    batch_size: int,
    epochs: int,
    load: bool,
    path: str,
    dataset_path: str,
    language: str,
) -> None:
    """Runs the training script."""
    use_cuda = torch.cuda.is_available()
    torch.manual_seed(42)
    device = torch.device("cuda" if use_cuda else "cpu")  # pylint: disable=no-member
    # device = torch.device("mps")

    # load dataset
    train_dataset = MLSDataset(dataset_path, language, Split.train, download=True)
    valid_dataset = MLSDataset(dataset_path, language, Split.valid, download=True)
    test_dataset = MLSDataset(dataset_path, language, Split.test, download=True)

    # TODO: add flag to choose tokenizer
    # load tokenizer (bpe by default):
    if not os.path.isfile("data/tokenizers/bpe_tokenizer_german_3000.json"):
        print("There is no tokenizer available. Do you want to train it on the dataset?")
        input("Press Enter to continue...")
        train_bpe_tokenizer(
            dataset_path=dataset_path,
            language=language,
            split="all",
            download=False,
            out_path="data/tokenizers/bpe_tokenizer_german_3000.json",
            vocab_size=3000,
        )

    tokenizer = Tokenizer.from_file("data/tokenizers/bpe_tokenizer_german_3000.json")

    train_dataset.set_tokenizer(tokenizer)
    valid_dataset.set_tokenizer(tokenizer)
    test_dataset.set_tokenizer(tokenizer)

    hparams = HParams(
        n_cnn_layers=3,
        n_rnn_layers=5,
        rnn_dim=512,
        n_class=tokenizer.get_vocab_size(),
        n_feats=128,
        stride=2,
        dropout=0.1,
        learning_rate=learning_rate,
        batch_size=batch_size,
        epochs=epochs,
    )

    train_loader = DataLoader(
        train_dataset,
        batch_size=hparams["batch_size"],
        shuffle=True,
    )

    valid_loader = DataLoader(
        valid_dataset,
        batch_size=hparams["batch_size"],
        shuffle=True,
    )

    # enable flag to find the most compatible algorithms in advance
    if use_cuda:
        torch.backends.cudnn.benchmark = True

    model = SpeechRecognitionModel(
        hparams["n_cnn_layers"],
        hparams["n_rnn_layers"],
        hparams["rnn_dim"],
        hparams["n_class"],
        hparams["n_feats"],
        hparams["stride"],
        hparams["dropout"],
    ).to(device)

    print("Num Model Parameters", sum((param.nelement() for param in model.parameters())))
    optimizer = optim.AdamW(model.parameters(), hparams["learning_rate"])
    criterion = nn.CTCLoss(blank=28).to(device)
    if load:
        checkpoint = torch.load(path)
        model.load_state_dict(checkpoint["model_state_dict"])
        optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        epoch = checkpoint["epoch"]
        loss = checkpoint["loss"]
    scheduler = optim.lr_scheduler.OneCycleLR(
        optimizer,
        max_lr=hparams["learning_rate"],
        steps_per_epoch=int(len(train_loader)),
        epochs=hparams["epochs"],
        anneal_strategy="linear",
    )

    iter_meter = IterMeter()
    for epoch in range(1, epochs + 1):
        loss = train(
            model,
            device,
            train_loader,
            criterion,
            optimizer,
            scheduler,
            epoch,
            iter_meter,
        )

        test(model=model, device=device, test_loader=valid_loader, criterion=criterion)
        print("saving epoch", str(epoch))
        torch.save(
            {"epoch": epoch, "model_state_dict": model.state_dict(), "loss": loss},
            path + str(epoch),
        )


@click.command()
@click.option("--learning_rate", default=1e-3, help="Learning rate")
@click.option("--batch_size", default=10, help="Batch size")
@click.option("--epochs", default=1, help="Number of epochs")
@click.option("--load", default=False, help="Do you want to load a model?")
@click.option(
    "--path",
    default="model",
    help="Path where the model will be saved to/loaded from",
)
@click.option(
    "--dataset_path",
    default="data/",
    help="Path for the dataset directory",
)
def run_cli(
    learning_rate: float,
    batch_size: int,
    epochs: int,
    load: bool,
    path: str,
    dataset_path: str,
) -> None:
    """Runs the training script."""

    run(
        learning_rate=learning_rate,
        batch_size=batch_size,
        epochs=epochs,
        load=load,
        path=path,
        dataset_path=dataset_path,
        language="mls_german_opus",
    )
