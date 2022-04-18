"""
Tests for FileStore

Desired behavior
----------------
- A FileStore is initialized on a directory containing files
- The FileStore reads the files and populates itself with file metadata
- If there is a FileStore.json present, its contents are read and merged with
  the file metadata
- If there are records (file_id) in the JSON metadata that are not associated
  with a file on disk anymore, they are marked as orphans with 'orphan: True'
  and added to the store.
- If there is no FileStore.json present
    - if read_only=False, the file is created
    - if read_only=True, no metadata is read in
- if read_only=False, the update() method is enabled
"""

from datetime import datetime, timezone
from distutils.dir_util import copy_tree
from pathlib import Path
import pytest

from maggma.core import StoreError
from maggma.stores.file_store import FileStore, FileRecord
from monty.io import zopen


@pytest.fixture
def test_dir(tmp_path):
    module_dir = Path(__file__).resolve().parent
    test_dir = module_dir / ".." / "test_files" / "file_store_test"
    copy_tree(str(test_dir), str(tmp_path))
    return tmp_path.resolve()


def test_filerecord(test_dir):
    """
    Test functionality of the FileRecord class
    """
    f = FileRecord.from_file(test_dir / "calculation1" / "input.in")
    assert f.name == "input.in"
    assert f.parent == "calculation1"
    assert f.path == test_dir / "calculation1" / "input.in"
    assert f.size == 0
    assert f.hash == f.compute_hash()
    assert f.file_id == f.get_file_id()
    assert f.last_updated == datetime.fromtimestamp(
        f.path.stat().st_mtime, tz=timezone.utc
    )


def test_newer_in_on_local_update(test_dir):
    """
    Init a FileStore
    modify one of the files on disk
    Init another FileStore on the same directory
    confirm that one record shows up in newer_in
    """
    fs = FileStore(test_dir, read_only=False)
    fs.connect()
    with open(test_dir / "calculation1" / "input.in", "w") as f:
        f.write("Ryan was here")
    fs2 = FileStore(test_dir, read_only=False)
    fs2.connect()

    assert fs2.last_updated > fs.last_updated
    assert (
        fs2.query_one({"path": {"$regex": "calculation1/input.in"}})["last_updated"]
        > fs.query_one({"path": {"$regex": "calculation1/input.in"}})["last_updated"]
    )
    assert len(fs.newer_in(fs2)) == 1


def test_max_depth(test_dir):
    """
    test max_depth parameter

    NOTE this test only creates a single temporary directory, meaning that
    the JSON file created by the first FileStore.init() persists for the other
    tests. This creates the possibility of orphaned metadata.
    """
    # default (None) should parse all 6 files
    fs = FileStore(test_dir, read_only=False)
    fs.connect()
    assert len(list(fs.query())) == 6

    # 0 depth should parse 1 file
    fs = FileStore(test_dir, read_only=False, max_depth=0)
    fs.connect()
    assert len(list(fs.query())) == 1

    # 1 depth should parse 5 files
    fs = FileStore(test_dir, read_only=False, max_depth=1)
    fs.connect()
    assert len(list(fs.query())) == 5

    # 2 depth should parse 6 files
    fs = FileStore(test_dir, read_only=False, max_depth=2)
    fs.connect()
    assert len(list(fs.query())) == 6


def test_orphaned_metadata(test_dir):
    """
    test behavior when orphaned metadata is found

    NOTE the design of this test exploits the fact that the test only creates
    a single temporary directory, meaning that the JSON file created by the
    first FileStore.init() persists for the other tests.
    """
    # make a FileStore of all files and add metadata to all of them
    fs = FileStore(test_dir, read_only=False)
    fs.connect()
    data = list(fs.query())
    for d in data:
        d.update({"tags": "Ryan was here"})
    fs.update(data)

    assert len(list(fs.query({"tags": {"$exists": True}}))) == 6
    fs.close()

    # re-init the store with a different max_depth parameter
    # this will result in orphaned metadata
    fs = FileStore(test_dir, read_only=True, max_depth=0)
    with pytest.warns(
        UserWarning, match="Orphaned metadata was found in FileStore.json"
    ):
        fs.connect()
    assert len(list(fs.query({"tags": {"$exists": True}}))) == 6
    assert len(list(fs.query({"name": {"$exists": True}}))) == 1
    assert len(list(fs.query({"orphan": True}))) == 5
    fs.close()

    # re-init the store after renaming one of the files on disk
    # this will result in orphaned metadata
    Path(test_dir / "calculation1" / "input.in").rename(
        test_dir / "calculation1" / "input_renamed.in"
    )
    fs = FileStore(test_dir, read_only=True)
    with pytest.warns(
        UserWarning, match="Orphaned metadata was found in FileStore.json"
    ):
        fs.connect()
    print(list(fs.query()))
    assert len(list(fs.query({"tags": {"$exists": True}}))) == 6
    assert len(list(fs.query({"name": {"$exists": True}}))) == 6
    assert len(list(fs.query({"orphan": True}))) == 1
    fs.close()


def test_file_filters(test_dir):
    """
    Make sure multiple patterns work correctly
    """
    # here, we should get 2 input.in files and the file_2_levels_deep.json
    # the store's FileStore.json should be skipped even though .json is
    # in the file patterns
    fs = FileStore(test_dir, read_only=False, file_filters=["*.in", "*.json"])
    fs.connect()
    assert len(list(fs.query())) == 3


def test_read_only(test_dir):
    """
    Make sure nothing is written to a read-only FileStore and that
    documents cannot be deleted
    """
    with pytest.warns(UserWarning, match="JSON file 'random.json' not found"):
        fs = FileStore(test_dir, read_only=True, json_name="random.json")
        fs.connect()
    assert not Path(test_dir / "random.json").exists()
    with pytest.raises(StoreError, match="read-only"):
        file_id = fs.query_one()["file_id"]
        fs.update({"file_id": file_id, "tags": "something"})
    with pytest.raises(StoreError, match="read-only"):
        fs.remove_docs({})


def test_remove(test_dir):
    """
    Test behavior of remove_docs()
    """
    fs = FileStore(test_dir, read_only=False)
    fs.connect()
    with pytest.raises(NotImplementedError, match="deleting"):
        fs.remove_docs({})


def test_metadata(test_dir):
    """
    1. init a FileStore
    2. add some metadata to both 'input.in' files
    3. confirm metadata written to .json
    4. close the store, init a new one
    5. confirm metadata correctly associated with the files
    """
    fs = FileStore(test_dir, read_only=False)
    fs.connect()
    item = list(fs.query({"name": "input.in", "parent": "calculation1"}))[0]
    k1 = item[fs.key]
    item.update({"metadata": {"experiment date": "2022-01-18"}})
    fs.update([item], fs.key)

    # make sure metadata has been added to the item without removing other contents
    item_from_store = list(fs.query({"file_id": k1}))[0]
    assert item_from_store.get("name", False)
    assert item_from_store.get("metadata", False)
    fs.close()
    data = fs.metadata_store.read_json_file(fs.path / fs.json_name)

    # only the updated item should have been written to the JSON,
    # and it should not contain any of the protected keys
    assert len(data) == 1
    item_from_file = [d for d in data if d["file_id"] == k1][0]
    assert item_from_file["metadata"] == {"experiment date": "2022-01-18"}
    assert not item_from_file.get("name")
    assert item_from_file.get("path")

    # make sure metadata is preserved after reconnecting
    fs2 = FileStore(test_dir, read_only=True)
    fs2.connect()
    data = fs2.metadata_store.read_json_file(fs2.path / fs2.json_name)
    item_from_file = [d for d in data if d["file_id"] == k1][0]
    assert item_from_file["metadata"] == {"experiment date": "2022-01-18"}

    # make sure reconnected store properly merges in the metadata
    item_from_store = [d for d in fs2.query({"file_id": k1})][0]
    assert item_from_store["name"] == "input.in"
    assert item_from_store["parent"] == "calculation1"
    assert item_from_store.get("metadata") == {"experiment date": "2022-01-18"}
    fs2.close()

    # make sure reconnecting with read_only=False doesn't remove metadata from the JSON
    fs3 = FileStore(test_dir, read_only=False)
    fs3.connect()
    data = fs3.metadata_store.read_json_file(fs3.path / fs3.json_name)
    item_from_file = [d for d in data if d["file_id"] == k1][0]
    assert item_from_file["metadata"] == {"experiment date": "2022-01-18"}
    item_from_store = [d for d in fs3.query({"file_id": k1})][0]
    assert item_from_store["name"] == "input.in"
    assert item_from_store["parent"] == "calculation1"
    assert item_from_store.get("metadata") == {"experiment date": "2022-01-18"}
    fs3.close()


def test_json_name(test_dir):
    """
    Make sure custom .json name works
    """
    fs = FileStore(test_dir, read_only=False, json_name="random.json")
    fs.connect()
    assert Path(test_dir / "random.json").exists()
