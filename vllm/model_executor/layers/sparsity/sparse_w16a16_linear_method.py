from typing import Any, Dict, Optional, Type

import torch
import torch.nn.functional as F

from vllm.model_executor.layers.linear import LinearMethodBase, set_weight_attrs
from vllm.model_executor.layers.sparsity.base_config import SparsityConfig
from vllm.model_executor.layers.parameters import LazyCompressedParameter
from magic_wand.semi_structured import (pad_tensor_to_multiple,
                                        extract_valid_rows)
from magic_wand import (CompressedStorageFormat, SparseBEGemmStorageFormat,
                        SparseSemiStructuredStorageFormat)
from magic_wand.ops import be_ds_gemm


class SparseW16A16LinearMethod(LinearMethodBase):
    """Linear method for Sparse W16A16.

    Args:
        sparsity_config: The sparse config.
    """
    storage_format_cls: Type[CompressedStorageFormat] = None

    def __init__(self, sparsity_config: SparsityConfig,
                 storage_format_cls: Type[CompressedStorageFormat]):
        self.sparsity_config = sparsity_config
        self.storage_format_cls = storage_format_cls

    def create_weights(self, input_size_per_partition: int,
                       output_size_per_partition: int, input_size: int,
                       output_size: int,
                       params_dtype: torch.dtype) -> Dict[str, Any]:
        supports_linear = (self.storage_format_cls !=
                           SparseBEGemmStorageFormat)
        weight = LazyCompressedParameter(
            torch.empty((output_size_per_partition, input_size_per_partition),
                        dtype=params_dtype),
            storage_format_cls=self.storage_format_cls,
            # if we don't support F.linear or something analogous,
            # transpose when we compress so we can use a basic matmul
            compress_transposed=not supports_linear)

        set_weight_attrs(weight, {"input_dim": 1, "output_dim": 0})

        return {"weight": weight}

    def apply_weights(
        self,
        weights: Dict[str, Any],
        x: torch.Tensor,
        bias: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        w: LazyCompressedParameter = weights["weight"]

        # if we never compressed (likely due to insufficient sparsity),
        # i.e. have uncompressed_data run normally
        if w.has_uncompressed_data:
            assert not w.has_compressed_data
            output = F.linear(x, w.uncompressed_data, bias)
        elif self.storage_format_cls == SparseSemiStructuredStorageFormat:
            assert bias is None
            w_encap = w.compressed_data.encapsulated_torch_sparse_tensor
            out_shape = (x.shape[:-1] + (w_encap.shape[0], ))
            reshaped_x, valid_rows_range = pad_tensor_to_multiple(
                x.reshape(-1, x.shape[-1]), 8)
            output = F.linear(
                reshaped_x, w_encap,
                torch.nn.Parameter(torch.zeros((w_encap.shape[0], ))).to(
                    reshaped_x.dtype).to(reshaped_x.device)).contiguous()
            output = extract_valid_rows(output, valid_rows_range)
            return output.reshape(out_shape)
        elif self.storage_format_cls == SparseBEGemmStorageFormat:
            assert bias is None
            assert w.compress_transposed
            out_shape = (x.shape[:-1] + (w.shape[0], ))
            reshaped_x = x.reshape(-1, x.shape[-1])
            y = be_ds_gemm(reshaped_x, w.compressed_data)
            return y.reshape(out_shape)
        else:
            # Standard matrix multiply
            # Uncompress to dense
            assert not w.compress_transposed
            output = F.linear(x, w.compressed_data.decompress(), bias)
        return output
