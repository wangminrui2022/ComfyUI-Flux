# (c) City96 || Apache-2.0 (apache.org/licenses/LICENSE-2.0)
import torch
import gguf
import copy
import logging
import numpy as np

import comfy.sd
import comfy.utils
import comfy.model_management
import comfy.model_patcher
import folder_paths

from .ops import GGMLTensor, GGMLOps
from .dequant import dequantize_tensor

# Add a custom keys for files ending in .gguf
if "unet_gguf" not in folder_paths.folder_names_and_paths:
    orig = folder_paths.folder_names_and_paths.get("diffusion_models", folder_paths.folder_names_and_paths.get("unet", [[], set()]))
    folder_paths.folder_names_and_paths["unet_gguf"] = (orig[0], {".gguf"})

if "clip_gguf" not in folder_paths.folder_names_and_paths:
    orig = folder_paths.folder_names_and_paths.get("clip", [[], set()])
    folder_paths.folder_names_and_paths["clip_gguf"] = (orig[0], {".gguf"})

def gguf_sd_loader(path):
    """
    Read state dict as fake tensors
    """
    reader = gguf.GGUFReader(path)
    sd = {}
    dt = {}
    for tensor in reader.tensors:
        sd[str(tensor.name)] = GGMLTensor(
            torch.from_numpy(tensor.data), # mmap
            tensor_type = tensor.tensor_type,
            tensor_shape = torch.Size(
                np.flip(list(tensor.shape))
            )
        )
        dt[str(tensor.tensor_type)] = dt.get(str(tensor.tensor_type), 0) + 1

    # sanity check debug print
    print("\nggml_sd_loader:")
    for k,v in dt.items():
        print(f" {k:30}{v:3}")
    print("\n")
    return sd

# for remapping llama.cpp -> original key names
clip_sd_map = {
    "enc.": "encoder.",
    ".blk.": ".block.",
    "token_embd": "shared",
    "output_norm": "final_layer_norm",
    "attn_q": "layer.0.SelfAttention.q",
    "attn_k": "layer.0.SelfAttention.k",
    "attn_v": "layer.0.SelfAttention.v",
    "attn_o": "layer.0.SelfAttention.o",
    "attn_norm": "layer.0.layer_norm",
    "attn_rel_b": "layer.0.SelfAttention.relative_attention_bias",
    "ffn_up": "layer.1.DenseReluDense.wi_1",
    "ffn_down": "layer.1.DenseReluDense.wo",
    "ffn_gate": "layer.1.DenseReluDense.wi_0",
    "ffn_norm": "layer.1.layer_norm",
}
# weights that should be dequantized on load
clip_sd_dequant = {
    "shared.weight",
}

def gguf_clip_loader(path):
    raw_sd = gguf_sd_loader(path)
    assert "enc.blk.23.ffn_up.weight" in raw_sd, "Invalid Text Encoder!"
    sd = {}
    for k,v in raw_sd.items():
        for s,d in clip_sd_map.items():
            k = k.replace(s,d)
        if k in clip_sd_dequant:
            v = dequantize_tensor(v, torch.float32).to(torch.float16)
            v = GGMLTensor(v, tensor_type=gguf.GGMLQuantizationType.F16, tensor_shape=v.shape)
        sd[k] = v
    return sd

# TODO: Temporary fix for now
class GGUFModelPatcher(comfy.model_patcher.ModelPatcher):
    def calculate_weight(self, patches, weight, key):
        if isinstance(weight, GGMLTensor):
            qtype = weight.tensor_type
            # TODO: don't even store these in a custom format
            if qtype in [gguf.GGMLQuantizationType.F32, gguf.GGMLQuantizationType.F16]:
                return super().calculate_weight(patches, weight, key)
            else:
                weight.patches.append((super().calculate_weight, patches, key))
                return weight
        else:
            return super().calculate_weight(patches, weight, key)

    def clone(self, *args, **kwargs):
        n = GGUFModelPatcher(self.model, self.load_device, self.offload_device, self.size, weight_inplace_update=self.weight_inplace_update)
        n.patches = {}
        for k in self.patches:
            n.patches[k] = self.patches[k][:]
        n.patches_uuid = self.patches_uuid

        n.object_patches = self.object_patches.copy()
        n.model_options = copy.deepcopy(self.model_options)
        n.backup = self.backup
        n.object_patches_backup = self.object_patches_backup
        return n

class UnetLoaderGGUF:
    @classmethod
    def INPUT_TYPES(s):
        unet_names = [x for x in folder_paths.get_filename_list("unet_gguf")]
        return {
            "required": {
                "unet_name": (unet_names,),
            }
        }

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "load_unet"
    CATEGORY = "bootleg"
    TITLE = "Unet Loader (GGUF)"

    def load_unet(self, unet_name):
        unet_path = folder_paths.get_full_path("unet", unet_name)
        sd = gguf_sd_loader(unet_path)
        model = comfy.sd.load_diffusion_model_state_dict(
            sd, model_options={"custom_operations": GGMLOps}
        )
        if model is None:
            logging.error("ERROR UNSUPPORTED UNET {}".format(unet_path))
            raise RuntimeError("ERROR: Could not detect model type of: {}".format(unet_path))
        model = GGUFModelPatcher.clone(model)
        return (model,)

clip_name_dict = {
    "stable_diffusion": comfy.sd.CLIPType.STABLE_DIFFUSION,
    "stable_cascade": comfy.sd.CLIPType.STABLE_CASCADE,
    "stable_audio": comfy.sd.CLIPType.STABLE_AUDIO,
    "sdxl": comfy.sd.CLIPType.STABLE_DIFFUSION,
    "sd3": comfy.sd.CLIPType.SD3,
    "flux": comfy.sd.CLIPType.FLUX,
}

class CLIPLoaderGGUF:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "clip_name": (s.get_filename_list(),),
                "type": (["stable_diffusion", "stable_cascade", "sd3", "stable_audio"],),
            }
        }

    RETURN_TYPES = ("CLIP",)
    FUNCTION = "load_clip"
    CATEGORY = "bootleg"
    TITLE = "CLIPLoader (GGUF)"

    @classmethod
    def get_filename_list(s):
        files = []
        files += folder_paths.get_filename_list("clip")
        files += folder_paths.get_filename_list("clip_gguf")
        return sorted(files)

    def load_data(self, ckpt_paths):
        clip_data = []
        for p in ckpt_paths:
            if p.endswith(".gguf"):
                clip_data.append(gguf_clip_loader(p))
            else:
                sd = comfy.utils.load_torch_file(p, safe_load=True)
                clip_data.append(
                    {k:GGMLTensor(v, tensor_type=gguf.GGMLQuantizationType.F16, tensor_shape=v.shape) for k,v in sd.items()}
                )
        return clip_data

    def load_patcher(self, clip_paths, clip_type, clip_data):
        clip = comfy.sd.load_text_encoder_state_dicts(
            clip_type = clip_type,
            state_dicts = clip_data,
            model_options = {"custom_operations": GGMLOps},
            embedding_directory = folder_paths.get_folder_paths("embeddings"),
        )
        clip.patcher = GGUFModelPatcher.clone(clip.patcher)

        # for some reason this is just missing in some SAI checkpoints
        if getattr(clip.cond_stage_model, "clip_l", None) is not None:
            if clip.cond_stage_model.clip_l.transformer.text_projection.weight.tensor_shape == None:
                clip.cond_stage_model.clip_l.transformer.text_projection = comfy.ops.manual_cast.Linear(768, 768)
        if getattr(clip.cond_stage_model, "clip_g", None) is not None:
            if clip.cond_stage_model.clip_g.transformer.text_projection.weight.tensor_shape == None:
                clip.cond_stage_model.clip_g.transformer.text_projection = comfy.ops.manual_cast.Linear(1280, 1280)

        return clip

    def load_clip(self, clip_name, type="stable_diffusion"):
        clip_path = folder_paths.get_full_path("clip", clip_name)
        clip_type = clip_name_dict.get(type, comfy.sd.CLIPType.STABLE_DIFFUSION)
        return (self.load_patcher([clip_path], clip_type, self.load_data([clip_path])),)

class DualCLIPLoaderGGUF(CLIPLoaderGGUF):
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "clip_name1": (s.get_filename_list(), ),
                "clip_name2": (s.get_filename_list(), ),
                "type": (["sdxl", "sd3", "flux"], ),
            }
        }

    TITLE = "DualCLIPLoader (GGUF)"

    def load_clip(self, clip_name1, clip_name2, type):
        clip_path1 = folder_paths.get_full_path("clip", clip_name1)
        clip_path2 = folder_paths.get_full_path("clip", clip_name2)
        clip_paths = [clip_path1, clip_path2]
        clip_type = clip_name_dict.get(type, comfy.sd.CLIPType.STABLE_DIFFUSION)
        return (self.load_patcher(clip_paths, clip_type, self.load_data(clip_paths)),)

class TripleCLIPLoaderGGUF(CLIPLoaderGGUF):
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "clip_name1": (s.get_filename_list(), ),
                "clip_name2": (s.get_filename_list(), ),
                "clip_name3": (s.get_filename_list(), ),
            }
        }

    TITLE = "TripleCLIPLoader (GGUF)"

    def load_clip(self, clip_name1, clip_name2, clip_name3, type="sd3"):
        clip_path1 = folder_paths.get_full_path("clip", clip_name1)
        clip_path2 = folder_paths.get_full_path("clip", clip_name2)
        clip_path3 = folder_paths.get_full_path("clip", clip_name3)
        clip_paths = [clip_path1, clip_path2, clip_path3]
        clip_type = clip_name_dict.get(type, comfy.sd.CLIPType.STABLE_DIFFUSION)
        return (self.load_patcher(clip_paths, clip_type, self.load_data(clip_paths)),)

NODE_CLASS_MAPPINGS = {
    "UnetLoaderGGUF": UnetLoaderGGUF,
    "CLIPLoaderGGUF": CLIPLoaderGGUF,
    "DualCLIPLoaderGGUF": DualCLIPLoaderGGUF,
    "TripleCLIPLoaderGGUF": TripleCLIPLoaderGGUF,
}
