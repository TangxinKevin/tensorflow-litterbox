# Copyright (C) 2016 Ross Wightman. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
# ==============================================================================
# Based on original Work Copyright (c) 2016, Brendan Shillingford. All rights reserved.
#
#     https://github.com/bshillingford/python-torchfile/
# ==============================================================================
"""
Mostly direct port of the Lua and C serialization implementation to 
Python, depending only on `struct`, `array`, and numpy.

Supported types:
 * `nil` to Python `None`
 * numbers to Python floats, or by default a heuristic changes them to ints or
   longs if they are integral
 * booleans
 * strings: read as byte strings (Python 3) or normal strings (Python 2), like
   lua strings which don't support unicode, and that can contain null chars
 * tables converted to a special dict (*); if they are list-like (i.e. have
   numeric keys from 1 through n) they become a python list by default
 * Torch classes: supports Tensors and Storages, and most classes such as 
   modules. Trivially extensible much like the Torch serialization code.
   Trivial torch classes like most `nn.Module` subclasses become 
   `TorchObject`s. The `torch_readers` dict contains the mapping from class
   names to reading functions.
 * functions: loaded into the `LuaFunction` `namedtuple`,
   which simply wraps the raw serialized data, i.e. upvalues and code.
   These are mostly useless, but exist so you can deserialize anything.

(*) Since Lua allows you to index a table with a table but Python does not, we 
    replace dicts with a subclass that is hashable, and change its
    equality comparison behaviour to compare by reference.
    See `hashable_uniq_dict`.

Currently, the implementation assumes the system-dependent binary Torch 
format, but minor refactoring can give support for the ascii format as well.
"""

TYPE_NIL = 0
TYPE_NUMBER = 1
TYPE_STRING = 2
TYPE_TABLE = 3
TYPE_TORCH = 4
TYPE_BOOLEAN = 5
TYPE_FUNCTION = 6
TYPE_RECUR_FUNCTION = 8
LEGACY_TYPE_RECUR_FUNCTION = 7

import struct
from array import array
import numpy as np
import sys
from collections import namedtuple

LuaFunction = namedtuple('LuaFunction', ['size', 'dumped', 'upvalues'])


class hashable_uniq_dict(dict):
    """
    Subclass of dict with equality and hashing semantics changed:
    equality and hashing is purely by reference/instance, to match
    the behaviour of lua tables.

    Supports lua-style dot indexing.

    This way, dicts can be keys of other dicts.
    """

    def __hash__(self):
        return id(self)

    def __getattr__(self, key):
        return self.get(key)

    def __eq__(self, other):
        return id(self) == id(other)
    # TODO: dict's __lt__ etc. still exist

torch_readers = {}


def add_tensor_reader(typename, dtype):
    def read_tensor_generic(reader, version):
        # source:
        # https://github.com/torch/torch7/blob/master/generic/Tensor.c#L1243
        ndim = reader.read_int()

        # read size:
        size = reader.read_long_array(ndim)
        # read stride:
        stride = reader.read_long_array(ndim)
        # storage offset:
        storage_offset = reader.read_long() - 1
        # read storage:
        storage = reader.read_obj()

        if storage is None or ndim == 0 or len(size) == 0 or len(stride) == 0:
            # empty torch tensor
            return np.empty((0), dtype=dtype)

        # convert stride to numpy style (i.e. in bytes)
        stride = [storage.dtype.itemsize * x for x in stride]

        # create numpy array that indexes into the storage:
        return np.lib.stride_tricks.as_strided(
            storage[storage_offset:],
            shape=size,
            strides=stride)
    torch_readers[typename] = read_tensor_generic
add_tensor_reader('torch.ByteTensor', dtype=np.uint8)
add_tensor_reader('torch.CharTensor', dtype=np.int8)
add_tensor_reader('torch.ShortTensor', dtype=np.int16)
add_tensor_reader('torch.IntTensor', dtype=np.int32)
add_tensor_reader('torch.LongTensor', dtype=np.int64)
add_tensor_reader('torch.FloatTensor', dtype=np.float32)
add_tensor_reader('torch.DoubleTensor', dtype=np.float64)
add_tensor_reader('torch.CudaTensor', np.float32)  # float
add_tensor_reader('torch.CudaByteTensor', dtype=np.uint8)
add_tensor_reader('torch.CudaCharTensor', dtype=np.int8)
add_tensor_reader('torch.CudaShortTensor', dtype=np.int16)
add_tensor_reader('torch.CudaIntTensor', dtype=np.int32)
add_tensor_reader('torch.CudaDoubleTensor', dtype=np.float64)


def add_storage_reader(typename, dtype):
    def read_storage(reader, version):
        # source:
        # https://github.com/torch/torch7/blob/master/generic/Storage.c#L244
        size = reader.read_long()
        return np.fromfile(reader.f, dtype=dtype, count=size)
    torch_readers[typename] = read_storage
add_storage_reader('torch.ByteStorage', dtype=np.uint8)
add_storage_reader('torch.CharStorage', dtype=np.int8)
add_storage_reader('torch.ShortStorage', dtype=np.int16)
add_storage_reader('torch.IntStorage', dtype=np.int32)
add_storage_reader('torch.LongStorage', dtype=np.int64)
add_storage_reader('torch.FloatStorage', dtype=np.float32)
add_storage_reader('torch.DoubleStorage', dtype=np.float64)
add_storage_reader('torch.CudaStorage', dtype=np.float32)  # float
add_storage_reader('torch.CudaByteStorage', dtype=np.uint8)
add_storage_reader('torch.CudaCharStorage', dtype=np.int8)
add_storage_reader('torch.CudaShortStorage', dtype=np.int16)
add_storage_reader('torch.CudaIntStorage', dtype=np.int32)
add_storage_reader('torch.CudaDoubleStorage', dtype=np.float64)


class TorchObject(object):
    """
    Simple torch object, used by `add_trivial_class_reader`.
    Supports both forms of lua-style indexing, i.e. getattr and getitem.
    Use the `torch_typename` method to get the object's torch class name.

    Equality is by reference, as usual for lua (and the default for Python
    objects).
    """

    def __init__(self, typename, obj):
        self._typename = typename
        self._obj = obj

    def __getattr__(self, k):
        return self._obj.get(k)

    def __getitem__(self, k):
        return self._obj.get(k)

    def torch_typename(self):
        return self._typename

    def __repr__(self):
        return "TorchObject(%s, %s)" % (self._typename, repr(self._obj))

    def __str__(self):
        return repr(self)

    def __dir__(self):
        keys = list(self._obj.keys())
        keys.append('torch_typename')
        return keys


def add_trivial_class_reader(typename):
    def reader(reader, version):
        obj = reader.read_obj()
        return TorchObject(typename, obj)
    torch_readers[typename] = reader
for mod in ["nn.ConcatTable", "nn.SpatialAveragePooling",
            "nn.TemporalConvolutionFB", "nn.BCECriterion", "nn.Reshape", "nn.gModule",
            "nn.SparseLinear", "nn.WeightedLookupTable", "nn.CAddTable",
            "nn.TemporalConvolution", "nn.PairwiseDistance", "nn.WeightedMSECriterion",
            "nn.SmoothL1Criterion", "nn.TemporalSubSampling", "nn.TanhShrink",
            "nn.MixtureTable", "nn.Mul", "nn.LogSoftMax", "nn.Min", "nn.Exp", "nn.Add",
            "nn.BatchNormalization", "nn.AbsCriterion", "nn.MultiCriterion",
            "nn.LookupTableGPU", "nn.Max", "nn.MulConstant", "nn.NarrowTable", "nn.View",
            "nn.ClassNLLCriterionWithUNK", "nn.VolumetricConvolution",
            "nn.SpatialSubSampling", "nn.HardTanh", "nn.DistKLDivCriterion",
            "nn.SplitTable", "nn.DotProduct", "nn.HingeEmbeddingCriterion",
            "nn.SpatialBatchNormalization", "nn.DepthConcat", "nn.Sigmoid",
            "nn.SpatialAdaptiveMaxPooling", "nn.Parallel", "nn.SoftShrink",
            "nn.SpatialSubtractiveNormalization", "nn.TrueNLLCriterion", "nn.Log",
            "nn.SpatialDropout", "nn.LeakyReLU", "nn.VolumetricMaxPooling",
            "nn.KMaxPooling", "nn.Linear", "nn.Euclidean", "nn.CriterionTable",
            "nn.SpatialMaxPooling", "nn.TemporalKMaxPooling", "nn.MultiMarginCriterion",
            "nn.ELU", "nn.CSubTable", "nn.MultiLabelMarginCriterion", "nn.Copy",
            "nn.CuBLASWrapper", "nn.L1HingeEmbeddingCriterion",
            "nn.VolumetricAveragePooling", "nn.StochasticGradient",
            "nn.SpatialContrastiveNormalization", "nn.CosineEmbeddingCriterion",
            "nn.CachingLookupTable", "nn.FeatureLPPooling", "nn.Padding", "nn.Container",
            "nn.MarginRankingCriterion", "nn.Module", "nn.ParallelCriterion",
            "nn.DataParallelTable", "nn.Concat", "nn.CrossEntropyCriterion",
            "nn.LookupTable", "nn.SpatialSoftMax", "nn.HardShrink", "nn.Abs", "nn.SoftMin",
            "nn.WeightedEuclidean", "nn.Replicate", "nn.DataParallel",
            "nn.OneBitQuantization", "nn.OneBitDataParallel", "nn.AddConstant", "nn.L1Cost",
            "nn.HSM", "nn.PReLU", "nn.JoinTable", "nn.ClassNLLCriterion", "nn.CMul",
            "nn.CosineDistance", "nn.Index", "nn.Mean", "nn.FFTWrapper", "nn.Dropout",
            "nn.SpatialConvolutionCuFFT", "nn.SoftPlus", "nn.AbstractParallel",
            "nn.SequentialCriterion", "nn.LocallyConnected",
            "nn.SpatialDivisiveNormalization", "nn.L1Penalty", "nn.Threshold", "nn.Power",
            "nn.Sqrt", "nn.MM", "nn.GroupKMaxPooling", "nn.CrossMapNormalization",
            "nn.ReLU", "nn.ClassHierarchicalNLLCriterion", "nn.Optim", "nn.SoftMax",
            "nn.SpatialConvolutionMM", "nn.Cosine", "nn.Clamp", "nn.CMulTable",
            "nn.LogSigmoid", "nn.LinearNB", "nn.TemporalMaxPooling", "nn.MSECriterion",
            "nn.Sum", "nn.SoftSign", "nn.Normalize", "nn.ParallelTable", "nn.FlattenTable",
            "nn.CDivTable", "nn.Tanh", "nn.ModuleFromCriterion", "nn.Square", "nn.Select",
            "nn.GradientReversal", "nn.SpatialFullConvolutionMap", "nn.SpatialConvolution",
            "nn.Criterion", "nn.SpatialConvolutionMap", "nn.SpatialLPPooling",
            "nn.Sequential", "nn.Transpose", "nn.SpatialUpSamplingNearest",
            "nn.SpatialFullConvolution", "nn.ModelParallel", "nn.RReLU",
            "nn.SpatialZeroPadding", "nn.Identity", "nn.Narrow", "nn.MarginCriterion",
            "nn.SelectTable", "nn.VolumetricFullConvolution",
            "nn.SpatialFractionalMaxPooling", "fbnn.ProjectiveGradientNormalization",
            "fbnn.Probe", "fbnn.SparseLinear", "cudnn._Pooling3D",
            "cudnn.VolumetricMaxPooling", "cudnn.SpatialCrossEntropyCriterion",
            "cudnn.VolumetricConvolution", "cudnn.SpatialAveragePooling", "cudnn.Tanh",
            "cudnn.LogSoftMax", "cudnn.SpatialConvolution", "cudnn._Pooling",
            "cudnn.SpatialMaxPooling", "cudnn.ReLU", "cudnn.SpatialCrossMapLRN",
            "cudnn.SoftMax", "cudnn._Pointwise", "cudnn.SpatialSoftMax", "cudnn.Sigmoid",
            "cudnn.SpatialLogSoftMax", "cudnn.VolumetricAveragePooling", "nngraph.Node",
            "nngraph.JustTable", "graph.Edge", "graph.Node", "graph.Graph"]:
    add_trivial_class_reader(mod)


class T7ReaderException(Exception):
    pass


class T7Reader:

    def __init__(self,
                 fileobj,
                 use_list_heuristic=True,
                 use_int_heuristic=True,
                 force_deserialize_classes=True,
                 force_8bytes_long=False):
        """
        Params:
        * `fileobj` file object to read from, must be actual file object
                    as it must support array, struct, and numpy
        * `use_list_heuristic`: automatically turn tables with only consecutive
                                positive integral indices into lists
                                (default True)
        * `use_int_heuristic`: cast all whole floats into ints (default True)
        * `force_deserialize_classes`: deserialize all classes, not just the
                                       whitelisted ones (default True)
        """
        self.f = fileobj
        self.objects = {}  # read objects so far

        self.use_list_heuristic = use_list_heuristic
        self.use_int_heuristic = use_int_heuristic
        self.force_deserialize_classes = force_deserialize_classes
        self.force_8bytes_long = force_8bytes_long

    def _read(self, fmt):
        sz = struct.calcsize(fmt)
        return struct.unpack(fmt, self.f.read(sz))

    def read_boolean(self):
        return self.read_int() == 1

    def read_int(self):
        return self._read('i')[0]

    def read_long(self):
        if self.force_8bytes_long:
            return self._read('q')[0]
        else:
            return self._read('l')[0]

    def read_long_array(self, n):
        if self.force_8bytes_long:
            lst = []
            for i in range(n):
                lst.append(self.read_long())
            return lst
        else:
            arr = array('l')
            arr.fromfile(self.f, n)
            return arr.tolist()       
    
    def read_float(self):
        return self._read('f')[0]

    def read_double(self):
        return self._read('d')[0]

    def read_string(self):
        size = self.read_int()
        bytes = self.f.read(size)
        return bytes.decode()

    def read_obj(self):
        typeidx = self.read_int()
        if typeidx == TYPE_NIL:
            return None
        elif typeidx == TYPE_NUMBER:
            x = self.read_double()
            # Extra checking for integral numbers:
            if self.use_int_heuristic and x.is_integer():
                return int(x)
            return x
        elif typeidx == TYPE_BOOLEAN:
            return self.read_boolean()
        elif typeidx == TYPE_STRING:
            return self.read_string()
        elif (typeidx == TYPE_TABLE or typeidx == TYPE_TORCH
                or typeidx == TYPE_FUNCTION or typeidx == TYPE_RECUR_FUNCTION
                or typeidx == LEGACY_TYPE_RECUR_FUNCTION):
            # read the index
            index = self.read_int()

            # check it is loaded already
            if index in self.objects:
                return self.objects[index]

            # otherwise read it
            if (typeidx == TYPE_FUNCTION or typeidx == TYPE_RECUR_FUNCTION
                    or typeidx == LEGACY_TYPE_RECUR_FUNCTION):
                size = self.read_int()
                dumped = self.f.read(size)
                upvalues = self.read_obj()
                obj = LuaFunction(size, dumped, upvalues)
                self.objects[index] = obj
                return obj
            elif typeidx == TYPE_TORCH:
                version = self.read_string()
                if version.startswith('V '):
                    versionNumber = int(version.partition(' ')[2])
                    className = self.read_string()
                else:
                    className = version
                    versionNumber = 0  # created before existence of versioning
                if className not in torch_readers:
                    if not self.force_deserialize_classes:
                        raise T7ReaderException(
                            'unsupported torch class: <%s>' % className)
                    obj = TorchObject(className, self.read_obj())
                else:
                    obj = torch_readers[className](self, version)
                self.objects[index] = obj
                return obj
            else:  # it is a table: returns a custom dict or a list
                size = self.read_int()
                obj = hashable_uniq_dict()  # custom hashable dict, can be a key
                key_sum = 0                # for checking if keys are consecutive
                keys_natural = True        # and also natural numbers 1..n.
                # If so, returns a list with indices converted to 0-indices.
                for i in range(size):
                    k = self.read_obj()
                    v = self.read_obj()
                    obj[k] = v

                    if self.use_list_heuristic:
                        if not isinstance(k, int) or k <= 0:
                            keys_natural = False
                        elif isinstance(k, int):
                            key_sum += k
                if self.use_list_heuristic:
                    # n(n+1)/2 = sum <=> consecutive and natural numbers
                    n = len(obj)
                    if keys_natural and n * (n + 1) == 2 * key_sum:
                        lst = []
                        for i in range(len(obj)):
                            lst.append(obj[i + 1])
                        obj = lst
                self.objects[index] = obj
                return obj
        else:
            raise T7ReaderException("unknown object")


def load(filename, **kwargs):
    """
    Loads the given t7 file using default settings; kwargs are forwarded
    to `T7Reader`.
    """
    with open(filename, 'rb') as f:
        reader = T7Reader(f, **kwargs)
        return reader.read_obj()
