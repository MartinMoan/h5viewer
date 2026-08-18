import numpy as np

from h5tools_app.core.h5_model import DATASET, GROUP, H5Model, build_column_layout, read_rows


def test_list_children_root_groups_before_datasets(sample_h5_path):
    with H5Model(sample_h5_path) as model:
        children = model.list_children("/")
        kinds = [c.kind for c in children]
        assert kinds == sorted(kinds, key=lambda k: 0 if k == GROUP else 1)
        names = {c.name for c in children}
        assert names == {"group1", "compound", "scalar", "wide", "empty"}


def test_node_info_dataset(sample_h5_path):
    with H5Model(sample_h5_path) as model:
        info = model.node_info("/group1/linear")
        assert info.kind == DATASET
        assert info.shape == (1000,)
        assert info.dtype == "int32"


def test_node_info_group_child_count(sample_h5_path):
    with H5Model(sample_h5_path) as model:
        info = model.node_info("/group1")
        assert info.kind == GROUP
        assert info.n_children == 3  # linear, matrix, nested


def test_get_attrs(sample_h5_path):
    with H5Model(sample_h5_path) as model:
        attrs = model.get_attrs("/")
        assert attrs["version"] == "1"
        assert attrs["title"] == "sample file"
        group_attrs = model.get_attrs("/group1")
        assert group_attrs["description"] == "a test group"


def test_column_layout_1d():
    layout = build_column_layout((1000,), np.dtype("int32"))
    assert layout.row_count == 1000
    assert layout.labels == ["value"]
    assert not layout.truncated


def test_column_layout_2d():
    layout = build_column_layout((50, 4), np.dtype("float64"))
    assert layout.row_count == 50
    assert layout.labels == ["[0]", "[1]", "[2]", "[3]"]


def test_column_layout_compound():
    dtype = np.dtype([("x", "f4"), ("y", "f4"), ("label", "S8")])
    layout = build_column_layout((25,), dtype)
    assert layout.labels == ["x", "y", "label"]
    assert layout.field_names == ("x", "y", "label")


def test_column_layout_truncates_wide_datasets():
    layout = build_column_layout((2, 1000), np.dtype("int64"), max_columns=256)
    assert layout.truncated is True
    assert layout.n_columns == 256
    assert layout.total_columns == 1000


def test_read_rows_1d_matches_source(sample_h5_path):
    with H5Model(sample_h5_path) as model:
        dataset = model.get_dataset("/group1/linear")
        layout = model.column_layout("/group1/linear")
        rows = read_rows(dataset, 10, 20, layout)
        assert rows.shape == (10, 1)
        assert [rows[i, 0] for i in range(10)] == list(range(10, 20))


def test_read_rows_2d_matches_source(sample_h5_path):
    with H5Model(sample_h5_path) as model:
        dataset = model.get_dataset("/group1/matrix")
        layout = model.column_layout("/group1/matrix")
        rows = read_rows(dataset, 0, 5, layout)
        expected = dataset[0:5]
        for r in range(5):
            for col in range(4):
                assert rows[r, col] == expected[r, col]


def test_read_rows_compound(sample_h5_path):
    with H5Model(sample_h5_path) as model:
        dataset = model.get_dataset("/compound")
        layout = model.column_layout("/compound")
        rows = read_rows(dataset, 0, 3, layout)
        assert rows[0, 0] == dataset[0]["x"]
        assert rows[0, 1] == dataset[0]["y"]


def test_scalar_dataset_reads_as_single_row(sample_h5_path):
    with H5Model(sample_h5_path) as model:
        dataset = model.get_dataset("/scalar")
        layout = model.column_layout("/scalar")
        assert layout.row_count == 1
        rows = read_rows(dataset, 0, 1, layout)
        assert rows[0, 0] == 42


def test_empty_dataset(sample_h5_path):
    with H5Model(sample_h5_path) as model:
        layout = model.column_layout("/empty")
        assert layout.row_count == 0
