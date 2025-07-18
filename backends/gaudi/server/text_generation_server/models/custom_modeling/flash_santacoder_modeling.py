import torch
import torch.distributed

from torch import nn
from transformers.activations import ACT2FN
from typing import Optional, List, Tuple

from text_generation_server.layers.attention import (
    paged_attention,
    attention,
    set_block_mapping,
    Seqlen,
    HPUPagedAttentionMetadata,
)
from text_generation_server.layers import (
    TensorParallelRowLinear,
    TensorParallelColumnLinear,
    SpeculativeHead,
    TensorParallelEmbedding,
    get_linear,
)
from text_generation_server.layers.attention.kv_cache import get_kv_scales
from text_generation_server.layers.gptq import GPTQWeightsLoader
from text_generation_server.layers.layernorm import (
    FastLayerNorm,
)
import habana_frameworks.torch as htorch


def load_multi_mqa(
    config, prefix: str, weights, bias: bool, head_size, num_heads, hidden_size
):
    if config.quantize == "gptq":
        return _load_multi_mqa_gptq(
            config, prefix, weights, bias, head_size, num_heads, hidden_size
        )
    else:
        return _load_multi_mqa(
            config, prefix, weights, bias, head_size, num_heads, hidden_size
        )


def _load_multi_mqa_gptq(
    config, prefix: str, weights, bias: bool, head_size, num_heads, hidden_size
):
    from text_generation_server.layers.gptq import GPTQWeight

    if any("c_attn" in k for k in weights.routing.keys()) and not config.transpose:
        world_size = weights.process_group.size()
        rank = weights.process_group.rank()

        slice_ = weights._get_slice(f"{prefix}.c_attn.qweight")
        shape = slice_.get_shape()
        block_size = (shape[1] - 2 * head_size) // world_size
        start = rank * block_size
        stop = (rank + 1) * block_size
        assert (shape[1] - 2 * head_size) % world_size == 0
        q_tensor = slice_[:, start:stop]
        kv_tensor = slice_[:, -2 * head_size :]
        qweight = torch.cat([q_tensor, kv_tensor], dim=1)
        qweight = qweight.to(device=weights.device)

        slice_ = weights._get_slice(f"{prefix}.c_attn.scales")
        shape = slice_.get_shape()
        block_size = (shape[1] - 2 * head_size) // world_size
        start = rank * block_size
        stop = (rank + 1) * block_size
        assert (shape[1] - 2 * head_size) % world_size == 0
        q_tensor = slice_[:, start:stop]
        kv_tensor = slice_[:, -2 * head_size :]
        scales = torch.cat([q_tensor, kv_tensor], dim=1)
        scales = scales.to(device=weights.device)

        slice_ = weights._get_slice(f"{prefix}.c_attn.qzeros")
        shape = slice_.get_shape()
        block_size = (shape[1] - (2 * head_size) * 4 // 32) // world_size
        start = rank * block_size
        stop = (rank + 1) * block_size
        assert 2 * head_size % (32 // 4) == 0
        q_tensor = slice_[:, start:stop]
        kv_tensor = slice_[:, -2 * head_size * 4 // 32 :]
        qzeros = torch.cat([q_tensor, kv_tensor], dim=1)
        qzeros = qzeros.to(device=weights.device)

        loader = weights.weights_loader
        assert isinstance(loader, GPTQWeightsLoader)
        loader._get_gptq_params(weights)
        if loader.quant_method == "gptq":
            g_idx = weights.get_tensor(f"{prefix}.c_attn.g_idx")
            g_idx = g_idx.to(device=weights.device)
        elif loader.quant_method == "awq":
            g_idx = None
            from text_generation_server.layers.awq.conversion_utils import (
                fast_awq_to_gptq,
            )

            qweight, qzeros = fast_awq_to_gptq(qweight, qzeros)

        from text_generation_server.layers.gptq import HAS_EXLLAMA

        weight = GPTQWeight(
            qweight=qweight,
            qzeros=qzeros,
            scales=scales,
            g_idx=g_idx,
            bits=loader.bits,
            groupsize=loader.groupsize,
            use_awq_kernel=loader.quantize == "awq",
            use_exllama=HAS_EXLLAMA,
        )

        if bias:
            slice_ = weights._get_slice(f"{prefix}.c_attn.bias")
            shape = slice_.get_shape()
            block_size = (shape[0] - 2 * head_size) // world_size
            assert (shape[0] - 2 * head_size) % world_size == 0
            q_tensor = slice_[start:stop]
            start = rank * block_size
            stop = (rank + 1) * block_size
            q_tensor = slice_[start:stop]
            kv_tensor = slice_[-2 * head_size :]
            bias = torch.cat([q_tensor, kv_tensor], dim=0)
            bias = bias.to(device=weights.device)

        return TensorParallelColumnLinear(get_linear(weight, bias))
    else:
        raise NotImplementedError("Gptq loading with santacoder is not implemented")


def _load_multi_mqa(
    config, prefix: str, weights, bias: bool, head_size, num_heads, hidden_size
):
    if any("c_attn" in k for k in weights.routing.keys()):
        slice_ = weights._get_slice(f"{prefix}.c_attn.weight")
        shape = slice_.get_shape()
        world_size = weights.process_group.size()
        rank = weights.process_group.rank()
        if config.transpose:
            block_size = (shape[1] - 2 * head_size) // world_size
            start = rank * block_size
            stop = (rank + 1) * block_size
            assert (shape[1] - 2 * head_size) % world_size == 0
            q_tensor = slice_[:, start:stop]
            kv_tensor = slice_[:, -2 * head_size :]
            weight = torch.cat([q_tensor, kv_tensor], dim=1).T
        else:
            block_size = (shape[0] - 2 * head_size) // world_size
            start = rank * block_size
            stop = (rank + 1) * block_size
            assert (shape[0] - 2 * head_size) % world_size == 0
            q_tensor = slice_[start:stop]
            kv_tensor = slice_[-2 * head_size :]
            weight = torch.cat([q_tensor, kv_tensor], dim=0)
        if bias:
            slice_ = weights._get_slice(f"{prefix}.c_attn.bias")
            shape = slice_.get_shape()
            block_size = (shape[0] - 2 * head_size) // world_size
            assert (shape[0] - 2 * head_size) % world_size == 0
            start = rank * block_size
            stop = (rank + 1) * block_size
            q_tensor = slice_[start:stop]
            kv_tensor = slice_[-2 * head_size :]
            bias = torch.cat([q_tensor, kv_tensor], dim=0)
    else:
        if config.transpose:
            w = [
                weights.get_sharded(f"{prefix}.q_attn.weight", dim=1).T,
                weights.get_tensor(f"{prefix}.kv_attn.weight").T,
            ]
            weight = torch.cat(w, dim=0)
        else:
            w = [
                weights.get_sharded(f"{prefix}.q_attn.weight", dim=0),
                weights.get_tensor(f"{prefix}.kv_attn.weight"),
            ]
            weight = torch.cat(w, dim=1)

        if bias:
            b = [
                weights.get_sharded(f"{prefix}.q_attn.bias", dim=0),
                weights.get_tensor(f"{prefix}.kv_attn.bias"),
            ]
            bias = torch.cat(b, dim=0)
        else:
            bias = None

    weight = weight.to(dtype=weights.dtype).to(device=weights.device)
    assert list(weight.shape) == [
        (num_heads + 2) * head_size,
        hidden_size,
    ], f"{weight.shape} != {[(num_heads + 2) * head_size, hidden_size]}"
    if bias is not None:
        bias = bias.to(dtype=weights.dtype).to(device=weights.device)
        assert list(bias.shape) == [
            (num_heads + 2) * head_size
        ], f"{weight.shape} != {[(num_heads + 2) * head_size]}"
    return TensorParallelColumnLinear(get_linear(weight, bias))


def load_col(config, prefix: str, weights, bias: bool):
    if config.transpose:
        weight = weights.get_sharded(f"{prefix}.weight", dim=1).T
    else:
        weight = weights.get_multi_weights_col([prefix], dim=0)

    if bias:
        bias = weights.get_sharded(f"{prefix}.bias", dim=0)
    else:
        bias = None
    return TensorParallelColumnLinear(get_linear(weight, bias))


def load_row(config, prefix: str, weights, bias: bool):
    if config.transpose:
        weight = weights.get_sharded(f"{prefix}.weight", dim=0).T
    else:
        weight = weights.get_weights_row(prefix)

    if bias and weights.process_group.rank() == 0:
        # Rank is only on the first rank process
        bias = weights.get_tensor(f"{prefix}.bias")
    else:
        bias = None
    return TensorParallelRowLinear(
        get_linear(weight, bias), process_group=weights.process_group
    )


class FlashMQAttention(torch.nn.Module):
    def __init__(self, prefix, config, weights):
        super().__init__()
        num_heads = config.num_attention_heads
        hidden_size = config.hidden_size

        self.num_heads = num_heads
        self.hidden_size = hidden_size
        self.head_size = hidden_size // num_heads

        if self.num_heads % weights.process_group.size() != 0:
            raise ValueError(
                f"`num_heads` must be divisible by `num_shards` (got `num_heads`: {self.num_heads} "
                f"and `num_shards`: {weights.process_group.size()}"
            )
        self.num_heads = self.num_heads // weights.process_group.size()

        self.softmax_scale = self.head_size ** (-0.5)

        self.c_attn = load_multi_mqa(
            config,
            prefix=prefix,
            weights=weights,
            bias=True,
            head_size=self.head_size,
            hidden_size=hidden_size,
            num_heads=self.num_heads,
        )
        self.c_proj = load_row(
            config, prefix=f"{prefix}.c_proj", weights=weights, bias=True
        )
        self.kv_scales = get_kv_scales(weights, f"{prefix}")
        self.kv_head_mapping = torch.zeros(
            self.num_heads, dtype=torch.int32, device=weights.device
        )

    def forward(
        self,
        hidden_states,
        cu_seqlen_prefill,
        kv_cache,
        slots,
        seqlen,
        hpu_attention_meta,
    ):
        qkv = self.c_attn(hidden_states)

        # Split query from key_value
        query, key_value = qkv.split(
            [self.head_size * self.num_heads, 2 * self.head_size], dim=1
        )

        # Prepare query and key_value for indexing
        query = query.view(-1, self.num_heads, self.head_size)
        key_value = key_value.view(-1, 2, 1, self.head_size)

        kv_cache.store(
            key=key_value[:, 0],
            value=key_value[:, 1],
            slots=slots,
            kv_scales=self.kv_scales,
        )

        # Prefill
        if cu_seqlen_prefill is not None:
            # sdpa
            attn_output = attention(
                query=query,
                key=key_value[:, 0],
                value=key_value[:, 1],
                kv_cache=kv_cache,
                kv_scales=self.kv_scales,
                seqlen=seqlen,
                softmax_scale=self.softmax_scale,
            )
        # Decode
        else:
            attn_output = paged_attention(
                query,
                kv_cache,
                self.kv_head_mapping,
                self.softmax_scale,
                seqlen,
                kv_scales=self.kv_scales,
                hpu_attention_meta=hpu_attention_meta,
            )

        return self.c_proj(attn_output.view(-1, self.num_heads * self.head_size))


class MLP(nn.Module):
    def __init__(self, prefix, config, weights):
        super().__init__()
        act = config.activation_function
        self.act = (
            ACT2FN[act]
            if "gelu" not in act
            else lambda x: torch.nn.functional.gelu(
                x,
                approximate=(
                    "tanh" if act in ["gelu_fast", "gelu_pytorch_tanh"] else "none"
                ),
            )
        )

        self.c_fc = load_col(
            config, prefix=f"{prefix}.c_fc", weights=weights, bias=True
        )
        self.c_proj = load_row(
            config, prefix=f"{prefix}.c_proj", weights=weights, bias=True
        )

    def forward(self, hidden_states):
        hidden_states = self.c_fc(hidden_states)
        hidden_states = self.act(hidden_states)
        hidden_states = self.c_proj(hidden_states)
        return hidden_states


class Block(nn.Module):
    def __init__(self, prefix: str, layer_id, config, weights):
        super().__init__()
        prefix = f"{prefix}.h.{layer_id}"
        self.ln_1 = FastLayerNorm.load(
            prefix=f"{prefix}.ln_1", weights=weights, eps=config.layer_norm_epsilon
        )
        self.ln_2 = FastLayerNorm.load(
            prefix=f"{prefix}.ln_2", weights=weights, eps=config.layer_norm_epsilon
        )
        self.self_attn = FlashMQAttention(
            prefix=f"{prefix}.attn",
            config=config,
            weights=weights,
        )
        self.mlp = MLP(
            prefix=f"{prefix}.mlp",
            config=config,
            weights=weights,
        )

    def forward(
        self,
        hidden_states,
        residual,
        cu_seqlen_prefill,
        kv_cache,
        slots,
        seqlen,
        hpu_attention_meta,
    ):
        hidden_states, residual = self.ln_1(hidden_states, residual)
        hidden_states = self.self_attn(
            hidden_states,
            cu_seqlen_prefill,
            kv_cache,
            slots,
            seqlen,
            hpu_attention_meta,
        )

        hidden_states, residual = self.ln_2(hidden_states, residual)

        mlp_output = self.mlp(hidden_states)

        return mlp_output, residual


class FlashSantacoderModel(nn.Module):
    def __init__(self, prefix: str, config, weights):
        super().__init__()
        self.config = config

        self.process_group = weights.process_group
        self.wte = TensorParallelEmbedding(
            prefix=f"{prefix}.wte",
            weights=weights,
            reduce=False,
        )
        self.wpe = TensorParallelEmbedding(
            prefix=f"{prefix}.wpe",
            weights=weights,
            reduce=False,
        )

        self.layers = nn.ModuleList(
            [
                Block(
                    prefix,
                    layer_id,
                    config,
                    weights,
                )
                for layer_id in range(config.num_hidden_layers)
            ]
        )
        self.ln_f = FastLayerNorm.load(
            prefix="transformer.ln_f", weights=weights, eps=config.layer_norm_epsilon
        )

        self.head_size = self.layers[0].self_attn.head_size
        self.num_heads = self.layers[0].self_attn.num_heads

    def forward(
        self,
        input_ids: torch.Tensor,
        position_ids: torch.Tensor,
        cu_seqlen_prefill: Optional[torch.Tensor],
        kv_cache: List[Tuple[torch.Tensor, torch.Tensor]],
        slots: torch.Tensor,
        seqlen: Seqlen,
        hpu_attention_meta: Optional[HPUPagedAttentionMetadata],
    ) -> torch.Tensor:
        if hpu_attention_meta is not None:
            hpu_attention_meta = set_block_mapping(
                hpu_attention_meta, input_ids.shape[0]
            )
        hidden_states = self.wte(input_ids) + self.wpe(position_ids)

        if self.process_group.size() > 1:
            torch.distributed.all_reduce(hidden_states, group=self.process_group)

        residual = None
        lazy_mode = htorch.utils.internal.is_lazy()
        if lazy_mode:
            htorch.core.mark_step()
        for i, layer in enumerate(self.layers):
            hidden_states, residual = layer(
                hidden_states,
                residual,
                cu_seqlen_prefill,
                kv_cache[i],
                slots,
                seqlen,
                hpu_attention_meta,
            )
            if lazy_mode:
                htorch.core.mark_step()

        hidden_states, _ = self.ln_f(hidden_states, residual)

        return hidden_states


class FlashSantacoderForCausalLM(nn.Module):
    def __init__(self, prefix, config, weights):
        super().__init__()

        if not prefix:
            prefix = "transformer"
        else:
            prefix = f"{prefix}.transformer"

        config.transpose = config.architectures[0].startswith("GPT2")
        self.model = FlashSantacoderModel(prefix, config, weights)
        self.lm_head = SpeculativeHead.load(
            config, prefix=f"{prefix}.wte", weights=weights
        )

    def forward(
        self,
        input_ids: torch.Tensor,
        position_ids: torch.Tensor,
        cu_seqlen_prefill: Optional[torch.Tensor],
        kv_cache: List[Tuple[torch.Tensor, torch.Tensor]],
        slots: torch.Tensor,
        seqlen: Seqlen,
        hpu_attention_meta: Optional[HPUPagedAttentionMetadata],
        lm_head_indices: Optional[torch.Tensor] = None,
        adapter_data: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        hidden_states = self.model(
            input_ids,
            position_ids,
            cu_seqlen_prefill,
            kv_cache,
            slots,
            seqlen,
            hpu_attention_meta,
        )
        if lm_head_indices is not None:
            hidden_states = hidden_states[lm_head_indices]
        logits = self.lm_head(hidden_states)
        return logits
