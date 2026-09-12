"""Render source HDF5 and officially decoded sample images side by side."""

import argparse
from pathlib import Path

import h5py
import numpy as np
from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    font_path = "C:/Windows/Fonts/msyh.ttc"
    font = ImageFont.truetype(font_path, 17)
    title_font = ImageFont.truetype(font_path, 24)
    samples = np.load(args.samples, allow_pickle=False)
    assert len(samples["images"]) == 9
    canvas = Image.new("RGB", (1152, 822), "#f5f7fa")
    draw = ImageDraw.Draw(canvas)
    draw.text((24, 18), "Panda / Lift 仿真测试片段：转换前后图像核对", font=title_font, fill="#172b4d")
    draw.text((24, 57), "每组：左为 HDF5 原图，右为官方加载器读取的 H.264 输出。84×84 图像放大2倍。", font=font, fill="#536179")
    draw.text((24, 84), "抽查每条任务的首、中、末帧；输出使用有损压缩，不声明像素无损。", font=font, fill="#536179")
    with h5py.File(args.input, "r") as handle:
        for index, decoded in enumerate(samples["images"]):
            episode = int(samples["episode_indices"][index])
            row = int(samples["frame_indices"][index])
            original = handle[f"data/demo_{episode}/obs/agentview_image"][row]
            left, top = 24 + (index % 3) * 376, 136 + (index // 3) * 224
            draw.text((left, top), f"demo_{episode}  /  frame {row}", font=font, fill="#172b4d")
            for offset, array in [(0, original), (180, decoded)]:
                canvas.paste(Image.fromarray(array).resize((168, 168), Image.Resampling.NEAREST), (left + offset, top + 29))
            draw.text((left, top + 199), "源图像", font=font, fill="#536179")
            draw.text((left + 180, top + 199), "官方输出读取", font=font, fill="#536179")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(args.output)
    print(args.output.resolve())


if __name__ == "__main__":
    main()
