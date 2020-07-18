from typing import Iterable
import torch

from .layer import Layer


class IdentityLayer(Layer):
    def __init__(self, module: torch.nn.Module, **kwargs) -> None:
        self.module = module
        super().__init__(
            in_features=0,
            out_features=0,
            dtype=torch.get_default_dtype(),
            **kwargs)

    def update_cov(self):
        return

    def multiply_preconditioner(self, grads: Iterable[torch.Tensor], damping: torch.Tensor) -> Iterable[torch.Tensor]:
        return grads
        
    @property
    def normalization_factor(self):
        return 1.

    @property
    def vars(self) -> Iterable[torch.Tensor]:
        return tuple()