from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from nilearn import datasets, surface


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "frontend" / "src" / "assets" / "fsaverage5-pial.json"


def normalize(coords_left: np.ndarray, coords_right: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    coords = np.vstack([coords_left, coords_right])
    center = coords.mean(axis=0)
    coords_left = coords_left - center
    coords_right = coords_right - center
    scale = np.max(np.linalg.norm(np.vstack([coords_left, coords_right]), axis=1))
    return coords_left / scale, coords_right / scale


def mesh_payload() -> dict[str, object]:
    fsaverage = datasets.fetch_surf_fsaverage(mesh="fsaverage5")
    left_coords, left_faces = surface.load_surf_mesh(fsaverage["pial_left"])
    right_coords, right_faces = surface.load_surf_mesh(fsaverage["pial_right"])
    left_coords, right_coords = normalize(left_coords, right_coords)
    return {
        "mesh": "fsaverage5",
        "space": "fsaverage",
        "surface": "pial",
        "hemiVertexCount": int(left_coords.shape[0]),
        "left": {
            "coords": np.round(left_coords, 5).astype(float).tolist(),
            "faces": left_faces.astype(int).tolist(),
        },
        "right": {
            "coords": np.round(right_coords, 5).astype(float).tolist(),
            "faces": right_faces.astype(int).tolist(),
        },
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = mesh_payload()
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUT}")
    print(
        f"vertices={payload['hemiVertexCount']} per hemi, "
        f"size={OUT.stat().st_size / 1024 / 1024:.2f} MiB"
    )


if __name__ == "__main__":
    main()
