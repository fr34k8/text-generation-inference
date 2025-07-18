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
    TensorParallelEmbedding,
    SpeculativeHead,
)
from text_generation_server.layers.attention.kv_cache import get_kv_scales
from text_generation_server.layers.rotary import PositionRotaryEmbedding
from text_generation_server.layers.layernorm import (
    FastRMSNorm,
)
import habana_frameworks.torch as htorch


def load_attention(config, prefix, weights):
    if config.num_attention_heads != config.num_key_value_heads:
        return _load_gqa(config, prefix, weights)
    else:
        return TensorParallelColumnLinear.load_multi(
            config,
            prefixes=[f"{prefix}.q_proj", f"{prefix}.k_proj", f"{prefix}.v_proj"],
            dim=0,
            weights=weights,
            bias=True,
        )


def _load_gqa(config, prefix: str, weights):
    assert config.hidden_size % config.num_attention_heads == 0
    assert config.num_attention_heads % weights.process_group.size() == 0

    return TensorParallelColumnLinear.load_multi(
        config,
        prefixes=[f"{prefix}.q_proj", f"{prefix}.k_proj", f"{prefix}.v_proj"],
        dim=0,
        weights=weights,
        bias=True,
    )


class Qwen2Attention(torch.nn.Module):
    def __init__(
        self,
        prefix: str,
        config,
        weights,
        rotary_emb,
    ):
        super().__init__()
        self.max_past = (
            config.sliding_window
            if config.use_sliding_window and config.sliding_window is not None
            else -1
        )
        self.num_heads = config.num_attention_heads
        self.hidden_size = config.hidden_size
        self.head_size = self.hidden_size // self.num_heads
        self.rotary_emb = rotary_emb

        self.softmax_scale = self.head_size**-0.5

        if self.num_heads % weights.process_group.size() != 0:
            raise ValueError(
                f"`num_heads` must be divisible by `num_shards` (got `num_heads`: {self.num_heads} "
                f"and `num_shards`: {weights.process_group.size()}"
            )
        self.num_heads = self.num_heads // weights.process_group.size()
        self.num_key_value_heads = (
            config.num_key_value_heads // weights.process_group.size()
        )

        self.query_key_value = load_attention(config, prefix, weights)

        self.kv_scales = get_kv_scales(weights, f"{prefix}")

        self.o_proj = TensorParallelRowLinear.load(
            config,
            prefix=f"{prefix}.o_proj",
            weights=weights,
            bias=False,
        )
        self.num_groups = self.num_heads // self.num_key_value_heads
        self.kv_head_mapping = torch.arange(
            0, self.num_key_value_heads, dtype=torch.int32, device=weights.device
        ).repeat_interleave(self.num_groups)

    def forward(
        self,
        hidden_states,
        cos,
        sin,
        cu_seqlen_prefill,
        kv_cache,
        slots,
        seqlen,
        hpu_attention_meta,
    ):
        qkv = self.query_key_value(hidden_states)
        query, kv = qkv.split(
            [
                self.head_size * self.num_heads,
                2 * self.head_size * self.num_key_value_heads,
            ],
            dim=1,
        )
        query = query.view(-1, self.num_heads, self.head_size)
        kv = kv.view(-1, 2, self.num_key_value_heads, self.head_size)

        self.rotary_emb(query, torch.select(kv, dim=1, index=0), cos, sin)

        kv_cache.store(
            key=kv[:, 0],
            value=kv[:, 1],
            slots=slots,
            kv_scales=self.kv_scales,
        )

        # Prefill
        if cu_seqlen_prefill is not None:
            # sdpa
            attn_output = attention(
                query=query,
                key=kv[:, 0],
                value=kv[:, 1],
                kv_cache=kv_cache,
                kv_scales=self.kv_scales,
                seqlen=seqlen,
                softmax_scale=self.softmax_scale,
                window_size_left=self.max_past,
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
                window_size_left=self.max_past,
            )

        return self.o_proj(attn_output.view(-1, self.num_heads * self.head_size))


class Qwen2MLP(nn.Module):
    def __init__(self, prefix, config, weights):
        super().__init__()
        act = config.hidden_act
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
        # Fuse gate and up proj
        self.gate_up_proj = TensorParallelColumnLinear.load_multi(
            config,
            prefixes=[f"{prefix}.gate_proj", f"{prefix}.up_proj"],
            weights=weights,
            dim=0,
            bias=False,
        )
        self.down_proj = TensorParallelRowLinear.load(
            config,
            prefix=f"{prefix}.down_proj",
            weights=weights,
            bias=False,
        )
        self.intermediate_size = (
            config.intermediate_size // weights.process_group.size()
        )

    def forward(self, hidden_states):
        gate_up_states = self.gate_up_proj(hidden_states)
        gate_up_states = gate_up_states.view(-1, 2, self.intermediate_size)
        return self.down_proj(self.act(gate_up_states[:, 0]) * gate_up_states[:, 1])


class Qwen2Layer(nn.Module):
    def __init__(self, prefix, layer_id, config, weights, rotary_emb):
        super().__init__()
        prefix = f"{prefix}.layers.{layer_id}"
        self.self_attn = Qwen2Attention(
            prefix=f"{prefix}.self_attn",
            config=config,
            weights=weights,
            rotary_emb=rotary_emb,
        )
        self.mlp = Qwen2MLP(prefix=f"{prefix}.mlp", config=config, weights=weights)
        self.input_layernorm = FastRMSNorm.load(
            prefix=f"{prefix}.input_layernorm", weights=weights, eps=config.rms_norm_eps
        )
        self.post_attention_layernorm = FastRMSNorm.load(
            prefix=f"{prefix}.post_attention_layernorm",
            weights=weights,
            eps=config.rms_norm_eps,
        )

    def forward(
        self,
        hidden_states,
        residual,
        cos,
        sin,
        cu_seqlen_prefill,
        kv_cache,
        slots,
        seqlen,
        hpu_attention_meta,
    ):
        normed_hidden_states, residual = self.input_layernorm(hidden_states)

        # Self Attention
        attn_output = self.self_attn(
            normed_hidden_states,
            cos,
            sin,
            cu_seqlen_prefill,
            kv_cache,
            slots,
            seqlen,
            hpu_attention_meta,
        )
        hidden_states = attn_output + residual

        # faster post attention rms norm
        hidden_states, residual = self.post_attention_layernorm(hidden_states)
        mlp_output = self.mlp(hidden_states)
        hidden_states = mlp_output + residual
        return hidden_states


class Qwen2Model(torch.nn.Module):
    def __init__(self, prefix: str, config, weights):
        super().__init__()

        prefix = f"{prefix}.model" if prefix else "model"

        process_group = weights.process_group
        self.tp_rank = process_group.rank()
        self.tp_world_size = process_group.size()

        rotary_emb = PositionRotaryEmbedding.static(
            config=config,
            dim=config.hidden_size // config.num_attention_heads,
            base=config.rope_theta,
            device=weights.device,
        )

        self.layers = nn.ModuleList(
            [
                Qwen2Layer(
                    prefix,
                    layer_id,
                    config,
                    weights,
                    rotary_emb,
                )
                for layer_id in range(config.num_hidden_layers)
            ]
        )
        self.norm = FastRMSNorm.load(
            prefix=f"{prefix}.norm", weights=weights, eps=config.rms_norm_eps
        )

        self.gradient_checkpointing = False

        self.head_size = self.layers[0].self_attn.head_size
        self.num_heads = self.layers[0].self_attn.num_heads
        self.num_key_value_heads = self.layers[0].self_attn.num_key_value_heads

    def forward(
        self,
        inputs_embeds: torch.Tensor,
        position_ids: torch.Tensor,
        cu_seqlen_prefill: Optional[torch.Tensor],
        kv_cache: List[Tuple[torch.Tensor, torch.Tensor]],
        slots: torch.Tensor,
        seqlen: Seqlen,
        hpu_attention_meta: Optional[HPUPagedAttentionMetadata],
    ) -> torch.Tensor:
        if hpu_attention_meta is not None:
            hpu_attention_meta = set_block_mapping(
                hpu_attention_meta, inputs_embeds.shape[0]
            )
        hidden_states = inputs_embeds

        cos, sin = self.layers[0].self_attn.rotary_emb.get_cos_sin(
            position_ids,
        )

        residual = None
        lazy_mode = htorch.utils.internal.is_lazy()
        if lazy_mode:
            htorch.core.mark_step()
        for i, layer in enumerate(self.layers):
            hidden_states = layer(
                hidden_states,
                residual,
                cos,
                sin,
                cu_seqlen_prefill,
                kv_cache[i],
                slots,
                seqlen,
                hpu_attention_meta,
            )
            if lazy_mode:
                htorch.core.mark_step()

        hidden_states, _ = self.norm(hidden_states)

        return hidden_states


class Qwen2ForCausalLM(torch.nn.Module):
    def __init__(self, prefix: str, config, weights):
        super().__init__()

        self.model = Qwen2Model(prefix, config, weights)

        if config.tie_word_embeddings:
            suffix = "model.embed_tokens"
        else:
            suffix = "lm_head"

        self.lm_head = SpeculativeHead.load(
            config,
            prefix=f"{prefix}.{suffix}" if prefix else suffix,
            weights=weights,
        )

        self.embed_tokens = TensorParallelEmbedding(
            prefix=f"{prefix}.embed_tokens" if prefix else "model.embed_tokens",
            weights=weights,
        )

        self.max_past = config.sliding_window
        self.max_past_tensor = (
            torch.tensor(config.sliding_window, device=weights.device)
            if self.max_past is not None
            else None
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
        inputs_embeds = self.embed_tokens(input_ids)

        hidden_states = self.model(
            inputs_embeds,
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
