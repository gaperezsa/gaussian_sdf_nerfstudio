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
Implementation of GaussianNeRF.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Type

import nerfacc
import torch
from nerfacc import ContractionType
from torch.nn import Parameter
from torchmetrics import PeakSignalNoiseRatio
from torchmetrics.functional import structural_similarity_index_measure
from torchmetrics.image.lpip import LearnedPerceptualImagePatchSimilarity
from typing_extensions import Literal

from nerfstudio.cameras.rays import RayBundle
from nerfstudio.engine.callbacks import (
    TrainingCallback,
    TrainingCallbackAttributes,
    TrainingCallbackLocation,
)
from nerfstudio.field_components.field_heads import FieldHeadNames
from nerfstudio.fields.gaussian_NeRF_Field import TCNNGaussianNeRFField
from nerfstudio.model_components.losses import MSELoss
from nerfstudio.model_components.ray_samplers import VolumetricSampler
from nerfstudio.model_components.renderers import (
    AccumulationRenderer,
    DepthRenderer,
    RGBRenderer,
)
from nerfstudio.models.base_model import Model, ModelConfig
from nerfstudio.utils import colormaps, colors


@dataclass
class GaussianNeRFModelConfig(ModelConfig):
    """Gaussian NeRF Model Config"""

    _target: Type = field(
        default_factory=lambda: GaussianNeRFModel
    )
    """target class to instantiate"""
    enable_collider: bool = False
    """Whether to create a scene collider to filter rays."""
    collider_params: Optional[Dict[str, float]] = None
    """Instant NGP doesn't use a collider."""
    max_num_samples_per_ray: int = 24
    """Number of samples in field evaluation."""
    grid_resolution: int = 128
    """Resolution of the grid used for the occupancy field (instant-ngp one)."""
    f_init: Literal["ones", "zeros", "rand"] = "ones"
    """function used to transition f^ grid to f."""
    f_transition_function: Literal["relu", "sigmoid"] = "sigmoid"
    """function used to transition f^ grid to f."""
    f_grid_resolution: int = 256
    """resolution of f^."""
    sigma: float = 1.0
    """standard deviation used in the normal convolution."""
    g_transition_function: Literal["identity", "sigmoid"] = "sigmoid"
    """function used to transition smooth grid g to occupancy grid G."""
    g_transition_alpha: float = 4.0
    """alpha hyperparameter used in the transition function from g to G."""
    g_transition_alpha_increments: float = 0
    """alpha hyperparameter is incremented by this ammount every step."""
    contraction_type: ContractionType = ContractionType.UN_BOUNDED_SPHERE
    """Contraction type used for spatial deformation of the field."""
    cone_angle: float = 0.004
    """Should be set to 0.0 for blender scenes but 1./256 for real scenes."""
    render_step_size: float = 0.01
    """Minimum step size for rendering."""
    near_plane: float = 0.05
    """How far along ray to start sampling."""
    far_plane: float = 1e3
    """How far along ray to stop sampling."""
    use_appearance_embedding: bool = False
    """Whether to use an appearance embedding."""
    background_color: Literal["random", "black", "white"] = "random"
    """The color that is given to untrained areas."""


class GaussianNeRFModel(Model):
    """Instant NGP model

    Args:
        config: instant NGP configuration to instantiate model
    """

    config: GaussianNeRFModelConfig
    field: TCNNGaussianNeRFField

    def __init__(self, config: GaussianNeRFModelConfig, **kwargs) -> None:
        super().__init__(config=config, **kwargs)

    def populate_modules(self):
        """Set the fields and modules."""
        super().populate_modules()

        self.field = TCNNGaussianNeRFField(
            sigma = self.config.sigma,
            f_init = self.config.f_init,
            f_transition_function = self.config.f_transition_function,
            f_grid_resolution = self.config.f_grid_resolution,
            g_transition_function = self.config.g_transition_function,
            g_transition_alpha = self.config.g_transition_alpha,
            g_transition_alpha_increments = self.config.g_transition_alpha_increments,
            aabb=self.scene_box.aabb,
            contraction_type=self.config.contraction_type,
            use_appearance_embedding=self.config.use_appearance_embedding,
            num_images=self.num_train_data,
        )

        self.scene_aabb = Parameter(self.scene_box.aabb.flatten(), requires_grad=False)

        # Occupancy Grid
        self.occupancy_grid = nerfacc.OccupancyGrid(
            roi_aabb=self.scene_aabb,
            resolution=self.config.grid_resolution,
            contraction_type=self.config.contraction_type,
        )

        # Sampler
        vol_sampler_aabb = self.scene_box.aabb if self.config.contraction_type == ContractionType.AABB else None
        self.sampler = VolumetricSampler(
            scene_aabb=vol_sampler_aabb,
            occupancy_grid=self.occupancy_grid,
            density_fn=self.field.density_fn,
        )

        # renderers
        background_color = "random"
        if self.config.background_color in ["white", "black"]:
            background_color = colors.COLORS_DICT[self.config.background_color]

        self.renderer_rgb = RGBRenderer(background_color=background_color)
        self.renderer_accumulation = AccumulationRenderer()
        self.renderer_depth = DepthRenderer(method="expected")

        # losses
        self.rgb_loss = MSELoss()

        # metrics
        self.psnr = PeakSignalNoiseRatio(data_range=1.0)
        self.ssim = structural_similarity_index_measure
        self.lpips = LearnedPerceptualImagePatchSimilarity(normalize=True)


    def get_training_callbacks(
        self, training_callback_attributes: TrainingCallbackAttributes
    ) -> List[TrainingCallback]:
        def update_occupancy_grid(step: int):
            # TODO: needs to get access to the sampler, on how the step size is determinated at each x. See
            # https://github.com/KAIR-BAIR/nerfacc/blob/127223b11401125a9fce5ce269bb0546ee4de6e8/examples/train_ngp_nerf.py#L190-L213
            self.occupancy_grid.every_n_step(
                step=step,
                occ_eval_fn=lambda x: self.field.get_opacity(x, self.config.render_step_size),
            )
        def update_alpha_value(step: int):
            self.field.g_transition_alpha += self.field.g_transition_alpha_increments

        return [
            TrainingCallback(
                where_to_run=[TrainingCallbackLocation.BEFORE_TRAIN_ITERATION],
                update_every_num_iters=1,
                func=update_occupancy_grid,
            ),
            TrainingCallback(
                where_to_run=[TrainingCallbackLocation.BEFORE_TRAIN_ITERATION],
                update_every_num_iters=1,
                func=update_alpha_value,
            ),
        ]

    def get_param_groups(self) -> Dict[str, List[Parameter]]:
        param_groups = {}
        if self.field is None:
            raise ValueError("populate_fields() must be called before get_param_groups")
        param_groups["fields"] = list(self.field.parameters())
        return param_groups

    def get_outputs(self, ray_bundle: RayBundle):
        assert self.field is not None
        num_rays = len(ray_bundle)

        with torch.no_grad():
            ray_samples, ray_indices = self.sampler(
                ray_bundle=ray_bundle,
                near_plane=self.config.near_plane,
                far_plane=self.config.far_plane,
                render_step_size=self.config.render_step_size,
                cone_angle=self.config.cone_angle,
            )

        field_outputs = self.field(ray_samples)

        # accumulation
        packed_info = nerfacc.pack_info(ray_indices, num_rays)
        weights = nerfacc.render_weight_from_density(
            packed_info=packed_info,
            sigmas=field_outputs[FieldHeadNames.DENSITY],
            t_starts=ray_samples.frustums.starts,
            t_ends=ray_samples.frustums.ends,
        )

        rgb = self.renderer_rgb(
            rgb=field_outputs[FieldHeadNames.RGB],
            weights=weights,
            ray_indices=ray_indices,
            num_rays=num_rays,
        )
        depth = self.renderer_depth(
            weights=weights, ray_samples=ray_samples, ray_indices=ray_indices, num_rays=num_rays
        )
        accumulation = self.renderer_accumulation(weights=weights, ray_indices=ray_indices, num_rays=num_rays)
        alive_ray_mask = accumulation.squeeze(-1) > 0

        outputs = {
            "rgb": rgb,
            "accumulation": accumulation,
            "depth": depth,
            "alive_ray_mask": alive_ray_mask,  # the rays we kept from sampler
            "num_samples_per_ray": packed_info[:, 1],
        }
        return outputs

    def get_metrics_dict(self, outputs, batch):
        image = batch["image"].to(self.device)
        metrics_dict = {}
        metrics_dict["psnr"] = self.psnr(outputs["rgb"], image)
        metrics_dict["num_samples_per_batch"] = outputs["num_samples_per_ray"].sum()
        metrics_dict["F_min"] = self.field.f.min()
        """ try:
            metrics_dict["F_25"] = torch.quantile(self.field.f, 0.25, interpolation='midpoint')
            metrics_dict["F_50"] = torch.quantile(self.field.f, 0.50, interpolation='midpoint')
            metrics_dict["F_75"] = torch.quantile(self.field.f, 0.75, interpolation='midpoint')
        except:
            pass """
        metrics_dict["F_max"] = self.field.f.max()
        metrics_dict["F_mean"] = self.field.f.mean()
        metrics_dict["F_std"] = self.field.f.std()
        metrics_dict["alpha_value"] = self.field.g_transition_alpha

        return metrics_dict

    def get_loss_dict(self, outputs, batch, metrics_dict=None):
        image = batch["image"].to(self.device)
        mask = outputs["alive_ray_mask"]
        rgb_loss = self.rgb_loss(image[mask], outputs["rgb"][mask])

        loss_dict = {"rgb_loss": rgb_loss}

        return loss_dict

    def get_image_metrics_and_images(
        self, outputs: Dict[str, torch.Tensor], batch: Dict[str, torch.Tensor]
    ) -> Tuple[Dict[str, float], Dict[str, torch.Tensor]]:

        image = batch["image"].to(self.device)
        rgb = outputs["rgb"]
        acc = colormaps.apply_colormap(outputs["accumulation"])
        depth = colormaps.apply_depth_colormap(
            outputs["depth"],
            accumulation=outputs["accumulation"],
        )
        alive_ray_mask = colormaps.apply_colormap(outputs["alive_ray_mask"])

        try:
            with torch.enable_grad():
                #import pdb;pdb.set_trace()
                radii, eikonal_loss = self.field.get_certified_radius()
                if radii is not None:
                    min_scalar = -4*self.field.sigma
                    max_scalar = 4*self.field.sigma

                    radiix = radii[self.field.f.shape[-1],:,:]
                    radiix_heatmap = (radiix - min_scalar)/(max_scalar - min_scalar)
                    radiix_heatmap = torch.clamp(radiix_heatmap,0,1)
                    radiix_heatmap = radiix_heatmap.detach().cpu()
                    radiix_heatmap = radiix_heatmap.unsqueeze(2)

                    radiiy = radii[:,self.field.f.shape[-1],:]
                    radiiy_heatmap = (radiiy - min_scalar)/(max_scalar - min_scalar)
                    radiiy_heatmap = torch.clamp(radiiy_heatmap,0,1)
                    radiiy_heatmap = radiiy_heatmap.detach().cpu()
                    radiiy_heatmap = radiiy_heatmap.unsqueeze(2)

                    radiiz = radii[:,:,self.field.f.shape[-1]]
                    radiiz_heatmap = (radiiz - min_scalar)/(max_scalar - min_scalar)
                    radiiz_heatmap = torch.clamp(radiiz_heatmap,0,1)
                    radiiz_heatmap = radiiz_heatmap.detach().cpu()
                    radiiz_heatmap = radiiz_heatmap.unsqueeze(2)

                    '''
                    plt.rcParams["figure.figsize"] = (12,12)
                    ax = sns.heatmap(radii[self.field.f.shape[-1],:,:].detach().cpu().numpy())
                    plt.savefig('foo.jpg', bbox_inches='tight')
                    orig_image = Image.open('foo.jpg')
                    im_matrix = np.array(orig_image)
                    radii_heatmap = torch.tensor(im_matrix,dtype=torch.float32)
                    plt.clf()
                    '''
                    
        except:
            print("no certified radii being calculated")

        combined_rgb = torch.cat([image, rgb], dim=1)
        combined_acc = torch.cat([acc], dim=1)
        combined_depth = torch.cat([depth], dim=1)
        combined_alive_ray_mask = torch.cat([alive_ray_mask], dim=1)

        # Switch images from [H, W, C] to [1, C, H, W] for metrics computations
        image = torch.moveaxis(image, -1, 0)[None, ...]
        rgb = torch.moveaxis(rgb, -1, 0)[None, ...]

        psnr = self.psnr(image, rgb)
        ssim = self.ssim(image, rgb)
        lpips = self.lpips(image, rgb)

        # all of these metrics will be logged as scalars
        metrics_dict = {"psnr": float(psnr.item()), "ssim": float(ssim), "lpips": float(lpips)}  # type: ignore
        # TODO(ethan): return an image dictionary

        images_dict = {
            "img": combined_rgb,
            "accumulation": combined_acc,
            "depth": combined_depth,
            "alive_ray_mask": combined_alive_ray_mask,
            "radiix_heatmap" : radiix_heatmap,
            "radiiy_heatmap" : radiiy_heatmap,
            "radiiz_heatmap" : radiiz_heatmap,
        }

        return metrics_dict, images_dict
