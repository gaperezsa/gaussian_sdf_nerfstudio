# Copyright 2022 The Nerfstudio Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
gaussian_NeRF_Field implementations using tiny-cuda-nn, torch, ....
"""


from typing import Optional

import torch
import torch.nn.functional as F
import numpy as np
from nerfacc import ContractionType, contract
from torch.nn.parameter import Parameter
from torchtyping import TensorType

from nerfstudio.cameras.rays import RaySamples
from nerfstudio.data.scene_box import SceneBox
from nerfstudio.field_components.activations import trunc_exp
from nerfstudio.field_components.embedding import Embedding
from nerfstudio.field_components.field_heads import FieldHeadNames
from nerfstudio.fields.base_field import Field

try:
    import tinycudann as tcnn
except ImportError:
    # tinycudann module doesn't exist
    pass


def get_normalized_directions(directions: TensorType["bs":..., 3]):
    """SH encoding must be in the range [0, 1]

    Args:
        directions: batch of directions
    """
    return (directions + 1.0) / 2.0

#adapted from https://stackoverflow.com/questions/67633879/implementing-a-3d-gaussian-blur-using-separable-2d-convolutions-in-pytorch
def make_gaussian_kernel(sigma):
        ks = int(sigma * 5)
        if ks % 2 == 0:
            ks += 1
        ts = torch.linspace(-ks // 2, ks // 2 + 1, ks)
        gauss = torch.exp((-(ts / sigma)**2 / 2))
        kernel = gauss / gauss.sum()

        return kernel


class TCNNGaussianNeRFField(Field):
    """TCNN implementation of the gaussian NeRF Field.

    Args:
        aabb: parameters of scene aabb bounds
        num_layers: number of hidden layers
        hidden_dim: dimension of hidden layers
        geo_feat_dim: output geo feat dimensions
        num_layers_color: number of hidden layers for color network
        hidden_dim_color: dimension of hidden layers for color network
        use_appearance_embedding: whether to use appearance embedding
        num_images: number of images, required if use_appearance_embedding is True
        appearance_embedding_dim: dimension of appearance embedding
        contraction_type: type of contraction
        num_levels: number of levels of the hashmap for the base mlp
        log2_hashmap_size: size of the hashmap for the base mlp
    """

    def __init__(
        self,
        aabb,
        sigma = 1,
        f_init = "ones",
        f_transition_function = "relu",
        f_grid_resolution = 256,
        g_transition_function = "sigmoid",
        g_transition_alpha = 4.0,
        g_transition_alpha_increments = 0.0,
        occupancy_to_density_transformation_function = "exponential",
        density_multiplier = 1.0,
        num_layers: int = 2,
        hidden_dim: int = 64,
        geo_feat_dim: int = 15,
        num_layers_color: int = 3,
        hidden_dim_color: int = 64,
        use_appearance_embedding: bool = False,
        num_images: Optional[int] = None,
        appearance_embedding_dim: int = 32,
        contraction_type: ContractionType = ContractionType.UN_BOUNDED_SPHERE,
        num_levels: int = 16,
        log2_hashmap_size: int = 19,
    ) -> None:
        super().__init__()

        self.aabb = Parameter(aabb, requires_grad=False)
        self.geo_feat_dim = geo_feat_dim
        self.contraction_type = contraction_type

        self.use_appearance_embedding = use_appearance_embedding
        if use_appearance_embedding:
            assert num_images is not None
            self.appearance_embedding_dim = appearance_embedding_dim
            self.appearance_embedding = Embedding(num_images, appearance_embedding_dim)

        # TODO: set this properly based on the aabb
        per_level_scale = 1.4472692012786865

        self.direction_encoding = tcnn.Encoding(
            n_input_dims=3,
            encoding_config={
                "otype": "SphericalHarmonics",
                "degree": 4,
            },
        )

        self.mlp_base = tcnn.NetworkWithInputEncoding(
            n_input_dims=3,
            n_output_dims=1 + self.geo_feat_dim,
            encoding_config={
                "otype": "HashGrid",
                "n_levels": num_levels,
                "n_features_per_level": 2,
                "log2_hashmap_size": log2_hashmap_size,
                "base_resolution": 16,
                "per_level_scale": per_level_scale,
            },
            network_config={
                "otype": "FullyFusedMLP",
                "activation": "ReLU",
                "output_activation": "None",
                "n_neurons": hidden_dim,
                "n_hidden_layers": num_layers - 1,
            },
        )


        #learnable f^
        if f_init == "ones":
            self.f = Parameter(torch.ones(f_grid_resolution,f_grid_resolution,f_grid_resolution))
        elif f_init == "zeros":
            self.f = Parameter(torch.zeros(f_grid_resolution,f_grid_resolution,f_grid_resolution))
        else :
            self.f = Parameter((1/2)*(torch.rand(f_grid_resolution,f_grid_resolution,f_grid_resolution))+(1/2)) #random uniformly distributed between 1/2 and 1

        #this function will be applied everytime f^ is queried
        self.f_transition_function = f_transition_function

        #this function will be applied after occuapncy is calculated to get density
        self.occupancy_to_density_transformation_function = occupancy_to_density_transformation_function
        
        #store sigma for certification radii calculation
        self.sigma = sigma

        #in order to apply this kernel, it needs to be in the same device as f
        self.gaussian_kernel = make_gaussian_kernel(sigma).cuda()

        #this function will be applied to the smoothed grid in order to get occupancy
        self.g_transition_function = g_transition_function

        #starting alpha value to be used in case g_transition_function is sigmoid
        self.g_transition_alpha = g_transition_alpha

        #increments to be applied every step
        self.g_transition_alpha_increments = g_transition_alpha_increments

        #constant multiplier of output density
        self.density_multiplier = density_multiplier

        in_dim = self.direction_encoding.n_output_dims + self.geo_feat_dim
        if self.use_appearance_embedding:
            in_dim += self.appearance_embedding_dim
        self.mlp_head = tcnn.Network(
            n_input_dims=in_dim,
            n_output_dims=3,
            network_config={
                "otype": "FullyFusedMLP",
                "activation": "ReLU",
                "output_activation": "Sigmoid",
                "n_neurons": hidden_dim_color,
                "n_hidden_layers": num_layers_color - 1,
            },
        )

    
    #adapted from https://stackoverflow.com/questions/67633879/implementing-a-3d-gaussian-blur-using-separable-2d-convolutions-in-pytorch
    def apply_3d_gaussian_blur(self,input_grid):
        # Make a test volume
        vol = input_grid

        # use class kernel
        k = self.gaussian_kernel

        # Separable 1D convolution
        vol_in = vol[None, None, ...]
        k1d = k[None, None, :, None, None]
        for i in range(3):
            vol_in = vol_in.permute(0, 1, 4, 2, 3)
            vol_in = F.conv3d(vol_in, k1d, stride=1, padding=(len(k) // 2, 0, 0))
        vol_3d_sep = vol_in
        #print((vol_3d- vol_3d_sep).abs().max()) # something ~1e-7
        #print(torch.allclose(vol_3d, vol_3d_sep)) # allclose checks if it is around 1e-8
        return vol_3d_sep

    

    def get_certified_radius(self):

        #taken from https://stackoverflow.com/questions/11144513/cartesian-product-of-x-and-y-array-points-into-single-array-of-2d-points
        def cartesian_product(*arrays):
            la = len(arrays)
            dtype = np.result_type(*arrays)
            arr = np.empty([len(a) for a in arrays] + [la], dtype=dtype)
            for i, a in enumerate(np.ix_(*arrays)):
                arr[...,i] = a
            return arr.reshape(-1, la)
        
        '''
        positions = ray_samples.frustums.get_positions()
        positions_flat = positions.view(-1, 3)
        positions_flat = contract(x=positions_flat, roi=self.aabb, type=self.contraction_type)
        positions_rescaled = (positions_flat*2)-1 #such that now they are between -1 and 1 as torch grid_sample requires
        positions_rescaled.requires_grad = True
        '''

        
        space_discrete_sample_indexes = np.linspace(-1, 1, 2*(self.f.shape[-1]))
        positions = cartesian_product(space_discrete_sample_indexes,space_discrete_sample_indexes,space_discrete_sample_indexes)
        positions = torch.tensor(positions,dtype=torch.float32).cuda()
        positions.requires_grad = True

        if self.f_transition_function == "relu":
            smoothed_grid = self.apply_3d_gaussian_blur(torch.nn.ReLU()(self.f))
        elif self.f_transition_function == "sigmoid":
            smoothed_grid = self.apply_3d_gaussian_blur(torch.nn.Sigmoid()(self.f))

        
        m = torch.distributions.normal.Normal(torch.tensor([0.0]).cuda(), torch.tensor([1.0]).cuda())
        radii = self.sigma * m.icdf(F.interpolate(smoothed_grid,scale_factor=2))
        radii = radii.squeeze()

        output_aux = (self.sigma * m.icdf(F.grid_sample(smoothed_grid,positions[None,None,None,...],align_corners=True))).squeeze()

        sdf_grads = torch.autograd.grad(
            outputs=output_aux,
            inputs=positions,
            grad_outputs=torch.ones_like(output_aux),
            create_graph=True,
            retain_graph=True,
            only_inputs=True)[0]
        
        eikonal_loss = ((sdf_grads.norm(2, dim=1) - 1) ** 2).mean()

        #radii = radii.reshape(2*(self.f.shape[-1]),2*(self.f.shape[-1]),2*(self.f.shape[-1]))
        return radii, eikonal_loss

    def get_density(self, ray_samples: RaySamples):
        positions = ray_samples.frustums.get_positions()
        positions_flat = positions.view(-1, 3)
        positions_flat = contract(x=positions_flat, roi=self.aabb, type=self.contraction_type)

        h = self.mlp_base(positions_flat).view(*ray_samples.frustums.shape, -1)
        base_density, base_mlp_out = torch.split(h, [1, self.geo_feat_dim], dim=-1)

        positions_rescaled = (positions_flat*2)-1 #such that now they are between -1 and 1 as torch grid_sample requires
        if self.f_transition_function == "relu":
            smoothed_grid = self.apply_3d_gaussian_blur(torch.nn.ReLU()(self.f))
        elif self.f_transition_function == "sigmoid":
            smoothed_grid = self.apply_3d_gaussian_blur(torch.nn.Sigmoid()(self.f))

        if self.g_transition_function == "sigmoid":
            arg_maxed_smoothed_grid = torch.nn.Sigmoid()(self.g_transition_alpha*(smoothed_grid-(1/2)))
        else:
            arg_maxed_smoothed_grid = smoothed_grid
        density_before_activation = F.grid_sample(arg_maxed_smoothed_grid,positions_rescaled[None,None,None,...],align_corners=True)
        density_before_activation = density_before_activation.view(-1,1) #to match previous instant-ngp implementation

        # Rectifying the density with an exponential is much more stable than a ReLU or
        # softplus, because it enables high post-activation (float32) density outputs
        # from smaller internal (float16) parameters.

        density = self.density_multiplier * trunc_exp(density_before_activation.to(positions))
        return density, base_mlp_out

    def get_outputs(self, ray_samples: RaySamples, density_embedding: Optional[TensorType] = None):
        directions = get_normalized_directions(ray_samples.frustums.directions)
        directions_flat = directions.view(-1, 3)

        d = self.direction_encoding(directions_flat)
        if density_embedding is None:
            positions = SceneBox.get_normalized_positions(ray_samples.frustums.get_positions(), self.aabb)
            h = torch.cat([d, positions.view(-1, 3)], dim=-1)
        else:
            h = torch.cat([d, density_embedding.view(-1, self.geo_feat_dim)], dim=-1)

        if self.use_appearance_embedding:
            if ray_samples.camera_indices is None:
                raise AttributeError("Camera indices are not provided.")
            camera_indices = ray_samples.camera_indices.squeeze()
            if self.training:
                embedded_appearance = self.appearance_embedding(camera_indices)
            else:
                embedded_appearance = torch.zeros(
                    (*directions.shape[:-1], self.appearance_embedding_dim), device=directions.device
                )
            h = torch.cat([h, embedded_appearance.view(-1, self.appearance_embedding_dim)], dim=-1)

        rgb = self.mlp_head(h).view(*ray_samples.frustums.directions.shape[:-1], -1).to(directions)
        return {FieldHeadNames.RGB: rgb}

    def get_opacity(self, positions: TensorType["bs":..., 3], step_size) -> TensorType["bs":..., 1]:
        """Returns the opacity for a position. Used primarily by the occupancy grid.

        Args:
            positions: the positions to evaluate the opacity at.
            step_size: the step size to use for the opacity evaluation.
        """
        density = self.density_fn(positions)
        ## TODO: We should scale step size based on the distortion. Currently it uses too much memory.
        # aabb_min, aabb_max = self.aabb[0], self.aabb[1]
        # if self.contraction_type is not ContractionType.AABB:
        #     x = (positions - aabb_min) / (aabb_max - aabb_min)
        #     x = x * 2 - 1  # aabb is at [-1, 1]
        #     mag = x.norm(dim=-1, keepdim=True)
        #     mask = mag.squeeze(-1) > 1

        #     dev = (2 * mag - 1) / mag**2 + 2 * x**2 * (1 / mag**3 - (2 * mag - 1) / mag**4)
        #     dev[~mask] = 1.0
        #     dev = torch.clamp(dev, min=1e-6)
        #     step_size = step_size / dev.norm(dim=-1, keepdim=True)
        # else:
        #     step_size = step_size * (aabb_max - aabb_min)

        opacity = density * step_size
        return opacity
