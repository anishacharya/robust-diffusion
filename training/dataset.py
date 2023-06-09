# Copyright (c) 2022, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# This work is licensed under a Creative Commons
# Attribution-NonCommercial-ShareAlike 4.0 International License.
# You should have received a copy of the license along with this
# work. If not, see http://creativecommons.org/licenses/by-nc-sa/4.0/

"""Streaming images and labels from datasets created with dataset_tool.py."""

import os
import numpy as np
import zipfile
import PIL.Image
import json
import torch
import dnnlib
from torch_utils.ambient_diffusion import get_box_mask, get_patch_mask, get_hat_patch_mask
from dnnlib.util import create_down_up_matrix, sample_ratio

try:
    import pyspng
except ImportError:
    pyspng = None


# Abstract base class for datasets.
class Dataset(torch.utils.data.Dataset):
    def __init__(self,
        name,                   # Name of the dataset.
        raw_shape,              # Shape of the raw image data (NCHW).
        max_size    = None,     # Artificially limit the size of the dataset. None = no limit. Applied before xflip.
        use_labels  = False,    # Enable conditioning labels? False = label dimension is zero.
        xflip       = False,    # Artificially double the size of the dataset via x-flips. Applied after max_size.
        random_seed = 0,        # Random seed to use when applying max_size.
        cache       = False,    # Cache images in CPU memory?
        corruption_probability = 0.,   # Probability to corrupt a single image.
        delta_probability = 0.,  # Probability to corrupt further an already corrupted image.
        mask_full_rgb = False,
        corruption_pattern = "dust",
        ratios = [1.0, 0.8, 0.6, 0.4, 0.2, 0.1],  # potential down-sampling ratios,
        normalize=True
    ):
        assert corruption_pattern in ["dust", "box", "fixed_box", "keep_patch"], \
            "corruption_pattern must be either 'dust', 'box', 'keep_patch', or 'fixed_box'"
        self._name = name
        self._raw_shape = list(raw_shape)
        self._use_labels = use_labels
        self._cache = cache
        self._cached_images = dict() # {raw_idx: np.ndarray, ...}
        self._raw_labels = None
        self._label_shape = None
        self.corruption_probability = corruption_probability
        self.delta_probability = delta_probability
        self.mask_full_rgb = mask_full_rgb
        self.corruption_pattern = corruption_pattern
        self.ratios = ratios
        self.normalize = normalize

        # Apply max_size.
        self._raw_idx = np.arange(self._raw_shape[0], dtype=np.int64)
        if (max_size is not None) and (self._raw_idx.size > max_size):
            np.random.RandomState(random_seed % (1 << 31)).shuffle(self._raw_idx)
            self._raw_idx = np.sort(self._raw_idx[:max_size])

        # Apply xflip.
        self._xflip = np.zeros(self._raw_idx.size, dtype=np.uint8)
        if xflip:
            self._raw_idx = np.tile(self._raw_idx, 2)
            self._xflip = np.concatenate([self._xflip, np.ones_like(self._xflip)])

        # select indices to corrupt
        self.num_corrupted_samples = int(self.corruption_probability * len(self._raw_idx))
        self.corrupted_indices = np.random.choice(a=self._raw_idx, size=num_corrupted_samples, replace=False)

    def _get_raw_labels(self):
        if self._raw_labels is None:
            self._raw_labels = self._load_raw_labels() if self._use_labels else None
            if self._raw_labels is None:
                self._raw_labels = np.zeros([self._raw_shape[0], 0], dtype=np.float32)
            assert isinstance(self._raw_labels, np.ndarray)
            assert self._raw_labels.shape[0] == self._raw_shape[0]
            assert self._raw_labels.dtype in [np.float32, np.int64]
            if self._raw_labels.dtype == np.int64:
                assert self._raw_labels.ndim == 1
                assert np.all(self._raw_labels >= 0)
        return self._raw_labels

    def close(self):  # to be overridden by subclass
        pass

    def _load_raw_image(self, raw_idx):  # to be overridden by subclass
        raise NotImplementedError

    def _load_raw_labels(self):  # to be overridden by subclass
        raise NotImplementedError

    def __getstate__(self):
        return dict(self.__dict__, _raw_labels=None)

    def __del__(self):
        try:
            self.close()
        except:
            pass

    def __len__(self):
        return self._raw_idx.size

    def __getitem__(self, idx):
        raw_idx = self._raw_idx[idx]
        image = self._cached_images.get(raw_idx, None)
        if image is None:
            image = self._load_raw_image(raw_idx)
            if self._cache:
                self._cached_images[raw_idx] = image
        assert isinstance(image, np.ndarray)
        assert list(image.shape) == self.image_shape
        assert image.dtype == np.uint8
        if self._xflip[idx]:
            assert image.ndim == 3  # CHW
            image = image[:, :, ::-1]

        # get array that masks each pixel with probability self.corruption_probability
        # with fixed seed for reproducibility
        np.random.seed(raw_idx)
        torch.manual_seed(raw_idx)
        if self.normalize:
            image = image.astype(np.float32) / 127.5 - 1

        # Apply Corruption iff it is part of the corrupted indices
        if raw_idx in self.corrupted_indices:
            if self.corruption_pattern == "dust":
                if self.mask_full_rgb:
                    corruption_mask = np.random.binomial(1, 1 - self.corruption_probability, size=image.shape[1:]).astype(np.float32)
                    corruption_mask = corruption_mask[np.newaxis, :, :].repeat(image.shape[0], axis=0)
                    extra_mask = np.random.binomial(1, 1 - self.delta_probability, size=image.shape[1:]).astype(np.float32)
                    extra_mask = extra_mask[np.newaxis, :, :].repeat(image.shape[0], axis=0)
                    hat_corruption_mask = np.minimum(corruption_mask, extra_mask)
                else:
                    corruption_mask = np.random.binomial(1, 1 - self.corruption_probability, size=image.shape).astype(np.float32)
                    hat_corruption_mask = np.minimum(corruption_mask, np.random.binomial(1, 1 - self.delta_probability, size=image.shape).astype(np.float32))

            elif self.corruption_pattern == "box":
                corruption_mask = get_box_mask((1,) + image.shape, 1 - self.corruption_probability, same_for_all_batch=False, device='cpu')[0]
                hat_corruption_mask = get_box_mask((1,) + image.shape, 1 - self.corruption_probability, same_for_all_batch=False, device='cpu')[0]
                hat_corruption_mask = corruption_mask * hat_corruption_mask

            elif self.corruption_pattern == "fixed_box":
                patch_size = int((self.corruption_probability) * image.shape[-2])
                corruption_mask = 1 - get_patch_mask((1,) + image.shape, patch_size, same_for_all_batch=False, device='cpu')[0]
                if self.delta_probability > 0:
                    hat_corruption_mask = 1 - get_patch_mask((1,) + image.shape, patch_size, same_for_all_batch=False, device='cpu')[0]
                    hat_corruption_mask = corruption_mask * hat_corruption_mask
                else:
                    hat_corruption_mask = corruption_mask

            elif self.corruption_pattern == "keep_patch":
                patch_size = int((1 - self.corruption_probability) * image.shape[-2])
                corruption_mask = get_patch_mask((1,) + image.shape, patch_size, same_for_all_batch=False, device='cpu')
                hat_patch_size =  int((1 - self.delta_probability) * patch_size)
                hat_corruption_mask = get_hat_patch_mask(corruption_mask, patch_size, hat_patch_size, same_for_all_batch=False, device='cpu')[0]
                corruption_mask = corruption_mask[0]

            else:
                raise NotImplementedError("Corruption pattern not implemented")

            return image.copy(), self.get_label(idx), corruption_mask, hat_corruption_mask

    def get_label(self, idx):
        label = self._get_raw_labels()[self._raw_idx[idx]]
        if label.dtype == np.int64:
            onehot = np.zeros(self.label_shape, dtype=np.float32)
            onehot[label] = 1
            label = onehot
        return label.copy()

    def get_details(self, idx):
        d = dnnlib.EasyDict()
        d.raw_idx = int(self._raw_idx[idx])
        d.xflip = (int(self._xflip[idx]) != 0)
        d.raw_label = self._get_raw_labels()[d.raw_idx].copy()
        return d

    @property
    def name(self):
        return self._name

    @property
    def image_shape(self):
        return list(self._raw_shape[1:])

    @property
    def num_channels(self):
        assert len(self.image_shape) == 3 # CHW
        return self.image_shape[0]

    @property
    def resolution(self):
        assert len(self.image_shape) == 3 # CHW
        assert self.image_shape[1] == self.image_shape[2]
        return self.image_shape[1]

    @property
    def label_shape(self):
        if self._label_shape is None:
            raw_labels = self._get_raw_labels()
            if raw_labels.dtype == np.int64:
                self._label_shape = [int(np.max(raw_labels)) + 1]
            else:
                self._label_shape = raw_labels.shape[1:]
        return list(self._label_shape)

    @property
    def label_dim(self):
        assert len(self.label_shape) == 1
        return self.label_shape[0]

    @property
    def has_labels(self):
        return any(x != 0 for x in self.label_shape)

    @property
    def has_onehot_labels(self):
        return self._get_raw_labels().dtype == np.int64


# Dataset subclass that loads images recursively from the specified directory
# or ZIP file.
class ImageFolderDataset(Dataset):
    def __init__(self,
        path,                   # Path to directory or zip.
        resolution      = None, # Ensure specific resolution, None = highest available.
        use_pyspng      = True, # Use pyspng if available?
        **super_kwargs,         # Additional arguments for the Dataset base class.
    ):
        self._path = path
        self._use_pyspng = use_pyspng
        self._zipfile = None

        if os.path.isdir(self._path):
            self._type = 'dir'
            self._all_fnames = {os.path.relpath(os.path.join(root, fname), start=self._path) for root, _dirs, files in os.walk(self._path) for fname in files}
        elif self._file_ext(self._path) == '.zip':
            self._type = 'zip'
            self._all_fnames = set(self._get_zipfile().namelist())
        else:
            raise IOError('Path must point to a directory or zip')

        PIL.Image.init()
        self._image_fnames = sorted(fname for fname in self._all_fnames if self._file_ext(fname) in PIL.Image.EXTENSION)
        if len(self._image_fnames) == 0:
            raise IOError('No image files found in the specified path')

        name = os.path.splitext(os.path.basename(self._path))[0]
        raw_shape = [len(self._image_fnames)] + list(self._load_raw_image(0).shape)
        if resolution is not None and (raw_shape[2] != resolution or raw_shape[3] != resolution):
            raise IOError('Image files do not match the specified resolution')
        super().__init__(name=name, raw_shape=raw_shape, **super_kwargs)

    @staticmethod
    def _file_ext(fname):
        return os.path.splitext(fname)[1].lower()

    def _get_zipfile(self):
        assert self._type == 'zip'
        if self._zipfile is None:
            self._zipfile = zipfile.ZipFile(self._path)
        return self._zipfile

    def _open_file(self, fname):
        if self._type == 'dir':
            return open(os.path.join(self._path, fname), 'rb')
        if self._type == 'zip':
            return self._get_zipfile().open(fname, 'r')
        return None

    def close(self):
        try:
            if self._zipfile is not None:
                self._zipfile.close()
        finally:
            self._zipfile = None

    def __getstate__(self):
        return dict(super().__getstate__(), _zipfile=None)

    def _load_raw_image(self, raw_idx):
        fname = self._image_fnames[raw_idx]
        with self._open_file(fname) as f:
            if self._use_pyspng and pyspng is not None and self._file_ext(fname) == '.png':
                image = pyspng.load(f.read())
            else:
                image = np.array(PIL.Image.open(f))
        if image.ndim == 2:
            image = image[:, :, np.newaxis] # HW => HWC
        image = image.transpose(2, 0, 1) # HWC => CHW
        return image

    def _load_raw_labels(self):
        fname = 'dataset.json'
        if fname not in self._all_fnames:
            return None
        with self._open_file(fname) as f:
            labels = json.load(f)['labels']
        if labels is None:
            return None
        labels = dict(labels)
        labels = [labels[fname.replace('\\', '/')] for fname in self._image_fnames]
        labels = np.array(labels)
        labels = labels.astype({1: np.int64, 2: np.float32}[labels.ndim])
        return labels

