from typing import Callable

import hypothesis.strategies as st
import numpy as np
import pytest
from hypothesis import given
from numpy.testing import assert_allclose, assert_array_equal

import mygrad as mg
from mygrad import Tensor
from tests.custom_strategies import tensors


@pytest.mark.parametrize("inplace_on_view", [True, False])
def test_raising_during_in_place_op_doesnt_corrupt_graph(inplace_on_view: bool):
    x = mg.arange(1.0, 5.0)
    y_base = 2 * x

    y = y_base[...] if inplace_on_view else y_base

    w = y[...]
    with pytest.raises(ValueError):
        y[:2] = y  # shape mismatch

    (2 * w).backward()
    assert (y.base is y_base) if inplace_on_view else (y.base is None)
    assert w.base is y_base
    assert np.shares_memory(w, y)
    assert_allclose(w.grad, 2 * np.ones_like(y))
    assert_allclose(y_base.grad, 2 * np.ones_like(y_base))
    assert_allclose(y.grad, 2 * np.ones_like(y))
    assert_allclose(x.grad, 4 * np.ones_like(y))


@pytest.mark.parametrize("inplace_on_view", [False, True])
@pytest.mark.parametrize("x_constant", [False, True])
@pytest.mark.parametrize("y_constant", [False, True])
def test_inplace_update_propagates_constant_info(
    inplace_on_view: bool, x_constant: bool, y_constant: bool
):
    x = mg.arange(1.0, 5.0, constant=x_constant)
    y = mg.zeros_like(x, constant=y_constant)

    if inplace_on_view:
        x = x[...]

    dangling_view = x[:2]
    assert x.constant is x_constant
    assert dangling_view.constant is x_constant

    x[...] = y

    assert x.constant is (x_constant and y_constant)
    assert dangling_view.constant is x.constant


@pytest.mark.parametrize("inplace_on_view", [True, False])
@pytest.mark.parametrize("constant", [True, False])
def test_in_place_op_propagates_to_views(constant: bool, inplace_on_view: bool):
    x = mg.arange(1.0, 5.0, constant=constant)
    y_base = +x
    y = y_base[...] if inplace_on_view else y_base

    view1 = y[...]
    view2 = view1[...]  # view of view
    y[:2] = -1  # should mutate all views
    assert y_base.base is None
    if inplace_on_view:
        assert y.base is y_base
    assert view1.base is y_base
    assert view2.base is y_base
    assert_array_equal(x, mg.arange(1.0, 5.0))

    assert_array_equal(y_base, [-1.0, -1.0, 3.0, 4.0])
    assert_array_equal(y_base, y)
    assert_array_equal(y_base, view1)
    assert_array_equal(y_base, view2)


@given(tensors(shape=(4,), constant=False))
@pytest.mark.parametrize("inplace_on_view", [True, False])
def test_simple_backprop_from_view_post_upstream_mutation(
    inplace_on_view: bool, x: Tensor
):
    y_base = +x
    y = y_base[...] if inplace_on_view else y_base
    z = y[...]
    y[:2] = 0  # base is mutated
    # downstream view should carry appropriate info
    # for backprop post-mutation
    z.backward()

    assert_array_equal(y, y_base)
    assert_array_equal(z, y_base)
    assert_array_equal(z.grad, np.ones_like(y_base))
    assert_array_equal(y_base.grad, np.ones_like(y_base))
    assert_array_equal(y.grad, np.ones_like(y_base))
    assert_array_equal(x.grad, [0.0, 0.0, 1.0, 1.0])


@given(tensors(shape=(4,), elements=st.floats(-10, 10), constant=False))
@pytest.mark.parametrize("inplace_on_view", [True, False])
def test_mutation_doesnt_corrupt_upstream_op(inplace_on_view: bool, x: Tensor):
    y_base = +x
    y = y_base[...] if inplace_on_view else y_base
    view = y[...]

    # z = x**4
    z = mg.multiply_sequence(x, y, view, view[...])

    y[:2] = 0  # shouldn't change backprop through z

    z.backward()  # dz/dx = 6 * x ** 2

    assert_allclose(z, x.data ** 4)
    assert_array_equal(view, y)
    assert_allclose(z.grad, np.ones_like(y))
    assert_allclose(x.grad, 4 * np.asarray(x) ** 3)

    assert y_base.grad is None
    assert y.grad is None
    assert view.grad is None


@pytest.mark.parametrize("constant", [True, False])
@pytest.mark.parametrize("inplace_on_view", [True, False])
def test_sets_and_restores_writeability(inplace_on_view: bool, constant: bool):
    x = mg.arange(1.0, 5.0, constant=constant)
    y_base = +x
    y = y_base[...] if inplace_on_view else y_base
    y[...] = 0
    assert x.data.flags.writeable is False
    assert y_base.data.flags.writeable is False
    assert y.data.flags.writeable is False
    y.backward()
    assert x.data.flags.writeable is True
    assert y_base.data.flags.writeable is True
    assert y.data.flags.writeable is True


@pytest.mark.parametrize("inplace_on_view", [True, False])
@given(x=tensors(read_only=True))
def test_respects_original_writeability(x: Tensor, inplace_on_view: bool):
    assert x.data.flags.writeable is False
    if inplace_on_view:
        x = x[...]

    with pytest.raises(ValueError):
        x[...] = 0


@pytest.mark.parametrize("constant", [True, False])
@pytest.mark.parametrize("inplace_on_view", [True, False])
def test_respects_disabled_memguard(constant: bool, inplace_on_view: bool):
    x = mg.arange(1.0, 5.0, constant=constant)
    y_base = +x
    y = y_base[...] if inplace_on_view else y_base

    with mg.mem_guard_off:
        y[...] = 0
    assert x.data.flags.writeable is False
    assert y_base.data.flags.writeable is True
    assert y.data.flags.writeable is True
    y.backward()
    assert x.data.flags.writeable is True
    assert y_base.data.flags.writeable is True
    assert y.data.flags.writeable is True


@pytest.mark.parametrize("inplace_on_view", [True, False])
@pytest.mark.parametrize(
    "target_op",
    [
        lambda x: x,  # backprop directly post-setitem var
        lambda x: +x,  # backprop from downstream node
        lambda x: x[...],  # backprop from downstream view
    ],
)
@pytest.mark.parametrize(
    "source_op",
    [
        lambda x: x,  # direct view
        lambda x: +x,  # downstream of view
        lambda x: x[...],  # view of view
    ],
)
@given(num_in_place_updates=st.integers(1, 3))
def test_writing_a_view_with_a_view(
    target_op: Callable[[Tensor], Tensor],
    source_op: Callable[[Tensor], Tensor],
    inplace_on_view: bool,
    num_in_place_updates: int,
):
    x = mg.arange(1.0, 5.0)
    y_base = +x
    y = y_base[...] if inplace_on_view else y_base
    dangling_view = y[...]

    for _ in range(num_in_place_updates):
        # after the first in-place update, any additional
        # should have no further effect
        y[:2] = source_op(y[-2:])  # y = [3, 4, 3, 4]

    proxy_y = target_op(y)

    # output: -1 x2 + 2 x3 + -3 x2 + 4 x3 -> -4 x2 + 6 x3
    ([-1, 2, -3, 4] * proxy_y).sum().backward()

    assert_array_equal(proxy_y.grad, [-1.0, 2.0, -3.0, 4.0])
    assert_array_equal(y_base.grad, [-1.0, 2.0, -3.0, 4.0])
    assert_array_equal(y.grad, [-1.0, 2.0, -3.0, 4.0])
    assert_array_equal(x.grad, [0.0, 0.0, -4.0, 6.0])

    assert_array_equal(y, dangling_view)
    assert dangling_view.base is y_base
    assert dangling_view.grad is None

    dangling_view.clear_graph()  # release memory

    assert x.data.flags.writeable
    assert y_base.data.flags.writeable
    assert y.data.flags.writeable
    assert dangling_view.data.flags.writeable


@given(
    x=tensors(shape=(3, 3), elements=st.floats(-1e6, 1e6), constant=False),
    y=tensors(
        shape=(3,),
        elements=st.floats(-1e6, 1e6),
        constant=False,
        read_only=st.booleans(),
    ),
)
def test_backprop_through_writeable_views(x: Tensor, y: Tensor):
    # this should ultimately leave view_of_x identical to x
    vert_flipped_x = x[::-1]
    vert_horiz_flipped_x = vert_flipped_x[:, ::-1]
    horiz_flipped_x = vert_horiz_flipped_x[::-1]
    view_of_x = horiz_flipped_x[:, ::-1].T.T

    diag_x = mg.einsum("ii->i", view_of_x)  # view of diag of x
    diag_x[...] = y  # set diag of x to be y

    # check that update propagated to views of `x`
    assert_allclose(vert_flipped_x[::-1], x)
    assert_allclose(vert_horiz_flipped_x[::-1][:, ::-1], x)
    assert_allclose(horiz_flipped_x[:, ::-1], x)
    assert_allclose(view_of_x, x)

    assert vert_flipped_x.base is x
    assert vert_horiz_flipped_x.base is x
    assert horiz_flipped_x.base is x
    assert view_of_x.base is x
    assert x.base is None

    assert not x.data.flags.writeable
    assert not diag_x.data.flags.writeable

    # y0**2 + x01*y1 + x02*y2
    (diag_x * x[0]).sum().backward()  # diag times top-row

    assert x.data.flags.writeable
    assert diag_x.data.flags.writeable

    grad_x = np.zeros_like(x)

    # dl/dx =
    # [2y0, y1, y2]
    # [0., x01, 0 ]
    # [0., 0., x02]
    np.einsum("ii->i", grad_x)[:] = x.data[0]
    grad_x[0, 0] = 2 * y.data[0]
    grad_x[0, 1:] = y.data[1:]

    assert_allclose(grad_x, x.grad)

    if not y.constant:
        # dl/dy = 2y0, x01, x02
        grad_y = x.data[0]
        grad_y[0] = grad_y[0] * 2
        assert_allclose(grad_y, y.grad)
    else:
        assert y.grad is None
