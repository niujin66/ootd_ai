"""Local PIXIE geometry smoke test. Requires the user's licensed model files."""

import argparse
import json
import os
from pathlib import Path
import sys
import time
import zipfile

import numpy as np
from PIL import Image, ImageOps
import torch
import trimesh


ROOT = Path(os.environ.get('OOTD_PIXIE_ROOT', Path(__file__).resolve().parents[2] / '3d模型测试'))
SOURCE = ROOT / 'pixie-5c8879b'
SAMPLE = SOURCE / 'TestSamples/body/GettyImages-545880635.jpg'
sys.path.insert(0, str(SOURCE))

from pixielib.pixie import PIXIE
from pixielib.utils.config import cfg


def prepare_config():
    data = SOURCE / 'data'
    data.mkdir(exist_ok=True)
    required = ('smplx_extra_joints.yaml', 'SMPLX_to_J14.pkl')
    with zipfile.ZipFile(ROOT / 'utilities.zip') as archive:
        for name in required:
            (data / name).write_bytes(archive.read(name))
    config = cfg.clone()
    config.pretrained_modelpath = str(ROOT / 'pixie_model.tar')
    config.model.smplx_model_path = str(ROOT / 'SMPLX_NEUTRAL_2020.npz')
    config.model.use_tex = False
    return config


def input_image(path, bbox, device):
    with Image.open(path) as raw:
        image = ImageOps.exif_transpose(raw).convert('RGB')
    left, top, right, bottom = bbox or (0, 0, image.width, image.height)
    if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
        raise ValueError('Bounding box must be inside the oriented input image')
    width = round(1.1 * max(right - left, bottom - top))
    x, y = (left + right - width) // 2, (top + bottom - width) // 2
    padded = Image.new('RGB', (width, width), (195, 195, 195))
    padded.paste(image, (-x, -y))

    def tensor(size):
        pixels = np.array(padded.resize((size, size), Image.Resampling.BILINEAR))
        return torch.from_numpy(pixels).permute(2, 0, 1).unsqueeze(0).to(device, torch.float32) / 255

    return {'body': {'image': tensor(224), 'image_hd': tensor(1024)}}


def export_mesh(vertices, faces, output, name, camera_pose=False):
    points = vertices[0].detach().cpu().numpy().copy()
    if not np.isfinite(points).all() or not np.ptp(points, axis=0).all():
        raise RuntimeError('Model returned invalid geometry')
    if camera_pose:
        # PIXIE's posed result is in camera coordinates. Rotate 180° about X
        # to use Y-up and front +Z, as for the canonical SMPL-X mesh.
        points[:, 1:] *= -1
    points[:, 1] -= points[:, 1].min()
    mesh = trimesh.Trimesh(vertices=points, faces=faces, process=False)
    mesh.visual.face_colors = [155, 181, 208, 255]
    mesh.export(output / f'{name}.obj')
    mesh.export(output / f'{name}.glb')
    return {'vertices': len(mesh.vertices), 'triangles': len(mesh.faces), 'bounds': mesh.bounds.tolist()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', type=Path, default=SAMPLE)
    parser.add_argument('--bbox', type=int, nargs=4, metavar=('L', 'T', 'R', 'B'))
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs' / 'pixie-official-sample')
    parser.add_argument('--device', default='cuda:0')
    args = parser.parse_args()
    bbox = args.bbox
    if args.image.resolve() == SAMPLE.resolve() and bbox is None:
        bbox = (162, 44, 332, 678)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.device.startswith('cuda'):
        torch.cuda.init()
        torch.cuda.reset_peak_memory_stats(args.device)
    started = time.perf_counter()
    print('Loading PIXIE and SMPL-X...', flush=True)
    model = PIXIE(config=prepare_config(), device=args.device)
    print('Running full-body inference...', flush=True)
    with torch.inference_mode():
        data = input_image(args.image, bbox, args.device)
        params = model.encode(data, threthold=True, keep_local=True, copy_and_paste=False)['body']
        pose_mesh = model.decode(params, param_type='body')['vertices']
        fixed_mesh = model.decode_Tpose(params)
    faces = model.smplx.faces_tensor.detach().cpu().numpy()
    report = {
        'input': args.image.name,
        'device': args.device,
        'torch': torch.__version__,
        'pose': export_mesh(pose_mesh, faces, args.output, 'person-posed', camera_pose=True),
        'fixed_pose': export_mesh(fixed_mesh, faces, args.output, 'person-tpose'),
        'elapsed_seconds': round(time.perf_counter() - started, 2),
    }
    if args.device.startswith('cuda'):
        report['peak_allocated_mib'] = round(torch.cuda.max_memory_allocated(args.device) / 1024**2)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    (args.output / 'run.json').write_text(text + '\n', encoding='utf-8')
    print(text, flush=True)


if __name__ == '__main__':
    main()
