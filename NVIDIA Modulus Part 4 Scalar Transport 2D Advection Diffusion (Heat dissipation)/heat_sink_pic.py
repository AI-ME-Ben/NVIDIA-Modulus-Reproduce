# SPDX-FileCopyrightText: Copyright (c) 2023 - 2024 NVIDIA CORPORATION & AFFILIATES.
# SPDX-FileCopyrightText: All rights reserved.
# SPDX-License-Identifier: Apache-2.0
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

import os
import warnings

import torch
import numpy as np
from sympy import Symbol, Eq

import modulus.sym
from modulus.sym.hydra import to_absolute_path, instantiate_arch, ModulusConfig
from modulus.sym.solver import Solver
from modulus.sym.domain import Domain
from modulus.sym.geometry.primitives_2d import Rectangle, Line, Channel2D
from modulus.sym.utils.sympy.functions import parabola
from modulus.sym.utils.io import csv_to_dict
from modulus.sym.eq.pdes.navier_stokes import NavierStokes, GradNormal
from modulus.sym.eq.pdes.basic import NormalDotVec
from modulus.sym.eq.pdes.turbulence_zero_eq import ZeroEquation
from modulus.sym.eq.pdes.advection_diffusion import AdvectionDiffusion
from modulus.sym.domain.constraint import (
    PointwiseBoundaryConstraint,
    PointwiseInteriorConstraint,
    IntegralBoundaryConstraint,
)
from modulus.sym.domain.monitor import PointwiseMonitor
from modulus.sym.domain.validator import PointwiseValidator
from modulus.sym.key import Key
from modulus.sym.node import Node
from modulus.sym.geometry import Parameterization, Parameter

import scipy.interpolate
import matplotlib.pyplot as plt
from modulus.sym.utils.io.plotter import ValidatorPlotter, InferencerPlotter

# Define custom class
class CustomValidatorPlotter(ValidatorPlotter):

    def __call__(self, invar, true_outvar, pred_outvar):
        "Custom plotting function for validator"

        # Get input variables
        x, y = invar["x"][:, 0], invar["y"][:, 0]
        extent = (x.min(), x.max(), y.min(), y.max())

        # Extract p, u, v, nu, c from true and predicted outputs
        p_true, u_true, v_true, nu_true, c_true = (
            true_outvar["p"][:, 0],
            true_outvar["u"][:, 0],
            true_outvar["v"][:, 0],
            true_outvar["nu"][:, 0],
            true_outvar["c"][:, 0]*273.15,
        )
        p_pred, u_pred, v_pred, nu_pred, c_pred = (
            pred_outvar["p"][:, 0],
            pred_outvar["u"][:, 0],
            pred_outvar["v"][:, 0],
            pred_outvar["nu"][:, 0],
            pred_outvar["c"][:, 0]*273.15,
        )

        # Interpolate all variables
        (
            p_true, u_true, v_true, nu_true, c_true,
            p_pred, u_pred, v_pred, nu_pred, c_pred
        ) = CustomValidatorPlotter.interpolate_output(
            x, y, [p_true, u_true, v_true, nu_true, c_true, p_pred, u_pred, v_pred, nu_pred, c_pred], extent
        )

        # Compute differences
        p_diff = p_true - p_pred
        u_diff = u_true - u_pred
        v_diff = v_true - v_pred
        nu_diff = nu_true - nu_pred
        c_diff = c_true - c_pred

        # Define color bar limits (ONLY for true and predicted values)
        colorbar_limits = {
            "p": (-1, 9),
            "u": (0, 2.2),
            "v": (-1.2, 1.2),
            "nu": (0.01, 0.04),
            "c": (0, 55),  
        }

        # Create plot (5 rows, 3 columns) with updated size
        f, axes = plt.subplots(5, 3, figsize=(20, 10), dpi=100)
        plt.suptitle("Heat sink 2D: PINN vs True Solution")

        # Titles and data
        titles = [
            "Modulus: p", "OpenFOAM: p", "Difference: p",
            "Modulus: u", "OpenFOAM: u", "Difference: u",
            "Modulus: v", "OpenFOAM: v", "Difference: v",
            "Modulus: nu", "OpenFOAM: nu", "Difference: nu",
            "Modulus: c", "OpenFOAM: c", "Difference: c",
        ]
        data = [
            p_pred, p_true, p_diff,
            u_pred, u_true, u_diff,
            v_pred, v_true, v_diff,
            nu_pred, nu_true, nu_diff,
            c_pred, c_true, c_diff,
        ]
        variables = ["p", "p", "None", "u", "u", "None", "v", "v", "None", "nu", "nu", "None", "c", "c", "None"]
        
            
        # Loop through subplots and apply limits (except for difference plots)
        for i, ax in enumerate(axes.flat):
            var = variables[i]  # Get variable type
            if var != "None":  # Apply color limits only to true & predicted values
                im = ax.imshow(data[i].T, origin="lower", extent=extent, cmap="jet", 
                               vmin=colorbar_limits[var][0], vmax=colorbar_limits[var][1])
            else:  # No fixed limits for difference plots
                im = ax.imshow(data[i].T, origin="lower", extent=extent, cmap="jet")

            ax.set_title(titles[i])
            ax.set_xlabel("x")
            ax.set_ylabel("y")
            plt.colorbar(im, ax=ax)

        plt.tight_layout()

        return [(f, "custom_plot")]

    # Define heat sink mask (adjust based on actual domain)
    @staticmethod
    def heat_sink_mask(xi, yi):
        """Returns a boolean mask where heat sink regions should be set to NaN"""
        mask = np.zeros_like(xi, dtype=bool)  
    
        heat_sink_x = -1  # Heat sink X position
        heat_sink_y_start = -0.3  # First fin Y position
        fin_thickness = 0.1
        nr_fins = 3
        gap = 0.15 + 0.1  # Fin spacing
        length = 1.0  # Heat sink length
    
        for j in range(nr_fins):
            fin_y = heat_sink_y_start + j * gap
            mask |= ((xi >= heat_sink_x) & (xi <= heat_sink_x + length) &   
                     (yi >= fin_y) & (yi <= fin_y + fin_thickness))
    
        return mask  

    @staticmethod
    def interpolate_output(x, y, values, extent):
        """Interpolates irregular points onto a mesh"""
        xi, yi = np.meshgrid(
            np.linspace(extent[0], extent[1], 100),
            np.linspace(extent[2], extent[3], 100),
            indexing="ij",
        )
        
        mask = CustomValidatorPlotter.heat_sink_mask(xi, yi)
        
        interpolated_values = [
            scipy.interpolate.griddata((x, y), value, (xi, yi), method='linear') for value in values
        ]
        
        interpolated_values = [np.nan_to_num(val, nan=np.nan) for val in interpolated_values]
        
        for i in range(len(interpolated_values)):
            interpolated_values[i][mask] = np.nan

        return interpolated_values


@modulus.sym.main(config_path="conf", config_name="config")
def run(cfg: ModulusConfig) -> None:
    # params for domain
    channel_length = (-2.5, 2.5)
    channel_width = (-0.5, 0.5)
    heat_sink_origin = (-1, -0.3)
    nr_heat_sink_fins = 3
    gap = 0.15 + 0.1
    heat_sink_length = 1.0
    heat_sink_fin_thickness = 0.1
    inlet_vel = 1.5
    heat_sink_temp = 350
    base_temp = 293.498
    nu = 0.01
    diffusivity = 0.01 / 5

    # define sympy varaibles to parametize domain curves
    x, y = Symbol("x"), Symbol("y")

    # define geometry
    channel = Channel2D(
        (channel_length[0], channel_width[0]), (channel_length[1], channel_width[1])
    )
    heat_sink = Rectangle(
        heat_sink_origin,
        (
            heat_sink_origin[0] + heat_sink_length,
            heat_sink_origin[1] + heat_sink_fin_thickness,
        ),
    )
    for i in range(1, nr_heat_sink_fins):
        heat_sink_origin = (heat_sink_origin[0], heat_sink_origin[1] + gap)
        fin = Rectangle(
            heat_sink_origin,
            (
                heat_sink_origin[0] + heat_sink_length,
                heat_sink_origin[1] + heat_sink_fin_thickness,
            ),
        )
        heat_sink = heat_sink + fin
    geo = channel - heat_sink

    inlet = Line(
        (channel_length[0], channel_width[0]), (channel_length[0], channel_width[1]), -1
    )
    outlet = Line(
        (channel_length[1], channel_width[0]), (channel_length[1], channel_width[1]), 1
    )

    x_pos = Parameter("x_pos")
    integral_line = Line(
        (x_pos, channel_width[0]),
        (x_pos, channel_width[1]),
        1,
        parameterization=Parameterization({x_pos: channel_length}),
    )

    # make list of nodes to unroll graph on
    ze = ZeroEquation(
        nu=nu, rho=1.0, dim=2, max_distance=(channel_width[1] - channel_width[0]) / 2
    )
    ns = NavierStokes(nu=ze.equations["nu"], rho=1.0, dim=2, time=False)
    ade = AdvectionDiffusion(T="c", rho=1.0, D=diffusivity, dim=2, time=False)
    gn_c = GradNormal("c", dim=2, time=False)
    normal_dot_vel = NormalDotVec(["u", "v"])
    flow_net = instantiate_arch(
        input_keys=[Key("x"), Key("y")],
        output_keys=[Key("u"), Key("v"), Key("p")],
        cfg=cfg.arch.fully_connected,
    )
    heat_net = instantiate_arch(
        input_keys=[Key("x"), Key("y")],
        output_keys=[Key("c")],
        cfg=cfg.arch.fully_connected,
    )

    nodes = (
        ns.make_nodes()
        + ze.make_nodes()
        + ade.make_nodes(detach_names=["u", "v"])
        + gn_c.make_nodes()
        + normal_dot_vel.make_nodes()
        + [flow_net.make_node(name="flow_network")]
        + [heat_net.make_node(name="heat_network")]
    )

    # make domain
    domain = Domain()

    # inlet
    inlet_parabola = parabola(
        y, inter_1=channel_width[0], inter_2=channel_width[1], height=inlet_vel
    )
    inlet = PointwiseBoundaryConstraint(
        nodes=nodes,
        geometry=inlet,
        outvar={"u": inlet_parabola, "v": 0, "c": 0},
        batch_size=cfg.batch_size.inlet,
    )
    domain.add_constraint(inlet, "inlet")

    # outlet
    outlet = PointwiseBoundaryConstraint(
        nodes=nodes,
        geometry=outlet,
        outvar={"p": 0},
        batch_size=cfg.batch_size.outlet,
    )
    domain.add_constraint(outlet, "outlet")

    # heat_sink wall
    hs_wall = PointwiseBoundaryConstraint(
        nodes=nodes,
        geometry=heat_sink,
        outvar={"u": 0, "v": 0, "c": (heat_sink_temp - base_temp) / 273.15},
        batch_size=cfg.batch_size.hs_wall,
    )
    domain.add_constraint(hs_wall, "heat_sink_wall")

    # channel wall
    channel_wall = PointwiseBoundaryConstraint(
        nodes=nodes,
        geometry=channel,
        outvar={"u": 0, "v": 0, "normal_gradient_c": 0},
        batch_size=cfg.batch_size.channel_wall,
    )
    domain.add_constraint(channel_wall, "channel_wall")

    # interior flow
    interior_flow = PointwiseInteriorConstraint(
        nodes=nodes,
        geometry=geo,
        outvar={"continuity": 0, "momentum_x": 0, "momentum_y": 0},
        batch_size=cfg.batch_size.interior_flow,
        compute_sdf_derivatives=True,
        lambda_weighting={
            "continuity": Symbol("sdf"),
            "momentum_x": Symbol("sdf"),
            "momentum_y": Symbol("sdf"),
        },
    )
    domain.add_constraint(interior_flow, "interior_flow")

    # interior heat
    interior_heat = PointwiseInteriorConstraint(
        nodes=nodes,
        geometry=geo,
        outvar={"advection_diffusion_c": 0},
        batch_size=cfg.batch_size.interior_heat,
        lambda_weighting={
            "advection_diffusion_c": 1.0,
        },
    )
    domain.add_constraint(interior_heat, "interior_heat")

    # integral continuity
    def integral_criteria(invar, params):
        sdf = geo.sdf(invar, params)
        return np.greater(sdf["sdf"], 0)

    integral_continuity = IntegralBoundaryConstraint(
        nodes=nodes,
        geometry=integral_line,
        outvar={"normal_dot_vel": 1},
        batch_size=cfg.batch_size.num_integral_continuity,
        integral_batch_size=cfg.batch_size.integral_continuity,
        lambda_weighting={"normal_dot_vel": 0.1},
        criteria=integral_criteria,
    )
    domain.add_constraint(integral_continuity, "integral_continuity")

    # add validation data
    file_path = "openfoam/heat_sink_zeroEq_Pr5_mesh20.csv"
    if os.path.exists(to_absolute_path(file_path)):
        mapping = {
            "Points:0": "x",
            "Points:1": "y",
            "U:0": "u",
            "U:1": "v",
            "p": "p",
            "d": "sdf",
            "nuT": "nu",
            "T": "c",
        }
        openfoam_var = csv_to_dict(to_absolute_path(file_path), mapping)
        openfoam_var["nu"] += nu
        openfoam_var["c"] += -base_temp
        openfoam_var["c"] /= 273.15
        openfoam_invar_numpy = {
            key: value
            for key, value in openfoam_var.items()
            if key in ["x", "y", "sdf"]
        }
        openfoam_outvar_numpy = {
            key: value
            for key, value in openfoam_var.items()
            if key in ["u", "v", "nu" , "p", "c"]  # add "nu"
        }
        openfoam_validator = PointwiseValidator(
            nodes=nodes,
            invar=openfoam_invar_numpy,
            true_outvar=openfoam_outvar_numpy,
            plotter=CustomValidatorPlotter(), # add
            batch_size=1024,# add
            requires_grad=True,# add
        )
        domain.add_validator(openfoam_validator)
    else:
        warnings.warn(
            f"Directory {file_path} does not exist. Will skip adding validators. Please download the additional files from NGC https://catalog.ngc.nvidia.com/orgs/nvidia/teams/modulus/resources/modulus_sym_examples_supplemental_materials"
        )

    # monitors for force, residuals and temperature
    global_monitor = PointwiseMonitor(
        geo.sample_interior(100),
        output_names=["continuity", "momentum_x", "momentum_y"],
        metrics={
            "mass_imbalance": lambda var: torch.sum(
                var["area"] * torch.abs(var["continuity"])
            ),
            "momentum_imbalance": lambda var: torch.sum(
                var["area"]
                * (torch.abs(var["momentum_x"]) + torch.abs(var["momentum_y"]))
            ),
        },
        nodes=nodes,
        requires_grad=True,
    )
    domain.add_monitor(global_monitor)

    force = PointwiseMonitor(
        heat_sink.sample_boundary(100),
        output_names=["p"],
        metrics={
            "force_x": lambda var: torch.sum(var["normal_x"] * var["area"] * var["p"]),
            "force_y": lambda var: torch.sum(var["normal_y"] * var["area"] * var["p"]),
        },
        nodes=nodes,
    )
    domain.add_monitor(force)

    peakT = PointwiseMonitor(
        heat_sink.sample_boundary(100),
        output_names=["c"],
        metrics={"peakT": lambda var: torch.max(var["c"])},
        nodes=nodes,
    )
    domain.add_monitor(peakT)

    # make solver
    slv = Solver(cfg, domain)

    # start solver
    slv.solve()


if __name__ == "__main__":
    run()
