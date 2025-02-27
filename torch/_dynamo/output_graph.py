import collections
import copy
import functools
import itertools
import logging
import operator
import re
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, OrderedDict, Set, Tuple, Union

import sympy
from typing_extensions import Protocol

import torch.nn
from torch import fx
from torch.fx.experimental.symbolic_shapes import ShapeEnv

from . import config, logging as torchdynamo_logging, variables
from .bytecode_transformation import create_instruction, Instruction, unique_id
from .codegen import PyCodegen
from .exc import BackendCompilerFailed, unimplemented
from .guards import Guard, GuardBuilder, TensorReference
from .mutation_guard import is_dynamic_nn_module
from .side_effects import SideEffects
from .source import ConstantSource, LocalSource, Source
from .utils import (
    assert_no_fake_params_or_buffers,
    checkpoint_params,
    CleanupHook,
    clone_inputs,
    count_calls,
    counters,
    format_graph_tabular,
    same,
)
from .variables.base import VariableTracker
from .variables.builder import GraphArg, VariableBuilder, wrap_fx_proxy
from .variables.nn_module import NNModuleVariable
from .variables.tensor import (
    DynamicShapeVariable,
    TensorVariable,
    UnspecializedNumpyVariable,
    UnspecializedPythonVariable,
)

log = logging.getLogger(__name__)


# TODO: I think this accepts int arguments too
class CompiledFn(Protocol):
    def __call__(self, *args: torch.Tensor) -> Tuple[torch.Tensor, ...]:
        ...


CompilerFn = Callable[[fx.GraphModule, List[torch.Tensor]], CompiledFn]


@functools.lru_cache(None)
def _step_logger():
    return torchdynamo_logging.get_step_logger(log)


@dataclass
class GraphCompileReason:
    """Stores why a given output graph was compiled; i.e. what caused the graph break."""

    reason: str
    user_stack: List[traceback.FrameSummary]


def _get_gen_rand_values_fn(random_calls):
    def _gen_rand_values():
        return [fn(*args, **kwargs) for fn, args, kwargs in random_calls]

    return _gen_rand_values


class FakeRootModule(torch.nn.Module):
    """Trick the constructor of fx.GraphModule"""

    def __init__(self, nn_modules: dict):
        super(FakeRootModule, self).__init__()
        for k, v in nn_modules.items():
            setattr(self, k, v)

    def __repr__(self):
        return "FakeRootModule(...)"


def wrap_compiler_fn(compiler_fn: CompilerFn) -> CompilerFn:
    """WrapperBackend if config.verify_correctness is True"""
    if config.verify_correctness:
        # wrap backend if verify_correctness is True
        wrapper_backend_compiler_fn = WrapperBackend(compiler_fn)

        wrapper_backend_compiler_fn._torchdynamo_orig_callable = compiler_fn  # type: ignore[attr-defined]
        return wrapper_backend_compiler_fn

    return compiler_fn


class WrapperBackend:
    def __init__(self, backend: CompilerFn):
        self.backend: CompilerFn = backend

    @property
    def example_inputs(self):
        return clone_inputs(self.original_example_inputs)

    def __call__(self, gm: torch.fx.GraphModule, example_inputs: List[torch.Tensor]):

        self.restore = checkpoint_params(gm)
        self.original_example_inputs = clone_inputs(example_inputs)
        self.gm = gm
        copy_gm = copy.deepcopy(self.gm)
        self.candidate = self.backend(copy_gm, self.original_example_inputs)

        if self.candidate is None or self.candidate is self.gm.forward:
            return self.gm.forward

        if not config.verify_correctness:
            return self.candidate

        # if verify_correctness=True
        try:
            correct = self.gm.forward(*self.example_inputs)
            result = self.candidate(*self.example_inputs)

            # TODO: replace `same` function with the one in testing
            if same(correct, result):
                return self.candidate

            raise RuntimeError(f"incorrect results of backend {self}")
            return self.gm.forward

        except Exception:
            log.exception("error in verify_correctness")
            raise
        finally:
            self.restore()


class OutputGraph(fx.Tracer):
    """
    Wrapper class to hold outputs of InstructionTranslator.  Mainly the
    generated fx.Graph.
    """

    def __init__(
        self,
        f_globals: Dict[str, Any],
        code_options: Dict[str, Any],
        compiler_fn: CompilerFn,
        root_tx,
    ):
        super(OutputGraph, self).__init__()

        # Mutable state checkpointed by copy_graphstate()
        self.graph = torch.fx.Graph()
        self.graphargs: List[GraphArg] = []
        self.guards: Set[Guard] = set()
        self.nn_modules: Optional[Dict[str, torch.nn.Module]] = dict()
        self.side_effects = SideEffects()
        self.code_options = dict(code_options)
        self.output_instructions: List[Instruction] = []
        # Node => computed real value (see utils.get_real_value)
        self.real_value_cache: Dict[fx.Node, torch.Tensor] = {}

        # Not checkpointed
        self.compiler_fn: CompilerFn = compiler_fn
        self.root_globals = f_globals
        self.root_tx = root_tx
        self.cleanups: List[CleanupHook] = []
        self.should_exit = False
        self.random_values_var = None
        self.initial_random_state = ()
        self.unspec_variable_map: Dict[
            str, Union[UnspecializedNumpyVariable, UnspecializedPythonVariable]
        ] = {}
        self.shape_env = ShapeEnv() if config.dynamic_shapes else None
        self.tensor_id_to_sym_shape_ref: Dict[int, Set[TensorReference]] = {}
        self.intermediary_symbols: Dict[sympy.Expr, None] = {}

        # Enables creating unique node names by tracking
        # all current placeholder node names
        self.name_to_input: OrderedDict[
            str, Optional[fx.Proxy]
        ] = collections.OrderedDict()

    @property
    def output(self):
        return self

    @property
    def fake_mode(self):
        return self.root_tx.fake_mode

    def copy_graphstate(self):
        """Create a checkpoint of the current state by copying everything"""
        assert self.nn_modules is not None
        graph_nodes = set(self.graph.nodes)
        return (
            graph_nodes,
            list(self.graphargs),
            set(self.guards),
            dict(self.nn_modules),
            self.side_effects.clone(),
        )

    def restore_graphstate(self, state):
        """Restore a checkpoint created by self.copy_graphstate()"""
        (
            graph_nodes,
            self.graphargs,
            self.guards,
            self.nn_modules,
            self.side_effects,
        ) = state
        # FX deepcopy doesn't work for a partially created graph, so just remove new nodes
        for node in reversed(list(self.graph.nodes)):
            if node not in graph_nodes:
                # Erasing node alone does not remove the meta information
                # So, remove the help tensor explicitly
                if "example_value" in node.meta:
                    del node.meta["example_value"]
                self.graph.erase_node(node)
                self.real_value_cache.pop(node, None)
                self.name_to_input.pop(node.name, None)

    def count_calls(self):
        return count_calls(self.graph)

    def get_submodule(self, keys):
        assert keys
        obj = self.nn_modules
        for k in keys.split("."):
            if isinstance(obj, dict):
                obj = obj[k]
            else:
                obj = getattr(obj, k)
        return obj

    def create_graph_input(self, name, type_expr=None):
        # unique
        if name in self.name_to_input:
            for i in itertools.count():
                if f"{name}_{i}" not in self.name_to_input:
                    name = f"{name}_{i}"
                    break

        if self.name_to_input:
            prev_name = next(reversed(self.name_to_input))
            ctx = self.graph.inserting_after(self.name_to_input[prev_name])
        else:
            ctx = self.graph.inserting_before(None)
        with ctx:
            proxy = self.create_proxy("placeholder", name, (), {}, type_expr=type_expr)
            self.name_to_input[name] = proxy.node
            return proxy

    def new_var(self, name="tmp"):
        existing = set(self.code_options["co_varnames"])
        for i in itertools.count():
            var = f"___{name}_{i}"
            if var not in existing:
                self.code_options["co_varnames"] = self.code_options["co_varnames"] + (
                    var,
                )
                return var

    def update_co_names(self, name):
        """Ensure self.code_options.co_names contains name"""
        if name not in self.code_options["co_names"]:
            self.code_options["co_names"] = tuple(self.code_options["co_names"]) + (
                name,
            )

    def register_attr_or_module(
        self, target: Union[torch.nn.Module, torch.Tensor, Any], *names, **options
    ):
        if is_dynamic_nn_module(target):
            return variables.UnspecializedNNModuleVariable(target, **options)

        options = dict(options)
        options["guards"] = set(options.get("guards", []))
        source: Source = options.get("source", None)
        if isinstance(target, torch.Tensor):
            if source:
                options["guards"].add(source.make_guard(GuardBuilder.TENSOR_MATCH))

            def wrap_name(module_key):
                return wrap_fx_proxy(
                    self,
                    self.create_proxy("get_attr", module_key, tuple(), {}),
                    example_value=target,
                    **options,
                )

        elif isinstance(target, torch.nn.Module):
            assert isinstance(target, torch.nn.Module)
            options["guards"].add(source.make_guard(GuardBuilder.NN_MODULE))

            def wrap_name(module_key):
                return NNModuleVariable(type(target), module_key, **options)

        elif isinstance(target, (torch.SymInt, torch.SymFloat)):
            # HACKY CODE REGION BEGIN
            # WE ARE PIGGYBACKING ON EXISTING INFRA TO REGISTER ATTRS
            # This ultimately gets written to self.nn_modules, which is unfortunate
            # Attrs that are tenors and symints and such need to be migrated to have their
            # own storage
            # alas, this is like this for now
            self.intermediary_symbols.update({target.get_pyobj().expr: None})

            def wrap_name(module_key):
                return DynamicShapeVariable.create(
                    self,
                    self.create_proxy("get_attr", module_key, tuple(), {}),
                    dyn_shape=target,
                    **options,
                )

            # HACKY CODE REGION END
        else:

            def wrap_name(module_key):
                self.output.update_co_names(module_key)
                self.root_globals[module_key] = target
                return VariableBuilder(self, ConstantSource(source_name=module_key))(
                    target
                )

        assert self.nn_modules is not None
        for k, v in self.nn_modules.items():
            if v is target:
                # it already exists
                return wrap_name(k)

        # create a new unique name
        name = "_".join(map(str, names))
        # e.g. repalce abc.xyz[123].qkv with abc.xyz_123.qkv
        name = re.sub(r"\[(\d+)\]", r"_\g<1>", name)
        # e.g. replace abc.xyz_123.qkv with abc_xyz_123_qkv
        name = re.sub(r"[^a-zA-Z0-9]", "_", name)

        if not name or not name[0].isalpha():
            name = "sub" + name
        base = name
        for i in itertools.count():
            if name not in self.nn_modules:
                self.nn_modules[name] = target
                return wrap_name(name)
            name = f"{base}_{i}"

        raise AssertionError("unreachable")

    def compile_subgraph(
        self, tx, partial_convert=False, reason: Optional[GraphCompileReason] = None
    ):
        """
        Generate a subgraph to continue execution on user code.
        Automatically restore live variables.
        """
        from .eval_frame import disable

        self.partial_convert = partial_convert
        self.compile_subgraph_reason = reason

        if not all(block.can_restore() for block in tx.block_stack):
            unimplemented("compile_subgraph with block_depth != 0")

        for block in reversed(tx.block_stack):
            block.exit(tx)

        tx.prune_dead_locals()
        stack_values = list(tx.stack)
        assert self.nn_modules is not None
        root = FakeRootModule(self.nn_modules)

        # Add all the local vars to the "stack" so restore at the end
        restore_vars = []
        val_to_names: OrderedDict[
            VariableTracker, List[str]
        ] = collections.OrderedDict()
        if stack_values:
            val_to_names[stack_values[-1]] = list()
        for k, v in tx.symbolic_locals.items():
            if isinstance(v.source, LocalSource) and v.source.name() == k:
                continue  # no need to restore initial state
            if v not in val_to_names:
                val_to_names[v] = list()
            val_to_names[v].append(k)
        for v in val_to_names.keys():
            restore_vars.extend(val_to_names[v])
            stack_values.extend([v] * len(val_to_names[v]))

        # to handle random calls
        if len(tx.random_calls) > 0:
            random_calls_instructions = []
            self.random_values_var = self.new_var("random_values")
            rand_fn_name = unique_id("__gen_rand_values")
            rand_fn = disable(_get_gen_rand_values_fn(tx.random_calls))
            self.install_global(rand_fn_name, rand_fn)
            codegen = PyCodegen(tx, root)
            random_calls_instructions.extend(
                [
                    codegen.create_load_global("random", add=True),
                    codegen.create_load_attr("setstate"),
                    codegen.create_load_const(tx.output.initial_random_state),
                    create_instruction("CALL_FUNCTION", 1),
                ]
            )
            random_calls_instructions.extend(codegen.load_function_name(rand_fn_name))
            random_calls_instructions.extend(
                [
                    create_instruction("CALL_FUNCTION", 0),
                    codegen.create_store(tx.output.random_values_var),
                ]
            )
            self.add_output_instructions(random_calls_instructions)

        if (
            stack_values
            and all(
                not isinstance(
                    v, (UnspecializedNumpyVariable, UnspecializedPythonVariable)
                )
                for v in stack_values
            )
            and all(isinstance(x, TensorVariable) for x in stack_values)
            and len(set(stack_values)) == len(stack_values)
            and self.side_effects.is_empty()
        ):

            # optimization to generate better code in a common case
            self.add_output_instructions(
                self.compile_and_call_fx_graph(tx, list(reversed(stack_values)), root)
                + [create_instruction("UNPACK_SEQUENCE", len(stack_values))]
            )
        else:
            graph_output_var = self.new_var("graph_out")
            pass1 = PyCodegen(tx, root, graph_output_var)
            self.side_effects.codegen_save_tempvars(pass1)
            pass1.foreach(stack_values)
            self.side_effects.codegen_update_mutated(pass1)

            # one more time now that we have established tempvars
            pass2 = PyCodegen(
                tx,
                root,
                graph_output_var,
                tempvars={val: None for val, count in pass1.uses.items() if count > 1},
            )
            self.side_effects.codegen_save_tempvars(pass2)
            pass2.foreach(stack_values)
            self.side_effects.codegen_update_mutated(pass2)

            output = []
            if count_calls(self.graph) != 0 or len(pass2.graph_outputs) != 0:
                output.extend(
                    self.compile_and_call_fx_graph(tx, pass2.graph_output_vars(), root)
                )

                if len(pass2.graph_outputs) != 0:
                    output.append(pass2.create_store(graph_output_var))
                else:
                    output.append(create_instruction("POP_TOP"))
            self.add_output_instructions(output + pass2.get_instructions())

        # restore all the live local vars
        self.add_output_instructions(
            [PyCodegen(tx).create_store(var) for var in reversed(restore_vars)]
        )

    def compile_and_call_fx_graph(self, tx, rv, root):
        """
        Generate code from self.graph and return the Instruction()s to
        call that generated code.
        """
        from .eval_frame import disable

        assert isinstance(rv, list)
        assert isinstance(root, FakeRootModule)
        for output in rv:
            self.guards.update(output.guards)

        self.create_node(
            "output", "output", (self.create_arg(tuple(x.as_proxy() for x in rv)),), {}
        )
        self.remove_unused_graphargs()
        ncalls = count_calls(self.graph)
        counters["stats"]["calls_captured"] += ncalls
        counters["stats"]["fusions_possible"] += ncalls - 1

        if config.dynamic_propagation:
            # free a bit of memory
            for node in self.graph.nodes:
                if "example_value" in node.meta:
                    del node.meta["example_value"]
            self.real_value_cache.clear()

        gm = fx.GraphModule(root, self.graph)
        gm.recompile()
        gm.compile_subgraph_reason = self.compile_subgraph_reason
        name = unique_id("__compiled_fn")

        assert_no_fake_params_or_buffers(gm)
        compiled_fn = self.call_user_compiler(gm)
        compiled_fn = disable(compiled_fn)

        counters["stats"]["unique_graphs"] += 1
        self.install_global(name, compiled_fn)

        try:
            # the call to tabulate can cause a lot of memory to be allocated
            if config.log_level <= logging.INFO:
                log.log(
                    logging.CODE,  # type: ignore[attr-defined]
                    f"TRACED GRAPH\n {name} {gm.forward.__code__.co_filename} {format_graph_tabular(gm.graph)}\n",
                )
        except ImportError:
            log.warning(
                "Unable to print graph: `format_graph_tabular` relies on the library `tabulate`, "
                "which could not be found on this machine. Run `pip "
                "install tabulate` to install the library."
            )

        cg = PyCodegen(tx)
        cg.make_call_generated_code(name)
        return cg.get_instructions()

    def call_user_compiler(self, gm: fx.GraphModule) -> CompiledFn:
        try:
            name = (
                self.compiler_fn.__name__
                if hasattr(self.compiler_fn, "__name__")
                else ""
            )
            _step_logger()(logging.INFO, f"calling compiler function {name}")
            compiler_fn = self.compiler_fn
            if config.verify_correctness:
                compiler_fn = wrap_compiler_fn(compiler_fn)
            compiled_fn = compiler_fn(gm, self.example_inputs())
            _step_logger()(logging.INFO, f"done compiler function {name}")
            assert callable(compiled_fn), "compiler_fn did not return callable"
        except Exception as e:
            compiled_fn = gm.forward
            raise BackendCompilerFailed(self.compiler_fn, e) from e
        return compiled_fn

    def example_inputs(self) -> List[torch.Tensor]:
        result = []
        for arg in self.graphargs:
            result.extend(arg.get_examples())
        return result

    def remove_unused_graphargs(self) -> None:
        for node in reversed(list(self.graph.nodes)):
            if len(list(node.users)) == 0:
                if node.op == "get_attr":
                    self.graph.erase_node(node)
                elif node.op == "call_function" and node.target is operator.getitem:
                    self.graph.erase_node(node)

        expanded_graphargs = []
        for arg in self.graphargs:
            expanded_graphargs.extend([arg] * len(arg))
            arg.uses = 0

        for node, arg in zip(self.graph.nodes, expanded_graphargs):
            assert node.op == "placeholder"
            arg.uses += len(node.users)

        for node, arg in list(zip(self.graph.nodes, expanded_graphargs)):
            if arg.uses == 0:
                if "example_value" in node.meta:
                    del node.meta["example_value"]
                self.graph.erase_node(node)
                self.real_value_cache.pop(node, None)
                self.name_to_input.pop(node.name, None)

        self.graphargs = [arg for arg in self.graphargs if arg.uses > 0]

    def add_output_instructions(self, prefix: List[Instruction]) -> None:
        """
        We call this on the creation of a new compiled subgraph that is inserted
        before user code.
        """
        self.output_instructions.extend(prefix)
        self.should_exit = True

    def install_global(self, name, value) -> None:
        self.cleanups.append(CleanupHook.create(self.root_globals, name, value))

    def cleanup(self) -> None:
        # There is a reference cycle between tracer and OutputGraph, causing
        # some of the tensor objects to be held alive for longer than necessary.

        # Clear cache for conversion of real -> fake tensors
        self.root_tx.fake_mode.fake_tensor_converter = None
        self.root_tx = None

        # Note: generated fx graph will hold a reference to the nn_module,
        # So depending on the backend they may not be released
        self.nn_modules = None

        # Cleanup graphargs
        for graph_arg in self.graphargs:
            graph_arg.erase()

        for node in self.graph.nodes:
            if "example_value" in node.meta:
                del node.meta["example_value"]
        self.real_value_cache.clear()
        self.name_to_input.clear()

    def create_proxy(
        self,
        kind,
        target,
        args,
        kwargs,
        name=None,
        type_expr=None,
        proxy_factory_fn=None,
        current_tx=None,
    ):
        rv = super().create_proxy(
            kind, target, args, kwargs, name, type_expr, proxy_factory_fn
        )

        # append stack trace to fx node
        tx = current_tx if current_tx else self.root_tx

        nn_module_stack = tx.nn_module_stack
        if nn_module_stack:
            rv.node.meta["nn_module_stack"] = nn_module_stack.copy()

        frame_summaries: List[traceback.FrameSummary] = []
        while tx:
            frame_summaries.append(tx.frame_summary())
            tx = getattr(tx, "parent", None)

        # official from_list stub doesn't have new-style type
        msgs = traceback.StackSummary.from_list(frame_summaries).format()  # type: ignore[arg-type]

        # Carry module_stack along with node.stack_trace for reusing stacktrace propagation infra
        nn_module_stack_str = f"Module stack: {nn_module_stack}\n"
        rv.node.stack_trace = nn_module_stack_str + " | ".join(msgs)

        return rv
