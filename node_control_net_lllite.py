import math
import torch
import os

import comfy

CURRENT_DIR = os.path.dirname(os.path.realpath(__file__))


def get_file_list(path):
    return [file for file in os.listdir(path) if file != "put_models_here.txt"]


def extra_options_to_module_prefix(extra_options):
    # extra_options = {'transformer_index': 2, 'block_index': 8, 'original_shape': [2, 4, 128, 128], 'block': ('input', 7), 'n_heads': 20, 'dim_head': 64}

    # block is: [('input', 4), ('input', 5), ('input', 7), ('input', 8), ('middle', 0),
    #   ('output', 0), ('output', 1), ('output', 2), ('output', 3), ('output', 4), ('output', 5)]
    # transformer_index is: [0, 1, 2, 3, 4, 5, 6, 7, 8], for each block
    # block_index is: 0-1 or 0-9, depends on the block
    # input 7 and 8, middle has 10 blocks

    # make module name from extra_options
    block = extra_options["block"]
    block_index = extra_options["block_index"]
    if block[0] == "input":
        module_pfx = f"lllite_unet_input_blocks_{block[1]}_1_transformer_blocks_{block_index}"
    elif block[0] == "middle":
        module_pfx = f"lllite_unet_middle_block_1_transformer_blocks_{block_index}"
    elif block[0] == "output":
        module_pfx = f"lllite_unet_output_blocks_{block[1]}_1_transformer_blocks_{block_index}"
    else:
        raise Exception("invalid block name")
    return module_pfx


def load_control_net_lllite_patch(path, cond_image, multiplier, sigma_start, sigma_end):
    # load weights
    ctrl_sd = comfy.utils.load_torch_file(path, safe_load=True)

    # split each weights for each module
    module_weights = {}
    for key, value in ctrl_sd.items():
        fragments = key.split(".")
        module_name = fragments[0]
        weight_name = ".".join(fragments[1:])

        if module_name not in module_weights:
            module_weights[module_name] = {}
        module_weights[module_name][weight_name] = value

    # load each module
    modules = {}
    for module_name, weights in module_weights.items():
        # ここの自動判定を何とかしたい
        if "conditioning1.4.weight" in weights:
            depth = 3
        elif weights["conditioning1.2.weight"].shape[-1] == 4:
            depth = 2
        else:
            depth = 1

        module = LLLiteModule(
            name=module_name,
            is_conv2d=weights["down.0.weight"].ndim == 4,
            in_dim=weights["down.0.weight"].shape[1],
            depth=depth,
            cond_emb_dim=weights["conditioning1.0.weight"].shape[0] * 2,
            mlp_dim=weights["down.0.weight"].shape[0],
            multiplier=multiplier,
        )
        info = module.load_state_dict(weights)
        modules[module_name] = module
        if len(modules) == 1:
            module.is_first = True

    print(f"loaded {path} successfully, {len(modules)} modules")

    # cond imageをセットする
    cond_image = cond_image.permute(0, 3, 1, 2)  # b,h,w,3 -> b,3,h,w
    cond_image = cond_image * 2.0 - 1.0  # 0-1 -> -1-+1

    for module in modules.values():
        module.set_cond_image(cond_image)

    class control_net_lllite_patch:
        def __init__(self, modules):
            self.modules = modules

        def __call__(self, q, k, v, extra_options):
            module_pfx = extra_options_to_module_prefix(extra_options)
            sigma = extra_options["sigmas"].detach().cpu()[0].item() if 'sigmas' in extra_options else 999999999.9

            is_attn1 = q.shape[-1] == k.shape[-1]  # self attention
            if is_attn1:
                module_pfx = module_pfx + "_attn1"
            else:
                module_pfx = module_pfx + "_attn2"

            module_pfx_to_q = module_pfx + "_to_q"
            module_pfx_to_k = module_pfx + "_to_k"
            module_pfx_to_v = module_pfx + "_to_v"

            if sigma <= sigma_start and sigma >= sigma_end:
                if module_pfx_to_q in self.modules:
                    q = q + self.modules[module_pfx_to_q](q)
                if module_pfx_to_k in self.modules:
                    k = k + self.modules[module_pfx_to_k](k)
                if module_pfx_to_v in self.modules:
                    v = v + self.modules[module_pfx_to_v](v)

            return q, k, v

        def to(self, device):
            for d in self.modules.keys():
                self.modules[d] = self.modules[d].to(device)
            return self

    return control_net_lllite_patch(modules)


class LLLiteModule(torch.nn.Module):
    def __init__(
        self,
        name: str,
        is_conv2d: bool,
        in_dim: int,
        depth: int,
        cond_emb_dim: int,
        mlp_dim: int,
        multiplier: int,
    ):
        super().__init__()
        self.name = name
        self.is_conv2d = is_conv2d
        self.multiplier = multiplier
        self.is_first = False

        modules = []
        modules.append(torch.nn.Conv2d(3, cond_emb_dim // 2, kernel_size=4, stride=4, padding=0))  # to latent (from VAE) size*2
        if depth == 1:
            modules.append(torch.nn.ReLU(inplace=True))
            modules.append(torch.nn.Conv2d(cond_emb_dim // 2, cond_emb_dim, kernel_size=2, stride=2, padding=0))
        elif depth == 2:
            modules.append(torch.nn.ReLU(inplace=True))
            modules.append(torch.nn.Conv2d(cond_emb_dim // 2, cond_emb_dim, kernel_size=4, stride=4, padding=0))
        elif depth == 3:
            # kernel size 8は大きすぎるので、4にする / kernel size 8 is too large, so set it to 4
            modules.append(torch.nn.ReLU(inplace=True))
            modules.append(torch.nn.Conv2d(cond_emb_dim // 2, cond_emb_dim // 2, kernel_size=4, stride=4, padding=0))
            modules.append(torch.nn.ReLU(inplace=True))
            modules.append(torch.nn.Conv2d(cond_emb_dim // 2, cond_emb_dim, kernel_size=2, stride=2, padding=0))

        self.conditioning1 = torch.nn.Sequential(*modules)

        if self.is_conv2d:
            self.down = torch.nn.Sequential(
                torch.nn.Conv2d(in_dim, mlp_dim, kernel_size=1, stride=1, padding=0),
                torch.nn.ReLU(inplace=True),
            )
            self.mid = torch.nn.Sequential(
                torch.nn.Conv2d(mlp_dim + cond_emb_dim, mlp_dim, kernel_size=1, stride=1, padding=0),
                torch.nn.ReLU(inplace=True),
            )
            self.up = torch.nn.Sequential(
                torch.nn.Conv2d(mlp_dim, in_dim, kernel_size=1, stride=1, padding=0),
            )
        else:
            self.down = torch.nn.Sequential(
                torch.nn.Linear(in_dim, mlp_dim),
                torch.nn.ReLU(inplace=True),
            )
            self.mid = torch.nn.Sequential(
                torch.nn.Linear(mlp_dim + cond_emb_dim, mlp_dim),
                torch.nn.ReLU(inplace=True),
            )
            self.up = torch.nn.Sequential(
                torch.nn.Linear(mlp_dim, in_dim),
            )

        self.depth = depth
        self.cond_image = None
        self.cond_emb = None

    # @torch.inference_mode()
    def set_cond_image(self, cond_image):
        # print("set_cond_image", self.name)
        self.cond_image = cond_image
        self.cond_emb = None

    def forward(self, x):
        if self.cond_emb is None:
            # print(f"cond_emb is None, {self.name}")
            cx = self.conditioning1(self.cond_image.to(x.device, dtype=x.dtype))
            if not self.is_conv2d:
                # reshape / b,c,h,w -> b,h*w,c
                n, c, h, w = cx.shape
                cx = cx.view(n, c, h * w).permute(0, 2, 1)
            self.cond_emb = cx

        cx = self.cond_emb
        # print(f"forward {self.name}, {cx.shape}, {x.shape}")

        # uncond/condでxはバッチサイズが2倍
        if x.shape[0] != cx.shape[0]:
            if self.is_conv2d:
                cx = cx.repeat(x.shape[0] // cx.shape[0], 1, 1, 1)
            else:
                # print("x.shape[0] != cx.shape[0]", x.shape[0], cx.shape[0])
                cx = cx.repeat(x.shape[0] // cx.shape[0], 1, 1)

        cx = torch.cat([cx, self.down(x)], dim=1 if self.is_conv2d else 2)
        cx = self.mid(cx)
        cx = self.up(cx)
        return cx * self.multiplier


class LLLiteLoader:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL",),
                "model_name": (get_file_list(os.path.join(CURRENT_DIR, "models")),),
                "cond_image": ("IMAGE",),
                "strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "start_percent": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.001}),
                "end_percent": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.001}),
            }
        }

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "load_lllite"
    CATEGORY = "loaders"

    def load_lllite(self, model, model_name, cond_image, strength, start_percent, end_percent):
        # cond_image is b,h,w,3, 0-1

        model_path = os.path.join(CURRENT_DIR, os.path.join(CURRENT_DIR, "models", model_name))

        model_lllite = model.clone()

        sigma_start = model_lllite.model.model_sampling.percent_to_sigma(start_percent)
        sigma_end = model_lllite.model.model_sampling.percent_to_sigma(end_percent)

        patch = load_control_net_lllite_patch(model_path, cond_image, strength, sigma_start, sigma_end)
        if patch is not None:
            model_lllite.set_model_attn1_patch(patch)
            model_lllite.set_model_attn2_patch(patch)

        return (model_lllite,)


NODE_CLASS_MAPPINGS = {"LLLiteLoader": LLLiteLoader}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LLLiteLoader": "Load LLLite",
}
