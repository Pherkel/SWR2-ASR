"""Class containing utils for the ASR system."""
import os
from enum import Enum
from multiprocessing import Pool
from typing import TypedDict

import numpy as np
import torch
import torchaudio
from tokenizers import Tokenizer
from torch.utils.data import Dataset
from tqdm import tqdm
import audio_metadata

from swr2_asr.tokenizer import CharTokenizer, TokenizerType

train_audio_transforms = torch.nn.Sequential(
    torchaudio.transforms.MelSpectrogram(sample_rate=16000, n_mels=128),
    torchaudio.transforms.FrequencyMasking(freq_mask_param=30),
    torchaudio.transforms.TimeMasking(time_mask_param=100),
)


# create enum specifiying dataset splits
class MLSSplit(str, Enum):
    """Enum specifying dataset as they are defined in the
    Multilingual LibriSpeech dataset"""

    train = "train"
    test = "test"
    dev = "dev"


class Split(str, Enum):
    """Extending the MLSSplit class to allow for a custom validatio split"""

    train = "train"
    valid = "valid"
    test = "test"
    dev = "dev"


def split_to_mls_split(split: Split) -> MLSSplit:
    """Converts the custom split to a MLSSplit"""
    if split == Split.valid:
        return MLSSplit.train
    else:
        return split  # type: ignore


class Sample(TypedDict):
    """Type for a sample in the dataset"""

    waveform: torch.Tensor
    spectrogram: torch.Tensor
    input_length: int
    utterance: torch.Tensor
    utterance_length: int
    sample_rate: int
    speaker_id: str
    book_id: str
    chapter_id: str


class MLSDataset(Dataset):
    """Custom Dataset for reading Multilingual LibriSpeech

    Attributes:
        dataset_path (str):
            path to the dataset
        language (str):
            language of the dataset
        split (Split):
            split of the dataset
        mls_split (MLSSplit):
            split of the dataset as defined in the Multilingual LibriSpeech dataset
        dataset_lookup (list):
            list of dicts containing the speakerid, bookid, chapterid and utterance

    directory structure:
        <dataset_path>
        ├── <language>
        │  ├── train
        │  │  ├── transcripts.txt
        │  │  └── audio
        │  │     └── <speakerid>
        │  │        └── <bookid>
        │  │           └── <speakerid>_<bookid>_<chapterid>.opus / .flac

        each line in transcripts.txt has the following format:
        <speakerid>_<bookid>_<chapterid> <utterance>
    """

    def __init__(self, dataset_path: str, language: str, split: Split, download: bool):
        """Initializes the dataset"""
        self.dataset_path = dataset_path
        self.language = language
        self.file_ext = ".opus" if "opus" in language else ".flac"
        self.mls_split: MLSSplit = split_to_mls_split(split)  # split path on disk
        self.split: Split = split  # split used internally
        self.dataset_lookup = []
        self.tokenizer: type[TokenizerType]

        self._handle_download_dataset(download)
        self._validate_local_directory()

        transcripts_path = os.path.join(dataset_path, language, self.mls_split, "transcripts.txt")

        with open(transcripts_path, "r", encoding="utf-8") as script_file:
            # read all lines in transcripts.txt
            transcripts = script_file.readlines()
            # split each line into (<speakerid>_<bookid>_<chapterid>, <utterance>)
            transcripts = [line.strip().split("\t", 1) for line in transcripts]  # type: ignore
            utterances = [utterance.strip() for _, utterance in transcripts]  # type: ignore
            identifier = [identifier.strip() for identifier, _ in transcripts]  # type: ignore
            identifier = [path.split("_") for path in identifier]

            if self.split == Split.valid:
                np.random.seed(42)
                indices = np.random.choice(len(utterances), int(len(utterances) * 0.2))
                utterances = [utterances[i] for i in indices]
                identifier = [identifier[i] for i in indices]
            elif self.split == Split.train:
                np.random.seed(42)
                indices = np.random.choice(len(utterances), int(len(utterances) * 0.8))
                utterances = [utterances[i] for i in indices]
                identifier = [identifier[i] for i in indices]

            self.dataset_lookup = [
                {
                    "speakerid": path[0],
                    "bookid": path[1],
                    "chapterid": path[2],
                    "utterance": utterance,
                }
                for path, utterance in zip(identifier, utterances, strict=False)
            ]

        self.max_spec_length = 0
        self.max_utterance_length = 0

    def set_tokenizer(self, tokenizer: type[TokenizerType]):
        """Sets the tokenizer"""
        self.tokenizer = tokenizer
        # self.calc_paddings()

    def _handle_download_dataset(self, download: bool):
        """Download the dataset"""
        if not os.path.exists(os.path.join(self.dataset_path, self.language)) and download:
            os.makedirs(self.dataset_path)
            url = f"https://dl.fbaipublicfiles.com/mls/{self.language}.tar.gz"

            torch.hub.download_url_to_file(url, self.dataset_path)
        elif not os.path.exists(os.path.join(self.dataset_path, self.language)) and not download:
            raise ValueError("Dataset not found. Set download to True to download it")

    def _validate_local_directory(self):
        # check if dataset_path exists
        if not os.path.exists(self.dataset_path):
            raise ValueError("Dataset path does not exist")
        if not os.path.exists(os.path.join(self.dataset_path, self.language)):
            raise ValueError("Language not found in dataset")
        if not os.path.exists(os.path.join(self.dataset_path, self.language, self.mls_split)):
            raise ValueError("Split not found in dataset")

    def _calculate_max_length(self, chunk):
        """Calculates the maximum length of the spectrogram and the utterance

        to be called in a multiprocessing pool
        """
        max_spec_length = 0
        max_utterance_length = 0

        for sample in chunk:
            audio_path = os.path.join(
                self.dataset_path,
                self.language,
                self.mls_split,
                "audio",
                sample["speakerid"],
                sample["bookid"],
                "_".join(
                    [
                        sample["speakerid"],
                        sample["bookid"],
                        sample["chapterid"],
                    ]
                )
                + self.file_ext,
            )
            metadata = audio_metadata.load(audio_path)
            audio_duration = metadata.streaminfo.duration
            sample_rate = metadata.streaminfo.sample_rate

            max_spec_length = int(max(max_spec_length, (audio_duration * sample_rate) // 200))
            max_utterance_length = max(max_utterance_length, len(self.tokenizer.encode(sample["utterance"]).ids))

        return max_spec_length, max_utterance_length

    def calc_paddings(self) -> None:
        """Sets the maximum length of the spectrogram and the utterance"""
        # check if dataset has been loaded and tokenizer has been set
        if not self.dataset_lookup:
            raise ValueError("Dataset not loaded")
        if not self.tokenizer:
            raise ValueError("Tokenizer not set")
        # check if paddings have been calculated already
        if os.path.isfile(os.path.join(self.dataset_path, self.language, self.mls_split, "paddings.txt")):
            print("Paddings already calculated")
            with open(os.path.join(self.dataset_path, self.language, self.mls_split, "paddings.txt"), "r") as f:
                self.max_spec_length, self.max_utterance_length = [int(line.strip()) for line in f.readlines()]
            return
        else:
            print("Calculating paddings...")

        thread_count = os.cpu_count()
        if thread_count is None:
            thread_count = 4
        chunk_size = len(self.dataset_lookup) // thread_count
        chunks = [self.dataset_lookup[i : i + chunk_size] for i in range(0, len(self.dataset_lookup), chunk_size)]

        with Pool(thread_count) as p:
            results = list(p.imap(self._calculate_max_length, chunks))

        for spec, utterance in results:
            self.max_spec_length = max(self.max_spec_length, spec)
            self.max_utterance_length = max(self.max_utterance_length, utterance)

        # write to file
        with open(os.path.join(self.dataset_path, self.language, self.mls_split, "paddings.txt"), "w") as f:
            f.write(f"{self.max_spec_length}\n")
            f.write(f"{self.max_utterance_length}")

    def __len__(self):
        """Returns the length of the dataset"""
        return len(self.dataset_lookup)

    def __getitem__(self, idx: int) -> Sample:
        """One sample"""
        # get the utterance
        utterance = self.dataset_lookup[idx]["utterance"]

        # get the audio file
        audio_path = os.path.join(
            self.dataset_path,
            self.language,
            self.mls_split,
            "audio",
            self.dataset_lookup[idx]["speakerid"],
            self.dataset_lookup[idx]["bookid"],
            "_".join(
                [
                    self.dataset_lookup[idx]["speakerid"],
                    self.dataset_lookup[idx]["bookid"],
                    self.dataset_lookup[idx]["chapterid"],
                ]
            )
            + self.file_ext,
        )

        waveform, sample_rate = torchaudio.load(audio_path)  # type: ignore

        # resample if necessary
        if sample_rate != 16000:
            resampler = torchaudio.transforms.Resample(sample_rate, 16000)
            waveform = resampler(waveform)
            sample_rate = 16000

        spec = torchaudio.transforms.MelSpectrogram(sample_rate=16000, n_mels=128)(waveform).squeeze(0).transpose(0, 1)

        input_length = spec.shape[0] // 2
        utterance_length = len(utterance)

        self.tokenizer.enable_padding()
        utterance = self.tokenizer.encode(
            utterance,
        ).ids

        utterance = torch.Tensor(utterance)

        return Sample(
            # TODO: add flag to only return spectrogram or waveform or both
            waveform=waveform,
            spectrogram=spec,
            input_length=input_length,
            utterance=utterance,
            utterance_length=utterance_length,
            sample_rate=sample_rate,
            speaker_id=self.dataset_lookup[idx]["speakerid"],
            book_id=self.dataset_lookup[idx]["bookid"],
            chapter_id=self.dataset_lookup[idx]["chapterid"],
        )

    def download(self, dataset_path: str, language: str):
        """Download the dataset"""
        os.makedirs(dataset_path)
        url = f"https://dl.fbaipublicfiles.com/mls/{language}.tar.gz"

        torch.hub.download_url_to_file(url, dataset_path)


if __name__ == "__main__":
    dataset_path = "/Volumes/pherkel/SWR2-ASR"
    language = "mls_german_opus"
    split = Split.train
    download = False

    dataset = MLSDataset(dataset_path, language, split, download)

    tok = Tokenizer.from_file("data/tokenizers/bpe_tokenizer_german_3000.json")
    dataset.set_tokenizer(tok)
    dataset.calc_paddings()

    print(f"Spectrogram shape: {dataset[41]['spectrogram'].shape}")
    print(f"Utterance shape: {dataset[41]['utterance'].shape}")
