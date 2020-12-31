# Here we build a computational graph out of mygrad view operations, and perform
# corresponding views on numpy arrays.
#
# The views are specifically chosen to be size-preserving and element-preserving operations
# so that we can exploit the property that::
#
#     view.backprop(view.data)
#
# from any view node should produce::
#
#     tensor.grad == tensor.data
#
# throughout the whole graph.
#
# E.g.::
#
#     x1 = Tensor([[0., 1., 2.],
#                  [3., 4., 5.],
#                  [6., 7., 8.]])
#
#     x2 = x1.T
#        = Tensor([[0., 3., 6.],
#                  [1., 4., 7.],
#                  [2., 5., 8.]])
#
#     x3 = x2[::-1]
#        = Tensor([[2., 5., 8.],
#                  [1., 4., 7.],
#                  [0., 3., 6.]])
#
# MyGrad designs its views such that the correspondence of a view's data to the base' data
# should indicate the identical relationship between their gradients. Thus setting
#
#     x3.backward(x3.data)
#
# forces x3's gradient to match its data; it follows from the correspondence stated above
# that the same should be true for all other  views associated with the base (x1)

from typing import Callable, List, NamedTuple, Optional, Tuple, TypeVar

import hypothesis.strategies as st
import numpy as np
from hypothesis import settings
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)
from numpy import ndarray
from numpy.testing import assert_equal

import mygrad as mg
from mygrad import Tensor
from tests.utils import clears_mem_state


class Pair(NamedTuple):
    tensor: Tensor
    array: ndarray
    parent_pair: Optional["Pair"]


T = TypeVar("T", ndarray, Tensor)


def check_base(*, child: T, parent: T):
    assert child.base is parent or child.base is parent.base, (
        f"child:\n{child}\nchild.base:\n{child.base}\n\n"
        f"parent:\n{parent}\nparent.base:\n{parent.base}"
    )


def check_pair(pair: Pair):
    """
    Checks:
    - equality
    - view-base relationships
    - memory sharing
    """

    if pair.parent_pair is not None:
        parent = pair.parent_pair
        assert np.shares_memory(pair.tensor, parent.tensor)
        assert np.shares_memory(pair.array, parent.array)

        check_base(child=pair.tensor, parent=parent.tensor)
        check_base(child=pair.array, parent=parent.array)
    else:
        assert pair.tensor.base is None
        assert pair.array.base is None

    assert_equal(
        actual=pair.tensor,
        desired=pair.array,
        err_msg="MyGrad view produced different result than NumPy view",
    )


def einsum(t: T) -> Callable:
    return mg.einsum if isinstance(t, Tensor) else np.einsum


def diagonal(t: T) -> T:
    return einsum(t)("ii->i", t)


view_ops = {
    "identity": lambda x: x[...],
    "horizontal flip": lambda x: x[:, ::-1],
    "vertical flip": lambda x: x[::-1],
    "transpose": lambda x: x.T,
    "einsum view": lambda x: einsum(x)("... -> ...", x),
    "add and remove leading newaxis": lambda x: x[np.newaxis][0],
    "add and remove middle newaxis": lambda x: x[:, np.newaxis, :][:, 0, :],
    "add and remove trailing newaxis": lambda x: x[..., np.newaxis][..., 0],
}

unary_mutation_ops = {
    "x += 2": lambda x: x.__iadd__(2.0),
    "x -= 2": lambda x: x.__isub__(2.0),
    "x /= 3": lambda x: x.__itruediv__(3.0),
    "x *= 3": lambda x: x.__imul__(3.0),
    "x += x": lambda x: x.__iadd__(x),
}

binary_mutation_ops = {
    "x[...] = y": lambda x, y: x.__setitem__(Ellipsis, y),
    "x[0] = y[0]": lambda x, y: x.__setitem__(slice(None, None, -1), y),
    "x += y": lambda x, y: x.__iadd__(y),
    "x += (x + y)": lambda x, y: x.__iadd__(x + y),
    "diag(x)[...] = diag(y)": lambda x, y: diagonal(x).__setitem__(
        Ellipsis, diagonal(y)
    ),
}


@settings(deadline=None)
class ViewGraphCompare(RuleBasedStateMachine):
    """
    This state machine creates tensor-array pairs - in correspondence
    with each other - from view operations. It also manipulates
    tensors/arrays with inplace operations.

    Everywhere the elements/shape of a tensor should match that of its
    corresponding array.

    The cases generated by this state machine exercises MyGrad's view
    semantics, its inplace operation semantics, and the features that
    emerge from their combination. More specifically it assures that:

     - Base-view relationships and memory sharing are consistent with NumPy
     - Mutations affects a tensor, its base, and the base's views
       consistently with NumPy
     - The correspondence between a base's data and a view's data dictates
       the same correspondence between the gradients of the base and view.
    """

    # Stores the tensor from which the base view is created.
    # This enables us to at least check that backprop always
    # reaches this tensor
    static_upstream_tensor: Tensor

    def __init__(self):
        super().__init__()
        # stores the corresponding node/tensor v1, v2, ... as they are
        # created via the unit test (through `create_node` or `fuse_nodes`)
        # `Node` is the naive implementation of `Tensor` that we are checking
        # against
        self.pair_list: List[Pair] = []

        # Stores the tensor from which we will trigger backprop
        self.terminal_tensor: Optional[Tensor] = None

    nodes = Bundle("nodes")

    def track_pair(self, pair: Pair):
        self.pair_list.append(pair)

    @initialize(target=nodes, shape=st.sampled_from([(3, 3), (4, 4)]))
    def create_base(self, shape: Tuple[int, int]) -> Pair:
        """
        Creates an equivalent tensor, array pair.

        These are square, 2D arrays from which we will
        begin to form views and/or perform mutations.
        """
        size = float(np.prod(shape))
        t = mg.arange(size).reshape(shape).copy()
        arr = np.arange(size).reshape(shape).copy()
        self.static_upstream_tensor = t

        pair = Pair(tensor=+t, array=arr, parent_pair=None)
        assert not np.shares_memory(t, arr)

        self.track_pair(pair)
        return pair

    @rule(target=nodes, parent=nodes, op=st.sampled_from(list(view_ops)))
    def create_view(self, parent: Pair, op: str) -> Pair:
        fn = view_ops[op]
        view_pair = Pair(
            tensor=fn(parent.tensor), array=fn(parent.array), parent_pair=parent
        )
        self.track_pair(view_pair)
        return view_pair

    @rule(pair=nodes, op=st.sampled_from(list(unary_mutation_ops)))
    def unary_mutate_pair(self, pair: Pair, op: str):
        fn = unary_mutation_ops[op]
        fn(pair.tensor)
        fn(pair.array)

    @rule(pair1=nodes, pair2=nodes, op=st.sampled_from(list(binary_mutation_ops)))
    def binary_mutate_pair(self, pair1: Pair, pair2: Pair, op: str):
        fn = binary_mutation_ops[op]
        fn(pair1.tensor, pair2.tensor)
        fn(pair1.array, pair2.array)

    @precondition(lambda self: self.terminal_tensor is None)
    @rule(pair=nodes)
    def pick_terminal_tensor(self, pair: Pair):
        self.terminal_tensor = pair.tensor

    @invariant()
    def check_all_nodes(self):
        for pair in self.pair_list:
            check_pair(pair)

    @clears_mem_state
    def teardown(self):
        if self.terminal_tensor is None:
            return

        t = self.terminal_tensor
        # see comment at top of script for explanation
        # of why we set `t.grad = t.data`
        t.backward(t.data)

        for tensor in (p.tensor for p in self.pair_list):
            assert_equal(actual=tensor.grad, desired=tensor.data)
            if tensor.base is not None:
                assert tensor.grad.base is tensor.base.grad

        # any backprop had to involve the static upstream
        # tensor from which the original base-view was created
        assert self.static_upstream_tensor.grad is not None

        # make sure backprop didn't break any relationships or
        # change any value
        self.check_all_nodes()

        # touching any of these tensors should null its gradients
        for tensor in (p.tensor for p in self.pair_list):
            _ = +tensor
            assert tensor.grad is None


TestGraphComparison = ViewGraphCompare.TestCase
