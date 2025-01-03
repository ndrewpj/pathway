# Copyright © 2024 Pathway

import json
import multiprocessing
import os
import pathlib
import time
from typing import Callable

import pandas as pd
import pytest

import pathway as pw
from pathway.internals import api
from pathway.internals.parse_graph import G
from pathway.tests.utils import (
    CsvPathwayChecker,
    consolidate,
    needs_multiprocessing_fork,
    run,
    wait_result_with_checker,
    write_csv,
    write_lines,
)


@pytest.mark.parametrize(
    "persistence_mode",
    [pw.PersistenceMode.PERSISTING, pw.PersistenceMode.OPERATOR_PERSISTING],
)
@needs_multiprocessing_fork
def test_groupby_count(persistence_mode, tmp_path):
    input_path = tmp_path / "data"
    os.makedirs(input_path)
    output_path = tmp_path / "output"
    os.makedirs(output_path)
    pstorage_path = tmp_path / "PStorage"

    def run(output_path):
        class InputSchema(pw.Schema):
            w: str

        t = pw.io.csv.read(input_path, schema=InputSchema)
        res = t.groupby(pw.this.w).reduce(pw.this.w, c=pw.reducers.count())
        pw.io.csv.write(res, output_path)

        pw.run(
            persistence_config=pw.persistence.Config(
                pw.persistence.Backend.filesystem(pstorage_path),
                snapshot_interval_ms=1000,
                persistence_mode=persistence_mode,
            ),
            monitoring_level=pw.MonitoringLevel.NONE,
        )

    file1 = """
    w
    abc
    def
    foo
    """
    write_csv(input_path / "1.csv", file1)
    p = multiprocessing.Process(target=run, daemon=True, args=(output_path / "1.csv",))
    p.start()
    time.sleep(2)  # sleep to write file2 after some time to simulate streaming behavior
    file2 = """
    w
    foo
    xyz
    """
    write_csv(input_path / "2.csv", file2)
    expected = """
       w | c
     abc | 1
     def | 1
     foo | 2
     xyz | 1
    """
    wait_result_with_checker(
        CsvPathwayChecker(expected, output_path, id_from=["w"]), 10, target=None, step=1
    )
    time.sleep(2)  # sleep needed to save persistence state (see snapshot_interval_ms)
    p.terminate()
    p.join()

    file3 = """
    w
    abc
    xxx
    """
    write_csv(input_path / "3.csv", file3)
    p = multiprocessing.Process(target=run, daemon=True, args=(output_path / "2.csv",))
    p.start()
    time.sleep(2)
    file4 = """
    w
    foo
    """
    write_csv(input_path / "4.csv", file4)
    expected = """
       w | c
     abc | 2
     def | 1
     foo | 3
     xyz | 1
     xxx | 1
    """
    wait_result_with_checker(
        CsvPathwayChecker(expected, output_path, id_from=["w"]), 10, target=None, step=1
    )
    time.sleep(2)
    p.terminate()
    p.join()

    file5 = """
    w
    abc
    def
    """
    write_csv(input_path / "5.csv", file5)
    p = multiprocessing.Process(target=run, daemon=True, args=(output_path / "3.csv",))
    p.start()
    time.sleep(2)
    file6 = """
    w
    xyz
    """
    write_csv(input_path / "6.csv", file6)
    expected = """
       w | c
     abc | 3
     def | 2
     foo | 3
     xyz | 2
     xxx | 1
    """
    wait_result_with_checker(
        CsvPathwayChecker(expected, output_path, id_from=["w"]), 10, target=None
    )
    time.sleep(2)
    p.terminate()
    p.join()


# Each run is denoted by a scenario.
# Each scenario consists of several sequences of commands.
# A scenario is executed as follows:
# - The command sequences are processed one by one.
# - When processing a sequence, the test first applies the commands that are denoted
#   by a sequence and then runs Pathway identity program in static mode.
# - After Pathway program finishes, the output is checked with the exepected output.
#
# There are two possible commands that can appear within scenario:
# - Upsert(X): create or modify a file named X.
# - Delete(X): delete a file named X.
#
# For simplicity, the files are named with natural integers.
# To be reproducible, this test creates all modifications deterministically.
@pytest.mark.parametrize(
    "scenario",
    [
        [["Upsert(1)", "Upsert(2)"], ["Delete(1)", "Delete(2)"]],
        [["Upsert(1)"], ["Upsert(1)"], ["Upsert(1)"]],
        [["Upsert(1)"], ["Upsert(1)"], ["Delete(1)"]],
        [["Upsert(1)"], ["Delete(1)"], ["Upsert(1)"]],
        [["Upsert(1)"], ["Delete(1)"], ["Upsert(2)"]],
        [
            ["Upsert(1)", "Upsert(2)", "Upsert(3)"],
            ["Delete(3)"],
            ["Upsert(4)"],
            ["Upsert(3)"],
        ],
        [
            ["Upsert(1)", "Upsert(2)", "Upsert(3)"],
            ["Delete(2)"],
            ["Delete(3)"],
            ["Delete(1)"],
        ],
        [
            ["Upsert(1)", "Upsert(2)", "Upsert(3)", "Upsert(4)"],
            ["Upsert(2)", "Upsert(3)"],
        ],
        [
            ["Upsert(1)", "Upsert(2)", "Upsert(3)", "Upsert(4)"],
            ["Delete(2)"],
            ["Upsert(3)"],
        ],
        [
            ["Upsert(1)", "Upsert(2)", "Upsert(3)", "Upsert(4)"],
            ["Delete(1)"],
        ],
        [
            ["Upsert(1)", "Upsert(2)", "Upsert(3)", "Upsert(4)"],
            ["Upsert(4)", "Upsert(3)", "Upsert(2)", "Upsert(1)"],
        ],
        [
            ["Upsert(1)", "Upsert(2)", "Upsert(3)", "Upsert(4)"],
            ["Delete(3)", "Delete(2)", "Upsert(1)"],
            ["Delete(1)"],
            ["Upsert(5)", "Upsert(1)", "Upsert(3)"],
        ],
        [
            ["Upsert(1)", "Upsert(2)"],
            ["Delete(2)", "Upsert(1)", "Upsert(3)"],
        ],
    ],
)
def test_persistence_modifications(tmp_path, scenario):
    inputs_path = tmp_path / "inputs"
    output_path = tmp_path / "output.txt"
    pstorage_path = tmp_path / "PStorage"
    os.mkdir(inputs_path)

    def pw_identity_program():
        G.clear()
        persistence_backend = pw.persistence.Backend.filesystem(pstorage_path)
        persistence_config = pw.persistence.Config(persistence_backend)
        table = pw.io.plaintext.read(inputs_path, mode="static")
        pw.io.jsonlines.write(table, output_path)
        pw.run(persistence_config=persistence_config)

    file_contents: dict[str, str] = {}
    next_file_contents = 0
    for sequence in scenario:
        expected_diffs = []
        for command in sequence:
            used_file_ids = set()
            if command.startswith("Upsert(") and command.endswith(")"):
                file_id = command[len("Upsert(") : -1]
                assert (
                    file_id not in used_file_ids
                ), "Incorrect scenario! File changed more than once in a single sequence"
                used_file_ids.add(file_id)

                # Record old state removal
                old_contents = file_contents.get(file_id)
                if old_contents is not None:
                    expected_diffs.append([old_contents, -1])

                # Handle new state change
                next_file_contents += 1
                new_contents = (
                    "a" * next_file_contents
                )  # This way, the metadata always changes: at least the file size
                file_contents[file_id] = new_contents
                expected_diffs.append([new_contents, 1])
                with open(inputs_path / file_id, "w") as f:
                    f.write(new_contents)
            elif command.startswith("Delete(") and command.endswith(")"):
                file_id = command[len("Delete(") : -1]
                assert (
                    file_id not in used_file_ids
                ), "Incorrect scenario! File changed more than once in a single sequence"
                used_file_ids.add(file_id)

                old_contents = file_contents.pop(file_id, None)
                assert (
                    old_contents is not None
                ), f"Incorrect scenario! Deletion of a nonexistent object {scenario}"
                expected_diffs.append([old_contents, -1])
                os.remove(inputs_path / file_id)
            else:
                raise ValueError(f"Unknown command: {command}")

        pw_identity_program()
        actual_diffs = []
        with open(output_path, "r") as f:
            for row in f:
                row_parsed = json.loads(row)
                actual_diffs.append([row_parsed["data"], row_parsed["diff"]])
        actual_diffs.sort()
        expected_diffs.sort()
        assert actual_diffs == expected_diffs


def combine_columns(df: pd.DataFrame) -> pd.Series:
    result = None
    for column in df.columns:
        if column == "time":
            continue
        if result is None:
            result = df[column].astype(str)
        else:
            result += "," + df[column].astype(str)
    return result


def get_one_table_runner(
    tmp_path: pathlib.Path,
    mode: api.PersistenceMode,
    logic: Callable[[pw.Table], pw.Table],
    schema: type[pw.Schema],
) -> tuple[Callable[[list[str], set[str]], None], pathlib.Path]:
    input_path = tmp_path / "1"
    os.makedirs(input_path)
    output_path = tmp_path / "out.csv"
    persistent_storage_path = tmp_path / "p"
    count = 0

    def run_computation(inputs, expected):
        nonlocal count
        count += 1
        G.clear()
        path = input_path / str(count)
        write_lines(path, inputs)
        t_1 = pw.io.csv.read(input_path, schema=schema, mode="static")
        res = logic(t_1)
        pw.io.csv.write(res, output_path)
        run(
            persistence_config=pw.persistence.Config(
                pw.persistence.Backend.filesystem(persistent_storage_path),
                persistence_mode=mode,
            )
        )
        result = consolidate(pd.read_csv(output_path))
        assert set(combine_columns(result)) == expected

    return run_computation, input_path


def get_two_tables_runner(
    tmp_path: pathlib.Path,
    mode: api.PersistenceMode,
    logic: Callable[[pw.Table, pw.Table], pw.Table],
    schema: type[pw.Schema],
    terminate_on_error: bool = True,
) -> tuple[
    Callable[[list[str], list[str], set[str]], None], pathlib.Path, pathlib.Path
]:

    input_path_1 = tmp_path / "1"
    input_path_2 = tmp_path / "2"
    os.makedirs(input_path_1)
    os.makedirs(input_path_2)
    output_path = tmp_path / "out.csv"
    persistent_storage_path = tmp_path / "p"
    count = 0

    def run_computation(inputs_1, inputs_2, expected):
        nonlocal count
        count += 1
        G.clear()
        path_1 = input_path_1 / str(count)
        path_2 = input_path_2 / str(count)
        write_lines(path_1, inputs_1)
        write_lines(path_2, inputs_2)
        t_1 = pw.io.csv.read(input_path_1, schema=schema, mode="static")
        t_2 = pw.io.csv.read(input_path_2, schema=schema, mode="static")
        res = logic(t_1, t_2)
        pw.io.csv.write(res, output_path)
        run(
            persistence_config=pw.persistence.Config(
                pw.persistence.Backend.filesystem(persistent_storage_path),
                persistence_mode=mode,
            ),
            terminate_on_error=terminate_on_error,
            # hack to allow changes from different files at different point in time
        )
        result = consolidate(pd.read_csv(output_path))
        assert set(combine_columns(result)) == expected

    return run_computation, input_path_1, input_path_2


@pytest.mark.parametrize(
    "mode", [api.PersistenceMode.PERSISTING, api.PersistenceMode.OPERATOR_PERSISTING]
)
def test_restrict(tmp_path, mode):
    class InputSchema(pw.Schema):
        a: int = pw.column_definition(primary_key=True)

    def logic(t_1: pw.Table, t_2: pw.Table) -> pw.Table:
        t_2.promise_universe_is_subset_of(t_1)
        return t_1.restrict(t_2)

    run, _, input_path_2 = get_two_tables_runner(
        tmp_path, mode, logic, InputSchema, terminate_on_error=False
    )

    run(["a", "1", "2", "3"], ["a", "1"], {"1,1"})
    run(["a"], ["a", "3"], {"3,1"})
    run(["a", "4", "5"], ["a", "5"], {"5,1"})
    run(["a", "6"], ["a", "4", "6"], {"4,1", "6,1"})
    os.remove(input_path_2 / "3")
    run(["a"], ["a"], {"5,-1"})


@pytest.mark.parametrize(
    "mode", [api.PersistenceMode.PERSISTING, api.PersistenceMode.OPERATOR_PERSISTING]
)
def test_with_universe_of(tmp_path, mode):
    class InputSchema(pw.Schema):
        a: int = pw.column_definition(primary_key=True)
        b: int

    def logic(t_1: pw.Table, t_2: pw.Table) -> pw.Table:
        return t_1.with_universe_of(t_2).with_columns(c=t_2.b)

    run, input_path_1, input_path_2 = get_two_tables_runner(
        tmp_path, mode, logic, InputSchema, terminate_on_error=False
    )

    run(["a,b", "1,2", "2,3"], ["a,b", "1,3", "2,4"], {"1,2,3,1", "2,3,4,1"})
    run(["a,b", "3,3", "5,1"], ["a,b", "3,4", "5,0"], {"3,3,4,1", "5,1,0,1"})
    os.remove(input_path_1 / "2")
    os.remove(input_path_2 / "2")
    run(
        ["a,b", "3,4"],
        ["a,b", "3,5"],
        {
            "3,3,4,-1",
            "5,1,0,-1",
            "3,4,5,1",
        },
    )


@pytest.mark.parametrize(
    "mode", [api.PersistenceMode.PERSISTING, api.PersistenceMode.OPERATOR_PERSISTING]
)
def test_intersect(tmp_path, mode):
    class InputSchema(pw.Schema):
        a: int = pw.column_definition(primary_key=True)

    def logic(t_1: pw.Table, t_2: pw.Table) -> pw.Table:
        return t_1.intersect(t_2)

    run, _, input_path_2 = get_two_tables_runner(tmp_path, mode, logic, InputSchema)

    run(["a", "1", "2", "3"], ["a", "1"], {"1,1"})
    run(["a"], ["a", "3"], {"3,1"})
    run(["a", "4", "5"], ["a", "5", "6"], {"5,1"})
    run(["a", "6"], ["a", "4"], {"4,1", "6,1"})
    os.remove(input_path_2 / "3")
    run(["a"], ["a"], {"5,-1", "6,-1"})


@pytest.mark.parametrize(
    "mode", [api.PersistenceMode.PERSISTING, api.PersistenceMode.OPERATOR_PERSISTING]
)
def test_difference(tmp_path, mode):
    class InputSchema(pw.Schema):
        a: int = pw.column_definition(primary_key=True)

    def logic(t_1: pw.Table, t_2: pw.Table) -> pw.Table:
        return t_1.difference(t_2)

    run, _, input_path_2 = get_two_tables_runner(tmp_path, mode, logic, InputSchema)

    run(["a", "1", "2", "3"], ["a", "1"], {"2,1", "3,1"})
    run(["a"], ["a", "3"], {"3,-1"})
    run(["a", "4", "5"], ["a", "5", "6"], {"4,1"})
    run(["a", "6"], ["a", "4"], {"4,-1"})
    os.remove(input_path_2 / "3")
    run(["a"], ["a"], {"5,1", "6,1"})


@pytest.mark.parametrize(
    "mode", [api.PersistenceMode.PERSISTING, api.PersistenceMode.OPERATOR_PERSISTING]
)
def test_sorting_ix(tmp_path, mode):
    class InputSchema(pw.Schema):
        a: int = pw.column_definition(primary_key=True)

    def logic(t_1: pw.Table) -> pw.Table:
        t_1 += t_1.sort(pw.this.a)
        t_1_filtered = t_1.filter(pw.this.prev.is_not_none())
        return t_1_filtered.select(b=t_1.ix(pw.this.prev).a, a=pw.this.a)

    run, input_path = get_one_table_runner(tmp_path, mode, logic, InputSchema)

    run(["a", "1", "6"], {"1,6,1"})
    run(["a", "3"], {"1,6,-1", "1,3,1", "3,6,1"})
    run(["a", "4", "5"], {"3,6,-1", "3,4,1", "4,5,1", "5,6,1"})
    os.remove(input_path / "2")
    run(["a"], {"1,3,-1", "3,4,-1", "1,4,1"})
    run(["a", "2"], {"1,4,-1", "1,2,1", "2,4,1"})


@pytest.mark.parametrize(
    "mode", [api.PersistenceMode.PERSISTING, api.PersistenceMode.OPERATOR_PERSISTING]
)
def test_update_rows(tmp_path, mode):
    class InputSchema(pw.Schema):
        a: int = pw.column_definition(primary_key=True)
        b: int

    def logic(t_1: pw.Table, t_2: pw.Table) -> pw.Table:
        return t_1.update_rows(t_2)

    run, _, input_path_2 = get_two_tables_runner(tmp_path, mode, logic, InputSchema)

    run(["a,b", "1,2", "2,4"], ["a,b", "1,3", "3,5"], {"1,3,1", "2,4,1", "3,5,1"})
    run(["a,b", "3,3"], ["a,b", "2,6", "5,1"], {"2,4,-1", "2,6,1", "5,1,1"})
    os.remove(input_path_2 / "1")
    run(["a,b"], ["a,b"], {"3,5,-1", "3,3,1", "1,3,-1", "1,2,1"})
    run(["a,b", "7,10"], ["a,b", "3,8"], {"3,3,-1", "3,8,1", "7,10,1"})


@pytest.mark.parametrize(
    "mode", [api.PersistenceMode.PERSISTING, api.PersistenceMode.OPERATOR_PERSISTING]
)
def test_update_cells(tmp_path, mode):
    class InputSchema(pw.Schema):
        a: int = pw.column_definition(primary_key=True)
        b: int

    def logic(t_1: pw.Table, t_2: pw.Table) -> pw.Table:
        t_2.promise_universe_is_subset_of(t_1)
        return t_1.update_cells(t_2)

    run, _, input_path_2 = get_two_tables_runner(
        tmp_path, mode, logic, InputSchema, terminate_on_error=False
    )

    run(["a,b", "1,2", "2,4"], ["a,b", "1,3"], {"1,3,1", "2,4,1"})
    run(["a,b", "3,3"], ["a,b", "2,6"], {"2,4,-1", "2,6,1", "3,3,1"})
    os.remove(input_path_2 / "1")
    run(["a,b"], ["a,b"], {"1,3,-1", "1,2,1"})
    run(["a,b", "7,10"], ["a,b", "3,8"], {"3,3,-1", "3,8,1", "7,10,1"})


@pytest.mark.parametrize(
    "mode", [api.PersistenceMode.PERSISTING, api.PersistenceMode.OPERATOR_PERSISTING]
)
def test_join(tmp_path, mode):
    class InputSchema(pw.Schema):
        a: int = pw.column_definition(primary_key=True)
        b: int

    def logic(t_1: pw.Table, t_2: pw.Table) -> pw.Table:
        return t_1.join(t_2, t_1.a == t_2.a).select(
            pw.this.a, b=pw.left.b, c=pw.right.b
        )

    run, _, input_path_2 = get_two_tables_runner(tmp_path, mode, logic, InputSchema)

    run(["a,b", "1,2", "2,4"], ["a,b", "1,3"], {"1,2,3,1"})
    run(["a,b", "3,3"], ["a,b", "2,6", "1,4"], {"2,4,6,1", "1,2,4,1"})
    os.remove(input_path_2 / "1")
    run(["a,b"], ["a,b"], {"1,2,3,-1"})
    run(["a,b", "1,4"], ["a,b", "1,8"], {"1,2,8,1", "1,4,8,1", "1,4,4,1"})


@pytest.mark.parametrize(
    "mode", [api.PersistenceMode.PERSISTING, api.PersistenceMode.OPERATOR_PERSISTING]
)
def test_groupby(tmp_path, mode):
    class InputSchema(pw.Schema):
        a: int
        b: int

    def logic(t_1: pw.Table) -> pw.Table:
        return t_1.groupby(pw.this.a).reduce(
            pw.this.a,
            c=pw.reducers.count(),
            s=pw.reducers.sum(pw.this.b),
            m=pw.reducers.max(pw.this.b),
        )

    run, input_path = get_one_table_runner(tmp_path, mode, logic, InputSchema)

    run(["a,b", "1,3", "2,4"], {"1,1,3,3,1", "2,1,4,4,1"})
    run(["a,b", "1,1"], {"1,1,3,3,-1", "1,2,4,3,1"})
    run(["a,b", "2,5"], {"2,1,4,4,-1", "2,2,9,5,1"})
    os.remove(input_path / "2")
    run(["a,b"], {"1,1,3,3,1", "1,2,4,3,-1"})
    run(["a,b", "2,0"], {"2,2,9,5,-1", "2,3,9,5,1"})
