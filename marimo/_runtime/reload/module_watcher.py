# Copyright 2024 Marimo. All rights reserved.
from __future__ import annotations

import itertools
import sys
import threading
import time
from modulefinder import ModuleFinder
from typing import TYPE_CHECKING, Callable, Literal

from marimo._ast.cell import CellId_t, CellImpl
from marimo._messaging.types import Stream
from marimo._runtime import dataflow
from marimo._runtime.reload.autoreload import ModuleReloader

if TYPE_CHECKING:
    import types


def _modules_imported_by_cell(cell: CellImpl) -> set[str]:
    """Get the modules imported by a cell"""
    modules = set()
    for import_data in cell.imports:
        if import_data.module in sys.modules:
            modules.add(import_data.module)
        if import_data.imported_symbol in sys.modules:
            # The imported symbol may or may not be a module, which
            # is why we check if it's in sys.modules
            #
            # e.g., from a import b
            #
            # a.b could be a module, but it could also be a function, ...
            modules.add(import_data.imported_symbol)
    return modules


def _depends_on(
    src_module: types.ModuleType,
    target_filenames: set[str],
    failed_filenames: set[str],
) -> bool:
    """Returns whether src_module depends on any of target_filenames"""
    if not hasattr(src_module, "__file__") or src_module.__file__ is None:
        return False

    if src_module.__file__ in failed_filenames:
        return False

    finder = ModuleFinder()
    try:
        finder.run_script(src_module.__file__)
    except SyntaxError:
        # user introduced a syntax error, maybe; don't
        # exclude this module from future searches
        return True
    except Exception:
        # some modules like numpy fail when called with run_script;
        # run_script takes a long time before failing on them, so
        # don't try to analyze them again
        failed_filenames.add(src_module.__file__)
        return False

    for found_module in itertools.chain([src_module], finder.modules.values()):
        if (
            hasattr(found_module, "__file__")
            and found_module.__file__ in target_filenames
        ):
            return True
    return False


def _check_modules(
    modules: dict[str, types.ModuleType],
    reloader: ModuleReloader,
    failed_filenames: set[str],
) -> dict[str, types.ModuleType]:
    """Returns the set of modules used by the graph that have been modified"""
    stale_modules: dict[str, types.ModuleType] = {}
    modified_modules = reloader.check(modules=sys.modules, reload=False)
    for modname, module in modules.items():
        if _depends_on(
            src_module=module,
            target_filenames=set(
                m.__file__ for m in modified_modules if m.__file__ is not None
            ),
            failed_filenames=failed_filenames,
        ):
            stale_modules[modname] = module

    return stale_modules


def watch_modules(
    graph: dataflow.DirectedGraph,
    mode: Literal["detect", "autorun"],
    enqueue_run_stale_cells: Callable[[], None],
    should_exit: threading.Event,
    run_is_processed: threading.Event,
    stream: Stream,
) -> None:
    """Watches for changes to modules used by graph

    The modules used by the graph are determined statically, by analyzing the
    modules imported by the notebook as well as the modules imported by those
    modules, recursively.
    """
    reloader = ModuleReloader()
    # modules that failed to be analyzed
    failed_filenames: set[str] = set()
    while not should_exit.is_set():
        run_is_processed.wait()
        time.sleep(1)
        # Collect the modules used by each cell
        modules: dict[str, types.ModuleType] = {}
        modname_to_cell_id: dict[str, CellId_t] = {}
        with graph.lock:
            for cell_id, cell in graph.cells.items():
                for modname in _modules_imported_by_cell(cell):
                    if modname in sys.modules:
                        modules[modname] = sys.modules[modname]
                        modname_to_cell_id[modname] = cell_id
        stale_modules = _check_modules(
            modules=modules,
            reloader=reloader,
            failed_filenames=failed_filenames,
        )
        if stale_modules:
            with graph.lock:
                # If any modules are stale, communicate that to the FE
                stale_cell_ids = dataflow.transitive_closure(
                    graph,
                    set(
                        modname_to_cell_id[modname]
                        for modname in stale_modules
                    ),
                )
                for cid in stale_cell_ids:
                    graph.cells[cid].set_stale(stale=True, stream=stream)
            if mode == "autorun":
                run_is_processed.clear()
                enqueue_run_stale_cells()


class ModuleWatcher:
    def __init__(
        self,
        graph: dataflow.DirectedGraph,
        mode: Literal["detect", "autorun"],
        enqueue_run_stale_cells: Callable[[], None],
        stream: Stream,
    ) -> None:
        # ModuleWatcher uses the graph to determine the modules used by the
        # notebook
        self.graph = graph
        # When set, signals the watcher thread to exit
        self.should_exit = threading.Event()
        # When False, an ExecuteStaleRequest is inflight to the kernel
        self.run_is_processed = threading.Event()
        self.run_is_processed.set()
        # To communicate staleness to the FE
        self.stream = stream
        # If autorun, stale cells are automatically scheduled for execution
        self.mode = mode
        # A callable that signals the kernel to run stale cells
        self.enqueue_run_stale_cells = enqueue_run_stale_cells
        threading.Thread(
            target=watch_modules,
            args=(
                self.graph,
                self.mode,
                self.enqueue_run_stale_cells,
                self.should_exit,
                self.run_is_processed,
                self.stream,
            ),
            daemon=True,
        ).start()

    def stop(self) -> None:
        self.should_exit.set()
