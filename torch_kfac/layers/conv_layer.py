from torch import inverse, renorm
from torch_kfac.utils.utils import append_homog, center, compute_cov, inverse_by_cholesky
from typing import Iterable, Union
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from .layer import Layer


class ConvLayer(Layer):
    def __init__(self, module: Union[nn.Conv1d, nn.Conv2d, nn.Conv3d], **kwargs) -> None:
        self.module = module
        in_features = np.prod(module.kernel_size) * module.in_channels + self.has_bias
        out_features = module.out_channels
        self.n_dim = len(module.kernel_size)
        super().__init__(in_features, out_features, dtype=module.weight.dtype, **kwargs)

        self._activations = None
        self._sensitivities = None

        @torch.no_grad()
        def forward_hook(module: nn.Module, inp: torch.Tensor, out: torch.Tensor) -> None:
            self._activations = self.extract_patches(inp[0])

        @torch.no_grad()
        def backward_hook(module: nn.Module, grad_inp: torch.Tensor, grad_out: torch.Tensor) -> None:
            self._sensitivities = grad_out[0].transpose(1, -1).contiguous()
            # Reshape to (batch_size, n_spatial_locations, n_out_features)
            self._sensitivities = self._sensitivities.view(
                self._sensitivities.shape[0],
                -1,
                self._sensitivities.shape[-1]
            )
            
        self.forward_hook_handle = self.module.register_forward_hook(forward_hook)
        self.backward_hook_handle = self.module.register_backward_hook(backward_hook)

        self._center = False

    def setup(self, center: bool = False, **kwargs) -> None:
        self._center = center

    def update_cov(self) -> None:
        act, sen = self._activations, self._sensitivities
        act = act.reshape(-1, act.shape[-1])
        sen = sen.reshape(-1, sen.shape[-1])
        if self._center:
            act = center(act)
            sen = center(sen)

        if self.has_bias:
            act = append_homog(act)

        activation_cov = compute_cov(act)
        sensitivity_cov = compute_cov(sen)
        self._activations_cov.add_to_average(activation_cov)
        self._sensitivities_cov.add_to_average(sensitivity_cov)

    def multiply_preconditioner(self, grads: Iterable[torch.Tensor], damping: torch.Tensor) -> Iterable[torch.Tensor]:
        act_cov, sen_cov = self.activation_covariance, self.sensitivity_covariance
        a_damp, s_damp = self.compute_damping(damping, self._activations.shape[1])
        act_cov_inverse = inverse_by_cholesky(act_cov, a_damp)
        sen_cov_inverse = inverse_by_cholesky(sen_cov, s_damp)

        if self.has_bias:
            weights, bias = grads
            # reshape to (out_features, in_features)
            weights = weights.view(weights.shape[0], -1)
            mat_grads = torch.cat([weights, bias[:, None]], -1)
        else:
            # reshape to (out_features, in_features)
            mat_grads = grads[0].view(grads.shape[0], -1)

        renorm_coeff = self._activations.shape[1]
        nat_grads = sen_cov_inverse @ mat_grads @ act_cov_inverse / renorm_coeff

        # Split up again
        if self.has_bias:
            return nat_grads[:, :-1].view_as(grads[0]), nat_grads[:, -1]
        else:
            return nat_grads.view_as(grads[0]),

    @property
    def has_bias(self) -> bool:
        return self.module.bias is not None

    @property
    def vars(self) -> Iterable[torch.Tensor]:
        if self.has_bias:
            return (self.module.weight, self.module.bias)
        else:
            return (self.module.weight,)

    def extract_patches(self, x: torch.Tensor) -> torch.Tensor:
        # Extract convolutional patches
        # Input: (batch_size, in_channels, spatial_dim1, ...)
        # Add padding
        if sum(self.module.padding) > 0:
            padding_mode = self.module.padding_mode
            if padding_mode == 'zeros':
                padding_mode = 'constant'
            x = F.pad(x, tuple(pad for pad in self.module.padding[::-1] for _ in range(2)), mode=padding_mode, value=0.)
        # Unfold the convolution
        for i, (size, stride) in enumerate(zip(self.module.kernel_size, self.module.stride)):
            x = x.unfold(i+2, size, stride)
        # Move in_channels to the end
        # https://github.com/pytorch/pytorch/issues/36048
        x = x.unsqueeze(2+self.n_dim).transpose(1, 2+self.n_dim).squeeze(1)
        # Make the memory contiguous
        x = x.contiguous()
        # Return the shape (batch_size, n_spatial_locations, n_in_features)
        x = x.view(
            x.shape[0],
            sum(x.shape[1+i] for i in range(self.n_dim)),
            -1
        )
        return x