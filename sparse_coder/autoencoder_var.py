# %%
"""
Dict learning on an activations dataset, with a basic autoencoder.

The script will save the trained encoder matrix to disk; that encoder matrix
is your learned dictionary.
"""


import numpy as np
import torch as t
import lightning as L
import yaml
from sklearn.model_selection import train_test_split
from torch.utils.data import DataLoader, Dataset


# %%
# Set up constants.
with open("act_config.yaml", "r") as f:
    try:
        config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(e)
SEED = config.get("SEED")
ACTS_DATA_PATH = config.get("ACTS_DATA_PATH")
PROMPT_IDS_PATH = config.get("PROMPT_IDS_PATH")
ENCODER_PATH = config.get("ENCODER_PATH")
EMBEDDING_DIM = config.get("EMBEDDING_DIM")
PROJECTION_FACTOR = config.get("PROJECTION_FACTOR")
PROJECTION_DIM = int(EMBEDDING_DIM * PROJECTION_FACTOR)

# We want to weight L1 quite heavily, versus MSE. Drive towards an L_0 of
# 20-100 at convergence.
LAMBDA_L1: float = 1.0
LEARNING_RATE: float = 1e-2
LOG_EVERY_N_STEPS: int = 5
EPOCHS: int = 150

# %%
# Use available tensor cores.
t.set_float32_matmul_precision("medium")


# %%
# Create a padding mask.
def padding_mask(
    activations_block: t.Tensor, unpadded_prompts: list[list[str]]
) -> t.Tensor:
    """Create a padding mask for the activations block."""
    masks: list = []

    for unpadded_prompt in unpadded_prompts:
        original_stream_length: int = len(unpadded_prompt)
        # The mask will drop the embedding dimension.
        mask: t.Tensor = t.zeros(
            (activations_block.size(1),),
            dtype=t.bool,
        )
        mask[:original_stream_length] = True
        masks.append(mask)

    # `masks` is of shape (batch, stream_dim).
    masks: t.Tensor = t.stack(masks, dim=0)
    return masks


# %%
# Define a `torch` dataset.
class ActivationsDataset(Dataset):
    """Dataset of hidden states from a pretrained model."""

    def __init__(self, tensor_data: t.Tensor, mask: t.Tensor):
        """Constructor; inherits from `torch.utils.data.Dataset` class."""
        self.data = tensor_data
        self.mask = mask

    def __len__(self):
        """Return the dataset length."""
        return len(self.data)

    def __getitem__(self, indx):
        """Return the item at the passed index."""
        return self.data[indx], self.mask[indx]


# %%
# Load, preprocess, and split the activations dataset.
padded_acts_block = t.load(ACTS_DATA_PATH)

prompts_ids: np.ndarray = np.load(PROMPT_IDS_PATH, allow_pickle=True)
prompts_ids_list = prompts_ids.tolist()
unpacked_prompts_ids = [
    elem for sublist in prompts_ids_list for elem in sublist
]
pad_mask: t.Tensor = padding_mask(padded_acts_block, unpacked_prompts_ids)

dataset: ActivationsDataset = ActivationsDataset(
    padded_acts_block,
    pad_mask,
)

training_indices, val_indices = train_test_split(
    np.arange(len(dataset)),
    test_size=0.2,
    random_state=SEED,
)

training_sampler = t.utils.data.SubsetRandomSampler(training_indices)
validation_sampler = t.utils.data.SubsetRandomSampler(val_indices)

training_loader: DataLoader = DataLoader(
    dataset,
    batch_size=32,
    sampler=training_sampler,
    num_workers=16,
)

validation_loader: DataLoader = DataLoader(
    dataset,
    batch_size=32,
    sampler=validation_sampler,
    num_workers=16,
)


# %%
# Define a tied autoencoder, with `lightning`.
class Autoencoder(L.LightningModule):
    """An autoencoder architecture."""

    def __init__(self, lr=LEARNING_RATE):
        super().__init__()
        self.save_hyperparameters()
        self.encoder = t.nn.Sequential(
            t.nn.Linear(EMBEDDING_DIM, PROJECTION_DIM, bias=True),
            t.nn.ReLU(),
        )

        # Orthogonal initialization.
        t.nn.init.orthogonal_(self.encoder[0].weight.data)

    def forward(self, state):  # pylint: disable=arguments-differ
        """The forward pass of an autoencoder for activations."""
        encoded_state = self.encoder(state)

        # Decode the sampled state.
        decoder_weights = self.encoder[0].weight.data.T
        output_state = t.nn.functional.linear(encoded_state, decoder_weights)

        return encoded_state, output_state

    def training_step(self, batch):  # pylint: disable=arguments-differ
        """Train the autoencoder."""
        data, mask = batch
        data_mask = mask.unsqueeze(-1).expand_as(data)
        masked_data = data * data_mask

        encoded_state, output_state = self.forward(masked_data)

        # The mask excludes the padding tokens from consideration.
        mse_loss = t.nn.functional.mse_loss(output_state, masked_data)
        l1_loss = t.nn.functional.l1_loss(
            encoded_state,
            t.zeros_like(encoded_state),
        )

        training_loss = mse_loss + (LAMBDA_L1 * l1_loss)
        l0_sparsity = (encoded_state != 0).float().sum(dim=-1).mean().item()
        print(f"L_0: {round(l0_sparsity, 2)}")
        self.log("training loss", training_loss)
        print(f"train loss: {round(training_loss.item(), 2)}")
        self.log("L1 component", LAMBDA_L1 * l1_loss)
        self.log("MSE component", mse_loss)
        self.log("L0 sparsity", l0_sparsity)
        return training_loss

    # Unused import resolves `lightning` bug.
    def validation_step(
        self, batch, batch_idx
    ):  # pylint: disable=unused-argument,arguments-differ
        """Validate the autoencoder."""
        data, mask = batch
        data_mask = mask.unsqueeze(-1).expand_as(data)
        masked_data = data * data_mask

        encoded_state, output_state = self.forward(masked_data)

        mse_loss = t.nn.functional.mse_loss(output_state, masked_data)
        l1_loss = t.nn.functional.l1_loss(
            encoded_state,
            t.zeros_like(encoded_state),
        )
        validation_loss = mse_loss + (LAMBDA_L1 * l1_loss)

        self.log("validation loss", validation_loss)
        return validation_loss

    def configure_optimizers(self):
        """Configure the `Adam` optimizer."""
        return t.optim.Adam(self.parameters(), lr=self.hparams.lr)


# %%
# Validation-loss-based early stopping.
early_stop = L.pytorch.callbacks.EarlyStopping(
    monitor="validation loss",
    min_delta=1e-5,
    patience=3,
    verbose=False,
    mode="min",
)

# %%
# Train the autoencoder. Note that `lightning` does its own parallelization.
model: Autoencoder = Autoencoder()
trainer: L.Trainer = L.Trainer(
    accelerator="auto",
    callbacks=early_stop,
    max_epochs=EPOCHS,
    log_every_n_steps=LOG_EVERY_N_STEPS,
)

trainer.fit(
    model,
    train_dataloaders=training_loader,
    val_dataloaders=validation_loader,
)

# %%
# Save the trained encoder matrix.
t.save(model.encoder[0].weight.data, ENCODER_PATH)
