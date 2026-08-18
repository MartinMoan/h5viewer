import h5py
import numpy as np
import pytest


@pytest.fixture()
def sample_h5_path(tmp_path):
    path = tmp_path / "sample.h5"
    with h5py.File(path, "w") as f:
        f.attrs["version"] = 1
        f.attrs["title"] = "sample file"

        group1 = f.create_group("group1")
        group1.attrs["description"] = "a test group"
        group1.create_dataset("linear", data=np.arange(1000, dtype="int32"))
        group1.create_dataset("matrix", data=np.arange(200, dtype="float64").reshape(50, 4))

        nested = group1.create_group("nested")
        nested.create_dataset("small", data=np.array([1, 2, 3]))

        compound_dtype = np.dtype([("x", "f4"), ("y", "f4"), ("label", "S8")])
        compound_data = np.zeros(25, dtype=compound_dtype)
        compound_data["x"] = np.arange(25)
        compound_data["y"] = np.arange(25) * 2.0
        compound_data["label"] = b"pt"
        f.create_dataset("compound", data=compound_data)

        f.create_dataset("scalar", data=42)
        f.create_dataset("wide", data=np.arange(2000).reshape(2, 1000))
        f.create_dataset("empty", shape=(0,), dtype="int32")

    return str(path)
