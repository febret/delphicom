"""Compare a reference image with a render of the generated model.

Robust to camera/pose differences: compares colour distributions (HSV
histograms), mean colour and a coarse edge/shape-agnostic palette overlap,
then writes a side-by-side montage.

Usage: python compare_images.py <reference.png> <render.png> <out_montage.png>
"""
import sys
import numpy as np
from PIL import Image


def load_rgb(path, size=256):
    im = Image.open(path).convert('RGB').resize((size, size), Image.LANCZOS)
    return np.asarray(im).astype(np.float32) / 255.0


def hsv_hist(rgb, bins=(12, 6, 6)):
    im = Image.fromarray((rgb * 255).astype(np.uint8)).convert('HSV')
    a = np.asarray(im).astype(np.float32)
    h = np.histogramdd(a.reshape(-1, 3), bins=bins, range=((0, 256),) * 3)[0]
    h = h.flatten()
    return h / (h.sum() + 1e-9)


def hist_intersection(a, b):
    return float(np.minimum(a, b).sum())


def main():
    ref, ren, outp = sys.argv[1], sys.argv[2], sys.argv[3]
    r = load_rgb(ref)
    g = load_rgb(ren)

    hr, hg = hsv_hist(r), hsv_hist(g)
    inter = hist_intersection(hr, hg)
    mean_diff = float(np.abs(r.mean(axis=(0, 1)) - g.mean(axis=(0, 1))).mean())

    print(f'colour-histogram intersection : {inter:.3f}  (1.0 = identical)')
    print(f'mean RGB diff                 : {mean_diff:.3f}')
    print(f'reference mean RGB            : {r.mean(axis=(0, 1)).round(3)}')
    print(f'render    mean RGB            : {g.mean(axis=(0, 1)).round(3)}')

    montage = Image.new('RGB', (512, 262), (255, 255, 255))
    montage.paste(Image.fromarray((r * 255).astype(np.uint8)), (0, 3))
    montage.paste(Image.fromarray((g * 255).astype(np.uint8)), (256, 3))
    montage.save(outp)
    print('montage:', outp)


if __name__ == '__main__':
    main()
