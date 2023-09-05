# %%
"""Steer the model with feature dims and observe the resulting completions."""


from contextlib import contextmanager
from typing import Callable, Optional, Tuple

import accelerate
import numpy as np
import torch as t
import yaml
from torch import nn
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    GenerationConfig,
    PreTrainedModel,
    PreTrainedTokenizer,
)

# %%
# Set up constants.
with open("act_access.yaml", "r") as f:
    try:
        access = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(e)
with open("act_config.yaml", "r") as f:
    try:
        config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(e)
HF_ACCESS_TOKEN = access.get("HF_ACCESS_TOKEN", "")
MODEL_DIR = config.get("MODEL_DIR")
ENCODER_PATH = config.get("ENCODER_PATH")
BIASES_PATH = config.get("BIASES_PATH")
SEED = config.get("SEED")
ACTS_LAYER = config.get("ACTS_LAYER")
ACT_NUM: int = ACTS_LAYER

DIMENSION_TO_ADD: int = 400
COEFF: int = 4

CHAT_PROMPT: str = "I want to kill you because "
MAX_NEW_TOKENS: int = 50
NUM_CONTINUATIONS: int = 5
DO_SAMPLE: bool = True
TEMPERATURE: float = 1.0
TOP_P: float = 0.9
REP_PENALTY: float = 2.0

sampling_kwargs: dict = {
    "temperature": TEMPERATURE,
    "top_p": TOP_P,
    "repetition_penalty": REP_PENALTY,
}

# Reproducibility.
t.manual_seed(SEED)
np.random.seed(SEED)

# Set up model.
t.set_grad_enabled(False)
accelerator = accelerate.Accelerator()
model: PreTrainedModel = AutoModelForCausalLM.from_pretrained(
    MODEL_DIR,
    device_map="auto",
    use_auth_token=HF_ACCESS_TOKEN,
)
tokenizer: PreTrainedTokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
model.eval()
model: PreTrainedModel = accelerator.prepare(model)

# %%
# Declare hooking types.
PreHookFn = Callable[[nn.Module, t.Tensor], Optional[t.Tensor]]
Hook = Tuple[nn.Module, PreHookFn]
Hooks = list[Hook]


# %%
def tokenize(text: str) -> dict[str, t.Tensor]:
    """Tokenize prompts onto the appropriate devices."""
    tokens = tokenizer(text, return_tensors="pt")
    tokens = accelerator.prepare(tokens)
    return tokens


# %%
# As a control: run the unmodified base model.
base_tokens = model.generate(
    **tokenize([CHAT_PROMPT] * NUM_CONTINUATIONS),
    generation_config=GenerationConfig(
        **sampling_kwargs,
        do_sample=DO_SAMPLE,
        max_new_tokens=MAX_NEW_TOKENS,
        eos_token_id=tokenizer.eos_token_id,
    ),
)
base_strings = [tokenizer.decode(o) for o in base_tokens]
print(("\n" + "." * 80 + "\n").join(base_strings))


# %%
# Hooking functionality.
@contextmanager
def pre_hooks(hooks: Hooks):
    """Register pre-forward hooks with torch."""
    handles = []
    try:
        handles = [mod.register_forward_pre_hook(hook) for mod, hook in hooks]
        yield
    finally:
        for handle in handles:
            handle.remove()


def get_blocks(mod):
    """Get the blocks of a model."""
    if isinstance(mod, PreTrainedModel):
        return mod.model.layers
    raise ValueError(f"Unsupported model type: {type(mod)}.")


@contextmanager
def residual_stream(mod: PreTrainedModel, layers: Optional[list[int]] = None):
    """Actually build hooks for a model."""
    # TODO Plausibly could be replaced by "output_hidden_states=True" in model
    # call.
    modded_streams = [None] * len(get_blocks(mod))

    # Factory function that builds the initial hooks.
    def _make_helper_hook(i):
        def _helper_hook(_, current_inputs):
            modded_streams[i] = current_inputs[0]

        return _helper_hook

    hooks = [
        (layer, _make_helper_hook(i))
        for i, layer in enumerate(get_blocks(mod))
        if i in layers
    ]
    # Register the hooks.
    with pre_hooks(hooks):
        yield modded_streams


def get_resid_pre(prompt: str, layer_num: int):
    """Get residual stream activations for a prompt, just before a layer."""
    # TODO: Automatic addition padding.
    with residual_stream(model, layers=[layer_num]) as unmodified_streams:
        model(**tokenize(prompt))
    return unmodified_streams[layer_num]


# %%
# Get the steering vector from the encoder.
encoder_weights = t.load(ENCODER_PATH)
# Remember that the biases have the shape (PROJECTION_DIM,).
encoder_biases = t.load(BIASES_PATH)



steering_vec =


# %%
# Run the model with the steering vector * COEFF.
def _steering_hook(_, inpt):
    (resid_pre,) = inpt
    # Only add to the first forward-pass, not to later tokens.
    if resid_pre.shape[1] == 1:
        # Caching in `model.generate` for new tokens.
        return
    ppos, apos = resid_pre.shape[1], steering_vec.shape[1]
    assert (
        apos <= ppos
    ), f"More modified streams ({apos}) than prompt streams ({ppos})!"
    resid_pre[:, :apos, :] += COEFF * steering_vec


layer = get_blocks(model)[ACT_NUM]
with pre_hooks(hooks=[(layer, _steering_hook)]):
    steered_tokens = model.generate(
        **tokenize([CHAT_PROMPT] * NUM_CONTINUATIONS),
        generation_config=GenerationConfig(
            **sampling_kwargs,
            do_sample=DO_SAMPLE,
            max_new_tokens=MAX_NEW_TOKENS,
            eos_token_id=tokenizer.eos_token_id,
        ),
    )
steered_strings = [tokenizer.decode(o) for o in steered_tokens]
print(("\n" + "-" * 80 + "\n").join(steered_strings))
